"""
Tajlandia AI Multi-Tenant Branding & Site Origin Resolution Service.
Supports dynamic branding for:
- tajlandia.com / tajlandia.pl ("Tajlandia AI")
- thai.estate ("Thai Estate")
- varsoviaestate.com ("Varsovia Estate")

Resolves tenant based on:
1. Explicit request site_origin (e.g. incoming header, domain, or chat payload)
2. Environment configuration (DEFAULT_TENANT / SITE_ORIGIN / BOT_NAME)
3. Default fallback (tajlandia.com)
"""
import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
from app.core.config import settings

logger = logging.getLogger(__name__)

@dataclass
class TenantProfile:
    site_origin: str
    brand_name: str
    bot_name: str
    brand_title: str
    brand_subtitle: str
    domain: str
    advisor_email: str
    website_url: str


TENANT_REGISTRY: Dict[str, TenantProfile] = {
    "tajlandia.com": TenantProfile(
        site_origin="tajlandia.com",
        brand_name="Tajlandia AI",
        bot_name="Tajlandia AI Advisor",
        brand_title="Tajlandia AI — Ekskluzywne Nieruchomości w Tajlandii",
        brand_subtitle="Ekskluzywne Nieruchomości",
        domain="tajlandia.com",
        advisor_email="advisor@tajlandia.com",
        website_url="https://tajlandia.com"
    ),
    "thai.estate": TenantProfile(
        site_origin="thai.estate",
        brand_name="Thai Estate",
        bot_name="Thai Estate Advisor",
        brand_title="Thai Estate — Luxury Thailand Real Estate",
        brand_subtitle="Luxury Real Estate",
        domain="thai.estate",
        advisor_email="advisor@thai.estate",
        website_url="https://thai.estate"
    ),
    "varsoviaestate.com": TenantProfile(
        site_origin="varsoviaestate.com",
        brand_name="Varsovia Estate",
        bot_name="Varsovia Estate Advisor",
        brand_title="Varsovia Estate — Premium Thailand Properties",
        brand_subtitle="Exclusive Estates",
        domain="varsoviaestate.com",
        advisor_email="advisor@varsoviaestate.com",
        website_url="https://varsoviaestate.com/en"
    ),
}

CANONICAL_DOMAINS = {
    "tajlandia": "tajlandia.com",
    "varsovia": "varsoviaestate.com",
    "thai": "thai.estate"
}


def normalize_tenant_domain_key(val: Optional[str]) -> str:
    """
    Normalizes any domain, site_origin, URL, or email to one of 3 canonical tenant keys:
      - 'tajlandia' (for tajlandia.com, tajlandia.pl, etc.)
      - 'varsovia' (for varsoviaestate.com, varsovia.estate, varsoviya, etc.)
      - 'thai' (for thai.estate, thairealestate.com, etc.)
    """
    if not val:
        return ""
    v = str(val).strip().lower()
    if "@" in v:
        v = v.split("@")[-1]
    if "://" in v:
        v = v.split("://")[1]
    v = v.split("/")[0].split(":")[0]

    if "tajlandia" in v:
        return "tajlandia"
    elif "varsovia" in v or "varsoviya" in v:
        return "varsovia"
    elif "thai" in v:
        return "thai"
    return v


ALL_CANONICAL_WEBSITES: list[str] = [
    "varsoviaestate.com",
    "tajlandia.com",
    "thai.estate"
]


def normalize_website_domain(val: Optional[str]) -> str:
    """
    Normalizes any input (key or domain, e.g. 'tajlandia', 'thai.estate', 'varsovia')
    to its full canonical domain string:
      - 'tajlandia.com'
      - 'varsoviaestate.com'
      - 'thai.estate'
    """
    if not val:
        return ""
    key = normalize_tenant_domain_key(val)
    if key in CANONICAL_DOMAINS:
        return CANONICAL_DOMAINS[key]
    val_clean = str(val).strip().lower()
    for canonical in ALL_CANONICAL_WEBSITES:
        if val_clean == canonical:
            return canonical
    return val_clean


def normalize_website_list(vals: Any) -> list[str]:
    """
    Normalizes a list, comma-separated string, or single string of websites
    into a deduplicated, ordered list of canonical domains.
    Example:
      ['tajlandia', 'varsoviaestate.com'] -> ['varsoviaestate.com', 'tajlandia.com']
      'thai.estate,tajlandia' -> ['tajlandia.com', 'thai.estate']
    """
    if not vals:
        return []
    raw_list: list[str] = []
    if isinstance(vals, str):
        raw_list = [s.strip() for s in vals.split(",") if s.strip()]
    elif isinstance(vals, (list, tuple, set)):
        for item in vals:
            if isinstance(item, str):
                for s in item.split(","):
                    if s.strip():
                        raw_list.append(s.strip())
    
    result: list[str] = []
    for r in raw_list:
        canon = normalize_website_domain(r)
        if canon and canon in ALL_CANONICAL_WEBSITES and canon not in result:
            result.append(canon)
        elif canon and canon not in result:
            result.append(canon)
    return result


def get_current_env_domain_key() -> str:
    """
    Returns the canonical domain key decided by .env
    (SITE_ORIGIN or DEFAULT_TENANT or BASE_WEBSITE_URL).
    """
    env_origin = (
        getattr(settings, "SITE_ORIGIN", None)
        or os.getenv("SITE_ORIGIN")
        or getattr(settings, "DEFAULT_TENANT", None)
        or os.getenv("DEFAULT_TENANT")
        or getattr(settings, "BASE_WEBSITE_URL", "varsoviaestate.com")
    )
    return normalize_tenant_domain_key(env_origin) or "varsovia"


def get_current_env_domain() -> str:
    """
    Returns the primary canonical domain name for the active .env.
    e.g. 'tajlandia.com', 'varsoviaestate.com', or 'thai.estate'
    """
    key = get_current_env_domain_key()
    return CANONICAL_DOMAINS.get(key, getattr(settings, "SITE_ORIGIN", "varsoviaestate.com"))


def get_user_domain_key(
    user_or_email: Any = None,
    user_domain: Optional[str] = None,
    preferences: Optional[dict] = None
) -> str:
    """
    Resolves the canonical domain key ('tajlandia', 'varsovia', 'thai') for a user.
    Checks:
    1. Explicit user.domain or user_domain argument
    2. user.preferences['domain'] or preferences['site_origin']
    3. Email domain
    4. Fallback: active .env domain key
    """
    d = user_domain
    if not d and hasattr(user_or_email, "domain") and getattr(user_or_email, "domain", None):
        d = user_or_email.domain
    if not d and hasattr(user_or_email, "preferences") and isinstance(user_or_email.preferences, dict):
        d = user_or_email.preferences.get("domain") or user_or_email.preferences.get("site_origin")
    if not d and isinstance(preferences, dict):
        d = preferences.get("domain") or preferences.get("site_origin")

    if d:
        k = normalize_tenant_domain_key(d)
        if k in ("tajlandia", "varsovia", "thai"):
            return k

    email_str = ""
    if isinstance(user_or_email, str):
        email_str = user_or_email
    elif hasattr(user_or_email, "email") and getattr(user_or_email, "email", None):
        email_str = user_or_email.email

    if email_str:
        k = normalize_tenant_domain_key(email_str)
        if k in ("tajlandia", "varsovia", "thai"):
            return k

    return get_current_env_domain_key()



def get_default_tenant_profile() -> TenantProfile:
    """
    Dynamically constructs the default tenant profile directly from .env / settings.
    Ensures that any change in .env (BASE_WEBSITE_URL, SITE_ORIGIN, BRAND_NAME, etc.)
    immediately controls branding, URLs, emails, and bot behavior without code changes!
    """
    site_origin = getattr(settings, "SITE_ORIGIN", "varsoviaestate.com") or "varsoviaestate.com"
    clean_domain = site_origin.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]
    return TenantProfile(
        site_origin=clean_domain,
        brand_name=getattr(settings, "BRAND_NAME", "Varsovia Estate") or "Varsovia Estate",
        bot_name=getattr(settings, "BOT_NAME", "Varsovia Estate Advisor") or "Varsovia Estate Advisor",
        brand_title=getattr(settings, "BRAND_TITLE", "Varsovia Estate — Premium Thailand Properties") or "Varsovia Estate — Premium Thailand Properties",
        brand_subtitle=getattr(settings, "BRAND_SUBTITLE", "Exclusive Estates") or "Exclusive Estates",
        domain=clean_domain,
        advisor_email=getattr(settings, "ADVISOR_EMAIL", "advisor@varsoviaestate.com") or "advisor@varsoviaestate.com",
        website_url=getattr(settings, "BASE_WEBSITE_URL", "https://varsoviaestate.com/en") or "https://varsoviaestate.com/en"
    )


def resolve_tenant(site_origin: Optional[str] = None) -> TenantProfile:
    """
    Resolves the appropriate TenantProfile based on incoming site_origin or environment.
    Priority:
    1. Explicit site_origin matching a registry tenant (tajlandia / thai.estate / varsovia).
    2. Otherwise, the dynamic default profile from .env / settings.
    """
    raw = (site_origin or "").strip().lower()

    # Clean domain wrappers (http://, https://, paths, ports)
    if "://" in raw:
        raw = raw.split("://")[1]
    raw = raw.split("/")[0].split(":")[0]

    if raw:
        key = normalize_tenant_domain_key(raw)
        if key == "tajlandia":
            return _apply_custom_bot_name(TENANT_REGISTRY["tajlandia.com"])
        if key == "thai":
            return _apply_custom_bot_name(TENANT_REGISTRY["thai.estate"])
        if key == "varsovia":
            return _apply_custom_bot_name(TENANT_REGISTRY["varsoviaestate.com"])
        for domain, profile in TENANT_REGISTRY.items():
            if domain == raw or raw.endswith(domain) or domain in raw:
                return _apply_custom_bot_name(profile)

        # Unknown host (e.g. "default", preview vercel.app) — keep default brand, log for ops.
        logger.warning(
            "Unknown site_origin=%r — using default tenant brand. "
            "Check client site_origin / embed settings.",
            site_origin or raw,
        )

    # Return dynamic default tenant loaded purely from .env
    return get_default_tenant_profile()


def _apply_custom_bot_name(profile: TenantProfile) -> TenantProfile:
    """
    Registry tenant profiles keep their own brand/bot/email.
    A global BOT_NAME (often Varsovia) must NOT overwrite tajlandia/thai tenants.
    """
    return profile


# ---------------------------------------------------------------------------
# Market scope per site.
# varsoviaestate.com sells every market in the catalogue (Thailand, Cambodia, Maldives).
# tajlandia.com and thai.estate are Thailand-only sites, so they must never show other countries.
# The site of the current public chat request is held in a ContextVar so every search path
# (many call sites) is scoped without changing each signature. Admin code never sets it, so the
# admin side stays unrestricted.
# ---------------------------------------------------------------------------
_REQUEST_SITE: ContextVar[Optional[str]] = ContextVar("request_site_origin", default=None)
_THAILAND_ONLY_SITES = frozenset({"tajlandia.com", "thai.estate"})


def set_request_site(site_origin: Optional[str]):
    """Mark the current public chat request with its site. Returns a token for reset_request_site."""
    return _REQUEST_SITE.set(str(site_origin or ""))


def reset_request_site(token) -> None:
    try:
        _REQUEST_SITE.reset(token)
    except Exception:
        pass


def allowed_countries_for_site(site_origin: Optional[str] = None) -> Optional[List[str]]:
    """
    Countries (lowercase, as stored in listing `province`) a site may show.
    None means no restriction (varsoviaestate.com, admin, or outside a public chat request).
    """
    raw = site_origin if site_origin is not None else _REQUEST_SITE.get()
    if raw is None:
        return None
    try:
        domain = resolve_tenant(raw).site_origin
    except Exception:
        return None
    if domain in _THAILAND_ONLY_SITES:
        return ["thailand"]
    return None
