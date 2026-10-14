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
        """Returns cached property records if valid and unexpired."""
        entry = self._cache.get(cache_key)
        if not entry:
            return None
        
        now = time.time()
        if now > entry["expires_at"]:
            del self._cache[cache_key]
            return None

        logger.debug(f"Cache HIT for key {cache_key}")
        return entry["data"]

    def set(self, cache_key: str, data: List[Dict[str, Any]], ttl_seconds: Optional[int] = None) -> None:
        """Stores property search results under normalized key."""
        ttl = ttl_seconds or self.default_ttl
        self._cache[cache_key] = {
            "data": data,
            "expires_at": time.time() + ttl
        }
        logger.debug(f"Cached {len(data)} results for key {cache_key} (TTL {ttl}s)")

    def invalidate(self) -> None:
        """Invalidates all cached search results when properties are updated."""
        self._cache.clear()
        self._version += 1
        logger.info(f"Property search cache invalidated. Active version: {self._version}")

# Global singleton cache instance
search_cache = PropertySearchCache()
