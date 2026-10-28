import logging
import os
import sys
from typing import List, Union, Optional, Dict, Any
from urllib.parse import urlparse
from pydantic_settings import BaseSettings
from pydantic import field_validator

logger = logging.getLogger(__name__)

# Clearly fake — no real credentials. Production/dev must set DATABASE_URL via env/.env.
_DATABASE_URL_PLACEHOLDER = "postgresql://USER:PASSWORD@127.0.0.1:5432/tajlandia_db"

_WEAK_SECRETS = {
    "",
    "secret",
    "changeme",
    "password",
    "tajlandia-internal-secret-token",
    "tajlandia-oauth-state-secret",
    "tajlandia-reset-secret",
    "test",
    "dev",
    "default",
}


class Settings(BaseSettings):
    PROJECT_NAME: str = "Varsovia Estate AI Property Platform"
    ENVIRONMENT: str = "development"
    
    # Dynamic Website, Branding & Domain Configuration (Configurable purely via .env)
    BASE_WEBSITE_URL: str = "https://varsoviaestate.com/en"
    DEFAULT_TENANT: str = "varsoviaestate.com"
    SITE_ORIGIN: str = "varsoviaestate.com"
    BRAND_NAME: str = "Varsovia Estate"
    BOT_NAME: Optional[str] = "Varsovia Estate Advisor"
    BRAND_TITLE: str = "Varsovia Estate — Premium Thailand Properties"
    BRAND_SUBTITLE: str = "Exclusive Estates"
    ADVISOR_EMAIL: str = "advisor@varsoviaestate.com"

    def get_property_detail_url(
        self,
        slug_or_id: str,
        country: Optional[str] = None,
        city: Optional[str] = None
    ) -> str:
        """
        Dynamically builds property detail page URL based on BASE_WEBSITE_URL from .env.
        - On varsoviaestate.com: {base}/properties/{country}/{city}/{slug}
        - Fallback/Legacy: {base}/oferty/{slug}
        """
        base = (self.BASE_WEBSITE_URL or "https://varsoviaestate.com/en").rstrip("/")
        clean_slug = str(slug_or_id or "").strip("/")

        if "varsovia" in base:
            raw_c = str(country or "").lower().strip()
            if "cambod" in raw_c or "kambod" in raw_c:
                c_slug = "cambodia"
            elif "viet" in raw_c:
                c_slug = "vietnam"
            else:
                c_slug = "thailand"

            raw_city = str(city or "").lower().strip()
            if "samui" in raw_city:
                city_slug = "samui"
            elif "phnom" in raw_city or "penh" in raw_city:
                city_slug = "phnom-penh"
            elif "sihanouk" in raw_city:
                city_slug = "sihanoukville"
            elif "pattaya" in raw_city:
                city_slug = "pattaya"
            elif "bangkok" in raw_city:
                city_slug = "bangkok"
            elif "phangan" in raw_city:
                city_slug = "phangan"
            elif "hua" in raw_city or "hin" in raw_city:
                city_slug = "hua-hin"
            elif "chiang" in raw_city:
                city_slug = "chiang-mai"
            else:
                city_slug = "phuket"

            return f"{base}/properties/{c_slug}/{city_slug}/{clean_slug}"

        return f"{base}/oferty/{clean_slug}"
    
    # Database Configuration (Multi-Database Support for Admin vs User Side)
    # Must be set via environment / .env in non-test runs — default has no real credentials.
    DATABASE_URL: str = ""
    ADMIN_DATABASE_URL: Optional[str] = None
    USER_DATABASE_URL: Optional[str] = None
    # Optional app signing secret (also check INTERNAL_API_SECRET for weakness)
    SECRET_KEY: str = ""
    
    # AI Configuration — accuracy over speed/cost; harden timeouts so one stuck call cannot hang Cloud Run
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-5.5"
    OPENAI_TEMPERATURE: float = 0.0
    OPENAI_TIMEOUT: float = 25.0  # seconds per request; honest fallback on timeout
    # Reasoning effort for calls WITHOUT tools (answer writing, guardrail checks). Default "medium" on
    # gpt-5.x is what made every reply take ~18 s. "low" keeps answers grounded (data comes from tools).
    OPENAI_REASONING_EFFORT: str = "low"
    # Search results: deterministic verified text (fast, cannot misquote a price) vs model-written wording.
    SEARCH_REPLY_USE_LLM: bool = False
    OPENAI_MAX_RETRIES: int = 1  # one retry after timeout/transient failure

    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-opus-5-5"
    ANTHROPIC_TEMPERATURE: float = 0.0
    ANTHROPIC_TIMEOUT: float = 60.0  # default; prefer ADMIN_MAIN_TIMEOUT / ADMIN_HELPER_TIMEOUT
    ANTHROPIC_MAX_RETRIES: int = 1  # one retry on connection errors only (not timeouts)

    # Admin chatbot performance (speed without losing accuracy)
    ADMIN_FAST_MODEL: str = "claude-haiku-4-5"  # classifier + guardrail judge
    ADMIN_MAIN_MODEL: str = ""  # empty → use ANTHROPIC_MODEL; set e.g. claude-sonnet-4-5 via env
    ADMIN_HELPER_TIMEOUT: float = 30.0
    ADMIN_MAIN_TIMEOUT: float = 60.0
    ADMIN_TURN_BUDGET_SEC: float = 75.0
    ADMIN_DEBUG_TIMING: bool = False
    ADMIN_TOOL_CACHE_TTL_SEC: float = 60.0
    ADMIN_MAX_TOOL_ROUNDS: int = 3
    ADMIN_NORMAL_MAX_TOKENS: int = 700
    ADMIN_LIST_MAX_TOKENS: int = 2000

    # Property catalog / name-index in-memory cache (stale-while-revalidate)
    CATALOG_CACHE_TTL_SEC: float = 900.0  # 15 minutes
    # Fixed FX used ONLY to compare listing prices against THB budgets (a few listings are priced in USD/EUR)
    FX_USD_THB: float = 35.0
    FX_EUR_THB: float = 38.0
    NAME_INDEX_TTL_SEC: float = 600.0  # 10 minutes

    # In-session memory: support 100+ consecutive property follow-ups
    SESSION_MEMORY_TURNS: int = 100
    SESSION_MEMORY_MESSAGES: int = 220  # 110 user+assistant turns buffered in memory
    # Cap real visitor chat messages per session (0 or less = unlimited)
    MAX_USER_MESSAGES_PER_SESSION: int = 50

    # Brochure PDF engine: "reportlab" (legacy) or "html" (Jinja2 + WeasyPrint)
    BROCHURE_ENGINE: str = "reportlab"
    # If HTML engine fails, fall back to ReportLab when true
    BROCHURE_FALLBACK_TO_REPORTLAB: bool = True
    # User-side brochure copy (Claude) — does not change OPENAI_MODEL / public chat
    BROCHURE_LLM_ENABLED: bool = True
    BROCHURE_LLM_MODEL: str = "claude-sonnet-5-5"
    BROCHURE_LLM_TIMEOUT: float = 25.0
    BROCHURE_LLM_MAX_TOKENS: int = 1200
    BROCHURE_LLM_CACHE_TTL_HOURS: float = 24.0
    BROCHURE_LLM_FOOTER_NOTE: bool = True
    # Force a brochure visual theme 1–7 (None = random). Also overridable via BROCHURE_FORCE_THEME env.
    BROCHURE_FORCE_THEME: Optional[int] = None

    # Persistent property context (reference memory separate from live facts)
    PROPERTY_CONTEXT_RECENT_MESSAGES: int = 20
    PROPERTY_CONTEXT_SUMMARY_CHARS: int = 4000
    # User-only turns fed to the public LLM (never prior assistant prose)
    PUBLIC_LLM_HISTORY_USER_TURNS: int = 6
    # Cap temperature for factual / tool-required public turns (0 = phrase from facts only)
    PUBLIC_LLM_FACTUAL_TEMPERATURE: float = 0.0
    # Chat history + property_context always go to Firestore (not Postgres)
    ENABLE_PROPERTY_CONTEXT_PERSISTENCE: bool = True

    # Lead/audit permanent writes (separate from chat memory which is always Firestore when sync is on)
    ENABLE_LEAD_SAVING: bool = True
    ENABLE_CHAT_LOGGING: bool = True
    CHAT_LOG_MASK_PII: bool = True
    ENABLE_AUDIT_LOGGING: bool = False
    # Absolute base for relative listing image URLs on property cards
    PUBLIC_API_BASE_URL: str = ""

    # Action Confirmation & Security
    ACTION_EXPIRATION_MINUTES: int = 30
    # Preferred alias for pending-action TTL (Task E); falls back to ACTION_EXPIRATION_MINUTES
    ADMIN_PENDING_ACTION_TTL_MIN: int = 30
    INTERNAL_API_SECRET: str = "tajlandia-internal-secret-token"
    ADMIN_EMAIL: Optional[str] = None
    ADMIN_PASSWORD: Optional[str] = None
    SESSION_EXPIRE_HOURS: int = 168  # 7 days session validity
    # Admin batch/CRUD undo snapshots in Firestore admin_batch_operations
    ADMIN_SNAPSHOT_RETENTION_DAYS: int = 6

    # Public property-page import (admin chatbot). Empty allow-list means any public domain.
    INGEST_ALLOWED_DOMAINS: str = ""
    INGEST_BLOCKED_DOMAINS: str = ""
    INGEST_IGNORE_ROBOTS: bool = False
    INGEST_MAX_BYTES: int = 3_000_000
    INGEST_TIMEOUT_SEC: float = 10.0
    INGEST_MAX_URLS_PER_HOUR: int = 10

    # Super-admin instruction store. "user" is the public bot's Firestore project.
    INSTRUCTIONS_FIRESTORE_TARGET: str = "user"

    # Google Workspace Integrations (OAuth2, Maps, SMTP)
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "https://web-production-935b27.up.railway.app/api/v1/auth/google/callback"
    GOOGLE_MAPS_API_KEY: str = ""

    # Gmail / SMTP Configuration
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    TAJLANDIA_SALES_EMAIL: str = "sales@tajlandia.com"

    # Report Generation Storage
    REPORT_STORAGE_DIR: str = "data/reports"
    # True when REPORT_STORAGE_DIR is a mounted persistent volume (not ephemeral container disk)
    REPORT_PERSISTENT_VOLUME: bool = False
    REPORT_RETENTION_DAYS: int = 7
    REPORT_SIGNED_LINK_TTL_SEC: int = 600

    # Universal / Fallback Cloud Firestore & Firebase Settings
    FIRESTORE_PROJECT_ID: str = "varsovia-8c135"
    FIRESTORE_DATABASE: str = "(default)"
    FIREBASE_SERVICE_ACCOUNT_KEY: Optional[str] = None
    FIREBASE_CREDENTIALS_PATH: str = "credentials/firebase_service_account.json"
    ENABLE_FIRESTORE_SYNC: bool = True

    # Dedicated Admin Cloud Firestore Settings (Admin Auth, Audit Logs & Management)
    ADMIN_FIRESTORE_PROJECT_ID: Optional[str] = None
    ADMIN_FIRESTORE_DATABASE: Optional[str] = None
    ADMIN_FIREBASE_SERVICE_ACCOUNT_KEY: Optional[str] = None
    ADMIN_FIREBASE_CREDENTIALS_PATH: Optional[str] = None
    ADMIN_ENABLE_FIRESTORE_SYNC: Optional[bool] = None

    # Dedicated User Cloud Firestore Settings (Public Chatbot, Leads & Property Catalog)
    USER_FIRESTORE_PROJECT_ID: Optional[str] = None
    USER_FIRESTORE_DATABASE: Optional[str] = None
    USER_FIREBASE_SERVICE_ACCOUNT_KEY: Optional[str] = None
    USER_FIREBASE_CREDENTIALS_PATH: Optional[str] = None
    USER_ENABLE_FIRESTORE_SYNC: Optional[bool] = None

    def get_admin_db_url(self) -> str:
        return self.ADMIN_DATABASE_URL or self.DATABASE_URL

    def get_user_db_url(self) -> str:
        return self.USER_DATABASE_URL or self.DATABASE_URL

    def get_admin_firestore_project_id(self) -> str:
        return self.ADMIN_FIRESTORE_PROJECT_ID or self.FIRESTORE_PROJECT_ID or "varsovia-8c135"

    def get_admin_firestore_database(self) -> str:
        return self.ADMIN_FIRESTORE_DATABASE or self.FIRESTORE_DATABASE or "(default)"

    def get_admin_firebase_service_account_key(self) -> Optional[str]:
        return self.ADMIN_FIREBASE_SERVICE_ACCOUNT_KEY or self.FIREBASE_SERVICE_ACCOUNT_KEY

    def get_admin_firebase_credentials_path(self) -> str:
        return self.ADMIN_FIREBASE_CREDENTIALS_PATH or self.FIREBASE_CREDENTIALS_PATH

    def get_admin_enable_firestore_sync(self) -> bool:
        if self.ADMIN_ENABLE_FIRESTORE_SYNC is not None:
            return self.ADMIN_ENABLE_FIRESTORE_SYNC
        return self.ENABLE_FIRESTORE_SYNC

    def get_user_firestore_project_id(self) -> str:
        return self.USER_FIRESTORE_PROJECT_ID or self.FIRESTORE_PROJECT_ID or "varsovia-8c135"

    def get_user_firestore_database(self) -> str:
        return self.USER_FIRESTORE_DATABASE or self.FIRESTORE_DATABASE or "(default)"

    def get_user_firebase_service_account_key(self) -> Optional[str]:
        return self.USER_FIREBASE_SERVICE_ACCOUNT_KEY or self.FIREBASE_SERVICE_ACCOUNT_KEY

    def get_user_firebase_credentials_path(self) -> str:
        return self.USER_FIREBASE_CREDENTIALS_PATH or self.FIREBASE_CREDENTIALS_PATH

    def get_user_enable_firestore_sync(self) -> bool:
        if self.USER_ENABLE_FIRESTORE_SYNC is not None:
            return self.USER_ENABLE_FIRESTORE_SYNC
        return self.ENABLE_FIRESTORE_SYNC
    
    # Vector Search Configuration
    EMBEDDING_DIMENSION: int = 1536
    

    # CORS Configuration
    ALLOWED_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8000",
        "https://tajlandia.pl",
        "https://thai.estate",
        "https://varsoviaestate.com",
        "https://tajlandia.com",
        "https://ai-chatbot-admin-web-git-ai-chatbot-admin-dev-sbt-pm.vercel.app",
    ]



    @field_validator("DATABASE_URL", "ADMIN_DATABASE_URL", "USER_DATABASE_URL", mode="before")
    def assemble_db_url(cls, v: Optional[str]) -> Optional[str]:
        if isinstance(v, str):
            if v.startswith("postgres://"):
                return v.replace("postgres://", "postgresql+psycopg2://", 1)
            elif v.startswith("postgresql://") and not v.startswith("postgresql+"):
                return v.replace("postgresql://", "postgresql+psycopg2://", 1)
            elif v.startswith("postgresql+psycopg://"):
                return v.replace("postgresql+psycopg://", "postgresql+psycopg2://", 1)
        return v

    @field_validator("ALLOWED_ORIGINS", mode="before")
    def assemble_cors_origins(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",")]
        elif isinstance(v, list):
            return v
        return [
            "http://localhost:3000",
            "http://localhost:8000",
            "https://tajlandia.com",
            "https://tajlandia.pl",
            "https://thai.estate",
            "https://varsoviaestate.com"
        ]

    class Config:
        env_file = ".env"
        extra = "allow"


settings = Settings()


def is_test_environment() -> bool:
    """True when running under pytest or ENVIRONMENT=test/testing."""
    env = (os.environ.get("ENVIRONMENT") or getattr(settings, "ENVIRONMENT", "") or "").lower()
    if env in ("test", "testing"):
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    if "pytest" in sys.modules:
        return True
    return False


def _database_url_is_configured(url: Optional[str]) -> bool:
    u = (url or "").strip()
    if not u:
        return False
    # Reject empty-ish / placeholder-only values
    if u in (_DATABASE_URL_PLACEHOLDER, "postgresql://USER:PASSWORD@127.0.0.1:5432/tajlandia_db"):
        return False
    if "USER:PASSWORD@" in u or "user:password@" in u.lower():
        return False
    if u.rstrip("/").endswith("://") or u in ("postgresql://", "postgresql+psycopg2://"):
        return False
    return True


def assert_database_configured_or_exit() -> None:
    """
    Fail fast at startup when DATABASE_URL is missing in a non-test environment.
    Tests must supply DATABASE_URL via conftest / env — never from a real password default.
    """
    if is_test_environment():
        return
    if _database_url_is_configured(settings.DATABASE_URL):
        return
    raise RuntimeError(
        "DATABASE_URL is not configured. Set DATABASE_URL in the environment or .env file "
        "(do not embed real passwords in code). Refusing to start."
    )


def log_config_warnings() -> None:
    """
    Emit WARNING logs once at startup for risky/off flags. Never logs secret values.
    """
    if not getattr(settings, "ENABLE_AUDIT_LOGGING", False):
        logger.warning("Admin audit logging is OFF")
    if not getattr(settings, "ENABLE_LEAD_SAVING", False):
        logger.warning("Lead saving is OFF")

    redirect = (getattr(settings, "GOOGLE_REDIRECT_URI", None) or "").strip()
    if redirect and "railway.app" in redirect.lower():
        on_cloud_run = bool(os.environ.get("K_SERVICE"))
        base = (getattr(settings, "BASE_WEBSITE_URL", None) or "").lower()
        if on_cloud_run or ("railway.app" not in base and base):
            logger.warning(
                "GOOGLE_REDIRECT_URI still points at a railway.app host while running on "
                "Cloud Run (K_SERVICE set) or a different base URL — update OAuth redirect"
            )

    admin_fs = (getattr(settings, "ADMIN_FIRESTORE_PROJECT_ID", None) or "").strip()
    if not admin_fs:
        logger.warning(
            "ADMIN_FIRESTORE_* is not set, so admin shares the public Firestore project"
        )

    secret = (getattr(settings, "INTERNAL_API_SECRET", None) or "").strip()
    jwt_or_key = (getattr(settings, "SECRET_KEY", None) or "").strip()
    if secret.lower() in _WEAK_SECRETS or len(secret) < 16:
        logger.warning("JWT/secret key (INTERNAL_API_SECRET) is a default or weak value")
    if jwt_or_key and (jwt_or_key.lower() in _WEAK_SECRETS or len(jwt_or_key) < 16):
        logger.warning("JWT/secret key (SECRET_KEY) is a default or weak value")
    if not secret and not jwt_or_key:
        logger.warning("JWT/secret key is a default or weak value")

    try:
        from app.services.report_service import get_report_storage_mode, is_s3_configured, is_persistent_volume_configured
        mode = get_report_storage_mode()
        if mode == "ephemeral" and not is_s3_configured() and not is_persistent_volume_configured():
            logger.warning(
                "Reports are stored on ephemeral disk and will disappear on redeploy"
            )
    except Exception as e:
        logger.debug("Could not evaluate report storage mode at startup: %s", e)


def _csv_setting(name: str) -> list:
    raw = getattr(settings, name, "") or ""
    if isinstance(raw, (list, tuple)):
        return [str(part).strip().lower() for part in raw if str(part).strip()]
    return [part.strip().lower() for part in str(raw).split(",") if part.strip()]


def _instruction_status() -> Dict[str, Any]:
    try:
        from app.services.instruction_store import instruction_config_status
        return instruction_config_status()
    except Exception:
        return {
            "instruction_store": "firestore",
            "firestore_target": getattr(settings, "INSTRUCTIONS_FIRESTORE_TARGET", "user") or "user",
            "cache_ttl_seconds": 60,
            "active_count": {},
            "last_error": None,
        }


def get_admin_config_status() -> Dict[str, Any]:
    """
    Labels/booleans only for admin system health. Never includes secrets or credentialed URLs.
    """
    redirect = (getattr(settings, "GOOGLE_REDIRECT_URI", None) or "").strip()
    redirect_host = ""
    if redirect:
        try:
            redirect_host = urlparse(redirect).hostname or ""
        except Exception:
            redirect_host = ""

    admin_fs = (getattr(settings, "ADMIN_FIRESTORE_PROJECT_ID", None) or "").strip()
    try:
        from app.services.report_service import get_report_storage_mode
        report_storage = get_report_storage_mode()
    except Exception:
        report_storage = "ephemeral"
    # Normalize for API contract: ephemeral | s3 (persistent volumes report as ephemeral=false via label)
    if report_storage == "persistent":
        report_storage_label = "persistent"
    elif report_storage == "s3":
        report_storage_label = "s3"
    else:
        report_storage_label = "ephemeral"

    return {
        "audit_logging": bool(getattr(settings, "ENABLE_AUDIT_LOGGING", False)),
        "lead_saving": bool(getattr(settings, "ENABLE_LEAD_SAVING", False)),
        "firestore_project_shared": not bool(admin_fs),
        "google_redirect_host": redirect_host,
        "database_configured": _database_url_is_configured(settings.DATABASE_URL),
        "report_storage": report_storage_label,
        "ingest_allowed_domains": _csv_setting("INGEST_ALLOWED_DOMAINS"),
        "ingest_blocked_domains": _csv_setting("INGEST_BLOCKED_DOMAINS"),
        "ingest_ignore_robots": bool(getattr(settings, "INGEST_IGNORE_ROBOTS", False)),
        "ingest_max_bytes": int(getattr(settings, "INGEST_MAX_BYTES", 3_000_000) or 3_000_000),
        "ingest_timeout_sec": float(getattr(settings, "INGEST_TIMEOUT_SEC", 10.0) or 10.0),
        "ingest_max_urls_per_hour": int(getattr(settings, "INGEST_MAX_URLS_PER_HOUR", 10) or 10),
        **_instruction_status(),
    }


def get_public_config_status() -> Dict[str, Any]:
    """Non-secret public-chatbot flags for ops / status pages. Never includes secrets."""
    return {
        "chat_logging": bool(getattr(settings, "ENABLE_CHAT_LOGGING", True)),
        "chat_log_mask_pii": bool(getattr(settings, "CHAT_LOG_MASK_PII", True)),
        "lead_saving": bool(getattr(settings, "ENABLE_LEAD_SAVING", True)),
        "fallback_types": ["none", "no_data", "off_topic", "did_you_mean", "error"],
    }
