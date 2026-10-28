import logging
import re
from typing import Optional, List, Dict, Any
from decimal import Decimal
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, cast, String
from pydantic import BaseModel, Field, field_validator
from app.db.models import Property, PropertyImage, PropertySpec
from app.core.tenant import normalize_website_list, normalize_website_domain, normalize_tenant_domain_key

logger = logging.getLogger(__name__)

class PropertyFilterParams(BaseModel):
    location: Optional[str] = Field(None, max_length=100)
    province: Optional[str] = Field(None, max_length=100)
    district: Optional[str] = Field(None, max_length=100)
    property_type: Optional[str] = Field(None, max_length=50)  # Villa, Condominium
    websites: Optional[List[str]] = Field(None, description="Filter properties by authorized/selected websites")
    min_price: Optional[Decimal] = None
    max_price: Optional[Decimal] = None
    max_price_exclusive: bool = False
    bedrooms: Optional[int] = None
    min_bedrooms: Optional[int] = None
    max_bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    min_area: Optional[Decimal] = None
    max_area: Optional[Decimal] = None
    has_pool: Optional[bool] = None
    beach_access: Optional[bool] = None
    sea_view: Optional[bool] = None
    required_amenities: Optional[List[str]] = Field(
        None, description="Canonical amenity keys that must be present (gym, pool, parking, …)"
    )
    query: Optional[str] = Field(None, max_length=200)
    sort_by_price: Optional[str] = Field(None, description="'asc' for cheapest first, 'desc' for most expensive first")
    status: Optional[str] = Field(None, max_length=50)
    is_public: Optional[bool] = True
    limit: int = 10
    offset: int = 0

    @field_validator("min_price", "max_price", "min_area", "max_area", mode="before")
    def validate_positive_numbers(cls, v):
        if v is None:
            return None
        try:
            val = Decimal(str(v))
            if val < 0:
                return None
            return val
        except Exception:
            return None

    @field_validator("bedrooms", "min_bedrooms", "max_bedrooms", "bathrooms", mode="before")
    def sanitize_rooms(cls, v):
        if v is None:
            return None
        try:
            val = int(v)
            if val < 0:
                return None
            return val
        except Exception:
            return None

    @field_validator("limit", mode="before")
    def sanitize_limit(cls, v):
        if v is None:
            return 10
        try:
            val = int(v)
            if val > 50:
                return 50
            if val < 1:
                return 10
            return val
        except Exception:
            return 10

    @field_validator("property_type", mode="before")
    def normalize_property_type(cls, v):
        if not v or not isinstance(v, str):
            return None
        v_low = v.strip().lower()
        if "villa" in v_low:
            return "Villa"
        elif "condo" in v_low or "apartment" in v_low:
            return "Condominium"
        return v.strip()

    @field_validator("websites", mode="before")
    def sanitize_websites(cls, v):
        if v is None:
            return None
        from app.core.tenant import normalize_website_list
        return normalize_website_list(v)

def build_property_query(db: Session, filters: PropertyFilterParams):
    """
    Safely builds parameterized SQLAlchemy queries from validated structured filters.
    Zero raw or LLM-generated SQL is ever executed.
    """
    query = db.query(Property)
    conditions = []
    
    if filters.is_public is not None:
        conditions.append(Property.is_public == filters.is_public)

    # General location lookup matching area, province, district, address, or property title
    if filters.location:
        loc = f"%{filters.location.strip()}%"
        conditions.append(
            or_(
                Property.province.ilike(loc),
                Property.area.ilike(loc),
                Property.district.ilike(loc),
                Property.address.ilike(loc),
                Property.property_name.ilike(loc)
            )
        )

    if filters.province:
        conditions.append(Property.province.ilike(f"%{filters.province.strip()}%"))

    if filters.district:
        conditions.append(Property.district.ilike(f"%{filters.district.strip()}%"))

    if filters.property_type:
        conditions.append(Property.property_type.ilike(f"%{filters.property_type.strip()}%"))

    if filters.status:
        conditions.append(Property.status.ilike(f"%{filters.status.strip()}%"))

    if filters.min_price is not None:
        conditions.append(Property.price_numeric_min >= filters.min_price)

    if filters.max_price is not None:
        if filters.max_price_exclusive:
            conditions.append(Property.price_numeric_min < filters.max_price)
        else:
            conditions.append(Property.price_numeric_min <= filters.max_price)

    if filters.bedrooms is not None:
        conditions.append(
            and_(
                Property.bedrooms_min <= filters.bedrooms,
                Property.bedrooms_max >= filters.bedrooms
            )
        )
    elif filters.min_bedrooms is not None and filters.max_bedrooms is not None:
        # Overlap between listing [min, max] and requested [min_bedrooms, max_bedrooms]
        conditions.append(Property.bedrooms_min <= filters.max_bedrooms)
        conditions.append(Property.bedrooms_max >= filters.min_bedrooms)
        conditions.append(Property.bedrooms_max.isnot(None))
    elif filters.min_bedrooms is not None:
        # Largest config must be >= min ("4 or more" matches "3-5")
        conditions.append(Property.bedrooms_max >= filters.min_bedrooms)
        conditions.append(Property.bedrooms_max.isnot(None))
    elif filters.max_bedrooms is not None:
        conditions.append(Property.bedrooms_max <= filters.max_bedrooms)
        conditions.append(Property.bedrooms_max.isnot(None))

    if filters.bathrooms is not None:
        conditions.append(Property.bathrooms >= filters.bathrooms)

    if filters.min_area is not None:
        conditions.append(Property.area_sqm_min >= filters.min_area)

    if filters.max_area is not None:
        conditions.append(Property.area_sqm_max <= filters.max_area)

    if filters.has_pool is not None:
        conditions.append(Property.has_pool == filters.has_pool)

    if filters.beach_access is not None:
        conditions.append(Property.beach_access == filters.beach_access)

    if filters.sea_view is not None:
        conditions.append(Property.sea_view == filters.sea_view)

    # Scoped Multi-Tenant Website Filter
    if filters.websites:
        site_conditions = []
        for site in filters.websites:
            canon = normalize_website_domain(site)
            key = normalize_tenant_domain_key(site)
            if canon:
                site_conditions.append(cast(Property.websites, String).ilike(f'%"{canon}"%'))
                site_conditions.append(cast(Property.websites, String).ilike(f'%{canon}%'))
            if key:
                site_conditions.append(cast(Property.websites, String).ilike(f'%"{key}"%'))

        # If tajlandia.com is among the selected sites, also match unassigned legacy properties
        if any("tajlandia" in s for s in filters.websites):
            site_conditions.append(Property.websites == None)
            site_conditions.append(cast(Property.websites, String) == "[]")
            site_conditions.append(cast(Property.websites, String) == "null")
            site_conditions.append(cast(Property.websites, String) == "")

        if site_conditions:
            conditions.append(or_(*site_conditions))

    if conditions:
        query = query.filter(and_(*conditions))

    return query.order_by(Property.price_numeric_min.asc().nulls_last())


def property_belongs_to_websites(prop: Optional[Any], target_websites: List[str]) -> bool:
    """
    Verifies if a Property record or Firestore property dict belongs to ANY of the given target_websites.
    Used for verifying RBAC authorization during GET, PUT, and DELETE operations.
    """
    if not prop or not target_websites:
        return False
    normalized_targets = [normalize_website_domain(w) for w in target_websites if w]
    target_keys = [normalize_tenant_domain_key(w) for w in target_websites if w]

    prop_sites = []
    if isinstance(prop, dict):
        raw = prop.get("websites") or prop.get("domains") or prop.get("allowed_websites")
        if not raw and prop.get("siteOrigin"):
            raw = [prop["siteOrigin"]]
    else:
        raw = getattr(prop, "websites", None)

    if raw:
        if isinstance(raw, list):
            prop_sites = [normalize_website_domain(s) for s in raw if s]
        elif isinstance(raw, str):
            prop_sites = [normalize_website_domain(s) for s in raw.split(",") if s.strip()]

    # If property has explicit website assignments, verify overlap
    if prop_sites:
        return any(
            s in normalized_targets or normalize_tenant_domain_key(s) in target_keys
            for s in prop_sites
        )

    # Fallback for unassigned legacy properties: default to tajlandia.com
    if any("tajlandia" in t for t in normalized_targets):
        return True

    return False


class _MockImage:
    def __init__(self, url: str):
        self.image_url = url

class FirestorePropertyAdapter:
    """
    Wraps a normalized Firestore project dictionary so it behaves like a SQLAlchemy Property model,
    enabling all downstream public and admin tools to seamlessly operate directly on live Firestore data.
    """
    def __init__(self, data: Dict[str, Any]):
        self._data = dict(data)
        self.property_id = str(data.get("property_id") or data.get("id") or "").strip()
        self.property_name = str(data.get("name") or data.get("property_name") or self.property_id)
        self.project_name = data.get("project_name") or self.property_name
        self.property_type = data.get("property_type") or "Villa / Condo"
        
        # Clean price display - zero .xx placeholders
        raw_price = data.get("price") or data.get("price_display") or "Price on request"
        p_num_min = data.get("price_numeric_min")
        if ".xx" in str(raw_price) and p_num_min:
            raw_price = f"from {float(p_num_min) / 1_000_000:.2f} million THB"
        elif ".xx" in str(raw_price):
            raw_price = "Price on request"
        self.price_display = raw_price
        self.price_numeric_min = p_num_min
        self.price_numeric_max = data.get("price_numeric_max")
        self.currency = data.get("currency") or "THB"
        self.price_note = data.get("price_note")

        self.bedrooms_display = str(data.get("bedrooms") or "")
        self.bedrooms_min = None
        self.bedrooms_max = None
        if self.bedrooms_display:
            b_nums = [int(n) for n in re.findall(r"\d+", self.bedrooms_display)]
            if b_nums:
                self.bedrooms_min = min(b_nums)
                self.bedrooms_max = max(b_nums)

        self.bathrooms = data.get("bathrooms")
        self.area_display = str(data.get("area") or "")
        self.area_sqm_min = None
        self.area_sqm_max = None
        if self.area_display:
            a_nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", self.area_display)]
            if a_nums:
                self.area_sqm_min = min(a_nums)
                self.area_sqm_max = max(a_nums)

        self.district = data.get("district") or ""
        self.province = data.get("province") or "Thailand"
        self.area = data.get("location") or self.district or "Thailand"
        self.address = data.get("address") or ""
        self.latitude = data.get("latitude")
        self.longitude = data.get("longitude")
        self.status = data.get("status") or "Available"
        self.has_pool = bool(data.get("has_pool"))
        self.beach_access = bool(data.get("beach_access"))
        self.sea_view = bool(data.get("sea_view"))
        self.description = data.get("description") or ""
        # Ownership only when present in property data — never invent a generic freehold/leasehold line
        self.ownership_type = data.get("ownership_type") or data.get("ownership") or None
        self.highlights = list(data.get("highlights") or [])
        self.amenities = list(data.get("amenities") or [])
        self.investment_information = data.get("investment_information") or data.get("expected_roi") or ""
        self.rental_information = data.get("rental_information") or ""
        self.payment_information = data.get("payment_information") or ""
        self.developer_name = data.get("developer_name") or data.get("developer") or ""
        self.detail_page_url = data.get("detail_page_url") or ""
        self.raw_metadata = data.get("raw_firestore_data") or {}

        # Images list with image_url attribute
        raw_images = data.get("images") or []
        self.images = [_MockImage(u) if isinstance(u, str) else u for u in raw_images]

    def __getitem__(self, item):
        return self._data.get(item)

    def get(self, item, default=None):
        return self._data.get(item, default)

    def __contains__(self, item):
        return item in self._data


def _properties_store_mode() -> str:
    from app.services.property_index import public_store_mode
    return public_store_mode()


def search_properties_safe(db: Session, filters: PropertyFilterParams) -> List[Any]:
    """
    Public property search. Default: in-memory Firestore property index.
    STORE_PROPERTIES_READ_USER=sql uses SQL; both = index first, SQL if empty.
    Firestore mode never falls back to SQL.
    """
    mode = _properties_store_mode()
    if mode in ("firestore", "both"):
        try:
            from app.services.property_index import get_property_index, IndexUnavailable
            cards = get_property_index().search(filters)
            if cards:
                return [FirestorePropertyAdapter(c) for c in cards]
            if mode == "firestore":
                return []  # warm or empty — never SQL
            # both: fall through to SQL when index returned nothing
        except Exception as e:
            from app.services.property_index import IndexUnavailable
            logger.error("property index search failed (STORE=%s): %s", mode, e)
            if mode == "firestore":
                if isinstance(e, IndexUnavailable):
                    raise
                raise IndexUnavailable(str(e)) from e
    if db is not None and mode in ("sql", "both"):
        query = build_property_query(db, filters)
        return query.offset(filters.offset).limit(filters.limit).all()
    return []

def get_property_by_id(db: Session, property_id: str) -> Optional[Any]:
    """Retrieve a public property by id via the property index (or SQL when configured)."""
    if not property_id or not isinstance(property_id, str):
        return None
    clean_id = property_id.strip()
    mode = _properties_store_mode()

    if mode in ("firestore", "both"):
        try:
            from app.services.property_index import get_property_index, IndexUnavailable
            card = get_property_index().get_by_id(clean_id)
            if card:
                return FirestorePropertyAdapter(card)
            # Direct Firestore document when index has no row for this id
            from app.services.firestore_service import get_property_by_id_from_firestore
            from app.services.property_index import sanitize_public_card
            fs_p = get_property_by_id_from_firestore(clean_id)
            if fs_p:
                return FirestorePropertyAdapter(sanitize_public_card(fs_p))
        except Exception as e:
            from app.services.property_index import IndexUnavailable
            logger.error("property index get_by_id failed (STORE=%s): %s", mode, e)
            if mode == "firestore":
                if isinstance(e, IndexUnavailable):
                    raise
                return None

    if mode in ("sql", "both") and db is not None:
        return db.query(Property).filter(
            Property.property_id == clean_id
        ).first()
    return None

def get_property_by_name(db: Session, name_query: str) -> Optional[Any]:
    """Fuzzy lookup property by name via the property index (or SQL when configured)."""
    if not name_query or not isinstance(name_query, str):
        return None
    clean_name = name_query.strip()
    mode = _properties_store_mode()

    if mode in ("firestore", "both"):
        try:
            from app.services.property_index import get_property_index, IndexUnavailable, sanitize_public_card
            from app.services.property_name_index import match_property_name
            idx = get_property_index()
            mr = match_property_name(clean_name)
            if mr.confident:
                card = idx.get_by_id(mr.confident.doc_id)
                if card:
                    return FirestorePropertyAdapter(card)
            card = idx.get_by_id(clean_name)
            if card:
                return FirestorePropertyAdapter(card)
            for rec in idx.all_records():
                if str(rec.get("name") or "").strip().lower() == clean_name.lower():
                    return FirestorePropertyAdapter(rec)
            from app.services.firestore_service import get_property_by_id_from_firestore
            fs_p = get_property_by_id_from_firestore(clean_name)
            if fs_p:
                return FirestorePropertyAdapter(sanitize_public_card(fs_p))
        except Exception as e:
            from app.services.property_index import IndexUnavailable
            logger.error("property index get_by_name failed (STORE=%s): %s", mode, e)
            if mode == "firestore":
                if isinstance(e, IndexUnavailable):
                    raise
                return None

    if mode in ("sql", "both") and db is not None:
        return db.query(Property).filter(
            Property.property_name.ilike(f"%{clean_name}%")
        ).first()
    return None


def to_compact_projection(p: Any, intent: str = "search") -> Dict[str, Any]:
    """
    Produces a minimal, token-efficient projection tailored to the specific query intent.
    Saves 80%+ tokens compared to serializing the entire Property ORM object.
    Never exposes '.xx' placeholder in prices.
    """
    clean_area = re.sub(r"^0\s*[-–]\s*", "", getattr(p, "area_display", "") or "") if getattr(p, "area_display", None) else "N/A"
    
    price_val = getattr(p, "price_display", None) or getattr(p, "price", None) or "Price on request"
    if ".xx" in str(price_val):
        p_num = getattr(p, "price_numeric_min", None)
        if p_num:
            price_val = f"from {float(p_num) / 1_000_000:.2f} million THB"
        else:
            price_val = "Price on request"

    p_id = getattr(p, "property_id", None) or getattr(p, "id", "")
    p_name = getattr(p, "property_name", None) or getattr(p, "name", "")

    compact: Dict[str, Any] = {
        "id": p_id,
        "name": p_name,
        "price": price_val,
        "location": f"{getattr(p, 'district', '') or ''}, {getattr(p, 'province', 'Thailand') or 'Thailand'}".strip(", "),
        "bedrooms": getattr(p, "bedrooms_display", None) or (str(getattr(p, "bedrooms_min", "")) if getattr(p, "bedrooms_min", None) else "N/A"),
        "area": clean_area,
        "pool": bool(getattr(p, "has_pool", False))
    }

    if intent in ["comparison", "property_comparison"]:
        compact["bathrooms"] = getattr(p, "bathrooms", "N/A") or "N/A"
        compact["status"] = getattr(p, "status", "Available") or "Available"
        compact["beach"] = bool(getattr(p, "beach_access", False))
        compact["sea_view"] = bool(getattr(p, "sea_view", False))
        _own = getattr(p, "ownership_type", None) or getattr(p, "ownership", None)
        if _own and str(_own).strip().lower() not in (
            "freehold / leasehold",
            "foreign freehold / leasehold available",
            "foreign freehold / leasehold 90 lat",
        ):
            compact["ownership"] = str(_own).strip()
        if getattr(p, "highlights", None):
            compact["highlights"] = p.highlights[:2]
    elif intent in ["details", "property_details"]:
        compact["bathrooms"] = getattr(p, "bathrooms", "N/A") or "N/A"
        compact["status"] = getattr(p, "status", "Available") or "Available"
        _own = getattr(p, "ownership_type", None) or getattr(p, "ownership", None)
        if _own and str(_own).strip().lower() not in (
            "freehold / leasehold",
            "foreign freehold / leasehold available",
            "foreign freehold / leasehold 90 lat",
        ):
            compact["ownership"] = str(_own).strip()
        compact["amenities"] = (getattr(p, "amenities", None) or [])[:4]
        if getattr(p, "investment_information", None):
            compact["investment"] = str(p.investment_information)[:150]

    return compact


def get_properties_by_ids(db: Session, property_ids: List[str]) -> List[Any]:
    """Retrieves multiple properties by IDs or names, preserving the requested order."""
    if not property_ids:
        return []
    clean_ids = [pid.strip() for pid in property_ids if isinstance(pid, str) and pid.strip()]
    results = []
    for pid in clean_ids:
        prop = get_property_by_id(db, pid)
        if not prop:
            prop = get_property_by_name(db, pid)
        if prop:
            results.append(prop)
    return results

