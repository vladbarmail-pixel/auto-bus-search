import atexit
import hashlib
import html as html_lib
import json
import math
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

MAX_PRICE = 45000
MIN_YEAR = 2014
MAX_DISTANCE_KM = 300
GDANSK_LAT = 54.3520
GDANSK_LON = 18.6466

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
session = requests.Session()
session.headers.update(HEADERS)

AUTOPLAC_SEARCH_URLS = [
    "https://autoplac.pl/oferty/samochody-osobowe/ford/transit-custom/pomorskie",
    "https://autoplac.pl/oferty/samochody-osobowe/ford/tourneo-custom/pomorskie",
    "https://autoplac.pl/oferty/samochody-osobowe/ford/transit-custom/warminsko-mazurskie",
    "https://autoplac.pl/oferty/samochody-osobowe/ford/tourneo-custom/warminsko-mazurskie",
    "https://autoplac.pl/oferty/samochody-osobowe/ford/transit-custom/kujawsko-pomorskie",
    "https://autoplac.pl/oferty/samochody-osobowe/ford/tourneo-custom/kujawsko-pomorskie",
    "https://autoplac.pl/oferty/samochody-osobowe/ford/transit-custom/zachodniopomorskie",
    "https://autoplac.pl/oferty/samochody-osobowe/ford/tourneo-custom/zachodniopomorskie",
    "https://autoplac.pl/oferty/samochody-osobowe/ford/transit-custom/wielkopolskie",
    "https://autoplac.pl/oferty/samochody-osobowe/ford/tourneo-custom/wielkopolskie",
]

GRATKA_SEARCH_URLS = [
    "https://motogratka.pl/motoryzacja/osobowe/ford/transit-custom/gdansk",
    "https://motogratka.pl/motoryzacja/osobowe/ford/tourneo-custom/gdansk",
    "https://gratka.pl/motoryzacja/osobowe/ford/transit-custom/gdansk",
    "https://gratka.pl/motoryzacja/osobowe/ford/tourneo-custom/gdansk",
]

_pw = None
_browser = None

def _close_browser():
    global _pw, _browser
    try:
        if _browser:
            _browser.close()
    except Exception:
        pass
    try:
        if _pw:
            _pw.stop()
    except Exception:
        pass
    _pw = None
    _browser = None

atexit.register(_close_browser)


def browser_html(url: str) -> str:
    global _pw, _browser
    from playwright.sync_api import sync_playwright
    if _browser is None:
        _pw = sync_playwright().start()
        _browser = _pw.chromium.launch(headless=True)
    context = _browser.new_context(
        locale="pl-PL",
        user_agent=HEADERS["User-Agent"],
        extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
    )
    page = context.new_page()
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1800)
        print(f"[browser] {url} -> HTTP {response.status if response else 'unknown'}")
        return page.content()
    finally:
        context.close()


def fetch(url: str, timeout: int = 25, browser_fallback: bool = False) -> str:
    last = None
    for attempt in range(2):
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code == 200 and r.text:
                return r.text
            last = RuntimeError(f"HTTP {r.status_code}")
        except Exception as exc:
            last = exc
        time.sleep(1 + attempt)
    if browser_fallback:
        return browser_html(url)
    raise RuntimeError(f"{url}: {last}")


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def rx_int(text: str, patterns):
    for p in patterns:
        m = re.search(p, text, flags=re.I)
        if m:
            digits = re.sub(r"\D", "", m.group(1))
            if digits:
                return int(digits)
    return None


def detect_seats(text: str):
    for p in [
        r"\b([89])\s*(?:os\.|osób|osob(?:owy|owa|owe|owych)?|miejsc(?:a)?|miejscowy|miejscowe|foteli)\b",
        r"\b([89])[-\s]?(?:cio[-\s]?)?osob",
        r"\b([89])[-\s]?miejsc",
    ]:
        m = re.search(p, text, flags=re.I)
        if m:
            return int(m.group(1))
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


def jsonld_objects(soup):
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            obj = json.loads(tag.get_text(strip=True))
        except Exception:
            continue
        stack = obj if isinstance(obj, list) else [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                yield cur
                stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
            elif isinstance(cur, list):
                stack.extend(cur)


def first_meta(soup, *keys):
    for attr, name in keys:
        tag = soup.find("meta", attrs={attr: name})
        if tag and tag.get("content"):
            return clean_text(tag["content"])
    return None


def parse_detail_html(raw: str, url: str, source: str) -> dict:
    soup = BeautifulSoup(raw, "html.parser")
    text = clean_text(soup.get_text(" ", strip=True))
    lower = text.lower()

    title = first_meta(soup, ("property", "og:title"), ("name", "twitter:title"))
    if not title:
        h1 = soup.find("h1")
        title = clean_text(h1.get_text(" ", strip=True)) if h1 else ""

    image_url = first_meta(soup, ("property", "og:image"), ("name", "twitter:image"))
    published_at = first_meta(soup, ("property", "article:published_time"), ("property", "article:modified_time"))
    price = year = mileage = seats = power = None
    engine = location = None
    lat = lon = None

    for obj in jsonld_objects(soup):
        if price is None:
            for candidate in [obj.get("price"), (obj.get("offers") or {}).get("price") if isinstance(obj.get("offers"), dict) else None]:
                if candidate is not None:
                    try:
                        price = int(float(str(candidate).replace(",", ".")))
                        break
                    except Exception:
                        pass

        if published_at is None:
            published_at = obj.get("datePosted") or obj.get("datePublished") or obj.get("uploadDate") or obj.get("dateModified")

        if image_url is None:
            img = obj.get("image")
            if isinstance(img, str):
                image_url = img
            elif isinstance(img, list) and img and isinstance(img[0], str):
                image_url = img[0]
            elif isinstance(img, dict):
                image_url = img.get("url") or img.get("contentUrl")

        if year is None:
            for k in ("vehicleModelDate", "productionDate", "releaseDate"):
                m = re.search(r"(20\d{2}|19\d{2})", str(obj.get(k) or ""))
                if m:
                    year = int(m.group(1))
                    break

        if mileage is None:
            odo = obj.get("mileageFromOdometer")
            if isinstance(odo, dict):
                try:
                    mileage = int(float(str(odo.get("value"))))
                except Exception:
                    pass

        if seats is None and obj.get("vehicleSeatingCapacity") is not None:
            try:
                seats = int(obj["vehicleSeatingCapacity"])
            except Exception:
                pass

        geo = obj.get("geo")
        if isinstance(geo, dict):
            lat = lat or geo.get("latitude")
            lon = lon or geo.get("longitude")

        addr = obj.get("address")
        if isinstance(addr, dict) and not location:
            location = ", ".join(x for x in [addr.get("addressLocality"), addr.get("addressRegion")] if x)

    if price is None:
        price = rx_int(text, [r"(\d[\d\s\xa0]{2,})\s*zł", r"Cena\s*[:\-]?\s*(\d[\d\s\xa0]{2,})"])
    if year is None:
        year = rx_int(text, [r"Rok produkcji\s*[:\-]?\s*(20\d{2}|19\d{2})", r"\b(20\d{2}|19\d{2})\b"])
    if mileage is None:
        mileage = rx_int(text, [r"Przebieg\s*[:\-]?\s*([\d\s\xa0.]{3,})\s*km", r"([\d\s\xa0.]{3,})\s*km"])
    if seats is None:
        seats = detect_seats(text)
    power = rx_int(text, [r"Moc(?: silnika)?\s*[:\-]?\s*(\d{2,3})\s*KM", r"\b(\d{2,3})\s*KM\b"])
    cc = rx_int(text, [r"Pojemność(?: silnika| skokowa)?(?: \[cm3\])?\s*[:\-]?\s*([\d\s]{3,5})"])
    if cc:
        engine = f"{cc} cm³"

    if not published_at:
        m = re.search(r"(?:Ost\. aktualizacja ogłoszenia|Ostatnia aktualizacja|Dodano)\s*:?\s*([^|]{5,30})", text, flags=re.I)
        if m:
            published_at = clean_text(m.group(1))

    if not location:
        m = re.search(r"([A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźż .-]{2,50})\s*\((Pomorskie|Warmińsko-Mazurskie|Kujawsko-Pomorskie|Zachodniopomorskie|Wielkopolskie|Mazowieckie)\)", text, flags=re.I)
        if m:
            location = f"{clean_text(m.group(1))}, {m.group(2)}"
        else:
            m = re.search(r"([A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźż .-]{2,50}),\s*(pomorskie|warmińsko-mazurskie|kujawsko-pomorskie|zachodniopomorskie|wielkopolskie|mazowieckie)", text, flags=re.I)
            if m:
                location = f"{clean_text(m.group(1))}, {m.group(2)}"

    distance = haversine_km(GDANSK_LAT, GDANSK_LON, lat, lon) if lat is not None and lon is not None else None

    if source == "AUTOPLAC":
        item_key = url.rstrip("/").split("/")[-1]
    else:
        m = re.search(r"/ob/(\d+)", url)
        item_key = m.group(1) if m else hashlib.sha1(url.encode()).hexdigest()[:16]

    return {
        "id": f"{source.lower()}:{item_key}",
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
        "fuel_lower": lower,
        "condition_lower": lower,
        "distance_km": distance,
        "published_at": published_at,
        "image_url": image_url,
    }


def generic_detail(url: str, source: str) -> dict:
    raw = fetch(url, browser_fallback=True)
    item = parse_detail_html(raw, url, source)
    if item.get("price") is None or item.get("seats") is None or not item.get("title"):
        rendered = browser_html(url)
        item = parse_detail_html(rendered, url, source)
    return item


def matches(item: dict):
    reasons = []
    title = (item.get("title") or "").lower()
    text = item.get("text_lower") or ""

    if "transit custom" not in title and "tourneo custom" not in title:
        if "transit custom" not in text and "tourneo custom" not in text:
            reasons.append("wrong model")
    if item.get("price") is None or item["price"] > MAX_PRICE:
        reasons.append("price missing/>45000")
    if item.get("year") is None or item["year"] < MIN_YEAR:
        reasons.append("year missing/<2014")
    if item.get("seats") not in (8, 9):
        reasons.append("8/9 seats not confirmed")
    if not any(x in text for x in ("diesel", "olej napędowy", "tdci", "ecoblue")):
        reasons.append("diesel not confirmed")

    bad = ("uszkodzony", "uszkodzona", "uszkodzone", "powypadkowy", "powypadkowa", "do naprawy", "po wypadku", "po kolizji")
    if any(x in title for x in bad):
        reasons.append("damaged")

    d = item.get("distance_km")
    if d is not None and d > MAX_DISTANCE_KM:
        reasons.append(">300 km from Gdańsk")
    elif d is None:
        loc = (item.get("location") or "").lower()
        # Conservative fallback when the listing does not publish coordinates.
        if loc and not any(r in loc for r in ("pomorskie", "warmińsko-mazurskie", "kujawsko-pomorskie")):
            reasons.append("distance unknown/outside core region")
    return not reasons, reasons


def extract_autoplac_urls(raw: str):
    text = html_lib.unescape(raw).replace("\\/", "/")
    urls = set()
    for m in re.findall(r'href=["\']([^"\']*/oferta/ford/(?:transit-custom|tourneo-custom)/[^"\']+)["\']', text, flags=re.I):
        urls.add(urljoin("https://autoplac.pl", m.split("?")[0]))
    for m in re.findall(r'https://autoplac\.pl/oferta/ford/(?:transit-custom|tourneo-custom)/[^"\'<>\s]+', text, flags=re.I):
        urls.add(m.split("?")[0])
    return sorted(urls)


def extract_gratka_urls(raw: str):
    text = html_lib.unescape(raw).replace("\\/", "/")
    urls = set()
    for p in [
        r'https://(?:moto)?gratka\.pl/motoryzacja/[^"\'<>\s]+/ob/\d+',
        r'href=["\']([^"\']*/motoryzacja/[^"\']+/ob/\d+)["\']',
    ]:
        for m in re.findall(p, text, flags=re.I):
            base = "https://motogratka.pl" if str(m).startswith("/") else ""
            urls.add(urljoin(base, m).split("?")[0])
    return sorted(urls)


def collect_source(source: str, search_urls, extractor):
    items = []
    successful = 0
    seen_urls = set()
    for search_url in search_urls:
        try:
            raw = fetch(search_url, browser_fallback=(source == "AUTOPLAC"))
            successful += 1
            urls = extractor(raw)
            print(f"[{source}] {search_url} -> {len(urls)} links")
        except Exception as exc:
            print(f"[{source}] search failed {search_url}: {exc}")
            continue

        for url in urls[:30]:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            try:
                item = generic_detail(url, source)
                ok, reasons = matches(item)
                if ok:
                    items.append(item)
                else:
                    print(f"[{source} skip] {url} -> {', '.join(reasons)}")
            except Exception as exc:
                print(f"[{source}] detail failed {url}: {exc}")
            time.sleep(0.05)
    return items, successful


def collect_autoplac():
    return collect_source("AUTOPLAC", AUTOPLAC_SEARCH_URLS, extract_autoplac_urls)


def collect_gratka():
    return collect_source("GRATKA", GRATKA_SEARCH_URLS, extract_gratka_urls)
