"""Live alternance offers for carousel template 3.

Reads the JobShift snapshot table `carousel_alternance_offer` (refreshed
hourly from the jobs database) and attaches a company logo from Welcome to
the Jungle when the listing URL or the company name matches.
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

_COMPANY_RE = re.compile(r"/companies/([^/?#]+)", re.IGNORECASE)
_DROP_PLACE = {
    "fr",
    "france",
    "ile-de-france",
    "île-de-france",
    "nouvelle-aquitaine",
    "auvergne-rhône-alpes",
    "auvergne-rhone-alpes",
    "occitanie",
    "provence-alpes-côte d'azur",
    "provence-alpes-cote d'azur",
    "hauts-de-france",
    "grand est",
    "pays de la loire",
    "bretagne",
    "normandie",
    "centre-val de loire",
    "bourgogne-franche-comté",
    "bourgogne-franche-comte",
}


def load_live_alternance_companies(count: int) -> list[dict]:
    """Return up to `count` real offers. Empty when JobShift is not configured."""
    rows = _fetch_rows(max(count * 4, 16))
    if len(rows) < 3:
        return []
    random.shuffle(rows)
    picked = rows[:count]
    attach_company_logos(picked)
    for company in picked:
        company.pop("_listing", None)
    return picked


def attach_company_logos(companies: list[dict]) -> None:
    """Fill logo_png from the company name (and listing URL when we have one)."""
    groups: dict[str, list[dict]] = {}
    for company in companies:
        if not isinstance(company, dict) or company.get("logo_png"):
            continue
        name = str(company.get("name") or "").strip()
        if not name:
            continue
        groups.setdefault(name.casefold(), []).append(company)
    if not groups:
        return

    def fetch(key: str) -> tuple[str, bytes | None]:
        sample = groups[key][0]
        return key, _logo_bytes(str(sample.get("_listing") or ""), str(sample.get("name") or ""))

    with ThreadPoolExecutor(max_workers=6) as pool:
        for key, logo in pool.map(fetch, list(groups)):
            if not logo:
                continue
            for company in groups[key]:
                company["logo_png"] = logo


def _fetch_rows(limit: int) -> list[dict]:
    base = os.getenv("JOBSHIFT_SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("JOBSHIFT_SUPABASE_ANON_KEY", "").strip()
    if not base or not key:
        return []
    # The jobs table is too large to filter from the bot. A hourly job copies
    # the current alternance offers into this small public table.
    query = urllib.parse.urlencode(
        {
            "select": "company,title,place,salary_min,salary_max,currency,remote,listing_url",
            "limit": str(max(8, min(limit, 40))),
        }
    )
    request = urllib.request.Request(
        f"{base}/rest/v1/carousel_alternance_offer?{query}",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "JobShiftCarousel/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return []
    if not isinstance(raw, list):
        return []

    seen: set[str] = set()
    companies: list[dict] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        name = str(row.get("company") or "").strip()
        title = re.sub(r"\s+", " ", str(row.get("title") or "")).strip(" -–—")
        key_name = name.casefold()
        if not name or not title or key_name in seen:
            continue
        seen.add(key_name)
        place = _clean_place(str(row.get("place") or ""))
        lines = _fact_lines(place, row.get("salary_min"), row.get("salary_max"), row.get("currency"), row.get("remote"))
        companies.append(
            {
                "name": name,
                "role": title,
                "lines": lines,
                "_listing": str(row.get("listing_url") or ""),
            }
        )
    return companies


def _fact_lines(place: str, salary_min, salary_max, currency, remote) -> list[str]:
    lines: list[str] = []
    if place:
        lines.append(f"📍 {place}")
    salary = _salary_line(salary_min, salary_max, currency)
    if salary:
        lines.append(f"💰 {salary}")
    if remote is True:
        lines.append("🏠 Télétravail")
    lines.append("🎓 Alternance")
    return lines


def _salary_line(salary_min, salary_max, currency) -> str:
    low = _money(salary_min, currency)
    high = _money(salary_max, currency)
    if low and high and low != high:
        return f"Salaire : {low} – {high}"
    amount = low or high
    if not amount:
        return ""
    return f"Salaire : {amount}"


def _money(value, currency) -> str:
    if value is None or value == "":
        return ""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return ""
    if amount <= 0:
        return ""
    text = f"{int(round(amount)):,}".replace(",", " ")
    code = str(currency or "EUR").upper()
    if code in {"EUR", "€"}:
        return f"{text} €"
    return f"{text} {code}".strip()


def _clean_place(place: str) -> str:
    parts = [part.strip() for part in place.replace("—", ",").split(",") if part.strip()]
    while parts and parts[-1].casefold() in _DROP_PLACE:
        parts.pop()
    if not parts:
        return ""
    # "Paris, Paris" or "Paris, Île-de-France" → the city.
    if len(parts) >= 2 and parts[0].casefold() == parts[1].casefold():
        return parts[0]
    return parts[0]


_CACHE_DIR = Path(__file__).resolve().parent / "assets" / "logo_cache"
_WIKI_LOCK = threading.Lock()
_WIKI_NEXT = 0.0
# Short names whose Welcome to the Jungle page is not the obvious slug.
_SLUG_ALIASES = {
    "bpce": ["groupe-bpce"],
    "iliad": ["free"],
    "airbus defence": ["airbus"],
}
# Pages whose title is not the short company name, so the logo file can be found.
_WIKI_TITLES = {
    "france travail": "France Travail",
    "securite sociale": "Sécurité sociale en France",
    "caf": "Caisse d'allocations familiales",
    "caisse des depots": "Caisse des dépôts et consignations",
    "vinci": "Vinci (entreprise)",
    "generali": "Generali",
    "generali france": "Generali",
    "intermarche": "Intermarché",
    "e leclerc": "E.Leclerc",
    "enedis": "Enedis",
    "urssaf": "URSSAF",
}
_WIKI_SKIP = ("disambig", "pictogram", "bus-logo", "ancien", "favicon", "icon")


def _logo_bytes(listing_url: str, company_name: str = "") -> bytes | None:
    key = _cache_key(company_name or listing_url)
    cached = _read_cache(key)
    if cached:
        return cached
    for slug in _slug_candidates(company_name, listing_url):
        image = _download(_wttj_logo_url(slug))
        if image:
            _write_cache(key, image)
            return image
    image = _wiki_logo(company_name)
    if image:
        _write_cache(key, image)
    return image


def _cache_key(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.casefold()).strip("-") or "company"


def _read_cache(key: str) -> bytes | None:
    path = _CACHE_DIR / f"{key}.png"
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data or None


def _write_cache(key: str, data: bytes) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (_CACHE_DIR / f"{key}.png").write_bytes(data)
    except OSError:
        return


def _short_name(name: str) -> str:
    return re.split(r"\s*[\(\[]", name or "", maxsplit=1)[0].strip()


def _slug_candidates(name: str, listing_url: str = "") -> list[str]:
    slugs: list[str] = []
    match = _COMPANY_RE.search(listing_url or "")
    if match:
        slugs.append(match.group(1))
    short = _short_name(name)
    texts = [short]
    if short.casefold().endswith(" france"):
        texts.append(short[: -len(" france")].strip())
    for text in texts:
        ascii_name = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
        ascii_name = ascii_name.replace("'", " ").replace("’", " ")
        hyphen = re.sub(r"[^a-z0-9]+", "-", ascii_name.casefold()).strip("-")
        if hyphen and hyphen not in slugs:
            slugs.append(hyphen)
    alias_key = _cache_key(short).replace("-", " ")
    for extra in _SLUG_ALIASES.get(alias_key, []):
        if extra not in slugs:
            slugs.append(extra)
    return slugs[:4]


def _wiki_logo(name: str) -> bytes | None:
    short = _short_name(name)
    title = _WIKI_TITLES.get(_cache_key(short).replace("-", " "), short)
    if not title:
        return None
    data = _wiki_get({
        "action": "query",
        "titles": title,
        "prop": "images",
        "imlimit": "40",
        "redirects": "1",
    })
    page = next(iter((data.get("query") or {}).get("pages", {}).values()), {})
    files = [str(item.get("title") or "") for item in (page.get("images") or []) if isinstance(item, dict)]
    best = _best_logo_file(files, title)
    if not best:
        return None
    info = _wiki_get({
        "action": "query",
        "titles": best,
        "prop": "imageinfo",
        "iiprop": "url",
        "iiurlwidth": "256",
    })
    page = next(iter((info.get("query") or {}).get("pages", {}).values()), {})
    imageinfo = page.get("imageinfo") if isinstance(page, dict) else None
    if not imageinfo:
        return None
    thumb = str(imageinfo[0].get("thumburl") or imageinfo[0].get("url") or "")
    return _download(thumb)


def _best_logo_file(files: list[str], company: str) -> str:
    token = _cache_key(_short_name(company)).split("-")[0]
    logos = []
    for title in files:
        low = title.casefold()
        if "logo" not in low or any(skip in low for skip in _WIKI_SKIP):
            continue
        logos.append(title)
    if not logos:
        return ""
    named = [title for title in logos if token and token in title.casefold()]
    pool = named or logos

    def score(title: str) -> tuple[int, int]:
        years = [int(year) for year in re.findall(r"(?:19|20)\d{2}", title)]
        year = max(years) if years else 2015
        if year < 1990:
            year = 0
        return (year, 1 if title.casefold().endswith(".svg") else 0)

    return max(pool, key=score)


def _wiki_get(params: dict) -> dict:
    global _WIKI_NEXT
    query = urllib.parse.urlencode({**params, "format": "json"})
    request = urllib.request.Request(
        "https://fr.wikipedia.org/w/api.php?" + query,
        headers={"User-Agent": "JobShiftCarousel/1.0 (carousel company logos)"},
    )
    with _WIKI_LOCK:
        for attempt in range(2):
            wait = _WIKI_NEXT - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _WIKI_NEXT = time.monotonic() + 0.6
            try:
                with urllib.request.urlopen(request, timeout=8) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt == 0:
                    _WIKI_NEXT = time.monotonic() + 2.0
                    continue
                return {}
            except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
                return {}
    return {}


def _wttj_logo_url(slug: str) -> str:
    if not slug:
        return ""
    api = "https://api.welcometothejungle.com/api/v1/organizations/" + urllib.parse.quote(slug)
    request = urllib.request.Request(api, headers={"User-Agent": "JobShiftCarousel/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return ""
    org = payload.get("organization") if isinstance(payload, dict) else None
    if not isinstance(org, dict):
        return ""
    logo = org.get("logo") if isinstance(org.get("logo"), dict) else {}
    return str(logo.get("url") or "")


def _download(url: str) -> bytes | None:
    if not url.startswith("https://"):
        return None
    request = urllib.request.Request(url, headers={"User-Agent": "JobShiftCarousel/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            data = response.read(2_000_000)
    except (OSError, urllib.error.URLError, TimeoutError):
        return None
    if not data or data[:8] == b"<!DOCTYPE" or data[:1] == b"<":
        return None
    # Confirm Pillow can open it before handing it to the renderer.
    try:
        from PIL import Image

        with Image.open(BytesIO(data)) as img:
            img.verify()
    except (OSError, ValueError):
        return None
    return data
