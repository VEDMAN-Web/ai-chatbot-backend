"""
Fetch a public property page and turn it into a chat draft.

Nothing in this module writes a listing. Saving happens only after the admin
types a confirmation, through save_ingested_listing.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import logging
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from app.core.config import settings

logger = logging.getLogger(__name__)

CONFIRM_FOOTER = "Reply 'confirm' to save, 'cancel' to discard, or tell me what to change."
INGEST_TOOL = "ingest_property_from_url"
_OWN_SITES = ("tajlandia.com", "varsoviaestate.com", "thai.estate")
_ALLOWED_TYPES = ("text/html", "application/json", "application/xml", "text/xml")
_USER_AGENT = "TajlandiaAdminPropertyIngest/1.0 (admin catalog import; +https://tajlandia.com)"
_NEVER_GUESS = {
    "price_display", "price_numeric_min", "price_numeric_max", "currency",
    "bedrooms_display", "bedrooms_min", "bedrooms_max", "bathrooms",
    "area_sqm_min", "area_sqm_max", "area_display", "ownership_type",
}
_REQUIRED = ("property_name", "property_type", "location", "price", "status")
_DISTRICTS = {
    "bang tao": ("Phuket", "Bang Tao", "Phuket"),
    "layan": ("Phuket", "Layan", "Phuket"),
    "kamala": ("Phuket", "Kamala", "Phuket"),
    "rawai": ("Phuket", "Rawai", "Phuket"),
    "surin": ("Phuket", "Surin", "Phuket"),
    "karon": ("Phuket", "Karon", "Phuket"),
    "kata": ("Phuket", "Kata", "Phuket"),
    "patong": ("Phuket", "Patong", "Phuket"),
    "chalong": ("Phuket", "Chalong", "Phuket"),
    "maenam": ("Surat Thani", "Maenam", "Samui"),
    "bophut": ("Surat Thani", "Bophut", "Samui"),
    "chaweng": ("Surat Thani", "Chaweng", "Samui"),
    "lamai": ("Surat Thani", "Lamai", "Samui"),
}
_IMAGE_SKIP = (
    "logo", "icon", "pixel", "favicon", "sprite", "spacer", "tracking", "1x1",
    "badge", "analytics", "menu", "avatar",
)
_FOREIGN_DOMAINS = (
    ("realestateindia", "India"),
    ("99acres", "India"),
    ("magicbricks", "India"),
    ("zameen", "Pakistan"),
)
_FOREIGN_PLACES = (
    ("dholera", "Dholera"),
    ("gujarat", "Gujarat"),
    ("ahmedabad", "Ahmedabad"),
    ("mumbai", "Mumbai"),
    ("delhi", "Delhi"),
    ("bangalore", "Bangalore"),
    ("bengaluru", "Bangalore"),
    ("kolkata", "Kolkata"),
    ("calcutta", "Kolkata"),
    ("kathua", "Kathua"),
    ("dubai", "Dubai"),
    ("poland", "Poland"),
    ("polska", "Poland"),
    ("warsaw", "Warsaw"),
    ("warszawa", "Warsaw"),
    ("pakistan", "Pakistan"),
    ("india", "India"),
)
_LAND_ALIASES = {
    "residential plot": "land",
    "residential plots": "land",
    "plot": "land",
    "plots": "land",
    "land": "land",
}
_THAI_PLACE_RE = re.compile(
    r"\b(phuket|bangkok|krabi|pattaya|chiang mai|chiangmai|samui|koh samui|surat thani|"
    r"chonburi|phang nga|hua hin|rawai|patong|karon|kata|kamala|layan|bang tao|surin|"
    r"chalong|chaweng|lamai|bophut|maenam)\b",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_FETCH_CACHE: Dict[str, Tuple[float, "FetchedPage"]] = {}
_RATE: Dict[str, List[float]] = {}


class IngestError(Exception):
    """A fetch or policy failure the admin should see. Nothing was written."""


class FetchedPage:
    def __init__(self, url: str, final_url: str, content_type: str, text: str, body: bytes):
        self.url = url
        self.final_url = final_url
        self.content_type = content_type
        self.text = text
        self.body = body
        self.fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        self.content_hash = hashlib.sha256(body).hexdigest()


class HttpResponse:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes, location: Optional[str] = None):
        self.status = status
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self.body = body or b""
        self.location = location or self.headers.get("location")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def reset_ingest_runtime_state() -> None:
    _FETCH_CACHE.clear()
    _RATE.clear()


def http_get(url: str, timeout: float, max_bytes: int, headers: Dict[str, str]) -> HttpResponse:
    """One HTTP GET. Redirects are not followed here; the caller checks each hop."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = _read_capped(response, max_bytes)
            return HttpResponse(getattr(response, "status", 200), dict(response.headers.items()), raw)
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location") if exc.headers else None
        if exc.code in (301, 302, 303, 307, 308):
            return HttpResponse(exc.code, dict(exc.headers.items()) if exc.headers else {}, b"", location)
        body = exc.read(max_bytes + 1) if hasattr(exc, "read") else b""
        if len(body) > max_bytes:
            raise IngestError("The page is larger than the download limit, so I did not import it.")
        return HttpResponse(exc.code, dict(exc.headers.items()) if exc.headers else {}, body, location)


def _read_capped(response: Any, max_bytes: int) -> bytes:
    chunks = []
    total = 0
    while True:
        piece = response.read(65536)
        if not piece:
            break
        total += len(piece)
        if total > max_bytes:
            raise IngestError("The page is larger than the download limit, so I did not import it.")
        chunks.append(piece)
    return b"".join(chunks)


def _domain_list(name: str) -> List[str]:
    raw = getattr(settings, name, "") or ""
    if isinstance(raw, (list, tuple)):
        parts = [str(part) for part in raw]
    else:
        parts = str(raw).split(",")
    return [part.strip().lower().lstrip(".") for part in parts if part.strip()]


def _host_matches(host: str, domains: List[str]) -> bool:
    host = (host or "").lower().strip(".")
    for domain in domains:
        if host == domain or host.endswith("." + domain):
            return True
    return False


def _blocked_ip(value: str) -> bool:
    try:
        addr = ipaddress.ip_address(value.split("%")[0])
    except ValueError:
        return False
    if getattr(addr, "ipv4_mapped", None) is not None:
        addr = addr.ipv4_mapped
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _resolve_ips(host: str) -> List[str]:
    host = (host or "").strip().strip("[]")
    if not host or re.fullmatch(r"\d+", host):
        raise IngestError("I will not fetch that URL because it points at a private or local address.")
    lowered = host.lower().rstrip(".")
    if lowered in {"localhost", "metadata", "metadata.google.internal", "metadata.google.com"}:
        raise IngestError("I will not fetch that URL because it points at a private or local address.")
    try:
        ipaddress.ip_address(lowered)
        ips = [lowered]
    except ValueError:
        try:
            infos = socket.getaddrinfo(lowered, None)
        except socket.gaierror as exc:
            raise IngestError(f"I could not resolve {host}. Nothing was saved.") from exc
        ips = []
        for info in infos:
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    if not ips or any(_blocked_ip(ip) for ip in ips):
        raise IngestError("I will not fetch that URL because it points at a private or local address.")
    return ips


def _check_url_policy(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise IngestError("Only http and https links can be imported.")
    host = parsed.hostname.lower().rstrip(".")
    _resolve_ips(host)
    allowed = _domain_list("INGEST_ALLOWED_DOMAINS")
    blocked = _domain_list("INGEST_BLOCKED_DOMAINS")
    if blocked and _host_matches(host, blocked):
        raise IngestError("That domain is not allowed for import.")
    if allowed and not _host_matches(host, allowed):
        raise IngestError("That domain is not on the import allow-list.")
    return url


def _content_type_ok(value: str, *, robots: bool = False) -> bool:
    base = (value or "").split(";")[0].strip().lower()
    if robots and base in {"text/plain", "text/html", ""}:
        return True
    return base in _ALLOWED_TYPES or base.endswith("+json") or base.endswith("+xml")


def _robots_disallows(url: str, timeout: float, max_bytes: int) -> bool:
    if bool(getattr(settings, "INGEST_IGNORE_ROBOTS", False)):
        return False
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        _check_url_policy(robots_url)
        response = http_get(
            robots_url,
            timeout=timeout,
            max_bytes=min(max_bytes, 200_000),
            headers={"User-Agent": _USER_AGENT, "Accept": "text/plain"},
        )
    except IngestError:
        return False
    except Exception:
        return False
    if response.status >= 400:
        return False
    ctype = response.headers.get("content-type", "text/plain")
    if not _content_type_ok(ctype, robots=True):
        return False
    path = parsed.path or "/"
    applies = False
    disallows: List[str] = []
    for raw_line in response.body.decode("utf-8", errors="replace").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, val = [part.strip() for part in line.split(":", 1)]
        if key.lower() == "user-agent":
            applies = val in {"*", _USER_AGENT}
        elif applies and key.lower() == "disallow" and val:
            disallows.append(val)
    return any(path.startswith(rule) for rule in disallows if rule != "/")


def fetch_public_page(url: str, *, use_cache: bool = True) -> FetchedPage:
    """GET a public page. Stops on SSRF, robots, size, type, and redirect loops."""
    clean = _check_url_policy(url)
    now = time.time()
    cached = _FETCH_CACHE.get(clean)
    if use_cache and cached and now - cached[0] < 15 * 60:
        return cached[1]

    timeout = float(getattr(settings, "INGEST_TIMEOUT_SEC", 10.0) or 10.0)
    max_bytes = int(getattr(settings, "INGEST_MAX_BYTES", 3_000_000) or 3_000_000)
    if _robots_disallows(clean, timeout, max_bytes):
        raise IngestError("The site's robots.txt disallows that path, so I did not fetch it.")

    current = clean
    headers = {"User-Agent": _USER_AGENT, "Accept": "text/html,application/json,application/xml"}
    response: Optional[HttpResponse] = None
    for _hop in range(6):
        _check_url_policy(current)
        response = http_get(current, timeout=timeout, max_bytes=max_bytes, headers=headers)
        if response.status in (301, 302, 303, 307, 308) and response.location:
            current = urljoin(current, response.location)
            continue
        break
    else:
        raise IngestError("The page redirected too many times. Nothing was saved.")
    if response is None or response.status >= 400:
        raise IngestError(f"The site returned HTTP {getattr(response, 'status', 'error')}. Nothing was saved.")
    if len(response.body) > max_bytes:
        raise IngestError("The page is larger than the download limit, so I did not import it.")
    ctype = response.headers.get("content-type", "")
    if not _content_type_ok(ctype):
        raise IngestError("That URL did not return a page or JSON I can read.")
    text = response.body.decode("utf-8", errors="replace")
    page = FetchedPage(clean, current, ctype, text, response.body)
    _FETCH_CACHE[clean] = (now, page)
    return page


def _charge_rate(actor: str) -> None:
    limit = int(getattr(settings, "INGEST_MAX_URLS_PER_HOUR", 10) or 10)
    now = time.time()
    stamps = [stamp for stamp in _RATE.get(actor, []) if now - stamp < 3600]
    if len(stamps) >= limit:
        raise IngestError(f"You have reached the limit of {limit} URL imports this hour.")
    stamps.append(now)
    _RATE[actor] = stamps


def extract_urls(message: Optional[str]) -> List[str]:
    found = []
    for match in _URL_RE.findall(message or ""):
        url = match.rstrip(".,);]")
        if url not in found:
            found.append(url)
    return found[:10]


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _prov(value: Any, status: str, source: str, evidence: str = "", reason: str = "", confidence: Optional[str] = None, needs_decision: bool = False) -> Dict[str, Any]:
    return {
        "value": value,
        "status": status,
        "source": source,
        "evidence": (evidence or "")[:180],
        "reason": reason,
        "confidence": confidence,
        "needs_decision": needs_decision,
    }


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    text = re.sub(r"[^\d.]", "", text)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _unwrap_json(data: Any) -> Dict[str, Any]:
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    if not isinstance(data, dict):
        return {}
    for key in ("property", "listing", "data", "result"):
        inner = data.get(key)
        if isinstance(inner, dict) and any(k in inner for k in ("name", "title", "price", "priceFrom")):
            return inner
    return data


def _fields_from_mapping(data: Dict[str, Any], source: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}

    def take(field: str, *keys: str) -> None:
        for key in keys:
            value = data.get(key)
            if isinstance(value, (dict, list)) or _blank(value):
                continue
            out[field] = _prov(value, "found", source, evidence=key)
            return

    take("property_name", "property_name", "name", "title")
    take("project_name", "project_name", "project")
    take("property_type", "property_type", "propertyType", "type")
    take("status", "status")
    take("province", "province", "addressRegion", "region")
    take("district", "district", "addressLocality", "city")
    take("area", "area")
    take("address", "address")
    take("price_display", "price_display", "priceDisplay")
    take("price_numeric_min", "price_numeric_min", "priceFrom", "price")
    take("price_numeric_max", "price_numeric_max", "priceTo")
    take("currency", "currency", "priceCurrency")
    take("bedrooms_display", "bedrooms_display", "bedrooms")
    take("bedrooms_min", "bedrooms_min")
    take("bedrooms_max", "bedrooms_max")
    take("bathrooms", "bathrooms", "numberOfBathroomsTotal")
    take("area_sqm_min", "area_sqm_min", "floorSize", "area_sqm")
    take("description", "description")
    take("developer_name", "developer_name", "developer")
    take("ownership_type", "ownership_type", "ownership")
    if isinstance(data.get("address"), dict):
        addr = data["address"]
        if not _blank(addr.get("addressRegion")) and "province" not in out:
            out["province"] = _prov(addr.get("addressRegion"), "found", source, evidence="address.addressRegion")
        if not _blank(addr.get("addressLocality")) and "district" not in out:
            out["district"] = _prov(addr.get("addressLocality"), "found", source, evidence="address.addressLocality")
        street = ", ".join(str(addr.get(k)) for k in ("streetAddress", "addressLocality", "addressRegion") if addr.get(k))
        if street and "address" not in out:
            out["address"] = _prov(street, "found", source, evidence="address")
    offers = data.get("offers")
    if isinstance(offers, list) and offers:
        offers = offers[0]
    if isinstance(offers, dict):
        if "price_numeric_min" not in out and not _blank(offers.get("price")):
            out["price_numeric_min"] = _prov(offers.get("price"), "found", source, evidence="offers.price")
        if "currency" not in out and not _blank(offers.get("priceCurrency")):
            out["currency"] = _prov(offers.get("priceCurrency"), "found", source, evidence="offers.priceCurrency")
    return out


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ldjson: List[str] = []
        self.next_data = ""
        self.nuxt = ""
        self.meta: Dict[str, str] = {}
        self.alternate_json: List[str] = []
        self.tables: List[Tuple[str, str]] = []
        self.images: List[Tuple[str, str]] = []
        self.lines: List[str] = []
        self.h1 = ""
        self.title = ""
        self._capture = ""
        self._mode = ""
        self._cell = ""
        self._cells: List[str] = []
        self._in_cell = False
        self._skip_tag = ""
        self._skip_depth = 0
        self._in_title = False
        self._in_h1 = False
        self._title_buf = ""
        self._h1_buf = ""
        self._buf: List[str] = []

    def _region_skip(self, tag: str, attr: Dict[str, str]) -> bool:
        if tag in {"nav", "footer", "style", "noscript", "svg"}:
            return True
        role = (attr.get("role") or "").lower()
        if role in {"navigation", "contentinfo"}:
            return True
        blob = f"{attr.get('class', '')} {attr.get('id', '')}".lower()
        tokens = set(re.split(r"[^a-z0-9]+", blob))
        return bool(tokens & {"menu", "navbar", "nav", "footer", "breadcrumb", "cookie"})

    def _flush_line(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf = []
        if text:
            self.lines.append(text)

    def handle_starttag(self, tag, attrs):
        attr = {k.lower(): v or "" for k, v in attrs}
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        if tag == "script":
            kind = attr.get("type", "").lower()
            if "ld+json" in kind:
                self._mode = "ld"
            elif attr.get("id") == "__NEXT_DATA__":
                self._mode = "next"
            else:
                self._mode = "ignore"
            self._capture = ""
            return
        if self._region_skip(tag, attr):
            self._skip_tag = tag
            self._skip_depth = 1
            return
        if tag in {"div", "p", "li", "br", "h1", "h2", "h3", "tr", "section", "article"}:
            self._flush_line()
        if tag == "meta":
            key = attr.get("property") or attr.get("name") or ""
            if key and attr.get("content"):
                self.meta[key.lower()] = attr["content"]
        elif tag == "link" and "json" in attr.get("type", "").lower() and attr.get("href"):
            self.alternate_json.append(attr["href"])
        elif tag == "title":
            self._in_title = True
            self._title_buf = ""
        elif tag == "h1":
            self._in_h1 = True
            self._h1_buf = ""
        elif tag in {"td", "th", "dt", "dd"}:
            self._in_cell = True
            self._cell = ""
        elif tag == "img":
            hint = " ".join(f"{key}={attr.get(key, '')}" for key in ("alt", "width", "height", "class"))
            self.images.append((attr.get("src", ""), hint))

    def handle_data(self, data):
        if "window.__NUXT__" in data and not self.nuxt:
            self.nuxt = data
        if self._mode:
            self._capture += data
            return
        if self._skip_depth:
            return
        if self._in_title:
            self._title_buf += data
            return
        if self._in_h1:
            self._h1_buf += data
        if self._in_cell:
            self._cell += data
        self._buf.append(data)

    def handle_endtag(self, tag):
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth <= 0:
                    self._skip_depth = 0
                    self._skip_tag = ""
            return
        if tag == "script" and self._mode:
            if self._mode == "ld":
                self.ldjson.append(self._capture)
            elif self._mode == "next":
                self.next_data = self._capture
            self._mode = ""
            return
        if tag == "title" and self._in_title:
            self.title = re.sub(r"\s+", " ", self._title_buf).strip()
            self._in_title = False
        elif tag == "h1" and self._in_h1:
            self.h1 = re.sub(r"\s+", " ", self._h1_buf).strip()
            self._in_h1 = False
            self._flush_line()
        elif tag in {"td", "th", "dt", "dd"} and self._in_cell:
            self._cells.append(re.sub(r"\s+", " ", self._cell).strip())
            self._in_cell = False
            if len(self._cells) >= 2:
                self.tables.append((self._cells[-2], self._cells[-1]))
        elif tag in {"div", "p", "li", "h2", "h3", "tr", "section", "article"}:
            self._flush_line()

    def main_text(self) -> str:
        self._flush_line()
        return "\n".join(self.lines)


def _parse_html(text: str) -> _PageParser:
    parser = _PageParser()
    try:
        parser.feed(text or "")
        parser.main_text()
    except Exception:
        logger.debug("HTML parse failed", exc_info=True)
    return parser


def _enough(fields: Dict[str, Dict[str, Any]]) -> bool:
    name = fields.get("property_name")
    if not name or _blank(name.get("value")):
        return False
    price = fields.get("price_numeric_min") or fields.get("price_display")
    if price and not _blank(price.get("value")):
        return True
    extras = [key for key in ("property_type", "province", "district", "status", "bedrooms_min", "description") if key in fields]
    return len(extras) >= 3


def _canon_property_type(value: str) -> Optional[str]:
    from app.services.action_service import ALLOWED_PROPERTY_TYPES

    token = re.sub(r"\s+", " ", (value or "").strip().lower()).replace("(s)", "s")
    token = re.sub(r"\s+", " ", token)
    token = _LAND_ALIASES.get(token, token)
    return ALLOWED_PROPERTY_TYPES.get(token)


def _is_land_phrase(value: str) -> bool:
    token = re.sub(r"\s+", " ", (value or "").strip().lower()).replace("(s)", "s")
    token = re.sub(r"\s+", " ", token)
    return token in _LAND_ALIASES


def _normalize_type_status(fields: Dict[str, Dict[str, Any]]) -> None:
    from app.services.action_service import ALLOWED_PROPERTY_TYPES, ALLOWED_STATUSES

    kind = fields.get("property_type")
    if kind and isinstance(kind.get("value"), str):
        canon = _canon_property_type(kind["value"])
        if canon:
            kind["value"] = canon
        elif _is_land_phrase(kind["value"]):
            allowed = ", ".join(sorted(set(ALLOWED_PROPERTY_TYPES.values())))
            kind["needs_decision"] = True
            kind["reason"] = f"Type must be one of: {allowed}"
    status = fields.get("status")
    if status and isinstance(status.get("value"), str):
        canon = ALLOWED_STATUSES.get(status["value"].strip().lower())
        if canon:
            status["value"] = canon


def _district_hit(text: str) -> Optional[Tuple[str, Tuple[str, str, str]]]:
    """Whole-word district match. 'Surinder' and 'Kolkata' do not match."""
    blob = text or ""
    for key, triple in _DISTRICTS.items():
        if re.search(r"\b" + re.escape(key) + r"\b", blob, re.IGNORECASE):
            return key, triple
    return None


def _location_scope(fields: Dict[str, Dict[str, Any]], main_text: str, title: str = "") -> str:
    parts = [title or ""]
    for key in ("address", "location", "property_name"):
        value = (fields.get(key) or {}).get("value")
        if value:
            parts.append(str(value))
    parts.append((main_text or "")[:2000])
    return "\n".join(part for part in parts if part)


def _foreign_place(fields: Dict[str, Dict[str, Any]], main_text: str, url: str, title: str = "") -> Optional[str]:
    host = (urlparse(url or "").hostname or "").lower()
    scope = _location_scope(fields, main_text, title)
    price = str((fields.get("price_display") or {}).get("value") or "")
    currency = str((fields.get("currency") or {}).get("value") or "")
    blob = f"{scope}\n{price}\n{currency}"
    for needle, place in _FOREIGN_PLACES:
        if re.search(r"\b" + re.escape(needle) + r"\b", blob, re.IGNORECASE):
            return place
    if re.search(r"₹|\bINR\b|\bRs\.?\b|\bLacs?\b|\bLakhs?\b|\bCrores?\b|\d(?:\.\d+)?\s*[-_]?\s*cr\b", blob, re.I):
        return "India"
    for needle, place in _FOREIGN_DOMAINS:
        if needle in host:
            return place
    return None


def foreign_listing_warning(place: str, language: str = "en") -> str:
    shown = place or "another country"
    lang = (language or "en").split("-")[0].lower()
    if lang == "pl":
        return (
            f"To wygląda na ofertę spoza Tajlandii ({shown}). "
            "Strony Tajlandia publikują nieruchomości z Tajlandii. "
            "Odpowiedz 'continue anyway', aby ją zostawić, albo 'cancel'."
        )
    if lang == "hi":
        return (
            f"Yeh Thailand ke bahar ki listing lagti hai ({shown}). "
            "Tajlandia sites Thailand ki properties dikhati hain. "
            "'continue anyway' likho taaki yeh rahe, ya 'cancel'."
        )
    if lang == "gu":
        return (
            f"Aa Thailand bahaar ni listing lage che ({shown}). "
            "Tajlandia sites Thailand ni properties list kare che. "
            "'continue anyway' lakho, athva 'cancel'."
        )
    return (
        f"This looks like a listing outside Thailand ({shown}). "
        "Tajlandia sites list Thailand properties. "
        "Reply 'continue anyway' to keep it, or 'cancel'."
    )


def _foreign_blocks(draft: Dict[str, Any]) -> bool:
    if not draft.get("foreign_place") or draft.get("foreign_acknowledged"):
        return False
    return not _admin_thai_location(draft)


def _admin_thai_location(draft: Dict[str, Any]) -> bool:
    provenance = draft.get("provenance") or {}
    for key in ("province", "district", "area", "address", "location"):
        item = provenance.get(key) or {}
        if item.get("status") == "provided" and _THAI_PLACE_RE.search(str(item.get("value") or "")):
            return True
    return False


def _apply_inferences(fields: Dict[str, Dict[str, Any]], page_text: str, main_text: str = "", title: str = "", allow_location: bool = True) -> None:
    if allow_location and "province" not in fields:
        hit = _district_hit(_location_scope(fields, main_text, title))
        if hit:
            key, (province, district, area) = hit
            fields["province"] = _prov(province, "inferred", "rule", reason=f"province taken from '{key}' in the address or page")
            if "district" not in fields:
                fields["district"] = _prov(district, "inferred", "rule", reason=f"district taken from '{key}'")
            if "area" not in fields:
                fields["area"] = _prov(area, "inferred", "rule", reason=f"area taken from the {province} address")
    display = fields.get("price_display")
    currency = str((fields.get("currency") or {}).get("value") or "")
    if display and "price_numeric_min" not in fields and not display.get("needs_decision"):
        text = str(display.get("value"))
        foreign_money = _money_currency(f"{text} {currency}") not in {None, "THB"}
        is_thb = (
            re.search(r"\b(THB|baht)\b", text, re.I)
            or currency.upper() == "THB"
            or "฿" in text
        )
        if is_thb and not foreign_money:
            from app.services.admin_property_fields import parse_money
            number = parse_money(text)
            if number and number > 0:
                fields["price_numeric_min"] = _prov(
                    int(number) if number == int(number) else number,
                    "inferred",
                    "rule",
                    reason="price_numeric_min parsed from price_display",
                )
    beds = fields.get("bedrooms_display") or fields.get("bedrooms_min")
    bed_text = str((beds or {}).get("value") or "")
    range_match = re.search(r"(\d+)\s*[-–]\s*(\d+)", bed_text) or re.search(r"(\d+)\s*[-–]\s*(\d+)\s*beds?", page_text, re.I)
    if range_match and "bedrooms_min" not in fields:
        low, high = int(range_match.group(1)), int(range_match.group(2))
        fields["bedrooms_display"] = _prov(f"{low}-{high}", "inferred", "rule", reason="bedrooms range parsed from the page")
        fields["bedrooms_min"] = _prov(low, "inferred", "rule", reason="bedrooms_min parsed from a range such as '2-3 beds'")
        fields["bedrooms_max"] = _prov(high, "inferred", "rule", reason="bedrooms_max parsed from a range such as '2-3 beds'")
    elif beds and "bedrooms_min" not in fields:
        single = re.search(r"\d+", bed_text)
        if single:
            count = int(single.group(0))
            fields["bedrooms_min"] = _prov(count, "inferred", "rule", reason="bedrooms_min parsed from the bedroom text")
            fields["bedrooms_max"] = _prov(count, "inferred", "rule", reason="bedrooms_max parsed from the bedroom text")


def _apply_guesses(fields: Dict[str, Dict[str, Any]], page_text: str) -> None:
    low = page_text.lower()
    if "status" not in fields and re.search(r"completion\s+20\d\d", low):
        year = re.search(r"completion\s+(20\d\d)", low)
        fields["status"] = _prov(
            "Under construction",
            "guess",
            "page",
            evidence=year.group(0) if year else "completion",
            reason=f"the page says '{year.group(0)}'" if year else "the page mentions a future completion",
            confidence="medium",
        )
    # Price, size, beds, baths, ownership, ROI and coordinates are never guessed.


def _money_currency(text: str) -> Optional[str]:
    blob = text or ""
    if re.search(r"₹|\bINR\b|\bRs\.?\b|\bLacs?\b|\bLakhs?\b|\bCrores?\b|\d(?:\.\d+)?\s*[-_]?\s*cr\b", blob, re.I):
        return "INR"
    if re.search(r"฿|\bTHB\b|\bbaht\b", blob, re.I):
        return "THB"
    if "$" in blob or re.search(r"\bUSD\b", blob, re.I):
        return "USD"
    if re.search(r"€|\bEUR\b", blob, re.I):
        return "EUR"
    if re.search(r"\bPLN\b|zł", blob, re.I):
        return "PLN"
    return None


def _foreign_price_needs_decision(fields: Dict[str, Dict[str, Any]]) -> None:
    currency = str((fields.get("currency") or {}).get("value") or "")
    display = str((fields.get("price_display") or {}).get("value") or "")
    detected = _money_currency(f"{currency} {display}")
    if detected and detected != "THB":
        if "price_display" in fields:
            fields["price_display"]["needs_decision"] = True
            fields["price_display"]["reason"] = "the page shows a non-THB price and no THB amount"
        fields.pop("price_numeric_min", None)
        fields.pop("price_numeric_max", None)


def _images_from(parser: _PageParser, base_url: str) -> List[str]:
    kept = []
    for src, hint in parser.images:
        if not src or src.startswith("data:"):
            continue
        absolute = urljoin(base_url, src)
        if not absolute.lower().startswith("https://"):
            continue
        name = absolute.lower()
        if any(token in name or token in hint.lower() for token in _IMAGE_SKIP):
            continue
        width = re.search(r"\b(\d{1,4})\b", hint)
        if width and int(width.group(1)) < 50 and "width" in hint.lower():
            continue
        if absolute not in kept:
            kept.append(absolute)
        if len(kept) >= 12:
            break
    return kept


def _labeled_pairs(parser: _PageParser) -> Dict[str, Dict[str, Any]]:
    lookup = {
        "name": "property_name", "title": "property_name", "property name": "property_name",
        "type": "property_type", "property type": "property_type",
        "status": "status", "price": "price_display", "location": "address",
        "address": "address", "province": "province", "district": "district",
        "bedrooms": "bedrooms_display", "beds": "bedrooms_display",
        "bathrooms": "bathrooms", "baths": "bathrooms",
        "area": "area_display", "size": "area_display",
        "description": "description", "developer": "developer_name",
        "ownership": "ownership_type",
    }
    out: Dict[str, Dict[str, Any]] = {}
    for label, value in parser.tables:
        key = lookup.get(label.strip().lower())
        if key and value and key not in out:
            out[key] = _prov(value, "found", "html", evidence=f"{label}: {value[:80]}")
    meta_map = {
        "og:title": "property_name",
        "og:description": "description",
        "product:price:amount": "price_numeric_min",
        "product:price:currency": "currency",
    }
    for meta_key, field in meta_map.items():
        if meta_key in parser.meta and field not in out:
            out[field] = _prov(parser.meta[meta_key], "found", "opengraph", evidence=meta_key)
    return out


_NON_LISTING_LD_TYPES = (
    "Organization", "Corporation", "LocalBusiness", "WebSite", "WebPage", "BreadcrumbList",
    "SearchAction", "ImageObject", "Person", "ContactPoint", "SiteNavigationElement",
)


def _json_blocks(parser: _PageParser) -> List[Dict[str, Any]]:
    blocks = []
    for raw in parser.ldjson:
        try:
            parsed = json.loads(raw)
        except Exception:
            continue
        items = parsed if isinstance(parsed, list) else [parsed]
        flat: List[Any] = []
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                flat.extend(item["@graph"])
            else:
                flat.append(item)
        for item in flat:
            if isinstance(item, dict):
                raw_kind = item.get("@type") or ""
                kind = " ".join(str(k) for k in raw_kind) if isinstance(raw_kind, list) else str(raw_kind)
                # The site's own company, page or breadcrumb data is not the listing:
                # its name and head-office address would overwrite the property's.
                if any(token in kind for token in _NON_LISTING_LD_TYPES) and not any(
                    token in kind for token in ("RealEstate", "Residence", "Apartment", "House", "Accommodation")
                ):
                    continue
                if any(token in kind for token in ("RealEstate", "Product", "Residence", "Offer", "Apartment", "House", "Place")):
                    blocks.append(item)
                elif "name" in item:
                    blocks.append(item)
    for raw in (parser.next_data,):
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except Exception:
            continue
        if isinstance(parsed, dict):
            blocks.append(parsed)
    return blocks


def _llm_fields(page_text: str) -> Dict[str, Dict[str, Any]]:
    from app.services.property_extractor import extract_property_data_with_llm, prepare_untrusted_page_text

    wrapped, _ignored = prepare_untrusted_page_text(page_text)
    data = extract_property_data_with_llm(wrapped)
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    low = page_text.lower()
    for key, value in data.items():
        if key in _NEVER_GUESS and key.startswith("price"):
            pass
        if _blank(value) or isinstance(value, (dict, list)):
            continue
        token = str(value).strip()
        number = _as_number(token) if key.startswith("price") or key.startswith("bedroom") or key in {"bathrooms", "area_sqm_min"} else None
        supported = token.lower() in low or (number is not None and str(int(number)) in re.sub(r"[^\d]", "", page_text))
        if not supported:
            continue
        out[key] = _prov(value, "found", "llm", evidence=token[:80])
    return out


_VISIBLE_LABELS = (
    ("property information", None),
    ("property type", "property_type"),
    ("property name", "property_name"),
    ("bedrooms", "bedrooms_display"),
    ("bedroom", "bedrooms_display"),
    ("bathrooms", "bathrooms"),
    ("bathroom", "bathrooms"),
    ("location", "address"),
    ("address", "address"),
    ("developer", "developer_name"),
    ("status", "status"),
    ("price", "price_display"),
    ("type", "property_type"),
    ("area", "area_display"),
    ("name", "property_name"),
)
_LABEL_ALT = "|".join(re.escape(label) for label, _field in _VISIBLE_LABELS)
_LABEL_LINE = re.compile(rf"(?i)^({_LABEL_ALT})\s*(?:[:=-]\s*|\s+)(.+)$")
_PRICE_VALUE = re.compile(
    r"(?i)(₹\s*[0-9][0-9,.]*(?:\s*(?:lac|lakh|lacs|lakhs|cr|crore|crores))?|"
    r"\brs\.?\s*[0-9][0-9,.]*(?:\s*(?:lac|lakh|lacs|lakhs|cr|crore|crores))?|"
    r"\b[0-9][0-9,.]*(?:\s+|-)(?:lac|lakh|lacs|lakhs|cr|crore|crores)\b|"
    r"\b[0-9][0-9,.]*\s*(?:million|mn|mil)\s*(?:thb|baht)\b|"
    r"\b[0-9][0-9,.]*(?:\s*thb|\s*baht|฿))"
)
_SLUG_PRICE = re.compile(r"(\d+(?:\.\d+)?)[-_](lac|lakh|lacs|lakhs|cr|crore|crores|million|mn|mil)\b", re.I)
_SITE_SUFFIX = re.compile(
    r"\s*[\|\-–—]\s*(?:real\s*estate\s*india|realestateindia|99acres|magicbricks|zameen|housing\.com).*$",
    re.IGNORECASE,
)


def _clean_listing_name(text: str) -> str:
    name = re.sub(r"\s+", " ", text or "").strip()
    name = _SITE_SUFFIX.sub("", name)
    name = re.sub(r"\s*\((?:[A-Za-z]{0,8}\d{4,}|listing\s*id[^)]*)\)", "", name, flags=re.I)
    name = re.sub(r"\s*listing\s*id\s*[:#-]?\s*[A-Za-z0-9-]+", "", name, flags=re.I)
    return name.strip(" |-–—")


def _single_label_line(line: str) -> Optional[Tuple[str, str]]:
    match = _LABEL_LINE.match((line or "").strip())
    if not match:
        return None
    label = re.sub(r"\s+", " ", match.group(1).strip().lower())
    value = match.group(2).strip().rstrip(".").strip()
    if not value or label == "property information":
        return None
    other_labels = [item for item, _field in _VISIBLE_LABELS if item != label and item != "property information"]
    if any(re.search(r"\b" + re.escape(item) + r"\b", value, re.I) for item in other_labels):
        return None
    field = dict(_VISIBLE_LABELS).get(label)
    if not field:
        return None
    return field, value


def _price_phrase(main_text: str, url: str) -> Optional[Tuple[str, Optional[str]]]:
    for line in (main_text or "").splitlines():
        labeled = _single_label_line(line)
        if labeled and labeled[0] == "price_display":
            return labeled[1], _money_currency(labeled[1])
        found = _PRICE_VALUE.search(line)
        if found:
            phrase = found.group(0).strip()
            return phrase, _money_currency(phrase)
    slug = _SLUG_PRICE.search(url or "")
    if slug:
        phrase = slug.group(0)
        unit = slug.group(2).lower()
        currency = "INR" if unit in {"lac", "lakh", "lacs", "lakhs", "cr", "crore", "crores"} else _money_currency(phrase)
        return phrase, currency
    return None


def _apply_visible_fallbacks(fields: Dict[str, Dict[str, Any]], parser: _PageParser, page: FetchedPage) -> None:
    """Fill gaps after JSON-LD, Open Graph and table cells. Does not overwrite them."""
    if "property_name" not in fields:
        name = _clean_listing_name(parser.h1) or _clean_listing_name(parser.title)
        if name:
            source = "h1" if parser.h1 else "title"
            fields["property_name"] = _prov(name, "found", "html", evidence=source)
    for line in parser.lines:
        pair = _single_label_line(line)
        if not pair:
            continue
        field, value = pair
        if field not in fields and value:
            fields[field] = _prov(value, "found", "html", evidence=f"{field}: {value[:80]}")
    if "price_display" not in fields and "price_numeric_min" not in fields:
        found = _price_phrase(parser.main_text(), page.final_url or page.url)
        if found:
            phrase, currency = found
            needs = currency not in {None, "THB"}
            fields["price_display"] = _prov(
                phrase,
                "found",
                "html",
                evidence=phrase[:80],
                needs_decision=needs,
                reason="" if not needs else "the page shows a non-THB price and no THB amount",
            )
            if currency and "currency" not in fields:
                fields["currency"] = _prov(currency, "found", "html", evidence="currency marker")


def extract_draft(page: FetchedPage) -> Dict[str, Any]:
    """Build a provenance draft. Invented model values stay missing."""
    from app.services.property_extractor import prepare_untrusted_page_text

    fields: Dict[str, Dict[str, Any]] = {}
    images: List[str] = []
    ctype = (page.content_type or "").lower()
    text = page.text or ""
    _cleaned, injection = prepare_untrusted_page_text(text)

    if "json" in ctype or text.lstrip()[:1] in "{[":
        try:
            payload = json.loads(text)
            fields.update(_fields_from_mapping(_unwrap_json(payload), "site_api"))
        except Exception:
            fields = {}

    parser = _parse_html(text) if not _enough(fields) or "html" in ctype else _parse_html(text)
    if not _enough(fields):
        for block in _json_blocks(parser):
            fields.update({k: v for k, v in _fields_from_mapping(_unwrap_json(block), "json_ld").items() if k not in fields})
            if _enough(fields):
                break
    if not _enough(fields):
        for key, prov in _labeled_pairs(parser).items():
            fields.setdefault(key, prov)
    if not _enough(fields):
        _apply_visible_fallbacks(fields, parser, page)
    if not _enough(fields):
        for key, prov in _llm_fields(text).items():
            fields.setdefault(key, prov)

    images = _images_from(parser, page.final_url)
    outside = _foreign_place(fields, parser.main_text(), page.final_url, parser.title or parser.h1)
    _normalize_type_status(fields)
    _apply_inferences(fields, text, main_text=parser.main_text(), title=parser.title or parser.h1, allow_location=not outside)
    _foreign_price_needs_decision(fields)
    _apply_guesses(fields, text)
    _normalize_type_status(fields)

    from app.services.action_service import name_slug, property_id_slug_error

    name = str((fields.get("property_name") or {}).get("value") or "").strip()
    slug = name_slug(name)
    if property_id_slug_error(slug):
        slug = "prop-" + page.content_hash[:8]
    return {
        "source_url": page.url,
        "final_url": page.final_url,
        "source_site": urlparse(page.final_url).hostname or "",
        "fetched_at": page.fetched_at,
        "content_hash": page.content_hash,
        "provenance": fields,
        "images": images,
        "cover_image": images[0] if images else None,
        "property_id": slug,
        "injection_warning": injection,
        "mode": "create",
        "guesses_accepted": [],
        "foreign_place": outside,
        "foreign_acknowledged": False,
    }


def _source_label(prov: Dict[str, Any]) -> str:
    status = prov.get("status")
    if status == "found":
        detail = prov.get("evidence") or prov.get("source") or "page"
        return f"Found - {detail}"
    if status == "inferred":
        return f"Inferred - {prov.get('reason') or 'derived from found data'}"
    if status == "guess":
        return f"Guess - {prov.get('reason') or 'from the page'} ({prov.get('confidence') or 'low'})"
    if status == "provided":
        return "provided by admin"
    return "Missing"


def _row(field: str, prov: Optional[Dict[str, Any]]) -> Dict[str, str]:
    if not prov:
        return {"field": field, "value": "", "source": "Missing"}
    value = prov.get("value")
    if prov.get("needs_decision"):
        return {"field": field, "value": value, "source": "Found - needs your decision (no THB price on the page)"}
    return {"field": field, "value": "" if value is None else value, "source": _source_label(prov)}


def _price_note(shown: str) -> str:
    detail = (shown or "").strip()
    if detail:
        return f"missing (page shows {detail}, not THB. Tell me the price in THB)"
    return "missing (tell me the price in THB)"


def _price_preview_row(provenance: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """One Price row. price, price_display and price_numeric_min are not listed separately."""
    display = provenance.get("price_display") or {}
    numeric = provenance.get("price_numeric_min") or {}
    shown = str(display.get("value") or "").strip()
    if display.get("needs_decision") or numeric.get("needs_decision"):
        return {"field": "price", "value": _price_note(shown or str(numeric.get("value") or "")), "source": "Missing"}
    ready_number = (
        numeric.get("status") in {"found", "inferred", "provided"}
        and not _blank(numeric.get("value"))
    )
    ready_text = display.get("status") in {"found", "inferred", "provided"} and bool(shown)
    if ready_text and _money_currency(shown) not in {None, "THB"}:
        return {"field": "price", "value": _price_note(shown), "source": "Missing"}
    if ready_number or ready_text:
        if ready_text:
            return {"field": "price", "value": shown, "source": _source_label(display)}
        return {"field": "price", "value": numeric.get("value"), "source": _source_label(numeric)}
    return {"field": "price", "value": _price_note(""), "source": "Missing"}


def _include_field(field: str, only_fields: Optional[List[str]]) -> bool:
    if only_fields is None:
        return True
    if field in only_fields:
        return True
    if field == "price" and any(item in only_fields for item in ("price", "price_display", "price_numeric_min", "price_numeric_max", "currency")):
        return True
    return False


def preview_rows(draft: Dict[str, Any], *, only_fields: Optional[List[str]] = None) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    provenance = draft.get("provenance") or {}
    decision_fields = []
    other_fields = []
    interesting = [
        "property_name", "property_type", "status", "province", "district", "area", "address",
        "bedrooms_display", "bedrooms_min", "bedrooms_max", "bathrooms",
        "area_sqm_min", "area_sqm_max", "description", "developer_name", "ownership_type",
    ]
    for field in interesting:
        if not _include_field(field, only_fields):
            continue
        prov = provenance.get(field)
        row = _row(field, prov)
        needs = (
            prov is None
            or prov.get("status") in {"missing", "guess"}
            or prov.get("needs_decision")
            or (prov.get("status") == "guess" and prov.get("confidence") == "low")
        )
        if field in {"property_name", "property_type", "status", "province"} and needs:
            decision_fields.append(row)
        elif needs and prov and prov.get("status") == "guess":
            decision_fields.append(row)
        elif prov is None and field in {"property_name", "property_type", "status", "province"}:
            decision_fields.append(row)
        elif prov:
            other_fields.append(row)
    if "province" not in provenance and "area" not in provenance and _include_field("location", only_fields):
        if not any(row["field"] == "province" for row in decision_fields):
            decision_fields.append({"field": "location", "value": "", "source": "Missing"})
    if _include_field("price", only_fields):
        price_row = _price_preview_row(provenance)
        if price_row["source"] == "Missing":
            decision_fields.append(price_row)
        else:
            other_fields.append(price_row)
    return decision_fields, other_fields


def _location_ready(provenance: Dict[str, Dict[str, Any]]) -> bool:
    for key in ("province", "area", "district", "address"):
        prov = provenance.get(key)
        if prov and prov.get("status") in {"found", "inferred", "provided"} and not _blank(prov.get("value")):
            return True
    return False


def _price_ready(provenance: Dict[str, Dict[str, Any]]) -> bool:
    for key in ("price_numeric_min", "price_display"):
        prov = provenance.get(key)
        if not prov or prov.get("needs_decision"):
            continue
        if prov.get("status") in {"found", "inferred", "provided"} and not _blank(prov.get("value")):
            return True
    return False


def missing_requirements(draft: Dict[str, Any]) -> List[str]:
    provenance = draft.get("provenance") or {}
    missing = []
    name = provenance.get("property_name")
    if not name or name.get("status") not in {"found", "inferred", "provided"} or _blank(name.get("value")):
        missing.append("property_name")
    kind = provenance.get("property_type")
    if not kind or kind.get("status") not in {"found", "inferred", "provided"} or _blank(kind.get("value")):
        missing.append("property_type")
    if not _location_ready(provenance):
        missing.append("location")
    if not _price_ready(provenance):
        missing.append("price")
    status = provenance.get("status")
    if not status or _blank(status.get("value")):
        missing.append("status")
    elif status.get("status") == "guess" and status.get("confidence") == "low" and "status" not in (draft.get("guesses_accepted") or []):
        missing.append("status")
    elif status.get("status") not in {"found", "inferred", "provided", "guess"}:
        missing.append("status")
    return missing


def unaccepted_guesses(draft: Dict[str, Any]) -> List[str]:
    accepted = set(draft.get("guesses_accepted") or [])
    found = []
    for field, prov in (draft.get("provenance") or {}).items():
        if prov.get("status") == "guess" and field not in accepted and field not in _NEVER_GUESS:
            found.append(field)
    return found


def savable_fields(draft: Dict[str, Any]) -> Dict[str, Any]:
    accepted = set(draft.get("guesses_accepted") or [])
    out: Dict[str, Any] = {}
    for field, prov in (draft.get("provenance") or {}).items():
        if field in _NEVER_GUESS and prov.get("status") == "guess":
            continue
        if prov.get("needs_decision") and prov.get("status") != "provided":
            continue
        status = prov.get("status")
        if status in {"found", "inferred", "provided"} or (status == "guess" and field in accepted):
            value = prov.get("value")
            if isinstance(value, (dict, list)) or _blank(value):
                continue
            out[field] = value
    if "province" not in out and out.get("address"):
        out["location"] = out["address"]
        out["province"] = out["address"]
    if out.get("bedrooms_min") is not None and "bedrooms" not in out:
        out["bedrooms"] = out["bedrooms_min"]
    previewed = draft.get("previewed_values")
    if isinstance(previewed, dict) and previewed:
        allowed = set(previewed)
        if "price_display" in allowed or "price_numeric_min" in allowed or "price" in allowed:
            allowed.update({"price_display", "price_numeric_min", "currency"})
        if "bedrooms_display" in allowed or "bedrooms_min" in allowed:
            allowed.update({"bedrooms_display", "bedrooms_min", "bedrooms_max", "bedrooms"})
        out = {key: value for key, value in out.items() if key in allowed}
    return out


def _record_previewed(draft: Dict[str, Any], rows: List[Dict[str, str]]) -> None:
    shown = dict(draft.get("previewed_values") or {})
    for row in rows:
        source = str(row.get("source") or "")
        field = row.get("field")
        if not field:
            continue
        if source == "Missing" or "needs your decision" in source.lower():
            shown.pop(field, None)
            continue
        if row.get("value") in (None, ""):
            continue
        shown[field] = row.get("value")
    draft["previewed_values"] = shown


def render_preview(draft: Dict[str, Any], *, changed_only: Optional[List[str]] = None, superseded: Optional[str] = None) -> str:
    from app.services.admin_change_diff import format_provenance_table

    full_decision, full_other = preview_rows(draft)
    _record_previewed(draft, full_decision + full_other)
    decision, other = preview_rows(draft, only_fields=changed_only)
    lines = []
    if _foreign_blocks(draft):
        lines.append(foreign_listing_warning(str(draft.get("foreign_place") or ""), str(draft.get("reply_language") or "en")))
        lines.append("")
    if superseded:
        lines.append(f"The previous draft for {superseded} was discarded.")
        lines.append("")
    if draft.get("injection_warning"):
        lines.append("The page contained text that looked like instructions; I ignored it.")
        lines.append("")
    if changed_only:
        lines.append("Updated")
        lines.append(format_provenance_table(decision + other))
    else:
        lines.append(f"Source: {draft.get('source_url')} ({draft.get('source_site')})")
        lines.append(f"Target websites: {', '.join(draft.get('websites') or []) or '—'}")
        lines.append(f"Images: {len(draft.get('images') or [])}")
        duplicate = draft.get("duplicate")
        if duplicate:
            lines.append(
                f"Duplicate: this matches existing '{duplicate.get('name')}' ({duplicate.get('property_id')}) in {duplicate.get('store')}. "
                "Confirm will update that listing. Say 'create new' if you wanted a separate property; the same name cannot be created twice."
            )
        else:
            lines.append("Duplicate: none")
        lines.append("")
        lines.append("Needs your decision")
        lines.append(format_provenance_table(decision or [{"field": "—", "value": "—", "source": "nothing else"}]))
        if other:
            lines.append("")
            lines.append("Found and inferred")
            lines.append(format_provenance_table(other))
    missing = missing_requirements(draft)
    if not draft.get("websites"):
        missing.append("website (say 'add to tajlandia.com')")
    if missing and not changed_only:
        lines.append("")
        lines.append("I still need: " + ", ".join(missing) + ".")
    guesses = unaccepted_guesses(draft)
    if guesses and not changed_only:
        lines.append("")
        lines.append(
            "Guessed, not saved until you accept: " + ", ".join(guesses)
            + ". Say 'use your guesses' to accept them, or give me the value."
        )
    lines.append("")
    lines.append(CONFIRM_FOOTER)
    return "\n".join(lines)


def _set_provided(draft: Dict[str, Any], field: str, value: Any) -> None:
    draft.setdefault("provenance", {})[field] = _prov(value, "provided", "admin", reason="provided by admin")
    draft.setdefault("admin_fields", [])
    if field not in draft["admin_fields"]:
        draft["admin_fields"].append(field)


# Whole-message confirmations that is_strict_confirm_message does not cover. Only used while a URL draft is open.
_EXTRA_CONFIRM_PHRASES = {
    "save", "save it", "save this", "save it now", "save listing", "save the listing", "save the property",
    "go ahead", "go ahead and save", "yes go ahead", "ok go ahead", "okay go ahead",
    "looks good", "looks good go ahead", "looks good save it", "all good", "all good save it",
}


def _phrase_key(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower(), flags=re.UNICODE)).strip()


_REQUEST_OPENERS = {
    "show", "give", "get", "list", "find", "search", "email", "send", "delete", "update", "create", "tell",
    "explain", "what", "how", "why", "when", "where", "who", "which", "generate", "export", "undo", "remove",
}
_MONEY_RE = re.compile(
    r"(?i)(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(million|mln|m|thousand|k)?(?![A-Za-z])\s*(thb|baht|฿)?"
)


def _find_price_phrase(text: str) -> Optional[str]:
    """A price only when the admin marks it: a unit (million/M/k), a currency word, or a 6+ digit number."""
    from app.services.admin_property_fields import parse_money

    for match in _MONEY_RE.finditer(text or ""):
        digits = match.group(1)
        unit, currency = match.group(2), match.group(3)
        plain = digits.replace(",", "")
        if not (unit or currency or len(plain.split(".")[0]) >= 6):
            continue
        phrase = match.group(0).strip()
        if parse_money(phrase):
            return phrase
    return None


def parse_missing_field_answer(message: Optional[str], missing: List[str]) -> Optional[Dict[str, Any]]:
    """
    Read a free-text reply like "25 million baht, under construction" into set commands.
    Only fields the draft still needs are read, so an answer never overrides a value that is already there.
    """
    from app.services.action_service import ALLOWED_PROPERTY_TYPES, ALLOWED_STATUSES

    raw = (message or "").strip()
    if not raw or re.search(r"https?://", raw, re.I):
        return None
    low = re.sub(r"\s+", " ", raw.lower())
    commands: List[Dict[str, Any]] = []
    if "price" in missing:
        phrase = _find_price_phrase(raw)
        if phrase:
            commands.append({"op": "set", "field": "price", "value": phrase})
    if "status" in missing:
        keys = sorted(set(ALLOWED_STATUSES) | {"ready", "ready to move"}, key=len, reverse=True)
        for key in keys:
            if re.search(rf"(?<![\w-]){re.escape(key)}(?![\w-])", low):
                commands.append({"op": "set", "field": "status", "value": key})
                break
    if "property_type" in missing:
        for key in sorted(set(ALLOWED_PROPERTY_TYPES) | {"plot", "plots"}, key=len, reverse=True):
            if re.search(rf"(?<![\w-]){re.escape(key)}(?![\w-])", low):
                commands.append({"op": "set", "field": "property_type", "value": key})
                break
    if not commands:
        return None
    return {"op": "multi", "commands": commands, "rejected": [], "report": False}


_ANSWER_LIKE_RE = re.compile(
    r"(?i)\d|\b(price|priced|cost|baht|thb|million|status|type|villa|condo|condominium|land|plot|plots|house|"
    r"apartment|townhouse|penthouse|commercial|ready|plan|construction|sold|reserved|available|planned)\b"
)


def _freeform_answer(draft_action: Any, message: Optional[str]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    For a reply parse_draft_command did not understand while the draft still lacks required fields.
    Returns (command, None) when the reply answers a missing field, (None, nudge) when it looks like an
    answer but could not be read, and (None, None) for anything else so other handlers still see it.
    """
    text = (message or "").strip()
    words = text.split()
    if not text or "?" in text or len(words) > 20 or (words and words[0].lower().strip(",.:;") in _REQUEST_OPENERS):
        return None, None
    payload = draft_action.action_payload if isinstance(getattr(draft_action, "action_payload", None), dict) else {}
    missing = missing_requirements(payload)
    if not missing:
        return None, None
    command = parse_missing_field_answer(text, missing)
    if command:
        return command, None
    if not _ANSWER_LIKE_RE.search(text):
        return None, None
    nudge = (
        "I could not read that as a missing value. I still need: "
        + ", ".join(_missing_phrase(payload, field) for field in missing)
        + ". For example: 'price 25000000', 'status off plan', 'type villa'. Nothing was saved.\n\n"
        + CONFIRM_FOOTER
    )
    return None, nudge


def parse_draft_command(message: Optional[str]) -> Optional[Dict[str, Any]]:
    from app.services.admin_agent import is_strict_confirm_message, is_strict_reject_message
    from app.services.action_service import ALLOWED_PROPERTY_TYPES, ALLOWED_STATUSES

    raw = (message or "").strip()
    low = re.sub(r"\s+", " ", raw.lower())
    if not low:
        return None
    if low in {"confirm all", "yes save all", "save all"}:
        return {"op": "confirm_all"}
    if is_strict_reject_message(raw) or low in {"cancel"}:
        return {"op": "cancel"}
    if is_strict_confirm_message(raw) or low in {"yes save it", "yes save"} or _phrase_key(raw) in _EXTRA_CONFIRM_PHRASES:
        return {"op": "confirm"}
    if low.rstrip(".") == "continue anyway":
        return {"op": "continue_anyway"}
    skip = re.fullmatch(r"skip ([a-z_ ]+)", low)
    if skip:
        return {"op": "skip", "field": skip.group(1).strip().replace(" ", "_")}
    from app.services.admin_chat_quality import parse_natural_edit_commands

    natural = parse_natural_edit_commands(raw)
    if natural:
        sets = [item for item in natural if item.get("op") == "set"]
        rejected = [item for item in natural if item.get("op") != "set"]
        return {"op": "multi", "commands": sets, "rejected": rejected, "report": len(natural) > 1 or bool(rejected)}
    if low in {"use your guesses", "accept guesses", "accept all guesses"}:
        return {"op": "accept_guesses"}
    accept = re.fullmatch(r"accept ([a-z_ ]+)", low)
    if accept:
        return {"op": "accept_field", "field": accept.group(1).strip().replace(" ", "_")}
    ignore = re.fullmatch(r"ignore the guess for ([a-z_ ]+)", low)
    if ignore:
        return {"op": "ignore_guess", "field": ignore.group(1).strip().replace(" ", "_")}
    if low == "create new":
        return {"op": "create_new"}
    image = re.fullmatch(r"remove image (\d+)", low)
    if image:
        return {"op": "remove_image", "index": int(image.group(1))}
    website = re.fullmatch(r"add to ([a-z0-9.-]+)(?: too)?", low)
    if website:
        return {"op": "add_website", "website": website.group(1)}
    price = re.fullmatch(r"(?:set )?price (.+)", low)
    if price:
        return {"op": "set", "field": "price", "value": price.group(1).strip()}
    beds = re.fullmatch(r"(?:set )?bedrooms? (\d+)", low)
    if beds:
        return {"op": "set", "field": "bedrooms", "value": int(beds.group(1))}
    baths = re.fullmatch(r"(?:set )?bathrooms? (\d+)", low)
    if baths:
        return {"op": "set", "field": "bathrooms", "value": int(baths.group(1))}
    status = re.fullmatch(r"(?:set )?status (.+)", low)
    if status:
        token = status.group(1).strip()
        canon = ALLOWED_STATUSES.get(token) or ALLOWED_STATUSES.get(token.replace("-", " "))
        if token in {"ready", "ready to move"}:
            canon = "Ready to move in"
        return {"op": "set", "field": "status", "value": canon or token}
    kind = re.fullmatch(r"(?:set )?type (.+)", low)
    if kind:
        token = kind.group(1).strip()
        return {"op": "set", "field": "property_type", "value": ALLOWED_PROPERTY_TYPES.get(token, token.title())}
    loc = re.fullmatch(r"(?:set )?location (.+)", low)
    if loc:
        return {"op": "set", "field": "location", "value": loc.group(1).strip()}
    return None


def _parse_admin_price(text: str) -> Tuple[Optional[float], str]:
    from app.services.admin_property_fields import parse_money

    value = parse_money(text)
    if value and value > 0:
        shown = int(value) if value == int(value) else value
        return float(shown), f"{shown:,.0f} THB" if isinstance(shown, float) else f"{shown:,} THB"
    return None, text


_REQUIRED_SKIP_BLOCK = {"property_name", "name", "price", "price_display", "property_type", "status", "websites", "website"}


def skip_is_refused(field: str) -> bool:
    token = (field or "").strip().lower().replace(" ", "_")
    return token in _REQUIRED_SKIP_BLOCK


def apply_command(draft: Dict[str, Any], command: Dict[str, Any]) -> List[str]:
    """Update the draft in memory. Returns the field names that changed. Never writes."""
    op = command.get("op")
    changed: List[str] = []
    if op == "multi":
        for sub in command.get("commands") or []:
            changed.extend(apply_command(draft, sub))
        return changed
    if op == "set" and command.get("field") == "price":
        from app.services.admin_property_fields import parse_money, price_sanity_message

        raw_price = str(command.get("value") or "")
        number, display = _parse_admin_price(raw_price)
        current_raw = ((draft.get("provenance") or {}).get("price_numeric_min") or {}).get("value")
        current_price = parse_money(current_raw) if current_raw is not None else None
        if number is not None:
            refusal = price_sanity_message(number, current_price)
            if refusal:
                draft["price_warning"] = refusal
                return changed
        _set_provided(draft, "price_display", display)
        changed.append("price_display")
        if number:
            _set_provided(draft, "price_numeric_min", int(number) if number == int(number) else number)
            _set_provided(draft, "currency", "THB")
            changed.extend(["price_numeric_min", "currency"])
    elif op == "set" and command.get("field") == "bedrooms":
        count = int(command["value"])
        _set_provided(draft, "bedrooms_min", count)
        _set_provided(draft, "bedrooms_max", count)
        _set_provided(draft, "bedrooms_display", str(count))
        changed.extend(["bedrooms_min", "bedrooms_max", "bedrooms_display"])
    elif op == "set" and command.get("field") == "bathrooms":
        _set_provided(draft, "bathrooms", int(command["value"]))
        changed.append("bathrooms")
    elif op == "set" and command.get("field") == "status":
        from app.services.admin_chat_quality import _canon_status_value
        from app.services.action_service import ALLOWED_STATUSES

        canon = _canon_status_value(str(command.get("value") or ""))
        if not canon:
            draft.setdefault("apply_rejected", []).append({
                "field": "status",
                "value": command.get("value"),
                "reason": "Status must be one of: " + ", ".join(sorted(set(ALLOWED_STATUSES.values()))),
            })
        else:
            _set_provided(draft, "status", canon)
            changed.append("status")
    elif op == "set" and command.get("field") == "property_type":
        from app.services.action_service import ALLOWED_PROPERTY_TYPES

        canon = _canon_property_type(str(command.get("value") or ""))
        if not canon:
            draft.setdefault("apply_rejected", []).append({
                "field": "property_type",
                "value": command.get("value"),
                "reason": "Type must be one of: " + ", ".join(sorted(set(ALLOWED_PROPERTY_TYPES.values()))),
            })
        else:
            _set_provided(draft, "property_type", canon)
            changed.append("property_type")
    elif op == "set" and command.get("field") == "province":
        text = str(command.get("value") or "").strip()
        if text:
            _set_provided(draft, "province", text[:1].upper() + text[1:])
            changed.append("province")
    elif op == "set" and command.get("field") == "property_name":
        text = str(command.get("value") or "").strip()
        if text:
            _set_provided(draft, "property_name", text)
            changed.append("property_name")
    elif op == "set" and command.get("field") == "has_pool":
        _set_provided(draft, "has_pool", bool(command.get("value")))
        changed.append("has_pool")
    elif op == "set" and command.get("field") == "parking":
        _set_provided(draft, "parking_spaces", int(command["value"]))
        changed.append("parking_spaces")
    elif op == "keep_page":
        provenance = draft.get("provenance") or {}
        kept = False
        for field in ("price_numeric_min", "price_display"):
            prov = provenance.get(field)
            if not prov or _blank(prov.get("value")):
                continue
            if prov.get("status") not in {"found", "inferred", "provided"}:
                continue
            prov["needs_decision"] = False
            prov["status"] = "provided"
            prov["reason"] = "admin kept the page value"
            kept = True
            changed.append(field)
        if not kept:
            draft["keep_page_missing"] = True
    elif op == "set" and command.get("field") == "location":
        text = str(command.get("value") or "")
        _set_provided(draft, "address", text)
        changed.append("address")
        hit = _district_hit(text)
        if hit:
            _key, (province, district, area) = hit
            _set_provided(draft, "province", province)
            _set_provided(draft, "district", district)
            _set_provided(draft, "area", area)
            changed.extend(["province", "district", "area"])
        else:
            _set_provided(draft, "province", text)
            changed.append("province")
    elif op == "accept_guesses":
        for field, prov in (draft.get("provenance") or {}).items():
            if prov.get("status") == "guess" and field not in _NEVER_GUESS:
                draft.setdefault("guesses_accepted", [])
                if field not in draft["guesses_accepted"]:
                    draft["guesses_accepted"].append(field)
                changed.append(field)
    elif op == "accept_field":
        field = str(command.get("field") or "")
        if field in (draft.get("provenance") or {}) and draft["provenance"][field].get("status") == "guess" and field not in _NEVER_GUESS:
            draft.setdefault("guesses_accepted", [])
            if field not in draft["guesses_accepted"]:
                draft["guesses_accepted"].append(field)
            changed.append(field)
    elif op == "ignore_guess":
        field = str(command.get("field") or "")
        prov = (draft.get("provenance") or {}).get(field)
        if prov and prov.get("status") == "guess":
            draft["provenance"].pop(field, None)
            draft["guesses_accepted"] = [item for item in (draft.get("guesses_accepted") or []) if item != field]
            changed.append(field)
    elif op == "remove_image":
        images = list(draft.get("images") or [])
        index = int(command.get("index") or 0) - 1
        if 0 <= index < len(images):
            images.pop(index)
            draft["images"] = images
            draft["cover_image"] = images[0] if images else None
            changed.append("images")
    elif op == "add_website":
        sites = list(draft.get("websites") or [])
        site = str(command.get("website") or "")
        if site and site not in sites:
            sites.append(site)
        draft["websites"] = sites
        changed.append("websites")
    elif op == "create_new":
        draft["mode"] = "create"
        draft["duplicate"] = None
        changed.append("mode")
    elif op == "continue_anyway":
        draft["foreign_acknowledged"] = True
        changed.append("foreign_listing")
    return changed


def find_duplicate(db: Any, url: str, name: str) -> Optional[Dict[str, str]]:
    from app.services.action_service import name_slug

    slug = name_slug(name)
    try:
        from app.services.firestore_service import get_firestore_client
        client = get_firestore_client(target="admin")
        if client:
            for doc in client.collection("projects").stream():
                data = doc.to_dict() or {}
                label = data.get("name") or data.get("title") or doc.id
                if isinstance(label, dict):
                    label = label.get("en") or next(iter(label.values()), doc.id)
                if data.get("sourceUrl") == url or (slug and name_slug(str(label)) == slug):
                    return {"property_id": doc.id, "name": str(label), "store": "Firestore"}
    except Exception as exc:
        logger.debug("duplicate firestore scan skipped: %s", exc)
    return None


def _target_websites(staff: Any, named: Optional[List[str]]) -> List[str]:
    from app.core.tenant import normalize_website_list

    if named:
        return normalize_website_list(named)
    selected = normalize_website_list(getattr(staff, "selected_websites", None) or [])
    if selected:
        return selected
    return normalize_website_list(getattr(staff, "allowed_websites", None) or [])


def draft_from_url(db: Any, staff: Any, url: str, websites: Optional[List[str]] = None, target_property_id: Optional[str] = None) -> Dict[str, Any]:
    _charge_rate(getattr(staff, "email", None) or "admin")
    page = fetch_public_page(url)
    draft = extract_draft(page)
    draft["confirm_via_chat_only"] = True
    draft["websites"] = _target_websites(staff, websites)
    name = str(((draft.get("provenance") or {}).get("property_name") or {}).get("value") or "")
    duplicate = find_duplicate(db, page.url, name)
    if target_property_id:
        draft["property_id"] = target_property_id
        draft["mode"] = "update"
        draft["duplicate"] = {"property_id": target_property_id, "name": target_property_id, "store": "requested"}
    elif duplicate:
        draft["duplicate"] = duplicate
        draft["mode"] = "update"
        draft["property_id"] = duplicate["property_id"]
    return draft


def _role_ok(staff: Any) -> bool:
    return (getattr(staff, "role", "") or "").lower() in {"admin", "super_admin"}


def _commit(db: Any) -> bool:
    """Commit and say whether it worked. A failed commit is rolled back and logged, never ignored."""
    try:
        db.commit()
        return True
    except Exception as exc:
        logger.warning("URL draft commit failed: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        return False


def _chat_response(conv: Any, text: str, pending_id: Optional[str], language: str) -> Dict[str, Any]:
    return {
        "type": "text",
        "text": text,
        "session_id": getattr(conv, "session_id", None),
        "conversation_id": str(getattr(conv, "id", "")),
        "requires_confirmation": False,
        "confirm_via_chat_only": True,
        "pending_action_id": pending_id,
        "action_status": "pending" if pending_id else None,
        "tools_used": [{"name": INGEST_TOOL}],
        "download_link": None,
        "language": language,
    }


def _stage(db: Any, staff: Any, conversation_id: str, draft: Dict[str, Any]):
    from app.services.action_service import create_pending_action

    return create_pending_action(
        db=db,
        staff_user_id=getattr(staff, "email", None) or "admin",
        tool_name=INGEST_TOOL,
        action_payload=draft,
        conversation_id=conversation_id,
    )


def _missing_phrase(draft: Dict[str, Any], field: str) -> str:
    if field != "price":
        return field
    display = ((draft.get("provenance") or {}).get("price_display") or {})
    shown = str(display.get("value") or "").strip()
    if display.get("needs_decision") and shown:
        return f"price (page shows {shown}, not THB. Tell me the price in THB)"
    return "price (the page showed no price)"


def _edit_report(draft: Dict[str, Any], changed: List[str], rejected: List[Dict[str, Any]]) -> str:
    from app.services.admin_chat_quality import human_field_label

    lines = ["Applied", "| Field | Value |", "| --- | --- |"]
    provenance = draft.get("provenance") or {}
    seen = set()
    for field in changed:
        if field in {"currency", "foreign_listing"}:
            continue
        label = human_field_label(field if field != "price_numeric_min" else "price")
        if label in seen:
            continue
        seen.add(label)
        if field in {"price_display", "price_numeric_min", "price"}:
            value = (provenance.get("price_display") or {}).get("value") or (provenance.get("price_numeric_min") or {}).get("value")
        else:
            value = (provenance.get(field) or {}).get("value")
        lines.append(f"| {label} | {'' if value is None else value} |")
    if not seen:
        lines.append("| — | nothing applied |")
    if rejected:
        lines.append("")
        lines.append("Not understood")
        lines.append("| Text | Reason |")
        lines.append("| --- | --- |")
        for item in rejected:
            text = str(item.get("value") or item.get("field") or "").replace("|", "/")
            reason = str(item.get("reason") or "I could not tell which field this is.").replace("|", "/")
            lines.append(f"| {text} | {reason} |")
    return "\n".join(lines)


def _save_one(db: Any, staff: Any, action: Any) -> str:
    from app.services.action_service import ACTION_STATUS_EXECUTED, save_ingested_listing

    draft = dict(action.action_payload or {})
    # "confirm" is the admin's answer to a preview that already showed the outside-Thailand
    # warning and the guessed values, so it counts as "continue anyway" and "use the guesses".
    # Price is never guessed; anything still missing is asked for once, below.
    if draft.get("foreign_place") and not draft.get("foreign_acknowledged"):
        draft["foreign_acknowledged"] = True
    guessed = [
        field for field, prov in (draft.get("provenance") or {}).items()
        if isinstance(prov, dict) and prov.get("status") == "guess" and field not in _NEVER_GUESS
    ]
    if guessed:
        draft["guesses_accepted"] = sorted(set(draft.get("guesses_accepted") or []) | set(guessed))
    if _foreign_blocks(draft):
        missing = missing_requirements(draft)
        if not draft.get("websites"):
            missing.append("website")
        text = foreign_listing_warning(str(draft.get("foreign_place") or ""), str(draft.get("reply_language") or "en"))
        if missing:
            text += "\n\nI still need: " + ", ".join(_missing_phrase(draft, field) for field in missing) + "."
        return (
            text
            + "\n\nNothing was saved yet. Reply 'continue anyway' first, then 'confirm'."
            + "\n\n" + CONFIRM_FOOTER
        )
    missing = missing_requirements(draft)
    if not draft.get("websites"):
        missing.append("website")
    unresolved = [
        field for field, prov in (draft.get("provenance") or {}).items()
        if isinstance(prov, dict) and prov.get("needs_decision") and prov.get("status") != "provided"
    ]
    if missing or unresolved:
        parts = [_missing_phrase(draft, field) for field in missing]
        for field in unresolved:
            if field not in parts and field not in missing and not (field.startswith("price") and "price" in missing):
                parts.append(field)
        optional = [field for field in ("bedrooms", "bathrooms", "parking") if field not in missing]
        skip_hint = ""
        if optional:
            skip_hint = f" You can say 'skip {optional[0]}' for an optional field."
        return (
            "I still need: "
            + "; ".join(parts)
            + ". Tell me the missing value."
            + skip_hint
            + " Nothing was saved.\n\n"
            + CONFIRM_FOOTER
        )
    pending_guesses = unaccepted_guesses(draft)
    if pending_guesses:
        return (
            "These guesses are not accepted yet: " + ", ".join(pending_guesses) + ". "
            "Say 'use your guesses' or 'accept status' before confirm. Nothing was saved.\n\n"
            + CONFIRM_FOOTER
        )
    if draft.get("duplicate") and draft.get("mode") != "update":
        return "This URL matches an existing listing, so I did not create a second one.\n\n" + CONFIRM_FOOTER
    draft["fields"] = savable_fields(draft)
    draft["guessed_fields"] = [field for field in (draft.get("guesses_accepted") or []) if (draft.get("provenance") or {}).get(field, {}).get("status") == "guess"]
    try:
        result = save_ingested_listing(db, staff, draft)
    except Exception as exc:
        from fastapi import HTTPException
        if isinstance(exc, HTTPException):
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            return f"{detail} Nothing was saved."
        return f"The save failed: {exc}. Nothing was saved."
    if not result.get("saved"):
        return result.get("message") or "Nothing was saved."
    from app.repositories import pending_actions_repo

    executed_at = datetime.now(timezone.utc)
    execution_result = {
        "operation_id": result.get("operation_id"),
        "property_id": result.get("property_id"),
    }
    pending_actions_repo.update_fields(
        db,
        str(action.id),
        status=ACTION_STATUS_EXECUTED,
        executed_at=executed_at,
        execution_result=execution_result,
    )
    action.status = ACTION_STATUS_EXECUTED
    action.executed_at = executed_at
    action.execution_result = execution_result
    audit = result.get("audit")
    text = result["message"]
    if audit == "disabled" and (getattr(staff, "role", "") or "").lower() == "super_admin":
        text += " History logging is turned off, so this change has no history entry."
    return text


def try_handle_property_url_turn(
    db: Any,
    conv: Any,
    message: Optional[str],
    staff: Any,
    language: str,
    open_pending: Optional[List[Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Handle a pasted URL or an edit/confirm of an open URL draft.
    Returns None when this turn is not an ingest turn.
    """
    from app.services.conversation_service import append_conversation_turn
    from app.services.admin_image_commands import looks_like_image_command
    from app.services.admin_intent_parser import smart_intent_kind

    if looks_like_image_command(message) or smart_intent_kind(message) == "image":
        return None

    drafts = [item for item in (open_pending or []) if getattr(item, "tool_name", None) == INGEST_TOOL and getattr(item, "status", None) == "PENDING"]
    urls = extract_urls(message)
    command = parse_draft_command(message) if drafts and not urls else None
    nudge = None
    if drafts and not urls and not command:
        command, nudge = _freeform_answer(drafts[0], message)
    from app.services.admin_chat_quality import classify_draft_question

    asking = bool(drafts and not urls and not command and classify_draft_question(message))
    if not urls and not command and not asking and not nudge:
        return None
    if not _role_ok(staff):
        text = "Only an admin or super admin can import a property from a URL."
        append_conversation_turn(db, conv, message or "", text)
        return _chat_response(conv, text, None, language)

    if nudge:
        append_conversation_turn(db, conv, message or "", nudge)
        return _chat_response(conv, nudge, str(drafts[0].id), language)

    if urls:
        from app.repositories import pending_actions_repo

        superseded = None
        for old in drafts:
            payload = old.action_payload or {}
            superseded = payload.get("source_url") or superseded
            pending_actions_repo.update_fields(
                db,
                str(old.id),
                status="SUPERSEDED",
                error="Superseded by a newer URL draft",
            )
            old.status = "SUPERSEDED"
            old.error_message = "Superseded by a newer URL draft"
        texts = []
        last_id = None
        errors = []
        for url in urls:
            try:
                draft = draft_from_url(db, staff, url)
                draft["reply_language"] = (language or "en").split("-")[0].lower()
                action = _stage(db, staff, str(getattr(conv, "id", "")), draft)
                last_id = str(action.id)
                texts.append(render_preview(draft, superseded=superseded if url == urls[0] else None))
                superseded = None
            except IngestError as exc:
                errors.append(f"{url}: {exc}")
        if not texts and errors:
            text = "\n".join(errors)
            append_conversation_turn(db, conv, message or "", text)
            return _chat_response(conv, text, None, language)
        text = "\n\n".join(texts)
        if errors:
            text += "\n\n" + "\n".join(errors)
        append_conversation_turn(db, conv, message or "", text)
        return _chat_response(conv, text, last_id, language)

    if command and command.get("op") == "cancel":
        from app.repositories import pending_actions_repo

        for draft_action in drafts:
            pending_actions_repo.update_fields(
                db,
                str(draft_action.id),
                status="REJECTED",
                error="Discarded by staff.",
            )
            draft_action.status = "REJECTED"
        text = "Discarded. Nothing was saved."
        append_conversation_turn(db, conv, message or "", text)
        return _chat_response(conv, text, None, language)

    if command and command.get("op") == "confirm_all":
        pieces = []
        for draft_action in reversed(drafts):
            payload = dict(draft_action.action_payload or {})
            needed = missing_requirements(payload)
            if not payload.get("websites"):
                needed.append("website")
            if payload.get("duplicate") or needed or unaccepted_guesses(payload):
                why = ", ".join(needed) if needed else "duplicate or unaccepted guess"
                pieces.append(f"Skipped {payload.get('source_url')}: {why}.")
                continue
            pieces.append(_save_one(db, staff, draft_action))
        text = "\n\n".join(pieces) if pieces else "Nothing was saved."
        append_conversation_turn(db, conv, message or "", text)
        return _chat_response(conv, text, None, language)

    latest = drafts[0]
    if command and command.get("op") == "skip":
        field = str(command.get("field") or "")
        if skip_is_refused(field):
            text = f"I cannot skip {field}. It is required before I can save."
        else:
            from app.repositories import pending_actions_repo

            payload = copy.deepcopy(dict(latest.action_payload or {}))
            payload.setdefault("skipped_fields", [])
            if field not in payload["skipped_fields"]:
                payload["skipped_fields"].append(field)
            provenance = payload.get("provenance") or {}
            provenance.pop(field, None)
            payload["provenance"] = provenance
            pending_actions_repo.update_fields(
                db, str(latest.id), action_payload=payload
            )
            latest.action_payload = payload
            text = f"Skipped {field}.\n\n" + render_preview(payload)
        append_conversation_turn(db, conv, message or "", text)
        return _chat_response(conv, text, str(latest.id), language)

    if drafts and not command and not urls:
        from app.services.admin_chat_quality import classify_draft_question

        if classify_draft_question(message):
            payload = latest.action_payload or {}
            text = render_preview(payload if isinstance(payload, dict) else {})
            append_conversation_turn(db, conv, message or "", text)
            return _chat_response(conv, text, str(latest.id), language)
        return None

    if command and command.get("op") == "confirm":
        text = _save_one(db, staff, latest)
        append_conversation_turn(db, conv, message or "", text)
        return _chat_response(conv, text, str(latest.id), language)

    # Deep copy so nested lists (guesses_accepted, images) are not the ORM's
    # tracked objects. An in-place append looks unchanged and is not written.
    payload = copy.deepcopy(dict(latest.action_payload or {}))
    changed = apply_command(payload, command or {})
    rejected = list((command or {}).get("rejected") or []) + list(payload.pop("apply_rejected", None) or [])
    price_warning = payload.pop("price_warning", None)
    if price_warning:
        text = price_warning
        append_conversation_turn(db, conv, message or "", text)
        return _chat_response(conv, text, str(latest.id), language)
    if payload.get("keep_page_missing"):
        text = "The page has no price to keep. Tell me the price. Nothing was invented."
    elif not changed and not rejected and command and command.get("op") not in {"confirm", "cancel", "confirm_all"}:
        text = "I did not change the draft. " + CONFIRM_FOOTER
    else:
        from app.repositories import pending_actions_repo

        pending_actions_repo.update_fields(
            db, str(latest.id), action_payload=payload
        )
        latest.action_payload = payload
        visible_changed = [field for field in changed if field != "foreign_listing"]
        text = render_preview(payload, changed_only=visible_changed or None)
        if (command or {}).get("report") or rejected:
            text = _edit_report(payload, visible_changed, rejected) + "\n\n" + text
    append_conversation_turn(db, conv, message or "", text)
    return _chat_response(conv, text, str(latest.id), language)
