import re
from typing import Optional, List, Dict, Any
from decimal import Decimal
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, cast, String
from pydantic import BaseModel, Field, field_validator
from app.db.models import Property, PropertyImage, PropertySpec
from app.core.tenant import normalize_website_list, normalize_website_domain, normalize_tenant_domain_key

class PropertyFilterParams(BaseModel):
    location: Optional[str] = Field(None, max_length=100)
    province: Optional[str] = Field(None, max_length=100)
    district: Optional[str] = Field(None, max_length=100)
    property_type: Optional[str] = Field(None, max_length=50)  # Villa, Condominium
    websites: Optional[List[str]] = Field(None, description="Filter properties by authorized/selected websites")
    min_price: Optional[Decimal] = None
    max_price: Optional[Decimal] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    min_area: Optional[Decimal] = None
    max_area: Optional[Decimal] = None
    has_pool: Optional[bool] = None
    beach_access: Optional[bool] = None
    sea_view: Optional[bool] = None
    query: Optional[str] = Field(None, max_length=200)
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

    @field_validator("bedrooms", "bathrooms", mode="before")
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
        conditions.append(Property.price_numeric_min <= filters.max_price)

    if filters.bedrooms is not None:
        conditions.append(
            and_(
                Property.bedrooms_min <= filters.bedrooms,
                Property.bedrooms_max >= filters.bedrooms
            )
        )

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


def property_belongs_to_websites(prop: Optional[Property], target_websites: List[str]) -> bool:
    """
    Verifies if a Property record belongs to ANY of the given target_websites.
    Used for verifying RBAC authorization during GET, PUT, and DELETE operations.
    """
    if not prop or not target_websites:
        return False
    normalized_targets = [normalize_website_domain(w) for w in target_websites if w]
    target_keys = [normalize_tenant_domain_key(w) for w in target_websites if w]

    prop_sites = []
    if getattr(prop, "websites", None):
        raw = prop.websites
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


def search_properties_safe(db: Session, filters: PropertyFilterParams) -> List[Property]:
    """Execute the safe query and fetch matched records with images."""
    if db is None:
        return []
    query = build_property_query(db, filters)
    return query.offset(filters.offset).limit(filters.limit).all()

def get_property_by_id(db: Session, property_id: str) -> Optional[Property]:
    """Retrieve full public record for a specific Property ID."""
    if db is None or not property_id or not isinstance(property_id, str):
        return None
    return db.query(Property).filter(
        Property.property_id == property_id.strip()
    ).first()

def get_property_by_name(db: Session, name_query: str) -> Optional[Property]:
    """Fuzzy lookup property by name."""
    if db is None or not name_query or not isinstance(name_query, str):
        return None
    return db.query(Property).filter(
        Property.property_name.ilike(f"%{name_query.strip()}%")
    ).first()


def to_compact_projection(p: Property, intent: str = "search") -> Dict[str, Any]:
    """
    Produces a minimal, token-efficient projection tailored to the specific query intent.
    Saves 80%+ tokens compared to serializing the entire Property ORM object.
    """
    clean_area = re.sub(r"^0\s*[-–]\s*", "", p.area_display or "") if p.area_display else "N/A"

    compact: Dict[str, Any] = {
        "id": p.property_id,
        "name": p.property_name,
        "price": p.price_display or "Price on request",
        "location": f"{p.district or ''}, {p.province or 'Thailand'}".strip(", "),
        "bedrooms": p.bedrooms_display or (str(p.bedrooms_min) if p.bedrooms_min else "N/A"),
        "area": clean_area,
        "pool": bool(p.has_pool)
    }

    if intent in ["comparison", "property_comparison"]:
        compact["bathrooms"] = p.bathrooms or "N/A"
        compact["status"] = p.status or "Available"
        compact["beach"] = bool(p.beach_access)
        compact["sea_view"] = bool(p.sea_view)
        compact["ownership"] = p.ownership_type or "Freehold / Leasehold"
        if p.highlights:
            compact["highlights"] = p.highlights[:2]
    elif intent in ["details", "property_details"]:
        compact["bathrooms"] = p.bathrooms or "N/A"
        compact["status"] = p.status or "Available"
        compact["ownership"] = p.ownership_type or "Freehold / Leasehold"
        compact["amenities"] = (p.amenities or [])[:4]
        if p.investment_information:
            compact["investment"] = p.investment_information[:150]

    return compact


def get_properties_by_ids(db: Session, property_ids: List[str]) -> List[Property]:
    """Retrieves multiple properties by IDs, preserving the requested order."""
    if not property_ids:
        return []
    clean_ids = [pid.strip().upper() for pid in property_ids if isinstance(pid, str) and pid.strip()]
    props = db.query(Property).filter(Property.property_id.in_(clean_ids)).all()
    prop_map = {p.property_id.upper(): p for p in props}
    return [prop_map[pid] for pid in clean_ids if pid in prop_map]

