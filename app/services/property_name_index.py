"""
In-memory property name index for confident / close matching.

Read-only cache of id, names, slugs, cityId refreshed about every 10 minutes.
Used by the public chatbot so short-name lookups are not limited to the first
400 Firestore documents.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from contextvars import ContextVar
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

CACHE_TTL_SEC = 600  # legacy default; runtime uses settings.NAME_INDEX_TTL_SEC when available
FUZZY_THRESHOLD = 0.86
FUZZY_GAP = 0.06  # best must beat 2nd by this much for confident fuzzy


def _name_index_ttl_sec() -> float:
    try:
        from app.core.config import settings
        return float(getattr(settings, "NAME_INDEX_TTL_SEC", CACHE_TTL_SEC) or CACHE_TTL_SEC)
    except Exception:
        return float(CACHE_TTL_SEC)

# Per-request (contextvar) Did-you-mean meta — never share across concurrent visitors
_cv_name_suggestions: ContextVar[Optional[Tuple[str, ...]]] = ContextVar("name_suggestions", default=None)
_cv_name_candidates: ContextVar[Optional[Tuple[Tuple[str, str], ...]]] = ContextVar("name_candidates", default=None)
_cv_name_query: ContextVar[Optional[str]] = ContextVar("name_query", default=None)

GENERIC_TOKENS = frozenset({
    "villa", "villas", "condo", "condos", "condominium", "condominiums",
    "residence", "residences", "apartment", "apartments", "home", "homes",
    "house", "houses", "estate", "estates", "resort", "resorts",
    "phuket", "samui", "koh", "bangkok", "pattaya", "phangan", "thailand",
    "tajlandia", "phase", "beach", "beachfront", "the", "and", "of", "at",
    "by", "in", "on", "new", "lux", "luxury", "premium", "private",
    "pool", "sea", "view", "seaview", "ocean", "bay", "heights", "tower",
    "towers", "park", "garden", "gardens", "eight", "nine", "one", "two",
    "three", "four", "five", "six", "seven", "apartamenty", "na", "z",
    "widokiem", "morze", "morskie",
})

# Conversational fillers (PL / EN) — ignored for distinctive token matching
FILLER_TOKENS = frozenset({
    "mi", "me", "my", "please", "pls", "a", "an", "the", "to", "for", "about",
    "show", "tell", "find", "get", "see", "give", "want", "need", "looking",
    "pokaz", "pokaż", "zobacz", "chce", "chcę", "prosze", "proszę", "o", "i",
    "daj", "poprosze", "poproszę", "jakis", "jakiś", "jakie", "jaką", "ten", "ta",
    "to", "tego", "tej", "those", "these", "that", "this", "some", "any",
    "can", "could", "would", "you", "your", "more", "info", "details", "detail",
})

# Address / street fragments: do not demote a unique brand hit (e.g. Mirage Sukhumvit 27)
LOCATIONISH_TOKENS = frozenset({
    "sukhumvit", "silom", "sathorn", "thonglor", "ekkamai", "ari", "onenimman",
    "road", "rd", "soi", "street", "st", "avenue", "ave", "boulevard", "blvd",
    "floor", "fl", "building", "bldg", "tower", "towers", "zone", "district",
    "bangtao", "layan", "kamala", "karon", "rawai", "patong", "chalong",
    "naithon", "nathon", "bophut", "maenam", "lamai", "choeng", "mon",
})

def get_last_name_search_suggestions() -> List[str]:
    """Per-request Did-you-mean labels (contextvar; safe under concurrent chats)."""
    tips = _cv_name_suggestions.get()
    return list(tips) if tips else []


def get_last_name_search_candidates() -> List[Dict[str, str]]:
    """List of {property_id, name} from this request's non-confident name search."""
    raw = _cv_name_candidates.get()
    if not raw:
        return []
    return [{"property_id": pid, "name": name} for pid, name in raw]


def get_last_name_search_query() -> Optional[str]:
    return _cv_name_query.get()


def set_last_name_search_meta(
    query: Optional[str],
    suggestions: Optional[Sequence[str]] = None,
    candidates: Optional[Sequence[Any]] = None,
) -> None:
    """
    Store Did-you-mean meta in the current context only (not process-global).

    Always pass candidates (objects/dicts with id + name) when available.
    If only display-name suggestions are provided, recover ids from the name index
    so pending confirmation ("yes") still works.
    """
    q = (query or "").strip() or None
    cand_pairs: List[Tuple[str, str]] = []
    if candidates:
        for c in candidates:
            if isinstance(c, dict):
                pid = str(c.get("property_id") or c.get("doc_id") or "").strip()
                name = str(c.get("name") or c.get("display_name") or "").strip()
            else:
                pid = str(getattr(c, "doc_id", "") or "").strip()
                name = str(getattr(c, "display_name", "") or "").strip()
            if pid and name:
                cand_pairs.append((pid, name))
    if suggestions is not None:
        tips = tuple(str(s).strip() for s in suggestions if str(s).strip())
    else:
        tips = tuple(name for _pid, name in cand_pairs)

    # Recover property ids when callers only passed suggestion labels
    if tips and not cand_pairs:
        try:
            idx = get_property_name_index()
            entries = list(getattr(idx, "_entries", []) or [])
            for tip in tips:
                tip_n = normalize_property_name(tip)
                if not tip_n:
                    continue
                hit = next(
                    (
                        e for e in entries
                        if normalize_property_name(e.display_name) == tip_n
                        or tip_n in (e.aliases_norm or [])
                    ),
                    None,
                )
                if hit and hit.doc_id and hit.display_name:
                    cand_pairs.append((hit.doc_id, hit.display_name))
        except Exception:
            pass

    _cv_name_query.set(q)
    _cv_name_candidates.set(tuple(cand_pairs) if cand_pairs else ())
    _cv_name_suggestions.set(tips)


def normalize_property_name(text: Optional[str]) -> str:
    """
    Lowercase, trim, strip accents, normalize apostrophes/hyphens to spaces,
    drop other punctuation, collapse whitespace.
    """
    if text is None:
        return ""
    s = str(text).strip().lower()
    if not s:
        return ""
    # Unify apostrophe / quote variants
    for ch in ("'", "'", "`", "´", "'"):
        s = s.replace(ch, " ")
    s = s.replace("-", " ").replace("_", " ").replace("/", " ")
    # Strip accents
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def strip_filler_words(text: Optional[str]) -> str:
    """Drop PL/EN conversational fillers before name matching (e.g. 'mi Anava')."""
    norm = normalize_property_name(text)
    if not norm:
        return ""
    kept = [t for t in norm.split() if t and t not in FILLER_TOKENS]
    return " ".join(kept)


def distinctive_tokens(text: Optional[str]) -> List[str]:
    # Strip fillers first so "mi Anava" / "pokaż mi Anava" keep only "anava"
    cleaned = strip_filler_words(text)
    return [
        t for t in cleaned.split()
        if t and t not in GENERIC_TOKENS and t not in FILLER_TOKENS and len(t) >= 2
    ]


def _field_strings(data: Dict[str, Any], doc_id: str) -> List[str]:
    out: List[str] = [str(doc_id)]
    for key in ("name", "title", "seoTitle", "projectName", "slug"):
        val = data.get(key)
        if isinstance(val, dict):
            for v in val.values():
                if v:
                    out.append(str(v))
        elif val:
            out.append(str(val))
    # Flat CSV-style keys
    for key in ("name_pl", "name_en", "slug_pl", "slug_en", "seoTitle_pl", "seoTitle_en"):
        if data.get(key):
            out.append(str(data[key]))
    return out


def display_name_from_data(data: Dict[str, Any], doc_id: str) -> str:
    raw = data.get("name") or data.get("title") or data.get("projectName")
    if isinstance(raw, dict):
        for k in ("en", "pl", "th", "ru"):
            if raw.get(k) and str(raw.get(k)).strip():
                return str(raw.get(k)).strip()
        for v in raw.values():
            if v and str(v).strip():
                return str(v).strip()
    if raw and str(raw).strip():
        return str(raw).strip()
    for key in ("name_en", "name_pl"):
        if data.get(key) and str(data[key]).strip():
            return str(data[key]).strip()
    return str(doc_id)


@dataclass
class NameIndexEntry:
    doc_id: str
    display_name: str
    city_id: Optional[str] = None
    aliases_norm: List[str] = field(default_factory=list)
    tokens: List[str] = field(default_factory=list)


@dataclass
class NameMatchResult:
    confident: Optional[NameIndexEntry] = None
    candidates: List[NameIndexEntry] = field(default_factory=list)
    reason: str = ""
    query: Optional[str] = None
    suggestions: List[str] = field(default_factory=list)


class PropertyNameIndex:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: List[NameIndexEntry] = []
        self._by_id: Dict[str, NameIndexEntry] = {}
        self._loaded_at: float = 0.0
        self._loader: Optional[Callable[[], List[Dict[str, Any]]]] = None
        self._refreshing = False
        self._inflight_event: Optional[threading.Event] = None

    def configure_loader(self, loader: Callable[[], List[Dict[str, Any]]]) -> None:
        self._loader = loader

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            self._by_id = {}
            self._loaded_at = 0.0

    def load_from_rows(self, rows: Sequence[Dict[str, Any]]) -> None:
        """Load from raw project dicts (or CSV-shaped rows with doc_id)."""
        t0 = time.monotonic()
        entries: List[NameIndexEntry] = []
        by_id: Dict[str, NameIndexEntry] = {}
        for row in rows:
            doc_id = str(row.get("doc_id") or row.get("id") or row.get("property_id") or "").strip()
            if not doc_id:
                continue
            # Support both Firestore shape and flat CSV
            data = dict(row)
            if "name" not in data and (row.get("name_pl") or row.get("name_en")):
                data["name"] = {
                    "pl": row.get("name_pl") or "",
                    "en": row.get("name_en") or "",
                }
            if "slug" not in data and (row.get("slug_pl") or row.get("slug_en")):
                data["slug"] = {
                    "pl": row.get("slug_pl") or "",
                    "en": row.get("slug_en") or "",
                }
            aliases = []
            seen = set()
            for raw in _field_strings(data, doc_id):
                n = normalize_property_name(raw)
                if n and n not in seen:
                    seen.add(n)
                    aliases.append(n)
            display = display_name_from_data(data, doc_id)
            tok: List[str] = []
            tok_seen = set()
            for a in aliases:
                for t in distinctive_tokens(a):
                    if t not in tok_seen:
                        tok_seen.add(t)
                        tok.append(t)
            city = row.get("cityId") or row.get("city_id") or (data.get("cityId") if isinstance(data, dict) else None)
            entry = NameIndexEntry(
                doc_id=doc_id,
                display_name=display,
                city_id=str(city).strip().lower() if city else None,
                aliases_norm=aliases,
                tokens=tok,
            )
            entries.append(entry)
            by_id[doc_id] = entry
        with self._lock:
            self._entries = entries
            self._by_id = by_id
            self._loaded_at = time.time()
        logger.info(
            "PropertyNameIndex loaded %s entries (timing.name_index_load duration_sec=%.3f)",
            len(entries),
            time.monotonic() - t0,
        )

    def _run_loader(self, loader: Callable[[], List[Dict[str, Any]]]) -> None:
        try:
            rows = loader()
            self.load_from_rows(rows or [])
        except Exception as e:
            # Failed refresh must never wipe good cached data
            logger.warning("PropertyNameIndex refresh failed (keeping stale): %s", e)

    def _background_refresh(self, loader: Callable[[], List[Dict[str, Any]]]) -> None:
        try:
            self._run_loader(loader)
        finally:
            with self._lock:
                self._refreshing = False

    def ensure_fresh(self, force: bool = False) -> None:
        """
        Keep the name index warm with single-flight + stale-while-revalidate.
        - Fresh: no-op.
        - Expired with data: return immediately; one background refresh.
        - Cold / force: only one thread loads; others wait for that result.
        """
        ttl = _name_index_ttl_sec()
        leader = False
        wait_event: Optional[threading.Event] = None
        bg_loader: Optional[Callable[[], List[Dict[str, Any]]]] = None

        with self._lock:
            age = time.time() - self._loaded_at
            has_data = bool(self._entries)
            if not force and has_data and age < ttl:
                return

            loader = self._loader
            if loader is None:
                return

            # Stale-while-revalidate: serve existing entries, refresh in background
            if not force and has_data:
                if not self._refreshing:
                    self._refreshing = True
                    bg_loader = loader
                # else already refreshing — callers keep using stale
            else:
                # Cold miss or forced reload — single-flight
                if self._refreshing and self._inflight_event is not None:
                    wait_event = self._inflight_event
                else:
                    self._refreshing = True
                    self._inflight_event = threading.Event()
                    wait_event = self._inflight_event
                    leader = True

        if bg_loader is not None:
            threading.Thread(
                target=self._background_refresh,
                args=(bg_loader,),
                daemon=True,
                name="name-index-swr",
            ).start()
            return

        if not leader and wait_event is not None:
            wait_event.wait(timeout=90.0)
            return

        if leader:
            try:
                self._run_loader(loader)
            finally:
                with self._lock:
                    self._refreshing = False
                    ev = self._inflight_event
                    self._inflight_event = None
                    if ev is not None:
                        ev.set()

    def match(self, query: str) -> NameMatchResult:
        self.ensure_fresh()
        q_raw = (query or "").strip()
        if not q_raw:
            return NameMatchResult(reason="empty", query=None)

        # Exact match uses the full normalized query so "The Ville Condo Samui" still hits.
        # Token / filler-stripped form handles "mi Anava" / "pokaż mi Anava".
        q_norm = normalize_property_name(q_raw)
        q_norm_stripped = normalize_property_name(strip_filler_words(q_raw) or q_raw)
        q_tokens = distinctive_tokens(q_raw)

        # Generic-only query: never confident (unless exact alias below)
        with self._lock:
            entries = list(self._entries)

        # 1. Exact normalized alias match (full or filler-stripped)
        exact = [
            e for e in entries
            if q_norm in e.aliases_norm or (q_norm_stripped and q_norm_stripped in e.aliases_norm)
        ]
        if len(exact) == 1:
            set_last_name_search_meta(q_raw, [])
            return NameMatchResult(confident=exact[0], reason="exact", query=q_raw, suggestions=[])
        if len(exact) > 1:
            q_slug = re.sub(r"[^a-z0-9]+", "-", q_norm).strip("-")

            def _exact_rank(e: NameIndexEntry) -> tuple:
                disp_norm = normalize_property_name(e.display_name)
                return (
                    0 if disp_norm == q_norm or disp_norm == q_norm_stripped else 1,
                    0 if normalize_property_name(e.doc_id) == q_norm or e.doc_id.lower() == q_slug else 1,
                    len(e.display_name),
                    e.doc_id,
                )

            exact_sorted = sorted(exact, key=_exact_rank)
            set_last_name_search_meta(q_raw, [])
            return NameMatchResult(
                confident=exact_sorted[0],
                candidates=exact_sorted[:3],
                reason="exact_multi",
                query=q_raw,
                suggestions=[],
            )

        if not q_tokens:
            set_last_name_search_meta(q_raw, [])
            return NameMatchResult(reason="generic_only", query=q_raw, suggestions=[])

        scored: List[Tuple[float, str, NameIndexEntry]] = []

        for e in entries:
            # Token overlap: all query distinctive tokens must appear in entry tokens
            if q_tokens and all(t in e.tokens for t in q_tokens):
                # Score by how specific: more shared relative to entry size
                overlap = len(q_tokens)
                score = overlap / max(len(e.tokens), 1) + overlap * 0.15
                # Boost if query tokens form a contiguous phrase in an alias
                joined = " ".join(q_tokens)
                if any(joined in a for a in e.aliases_norm):
                    score += 0.35
                scored.append((score, "token", e))
                continue

            # Fuzzy against aliases
            best_ratio = 0.0
            for a in e.aliases_norm:
                if not a:
                    continue
                # Skip very short alias fuzzy with short query noise
                if len(q_norm) < 4 or len(a) < 4:
                    continue
                r = SequenceMatcher(None, q_norm, a).ratio()
                if r > best_ratio:
                    best_ratio = r
            if best_ratio >= FUZZY_THRESHOLD:
                scored.append((best_ratio, "fuzzy", e))

        if not scored:
            set_last_name_search_meta(q_raw, [])
            return NameMatchResult(reason="no_match")

        scored.sort(key=lambda x: (-x[0], x[2].doc_id))
        best_score, best_kind, best_entry = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0

        candidates = []
        seen_ids = set()
        for _s, _k, e in scored:
            if e.doc_id in seen_ids:
                continue
            seen_ids.add(e.doc_id)
            candidates.append(e)
            if len(candidates) >= 3:
                break

        # Confident token match: all distinctive tokens matched AND (unique top or strong gap)
        confident = None
        if best_kind == "token":
            # Require uniqueness or clear gap; reject if query is single short token shared by many
            same_score = [s for s in scored if s[0] >= best_score - 0.001 and s[1] == "token"]
            if len(same_score) == 1:
                confident = best_entry
            elif best_score - second >= 0.2:
                confident = best_entry
            # Single distinctive token that matches many projects -> not confident
            if len(q_tokens) == 1 and len(same_score) > 1:
                confident = None
        elif best_kind == "fuzzy":
            if best_score >= FUZZY_THRESHOLD and (best_score - second) >= FUZZY_GAP:
                confident = best_entry

        # Place contradiction: the visitor named a place ("Villa Mirage Samui") that the
        # matched listing is not in (Mirage Sukhumvit 27 is in Bangkok). A shared brand word
        # is not enough to present it as "the" listing, so treat it as did-you-mean instead.
        if confident is not None and confident.city_id in _KNOWN_CITY_SLUGS:
            q_words = set(re.findall(r"[a-z]+", q_norm))
            asked = {_PLACE_WORDS[w] for w in q_words if w in _PLACE_WORDS}
            if asked and confident.city_id not in asked and not any(
                w in a.split() for w in asked for a in confident.aliases_norm
            ):
                confident = None

        # Partial brand names: demote only when the query equals the *primary*
        # brand token but the listing still has another name-like token
        # (e.g. "Satoshi" vs "Satoshi's Paradise"). Location suffixes
        # (Sukhumvit 27) and generics (Villas, Condominium) do not demote.
        # Non-primary unique hits stay confident ("Champaca" -> Mono Champaca).
        if confident is not None and q_norm not in confident.aliases_norm and len(q_tokens) == 1:
            qt = q_tokens[0]
            matching_aliases = [
                a for a in confident.aliases_norm
                if qt in distinctive_tokens(a) or qt in a.split()
            ]
            if matching_aliases:
                short = min(matching_aliases, key=lambda a: (len(a.split()), len(a)))
                short_dist = distinctive_tokens(short)
                primary = short_dist[0] if short_dist else ""
                extras = [
                    t for t in short_dist
                    if t != qt and t not in LOCATIONISH_TOKENS and not t.isdigit()
                ]
                if primary == qt and extras:
                    confident = None

        if confident is None:
            set_last_name_search_meta(q_raw, candidates=candidates)
            tips = [c.display_name for c in candidates]
        else:
            set_last_name_search_meta(q_raw, suggestions=[], candidates=[])
            tips = []
        return NameMatchResult(
            confident=confident,
            candidates=candidates if confident is None else [],
            reason="token" if best_kind == "token" else "fuzzy",
            query=q_raw,
            suggestions=tips,
        )


# Place words a visitor may attach to a project name -> city slug used in the catalog
_PLACE_WORDS = {
    "phuket": "phuket", "samui": "samui", "bangkok": "bangkok", "pattaya": "pattaya",
    "phangan": "phangan", "maldives": "maldives", "sihanoukville": "sihanoukville",
}
_KNOWN_CITY_SLUGS = frozenset(set(_PLACE_WORDS.values()) | {"phnom-penh"})

# Process-wide index
_INDEX = PropertyNameIndex()


def get_property_name_index() -> PropertyNameIndex:
    return _INDEX


def match_property_name(query: str) -> NameMatchResult:
    return _INDEX.match(query)


def default_firestore_name_loader(target: str = "user") -> List[Dict[str, Any]]:
    """
    Read-only projection: id + name/slug/title/cityId fields only.
    Streams all project docs (paginated) without full normalize.
    """
    from app.services.firestore_service import get_firestore_client

    t0 = time.monotonic()
    client = get_firestore_client(target=target)
    if not client:
        return []
    col = client.collection("projects")
    rows: List[Dict[str, Any]] = []
    # Select only needed fields when possible
    try:
        stream = col.select(
            ["name", "title", "seoTitle", "projectName", "slug", "cityId"]
        ).stream()
    except Exception:
        stream = col.stream()

    for doc in stream:
        data = doc.to_dict() or {}
        data["doc_id"] = doc.id
        data["id"] = doc.id
        rows.append(data)
    logger.info(
        "timing.name_index_stream duration_sec=%.3f count=%s target=%s",
        time.monotonic() - t0,
        len(rows),
        target,
    )
    return rows


def ensure_name_index_from_firestore(target: str = "user", force: bool = False) -> None:
    idx = get_property_name_index()
    if idx._loader is None:  # noqa: SLF001 — intentional configure-once
        idx.configure_loader(lambda: default_firestore_name_loader(target=target))
    idx.ensure_fresh(force=force)


def prewarm_property_name_index(target: str = "user") -> None:
    """Best-effort name-index warm — safe for startup background thread."""
    try:
        ensure_name_index_from_firestore(target=target, force=True)
        logger.info("Property name index pre-warm complete (target=%s).", target)
    except Exception as e:
        logger.warning("Property name index pre-warm skipped: %s", e)
