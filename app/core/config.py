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
    
    # Database
    DATABASE_URL: str = "postgresql://postgres:xyz123%4024@127.0.0.1:5432/tajlandia_db"
    
    # AI Configuration (Model is strictly configurable, not hardcoded)
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-5.5"
    OPENAI_TEMPERATURE: float = 0.2
    
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-opus-5-5"
    ANTHROPIC_TEMPERATURE: float = 0.2

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
    TAJLANDIA_SALES_EMAIL: str = "sales@varsoviaestate.com"

    # Report Generation Storage
    REPORT_STORAGE_DIR: str = "data/reports"

    # Cloud Firestore & Firebase Settings
    FIRESTORE_PROJECT_ID: str = "varsovia-8c135"
    FIRESTORE_DATABASE: str = "(default)"
    FIREBASE_SERVICE_ACCOUNT_KEY: Optional[str] = None
    FIREBASE_CREDENTIALS_PATH: str = "credentials/firebase_service_account.json"
    ENABLE_FIRESTORE_SYNC: bool = True
    
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



    @field_validator("DATABASE_URL", mode="before")
    def assemble_db_url(cls, v: str) -> str:
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
