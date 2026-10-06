import hashlib
import html as html_lib
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
SEEN_FILE = ROOT / "seen.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
FIRST_RUN_SEND = os.getenv("FIRST_RUN_SEND", "false").lower() == "true"\nTEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"

MAX_PRICE = 45000
MIN_YEAR = 2014
MAX_DISTANCE_KM = 300
GDANSK_LAT = 54.3520
GDANSK_LON = 18.6466

OTOMOTO_URLS = [
    (
        "OTOMOTO",
        "https://www.otomoto.pl/osobowe/ford/transit-custom/od-2014/gdansk"
        "?search%5Bdist%5D=300"
        "&search%5Bfilter_enum_damaged%5D=0"
        "&search%5Bfilter_enum_fuel_type%5D=diesel"
        "&search%5Bfilter_float_nr_seats%5D%5B0%5D=8"
        "&search%5Bfilter_float_nr_seats%5D%5B1%5D=9"
        "&search%5Bfilter_float_price%3Ato%5D=45000"
        "&search%5Border%5D=created_at_first%3Adesc",
    ),
    (
        "OTOMOTO",
        "https://www.otomoto.pl/osobowe/ford/tourneo-custom/od-2014/gdansk"
        "?search%5Bdist%5D=300"
        "&search%5Bfilter_enum_damaged%5D=0"
        "&search%5Bfilter_enum_fuel_type%5D=diesel"
        "&search%5Bfilter_float_nr_seats%5D%5B0%5D=8"
        "&search%5Bfilter_float_nr_seats%5D%5B1%5D=9"
        "&search%5Bfilter_float_price%3Ato%5D=45000"
        "&search%5Border%5D=created_at_first%3Adesc",
    ),
]

OLX_QUERIES = ["Ford Transit Custom", "Ford Tourneo Custom"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}
API_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept-Language": HEADERS["Accept-Language"],
    "Accept": "application/json",
    "Referer": "https://www.olx.pl/",
    "Origin": "https://www.olx.pl",
    "Connection": "keep-alive",
}

session = requests.Session()
session.headers.update(HEADERS)


def fetch(url: str, timeout: int = 25, headers=None) -> str:
    last_error = None
    for attempt in range(3):
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True, headers=headers)
            if r.status_code == 200 and r.text:
                return r.text
            last_error = RuntimeError(f"HTTP {r.status_code} for {url}")
        except requests.RequestException as exc:
            last_error = exc
        time.sleep(2 + attempt * 2)
    raise RuntimeError(str(last_error))


def fetch_with_browser(url: str) -> str:
    from playwright.sync_api import sync_playwright

    print("[OLX browser] launching Chromium")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="pl-PL",
            user_agent=HEADERS["User-Agent"],
            extra_http_headers={
                "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
                "Referer": "https://www.olx.pl/",
            },
        )
        page = context.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            pre = page.locator("body > pre")
            body = pre.inner_text() if pre.count() else page.locator("body").inner_text()
            print(f"[OLX browser] HTTP {response.status if response else 'unknown'}, chars={len(body)}")
            return body
        finally:
            context.close()
            browser.close()


def normalize_html(raw: str) -> str:
    raw = html_lib.unescape(raw)
    raw = raw.replace("\\/", "/")
    raw = raw.replace("\\u002F", "/").replace("\\u002f", "/")
    return raw


def extract_otomoto_urls(raw: str) -> list[str]:
    text = normalize_html(raw)
    urls = set()
    patterns = [
        r'https://www\.otomoto\.pl/osobowe/oferta/[^"\'<>\s]+?\.html',
        r'/osobowe/oferta/[^"\'<>\s]+?\.html',
    ]
    for pat in patterns:
        for match in re.findall(pat, text, flags=re.I):
            urls.add(urljoin("https://www.otomoto.pl", match).split("?")[0])
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


def parse_otomoto_listing(url: str) -> dict:
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
    published_at = None
    image_url = None
    for obj in first_json_ld(soup):
        if price is None:
            price = deep_find_price(obj)
        if published_at is None:
            published_at = obj.get("datePosted") or obj.get("datePublished") or obj.get("uploadDate")
        if image_url is None:
            img = obj.get("image")
            if isinstance(img, str):
                image_url = img
            elif isinstance(img, list) and img:
                image_url = img[0] if isinstance(img[0], str) else None
            elif isinstance(img, dict):
                image_url = img.get("url") or img.get("contentUrl")
    if not price:
        price = rx_int(page_text, [r"(\d[\d\s\xa0]{2,})\s*PLN", r"Cena(?:\s+brutto)?\s*[:\-]?\s*(\d[\d\s\xa0]{2,})"])

    year = rx_int(page_text, [r"Rok produkcji\s*[:\-]?\s*(20\d{2}|19\d{2})"])
    mileage = rx_int(page_text, [r"Przebieg\s*[:\-]?\s*([\d\s\xa0]{3,})\s*km"])
    seats = rx_int(page_text, [r"Liczba miejsc\s*[:\-]?\s*(\d{1,2})"])
    power = rx_int(page_text, [r"Moc\s*[:\-]?\s*(\d{2,3})\s*KM"])

    engine = None
    m = re.search(r"Pojemność skokowa\s*[:\-]?\s*([\d\s\xa0]{3,5})\s*cm", page_text, flags=re.I)
    if m:
        engine = re.sub(r"\s+", "", m.group(1)) + " cm³"

    location = None
    desc = soup.find("meta", attrs={"name": "description"})
    if desc and desc.get("content"):
        dm = re.search(r"(Gdańsk|Gdynia|Sopot|Szczecin|Elbląg|Olsztyn|Bydgoszcz|Toruń|Koszalin|Poznań|Piła|Grudziądz|Iława)[^,.;]{0,60}", desc["content"], flags=re.I)
        if dm:
            location = clean_text(dm.group(0))

    m = re.search(r"-ID([A-Za-z0-9]+)\.html", url)
    item_id = "otomoto:" + (m.group(1) if m else hashlib.sha1(url.encode()).hexdigest()[:16])

    return {
        "id": item_id, "source": "OTOMOTO", "url": url, "title": title,
        "price": price, "year": year, "mileage": mileage, "seats": seats,
        "power": power, "engine": engine, "location": location,
        "text_lower": lower, "distance_km": None,
        "published_at": published_at, "image_url": image_url,
    }


def param_map(offer: dict) -> dict:
    out = {}
    for p in offer.get("params") or []:
        key = p.get("key")
        if key:
            out[key] = p.get("value") or {}
    return out


def value_key(v):
    return str(v.get("key")) if isinstance(v, dict) and v.get("key") is not None else ""


def value_label(v):
    return str(v.get("label")) if isinstance(v, dict) and v.get("label") is not None else ""


def int_from_value(v):
    if isinstance(v, dict):
        for k in ("value", "key", "label"):
            if v.get(k) is not None:
                s = re.sub(r"\D", "", str(v.get(k)))
                if s:
                    try:
                        return int(s)
                    except Exception:
                        pass
    return None


def haversine_km(lat1, lon1, lat2, lon2):
    try:
        lat1, lon1, lat2, lon2 = map(float, (lat1, lon1, lat2, lon2))
    except Exception:
        return None
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def detect_seats(text: str):
    patterns = [
        r"\b([89])\s*(?:os\.|osob(?:owy|owe|owych)?|miejsc(?:a)?|miejscowy|miejscowe)\b",
        r"\b([89])[-\s]?osob",
        r"\b([89])[-\s]?miejsc",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            return int(m.group(1))
    return None


def parse_olx_offer(offer: dict) -> dict:
    params = param_map(offer)
    title = clean_text(offer.get("title") or "")
    description = clean_text(offer.get("description") or "")
    labels = " ".join(value_label(v) + " " + value_key(v) for v in params.values())
    full_text = clean_text(f"{title} {description} {labels}")
    lower = full_text.lower()

    price_v = params.get("price") or {}
    price = None
    if isinstance(price_v, dict):
        try:
            price = int(float(price_v.get("value")))
        except Exception:
            price = int_from_value(price_v)

    year = int_from_value(params.get("year") or {})
    mileage = int_from_value(params.get("milage") or {})
    fuel = (value_key(params.get("petrol") or {}) + " " + value_label(params.get("petrol") or {})).lower()
    condition = (value_key(params.get("condition") or {}) + " " + value_label(params.get("condition") or {})).lower()
    seats = detect_seats(full_text)

    location_obj = offer.get("location") or {}
    city = location_obj.get("city") or {}
    region = location_obj.get("region") or {}
    city_name = city.get("name") if isinstance(city, dict) else None
    region_name = region.get("name") if isinstance(region, dict) else None
    location = ", ".join(x for x in [city_name, region_name] if x) or None

    map_obj = offer.get("map") or {}
    lat = map_obj.get("lat") if isinstance(map_obj, dict) else None
    lon = map_obj.get("lon") if isinstance(map_obj, dict) else None
    if lon is None and isinstance(map_obj, dict):
        lon = map_obj.get("lng")
    distance = haversine_km(GDANSK_LAT, GDANSK_LON, lat, lon) if lat is not None and lon is not None else None

    url = offer.get("url") or ""
    offer_id = offer.get("id")
    item_id = f"olx:{offer_id}" if offer_id else "olx:" + hashlib.sha1(url.encode()).hexdigest()[:16]
    published_at = offer.get("created_time") or offer.get("created_at") or offer.get("published_at")
    image_url = None
    photos = offer.get("photos") or []
    if photos:
        photo = photos[0]
        if isinstance(photo, dict):
            image_url = photo.get("link") or photo.get("url")
            if image_url and "{width}" in image_url:
                image_url = image_url.replace("{width}", "800").replace("{height}", "600")
        elif isinstance(photo, str):
            image_url = photo
    power = int_from_value(params.get("enginepower") or {})
    engine_cc = int_from_value(params.get("enginesize") or {})

    return {
        "id": item_id, "source": "OLX", "url": url, "title": title,
        "price": price, "year": year, "mileage": mileage, "seats": seats,
        "power": power, "engine": f"{engine_cc} cm³" if engine_cc else None,
        "location": location, "text_lower": lower, "fuel_lower": fuel,
        "condition_lower": condition, "distance_km": distance,
        "published_at": published_at, "image_url": image_url,
    }


def matches(item: dict) -> tuple[bool, list[str]]:
    reasons = []
    title_lower = (item.get("title") or "").lower()
    text = item.get("text_lower") or ""

    if "transit custom" not in title_lower and "tourneo custom" not in title_lower:
        if "transit custom" not in text and "tourneo custom" not in text:
            reasons.append("wrong model")
    if item.get("price") is None or item["price"] > MAX_PRICE:
        reasons.append("price missing/>45000")
    if item.get("year") is None or item["year"] < MIN_YEAR:
        reasons.append("year missing/<2014")
    if item.get("seats") not in (8, 9):
        reasons.append("8/9 seats not confirmed")

    fuel = item.get("fuel_lower") or text
    if "diesel" not in fuel and "olej napędowy" not in fuel:
        reasons.append("diesel not confirmed")

    # OLX Poland exposes the technical condition explicitly:
    # uzywane/nowe = OK, uszkodzone = damaged. Do not scan the whole
    # description for the word "uszkodzony", because OLX boilerplate can
    # mention that word even for an undamaged vehicle.
    condition = item.get("condition_lower") or ""
    title_damage_terms = (
        "uszkodzony", "uszkodzona", "uszkodzone",
        "powypadkowy", "powypadkowa", "do naprawy",
        "po wypadku", "po kolizji",
    )
    if "uszkodzone" in condition or any(term in title_lower for term in title_damage_terms):
        reasons.append("damaged")

    distance = item.get("distance_km")
    if distance is not None and distance > MAX_DISTANCE_KM:
        reasons.append(">300 km from Gdańsk")
    return (not reasons), reasons


def collect_otomoto() -> tuple[list[dict], int]:
    items = []
    successful_searches = 0
    for source, search_url in OTOMOTO_URLS:
        print(f"[{source}] loading search: {search_url}")
        try:
            raw = fetch(search_url)
            successful_searches += 1
            urls = extract_otomoto_urls(raw)
            print(f"[{source}] found {len(urls)} listing URLs")
        except Exception as exc:
            print(f"[{source}] search failed: {exc}", file=sys.stderr)
            continue
        for url in urls[:40]:
            try:
                item = parse_otomoto_listing(url)
                ok, reasons = matches(item)
                if ok:
                    items.append(item)
                else:
                    print(f"[skip] {url} -> {', '.join(reasons)}")
            except Exception as exc:
                print(f"[{source}] detail failed {url}: {exc}", file=sys.stderr)
            time.sleep(0.15)
    return items, successful_searches


def collect_olx() -> tuple[list[dict], int]:
    items = []
    successful_searches = 0
    for query in OLX_QUERIES:
        params = {
            "category_id": 84, "limit": 50, "offset": 0,
            "sort_by": "created_at:desc", "query": query,
            "filter_float_year:from": MIN_YEAR,
            "filter_float_price:to": MAX_PRICE,
        }
        url = "https://www.olx.pl/api/v1/offers/?" + urlencode(params)
        print(f"[OLX API] loading: {query}")
        try:
            try:
                raw = fetch(url, headers=API_HEADERS)
            except Exception as direct_exc:
                print(f"[OLX API] direct blocked: {direct_exc}; trying Chromium")
                raw = fetch_with_browser(url)
            data = json.loads(raw)
            offers = data.get("data") or []
            successful_searches += 1
            print(f"[OLX API] {query}: {len(offers)} raw offers")
        except Exception as exc:
            print(f"[OLX API] {query} failed after Chromium fallback: {exc}", file=sys.stderr)
            continue

        for offer in offers:
            try:
                item = parse_olx_offer(offer)
                ok, reasons = matches(item)
                if ok:
                    items.append(item)
                else:
                    print(f"[OLX skip] {item.get('url')} -> {', '.join(reasons)}")
            except Exception as exc:
                print(f"[OLX] offer parse failed: {exc}", file=sys.stderr)
    return items, successful_searches


def fmt_num(value):
    return f"{value:,}".replace(",", " ") if isinstance(value, int) else "brak danych"


def format_published(value) -> str | None:
    if not value:
        return None
    try:
        from datetime import datetime, timezone
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Poland: UTC+2 on the current October runs; zoneinfo handles DST automatically.
        from zoneinfo import ZoneInfo
        dt = dt.astimezone(ZoneInfo("Europe/Warsaw"))
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(value)


def make_message(item: dict) -> str:
    source_icon = "🔵" if item["source"] == "OTOMOTO" else "🟢"
    lines = [
        f"{source_icon} {item['source']} — NOWE OGŁOSZENIE", "",
        f"🚐 {item['title'] or 'Ford Custom'}",
        f"💰 {fmt_num(item['price'])} zł" if item["price"] else "💰 cena: brak danych",
        f"📅 Rok: {item['year'] or 'brak danych'}",
        f"🛣 Przebieg: {fmt_num(item['mileage'])} km" if item["mileage"] else "🛣 Przebieg: brak danych",
        f"👥 Miejsca: {item['seats'] or 'brak danych'}",
    ]
    published = format_published(item.get("published_at"))
    if published:
        lines.append(f"🕒 Opublikowano: {published}")
    if item["engine"] or item["power"]:
        extra = " / ".join(x for x in [item["engine"], f"{item['power']} KM" if item["power"] else None] if x)
        lines.append(f"⚙️ {extra}")
    if item["location"]:
        lines.append(f"📍 {item['location']}")
    if item.get("distance_km") is not None:
        lines.append(f"📏 ~{round(item['distance_km'])} km od Gdańska")
    lines.extend(["", f"🔗 {item['url']}"])
    return "\n".join(lines)[:3900]


def telegram_send(text: str, item: dict | None = None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    image_url = item.get("image_url") if item else None
    if image_url:
        photo_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        r = requests.post(
            photo_url,
            json={"chat_id": TELEGRAM_CHAT_ID, "photo": image_url, "caption": text[:1024]},
            timeout=25,
        )
        if r.ok:
            return
        print(f"[Telegram] sendPhoto failed ({r.status_code}); falling back to text")

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": False}, timeout=20)
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
    SEEN_FILE.write_text(json.dumps(sorted(seen)[-2500:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def collect() -> list[dict]:
    otomoto_items, otomoto_ok = collect_otomoto()
    olx_items, olx_ok = collect_olx()
    print(f"Source health: OTOMOTO searches OK={otomoto_ok}/{len(OTOMOTO_URLS)}, OLX API searches OK={olx_ok}/{len(OLX_QUERIES)}")
    if otomoto_ok == 0 and olx_ok == 0:
        raise RuntimeError("Both OTOMOTO and OLX are unavailable")
    unique = {}
    for item in otomoto_items + olx_items:
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
                "✅ Monitoring uruchomiony.\n"
                f"Zapisano {len(current_ids)} aktualnych pasujących ogłoszeń jako punkt startowy.\n"
                "Źródła: OTOMOTO + OLX.\n"
                "Od teraz wysyłam tylko nowe oferty spełniające filtry."
            )
        return

    new_items = [item for item in items if item["id"] not in seen]
    for item in new_items[:20]:
        telegram_send(make_message(item), item)
        seen.add(item["id"])
        time.sleep(0.8)
    save_seen(seen)
    print(f"Sent {min(len(new_items), 20)} new listings")


if __name__ == "__main__":
    main()
