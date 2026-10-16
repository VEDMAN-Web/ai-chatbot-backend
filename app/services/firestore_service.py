import os
import json
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
import base64
import re

from app.core.config import settings

logger = logging.getLogger(__name__)

# Singleton Firestore clients
_admin_firestore_client = None
_user_firestore_client = None


class FirestoreDatabaseError(Exception):
    """Raised when communication with Firestore database fails."""
    pass


def _create_single_firestore_client(project_id: str, database: str, env_key: Optional[str], cred_path: Optional[str], label: str):
    try:
        from google.cloud import firestore
        from google.oauth2 import service_account

        # 1. Check raw JSON or base64 string in env_key
        if env_key:
            env_key_str = env_key.strip()
            if not env_key_str.startswith("{"):
                try:
                    env_key_str = base64.b64decode(env_key_str).decode("utf-8")
                except Exception:
                    pass
            key_data = json.loads(env_key_str)
            target_project = key_data.get("project_id") or project_id
            creds = service_account.Credentials.from_service_account_info(key_data)
            cl = firestore.Client(project=target_project, database=database, credentials=creds)
            logger.info(f"[{label.upper()}] Firestore client initialized via service account key (project: {target_project}, db: {database}).")
            return cl

        # 2. Check file path on disk
        if cred_path:
            p = cred_path
            if not os.path.isabs(p):
                base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                p = os.path.join(base_dir, p)
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    key_data = json.load(f)
                target_project = key_data.get("project_id") or project_id
                creds = service_account.Credentials.from_service_account_info(key_data)
                cl = firestore.Client(project=target_project, database=database, credentials=creds)
                logger.info(f"[{label.upper()}] Firestore client initialized from key file: {p} (project: {target_project}, db: {database}).")
                return cl

        # 3. Fallback to Application Default Credentials (ADC)
        cl = firestore.Client(project=project_id, database=database)
        logger.info(f"[{label.upper()}] Firestore client initialized via Application Default Credentials (project: {project_id}, db: {database}).")
        return cl
    except Exception as e:
        logger.error(f"[{label.upper()}] Failed to initialize Firestore client: {e}")
        return None


def get_admin_firestore_client():
    """Returns the dedicated Admin Firestore client (for admin_authentication, staff audits & admin ops)."""
    global _admin_firestore_client
    if _admin_firestore_client is not None:
        return _admin_firestore_client
    _admin_firestore_client = _create_single_firestore_client(
        project_id=settings.get_admin_firestore_project_id(),
        database=settings.get_admin_firestore_database(),
        env_key=settings.get_admin_firebase_service_account_key(),
        cred_path=settings.get_admin_firebase_credentials_path(),
        label="admin"
    )
    return _admin_firestore_client


def get_user_firestore_client():
    """Returns the dedicated User/Public Firestore client (for public chatbot conversations, leads & property catalog)."""
    global _user_firestore_client
    if _user_firestore_client is not None:
        return _user_firestore_client
    _user_firestore_client = _create_single_firestore_client(
        project_id=settings.get_user_firestore_project_id(),
        database=settings.get_user_firestore_database(),
        env_key=settings.get_user_firebase_service_account_key(),
        cred_path=settings.get_user_firebase_credentials_path(),
        label="user"
    )
    return _user_firestore_client


def get_firestore_client(target: str = "user", *args, **kwargs):
    """
    Universal Firestore client getter.
    Defaults to get_user_firestore_client() for customer chat & catalog queries,
    or get_admin_firestore_client() for admin operations.
    """
    if target == "admin":
        cl = get_admin_firestore_client()
        if cl:
            return cl
        return get_user_firestore_client()
    else:
        cl = get_user_firestore_client()
        if cl:
            return cl
        return get_admin_firestore_client()


def check_firestore_health() -> Dict[str, Any]:
    """Tests live connectivity to both user and admin Firestore databases."""
    res = {"connected": False}
    
    admin_cl = get_admin_firestore_client()
    user_cl = get_user_firestore_client()
    
    admin_info = {"connected": False}
    if admin_cl:
        try:
            admin_cols = list(admin_cl.collections())
            admin_info = {
                "connected": True,
                "project_id": getattr(admin_cl, "project", ""),
                "database": getattr(admin_cl, "_database", "(default)"),
                "collections_count": len(admin_cols)
            }
        except Exception as ae:
            admin_info = {"connected": False, "error": str(ae)}

    user_info = {"connected": False}
    if user_cl:
        try:
            user_cols = list(user_cl.collections())
            user_info = {
                "connected": True,
                "project_id": getattr(user_cl, "project", ""),
                "database": getattr(user_cl, "_database", "(default)"),
                "collections_count": len(user_cols)
            }
        except Exception as ue:
            user_info = {"connected": False, "error": str(ue)}

    res["connected"] = admin_info["connected"] or user_info["connected"]
    res["admin_firestore"] = admin_info
    res["user_firestore"] = user_info
    
    primary = user_info if user_info["connected"] else admin_info
    res["project_id"] = primary.get("project_id", "")
    res["database"] = primary.get("database", "(default)")
    res["collections_count"] = primary.get("collections_count", 0)
    return res


# ==============================================================================
# 1. Properties & Projects Synchronization
# ==============================================================================

def normalize_firestore_project(doc_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalizes a Firestore `projects` document into the standard Tajlandia property card format.
    Handles multilingual name fields, coordinates, pricing, and images.
    """
    # Name/Title handling (can be dict like {'pl': '...', 'en': '...'} or string)
    raw_name = data.get("name") or data.get("title") or data.get("projectName") or ""
    if isinstance(raw_name, dict):
        name_str = raw_name.get("pl") or raw_name.get("en") or next((str(v) for v in raw_name.values() if v), doc_id)
    else:
        name_str = str(raw_name) if raw_name else doc_id

    # Pricing
    price_min = data.get("priceFrom")
    price_max = data.get("priceTo")
    currency = data.get("currency") or "THB"
    try:
        price_num_min = float(price_min) if price_min is not None else None
    except (ValueError, TypeError):
        price_num_min = None

    try:
        price_num_max = float(price_max) if price_max is not None else None
    except (ValueError, TypeError):
        price_num_max = None

    if price_num_min:
        if price_num_min >= 1_000_000:
            price_display = f"from {price_num_min / 1_000_000:.2f}M {currency}"
        else:
            price_display = f"from {price_num_min:,.0f} {currency}"
    else:
        price_display = "Price on request"

    # Coordinates
    coords = data.get("coordinates") or {}
    lat = None
    lng = None
    if isinstance(coords, dict):
        lat = coords.get("latitude") or coords.get("lat")
        lng = coords.get("longitude") or coords.get("lng")
    elif hasattr(coords, "latitude") and hasattr(coords, "longitude"):
        lat = coords.latitude
        lng = coords.longitude

    try:
        lat = float(lat) if lat is not None else None
        lng = float(lng) if lng is not None else None
    except (ValueError, TypeError):
        lat, lng = None, None

    # Address & Location strings
    address_raw = data.get("address")
    if isinstance(address_raw, dict):
        address = ", ".join([str(v) for v in address_raw.values() if v and isinstance(v, (str, int, float))])
    elif address_raw:
        address = str(address_raw)
    else:
        address = ""

    city_raw = data.get("cityId") or data.get("city") or ""
    if isinstance(city_raw, dict):
        city_id = next((str(v) for v in city_raw.values() if v), "")
    else:
        city_id = str(city_raw) if city_raw else ""

    country_raw = data.get("countryId") or data.get("country") or "Thailand"
    if isinstance(country_raw, dict):
        country_id = next((str(v) for v in country_raw.values() if v), "Thailand")
    else:
        country_id = str(country_raw) if country_raw else "Thailand"

    loc_parts = [str(p).strip() for p in [city_id, address, country_id] if p and str(p).strip()]
    location_str = ", ".join(loc_parts) if loc_parts else "Thailand"

    # Images
    images = list(data.get("images") or [])
    cover_image = data.get("coverImage")
    if cover_image and cover_image not in images:
        images.insert(0, cover_image)

    # Bedrooms & Area
    layouts = data.get("layouts") or []
    bedrooms_str = "1-4 Beds"
    if layouts and isinstance(layouts, list):
        bed_nums = [l.get("bedrooms") for l in layouts if isinstance(l, dict) and l.get("bedrooms") is not None]
        if bed_nums:
            min_b, max_b = min(bed_nums), max(bed_nums)
            bedrooms_str = f"{min_b}-{max_b} Beds" if min_b != max_b else f"{min_b} Beds"

    area_from = data.get("areaFrom")
    area_to = data.get("areaTo")
    if area_from and area_to and area_from != area_to:
        area_str = f"{area_from}-{area_to} sqm"
    elif area_from:
        area_str = f"{area_from} sqm"
    else:
        area_str = "N/A"

    amenities = list(data.get("amenities") or [])
    tags = list(data.get("tags") or [])
    all_features = [str(a).lower() for a in (amenities + tags)]
    combined_desc = (
        str(name_str) + " " +
        str(data.get("propertyType") or "") + " " +
        str(data.get("description") or "") + " " +
        str(data.get("shortDescription") or "") + " " +
        " ".join(all_features)
    ).lower()

    pool_terms = ["pool", "basen", "basseyn", "swimming", "infinity", "jacuzzi", "water-park", "waterpark"]
    has_pool = any(term in combined_desc for term in pool_terms)

    beach_terms = ["beach", "plaz", "morz", "beachfront", "sea-view", "sea view", "ocean-view", "ocean view"]
    beach_access = any(term in combined_desc for term in beach_terms)

    # Sanitize raw_firestore_data to ensure JSON serializability (Firestore DatetimeWithNanoseconds -> ISO string)
    def _make_json_safe(obj: Any) -> Any:
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        elif hasattr(obj, "to_dict"):
            return _make_json_safe(obj.to_dict())
        elif isinstance(obj, dict):
            return {k: _make_json_safe(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple, set)):
            return [_make_json_safe(item) for item in obj]
        elif isinstance(obj, (int, float, str, bool)) or obj is None:
            return obj
        return str(obj)

    safe_raw_data = _make_json_safe(data)

    # Detail Page URL: use multilingual slug (en or pl) if available, fallback to doc_id
    slug_val = data.get("slug")
    slug_str = ""
    if isinstance(slug_val, dict):
        slug_str = slug_val.get("en") or slug_val.get("pl") or next((str(v) for v in slug_val.values() if v), "")
    elif slug_val:
        slug_str = str(slug_val).strip()
    slug_target = slug_str if slug_str else doc_id

    # Description normalization
    desc_val = data.get("description")
    desc_str = ""
    if isinstance(desc_val, dict):
        desc_str = desc_val.get("en") or desc_val.get("pl") or next((str(v) for v in desc_val.values() if v), "")
    elif desc_val:
        desc_str = str(desc_val).strip()

    if not desc_str:
        s_desc = data.get("shortDescription")
        if isinstance(s_desc, dict):
            desc_str = s_desc.get("en") or s_desc.get("pl") or next((str(v) for v in s_desc.values() if v), "")
        elif s_desc:
            desc_str = str(s_desc).strip()

    if not desc_str:
        inv = data.get("investmentHighlights")
        if isinstance(inv, dict):
            pts = inv.get("en") or inv.get("pl") or []
            if pts and isinstance(pts, list):
                desc_str = " ".join([str(p).strip() for p in pts if p])

    if not desc_str:
        desc_str = data.get("_descriptionRu") or data.get("_shortDescriptionRu") or ""

    if not desc_str:
        desc_str = f"Exclusive development '{name_str}' in {location_str}. Featuring {bedrooms_str}, {area_str}, with pricing starting from {price_display}."

    # Websites & Domains Scope
    raw_websites = data.get("websites") or data.get("domains") or data.get("allowed_websites")
    if raw_websites:
        from app.core.tenant import normalize_website_list
        websites_list = normalize_website_list(raw_websites)
    else:
        site_orig = data.get("siteOrigin") or "varsoviaestate.com"
        websites_list = [site_orig]

    return {
        "property_id": doc_id,
        "name": name_str,
        "property_type": data.get("propertyType") or "Villa / Condo",
        "price": price_display,
        "price_numeric_min": price_num_min,
        "price_numeric_max": price_num_max,
        "currency": currency,
        "bedrooms": bedrooms_str,
        "area": area_str,
        "location": location_str,
        "address": address,
        "district": city_id,
        "province": country_id,
        "latitude": lat,
        "longitude": lng,
        "status": data.get("completionStatus") or ("Available" if data.get("isActive", True) else "Sold Out"),
        "has_pool": has_pool,
        "beach_access": beach_access,
        "images": images,
        "detail_page_url": settings.get_property_detail_url(slug_target, country=country_id, city=city_id),
        "highlights": (data.get("highlights") or amenities)[:3],
        "expected_roi": data.get("expectedROI") or data.get("rentalYield"),
        "description": desc_str,
        "websites": websites_list,
        "is_public": data.get("isActive", True),
        "raw_firestore_data": safe_raw_data
    }


def fetch_properties_from_firestore(
    limit: int = 150,
    city_id: Optional[str] = None,
    active_only: bool = True,
    target: str = "user"
) -> List[Dict[str, Any]]:
    """
    Fetches real property projects from Firestore `projects` collection.
    Supports cityId filtering and active_only flag.
    Routes to user or admin Firestore client based on target.
    """
    client = get_firestore_client(target=target)
    if not client:
        logger.warning("Firestore client not available.")
        raise FirestoreDatabaseError("Firestore client is not initialized or unavailable.")

    try:
        col = client.collection("projects")
        query = col
        if active_only:
            try:
                from google.cloud.firestore_v1.base_query import FieldFilter
                query = query.where(filter=FieldFilter("isActive", "==", True))
            except Exception:
                query = query.where("isActive", "==", True)

        if city_id:
            try:
                from google.cloud.firestore_v1.base_query import FieldFilter
                query = query.where(filter=FieldFilter("cityId", "==", str(city_id).lower()))
            except Exception:
                query = query.where("cityId", "==", str(city_id).lower())

        query = query.limit(limit)
        docs = list(query.stream())

        results = []
        for doc in docs:
            data = doc.to_dict() or {}
            normalized = normalize_firestore_project(doc.id, data)
            results.append(normalized)

        logger.info(f"Successfully fetched {len(results)} properties from Firestore 'projects'.")
        return results

    except Exception as e:
        logger.error(f"Error fetching properties from Firestore: {e}")
        raise FirestoreDatabaseError(f"Error communicating with Firestore: {e}")


def get_firestore_properties_count(target: str = "user") -> int:
    """Returns the total number of property projects in Firestore 'projects' collection."""
    client = get_firestore_client(target=target)
    if not client:
        return 0
    try:
        count_query = client.collection("projects").count()
        results = count_query.get()
        return int(results[0][0].value)
    except Exception as e:
        logger.debug(f"Firestore aggregation count query fallback: {e}")
        try:
            docs = list(client.collection("projects").select([]).stream())
            return len(docs)
        except Exception as e2:
            logger.warning(f"Error counting Firestore properties: {e2}")
            return 0


def get_property_by_id_from_firestore(property_id: str, target: str = "user") -> Optional[Dict[str, Any]]:
    """
    Retrieves a single property project from Firestore by document ID, slugified name, or title.
    Routes to user or admin Firestore client based on target.
    """
    client = get_firestore_client(target=target)
    if not client:
        return None

    raw_query = (property_id or "").strip()
    if not raw_query:
        return None

    # Never treat general city or country name as a single property!
    city_country_keywords = [
        "phuket", "samui", "koh samui", "pattaya", "bangkok", "phangan", "koh phangan",
        "hua hin", "hua-hin", "krabi", "chiang mai", "thailand", "tajlandia"
    ]
    if raw_query.lower() in city_country_keywords:
        return None

    try:
        col = client.collection("projects")

        # 1. Exact document ID lookup
        doc = col.document(raw_query).get()
        if doc.exists:
            return normalize_firestore_project(doc.id, doc.to_dict() or {})

        # 2. Slugified ID lookup (e.g., 'Sea Heaven Phuket Naithon Phase 3.1' -> 'sea-heaven-phuket-naithon-phase-3-1')
        slug_cand = re.sub(r'[^a-zA-Z0-9]+', '-', raw_query.lower()).strip('-')
        if slug_cand and slug_cand not in city_country_keywords:
            doc = col.document(slug_cand).get()
            if doc.exists:
                return normalize_firestore_project(doc.id, doc.to_dict() or {})

        # 3. Query by slug fields
        if slug_cand and slug_cand not in city_country_keywords:
            for field in ["slug.en", "slug.pl", "slug.ru", "slug"]:
                try:
                    from google.cloud.firestore_v1.base_query import FieldFilter
                    docs = list(col.where(filter=FieldFilter(field, "==", slug_cand)).limit(1).stream())
                    if docs:
                        return normalize_firestore_project(docs[0].id, docs[0].to_dict() or {})
                except Exception:
                    try:
                        docs = list(col.where(field, "==", slug_cand).limit(1).stream())
                        if docs:
                            return normalize_firestore_project(docs[0].id, docs[0].to_dict() or {})
                    except Exception:
                        pass

        # 4. Query by exact name or title fields
        for field in ["name.pl", "name.en", "seoTitle.pl", "seoTitle.en", "title.en", "title.pl", "name", "title"]:
            try:
                from google.cloud.firestore_v1.base_query import FieldFilter
                docs = list(col.where(filter=FieldFilter(field, "==", raw_query)).limit(1).stream())
                if docs:
                    return normalize_firestore_project(docs[0].id, docs[0].to_dict() or {})
            except Exception:
                try:
                    docs = list(col.where(field, "==", raw_query).limit(1).stream())
                    if docs:
                        return normalize_firestore_project(docs[0].id, docs[0].to_dict() or {})
                except Exception:
                    pass

        # 5. Fuzzy match in relevant city if city recognized
        city_id = None
        for c in ["phuket", "samui", "pattaya", "bangkok", "phangan", "hua-hin"]:
            if c in slug_cand:
                city_id = c
                break

        def _name_blob(data: Dict[str, Any], doc_id: str) -> str:
            parts = [str(doc_id)]
            for key in ("name", "title", "seoTitle", "projectName", "slug"):
                val = data.get(key)
                if isinstance(val, dict):
                    parts.extend(str(v) for v in val.values() if v)
                elif val:
                    parts.append(str(val))
            return " ".join(parts).lower()

        query_l = raw_query.lower().strip()
        slug_tokens = [t for t in slug_cand.split("-") if len(t) >= 3]

        if city_id and slug_cand not in city_country_keywords and len(slug_cand) > len(city_id):
            try:
                from google.cloud.firestore_v1.base_query import FieldFilter
                city_docs = list(col.where(filter=FieldFilter("cityId", "==", city_id)).limit(150).stream())
                for d in city_docs:
                    data = d.to_dict() or {}
                    blob = _name_blob(data, d.id)
                    if slug_cand in blob.replace(" ", "-") or query_l in blob:
                        return normalize_firestore_project(d.id, data)
                    if slug_tokens and all(t in blob for t in slug_tokens):
                        return normalize_firestore_project(d.id, data)
            except Exception:
                pass

        # 6. Global fuzzy name match (e.g. "Mono Champaca" without city in the query)
        # Scans a bounded window so "tell me more about <shown card name>" works.
        if slug_cand not in city_country_keywords and len(query_l) >= 4:
            try:
                scanned = 0
                best = None
                for d in col.limit(400).stream():
                    scanned += 1
                    data = d.to_dict() or {}
                    blob = _name_blob(data, d.id)
                    blob_slug = re.sub(r"[^a-z0-9]+", "-", blob).strip("-")
                    if query_l == blob or slug_cand == str(d.id).lower():
                        return normalize_firestore_project(d.id, data)
                    if query_l in blob or slug_cand in blob_slug:
                        return normalize_firestore_project(d.id, data)
                    if slug_tokens and all(t in blob for t in slug_tokens):
                        best = best or (d.id, data)
                if best:
                    return normalize_firestore_project(best[0], best[1])
            except Exception as fuzzy_err:
                logger.debug(f"Firestore global fuzzy name lookup failed: {fuzzy_err}")

        return None
    except Exception as e:
        logger.error(f"Error fetching property '{property_id}' from Firestore: {e}")
        return None


def search_properties_firestore(
    location: Optional[str] = None,
    property_type: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    bedrooms: Optional[int] = None,
    has_pool: Optional[bool] = None,
    beach_access: Optional[bool] = None,
    query: Optional[str] = None,
    websites: Optional[List[str]] = None,
    target: str = "user",
    limit: int = 6,
    exclude_property_ids: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """
    Searches properties in Firestore projects collection using structured criteria.
    Includes Google Maps URLs, embeds, and landmark calculations.
    Routes to user or admin Firestore client based on target.
    """
    from app.services.maps_service import (
        build_google_maps_url,
        build_google_maps_embed_url,
        get_property_proximity_landmarks
    )

    matched = []
    loc_clean = (location or "").lower().strip()
    pt_clean = (property_type or "").lower().strip()
    q_clean = (query or "").strip()

    # 1. Direct search by query / project name (e.g. "15 Sukhumvit Residences", "Sea Heaven Phuket Naithon Phase 3.1")
    if q_clean:
        direct_prop = get_property_by_id_from_firestore(q_clean, target=target)
        if direct_prop:
            maps_url = build_google_maps_url(
                latitude=direct_prop.get("latitude"),
                longitude=direct_prop.get("longitude"),
                property_name=direct_prop.get("name"),
                location_str=direct_prop.get("location") or direct_prop.get("address")
            )
            embed_info = build_google_maps_embed_url(
                latitude=direct_prop.get("latitude"),
                longitude=direct_prop.get("longitude"),
                location_str=direct_prop.get("location") or direct_prop.get("address"),
                property_name=direct_prop.get("name")
            )
            direct_prop["maps_url"] = maps_url
            direct_prop["maps_embed_url"] = embed_info.get("embed_url")
            direct_prop["nearby_landmarks"] = get_property_proximity_landmarks(
                latitude=direct_prop.get("latitude"),
                longitude=direct_prop.get("longitude"),
                district=direct_prop.get("district"),
                province=direct_prop.get("province")
            )
            return [direct_prop]

    # Known regions must never trigger direct single-property resolution
    known_regions = [
        "phuket", "samui", "koh samui", "pattaya", "bangkok", "phangan", "koh phangan",
        "hua hin", "hua-hin", "krabi", "chiang mai", "thailand", "tajlandia"
    ]

    # If location looks like a specific project name rather than a region, attempt direct resolution
    if loc_clean and loc_clean not in known_regions and len(loc_clean) >= 3:
        direct_prop = get_property_by_id_from_firestore(location, target=target)
        if direct_prop:
            maps_url = build_google_maps_url(
                latitude=direct_prop.get("latitude"),
                longitude=direct_prop.get("longitude"),
                property_name=direct_prop.get("name"),
                location_str=direct_prop.get("location") or direct_prop.get("address")
            )
            embed_info = build_google_maps_embed_url(
                latitude=direct_prop.get("latitude"),
                longitude=direct_prop.get("longitude"),
                location_str=direct_prop.get("location") or direct_prop.get("address"),
                property_name=direct_prop.get("name")
            )
            direct_prop["maps_url"] = maps_url
            direct_prop["maps_embed_url"] = embed_info.get("embed_url")
            direct_prop["nearby_landmarks"] = get_property_proximity_landmarks(
                latitude=direct_prop.get("latitude"),
                longitude=direct_prop.get("longitude"),
                district=direct_prop.get("district"),
                province=direct_prop.get("province")
            )
            matched.append(direct_prop)

    # Determine city hint for targeted Firestore query
    city_hint = None
    for c in ["phuket", "samui", "pattaya", "bangkok", "phangan", "hua-hin", "sihanoukville", "phnom-penh"]:
        if c in loc_clean:
            city_hint = c
            break

    all_props = fetch_properties_from_firestore(limit=250, city_id=city_hint, active_only=True, target=target)
    if not all_props and not matched:
        return []

    exclude_set = {str(eid).strip().lower() for eid in (exclude_property_ids or []) if eid}

    candidates = []
    for p in all_props:
        # Exclude properties already recommended in this chat session
        if exclude_set:
            p_id = str(p.get("property_id") or p.get("id") or "").strip().lower()
            p_name_l = str(p.get("name") or "").strip().lower()
            if p_id in exclude_set or p_name_l in exclude_set:
                continue

        # Website / Tenant Domain filter
        if websites:
            p_sites = p.get("websites") or p.get("domains") or ([p.get("siteOrigin")] if p.get("siteOrigin") else [])
            if p_sites:
                from app.core.tenant import normalize_website_domain, normalize_tenant_domain_key
                norm_w = [normalize_website_domain(w) for w in websites if w]
                key_w = [normalize_tenant_domain_key(w) for w in websites if w]
                if not any(normalize_website_domain(s) in norm_w or normalize_tenant_domain_key(s) in key_w for s in p_sites):
                    continue

        # Location filter
        if loc_clean:
            p_loc = (p.get("location") or "").lower()
            p_name = (p.get("name") or "").lower()
            p_addr = (p.get("address") or "").lower()
            p_dist = (p.get("district") or "").lower()
            if not any(loc_clean in field for field in [p_loc, p_name, p_addr, p_dist]):
                continue

        # Property type filter
        if pt_clean:
            p_type = (p.get("property_type") or "").lower()
            p_name = (p.get("name") or "").lower()
            is_condo_query = any(k in pt_clean for k in ["condo", "apart", "mieszkanie", "studio"])
            is_villa_query = (not is_condo_query) and (any(k in pt_clean for k in ["villa", "willa", "house"]) or bool(re.search(r"\bdom(?:y|u|em)?\b", pt_clean)))
            
            if is_villa_query and "villa" not in p_type and "villa" not in p_name:
                continue
            elif is_condo_query and "condo" not in p_type and "apart" not in p_type:
                continue

        # Bedroom filter
        if bedrooms is not None and bedrooms > 0:
            b_str = str(p.get("bedrooms") or "")
            import re
            b_nums = [int(n) for n in re.findall(r"\d+", b_str)]
            if b_nums:
                min_b, max_b = min(b_nums), max(b_nums)
                if not (min_b <= bedrooms <= max_b):
                    continue

        # Price filters
        p_min = p.get("price_numeric_min")
        if min_price is not None and p_min is not None and p_min < min_price:
            continue
        if max_price is not None and p_min is not None and p_min > max_price:
            continue

        # Amenities filter
        if has_pool is True and not p.get("has_pool"):
            continue
        if beach_access is True and not p.get("beach_access"):
            continue

        candidates.append(p)

    # Preserve any direct match (e.g. from specific project query) at position 0
    direct_ids = {m.get("property_id") for m in matched if m.get("property_id")}
    remaining = [c for c in candidates if c.get("property_id") not in direct_ids]

    # Dynamic rotation: Shuffle remaining candidate matches so broad discovery queries
    # showcase a fresh, varied selection of verified properties instead of always the same 4
    import random
    random.shuffle(remaining)

    for p in remaining:
        # Add Google Maps URL & Embed
        maps_url = build_google_maps_url(
            latitude=p.get("latitude"),
            longitude=p.get("longitude"),
            property_name=p.get("name"),
            location_str=p.get("location") or p.get("address")
        )
        embed_info = build_google_maps_embed_url(
            latitude=p.get("latitude"),
            longitude=p.get("longitude"),
            location_str=p.get("location") or p.get("address"),
            property_name=p.get("name")
        )
        p["maps_url"] = maps_url
        p["maps_embed_url"] = embed_info.get("embed_url")
        p["nearby_landmarks"] = get_property_proximity_landmarks(
            latitude=p.get("latitude"),
            longitude=p.get("longitude"),
            district=p.get("district"),
            province=p.get("province")
        )

        matched.append(p)
        if len(matched) >= limit:
            break

    return matched


def save_or_update_property_firestore(
    property_id: str,
    fields: Dict[str, Any],
    images: Optional[List[str]] = None,
    cover_image: Optional[str] = None,
    target: str = "admin"
) -> bool:
    """
    Creates or updates a property project in Cloud Firestore `projects` collection.
    Supports updating core property information, pricing, bedrooms, and property images.
    Routes to admin or user Firestore client based on target (defaults to admin).
    """
    client = get_firestore_client(target=target)
    if not client:
        logger.warning(f"Firestore client ({target}) not available for property update.")
        return False

    clean_id = str(property_id).strip()
    if not clean_id:
        return False

    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        doc_ref = client.collection("projects").document(clean_id)
        doc = doc_ref.get()
        existing_data = doc.to_dict() or {} if doc.exists else {}

        update_payload: Dict[str, Any] = {"updatedAt": now_iso}
        if not doc.exists:
            update_payload["createdAt"] = now_iso
            update_payload["isActive"] = True

        # Property Name & Title
        prop_name = fields.get("property_name") or fields.get("title") or fields.get("name")
        if prop_name:
            update_payload["name"] = prop_name
            update_payload["title"] = prop_name
            if isinstance(existing_data.get("name"), dict):
                merged_name = dict(existing_data["name"])
                merged_name["en"] = prop_name
                merged_name["pl"] = prop_name
                update_payload["name"] = merged_name

        # Location / City / Address
        if fields.get("district") or fields.get("province") or fields.get("address"):
            update_payload["address"] = fields.get("address") or fields.get("district") or ""
        if fields.get("province"):
            update_payload["province"] = fields.get("province")
            update_payload["cityId"] = str(fields["province"]).lower().replace(" ", "-")

        # Property Type
        if fields.get("property_type"):
            update_payload["propertyType"] = fields.get("property_type")

        # Pricing
        if fields.get("price_display"):
            update_payload["priceDisplay"] = fields.get("price_display")
        if fields.get("price_numeric_min") is not None:
            update_payload["priceFrom"] = float(fields["price_numeric_min"])
        if fields.get("price_numeric_max") is not None:
            update_payload["priceTo"] = float(fields["price_numeric_max"])

        # Status
        if fields.get("status"):
            update_payload["status"] = fields.get("status")

        # Description
        if fields.get("description"):
            update_payload["description"] = fields.get("description")

        # Pool & Amenities
        if fields.get("has_pool") is not None:
            amenities = list(existing_data.get("amenities") or [])
            if fields["has_pool"] and "pool" not in [str(a).lower() for a in amenities]:
                amenities.append("Private Pool")
            update_payload["amenities"] = amenities

        # Multi-Tenant Websites & Domains Scope
        raw_websites = fields.get("websites") or fields.get("domains") or fields.get("allowed_websites")
        if raw_websites:
            from app.core.tenant import normalize_website_list
            norm_ws = normalize_website_list(raw_websites)
            if norm_ws:
                update_payload["websites"] = norm_ws
                update_payload["domains"] = norm_ws
                update_payload["siteOrigin"] = norm_ws[0]

        # Images
        final_images = list(existing_data.get("images") or [])
        if images:
            for img in images:
                if img and str(img).strip() and str(img).strip() not in final_images:
                    final_images.append(str(img).strip())
            update_payload["images"] = final_images

        if cover_image:
            update_payload["coverImage"] = str(cover_image).strip()
            if str(cover_image).strip() not in final_images:
                final_images.insert(0, str(cover_image).strip())
                update_payload["images"] = final_images
        elif final_images and not existing_data.get("coverImage"):
            update_payload["coverImage"] = final_images[0]

        doc_ref.set(update_payload, merge=True)
        logger.info(f"Successfully saved/updated property '{clean_id}' in Firestore 'projects'.")
        return True

    except Exception as e:
        logger.error(f"Error saving/updating property '{clean_id}' in Firestore: {e}")
        return False


def delete_property_firestore(
    property_id: str,
    hard_delete: bool = False,
    staff_email: Optional[str] = None,
    target: str = "admin"
) -> bool:
    """
    Deactivates or deletes a property from Cloud Firestore `projects` collection.
    By default performs a soft delete (isActive=False, status='Archived') to preserve referential integrity,
    or permanently removes the document if hard_delete is True.
    Routes to admin or user Firestore client based on target (defaults to admin).
    """
    client = get_firestore_client(target=target)
    if not client:
        logger.warning(f"Firestore client ({target}) not available for property deletion.")
        return False

    clean_id = str(property_id).strip()
    if not clean_id:
        return False

    try:
        doc_ref = client.collection("projects").document(clean_id)
        if hard_delete:
            doc_ref.delete()
            logger.info(f"Permanently deleted property '{clean_id}' from Firestore 'projects'.")
        else:
            now_iso = datetime.now(timezone.utc).isoformat()
            doc_ref.set({
                "isActive": False,
                "status": "Archived",
                "updatedAt": now_iso,
                "_deletedAt": now_iso,
                "_deletedBy": staff_email or "admin"
            }, merge=True)
            logger.info(f"Soft-deleted/Archived property '{clean_id}' in Firestore 'projects'.")
        return True
    except Exception as e:
        logger.error(f"Error deleting property '{clean_id}' in Firestore: {e}")
        return False




# ==============================================================================
# 2. Lead Management
# ==============================================================================

def create_lead_in_firestore(lead_data: Dict[str, Any]) -> Optional[str]:
    """
    Creates a new customer lead in Firestore's `leads` collection.
    Returns the created document ID.
    """
    client = get_firestore_client()
    if not client:
        logger.warning("Firestore client not available. Cannot save lead.")
        return None

    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "name": lead_data.get("name") or "Website Visitor",
            "email": lead_data.get("email"),
            "phone": lead_data.get("phone") or "",
            "status": lead_data.get("status") or "new",
            "brand": lead_data.get("brand") or "tajlandia",
            "sourceType": lead_data.get("source_type") or "chatbot",
            "sourceUrl": lead_data.get("source_url") or "https://web-production-55e59.up.railway.app",
            "sourcePage": lead_data.get("source_page") or "/chat",
            "propertyId": lead_data.get("property_id") or "",
            "propertyName": lead_data.get("property_name") or "",
            "message": lead_data.get("message") or "",
            "createdAt": now_iso,
            "updatedAt": now_iso,
            "metadata": lead_data.get("metadata") or {}
        }

        doc_ref = client.collection("leads").document()
        doc_ref.set(payload)
        logger.info(f"Created new lead in Firestore 'leads' collection with ID: {doc_ref.id}")
        return doc_ref.id

    except Exception as e:
        logger.error(f"Failed to create lead in Firestore: {e}")
        return None


# ==============================================================================
# 3. Query & Interaction Logging
# ==============================================================================

def log_query_to_firestore(
    session_id: str,
    user_question: str,
    ai_response: str,
    matched_properties: Optional[List[str]] = None,
    error: Optional[str] = None,
    latency_ms: Optional[int] = None
) -> Optional[str]:
    """
    Logs every chatbot conversation turn to Firestore `chat_query_logs`.
    Tracks user question, AI response, timestamps, matched properties, and errors.
    """
    client = get_firestore_client()
    if not client:
        return None

    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "sessionId": session_id,
            "userQuestion": user_question,
            "aiResponse": ai_response,
            "matchedProperties": matched_properties or [],
            "hasError": bool(error),
            "errorMessage": str(error) if error else None,
            "latencyMs": latency_ms,
            "timestamp": now_iso,
            "source": "public_chatbot",
            "clientPlatform": "web"
        }

        doc_ref = client.collection("chat_query_logs").document()
        doc_ref.set(payload)
        return doc_ref.id

    except Exception as e:
        logger.error(f"Failed to log query to Firestore: {e}")
        return None


# ==============================================================================
# 4. Database Change Auditing
# ==============================================================================

def create_audit_log_in_firestore(
    admin_email: str,
    action: str,
    details: Any,
    target_type: str,
    target_id: Optional[str] = None,
    admin_uid: Optional[str] = None
) -> Optional[str]:
    """
    Records an administrative action to Firestore's `admin_audit_log` collection.
    Matches existing Varsovia / Tajlandia Firestore audit schema.
    """
    client = get_firestore_client()
    if not client:
        return None

    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "adminEmail": admin_email,
            "adminUid": admin_uid or "admin_system",
            "action": action,
            "details": details,
            "targetType": target_type,
            "targetId": target_id or "",
            "createdAt": now_iso
        }

        doc_ref = client.collection("admin_audit_log").document()
        doc_ref.set(payload)
        logger.info(f"Audited action '{action}' on target '{target_id}' in Firestore.")
        return doc_ref.id

    except Exception as e:
        logger.error(f"Failed to write audit log to Firestore: {e}")
        return None


def get_audit_logs_from_firestore(
    admin_email: Optional[str] = None,
    action: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    limit: int = 50
) -> List[Dict[str, Any]]:
    """
    Fetches administrative audit logs from Firestore's `admin_audit_log` collection.
    Supports filtering by adminEmail, action, targetType, and targetId.
    """
    client = get_firestore_client()
    if not client:
        return []

    try:
        from google.cloud.firestore_v1.base_query import FieldFilter
        query = client.collection("admin_audit_log")

        if admin_email:
            clean_email = admin_email.strip().lower()
            try:
                query = query.where(filter=FieldFilter("adminEmail", "==", clean_email))
            except Exception:
                query = query.where("adminEmail", "==", clean_email)

        if action:
            try:
                query = query.where(filter=FieldFilter("action", "==", action.strip()))
            except Exception:
                query = query.where("action", "==", action.strip())

        if target_type:
            try:
                query = query.where(filter=FieldFilter("targetType", "==", target_type.strip()))
            except Exception:
                query = query.where("targetType", "==", target_type.strip())

        if target_id:
            try:
                query = query.where(filter=FieldFilter("targetId", "==", target_id.strip()))
            except Exception:
                query = query.where("targetId", "==", target_id.strip())

        docs = list(query.limit(limit).stream())
        results = []
        for d in docs:
            data = d.to_dict() or {}
            data["id"] = d.id
            results.append(data)

        results.sort(key=lambda x: str(x.get("createdAt") or ""), reverse=True)
        return results

    except Exception as e:
        logger.error(f"Failed to fetch audit logs from Firestore: {e}")
        return []


# ==============================================================================
# 5. Conversation History & Session Management (Pure Firestore)
# ==============================================================================

class FirestoreConversation:
    """Wrapper that mimics SQLAlchemy Conversation object so existing code works seamlessly."""
    def __init__(self, doc_id: str, data: Dict[str, Any]):
        self.id = doc_id
        self.session_id = data.get("sessionId") or doc_id
        self.site_origin = data.get("siteOrigin") or getattr(settings, "SITE_ORIGIN", "varsoviaestate.com")
        self.agent_type = data.get("agentType") or "public"
        self.message_history = data.get("messageHistory") or []
        self.preferences = data.get("preferences") or {}
        self.lead_score = float(data.get("leadScore") or 0.0)
        self.created_at = data.get("createdAt")
        self.updated_at = data.get("updatedAt")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "siteOrigin": self.site_origin,
            "agentType": self.agent_type,
            "messageHistory": self.message_history,
            "preferences": self.preferences,
            "leadScore": self.lead_score,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at
        }


def _conversation_doc_id(session_id: str, agent_type: str = "public") -> str:
    """Stable Firestore document id — scoped by agent so public/admin never collide."""
    clean_sid = (session_id or "anonymous").strip()
    at = (agent_type or "public").strip() or "public"
    return f"{at}__{clean_sid}"


def get_or_create_conversation_firestore(
    session_id: str,
    site_origin: Optional[str] = None,
    agent_type: str = "public"
) -> FirestoreConversation:
    """
    Retrieves or creates a conversation in Firestore `conversations`.
    Stores full messageHistory + preferences (property_context). No PostgreSQL.
    """
    client = get_firestore_client(target="admin" if agent_type == "admin" else "user")
    clean_sid = session_id.strip() if session_id else "anonymous"
    clean_origin = (site_origin or getattr(settings, "SITE_ORIGIN", "varsoviaestate.com")).strip()
    now_iso = datetime.now(timezone.utc).isoformat()
    doc_id = _conversation_doc_id(clean_sid, agent_type)

    empty = {
        "sessionId": clean_sid,
        "siteOrigin": clean_origin,
        "agentType": agent_type,
        "messageHistory": [],
        "preferences": {},
        "leadScore": 0.0,
        "createdAt": now_iso,
        "updatedAt": now_iso,
    }

    if not client:
        return FirestoreConversation(doc_id, empty)

    try:
        col = client.collection("conversations")
        doc_ref = col.document(doc_id)
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict() or {}
            return FirestoreConversation(doc.id, data)

        # Backward compatible: legacy docs keyed only by session_id (public)
        if agent_type == "public":
            legacy = col.document(clean_sid).get()
            if legacy.exists:
                data = legacy.to_dict() or {}
                data["agentType"] = agent_type
                data["sessionId"] = clean_sid
                # Migrate to agent-scoped id
                doc_ref.set(data, merge=True)
                return FirestoreConversation(doc_id, data)

        initial_data = dict(empty)
        doc_ref.set(initial_data)
        return FirestoreConversation(doc_id, initial_data)
    except Exception as e:
        logger.error(f"Error accessing conversation in Firestore: {e}")
        return FirestoreConversation(doc_id, empty)


def _compact_card_for_storage(c: Any) -> Any:
    """Extracts only essential fields from property cards to prevent document bloat."""
    if not isinstance(c, dict):
        return c
    compact = {
        "property_id": c.get("property_id") or c.get("id"),
        "name": c.get("name") or c.get("title"),
        "location": c.get("location"),
        "price": c.get("price"),
        "price_numeric_min": c.get("price_numeric_min"),
        "bedrooms": c.get("bedrooms"),
        "property_type": c.get("property_type")
    }
    imgs = c.get("images")
    if imgs and isinstance(imgs, list) and len(imgs) > 0:
        compact["image"] = imgs[0]
    elif c.get("featured_image"):
        compact["image"] = c.get("featured_image")
    return {k: v for k, v in compact.items() if v is not None}


def append_conversation_turn_firestore(
    conversation: Any,
    user_message: str,
    assistant_message: str,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    tool_results: Optional[List[Dict[str, Any]]] = None,
    structured_data: Optional[Dict[str, Any]] = None,
    new_preferences: Optional[Dict[str, Any]] = None
) -> Any:
    """
    Appends conversation turn directly in Firestore `conversations` collection.
    Guarantees document size stays strictly within Firestore's 1MB limit.
    """
    history = list(getattr(conversation, "message_history", []) or [])
    timestamp = datetime.now(timezone.utc).isoformat()

    # User turn
    history.append({
        "role": "user",
        "content": user_message,
        "timestamp": timestamp
    })

    # Assistant turn
    asst_turn = {
        "role": "assistant",
        "content": assistant_message,
        "timestamp": timestamp
    }
    if tool_calls:
        asst_turn["tool_calls"] = tool_calls
    if tool_results:
        def _strip_heavy(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {k: _strip_heavy(v) for k, v in obj.items() if k not in ("raw_firestore_data", "raw_data", "html_content", "base64", "pdf_bytes", "description")}
            elif isinstance(obj, (list, tuple, set)):
                return [_strip_heavy(item) for item in obj[:6]]
            return obj
        asst_turn["tool_results"] = _strip_heavy(tool_results)
    if structured_data:
        clean_struct = {}
        if "properties" in structured_data and isinstance(structured_data["properties"], list):
            clean_struct["properties"] = [_compact_card_for_storage(p) for p in structured_data["properties"][:6]]
            asst_turn["properties"] = clean_struct["properties"]
        elif "property_cards" in structured_data and isinstance(structured_data["property_cards"], list):
            clean_struct["properties"] = [_compact_card_for_storage(p) for p in structured_data["property_cards"][:6]]
            asst_turn["properties"] = clean_struct["properties"]
        if "property_ids" in structured_data:
            clean_struct["property_ids"] = structured_data["property_ids"]
        asst_turn["structured_data"] = clean_struct

    history.append(asst_turn)

    # Bound history for 100+ follow-ups (config-driven)
    max_msgs = int(getattr(settings, "SESSION_MEMORY_MESSAGES", 220) or 220)
    if len(history) > max_msgs:
        history = history[-max_msgs:]

    # Older turns: drop bulky cards/tools but KEEP property_ids for reference recovery
    keep_meta = 8
    for i in range(max(0, len(history) - keep_meta)):
        if not isinstance(history[i], dict):
            continue
        history[i].pop("tool_results", None)
        history[i].pop("tool_calls", None)
        history[i].pop("properties", None)
        sd = history[i].get("structured_data")
        if isinstance(sd, dict) and sd.get("property_ids"):
            history[i]["structured_data"] = {"property_ids": list(sd.get("property_ids") or [])[:10]}
        else:
            history[i].pop("structured_data", None)

    conversation.message_history = history

    # Update preferences (includes property_context)
    if new_preferences:
        prefs = dict(getattr(conversation, "preferences", {}) or {})
        for k, v in new_preferences.items():
            if v is not None:
                prefs[k] = v
        conversation.preferences = prefs

    conversation.updated_at = timestamp

    # Write FULL history + preferences to Firestore (no Postgres)
    sid = getattr(conversation, "session_id", None)
    agent_type = getattr(conversation, "agent_type", "public")
    client = get_firestore_client(target="admin" if agent_type == "admin" else "user")
    if client and sid:
        doc_id = _conversation_doc_id(sid, agent_type)
        try:
            doc_ref = client.collection("conversations").document(doc_id)
            payload = {
                "sessionId": sid,
                "siteOrigin": getattr(conversation, "site_origin", getattr(settings, "SITE_ORIGIN", "varsoviaestate.com")),
                "agentType": agent_type,
                "messageHistory": conversation.message_history,
                "preferences": conversation.preferences,
                "leadScore": getattr(conversation, "lead_score", 0.0),
                "updatedAt": timestamp,
            }

            # Proactive Firestore document size guard (max ~1MB)
            try:
                raw_bytes = len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
            except Exception:
                raw_bytes = 0

            if raw_bytes > 700_000:
                logger.warning(
                    f"Firestore conversation size ({raw_bytes} bytes) approaching 1MB. Compacting history..."
                )
                compacted = []
                for idx, t in enumerate(history):
                    t_copy = dict(t) if isinstance(t, dict) else {"role": "user", "content": str(t)}
                    if idx < len(history) - 1:
                        t_copy.pop("tool_results", None)
                        t_copy.pop("tool_calls", None)
                        t_copy.pop("properties", None)
                        sd = t_copy.get("structured_data")
                        if isinstance(sd, dict) and sd.get("property_ids"):
                            t_copy["structured_data"] = {"property_ids": list(sd["property_ids"])[:10]}
                        else:
                            t_copy.pop("structured_data", None)
                    compacted.append(t_copy)
                # Keep preferences (property_context) always; trim history only
                keep_n = max(40, min(max_msgs, 120))
                payload["messageHistory"] = compacted[-keep_n:]
                conversation.message_history = payload["messageHistory"]

            doc_ref.set(payload, merge=True)
            # Keep wrapper id aligned with Firestore doc id
            conversation.id = doc_id
        except Exception as e:
            err_str = str(e)
            if any(k in err_str.lower() for k in ["size", "exceeds", "1,048,576", "400"]):
                logger.warning(
                    f"Firestore document size exceeded for {sid}. Pruning while preserving preferences..."
                )
                try:
                    minimal_history = []
                    for t in history[-40:]:
                        entry = {
                            "role": t.get("role", "user"),
                            "content": (t.get("content") or "")[:1500],
                            "timestamp": t.get("timestamp", timestamp),
                        }
                        sd = t.get("structured_data") if isinstance(t, dict) else None
                        if isinstance(sd, dict) and sd.get("property_ids"):
                            entry["structured_data"] = {"property_ids": list(sd["property_ids"])[:10]}
                        minimal_history.append(entry)
                    doc_ref = client.collection("conversations").document(doc_id)
                    doc_ref.set({
                        "sessionId": sid,
                        "siteOrigin": getattr(conversation, "site_origin", getattr(settings, "SITE_ORIGIN", "varsoviaestate.com")),
                        "agentType": agent_type,
                        "messageHistory": minimal_history,
                        "preferences": getattr(conversation, "preferences", {}),
                        "leadScore": getattr(conversation, "lead_score", 0.0),
                        "updatedAt": timestamp,
                    }, merge=True)
                    conversation.message_history = minimal_history
                    conversation.id = doc_id
                    logger.info(f"Pruned and persisted conversation {sid} to Firestore below 1MB.")
                except Exception as retry_e:
                    logger.error(f"Fallback pruning for Firestore also failed: {retry_e}")
            else:
                logger.error(f"Error persisting conversation turn to Firestore: {e}")

    return conversation
