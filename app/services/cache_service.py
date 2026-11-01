import time
import hashlib
import json
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

class PropertySearchCache:
    """
    In-memory normalized search results cache with TTL expiration.
    Caches property IDs and card payloads for canonical search filter signatures.
    Invalidated instantly when listings or specs are updated.
    Guarantees soft-deleted listings are never served or retained.
    """
    def __init__(self, default_ttl_seconds: int = 900): # 15 minutes default TTL
        self.default_ttl = default_ttl_seconds
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._version: int = 1

    def make_cache_key(self, filters: Dict[str, Any], offset: int = 0, limit: int = 4) -> str:
        """Produces a deterministic, canonical hash key for a filter combination."""
        canonical_items = []
        for k in sorted(filters.keys()):
            v = filters[k]
            if v is not None:
                canonical_items.append((k, str(v).lower()))
        canonical_items.append(("offset", offset))
        canonical_items.append(("limit", limit))
        canonical_items.append(("v", self._version))
        
        serialized = json.dumps(canonical_items, sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]

    def get(self, cache_key: str) -> Optional[List[Dict[str, Any]]]:
        """Returns cached property records if valid and unexpired, filtering any soft-deleted items."""
        entry = self._cache.get(cache_key)
        if not entry:
            return None
        
        now = time.time()
        if now > entry["expires_at"]:
            del self._cache[cache_key]
            return None

        data = entry["data"]
        if isinstance(data, list):
            from app.services.firestore_service import is_soft_deleted_record
            clean = [
                p for p in data
                if not is_soft_deleted_record(p) and not (isinstance(p, dict) and p.get("is_deleted"))
            ]
            if len(clean) != len(data):
                entry["data"] = clean
            data = clean

        logger.debug(f"Cache HIT for key {cache_key}")
        return data

    def set(self, cache_key: str, data: List[Dict[str, Any]], ttl_seconds: Optional[int] = None) -> None:
        """Stores property search results under normalized key, filtering soft-deleted items."""
        ttl = ttl_seconds or self.default_ttl
        from app.services.firestore_service import is_soft_deleted_record
        clean_data = [
            p for p in data
            if not is_soft_deleted_record(p) and not (isinstance(p, dict) and p.get("is_deleted"))
        ] if isinstance(data, list) else data

        self._cache[cache_key] = {
            "data": clean_data,
            "expires_at": time.time() + ttl
        }
        cnt = len(clean_data) if isinstance(clean_data, list) else 1
        logger.debug(f"Cached {cnt} results for key {cache_key} (TTL {ttl}s)")

    def evict_property(self, property_id: str) -> int:
        """Evicts any cached result set that contains the given property_id."""
        if not property_id:
            return 0
        clean_id = str(property_id).strip().lower()
        keys_to_evict = []
        for key, entry in list(self._cache.items()):
            data = entry.get("data") or []
            if any(
                str(item.get("property_id") or item.get("id") or "").strip().lower() == clean_id
                for item in data if isinstance(item, dict)
            ):
                keys_to_evict.append(key)
        for key in keys_to_evict:
            self._cache.pop(key, None)
        if keys_to_evict:
            logger.info(f"Evicted {len(keys_to_evict)} cached queries containing property {property_id}")
        return len(keys_to_evict)

    def invalidate(self) -> None:
        """Invalidates all cached search results when properties are updated."""
        self._cache.clear()
        self._version += 1
        logger.info(f"Property search cache invalidated. Active version: {self._version}")

# Global singleton cache instance
search_cache = PropertySearchCache()
