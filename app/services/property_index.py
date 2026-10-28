"""
Public property index: loads Firestore `projects` into an in-memory cache per site.

- Cache TTL 60s; re-check meta/properties_version every 15s; reload on version bump.
- Thread-safe. On Firestore failure: serve last good cache; if none, raise IndexUnavailable.
- Public visibility: isActive true/missing, status not archived/deleted, websites contain site.
- Never exposes raw_firestore_data / admin notes in returned cards.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from copy import deepcopy
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Set

logger = logging.getLogger(__name__)

CACHE_TTL_SEC = 60.0
VERSION_CHECK_SEC = 15.0
_ARCHIVED_STATUSES = frozenset({"archived", "deleted"})


class IndexUnavailable(RuntimeError):
    """No usable property cache (Firestore down and never loaded)."""


def _strip_accents(text: str) -> str:
    raw = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in raw if not unicodedata.combining(ch))


def _norm_text(text: Any) -> str:
    return _strip_accents(str(text or "").lower()).strip()


def public_store_mode() -> str:
    from app.core.config import settings
    mode = str(getattr(settings, "STORE_PROPERTIES_READ_USER", "firestore") or "firestore").strip().lower()
    if mode not in {"firestore", "postgres", "both"}:
        return "firestore"
    return mode


def sanitize_public_card(card: Dict[str, Any]) -> Dict[str, Any]:
    """Drop internal / admin-only fields before replies or cards."""
    out = dict(card or {})
    out.pop("raw_firestore_data", None)
    out.pop("admin_notes", None)
    out.pop("adminNotes", None)
    out.pop("_admin_notes", None)
    return out


def _parse_bedrooms(card: Dict[str, Any]) -> tuple:
    from app.services.firestore_service import bedroom_nums_from_listing
    nums = bedroom_nums_from_listing(card.get("bedrooms"))
    if not nums:
        bmin = card.get("bedrooms_min")
        bmax = card.get("bedrooms_max")
        try:
            if bmin is not None:
                nums.append(int(bmin))
            if bmax is not None:
                nums.append(int(bmax))
        except (TypeError, ValueError):
            pass
    if not nums:
        return None, None
    return min(nums), max(nums)


def _parse_area_sqm(card: Dict[str, Any]) -> tuple:
    area = str(card.get("area") or "")
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", area)]
    if not nums:
        try:
            amin = float(card["area_sqm_min"]) if card.get("area_sqm_min") is not None else None
            amax = float(card["area_sqm_max"]) if card.get("area_sqm_max") is not None else None
            return amin, amax
        except (TypeError, ValueError, KeyError):
            return None, None
    return min(nums), max(nums)


def _status_blocked(raw: Dict[str, Any], card: Dict[str, Any]) -> bool:
    for key in ("completionStatus", "status", "projectStatus"):
        val = raw.get(key) if key in raw else card.get("status")
        low = str(val or "").strip().lower()
        if low in _ARCHIVED_STATUSES:
            return True
    return False


def _is_active(raw: Dict[str, Any]) -> bool:
    if "isActive" not in raw and "is_active" not in raw:
        return True
    flag = raw.get("isActive", raw.get("is_active", True))
    return flag is not False


def _enrich_index_record(card: Dict[str, Any], raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    rec = sanitize_public_card(card)
    bmin, bmax = _parse_bedrooms(rec)
    amin, amax = _parse_area_sqm(rec)
    rec["bedrooms_min"] = bmin
    rec["bedrooms_max"] = bmax
    rec["area_sqm_min"] = amin
    rec["area_sqm_max"] = amax
    raw = raw or {}
    ownership = (
        rec.get("ownership")
        or rec.get("ownership_type")
        or raw.get("ownership")
        or raw.get("ownershipType")
        or raw.get("tenure")
    )
    if ownership:
        rec["ownership"] = ownership
        rec["ownership_type"] = ownership
    developer = rec.get("developer") or rec.get("developer_name") or raw.get("developer") or raw.get("developerName")
    if developer:
        rec["developer"] = developer
        rec["developer_name"] = developer
    completion = (
        rec.get("completion")
        or rec.get("completion_date")
        or raw.get("completionDate")
        or raw.get("completion")
    )
    if completion:
        rec["completion"] = completion
    images = list(rec.get("images") or [])
    cover = raw.get("coverImage") or (images[0] if images else None)
    if cover:
        rec["cover_image"] = cover
        if cover not in images:
            images = [cover] + images
            rec["images"] = images
    # Search blob (accent-folded)
    blob_parts = [
        rec.get("name"),
        rec.get("province"),
        rec.get("district"),
        rec.get("area"),
        rec.get("address"),
        rec.get("location"),
        rec.get("property_type"),
    ]
    rec["_search_blob"] = " ".join(_norm_text(p) for p in blob_parts if p)
    return rec


def _location_matches(rec: Dict[str, Any], location: str) -> bool:
    if not location:
        return True
    from app.services.firestore_service import canonical_region_name
    loc = _norm_text(canonical_region_name(location))
    if not loc:
        return True
    loc_h = loc.replace(" ", "-")
    blob = rec.get("_search_blob") or ""
    if loc in blob or loc_h in blob:
        return True
    for field in ("province", "district", "area", "address", "location", "name"):
        val = _norm_text(rec.get(field))
        if loc in val or loc_h in val:
            return True
    return False


def _type_matches(rec: Dict[str, Any], property_type: Optional[str]) -> bool:
    if not property_type:
        return True
    pt = _norm_text(property_type)
    p_type = _norm_text(rec.get("property_type"))
    p_name = _norm_text(rec.get("name"))
    is_condo = any(k in pt for k in ("condo", "apart", "mieszkanie", "studio"))
    is_villa = (not is_condo) and (
        any(k in pt for k in ("villa", "willa", "house")) or bool(re.search(r"\bdom(?:y|u|em)?\b", pt))
    )
    if is_villa:
        return "villa" in p_type or "villa" in p_name or "willa" in p_type
    if is_condo:
        return "condo" in p_type or "apart" in p_type or "condo" in p_name
    return pt in p_type or pt in p_name


def _price_of(rec: Dict[str, Any]) -> Optional[float]:
    for key in ("price_thb_min", "price_numeric_min"):
        val = rec.get(key)
        if val is None:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def _site_keys(site: Optional[str]) -> List[str]:
    if not site:
        return []
    from app.core.tenant import normalize_website_domain, normalize_tenant_domain_key
    return [k for k in (normalize_website_domain(site), normalize_tenant_domain_key(site)) if k]


class PropertyIndex:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: List[Dict[str, Any]] = []
        self._by_id: Dict[str, Dict[str, Any]] = {}
        self._loaded_at: float = 0.0
        self._version_checked_at: float = 0.0
        self._properties_version: Optional[int] = None
        self._has_cache: bool = False
        self._client_override: Any = None
        self._load_error: Optional[str] = None

    def set_client_override(self, client: Any) -> None:
        """Tests: inject a dict-based fake Firestore client."""
        with self._lock:
            self._client_override = client

    def clear(self) -> None:
        with self._lock:
            self._records = []
            self._by_id = {}
            self._loaded_at = 0.0
            self._version_checked_at = 0.0
            self._properties_version = None
            self._has_cache = False
            self._load_error = None

    def has_cache(self) -> bool:
        with self._lock:
            return self._has_cache

    def _client(self) -> Any:
        if self._client_override is not None:
            return self._client_override
        from app.services.firestore_service import get_firestore_client
        return get_firestore_client("user")

    def _read_version(self, client: Any) -> Optional[int]:
        try:
            snap = client.collection("meta").document("properties_version").get()
            if not getattr(snap, "exists", False):
                return None
            data = snap.to_dict() or {}
            ver = data.get("version")
            return int(ver) if ver is not None else None
        except Exception as e:
            logger.debug("properties_version read failed: %s", e)
            return None

    def _load_from_client(self, client: Any) -> List[Dict[str, Any]]:
        from app.services.firestore_service import normalize_firestore_project

        rows: List[Dict[str, Any]] = []
        stream = client.collection("projects").stream()
        for snap in stream:
            doc_id = getattr(snap, "id", None) or ""
            raw = snap.to_dict() if hasattr(snap, "to_dict") else None
            if not isinstance(raw, dict):
                continue
            if not _is_active(raw):
                continue
            try:
                card = normalize_firestore_project(str(doc_id), raw)
            except Exception as e:
                logger.warning("normalize project %s failed: %s", doc_id, e)
                continue
            if _status_blocked(raw, card):
                continue
            rows.append(_enrich_index_record(card, raw))
        return rows

    def _apply_rows(self, rows: List[Dict[str, Any]], version: Optional[int]) -> None:
        by_id = {str(r.get("property_id") or "").strip(): r for r in rows if r.get("property_id")}
        self._records = list(by_id.values())
        self._by_id = by_id
        self._loaded_at = time.monotonic()
        self._version_checked_at = self._loaded_at
        self._properties_version = version
        self._has_cache = True
        self._load_error = None
        # Refresh name index from loaded public rows
        try:
            from app.services.property_name_index import get_property_name_index
            name_rows = []
            for r in self._records:
                name_rows.append({
                    "doc_id": r.get("property_id"),
                    "id": r.get("property_id"),
                    "name": {"en": r.get("name")},
                    "cityId": r.get("district") or "",
                })
            idx = get_property_name_index()
            idx.load_from_rows(name_rows)
            idx._loaded_at = time.time() + 10**9  # keep warm during tests / short TTL
        except Exception as e:
            logger.warning("property name index refresh from property_index failed: %s", e)

    def ensure_fresh(self, *, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            client = self._client()
            need_version = force or (now - self._version_checked_at) >= VERSION_CHECK_SEC
            need_ttl = force or (not self._has_cache) or ((now - self._loaded_at) >= CACHE_TTL_SEC)
            version: Optional[int] = self._properties_version
            version_changed = False
            if need_version:
                if client:
                    try:
                        version = self._read_version(client)
                        self._version_checked_at = now
                        if (
                            self._properties_version is not None
                            and version is not None
                            and int(version) != int(self._properties_version)
                        ):
                            version_changed = True
                        elif self._properties_version is None and version is not None and self._has_cache:
                            # first successful version read after load — keep unless force/ttl
                            self._properties_version = version
                    except Exception as e:
                        logger.warning("property index version check failed: %s", e)
                else:
                    self._version_checked_at = now

            if not (force or need_ttl or version_changed or not self._has_cache):
                return

            if not client:
                if self._has_cache:
                    logger.warning("Firestore unavailable; serving last good property index cache")
                    return
                self._load_error = "firestore_unavailable"
                raise IndexUnavailable("Firestore client unavailable and property index has no cache")

            try:
                rows = self._load_from_client(client)
                if version is None:
                    version = self._read_version(client)
                self._apply_rows(rows, version)
                logger.info(
                    "Property index loaded: %s public projects (version=%s)",
                    len(self._records),
                    version,
                )
            except IndexUnavailable:
                raise
            except Exception as e:
                logger.warning("Property index reload failed: %s", e)
                if self._has_cache:
                    logger.warning("Serving last good property index cache after reload failure")
                    return
                self._load_error = str(e)
                raise IndexUnavailable(f"Property index empty and reload failed: {e}") from e

    def _visible_for_site(self, rec: Dict[str, Any], site: Optional[str]) -> bool:
        if not site:
            return True
        from app.services.property_search import property_belongs_to_websites
        return property_belongs_to_websites(rec, [site])

    def all_records(self, *, site: Optional[str] = None) -> List[Dict[str, Any]]:
        self.ensure_fresh()
        with self._lock:
            rows = [sanitize_public_card(r) for r in self._records if self._visible_for_site(r, site)]
        return rows

    def get_by_id(self, property_id: str, *, site: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not property_id:
            return None
        self.ensure_fresh()
        pid = str(property_id).strip()
        with self._lock:
            rec = self._by_id.get(pid)
            if rec is None:
                # case-insensitive id
                low = pid.lower()
                for k, v in self._by_id.items():
                    if k.lower() == low:
                        rec = v
                        break
            if rec is None:
                return None
            if not self._visible_for_site(rec, site):
                return None
            return sanitize_public_card(rec)

    def get_by_ids(
        self, property_ids: Sequence[str], *, site: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for pid in property_ids or []:
            card = self.get_by_id(str(pid), site=site)
            if card:
                out.append(card)
        return out

    def search(
        self,
        filters: Any,
        *,
        site: Optional[str] = None,
        exclude_property_ids: Optional[Sequence[str]] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        In-memory search matching public filter semantics (location contains,
        type, status, price, bedroom overlap, bathrooms, area, pool, beach,
        amenities, sort, pagination, exclude seen).
        """
        self.ensure_fresh()
        from app.services.firestore_service import listing_matches_bedroom_constraints
        from app.services.amenity_service import property_has_amenity

        lim = int(limit if limit is not None else getattr(filters, "limit", 10) or 10)
        offset = int(getattr(filters, "offset", 0) or 0)
        exclude: Set[str] = {
            str(x).strip().lower() for x in (exclude_property_ids or []) if x
        }
        # Named query → name index (Did you mean path handled by caller / firestore search parity)
        q = getattr(filters, "query", None)
        if q:
            from app.services.property_name_index import match_property_name
            mr = match_property_name(str(q))
            if mr.confident:
                card = self.get_by_id(mr.confident.doc_id, site=site)
                return [card] if card else []
            from app.services.property_name_index import set_last_name_search_meta
            cands = list(mr.candidates or [])[:3]
            set_last_name_search_meta(
                str(q),
                suggestions=[c.display_name for c in cands],
                candidates=cands,
            )
            return []

        websites = list(getattr(filters, "websites", None) or [])
        if site and not websites:
            websites = [site]

        location = getattr(filters, "location", None)
        province = getattr(filters, "province", None)
        district = getattr(filters, "district", None)
        property_type = getattr(filters, "property_type", None)
        status = getattr(filters, "status", None)
        min_price = getattr(filters, "min_price", None)
        max_price = getattr(filters, "max_price", None)
        max_excl = bool(getattr(filters, "max_price_exclusive", False))
        bedrooms = getattr(filters, "bedrooms", None)
        min_beds = getattr(filters, "min_bedrooms", None)
        max_beds = getattr(filters, "max_bedrooms", None)
        bathrooms = getattr(filters, "bathrooms", None)
        min_area = getattr(filters, "min_area", None)
        max_area = getattr(filters, "max_area", None)
        has_pool = getattr(filters, "has_pool", None)
        beach = getattr(filters, "beach_access", None)
        sea_view = getattr(filters, "sea_view", None)
        req_am = list(getattr(filters, "required_amenities", None) or [])
        sort_by = (getattr(filters, "sort_by_price", None) or "").strip().lower()

        with self._lock:
            pool = list(self._records)

        matched: List[Dict[str, Any]] = []
        for rec in pool:
            pid = str(rec.get("property_id") or "").strip().lower()
            pname = str(rec.get("name") or "").strip().lower()
            if exclude and (pid in exclude or pname in exclude):
                continue
            if websites:
                from app.services.property_search import property_belongs_to_websites
                if not property_belongs_to_websites(rec, websites):
                    continue
            elif site and not self._visible_for_site(rec, site):
                continue

            if location and not _location_matches(rec, str(location)):
                continue
            if province and _norm_text(province) not in _norm_text(rec.get("province")):
                continue
            if district and _norm_text(district) not in _norm_text(rec.get("district")):
                continue
            if not _type_matches(rec, property_type):
                continue
            if status and _norm_text(status) not in _norm_text(rec.get("status")):
                continue

            price = _price_of(rec)
            if min_price is not None:
                try:
                    if price is None or price < float(min_price):
                        continue
                except (TypeError, ValueError):
                    continue
            if max_price is not None:
                try:
                    mx = float(max_price)
                    if price is None:
                        continue  # exclude price-on-request when budget set
                    if max_excl:
                        if not (price < mx):
                            continue
                    elif not (price <= mx):
                        continue
                except (TypeError, ValueError):
                    continue

            if not listing_matches_bedroom_constraints(
                rec.get("bedrooms"),
                bedrooms=int(bedrooms) if bedrooms is not None else None,
                min_bedrooms=int(min_beds) if min_beds is not None else None,
                max_bedrooms=int(max_beds) if max_beds is not None else None,
            ):
                continue

            if bathrooms is not None:
                try:
                    baths = rec.get("bathrooms")
                    if baths is None or float(baths) < float(bathrooms):
                        continue
                except (TypeError, ValueError):
                    continue

            if min_area is not None:
                amin = rec.get("area_sqm_min")
                try:
                    if amin is None or float(amin) < float(min_area):
                        continue
                except (TypeError, ValueError):
                    continue
            if max_area is not None:
                amax = rec.get("area_sqm_max")
                try:
                    if amax is None or float(amax) > float(max_area):
                        continue
                except (TypeError, ValueError):
                    continue

            if has_pool is not None and bool(rec.get("has_pool")) != bool(has_pool):
                continue
            if beach is not None and bool(rec.get("beach_access")) != bool(beach):
                continue
            if sea_view is not None and bool(rec.get("sea_view")) != bool(sea_view):
                continue
            if req_am and not all(property_has_amenity(rec, a) for a in req_am):
                continue

            matched.append(rec)

        if sort_by in {"asc", "cheapest", "price_asc"}:
            matched.sort(key=lambda r: (_price_of(r) is None, _price_of(r) or 0.0))
        elif sort_by in {"desc", "expensive", "most_expensive", "price_desc"}:
            matched.sort(key=lambda r: (_price_of(r) is None, -(_price_of(r) or 0.0)))
        elif sort_by in {"newest", "new"}:
            matched.sort(
                key=lambda r: str(r.get("completion") or r.get("updated_at") or ""),
                reverse=True,
            )
        else:
            # Default: cheapest first (SQL parity)
            matched.sort(key=lambda r: (_price_of(r) is None, _price_of(r) or 0.0))

        sliced = matched[offset: offset + lim]
        return [sanitize_public_card(r) for r in sliced]


_INDEX = PropertyIndex()


def get_property_index() -> PropertyIndex:
    return _INDEX


def reset_property_index_for_tests() -> None:
    _INDEX.clear()
    _INDEX.set_client_override(None)
