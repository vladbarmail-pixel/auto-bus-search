import hashlib
import html as html_lib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
SEEN_FILE = ROOT / "seen.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
FIRST_RUN_SEND = os.getenv("FIRST_RUN_SEND", "false").lower() == "true"

OTOMOTO_URL = os.getenv(
    "OTOMOTO_URL",
    "https://www.otomoto.pl/osobowe/ford/tourneo-custom--transit-custom/seg-minivan/od-2014/gdansk"
    "?search%5Bdist%5D=300"
    "&search%5Bfilter_enum_damaged%5D=0"
    "&search%5Bfilter_enum_fuel_type%5D=diesel"
    "&search%5Bfilter_float_nr_seats%5D%5B0%5D=8"
    "&search%5Bfilter_float_nr_seats%5D%5B1%5D=9"
    "&search%5Bfilter_float_price%3Ato%5D=45000"
    "&search%5Border%5D=created_at_first%3Adesc",
)

OLX_URLS = [
    os.getenv(
        "OLX_TRANSIT_URL",
        "https://www.olx.pl/motoryzacja/samochody/gdansk/q-ford-transit-custom/"
        "?search%5Bdist%5D=300&search%5Border%5D=created_at%3Adesc",
    ),
    os.getenv(
        "OLX_TOURNEO_URL",
        "https://www.olx.pl/motoryzacja/samochody/gdansk/q-ford-tourneo-custom/"
        "?search%5Bdist%5D=300&search%5Border%5D=created_at%3Adesc",
    ),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}

session = requests.Session()
session.headers.update(HEADERS)


def fetch(url: str, timeout: int = 25) -> str:
    last_error = None
    for attempt in range(3):
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code == 200 and r.text:
                return r.text
            last_error = RuntimeError(f"HTTP {r.status_code} for {url}")
        except requests.RequestException as exc:
            last_error = exc
        time.sleep(2 + attempt * 2)
    raise RuntimeError(str(last_error))


def normalize_html(raw: str) -> str:
    raw = html_lib.unescape(raw)
    raw = raw.replace("\\/", "/")
    raw = raw.replace("\\u002F", "/").replace("\\u002f", "/")
    return raw


def extract_listing_urls(raw: str, source: str) -> list[str]:
    text = normalize_html(raw)
    urls = set()

    if source == "OTOMOTO":
        patterns = [
            r'https://www\.otomoto\.pl/osobowe/oferta/[^"\'<>s]+?\.html',
            r'/osobowe/oferta/[^"\'<>s]+?\.html',
        ]
        base = "https://www.otomoto.pl"
    else:
        patterns = [
            r'https://www\.olx\.pl/d/oferta/[^"\'<>s]+?\.html',
            r'/d/oferta/[^"\'<>s]+?\.html',
        ]
        base = "https://www.olx.pl"

    for pat in patterns:
        for match in re.findall(pat, text, flags=re.I):
            url = urljoin(base, match)
            url = url.split("?")[0]
            urls.add(url)

    return sorted(urls)


def first_json_ld(soup: BeautifulSoup):
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.get_text(strip=True))
        except Exception:
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    yield item
        elif isinstance(data, dict):
            yield data


def deep_find_price(obj):
    if isinstance(obj, dict):
        for key in ("price", "lowPrice"):
            if key in obj:
                try:
                    return int(float(str(obj[key]).replace(",", ".")))
                except Exception:
                    pass
        for value in obj.values():
            got = deep_find_price(value)
            if got:
                return got
    elif isinstance(obj, list):
        for value in obj:
            got = deep_find_price(value)
            if got:
                return got
    return None


def rx_int(text: str, patterns: list[str]):
    for p in patterns:
        m = re.search(p, text, flags=re.I)
        if m:
            try:
                return int(re.sub(r"\D", "", m.group(1)))
            except Exception:
                pass
    return None


def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def parse_listing(url: str, source: str) -> dict:
    raw = fetch(url)
    soup = BeautifulSoup(raw, "html.parser")
    page_text = clean_text(soup.get_text(" ", strip=True))
    lower = page_text.lower()

    title = ""
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        title = clean_text(og_title["content"])
    if not title and soup.title:
        title = clean_text(soup.title.get_text(" ", strip=True))

    price = None
    for obj in first_json_ld(soup):
        price = deep_find_price(obj)
        if price:
            break
    if not price:
        price = rx_int(
            page_text,
            [
                r'(\d[\d\s\xa0]{2,})\s*PLN',
                r'Cena(?:\s+brutto)?\s*[:\-]?\s*(\d[\d\s\xa0]{2,})',
            ],
        )

    year = rx_int(page_text, [r'Rok produkcji\s*[:\-]?\s*(20\d{2}|19\d{2})'])
    mileage = rx_int(page_text, [r'Przebieg\s*[:\-]?\s*([\d\s\xa0]{3,})\s*km'])
    seats = rx_int(page_text, [r'Liczba miejsc\s*[:\-]?\s*(\d{1,2})'])
    power = rx_int(page_text, [r'Moc\s*[:\-]?\s*(\d{2,3})\s*KM'])
    engine = None
    m = re.search(r'Pojemność skokowa\s*[:\-]?\s*([\d\s\xa0]{3,5})\s*cm', page_text, flags=re.I)
    if m:
        engine = re.sub(r"\s+", "", m.group(1)) + " cm³"

    location = None
    loc_patterns = [
        r'Znajdź na mapie\s+([^|]{2,80}?)(?:\s{2,}|Kontakt|Prawa konsumentów|$)',
        r'Lokalizacja\s*[:\-]?\s*([^|]{2,80}?)(?:\s{2,}|$)',
    ]
    for p in loc_patterns:
        m = re.search(p, page_text, flags=re.I)
        if m:
            location = clean_text(m.group(1))[:80]
            break

    if not location:
        desc = soup.find("meta", attrs={"name": "description"})
        if desc and desc.get("content"):
            dm = re.search(
                r'(Gdańsk|Gdynia|Sopot|Szczecin|Elbląg|Olsztyn|Bydgoszcz|Toruń|Koszalin|Poznań|Konin|Warszawa)[^,.;]{0,60}',
                desc["content"],
                flags=re.I,
            )
            if dm:
                location = clean_text(dm.group(0))

    item_id = None
    if source == "OTOMOTO":
        m = re.search(r'-ID([A-Za-z0-9]+)\.html', url)
        if m:
            item_id = "otomoto:" + m.group(1)
    else:
        m = re.search(r'-ID([A-Za-z0-9]+)\.html', url)
        if m:
            item_id = "olx:" + m.group(1)
    if not item_id:
        item_id = source.lower() + ":" + hashlib.sha1(url.encode()).hexdigest()[:16]

    return {
        "id": item_id,
        "source": source,
        "url": url,
        "title": title,
        "price": price,
        "year": year,
        "mileage": mileage,
        "seats": seats,
        "power": power,
        "engine": engine,
        "location": location,
        "text_lower": lower,
    }


def matches(item: dict) -> tuple[bool, list[str]]:
    reasons = []
    title_lower = item["title"].lower()
    text = item["text_lower"]

    if "transit custom" not in title_lower and "tourneo custom" not in title_lower:
        if "transit custom" not in text and "tourneo custom" not in text:
            reasons.append("wrong model")

    if item["price"] is not None and item["price"] > 45000:
        reasons.append("price > 45000")

    if item["year"] is not None and item["year"] < 2014:
        reasons.append("year < 2014")

    if item["seats"] is not None and item["seats"] not in (8, 9):
        reasons.append("not 8/9 seats")

    if "benzyna" in text and "diesel" not in text:
        reasons.append("not diesel")

    bad_terms = (
        "uszkodzony",
        "uszkodzona",
        "powypadkowy",
        "powypadkowa",
        "do naprawy",
        "po wypadku",
        "po kolizji",
    )
    if any(term in text for term in bad_terms):
        reasons.append("damaged")

    return (not reasons), reasons


def fmt_num(value):
    return f"{value:,}".replace(",", " ") if isinstance(value, int) else "brak danych"


def make_message(item: dict) -> str:
    source_icon = "🔵" if item["source"] == "OTOMOTO" else "🟢"
    lines = [
        f"{source_icon} {item['source']} — NOWE OGŁOSZENIE",
        "",
        f"🚐 {item['title'] or 'Ford Custom'}",
        f"💰 {fmt_num(item['price'])} zł" if item["price"] else "💰 cena: brak danych",
        f"📅 Rok: {item['year'] or 'brak danych'}",
        f"🛣 Przebieg: {fmt_num(item['mileage'])} km" if item["mileage"] else "🛣 Przebieg: brak danych",
        f"👥 Miejsca: {item['seats'] or 'brak danych'}",
    ]
    if item["engine"] or item["power"]:
        extra = " / ".join(
            x for x in [item["engine"], f"{item['power']} KM" if item["power"] else None] if x
        )
        lines.append(f"⚙️ {extra}")
    if item["location"]:
        lines.append(f"📍 {item['location']}")
    lines.extend(["", f"🔗 {item['url']}"])
    return "\n".join(lines)[:3900]


def telegram_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text[:500]}")


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        return set(data if isinstance(data, list) else [])
    except Exception:
        return set()


def save_seen(seen: set[str]):
    SEEN_FILE.write_text(
        json.dumps(sorted(seen)[-2500:], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def collect() -> list[dict]:
    all_items = []
    sources = [("OTOMOTO", OTOMOTO_URL)] + [("OLX", u) for u in OLX_URLS]

    for source, search_url in sources:
        print(f"[{source}] loading search: {search_url}")
        try:
            raw = fetch(search_url)
            urls = extract_listing_urls(raw, source)
            print(f"[{source}] found {len(urls)} listing URLs")
        except Exception as exc:
            print(f"[{source}] search failed: {exc}", file=sys.stderr)
            continue

        for url in urls[:30]:
            try:
                item = parse_listing(url, source)
                ok, reasons = matches(item)
                if ok:
                    all_items.append(item)
                else:
                    print(f"[skip] {url} -> {', '.join(reasons)}")
            except Exception as exc:
                print(f"[{source}] detail failed {url}: {exc}", file=sys.stderr)
            time.sleep(0.25)

    unique = {}
    for item in all_items:
        unique[item["id"]] = item
    return list(unique.values())


def main():
    items = collect()
    seen = load_seen()
    current_ids = {item["id"] for item in items}
    print(f"Matching listings: {len(items)}; seen: {len(seen)}")

    if not seen and not FIRST_RUN_SEND:
        seen.update(current_ids)
        save_seen(seen)
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            telegram_send(
                f"✅ Monitoring uruchomiony.\n"
                f"Zapisano {len(current_ids)} aktualnych ogłoszeń jako punkt startowy.\n"
                f"Od teraz będę wysyłać tylko nowe oferty z OTOMOTO i OLX."
            )
        return

    new_items = [item for item in items if item["id"] not in seen]

    for item in new_items[:20]:
        telegram_send(make_message(item))
        seen.add(item["id"])
        time.sleep(0.8)

    save_seen(seen)
    print(f"Sent {min(len(new_items), 20)} new listings")


if __name__ == "__main__":
    main()
