import os
from typing import List, Union, Optional
from pydantic_settings import BaseSettings
from pydantic import field_validator

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
    DATABASE_URL: str = "postgresql://postgres:xyz123%4024@127.0.0.1:5432/tajlandia_db"
    ADMIN_DATABASE_URL: Optional[str] = None
    USER_DATABASE_URL: Optional[str] = None
    
    # AI Configuration — accuracy over speed/cost; harden timeouts so one stuck call cannot hang Cloud Run
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-5.5"
    OPENAI_TEMPERATURE: float = 0.0
    OPENAI_TIMEOUT: float = 25.0  # seconds per request; honest fallback on timeout
    # Reasoning effort for calls WITHOUT tools (answer writing, guardrail checks). Default "medium" on
    # gpt-5.x is what made every reply take ~18 s. "low" keeps answers grounded (data comes from tools).
    OPENAI_REASONING_EFFORT: str = "low"
    OPENAI_MAX_RETRIES: int = 1  # one retry after timeout/transient failure

    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-opus-5-5"
    ANTHROPIC_TEMPERATURE: float = 0.0
    ANTHROPIC_TIMEOUT: float = 600.0  # 10 minutes — same for admin bot

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

    # Persistent property context (reference memory separate from live facts)
    PROPERTY_CONTEXT_RECENT_MESSAGES: int = 20
    PROPERTY_CONTEXT_SUMMARY_CHARS: int = 4000
    # Chat history + property_context always go to Firestore (not Postgres)
    ENABLE_PROPERTY_CONTEXT_PERSISTENCE: bool = True

    # Lead/audit permanent writes (separate from chat memory which is always Firestore when sync is on)
    ENABLE_LEAD_SAVING: bool = False
    ENABLE_CHAT_LOGGING: bool = False  # legacy flag — chat memory uses Firestore via ENABLE_FIRESTORE_SYNC
    ENABLE_AUDIT_LOGGING: bool = False

    # Action Confirmation & Security
    ACTION_EXPIRATION_MINUTES: int = 15
    INTERNAL_API_SECRET: str = "tajlandia-internal-secret-token"
    ADMIN_EMAIL: Optional[str] = None
    ADMIN_PASSWORD: Optional[str] = None
    SESSION_EXPIRE_HOURS: int = 168  # 7 days session validity

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
