#!/usr/bin/env python3
"""Build an RSS feed for Turkish fuel-price news and confirmed price changes.

Data sources
------------
* Petrol Ofisi's public pump-price table: confirmed list prices (VAT included).
* Curated Turkish energy RSS feeds (default: Enerji Günlüğü).

The script uses only the Python standard library, so it can run from cron or a
GitHub Actions workflow without installing anything.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import logging
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__version__ = "1.0.0"

LOG = logging.getLogger("yakit-rss")

ATOM_NS = "http://www.w3.org/2005/Atom"
ET.register_namespace("atom", ATOM_NS)

ROW_RE = re.compile(
    r'<tr\b[^>]*\bdata-(?:dis)?ctrict-name="([^"]*)"[^>]*>(.*?)</tr>',
    re.IGNORECASE | re.DOTALL,
)
CELL_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
WITH_TAX_RE = re.compile(
    r'<span[^>]*class="[^"]*with-tax[^"]*"[^>]*>\s*([^<]+?)\s*</span>',
    re.IGNORECASE | re.DOTALL,
)
QUESTION_RE = re.compile(
    r"(var mi|ne kadar|kac tl|kac lira|kac para|mi geldi|mu geldi|"
    r"geldi mi|geliyor mu|mi geliyor|mu geliyor|olacak mi|mi olacak|"
    r"gelecek mi|gelir mi|olur mu|bekleniyor mu|yansir mi|"
    r"ne olacak|ne zaman|son durum|guncel fiyat|fiyatlar ne)"
)

# product key -> (human label, column heading on the Petrol Ofisi page)
PRODUCT_COLUMNS: dict[str, tuple[str, str]] = {
    "benzin": ("Benzin", "V/Max Kurşunsuz 95"),
    "motorin": ("Motorin", "V/Max Diesel"),
    "lpg": ("LPG", "PO/gaz Otogaz"),
    "gazyagi": ("Gazyağı", "Gazyağı"),
    "kalorifer": ("Kalorifer Yakıtı", "Kalorifer Yakıtı"),
    "fuel_oil": ("Fuel Oil", "Fuel Oil"),
}

TR_PROVINCE_NAMES = [
    "Adana",
    "Adıyaman",
    "Afyonkarahisar",
    "Ağrı",
    "Aksaray",
    "Amasya",
    "Ankara",
    "Antalya",
    "Ardahan",
    "Artvin",
    "Aydın",
    "Balıkesir",
    "Bartın",
    "Batman",
    "Bayburt",
    "Bilecik",
    "Bingöl",
    "Bitlis",
    "Bolu",
    "Burdur",
    "Bursa",
    "Çanakkale",
    "Çankırı",
    "Çorum",
    "Denizli",
    "Diyarbakır",
    "Düzce",
    "Edirne",
    "Elazığ",
    "Erzincan",
    "Erzurum",
    "Eskişehir",
    "Gaziantep",
    "Giresun",
    "Gümüşhane",
    "Hakkâri",
    "Hatay",
    "Iğdır",
    "Isparta",
    "İstanbul",
    "İzmir",
    "Kahramanmaraş",
    "Karabük",
    "Karaman",
    "Kars",
    "Kastamonu",
    "Kayseri",
    "Kırıkkale",
    "Kırklareli",
    "Kırşehir",
    "Kilis",
    "Kocaeli",
    "Konya",
    "Kütahya",
    "Malatya",
    "Manisa",
    "Mardin",
    "Mersin",
    "Muğla",
    "Muş",
    "Nevşehir",
    "Niğde",
    "Ordu",
    "Osmaniye",
    "Rize",
    "Sakarya",
    "Samsun",
    "Siirt",
    "Sinop",
    "Sivas",
    "Şanlıurfa",
    "Şırnak",
    "Tekirdağ",
    "Tokat",
    "Trabzon",
    "Tunceli",
    "Uşak",
    "Van",
    "Yalova",
    "Yozgat",
    "Zonguldak",
]

TR_CITY_ALIASES = {
    "afyon": "Afyonkarahisar",
}

MAX_CITY_ITEMS = 12
POPULAR_CITY_SLUGS = ["istanbul-avrupa", "istanbul-anadolu", "ankara", "izmir"]

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "city": "ISTANBUL (AVRUPA)",
    "city_label": "İstanbul (Avrupa)",
    "cities": [],
    "products": ["benzin", "motorin", "lpg"],
    "price_source_url": "https://www.petrolofisi.com.tr/en/fuels-prices",
    "news_enabled": True,
    "news_sources": [
        {
            "name": "Enerji Günlüğü",
            "url": "https://www.enerjigunlugu.net/rss/",
        },
        {
            "name": "Dünya",
            "url": "https://www.dunya.com/rss",
        },
        {
            "name": "Bloomberg HT",
            "url": "https://www.bloomberght.com/rss",
        },
    ],
    "news_filter": "changes",
    "news_max_age_days": 3,
    "max_items": 80,
    "state_file": "state.json",
    "page_output": "docs/index.html",
    "page_url": "",
    "timezone": "Europe/Istanbul",
    "timeout_seconds": 30,
    "user_agent": "yakit-rss/1.0 (personal RSS feed)",
    "prediction_enabled": True,
    "prediction_in_feed": True,
    "prediction_pass_through": 1.0,
    "prediction_vat": 0.20,
    "prediction_min_delta_tl": 0.25,
    "prediction_products": ["benzin", "motorin"],
    "market_history_days": 30,
    "market_user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "esel_note": (
        "Eşel mobil sistemi 1 Ekim 2026 itibarıyla sona erdi. "
        "Tahminler, uluslararası ürün maliyetindeki değişimin KDV dahil "
        "tamamen pompaya yansıdığı varsayımıyla hesaplanır."
    ),
}


class ConfigError(Exception):
    """The user's configuration cannot be used."""


class SourceError(Exception):
    """A remote source returned something we could not understand."""


@dataclass(frozen=True)
class PriceRow:
    name: str
    prices: dict[str, float]


@dataclass(frozen=True)
class NewsItem:
    title: str
    link: str
    published: datetime
    publisher: str
    publisher_url: str


@dataclass(frozen=True)
class CityInfo:
    name: str
    label: str
    slug: str


# ---------------------------------------------------------------------------
# Configuration and small helpers
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an RSS feed for Turkish fuel prices and announcements."
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="config file (default: config.json next to this script)",
    )
    parser.add_argument("--city", help="price-table city, e.g. 'ANKARA' or 'ISTANBUL (AVRUPA)'")
    parser.add_argument("--city-label", help="display name used in feed titles")
    parser.add_argument("--state", help="state file path")
    parser.add_argument("--max-items", type=int, help="maximum number of items in the feed")
    parser.add_argument("--no-news", action="store_true", help="do not fetch curated news feeds")
    parser.add_argument(
        "--all-news",
        action="store_true",
        help="include all fuel headlines, not only increase/decrease stories",
    )
    parser.add_argument(
        "--no-predictions",
        action="store_true",
        help="do not fetch market data or generate price predictions",
    )
    parser.add_argument("--page-output", help="overview HTML output path")
    parser.add_argument("--list-cities", action="store_true", help="list city names and exit")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report without writing files")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )


def load_config(path: Path, *, required: bool = True) -> tuple[dict[str, Any], Path]:
    cfg = dict(DEFAULT_CONFIG)
    base = Path.cwd()
    if path.exists():
        base = path.resolve().parent
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Could not read {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must contain a JSON object")
        unknown = sorted(set(raw) - set(DEFAULT_CONFIG))
        if unknown:
            LOG.warning("Ignoring unknown config keys: %s", ", ".join(unknown))
        cfg.update({key: value for key, value in raw.items() if key in DEFAULT_CONFIG})
    elif required:
        raise ConfigError(f"Config file not found: {path}")
    else:
        LOG.warning("Config file %s not found; using built-in defaults", path)
    return cfg, base


def apply_cli_overrides(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    if args.city:
        cfg["city"] = args.city
    if args.city_label:
        cfg["city_label"] = args.city_label
    if args.page_output:
        cfg["page_output"] = args.page_output
    if args.state:
        cfg["state_file"] = args.state
    if args.max_items is not None:
        cfg["max_items"] = args.max_items
    if args.no_news:
        cfg["news_enabled"] = False
    if args.all_news:
        cfg["news_filter"] = "all"
    if args.no_predictions:
        cfg["prediction_enabled"] = False


def validate_config(cfg: dict[str, Any]) -> None:
    if not isinstance(cfg.get("city"), str) or not cfg["city"].strip():
        raise ConfigError("'city' must be a non-empty string")
    if not isinstance(cfg.get("city_label"), str):
        raise ConfigError("'city_label' must be a string")
    cities = cfg.get("cities")
    if not isinstance(cities, list) or not all(isinstance(city, str) for city in cities):
        raise ConfigError("'cities' must be a list of city names")
    cfg["cities"] = list(dict.fromkeys(city.strip() for city in cities if city.strip()))

    products = cfg.get("products")
    if not isinstance(products, list) or not products:
        raise ConfigError("'products' must be a non-empty list")
    if not all(isinstance(product, str) for product in products):
        raise ConfigError("'products' must contain strings")
    invalid = [product for product in products if product not in PRODUCT_COLUMNS]
    if invalid:
        raise ConfigError(
            f"Unknown product(s): {', '.join(invalid)}. "
            f"Choose from: {', '.join(PRODUCT_COLUMNS)}"
        )
    cfg["products"] = list(dict.fromkeys(products))

    if cfg.get("news_filter") not in {"changes", "all"}:
        raise ConfigError("'news_filter' must be 'changes' or 'all'")

    news_sources = cfg.get("news_sources")
    if not isinstance(news_sources, list):
        raise ConfigError("'news_sources' must be a list")
    validated_sources: list[dict[str, str]] = []
    for source in news_sources:
        if not isinstance(source, dict):
            raise ConfigError("Each news source must be an object with 'name' and 'url'")
        name = source.get("name")
        url = source.get("url")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("Each news source needs a non-empty 'name'")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ConfigError(f"News source {name!r} needs an http(s) 'url'")
        validated_sources.append({"name": name.strip(), "url": url.strip()})
    if cfg.get("news_enabled") and not validated_sources:
        raise ConfigError("'news_sources' cannot be empty while news is enabled")
    cfg["news_sources"] = validated_sources

    for key in (
        "price_source_url",
        "state_file",
        "page_output",
        "timezone",
        "user_agent",
        "market_user_agent",
        "esel_note",
    ):
        if not isinstance(cfg.get(key), str) or not cfg[key].strip():
            raise ConfigError(f"'{key}' must be a non-empty string")

    for key, low, high in (
        ("news_max_age_days", 1, 30),
        ("max_items", 10, 500),
        ("timeout_seconds", 5, 120),
        ("market_history_days", 5, 90),
    ):
        value = cfg.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ConfigError(f"'{key}' must be an integer between {low} and {high}")

    for key in ("prediction_enabled", "prediction_in_feed"):
        if not isinstance(cfg.get(key), bool):
            raise ConfigError(f"'{key}' must be true or false")
    for key, low, high in (
        ("prediction_pass_through", 0.0, 1.5),
        ("prediction_vat", 0.0, 0.5),
        ("prediction_min_delta_tl", 0.0, 10.0),
    ):
        value = cfg.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"'{key}' must be a number between {low} and {high}")
        if not low <= float(value) <= high:
            raise ConfigError(f"'{key}' must be a number between {low} and {high}")
        cfg[key] = float(value)

    prediction_products = cfg.get("prediction_products")
    if not isinstance(prediction_products, list) or not prediction_products:
        raise ConfigError("'prediction_products' must be a non-empty list")
    if not all(isinstance(product, str) for product in prediction_products):
        raise ConfigError("'prediction_products' must contain strings")
    unsupported = [
        product for product in prediction_products if product not in {"benzin", "motorin"}
    ]
    if unsupported:
        raise ConfigError(
            "'prediction_products' currently supports only 'benzin' and 'motorin'; "
            f"got: {', '.join(unsupported)}"
        )
    missing_products = [
        product for product in prediction_products if product not in cfg["products"]
    ]
    if missing_products:
        raise ConfigError(
            f"Prediction product(s) missing from 'products': {', '.join(missing_products)}"
        )
    cfg["prediction_products"] = list(dict.fromkeys(prediction_products))

    page_url = cfg.get("page_url", "")
    if page_url and not str(page_url).startswith(("http://", "https://")):
        raise ConfigError("'page_url' must start with http:// or https://")


def resolve_path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def get_timezone(name: str) -> Any:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        LOG.warning("Timezone %r is not available; falling back to UTC+3", name)
        return timezone(timedelta(hours=3), name="TRT")


def fold_text(value: str) -> str:
    """Case/diacritic fold Turkish text for matching ('İSTANBUL' == 'istanbul')."""
    value = value.replace("İ", "I").replace("ı", "i")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.casefold()
    return re.sub(r"\s+", " ", value).strip()


TR_PROVINCE_BY_FOLD = {fold_text(name): name for name in TR_PROVINCE_NAMES}
TR_PROVINCE_BY_FOLD.update(
    {fold_text(alias): name for alias, name in TR_CITY_ALIASES.items()}
)


def city_display_name(raw: str) -> str:
    base = raw.split(" (", 1)[0].strip()
    suffix = ""
    upper = raw.upper()
    if "(AVRUPA)" in upper:
        suffix = " (Avrupa)"
    elif "(ANADOLU)" in upper:
        suffix = " (Anadolu)"
    label = TR_PROVINCE_BY_FOLD.get(fold_text(base), base.title())
    return label + suffix


def city_slug(raw: str) -> str:
    slug = fold_text(city_display_name(raw)).replace("(", "").replace(")", "")
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return slug or "city"


def city_label_for(cfg: dict[str, Any], name: str) -> str:
    if fold_text(name) == fold_text(cfg["city"]):
        return cfg["city_label"] or city_display_name(name)
    return city_display_name(name)


def select_city_infos(rows: list[PriceRow], cfg: dict[str, Any]) -> list[CityInfo]:
    """Resolve config cities to price-table rows, always including the default."""
    if cfg["cities"]:
        selected = [find_city(rows, name) for name in cfg["cities"]]
    else:
        selected = list(rows)

    default_row = find_city(rows, cfg["city"])
    cfg["city"] = default_row.name
    by_name = {row.name: row for row in selected}
    by_name[default_row.name] = default_row

    infos: list[CityInfo] = []
    used_slugs: set[str] = set()
    for row in by_name.values():
        slug = city_slug(row.name)
        if slug in used_slugs:
            slug = f"{slug}-{len(used_slugs) + 1}"
        used_slugs.add(slug)
        infos.append(
            CityInfo(
                name=row.name,
                label=city_label_for(cfg, row.name),
                slug=slug,
            )
        )
    infos.sort(key=lambda info: fold_text(info.label))
    return infos


def city_infos_from_state(state: dict[str, Any], cfg: dict[str, Any]) -> list[CityInfo]:
    names = list(state.get("prices", {}).keys())
    if cfg["cities"]:
        allowed = {fold_text(city) for city in cfg["cities"]}
        names = [name for name in names if fold_text(name) in allowed]
    if not any(fold_text(name) == fold_text(cfg["city"]) for name in names):
        names.append(cfg["city"])
    if not names:
        names = [cfg["city"]]
    infos = [
        CityInfo(
            name=name,
            label=city_label_for(cfg, name),
            slug=city_slug(name),
        )
        for name in names
    ]
    infos.sort(key=lambda info: fold_text(info.label))
    return infos


def strip_tags(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def tr_number(value: float) -> str:
    """Format a number the Turkish way: 1234.5 -> '1.234,50'."""
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def parse_price_number(raw: str) -> float:
    raw = html.unescape(raw).strip()
    match = re.search(r"\d[\d.,]*", raw)
    if not match:
        raise ValueError(f"no number in {raw!r}")
    number = match.group(0).rstrip(".,")
    if "," in number and "." in number:
        number = number.replace(".", "").replace(",", ".")
    elif "," in number:
        number = number.replace(",", ".")
    value = float(number)
    if not 0 < value < 100_000:
        raise ValueError(f"implausible price {value!r}")
    return round(value, 2)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def http_get(url: str, *, user_agent: str, timeout: int, retries: int = 2) -> bytes:
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.5",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= retries:
                raise SourceError(f"HTTP {exc.code} from {url}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            if attempt >= retries:
                raise SourceError(f"Could not fetch {url}: {exc}") from exc
        time.sleep(1.5 * (attempt + 1))
    raise SourceError(f"Could not fetch {url}: {last_error}")


# ---------------------------------------------------------------------------
# Market data and predictions
# ---------------------------------------------------------------------------

GALLON_LITERS = 3.785411784
BARREL_LITERS = 158.987294928

MARKET_LIMITS: dict[str, tuple[float, float]] = {
    "BZ=F": (20.0, 300.0),
    "RB=F": (0.5, 20.0),
    "HO=F": (0.5, 20.0),
    "TRY=X": (5.0, 300.0),
}


@dataclass(frozen=True)
class MarketSeries:
    symbol: str
    label: str
    price: float
    previous: float | None
    history: list[float]
    currency: str
    market_time: datetime | None
    source: str

    @property
    def change_pct(self) -> float | None:
        if self.previous is None or self.previous <= 0:
            return None
        return (self.price - self.previous) / self.previous * 100.0


@dataclass
class Prediction:
    product: str
    label: str
    current_price: float
    predicted_price: float
    delta: float
    direction: str
    confidence: str
    source: str
    basis_cost: float | None
    current_cost: float | None
    basis_at: str
    note: str


def validate_market_value(symbol: str, value: float) -> None:
    low, high = MARKET_LIMITS.get(symbol, (0.0, 1_000_000.0))
    if not low <= value <= high:
        raise SourceError(f"Implausible {symbol} value: {value!r}")


def parse_yahoo_chart(raw: bytes | str, *, symbol: str, label: str, tz: Any) -> MarketSeries:
    try:
        payload = json.loads(raw)
        result = payload["chart"]["result"][0]
    except (TypeError, ValueError, KeyError, IndexError) as exc:
        raise SourceError(f"Yahoo returned an unexpected response for {symbol}") from exc

    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    quote_sets = (result.get("indicators") or {}).get("quote") or []
    closes = quote_sets[0].get("close") if quote_sets else []
    pairs = [
        (timestamp, float(close))
        for timestamp, close in zip(timestamps, closes or [])
        if isinstance(close, (int, float)) and not isinstance(close, bool)
    ]
    history = [close for _, close in pairs]

    raw_price = meta.get("regularMarketPrice")
    if isinstance(raw_price, (int, float)) and not isinstance(raw_price, bool):
        price = float(raw_price)
    elif history:
        price = history[-1]
    else:
        raise SourceError(f"Yahoo has no price for {symbol}")

    validate_market_value(symbol, price)

    previous: float | None = None
    if history and abs(history[-1] - price) > 0.005:
        previous = history[-1]
        history.append(price)
    elif len(history) >= 2:
        previous = history[-2]
    elif isinstance(meta.get("chartPreviousClose"), (int, float)):
        previous = float(meta["chartPreviousClose"])

    market_time: datetime | None = None
    raw_time = meta.get("regularMarketTime")
    if isinstance(raw_time, (int, float)) and not isinstance(raw_time, bool):
        market_time = datetime.fromtimestamp(raw_time, tz=timezone.utc).astimezone(tz)

    return MarketSeries(
        symbol=symbol,
        label=label,
        price=price,
        previous=previous,
        history=history,
        currency=str(meta.get("currency") or "USD"),
        market_time=market_time,
        source="Yahoo Finance",
    )


def fetch_yahoo_series(
    cfg: dict[str, Any],
    symbol: str,
    label: str,
    tz: Any,
    *,
    days: int = 5,
) -> MarketSeries:
    encoded = urllib.parse.quote(symbol, safe="")
    if days <= 5:
        range_param = "5d"
    elif days <= 30:
        range_param = "1mo"
    else:
        range_param = "3mo"
    last_error: Exception | None = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = (
            f"https://{host}/v8/finance/chart/{encoded}"
            f"?interval=1d&range={range_param}"
        )
        try:
            raw = http_get(
                url,
                user_agent=cfg["market_user_agent"],
                timeout=cfg["timeout_seconds"],
                retries=1,
            )
            return parse_yahoo_chart(raw, symbol=symbol, label=label, tz=tz)
        except (SourceError, UnicodeError) as exc:
            last_error = exc
            LOG.debug("Yahoo %s failed on %s: %s", symbol, host, exc)
    raise SourceError(f"Yahoo {symbol} unavailable: {last_error}")


def parse_tcmb_usd_try(raw: bytes | str, tz: Any) -> MarketSeries:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SourceError("TCMB returned invalid XML") from exc

    node = root.find(".//Currency[@Kod='USD']")
    if node is None:
        raise SourceError("TCMB XML has no USD currency")
    raw_value = node.findtext("ForexSelling") or node.findtext("ForexBuying")
    if not raw_value:
        raise SourceError("TCMB USD rate is empty")
    try:
        value = float(raw_value.replace(",", "."))
    except ValueError as exc:
        raise SourceError(f"Could not parse TCMB USD rate: {raw_value!r}") from exc
    validate_market_value("TRY=X", value)

    market_time: datetime | None = None
    date_text = root.attrib.get("Tarih", "")
    try:
        market_time = datetime.strptime(date_text, "%d.%m.%Y").replace(
            hour=15, minute=30, tzinfo=tz
        )
    except ValueError:
        pass

    return MarketSeries(
        symbol="TRY=X",
        label="USD/TRY",
        price=value,
        previous=None,
        history=[],
        currency="TRY",
        market_time=market_time,
        source="TCMB",
    )


def fetch_tcmb_usd_try(cfg: dict[str, Any], tz: Any) -> MarketSeries:
    raw = http_get(
        "https://www.tcmb.gov.tr/kurlar/today.xml",
        user_agent=cfg["market_user_agent"],
        timeout=cfg["timeout_seconds"],
    )
    return parse_tcmb_usd_try(raw, tz)


def fetch_usd_try(cfg: dict[str, Any], tz: Any) -> MarketSeries:
    try:
        return fetch_yahoo_series(cfg, "TRY=X", "USD/TRY", tz, days=5)
    except SourceError as exc:
        LOG.warning("Yahoo USD/TRY unavailable: %s", exc)

    try:
        return fetch_tcmb_usd_try(cfg, tz)
    except (SourceError, ET.ParseError, ValueError) as exc:
        LOG.warning("TCMB USD/TRY unavailable: %s", exc)

    try:
        raw = http_get(
            "https://api.frankfurter.app/latest?from=USD&to=TRY",
            user_agent=cfg["market_user_agent"],
            timeout=cfg["timeout_seconds"],
        )
        payload = json.loads(raw)
        value = float(payload["rates"]["TRY"])
        validate_market_value("TRY=X", value)
        market_time: datetime | None = None
        try:
            market_time = datetime.fromisoformat(payload["date"]).replace(tzinfo=tz)
        except (KeyError, ValueError):
            pass
        return MarketSeries(
            symbol="TRY=X",
            label="USD/TRY",
            price=value,
            previous=None,
            history=[],
            currency="TRY",
            market_time=market_time,
            source="Frankfurter (ECB)",
        )
    except (SourceError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SourceError(f"No USD/TRY source available: {exc}") from exc


def fetch_market_data(cfg: dict[str, Any], tz: Any) -> dict[str, MarketSeries | None]:
    market: dict[str, MarketSeries | None] = {
        "brent": None,
        "gasoline": None,
        "diesel": None,
        "usdtry": None,
    }
    requests = (
        ("brent", "BZ=F", "Brent", max(25, cfg["market_history_days"])),
        ("gasoline", "RB=F", "RBOB Benzin", 5),
        ("diesel", "HO=F", "ULSD Motorin", 5),
    )
    for key, symbol, label, days in requests:
        try:
            series = fetch_yahoo_series(cfg, symbol, label, tz, days=days)
            market[key] = series
            LOG.info("%s: %.4f %s", label, series.price, series.currency)
        except SourceError as exc:
            LOG.warning("%s unavailable: %s", label, exc)

    try:
        market["usdtry"] = fetch_usd_try(cfg, tz)
        LOG.info(
            "USD/TRY: %.4f (%s)",
            market["usdtry"].price,
            market["usdtry"].source,
        )
    except SourceError as exc:
        LOG.warning("USD/TRY unavailable: %s", exc)
    return market


def market_cost_for_product(
    product: str, market: dict[str, MarketSeries | None]
) -> tuple[float, str, str] | None:
    usdtry_series = market.get("usdtry")
    if not usdtry_series:
        return None
    usdtry = usdtry_series.price

    if product == "benzin":
        series = market.get("gasoline")
        if series:
            return series.price / GALLON_LITERS * usdtry, "RBOB vadeli", "orta"
    elif product == "motorin":
        series = market.get("diesel")
        if series:
            return series.price / GALLON_LITERS * usdtry, "ULSD vadeli", "orta"
    else:
        return None

    brent = market.get("brent")
    if brent:
        return brent.price / BARREL_LITERS * usdtry, "Brent proxy", "düşük"
    return None


def update_prediction_baseline(
    state: dict[str, Any],
    city: str,
    product: str,
    current_price: float,
    market_cost: float,
    source: str,
    now: datetime,
) -> tuple[dict[str, Any], bool]:
    all_baselines = state.setdefault("prediction_baselines", {})
    baselines = all_baselines.setdefault(city, {})
    baseline = baselines.get(product)
    valid_baseline = isinstance(baseline, dict) and baseline.get("source") == source
    if valid_baseline:
        try:
            float(baseline["cost_tl_l"])
            old_price = float(baseline["pump_price"])
        except (KeyError, TypeError, ValueError):
            valid_baseline = False

    reset = not valid_baseline
    if valid_baseline and abs(current_price - old_price) >= 0.005:
        reset = True

    if reset:
        baseline = {
            "pump_price": round(current_price, 2),
            "cost_tl_l": round(market_cost, 4),
            "source": source,
            "basis_at": now.isoformat(),
        }
        baselines[product] = baseline
    return baseline, reset


def compute_prediction(
    cfg: dict[str, Any],
    product: str,
    current_price: float,
    market_cost: float,
    source: str,
    confidence: str,
    baseline: dict[str, Any],
    reset: bool,
    now: datetime,
    price_ok: bool,
) -> Prediction:
    label = PRODUCT_COLUMNS[product][0]
    try:
        basis_cost = float(baseline["cost_tl_l"])
    except (KeyError, TypeError, ValueError):
        basis_cost = market_cost

    delta = (
        (market_cost - basis_cost)
        * (1.0 + cfg["prediction_vat"])
        * cfg["prediction_pass_through"]
    )
    if abs(delta) < 0.005:
        delta = 0.0

    if delta > 0.01:
        direction = "zam"
    elif delta < -0.01:
        direction = "indirim"
    else:
        direction = "sabit"

    if reset:
        note = "Baz alındı; yeni tahmin için piyasa hareketi bekleniyor."
    elif direction == "sabit":
        note = "Baz alındığından bu yana maliyet farkı yok."
    else:
        note = "Baz alındığından bu yana maliyet değişimi."

    if not price_ok:
        note += " Fiyat kaynağı son bilinen değeri gösteriyor."
        confidence = "düşük"

    try:
        basis_dt = datetime.fromisoformat(baseline["basis_at"])
        if basis_dt.tzinfo is None:
            basis_dt = basis_dt.replace(tzinfo=now.tzinfo)
        if now - basis_dt.astimezone(now.tzinfo) > timedelta(days=14):
            confidence = "düşük"
    except (KeyError, TypeError, ValueError):
        pass

    return Prediction(
        product=product,
        label=label,
        current_price=round(current_price, 2),
        predicted_price=round(current_price + delta, 2),
        delta=round(delta, 2),
        direction=direction,
        confidence=confidence,
        source=source,
        basis_cost=round(basis_cost, 4),
        current_cost=round(market_cost, 4),
        basis_at=str(baseline.get("basis_at", "")),
        note=note,
    )


def build_predictions(
    cfg: dict[str, Any],
    state: dict[str, Any],
    city: str,
    current_prices: dict[str, float],
    market: dict[str, MarketSeries | None],
    now: datetime,
    price_ok: bool,
) -> list[Prediction]:
    if not cfg["prediction_enabled"]:
        return []

    predictions: list[Prediction] = []
    for product in cfg["prediction_products"]:
        current_price = current_prices.get(product)
        if current_price is None:
            continue

        cost_info = market_cost_for_product(product, market)
        if cost_info is None:
            predictions.append(
                Prediction(
                    product=product,
                    label=PRODUCT_COLUMNS[product][0],
                    current_price=round(current_price, 2),
                    predicted_price=round(current_price, 2),
                    delta=0.0,
                    direction="sabit",
                    confidence="veri yok",
                    source="—",
                    basis_cost=None,
                    current_cost=None,
                    basis_at="",
                    note="Piyasa verisi alınamadı; tahmin üretilemedi.",
                )
            )
            continue

        market_cost, source, confidence = cost_info
        baseline, reset = update_prediction_baseline(
            state, city, product, current_price, market_cost, source, now
        )
        predictions.append(
            compute_prediction(
                cfg=cfg,
                product=product,
                current_price=current_price,
                market_cost=market_cost,
                source=source,
                confidence=confidence,
                baseline=baseline,
                reset=reset,
                now=now,
                price_ok=price_ok,
            )
        )
    return predictions


def make_prediction_item(
    cfg: dict[str, Any],
    state: dict[str, Any],
    city: str,
    prediction: Prediction,
    market: dict[str, MarketSeries | None],
    now: datetime,
    tz: Any,
    page_url: str,
) -> dict[str, Any] | None:
    if prediction.direction == "sabit":
        return None
    if abs(prediction.delta) < cfg["prediction_min_delta_tl"]:
        return None

    bucket = math.ceil(abs(prediction.delta) * 2) / 2
    key = (
        f"{city}|{prediction.product}|{now.astimezone(tz).date().isoformat()}|"
        f"{prediction.direction}|{bucket:.1f}"
    )
    prediction_items = state.setdefault("prediction_items", {})
    if key in prediction_items:
        return None

    delta_text = f"{prediction.delta:+.2f}".replace(".", ",")
    predicted_text = tr_number(prediction.predicted_price)
    current_text = tr_number(prediction.current_price)
    direction_text = "zam yönünde" if prediction.direction == "zam" else "indirim yönünde"
    title = (
        f"[Tahmin] {prediction.label} · {delta_text} TL {direction_text} "
        f"(tahmini {predicted_text} TL)"
    )

    brent = market.get("brent")
    usdtry = market.get("usdtry")
    basis_at = prediction.basis_at
    try:
        basis_at = datetime.fromisoformat(basis_at).astimezone(tz).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        basis_at = prediction.basis_at or "—"

    description = (
        f"<p><strong>{html.escape(prediction.label)}</strong> için piyasa bazlı "
        f"tahmin: <strong>{delta_text} TL</strong> ({direction_text}).</p>"
        f"<p>Güncel: {current_text} TL → Tahmini: {predicted_text} TL</p>"
        f"<p>Baz maliyet: {tr_number(prediction.basis_cost or 0)} TL/L "
        f"({html.escape(basis_at)})<br>"
        f"Güncel maliyet: {tr_number(prediction.current_cost or 0)} TL/L<br>"
        f"Kaynak: {html.escape(prediction.source)}; "
        f"USD/TRY: {tr_number(usdtry.price) if usdtry else '—'}; "
        f"Brent: {tr_number(brent.price) + ' $' if brent else '—'}</p>"
        "<p><small>Bu bir tahmindir; resmi fiyat değildir. Vergi, marj ve eşel "
        "değişiklikleri model dışıdır.</small></p>"
    )

    link = page_url or cfg["price_source_url"]
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    item = {
        "id": f"prediction:{digest}",
        "title": title,
        "link": link,
        "description": description,
        "published": now.isoformat(),
        "category": "Tahmin",
        "source": "yakit-rss tahmin modeli",
        "source_url": link,
    }
    prediction_items[key] = now.astimezone(tz).isoformat()
    return item


def prune_prediction_items(state: dict[str, Any], now: datetime, tz: Any) -> None:
    cutoff = now - timedelta(days=14)
    for key, value in list(state.get("prediction_items", {}).items()):
        try:
            seen = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            del state["prediction_items"][key]
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=tz)
        if seen < cutoff:
            del state["prediction_items"][key]


# ---------------------------------------------------------------------------
# Petrol Ofisi price table
# ---------------------------------------------------------------------------


def parse_prices(page: str, products: list[str]) -> list[PriceRow]:
    thead = re.search(r"<thead\b[^>]*>(.*?)</thead>", page, re.IGNORECASE | re.DOTALL)
    if not thead:
        raise SourceError("Price page has no <thead>; the page layout may have changed")

    headers = [
        re.sub(r"\s+", " ", strip_tags(cell)).strip()
        for cell in re.findall(r"<th\b[^>]*>(.*?)</th>", thead.group(1), re.IGNORECASE | re.DOTALL)
    ]
    if not headers:
        raise SourceError("Price page has no column headings")

    positions: dict[str, int] = {}
    by_header: dict[str, int] = {}
    for index, header in enumerate(headers):
        by_header.setdefault(fold_text(header), index)
    for product in products:
        column = PRODUCT_COLUMNS[product][1]
        index = by_header.get(fold_text(column))
        if index is None:
            raise SourceError(
                f"Column {column!r} not found on the price page. "
                f"Available columns: {headers}"
            )
        positions[product] = index

    rows: list[PriceRow] = []
    for match in ROW_RE.finditer(page):
        name = html.unescape(match.group(1)).strip()
        cells = CELL_RE.findall(match.group(2))
        if len(cells) < len(headers):
            LOG.debug("Skipping malformed row %r (%d cells)", name, len(cells))
            continue

        prices: dict[str, float] = {}
        valid = True
        for product, index in positions.items():
            cell = cells[index]
            with_tax = WITH_TAX_RE.search(cell)
            raw = with_tax.group(1) if with_tax else strip_tags(cell)
            try:
                prices[product] = parse_price_number(raw)
            except ValueError:
                LOG.debug("Could not parse %s price for %s from %r", product, name, raw)
                valid = False
                break
        if valid:
            rows.append(PriceRow(name=name, prices=prices))

    if not rows:
        raise SourceError("No price rows found; the page layout may have changed")
    LOG.debug("Parsed %d price rows", len(rows))
    return rows


def find_city(rows: list[PriceRow], city: str) -> PriceRow:
    target = fold_text(city)
    exact = [row for row in rows if fold_text(row.name) == target]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        names = ", ".join(row.name for row in exact)
        raise ConfigError(f"City {city!r} is ambiguous: {names}")

    display = [row for row in rows if fold_text(city_display_name(row.name)) == target]
    if len(display) == 1:
        return display[0]
    if len(display) > 1:
        names = ", ".join(row.name for row in display)
        raise ConfigError(f"City {city!r} is ambiguous: {names}")

    prefix = [row for row in rows if fold_text(row.name).startswith(target + " ")]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        names = ", ".join(row.name for row in prefix)
        raise ConfigError(f"City {city!r} matches several rows: {names}")

    folded_names = {fold_text(row.name): row.name for row in rows}
    suggestions = [
        folded_names[match]
        for match in difflib.get_close_matches(target, list(folded_names), n=5, cutoff=0.5)
    ]
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise ConfigError(f"City {city!r} was not found in the price table.{hint}")


# ---------------------------------------------------------------------------
# Curated news feeds
# ---------------------------------------------------------------------------


def parse_feed(raw: bytes | str, source: dict[str, str], tz: Any) -> list[NewsItem]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SourceError(f"{source['name']} returned invalid XML: {exc}") from exc

    now = datetime.now(tz)
    nodes = root.findall(".//{*}item")
    if not nodes:
        nodes = root.findall(".//{*}entry")

    items: list[NewsItem] = []
    for node in nodes:
        title = child_text_any(node, "title")
        if not title:
            continue

        links = node.findall("{*}link")
        link = ""
        for candidate in links:
            href = candidate.attrib.get("href", "").strip()
            if href and candidate.attrib.get("rel", "alternate") == "alternate":
                link = href
                break
        if not link:
            for candidate in links:
                link = candidate.attrib.get("href", "").strip()
                if link:
                    break
        if not link:
            link = child_text_any(node, "link")
        if not link:
            continue

        raw_date = (
            child_text_any(node, "pubDate")
            or child_text_any(node, "published")
            or child_text_any(node, "updated")
            or child_text_any(node, "date")
        )
        published = parse_feed_date(raw_date, fallback=now)
        publisher = source["name"]
        title = clean_news_title(title, publisher)
        items.append(
            NewsItem(
                title=title,
                link=link,
                published=published,
                publisher=publisher,
                publisher_url=source["url"],
            )
        )
    return items


def fetch_news(cfg: dict[str, Any], tz: Any) -> list[NewsItem]:
    all_items: list[NewsItem] = []
    for source in cfg["news_sources"]:
        try:
            raw = http_get(
                source["url"],
                user_agent=cfg["user_agent"],
                timeout=cfg["timeout_seconds"],
            )
            parsed = parse_feed(raw, source, tz)
            LOG.info("News source %s: %d items", source["name"], len(parsed))
            all_items.extend(parsed)
        except (SourceError, ET.ParseError, UnicodeError) as exc:
            LOG.warning("News source %s unavailable: %s", source["name"], exc)

    seen: set[str] = set()
    deduped: list[NewsItem] = []
    for item in all_items:
        key = item.link or fold_text(item.title)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def child_text_any(node: ET.Element, tag: str) -> str:
    child = node.find(f"{{*}}{tag}")
    if child is None:
        child = node.find(tag)
    if child is None:
        return ""
    return html.unescape("".join(child.itertext())).strip()


def parse_feed_date(raw: str, fallback: datetime) -> datetime:
    if not raw:
        return fallback
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def clean_news_title(title: str, publisher: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    if publisher:
        for separator in (" - ", " | ", " – "):
            suffix = separator + publisher
            if title.endswith(suffix):
                return title[: -len(suffix)].rstrip()
    return title


def news_is_relevant(item: NewsItem, mode: str) -> bool:
    text = fold_text(item.title)
    if not re.search(r"(benzin|motorin|mazot|lpg|otogaz|akaryakit)", text):
        return False
    if mode == "all":
        return True
    if not re.search(r"\bzam(?!an|siz)|\bindirim(?!siz)", text):
        return False
    if QUESTION_RE.search(text):
        return False
    return True


def news_fuels(text: str) -> list[str]:
    fuels: list[str] = []
    if "benzin" in text:
        fuels.append("benzin")
    if "motorin" in text or "mazot" in text:
        fuels.append("motorin")
    if "lpg" in text or "otogaz" in text:
        fuels.append("lpg")
    if not fuels and "akaryakit" in text:
        fuels.append("akaryakit")
    return fuels


def news_direction(text: str) -> str:
    has_zam = bool(re.search(r"\bzam(?!an|siz)", text))
    has_indirim = bool(re.search(r"\bindirim(?!siz)", text))
    if has_zam and has_indirim:
        return "both"
    if has_indirim:
        return "indirim"
    if has_zam:
        return "zam"
    return "none"


def news_status(text: str) -> str:
    if "iptal" in text:
        return "iptal"
    if re.search(
        r"(geldi|yansidi|degisti|kesinlesti|ucuzladi|dustu|artti|guncellendi|"
        r"yapildi|uygulandi)",
        text,
    ):
        return "confirmed"
    if re.search(
        r"(geliyor|bekleniyor|yolda|yapilacak|kapida|belli oldu|aciklandi|"
        r"gelecek|dusecek|artacak|yansiyacak|olacak)",
        text,
    ):
        return "expected"
    if re.search(r"(gecerli|yururlukte)", text):
        return "confirmed"
    return "other"


def news_score(item: NewsItem) -> float:
    text = fold_text(item.title)
    score = 0.0
    if re.search(r"\d+(?:[.,]\d+)?\s*(?:tl|lira|kurus)", text):
        score += 2
    elif re.search(r"\d", text):
        score += 0.5
    if 30 <= len(text) <= 120:
        score += 1
    if item.title.isupper():
        score -= 2
    if "son dakika" in text:
        score -= 1
    if text.count("!") > 1:
        score -= 1
    if "..." in text or "…" in text:
        score -= 0.5
    return score


def select_news(
    items: list[NewsItem],
    cfg: dict[str, Any],
    state: dict[str, Any],
    now: datetime,
    tz: Any,
) -> list[tuple[str, NewsItem]]:
    """Filter, cluster and deduplicate headlines.

    Headlines are grouped by fuel + direction + day + status. One representative
    per group is kept, so 20 outlets reporting the same change produce one item.
    """
    cutoff = now - timedelta(days=cfg["news_max_age_days"])
    groups: dict[str, list[NewsItem]] = {}

    for item in items:
        local_time = item.published.astimezone(tz)
        if local_time < cutoff or local_time > now + timedelta(hours=6):
            continue
        if not news_is_relevant(item, cfg["news_filter"]):
            continue

        text = fold_text(item.title)
        fuels = news_fuels(text)
        # The first fuel is the one the headline leads with. Articles that
        # mention benzin/motorin/LPG only because they list prices still belong
        # to the benzin event, so they should cluster together.
        fuel_key = fuels[0] if fuels else "akaryakit"
        # A story published after midnight often reports the previous evening's
        # announcement. Shift the event day back six hours so both sides of
        # midnight land in the same cluster.
        date_key = (local_time - timedelta(hours=6)).date().isoformat()
        base = f"{fuel_key}|{news_direction(text)}|{date_key}"
        key = f"{base}|{news_status(text)}"
        groups.setdefault(key, []).append(item)

    # If the same day has a concrete "expected" or "confirmed" story, drop
    # vague "other" headlines about the same fuel and direction.
    non_other = {key.rsplit("|", 1)[0] for key in groups if not key.endswith("|other")}
    for key in list(groups):
        if key.endswith("|other") and key.rsplit("|", 1)[0] in non_other:
            del groups[key]

    selected: list[tuple[str, NewsItem]] = []
    for key, group in groups.items():
        if key in state.get("news_events", {}):
            continue
        selected.append((key, max(group, key=news_score)))

    selected.sort(key=lambda pair: pair[1].published, reverse=True)
    return selected


# ---------------------------------------------------------------------------
# State and feed items
# ---------------------------------------------------------------------------


def empty_state() -> dict[str, Any]:
    return {
        "version": 2,
        "default_city": "",
        "prices": {},
        "prediction_baselines": {},
        "city_items": {},
        "news_items": [],
        "news_events": {},
        "prediction_items": {},
    }


def migrate_state(state: dict[str, Any]) -> dict[str, Any]:
    """Convert the old single-city state to the multi-city format."""
    old_city = state.get("city", "") if isinstance(state.get("city"), str) else ""
    migrated = empty_state()
    migrated["default_city"] = old_city

    old_prices = state.get("prices", {})
    if old_city and isinstance(old_prices, dict):
        migrated["prices"][old_city] = old_prices

    old_baselines = state.get("prediction_baselines", {})
    if old_city and isinstance(old_baselines, dict):
        migrated["prediction_baselines"][old_city] = old_baselines

    old_items = state.get("items", [])
    if isinstance(old_items, list):
        news_items = [
            item
            for item in old_items
            if isinstance(item, dict) and item.get("category") == "Haber"
        ]
        city_items = [
            item
            for item in old_items
            if isinstance(item, dict) and item.get("category") != "Haber"
        ]
        migrated["news_items"] = news_items
        if old_city:
            migrated["city_items"][old_city] = city_items

    old_news_events = state.get("news_events", {})
    if isinstance(old_news_events, dict):
        migrated["news_events"] = old_news_events

    old_prediction_items = state.get("prediction_items", {})
    if old_city and isinstance(old_prediction_items, dict):
        migrated["prediction_items"] = {
            f"{old_city}|{key}": value for key, value in old_prediction_items.items()
        }
    return migrated


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            path.replace(backup)
        except OSError:
            pass
        LOG.warning("Could not read %s (%s); starting fresh (backup: %s)", path, exc, backup)
        return empty_state()

    if not isinstance(state, dict):
        LOG.warning("State file is not an object; starting fresh")
        return empty_state()

    if state.get("version", 1) < 2 or "city_items" not in state:
        state = migrate_state(state)

    state.setdefault("version", 2)
    state.setdefault("default_city", state.get("city", ""))
    state.setdefault("prices", {})
    state.setdefault("prediction_baselines", {})
    state.setdefault("city_items", {})
    state.setdefault("news_items", [])
    state.setdefault("news_events", {})
    state.setdefault("prediction_items", {})

    if not isinstance(state.get("default_city"), str):
        state["default_city"] = ""

    normalized_prices: dict[str, dict[str, float]] = {}
    if isinstance(state["prices"], dict):
        for city, prices in state["prices"].items():
            if not isinstance(city, str) or not isinstance(prices, dict):
                continue
            normalized_prices[city] = {
                product: float(value)
                for product, value in prices.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
    state["prices"] = normalized_prices

    if not isinstance(state["prediction_baselines"], dict):
        state["prediction_baselines"] = {}
    if not isinstance(state["city_items"], dict):
        state["city_items"] = {}
    state["city_items"] = {
        city: items for city, items in state["city_items"].items() if isinstance(items, list)
    }
    if not isinstance(state["news_items"], list):
        state["news_items"] = []
    if not isinstance(state["news_events"], dict):
        state["news_events"] = {}
    if not isinstance(state["prediction_items"], dict):
        state["prediction_items"] = {}
    return state


def merge_items(
    existing: list[dict[str, Any]],
    new_items: list[dict[str, Any]],
    max_items: int,
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    required = {"id", "title", "link", "description", "published", "category"}
    for item in [*existing, *new_items]:
        if not isinstance(item, dict) or not required.issubset(item):
            continue
        if not isinstance(item["description"], str):
            continue
        if not all(
            isinstance(item[key], str) and item[key]
            for key in required - {"description"}
        ):
            continue
        by_id[item["id"]] = item

    def sort_key(item: dict[str, Any]) -> datetime:
        try:
            value = datetime.fromisoformat(item["published"])
        except (TypeError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    return sorted(by_id.values(), key=sort_key, reverse=True)[:max_items]


def city_feed_items(
    city: str, state: dict[str, Any], cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    news_items = state.get("news_items", [])
    local_items = state.get("city_items", {}).get(city, [])
    return merge_items([], [*news_items, *local_items], cfg["max_items"])


def make_price_item(
    city: str,
    city_label: str,
    product: str,
    old: float,
    new: float,
    now: datetime,
    tz: Any,
    source_url: str,
) -> dict[str, Any]:
    label = PRODUCT_COLUMNS[product][0]
    diff = round(new - old, 2)
    percent = (diff / old * 100) if old else 0.0
    date_key = now.astimezone(tz).date().isoformat()
    digest = hashlib.sha1(
        f"{city}|{product}|{old:.2f}|{new:.2f}|{date_key}".encode("utf-8")
    ).hexdigest()[:16]
    diff_text = f"{diff:+.2f}".replace(".", ",")
    percent_text = f"{percent:+.2f}".replace(".", ",")
    title = (
        f"[Fiyat] {city_label} · {label} {tr_number(old)} → {tr_number(new)} TL "
        f"({diff_text} TL)"
    )
    description = (
        f"<p><strong>{html.escape(label)}</strong>: "
        f"{tr_number(old)} TL → <strong>{tr_number(new)} TL</strong> "
        f"({diff_text} TL, {percent_text}%).</p>"
        f"<p>Bölge: {html.escape(city_label)}<br>"
        f"Kaynak: Petrol Ofisi (KDV dahil tavsiye edilen fiyat)<br>"
        f"Kontrol: {now.astimezone(tz):%d.%m.%Y %H:%M}</p>"
        f'<p><a href="{html.escape(source_url, quote=True)}">Fiyat tablosu</a></p>'
    )
    return {
        "id": f"price:{digest}",
        "title": title,
        "link": source_url,
        "description": description,
        "published": now.isoformat(),
        "category": "Fiyat",
        "source": "Petrol Ofisi",
        "source_url": source_url,
    }


def make_baseline_item(
    city: str,
    city_label: str,
    missing: dict[str, float],
    now: datetime,
    tz: Any,
    source_url: str,
) -> dict[str, Any]:
    parts = [
        f"{PRODUCT_COLUMNS[product][0]} {tr_number(price)} TL"
        for product, price in missing.items()
    ]
    title = f"[Başlangıç] {city_label} · " + " · ".join(parts)
    rows = "".join(
        f"<li>{html.escape(PRODUCT_COLUMNS[product][0])}: "
        f"<strong>{tr_number(price)} TL</strong></li>"
        for product, price in missing.items()
    )
    description = (
        f"<p>Fiyat takibi başladı. Güncel fiyatlar:</p><ul>{rows}</ul>"
        f"<p>Kaynak: Petrol Ofisi (KDV dahil tavsiye edilen fiyat)<br>"
        f"Kontrol: {now.astimezone(tz):%d.%m.%Y %H:%M}</p>"
        f'<p><a href="{html.escape(source_url, quote=True)}">Fiyat tablosu</a></p>'
    )
    date_key = now.astimezone(tz).date().isoformat()
    digest = hashlib.sha1(
        f"{city}|{json.dumps(missing, sort_keys=True)}|{date_key}".encode("utf-8")
    ).hexdigest()[:16]
    return {
        "id": f"baseline:{digest}",
        "title": title,
        "link": source_url,
        "description": description,
        "published": now.isoformat(),
        "category": "Fiyat",
        "source": "Petrol Ofisi",
        "source_url": source_url,
    }


def make_news_item(item: NewsItem, event_key: str, tz: Any) -> dict[str, Any]:
    digest = hashlib.sha1(event_key.encode("utf-8")).hexdigest()[:16]
    title = item.title if len(item.title) <= 200 else item.title[:197].rstrip() + "..."
    publisher = item.publisher or "Haber kaynağı"
    published_text = item.published.astimezone(tz).strftime("%d.%m.%Y %H:%M")
    description = (
        f"<p><strong>{html.escape(item.title)}</strong></p>"
        f"<p>Kaynak: {html.escape(publisher)} · {published_text}</p>"
        f'<p><a href="{html.escape(item.link, quote=True)}">Haberi oku</a></p>'
        "<p><small>Bu bir haber başlığıdır; fiyat değişikliği kesinleşmemiş veya "
        "iptal edilmiş olabilir. Kesinleşen fiyatlar Petrol Ofisi tablosundan ayrıca "
        "yayımlanır.</small></p>"
    )
    return {
        "id": f"news:{digest}",
        "title": f"[Haber] {title}",
        "link": item.link,
        "description": description,
        "published": item.published.astimezone(tz).isoformat(),
        "category": "Haber",
        "source": publisher,
        "source_url": item.publisher_url or item.link,
    }


def prune_news_events(state: dict[str, Any], now: datetime, tz: Any) -> None:
    cutoff = now - timedelta(days=45)
    for key, value in list(state.get("news_events", {}).items()):
        try:
            seen = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            del state["news_events"][key]
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=tz)
        if seen < cutoff:
            del state["news_events"][key]


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------


def build_feed(
    cfg: dict[str, Any],
    city_label: str,
    prices: dict[str, float],
    items: list[dict[str, Any]],
    now: datetime,
    tz: Any,
    price_ok: bool,
    feed_url: str = "",
) -> bytes:
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = f"Akaryakıt Fiyat Takibi · {city_label}"
    ET.SubElement(channel, "link").text = cfg["price_source_url"]
    ET.SubElement(channel, "description").text = build_channel_description(
        cfg, prices, now, tz, price_ok
    )
    ET.SubElement(channel, "language").text = "tr"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(now)
    ET.SubElement(channel, "generator").text = f"yakit-rss {__version__}"
    ET.SubElement(channel, "ttl").text = "30"

    if feed_url:
        ET.SubElement(
            channel,
            f"{{{ATOM_NS}}}link",
            {
                "href": feed_url,
                "rel": "self",
                "type": "application/rss+xml",
            },
        )

    for item in items:
        node = ET.SubElement(channel, "item")
        ET.SubElement(node, "title").text = item["title"]
        ET.SubElement(node, "link").text = item["link"]
        ET.SubElement(node, "guid", {"isPermaLink": "false"}).text = item["id"]
        try:
            published = datetime.fromisoformat(item["published"])
            if published.tzinfo is None:
                published = published.replace(tzinfo=tz)
        except (TypeError, ValueError):
            published = now
        ET.SubElement(node, "pubDate").text = format_datetime(published)
        ET.SubElement(node, "description").text = item["description"]
        ET.SubElement(node, "category").text = item["category"]
        if item.get("source"):
            source = ET.SubElement(
                node, "source", {"url": item.get("source_url") or item["link"]}
            )
            source.text = item["source"]

    ET.indent(rss, space="  ")
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True) + b"\n"


def build_channel_description(
    cfg: dict[str, Any],
    prices: dict[str, float],
    now: datetime,
    tz: Any,
    price_ok: bool,
) -> str:
    labels = [
        f"{PRODUCT_COLUMNS[product][0]} {tr_number(prices[product])} TL"
        for product in cfg["products"]
        if product in prices
    ]
    parts: list[str] = []
    if labels:
        prefix = "Güncel" if price_ok else "Son bilinen"
        parts.append(f"{prefix}: " + " · ".join(labels) + ".")
    parts.append(f"Son kontrol: {now.astimezone(tz):%d.%m.%Y %H:%M}.")
    parts.append(
        "Fiyatlar KDV dahil Petrol Ofisi tavsiye edilen fiyatlarıdır; "
        "istasyonlar arasında fark olabilir."
    )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# HTML overview page
# ---------------------------------------------------------------------------

PAGE_CSS = """
:root {
  color-scheme: light dark;
  --bg: #f4f6f8;
  --card: #ffffff;
  --text: #17202a;
  --muted: #5d6975;
  --line: #dde3e9;
  --accent: #0b63b6;
  --up: #b42318;
  --down: #067647;
  --warn: #9a6700;
  --shadow: 0 1px 2px rgba(16, 24, 40, .04);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #101419;
    --card: #181f26;
    --text: #eef3f7;
    --muted: #a5b1bc;
    --line: #2a3540;
    --accent: #69b0ff;
    --up: #ff8a80;
    --down: #63d68b;
    --warn: #e3b341;
    --shadow: none;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  line-height: 1.5;
}
a { color: var(--accent); }
.wrap { max-width: 1080px; margin: 0 auto; padding: 28px 18px 48px; }
header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  flex-wrap: wrap;
  margin-bottom: 22px;
}
.eyebrow {
  margin: 0 0 4px;
  color: var(--muted);
  font-size: .78rem;
  font-weight: 700;
  letter-spacing: .12em;
  text-transform: uppercase;
}
h1 { margin: 0; font-size: clamp(1.55rem, 3vw, 2.3rem); line-height: 1.15; }
h2 { margin: 0 0 12px; font-size: 1.05rem; letter-spacing: .01em; }
h3 { margin: 0; font-size: .95rem; }
.sub { margin: 6px 0 0; color: var(--muted); font-size: .9rem; }
.rss {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 9px 14px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--card);
  color: var(--text);
  text-decoration: none;
  font-weight: 650;
  font-size: .9rem;
  box-shadow: var(--shadow);
}
.rss:hover { border-color: var(--accent); }
.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.city-picker {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 7px 11px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--card);
  box-shadow: var(--shadow);
}
.city-picker label {
  color: var(--muted);
  font-size: .76rem;
  font-weight: 750;
  letter-spacing: .04em;
  text-transform: uppercase;
}
.city-picker select {
  max-width: 15rem;
  border: 0;
  outline: none;
  background-color: var(--card);
  color: var(--text);
  font: inherit;
  font-size: .9rem;
  font-weight: 650;
  color-scheme: light dark;
}
.city-picker select option,
.city-picker select optgroup {
  background-color: var(--card);
  color: var(--text);
}
.city-picker select:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
@media (prefers-color-scheme: dark) {
  .city-picker select { color-scheme: dark; }
  .city-picker select option,
  .city-picker select optgroup {
    background-color: var(--card);
    color: var(--text);
  }
}
.reset-city {
  border: 0;
  background: transparent;
  color: var(--accent);
  font: inherit;
  font-size: .76rem;
  font-weight: 650;
  cursor: pointer;
  padding: 0;
}
section { margin: 26px 0; }
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(215px, 1fr));
  gap: 12px;
}
.card {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 15px 16px;
  box-shadow: var(--shadow);
}
.card .label { color: var(--muted); font-size: .85rem; font-weight: 600; }
.value { margin-top: 2px; font-size: 1.65rem; font-weight: 750; letter-spacing: -.02em; }
.value .unit { font-size: .82rem; font-weight: 600; color: var(--muted); letter-spacing: 0; }
.muted { color: var(--muted); }
.small { font-size: .82rem; }
.row { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: .72rem;
  font-weight: 750;
  letter-spacing: .02em;
  white-space: nowrap;
}
.badge-up { color: var(--up); background: rgba(180, 35, 24, .12); }
.badge-down { color: var(--down); background: rgba(6, 118, 71, .12); }
.badge-flat { color: var(--muted); background: rgba(120, 130, 140, .14); }
.badge-news { color: var(--accent); background: rgba(11, 99, 182, .11); }
.badge-price { color: var(--down); background: rgba(6, 118, 71, .11); }
.badge-forecast { color: var(--warn); background: rgba(154, 103, 0, .14); }
.delta-up { color: var(--up); font-weight: 700; }
.delta-down { color: var(--down); font-weight: 700; }
.delta-flat { color: var(--muted); font-weight: 700; }
.alert {
  border: 1px solid var(--line);
  border-left: 4px solid var(--accent);
  border-radius: 10px;
  background: var(--card);
  padding: 11px 14px;
  margin: 0 0 14px;
  font-size: .9rem;
}
.alert.warn { border-left-color: var(--warn); }
.chart { margin-top: 12px; }
.chart svg { width: 100%; height: auto; display: block; color: var(--accent); }
.chart-meta { display: flex; justify-content: space-between; gap: 12px; margin-top: 4px; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; border-bottom: 1px solid var(--line); padding: 9px 6px; font-size: .9rem; }
th { color: var(--muted); font-size: .76rem; text-transform: uppercase; letter-spacing: .06em; }
tr:last-child td { border-bottom: 0; }
.feed { list-style: none; margin: 0; padding: 0; }
.feed li {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 9px;
  align-items: baseline;
  padding: 10px 0;
  border-bottom: 1px solid var(--line);
}
.feed li:last-child { border-bottom: 0; }
.feed a { color: var(--text); text-decoration: none; font-weight: 600; }
.feed a:hover { color: var(--accent); }
.feed time { color: var(--muted); font-size: .78rem; white-space: nowrap; }
.note {
  margin: 10px 0 0;
  color: var(--muted);
  font-size: .82rem;
}
footer {
  margin-top: 34px;
  padding-top: 16px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: .8rem;
}
@media (max-width: 620px) {
  .feed li { grid-template-columns: auto 1fr; }
  .feed time { grid-column: 2; }
}
"""


def fmt_number(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f}".replace(".", ",") + "%"


def fmt_datetime(value: datetime | None, tz: Any) -> str:
    if value is None:
        return "—"
    return value.astimezone(tz).strftime("%d.%m.%Y %H:%M")


def product_unit(product: str) -> str:
    return "TL/kg" if product in {"kalorifer", "fuel_oil"} else "TL/L"


def build_sparkline(values: list[float], *, width: int = 640, height: int = 150) -> str:
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    span = high - low or 1.0
    pad = 10
    points: list[str] = []
    for index, value in enumerate(values):
        x = pad + index * (width - 2 * pad) / (len(values) - 1)
        y = height - pad - ((value - low) / span) * (height - 2 * pad)
        points.append(f"{x:.1f},{y:.1f}")
    joined = " ".join(points)
    last_x, last_y = points[-1].split(",")
    area = f"{pad},{height - pad} {joined} {width - pad},{height - pad}"
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Brent son {len(values)} işlem günü">'
        f"<title>Brent son {len(values)} işlem günü</title>"
        f'<polygon points="{area}" fill="currentColor" opacity=".08"></polygon>'
        f'<polyline points="{joined}" fill="none" stroke="currentColor" '
        f'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"></polyline>'
        f'<circle cx="{last_x}" cy="{last_y}" r="3.2" fill="currentColor"></circle>'
        "</svg>"
    )


def market_card(label: str, series: MarketSeries | None, unit: str, digits: int, tz: Any) -> str:
    if series is None:
        return (
            f'<div class="card"><div class="label">{html.escape(label)}</div>'
            '<div class="value">—</div><div class="muted small">Veri yok</div></div>'
        )
    change = series.change_pct
    if change is None:
        change_class = "delta-flat"
        change_text = "—"
    elif abs(change) < 0.005:
        change_class = "delta-flat"
        change_text = "0,00%"
    elif change > 0:
        change_class = "delta-up"
        change_text = fmt_pct(change)
    else:
        change_class = "delta-down"
        change_text = fmt_pct(change)
    return (
        f'<div class="card"><div class="label">{html.escape(label)}</div>'
        f'<div class="value">{fmt_number(series.price, digits)} '
        f'<span class="unit">{html.escape(unit)}</span></div>'
        f'<div class="{change_class} small">{change_text}</div>'
        f'<div class="muted small">{html.escape(series.source)} · '
        f'{fmt_datetime(series.market_time, tz)}</div></div>'
    )


def price_card(product: str, price: float | None, price_ok: bool) -> str:
    label = PRODUCT_COLUMNS[product][0]
    if price is None:
        return (
            f'<div class="card"><div class="label">{html.escape(label)}</div>'
            '<div class="value">—</div><div class="muted small">Fiyat yok</div></div>'
        )
    note = "Petrol Ofisi · KDV dahil" if price_ok else "Son bilinen"
    return (
        f'<div class="card"><div class="label">{html.escape(label)}</div>'
        f'<div class="value">{tr_number(price)} '
        f'<span class="unit">{product_unit(product)}</span></div>'
        f'<div class="muted small">{note}</div></div>'
    )


def prediction_card(prediction: Prediction) -> str:
    if prediction.confidence == "veri yok":
        return (
            f'<div class="card"><div class="row"><span class="label">'
            f'{html.escape(prediction.label)}</span>'
            '<span class="badge badge-flat">Veri yok</span></div>'
            '<div class="value">—</div>'
            f'<div class="muted small">{html.escape(prediction.note)}</div></div>'
        )

    if prediction.direction == "zam":
        badge = '<span class="badge badge-up">Zam</span>'
        delta_class = "delta-up"
    elif prediction.direction == "indirim":
        badge = '<span class="badge badge-down">İndirim</span>'
        delta_class = "delta-down"
    else:
        badge = '<span class="badge badge-flat">Sabit</span>'
        delta_class = "delta-flat"

    delta_text = f"{prediction.delta:+.2f}".replace(".", ",")
    if prediction.delta == 0:
        delta_text = "0,00"
    confidence = prediction.confidence.capitalize()
    return (
        f'<div class="card"><div class="row"><span class="label">'
        f'{html.escape(prediction.label)}</span>{badge}</div>'
        f'<div class="value {delta_class}">{delta_text} '
        '<span class="unit">TL</span></div>'
        f'<div class="small">{tr_number(prediction.current_price)} → '
        f'<strong>{tr_number(prediction.predicted_price)} TL</strong></div>'
        f'<div class="muted small">Güven: {html.escape(confidence)} · '
        f'Kaynak: {html.escape(prediction.source)}</div>'
        f'<div class="muted small">Maliyet: '
        f'{tr_number(prediction.current_cost or 0)} TL/L · Baz: '
        f'{tr_number(prediction.basis_cost or 0)} TL/L</div>'
        f'<div class="muted small">{html.escape(prediction.note)}</div></div>'
    )


def feed_item_html(item: dict[str, Any], tz: Any) -> str:
    category = item.get("category", "Haber")
    badge_class = {"Fiyat": "badge-price", "Tahmin": "badge-forecast"}.get(
        category, "badge-news"
    )
    title = item["title"]
    for prefix in ("[Haber] ", "[Fiyat] ", "[Tahmin] "):
        if title.startswith(prefix):
            title = title[len(prefix):]
            break
    try:
        published = datetime.fromisoformat(item["published"])
        published_text = fmt_datetime(published, tz)
    except (TypeError, ValueError):
        published_text = ""
    return (
        f'<li><span class="badge {badge_class}">{html.escape(category)}</span>'
        f'<a href="{html.escape(item["link"], quote=True)}">'
        f'{html.escape(title)}</a>'
        f'<time>{html.escape(published_text)}</time></li>'
    )


def build_overview_page(
    cfg: dict[str, Any],
    city_infos: list[CityInfo],
    state: dict[str, Any],
    prices_by_city: dict[str, dict[str, float]],
    predictions_by_city: dict[str, list[Prediction]],
    market: dict[str, MarketSeries | None],
    now: datetime,
    tz: Any,
    price_ok: bool,
) -> str:
    if not city_infos:
        raise ConfigError("No cities available for the overview page")
    prediction_enabled = cfg["prediction_enabled"]
    default_info = next(
        (info for info in city_infos if info.name == cfg["city"]),
        city_infos[0],
    )

    def prices_html(city_name: str) -> str:
        city_prices = prices_by_city.get(city_name, {})
        return "".join(
            price_card(product, city_prices.get(product), price_ok)
            for product in cfg["products"]
        )

    def predictions_html(city_name: str) -> str:
        if not prediction_enabled:
            return ""
        city_predictions = predictions_by_city.get(city_name, [])
        cards = "".join(prediction_card(prediction) for prediction in city_predictions)
        return cards or '<div class="card muted">Tahmin üretilemedi.</div>'

    def latest_html(city_name: str) -> str:
        city_items = city_feed_items(city_name, state, cfg)[:8]
        rows = "".join(feed_item_html(item, tz) for item in city_items)
        return rows or '<li class="muted">Henüz kayıt yok.</li>'

    city_data: dict[str, dict[str, str]] = {}
    for info in city_infos:
        feed_url = f"feeds/{info.slug}.xml"
        city_data[info.slug] = {
            "name": info.label,
            "feed": feed_url,
            "prices": prices_html(info.name),
            "predictions": predictions_html(info.name),
            "latest": latest_html(info.name),
        }

    def option_html(info: CityInfo) -> str:
        selected = " selected" if info.name == default_info.name else ""
        return (
            f'<option value="{html.escape(info.slug, quote=True)}"{selected}>'
            f"{html.escape(info.label)}</option>"
        )

    popular_rank = {slug: index for index, slug in enumerate(POPULAR_CITY_SLUGS)}
    popular = sorted(
        [info for info in city_infos if info.slug in popular_rank],
        key=lambda info: popular_rank[info.slug],
    )
    other = [info for info in city_infos if info.slug not in popular_rank]
    groups: list[str] = []
    if popular:
        groups.append(
            '<optgroup label="Popüler şehirler">'
            + "".join(option_html(info) for info in popular)
            + "</optgroup>"
        )
    if other:
        groups.append(
            '<optgroup label="Diğer iller">'
            + "".join(option_html(info) for info in other)
            + "</optgroup>"
        )
    options_html = "".join(groups)
    default_feed = f"feeds/{default_info.slug}.xml"

    source_names = ["Petrol Ofisi"]
    if cfg["news_enabled"]:
        source_names.extend(source["name"] for source in cfg["news_sources"])
    if prediction_enabled:
        source_names.extend(["Yahoo Finance", "TCMB"])
    sources_text = ", ".join(dict.fromkeys(source_names))

    alerts: list[str] = []
    if not price_ok:
        alerts.append(
            '<div class="alert warn">Fiyat kaynağına ulaşılamadı; '
            "son bilinen fiyatlar gösteriliyor.</div>"
        )
    if prediction_enabled and not market.get("usdtry"):
        alerts.append(
            '<div class="alert warn">Piyasa verisi alınamadı; tahminler '
            "güncellenemedi.</div>"
        )
    if not prediction_enabled:
        alerts.append(
            '<div class="alert">Tahminler kapalı. '
            "Yalnızca haberler ve doğrulanmış fiyatlar yayımlanıyor.</div>"
        )

    if prediction_enabled:
        if cfg["prediction_pass_through"] < 0.999:
            esel_note = (
                "Eşel mobil varsayımı: maliyet değişiminin "
                f"%{cfg['prediction_pass_through'] * 100:.0f}'i pompaya yansıtılıyor."
            )
        else:
            esel_note = cfg["esel_note"]
    else:
        esel_note = "Tahmin modeli kapalı."

    market_cards = "".join(
        [
            market_card("Brent", market.get("brent"), "$/varil", 2, tz),
            market_card("USD/TRY", market.get("usdtry"), "TL", 4, tz),
            market_card("RBOB Benzin", market.get("gasoline"), "$/gal", 2, tz),
            market_card("ULSD Motorin", market.get("diesel"), "$/gal", 2, tz),
        ]
    )

    brent = market.get("brent")
    chart_svg = ""
    chart_labels = ""
    if brent and len(brent.history) >= 2:
        values = brent.history[-cfg["market_history_days"] :]
        chart_svg = build_sparkline(values)
        chart_labels = (
            f'<div class="chart-meta small muted">'
            f'<span>min {fmt_number(min(values))} $</span>'
            f'<span>max {fmt_number(max(values))} $</span>'
            f"<span>son {fmt_number(values[-1])} $</span></div>"
        )
    chart_card = (
        f'<div class="card chart">{chart_svg}{chart_labels}</div>' if chart_svg else ""
    )

    canonical = ""
    if cfg.get("page_url"):
        canonical = (
            f'<link rel="canonical" href="{html.escape(cfg["page_url"], quote=True)}">'
        )

    prediction_section = ""
    if prediction_enabled:
        min_delta_text = tr_number(cfg["prediction_min_delta_tl"])
        prediction_section = f"""
    <section>
      <h2>Tahmin</h2>
      <div class="grid" id="predictions">{predictions_html(default_info.name)}</div>
      <p class="note">{html.escape(esel_note)} Model; Brent referansı, RBOB/ULSD
      vadeli fiyatları ve USD/TRY ile hesaplanır. Platts CIF Med fiyatı değildir;
      vergi, marj ve dağıtıcı kararı değişiklikleri model dışıdır. LPG, ithal
      parite/Saudi CP nedeniyle tahmin modeline dahil değildir. Akışa yalnızca
      |Δ| ≥ {min_delta_text} TL olan tahminler eklenir.</p>
    </section>
        """

    market_section = ""
    if prediction_enabled:
        market_section = f"""
    <section>
      <h2>Piyasa</h2>
      <div class="grid">{market_cards}</div>
      {chart_card}
    </section>
        """

    data_json = json.dumps(city_data, ensure_ascii=False).replace("</", "<\\/")

    return f"""<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="1800">
  <meta name="description" content="{html.escape(default_info.label)} akaryakıt fiyatları, zam/indirim haberleri ve piyasa bazlı tahminler.">
  <title>Akaryakıt Fiyat Takibi · {html.escape(default_info.label)}</title>
  <link rel="alternate" type="application/rss+xml" title="Akaryakıt Fiyat Takibi" href="{html.escape(default_feed, quote=True)}">
  {canonical}
  <style>{PAGE_CSS}</style>
</head>
<body>
  <div class="wrap">
    <header>
      <div>
        <p class="eyebrow">Akaryakıt Fiyat Takibi</p>
        <h1 id="city-name">{html.escape(default_info.label)}</h1>
        <p class="sub">Son güncelleme: {fmt_datetime(now, tz)} · Kaynaklar:
        {html.escape(sources_text)}</p>
      </div>
      <div class="header-actions">
        <div class="city-picker">
          <label for="city-select">Şehir</label>
          <select id="city-select" data-default="{html.escape(default_info.slug, quote=True)}" title="Seçiminiz çerezde saklanır">
            {options_html}
          </select>
          <button type="button" class="reset-city" id="reset-city" title="Çerezi temizle ve varsayılan şehre dön">Sıfırla</button>
        </div>
        <a class="rss" id="rss-link" href="{html.escape(default_feed, quote=True)}">RSS akışı</a>
      </div>
    </header>

    <noscript><div class="alert">Şehir değiştirmek için JavaScript gerekir; varsayılan şehir gösteriliyor.</div></noscript>
    {''.join(alerts)}

    <section>
      <h2>Güncel fiyatlar</h2>
      <div class="grid" id="current-prices">{prices_html(default_info.name)}</div>
      <p class="note">Petrol Ofisi tavsiye edilen KDV dahil fiyatlardır; istasyonlar
      arasında fark olabilir. Varsayılan şehir <code>config.json</code> içindeki
      <code>city</code> değeridir; son seçiminiz çerezde saklanır.</p>
    </section>

    {prediction_section}
    {market_section}

    <section>
      <h2>Son akış</h2>
      <div class="card"><ul class="feed" id="latest">{latest_html(default_info.name)}</ul></div>
      {('<p class="note">Haber kaynakları şu anda yeni bir fiyat haberi vermedi; onaylanmış fiyat değişiklikleri ve tahminler güncellenmeye devam ediyor.</p>' if cfg["news_enabled"] and not state["news_items"] else '')}
    </section>

    <footer>
      <p>Fiyatlar bilgi amaçlıdır; pompa fiyatı istasyona göre değişebilir.
      Tahminler resmi fiyat veya yatırım tavsiyesi değildir.</p>
      <p>{html.escape(esel_note)}</p>
    </footer>
  </div>
  <script type="application/json" id="city-data">{data_json}</script>
  <script>
  (() => {{
    const data = JSON.parse(document.getElementById('city-data').textContent);
    const select = document.getElementById('city-select');
    const defaultSlug = select.dataset.default;
    const cookieName = 'yakit_city';
    const nameEl = document.getElementById('city-name');
    const pricesEl = document.getElementById('current-prices');
    const predictionsEl = document.getElementById('predictions');
    const latestEl = document.getElementById('latest');
    const rssEl = document.getElementById('rss-link');
    const alternate = document.querySelector('link[rel="alternate"]');

    function readCookie(name) {{
      try {{
        const match = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
        return match ? decodeURIComponent(match[1]) : null;
      }} catch (error) {{
        return null;
      }}
    }}
    function writeCookie(name, value) {{
      try {{
        let cookie = name + '=' + encodeURIComponent(value)
          + '; Max-Age=31536000; Path=/; SameSite=Lax';
        if (location.protocol === 'https:') cookie += '; Secure';
        document.cookie = cookie;
      }} catch (error) {{}}
    }}
    function clearCookie(name) {{
      try {{
        let cookie = name + '=; Max-Age=0; Path=/; SameSite=Lax';
        if (location.protocol === 'https:') cookie += '; Secure';
        document.cookie = cookie;
      }} catch (error) {{}}
    }}

    const cookieCity = readCookie(cookieName);
    const params = new URLSearchParams(location.search);
    const requested = params.get('city');
    const initial = data[requested] ? requested : (data[cookieCity] ? cookieCity : defaultSlug);
    select.value = initial;

    function render(slug, remember) {{
      const city = data[slug];
      if (!city) return;
      nameEl.textContent = city.name;
      pricesEl.innerHTML = city.prices;
      if (predictionsEl) predictionsEl.innerHTML = city.predictions;
      latestEl.innerHTML = city.latest;
      rssEl.href = city.feed;
      if (alternate) alternate.href = city.feed;
      document.title = 'Akaryakıt Fiyat Takibi · ' + city.name;
      const url = new URL(location.href);
      url.searchParams.set('city', slug);
      try {{ history.replaceState(null, '', url); }} catch (error) {{}}
      if (remember) writeCookie(cookieName, slug);
    }}

    select.addEventListener('change', () => render(select.value, true));
    const reset = document.getElementById('reset-city');
    if (reset) {{
      reset.addEventListener('click', () => {{
        select.value = defaultSlug;
        clearCookie(cookieName);
        render(defaultSlug, false);
      }});
    }}
    render(initial, false);
  }})();
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary = tempfile.mkstemp(prefix=f"{path.name}.", dir=path.parent)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def state_without_timestamp(state: dict[str, Any]) -> str:
    return json.dumps(
        {key: value for key, value in state.items() if key != "updated_at"},
        ensure_ascii=False,
        sort_keys=True,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)

    config_path = Path(args.config).expanduser()
    try:
        cfg, base = load_config(
            config_path,
            required=args.config != str(DEFAULT_CONFIG_PATH),
        )
        apply_cli_overrides(cfg, args)
        validate_config(cfg)
    except ConfigError as exc:
        LOG.error("%s", exc)
        return 2

    tz = get_timezone(cfg["timezone"])
    now = datetime.now(tz)
    page_path = resolve_path(base, cfg["page_output"])
    state_path = resolve_path(base, cfg["state_file"])
    source_url = cfg["price_source_url"]

    if args.list_cities:
        try:
            page = http_get(
                source_url,
                user_agent=cfg["user_agent"],
                timeout=cfg["timeout_seconds"],
            ).decode("utf-8", "replace")
            rows = parse_prices(page, ["benzin"])
        except (SourceError, UnicodeError) as exc:
            LOG.error("%s", exc)
            return 2
        for row in rows:
            print(row.name)
        return 0

    state = load_state(state_path)
    state_before = state_without_timestamp(state)
    page_url = cfg.get("page_url") or ""

    legacy_google = [
        item
        for item in state["news_items"]
        if isinstance(item.get("link"), str) and "news.google.com" in item["link"]
    ]
    if legacy_google:
        state["news_items"] = [
            item for item in state["news_items"] if item not in legacy_google
        ]
        state["news_events"] = {}
        LOG.info("Removed %d legacy Google News item(s)", len(legacy_google))

    allowed_predictions = set(cfg["prediction_products"])
    state["prediction_baselines"] = {
        city: {
            product: baseline
            for product, baseline in baselines.items()
            if product in allowed_predictions
        }
        for city, baselines in state["prediction_baselines"].items()
        if isinstance(baselines, dict)
    }
    state["prediction_items"] = {
        key: value
        for key, value in state["prediction_items"].items()
        if len(key.split("|", 2)) >= 2
        and key.split("|", 2)[1] in allowed_predictions
    }

    price_ok = False
    rows: list[PriceRow] = []
    try:
        page_html = http_get(
            source_url,
            user_agent=cfg["user_agent"],
            timeout=cfg["timeout_seconds"],
        ).decode("utf-8", "replace")
        rows = parse_prices(page_html, cfg["products"])
        price_ok = True
    except (SourceError, UnicodeError) as exc:
        LOG.warning("Price source unavailable: %s", exc)

    if price_ok:
        try:
            city_infos = select_city_infos(rows, cfg)
        except ConfigError as exc:
            LOG.error("%s", exc)
            return 2
    else:
        city_infos = city_infos_from_state(state, cfg)
        for info in city_infos:
            if fold_text(info.name) == fold_text(cfg["city"]):
                cfg["city"] = info.name
                break

    if not city_infos:
        LOG.error("No cities available")
        return 2

    selected_names = {info.name for info in city_infos}
    default_info = next(
        (info for info in city_infos if info.name == cfg["city"]),
        city_infos[0],
    )
    cfg["city"] = default_info.name
    state["default_city"] = cfg["city"]
    state["city_items"] = {
        city: merge_items([], items, MAX_CITY_ITEMS)
        for city, items in state["city_items"].items()
        if isinstance(city, str) and isinstance(items, list)
    }
    state["news_items"] = merge_items([], state["news_items"], cfg["max_items"])

    if price_ok:
        state["prices"] = {
            city: prices for city, prices in state["prices"].items() if city in selected_names
        }
        state["prediction_baselines"] = {
            city: baselines
            for city, baselines in state["prediction_baselines"].items()
            if city in selected_names
        }
        state["city_items"] = {
            city: items for city, items in state["city_items"].items() if city in selected_names
        }
        state["prediction_items"] = {
            key: value
            for key, value in state["prediction_items"].items()
            if key.split("|", 1)[0] in selected_names
        }

    current_prices_by_city: dict[str, dict[str, float]] = {}
    new_city_item_count = 0

    if price_ok:
        rows_by_name = {row.name: row for row in rows}
        for info in city_infos:
            row = rows_by_name.get(info.name)
            if row is None:
                LOG.warning("No price row for %s", info.name)
                current_prices_by_city[info.name] = dict(
                    state["prices"].get(info.name, {})
                )
                continue

            current_prices = {
                product: round(row.prices[product], 2) for product in cfg["products"]
            }
            current_prices_by_city[info.name] = current_prices
            old_prices = state["prices"].get(info.name, {})
            missing: dict[str, float] = {}
            changes: dict[str, dict[str, float]] = {}
            for product in cfg["products"]:
                new_price = current_prices[product]
                old_price = old_prices.get(product)
                if old_price is None:
                    missing[product] = new_price
                elif abs(new_price - old_price) >= 0.005:
                    changes[product] = {"old": old_price, "new": new_price}

            new_items_for_city: list[dict[str, Any]] = []
            if missing:
                new_items_for_city.append(
                    make_baseline_item(
                        city=info.name,
                        city_label=info.label,
                        missing=missing,
                        now=now,
                        tz=tz,
                        source_url=source_url,
                    )
                )
                LOG.info(
                    "Baseline recorded for %s: %s",
                    info.label,
                    ", ".join(missing),
                )
            for product, change in changes.items():
                new_items_for_city.append(
                    make_price_item(
                        city=info.name,
                        city_label=info.label,
                        product=product,
                        old=change["old"],
                        new=change["new"],
                        now=now,
                        tz=tz,
                        source_url=source_url,
                    )
                )
                LOG.info(
                    "Price change %s · %s: %.2f -> %.2f TL",
                    info.label,
                    PRODUCT_COLUMNS[product][0],
                    change["old"],
                    change["new"],
                )

            if new_items_for_city:
                state["city_items"].setdefault(info.name, []).extend(new_items_for_city)
                state["city_items"][info.name] = merge_items(
                    [], state["city_items"][info.name], MAX_CITY_ITEMS
                )
                new_city_item_count += len(new_items_for_city)

            state["prices"][info.name] = current_prices
            LOG.info(
                "%s: %s",
                info.label,
                ", ".join(
                    f"{PRODUCT_COLUMNS[product][0]} "
                    f"{tr_number(current_prices[product])} TL"
                    for product in cfg["products"]
                ),
            )
    else:
        current_prices_by_city = {
            info.name: dict(state["prices"].get(info.name, {}))
            for info in city_infos
        }

    new_news_items: list[dict[str, Any]] = []
    if cfg["news_enabled"]:
        try:
            fetched_news = fetch_news(cfg, tz)
            selected_news = select_news(fetched_news, cfg, state, now, tz)
            for event_key, news_item in selected_news:
                new_news_items.append(make_news_item(news_item, event_key, tz))
                state["news_events"][event_key] = news_item.published.astimezone(tz).isoformat()
            if new_news_items:
                state["news_items"] = merge_items(
                    state["news_items"], new_news_items, cfg["max_items"]
                )
            LOG.info(
                "News: %d fetched, %d new",
                len(fetched_news),
                len(new_news_items),
            )
        except (SourceError, ET.ParseError, UnicodeError) as exc:
            LOG.warning("News source unavailable: %s", exc)

    market: dict[str, MarketSeries | None] = {}
    predictions_by_city: dict[str, list[Prediction]] = {}
    new_prediction_item_count = 0
    if cfg["prediction_enabled"]:
        market = fetch_market_data(cfg, tz)
        for info in city_infos:
            city_prices = current_prices_by_city.get(info.name, {})
            if not city_prices:
                predictions_by_city[info.name] = []
                continue
            city_predictions = build_predictions(
                cfg, state, info.name, city_prices, market, now, price_ok
            )
            predictions_by_city[info.name] = city_predictions
            for prediction in city_predictions:
                LOG.info(
                    "Tahmin %s · %s: %+.2f TL -> %.2f TL (%s, %s)",
                    info.label,
                    prediction.label,
                    prediction.delta,
                    prediction.predicted_price,
                    prediction.direction,
                    prediction.confidence,
                )
                if cfg["prediction_in_feed"]:
                    item = make_prediction_item(
                        cfg,
                        state,
                        info.name,
                        prediction,
                        market,
                        now,
                        tz,
                        page_url,
                    )
                    if item is not None:
                        state["city_items"].setdefault(info.name, []).append(item)
                        state["city_items"][info.name] = merge_items(
                            [], state["city_items"][info.name], MAX_CITY_ITEMS
                        )
                        new_prediction_item_count += 1
        prune_prediction_items(state, now, tz)

    prune_news_events(state, now, tz)
    state["version"] = 2

    if args.dry_run:
        LOG.info(
            "Dry run: %d city item(s), %d news item(s), %d prediction item(s); "
            "would write %s and %d city feed(s)",
            new_city_item_count,
            len(new_news_items),
            new_prediction_item_count,
            page_path,
            len(city_infos),
        )
        return 0

    feeds_dir = page_path.parent / "feeds"
    if feeds_dir.exists():
        for old_feed in feeds_dir.glob("*.xml"):
            try:
                old_feed.unlink()
            except OSError:
                pass
    page_root = page_url.rstrip("/")
    for info in city_infos:
        feed_items = city_feed_items(info.name, state, cfg)
        feed_prices = state["prices"].get(info.name, {})
        feed_url = f"{page_root}/feeds/{info.slug}.xml" if page_root else ""
        feed = build_feed(
            cfg,
            info.label,
            feed_prices,
            feed_items,
            now,
            tz,
            price_ok,
            feed_url,
        )
        atomic_write(feeds_dir / f"{info.slug}.xml", feed)

    page = build_overview_page(
        cfg,
        city_infos,
        state,
        current_prices_by_city,
        predictions_by_city,
        market,
        now,
        tz,
        price_ok,
    )
    atomic_write(page_path, page.encode("utf-8"))

    if state_before != state_without_timestamp(state):
        state["updated_at"] = now.isoformat()
        payload = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        atomic_write(state_path, payload)

    LOG.info(
        "Wrote %s and %d city feed(s)",
        page_path,
        len(city_infos),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
