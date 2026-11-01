import asyncio
import logging
import os
import threading
from fastapi import FastAPI, Depends, Query, HTTPException, status, Request, File, UploadFile, Form
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import List, Optional, Any
from app.core.config import settings
from app.db.session import get_db, get_admin_db, get_user_db
from app.services.property_search import PropertyFilterParams, search_properties_safe, get_property_by_id, property_belongs_to_websites
from app.core.tenant import normalize_website_list
from app.db.models import Property
from app.api.v1.chat import router as chat_router
from app.api.v1.admin import router as admin_router
from app.api.v1.auth import router as auth_router
from app.api.v1.superadmin_instructions import router as superadmin_instructions_router
from app.api.v1.chat_logs import router as chat_logs_router
from app.api.v1.drive_ingestion import router as drive_ingestion_router

logger = logging.getLogger(__name__)

# Make the app's own "timing.*" lines visible in Cloud Run logs (they were silently dropped before,
# because nothing set a log level). Set APP_LOG_LEVEL=WARNING to turn them off again.
_app_log = logging.getLogger("app")
_app_log.setLevel(os.getenv("APP_LOG_LEVEL", "INFO").strip().upper() or "INFO")
if not _app_log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    _app_log.addHandler(_h)

# API docs list every route, enabled by default in local/staging (disable with ENABLE_API_DOCS=false in prod)
_DOCS_ON = os.getenv("ENABLE_API_DOCS", "true").strip().lower() in ("1", "true", "yes")
# Background purge of expired admin undo snapshots (every 6 hours)
_snapshot_purge_task: Optional[Any] = None
_SNAPSHOT_PURGE_INTERVAL_SEC = 6 * 60 * 60

app = FastAPI(
    title="Tajlandia AI Property Intelligence Platform API",
    description="Multi-tenant backend for Public Customer Chatbot & Internal Admin Agent.",
    version="1.0.0",
    docs_url="/docs" if _DOCS_ON else None,
    redoc_url="/redoc" if _DOCS_ON else None,
    openapi_url="/openapi.json" if _DOCS_ON else None,
)

# Mount Static Assets & Frontend
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


def _prewarm_caches_background() -> None:
    """Load catalog + name index without blocking startup; never raise."""
    try:
        from app.services.firestore_service import prewarm_property_catalog
        prewarm_property_catalog(target="user")
    except Exception as e:
        logger.warning("Startup catalog pre-warm failed (non-fatal): %s", e)
    try:
        from app.services.property_name_index import prewarm_property_name_index
        prewarm_property_name_index(target="user")
    except Exception as e:
        logger.warning("Startup name-index pre-warm failed (non-fatal): %s", e)


@app.on_event("startup")
def startup_db_init():
    """Ensure all PostgreSQL tables and extensions exist on boot for both admin and user databases."""
    # Fail fast if DATABASE_URL is missing outside tests; then emit config flag warnings (no secrets)
    try:
        from app.core.config import (
            assert_database_configured_or_exit,
            assert_secrets_configured_or_exit,
            log_config_warnings,
        )
        assert_database_configured_or_exit()
        assert_secrets_configured_or_exit()
        log_config_warnings()
    except RuntimeError:
        raise
    except Exception as e:
        logger.warning("Startup config checks failed (non-fatal if already asserted): %s", e)

    try:
        from app.db.schema_sync import ensure_postgres_schema
        from app.db.session import admin_engine, user_engine

        try:
            ensure_postgres_schema((admin_engine, user_engine))
            print("FastAPI Startup: Verified all admin and user database tables and columns.")
        except Exception as eng_e:
            print(f"FastAPI Startup: Database schema was not fully applied: {eng_e}")
    except Exception as e:
        print(f"FastAPI Startup DB Notice: {e}")

    # Non-blocking cache pre-warm (Firestore may be unavailable in some envs)
    try:
        threading.Thread(
            target=_prewarm_caches_background,
            daemon=True,
            name="prewarm-property-caches",
        ).start()
    except Exception as e:
        logger.warning("Could not start cache pre-warm thread: %s", e)


def _run_purge_expired_snapshots_safe() -> int:
    """Never raise — used by startup, loop, and maintenance endpoint internals."""
    try:
        from app.services.admin_batch_service import purge_expired_snapshots
        return int(purge_expired_snapshots() or 0)
    except Exception as e:
        logger.warning("purge_expired_snapshots wrapper error: %s", e)
        return 0


def _run_purge_old_reports_safe() -> int:
    """Never raise — purge reports older than retention (default 7 days)."""
    try:
        from app.services.report_service import purge_old_reports
        return int(purge_old_reports() or 0)
    except Exception as e:
        logger.warning("purge_old_reports wrapper error: %s", e)
        return 0


async def _snapshot_purge_loop() -> None:
    """Background asyncio loop: purge expired admin snapshots + old reports every 6 hours."""
    while True:
        try:
            await asyncio.sleep(_SNAPSHOT_PURGE_INTERVAL_SEC)
            deleted = await asyncio.to_thread(_run_purge_expired_snapshots_safe)
            if deleted:
                logger.info("Background snapshot purge deleted %s document(s)", deleted)
            reports_deleted = await asyncio.to_thread(_run_purge_old_reports_safe)
            if reports_deleted:
                logger.info("Background report purge deleted %s file(s)", reports_deleted)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Must never crash the app
            logger.warning("Background snapshot purge loop error (continuing): %s", e)


@app.on_event("startup")
async def startup_snapshot_purge():
    """
    (a) Purge expired admin undo snapshots once at boot.
    (b) Start a 6-hour background asyncio purge loop.
    Infra should also enable a Firestore TTL policy on field `expire_at` as a second safety net.
    """
    global _snapshot_purge_task
    try:
        deleted = await asyncio.to_thread(_run_purge_expired_snapshots_safe)
        logger.info("Startup snapshot purge deleted %s expired document(s)", deleted)
    except Exception as e:
        logger.warning("Startup snapshot purge failed (non-fatal): %s", e)
    try:
        reports_deleted = await asyncio.to_thread(_run_purge_old_reports_safe)
        if reports_deleted:
            logger.info("Startup report purge deleted %s old report(s)", reports_deleted)
    except Exception as e:
        logger.warning("Startup report purge failed (non-fatal): %s", e)
    try:
        _snapshot_purge_task = asyncio.create_task(
            _snapshot_purge_loop(),
            name="admin-snapshot-purge-loop",
        )
    except Exception as e:
        logger.warning("Could not start snapshot purge loop (non-fatal): %s", e)


@app.on_event("startup")
async def startup_resume_drive_ingestion():
    """Restart Google Drive ingestion jobs that were queued or processing when the server stopped."""
    from app.core.config import is_test_environment
    if is_test_environment() or not bool(getattr(settings, "DRIVE_INGEST_RESUME_ON_STARTUP", True)):
        return
    try:
        from app.services.drive_folder_ingest import resume_interrupted_jobs
        await asyncio.to_thread(resume_interrupted_jobs)
    except Exception as e:
        logger.warning("Drive ingestion resume failed (non-fatal): %s", type(e).__name__)


@app.on_event("shutdown")
async def shutdown_snapshot_purge():
    """Cancel the background snapshot purge loop on app shutdown."""
    global _snapshot_purge_task
    task = _snapshot_purge_task
    _snapshot_purge_task = None
    if task is not None:
        try:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug("Snapshot purge task shutdown: %s", e)
        except Exception as e:
            logger.warning("Error cancelling snapshot purge task: %s", e)


@app.get("/", include_in_schema=False)
def serve_frontend():
    """Serves the main Tajlandia AI modern dark Single Page App."""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Tajlandia AI Backend Active"}

@app.get("/admin-ui", include_in_schema=False)
def serve_admin_frontend():
    """Serves admin view of frontend."""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Tajlandia AI Backend Active"}

# Robust CORS Configuration: allows all frontend ports (Vite 5173, Next 3000, etc.) and deployed domains
# Only our own sites, our Vercel apps and localhost may call the API from a browser.
# Emergency switch: set CORS_ALLOW_ALL=true on Cloud Run to restore the old open behaviour without a deploy.
_CORS_ORIGIN_REGEX = (
    r"https://(www\.)?(varsoviaestate\.com|tajlandia\.com|tajlandia\.pl|thai\.estate)"
    r"|https://ai-chatbot-[a-z0-9-]+\.vercel\.app"
    r"|http://(localhost|127\.0\.0\.1)(:\d+)?"
)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r".*" if os.getenv("CORS_ALLOW_ALL", "").lower() == "true" else _CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register Public Chatbot & Admin Agent Routers
app.include_router(chat_router, prefix="/api/v1")
app.include_router(chat_router, prefix="", include_in_schema=False)
app.include_router(admin_router, prefix="/api/v1")
app.include_router(superadmin_instructions_router, prefix="/api/v1")
app.include_router(chat_logs_router, prefix="/api/v1")
app.include_router(drive_ingestion_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")

# Convenience route aliases for authentication and user management
from app.api.v1.auth import (
    user_login,
    user_logout,
    register_user,
    list_admin_users_with_google_status,
    create_admin_user,
    get_admin_user_details,
    update_admin_user,
    delete_admin_user,
    block_admin_user,
    UserLoginResponse,
    UserLogoutResponse,
    AdminUserDirectoryItem
)
app.post("/login", response_model=UserLoginResponse, tags=["Authentication"], include_in_schema=False)(user_login)
app.post("/logout", response_model=UserLogoutResponse, tags=["Authentication"], include_in_schema=False)(user_logout)
app.post("/api/v1/login", response_model=UserLoginResponse, tags=["Authentication"], include_in_schema=False)(user_login)
app.post("/api/v1/logout", response_model=UserLogoutResponse, tags=["Authentication"], include_in_schema=False)(user_logout)
app.post("/admin/login", response_model=UserLoginResponse, tags=["Authentication"], include_in_schema=False)(user_login)
app.post("/api/v1/admin/login", response_model=UserLoginResponse, tags=["Authentication"], include_in_schema=False)(user_login)
app.post("/api/v1/register", response_model=AdminUserDirectoryItem, status_code=status.HTTP_201_CREATED, tags=["Authentication"], include_in_schema=False)(register_user)
app.post("/register", response_model=AdminUserDirectoryItem, status_code=status.HTTP_201_CREATED, tags=["Authentication"], include_in_schema=False)(register_user)
app.post("/api/admin/auth/register", response_model=AdminUserDirectoryItem, status_code=status.HTTP_201_CREATED, tags=["Authentication"], include_in_schema=True)(register_user)
app.post("/api/admin/auth/login", response_model=UserLoginResponse, tags=["Authentication"], include_in_schema=True)(user_login)
app.post("/admin/auth/register", response_model=AdminUserDirectoryItem, status_code=status.HTTP_201_CREATED, tags=["Authentication"], include_in_schema=False)(register_user)
app.post("/admin/auth/login", response_model=UserLoginResponse, tags=["Authentication"], include_in_schema=False)(user_login)
app.post("/api/auth/register", response_model=AdminUserDirectoryItem, status_code=status.HTTP_201_CREATED, tags=["Authentication"], include_in_schema=False)(register_user)
app.post("/api/auth/login", response_model=UserLoginResponse, tags=["Authentication"], include_in_schema=False)(user_login)

# User management aliases (/api/v1/admin/users and /api/v1/users)
app.get("/api/v1/admin/users", response_model=list[AdminUserDirectoryItem], tags=["Admin Directory"], include_in_schema=False)(list_admin_users_with_google_status)
app.get("/api/v1/users", response_model=list[AdminUserDirectoryItem], tags=["Admin Directory"], include_in_schema=False)(list_admin_users_with_google_status)
app.post("/api/v1/admin/users", response_model=AdminUserDirectoryItem, status_code=status.HTTP_201_CREATED, tags=["Admin Directory"], include_in_schema=False)(create_admin_user)
app.post("/api/v1/users", response_model=AdminUserDirectoryItem, status_code=status.HTTP_201_CREATED, tags=["Admin Directory"], include_in_schema=False)(create_admin_user)
app.get("/api/v1/admin/users/{user_id}", response_model=AdminUserDirectoryItem, tags=["Admin Directory"], include_in_schema=False)(get_admin_user_details)
app.get("/api/v1/users/{user_id}", response_model=AdminUserDirectoryItem, tags=["Admin Directory"], include_in_schema=False)(get_admin_user_details)
app.patch("/api/v1/admin/users/{user_id}", response_model=AdminUserDirectoryItem, tags=["Admin Directory"], include_in_schema=False)(update_admin_user)
app.put("/api/v1/admin/users/{user_id}", response_model=AdminUserDirectoryItem, tags=["Admin Directory"], include_in_schema=False)(update_admin_user)
app.patch("/api/v1/users/{user_id}", response_model=AdminUserDirectoryItem, tags=["Admin Directory"], include_in_schema=False)(update_admin_user)
app.put("/api/v1/users/{user_id}", response_model=AdminUserDirectoryItem, tags=["Admin Directory"], include_in_schema=False)(update_admin_user)
app.delete("/api/v1/admin/users/{user_id}", tags=["Admin Directory"], include_in_schema=False)(delete_admin_user)
app.delete("/api/v1/users/{user_id}", tags=["Admin Directory"], include_in_schema=False)(delete_admin_user)
app.post("/api/v1/admin/users/{user_id}/block", response_model=AdminUserDirectoryItem, tags=["Admin Directory"], include_in_schema=False)(block_admin_user)
app.post("/api/v1/users/{user_id}/block", response_model=AdminUserDirectoryItem, tags=["Admin Directory"], include_in_schema=False)(block_admin_user)

@app.get("/health", tags=["System"])
def health_check(
    admin_db: Session = Depends(get_admin_db),
    user_db: Session = Depends(get_user_db)
):
    """System health check, PostgreSQL (admin and user instances), and Firestore connection test."""
    try:
        admin_count = admin_db.query(Property).count()
        user_count = user_db.query(Property).count()
        fs_health = {"connected": False}
        if (
            getattr(settings, "ENABLE_FIRESTORE_SYNC", False)
            or settings.get_admin_enable_firestore_sync()
            or settings.get_user_enable_firestore_sync()
        ):
            try:
                from app.services.firestore_service import check_firestore_health
                fs_health = check_firestore_health()
            except Exception as fe:
                fs_health = {"connected": False, "error": str(fe)}

        return {
            "status": "healthy",
            "environment": settings.ENVIRONMENT,
            "database": "connected",
            "admin_database": {"connected": True, "properties_count": admin_count},
            "user_database": {"connected": True, "properties_count": user_count},
            "total_properties": user_count or admin_count,
            "firestore": fs_health
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database connection failed: {e}"
        )

@app.get("/api/v1/branding", tags=["System"])
def get_branding(site_origin: Optional[str] = Query(None, description="Optional tenant domain")):
    """
    Returns active tenant branding, bot name, and contact details.
    Adapts dynamically based on environment settings or client site_origin
    (tajlandia.com, thai.estate, varsoviaestate.com).
    """
    from app.core.tenant import resolve_tenant
    tenant = resolve_tenant(site_origin)
    return {
        "site_origin": tenant.site_origin,
        "brand_name": tenant.brand_name,
        "bot_name": tenant.bot_name,
        "brand_title": tenant.brand_title,
        "brand_subtitle": tenant.brand_subtitle,
        "domain": tenant.domain,
        "advisor_email": tenant.advisor_email,
        "website_url": tenant.website_url
    }

@app.get("/api/v1/properties", tags=["Properties"])
def list_properties(
    request: Request,
    location: Optional[str] = Query(None, description="Location search query (e.g. Phuket, Bang Tao, Samui)"),
    property_type: Optional[str] = Query(None, description="Villa or Condominium"),
    min_price: Optional[float] = Query(None, ge=0),
    max_price: Optional[float] = Query(None, ge=0),
    bedrooms: Optional[int] = Query(None, ge=0),
    has_pool: Optional[bool] = Query(None),
    beach_access: Optional[bool] = Query(None),
    status: Optional[str] = Query(None),
    websites: Optional[str] = Query(None, description="Comma-separated website domains or single domain"),
    site_origin: Optional[str] = Query(None, description="Tenant site origin domain"),
    limit: Optional[int] = Query(50, ge=1, le=500, description="Max properties to return"),
    offset: Optional[int] = Query(0, ge=0, description="Pagination offset"),
    db: Session = Depends(get_user_db)
):
    """Direct property filtering endpoint using safe SQLAlchemy query builder with website scoping."""
    raw_sites = (
        websites
        or request.query_params.get("selected_websites")
        or request.query_params.get("sites")
        or site_origin
        or request.headers.get("x-selected-websites")
        or request.headers.get("x-selected-sites")
        or request.headers.get("x-site-origin")
    )
    target_websites = normalize_website_list(raw_sites) if raw_sites else None

    params = PropertyFilterParams(
        location=location,
        property_type=property_type,
        min_price=min_price,
        max_price=max_price,
        bedrooms=bedrooms,
        has_pool=has_pool,
        beach_access=beach_access,
        status=status,
        websites=target_websites,
        limit=limit or 50,
        offset=offset or 0
    )
    results = search_properties_safe(db, params)
    
    output = []
    for p in results:
        img_urls = [getattr(img, "image_url", img) for img in (getattr(p, "images", None) or [])]
        output.append({
            "property_id": p.property_id,
            "property_name": p.property_name,
            "property_type": p.property_type,
            "status": p.status,
            "area": p.area,
            "district": p.district,
            "price_display": p.price_display,
            "bedrooms": p.bedrooms_display,
            "area_sqm": p.area_display,
            "has_pool": p.has_pool,
            "beach_access": p.beach_access,
            "images": img_urls,
            "websites": p.websites or ["tajlandia.com"],
            "detail_page_url": p.detail_page_url
        })
    return {"count": len(output), "properties": output}

@app.get("/api/v1/properties/{property_id}", tags=["Properties"])
def get_property(
    property_id: str,
    request: Request,
    websites: Optional[str] = Query(None, description="Optional website filter"),
    site_origin: Optional[str] = Query(None, description="Tenant site origin domain"),
    db: Session = Depends(get_user_db)
):
    """Lookup property by exact Property ID with optional website scope check."""
    raw_sites = (
        websites
        or request.query_params.get("selected_websites")
        or request.query_params.get("sites")
        or site_origin
        or request.headers.get("x-selected-websites")
        or request.headers.get("x-selected-sites")
        or request.headers.get("x-site-origin")
    )
    target_websites = normalize_website_list(raw_sites) if raw_sites else None

    p = get_property_by_id(db, property_id)
    if not p:
        if getattr(settings, "ENABLE_FIRESTORE_SYNC", False):
            try:
                from app.services.firestore_service import get_property_by_id_from_firestore
                fs_p = get_property_by_id_from_firestore(property_id)
                if fs_p:
                    if target_websites and not property_belongs_to_websites(fs_p, target_websites):
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Property with ID '{property_id}' not found in website scope {target_websites}."
                        )
                    return {
                        "found": True,
                        "property_id": fs_p["property_id"],
                        "property_name": fs_p["name"],
                        "property_type": fs_p["property_type"],
                        "status": fs_p["status"],
                        "location": {
                            "country": fs_p.get("province") or "Thailand",
                            "province": fs_p.get("province"),
                            "district": fs_p.get("district"),
                            "address": fs_p.get("address"),
                            "latitude": fs_p.get("latitude"),
                            "longitude": fs_p.get("longitude")
                        },
                        "pricing": {
                            "display": fs_p.get("price"),
                            "numeric_min": fs_p.get("price_numeric_min"),
                            "currency": fs_p.get("currency", "THB")
                        },
                        "details": {
                            "bedrooms": fs_p.get("bedrooms"),
                            "area": fs_p.get("area")
                        },
                        "amenities": fs_p.get("raw_firestore_data", {}).get("amenities", []),
                        "images": fs_p.get("images", []),
                        "websites": fs_websites or ["tajlandia.com"],
                        "detail_page_url": fs_p.get("detail_page_url")
                    }
            except HTTPException:
                raise
            except Exception:
                pass
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property with ID '{property_id}' not found."
        )

    if target_websites and not property_belongs_to_websites(p, target_websites):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property with ID '{property_id}' does not belong to selected website(s) {target_websites}."
        )

    img_urls = [getattr(img, "image_url", img) for img in (getattr(p, "images", None) or [])]
    return {
        "found": True,
        "property_id": getattr(p, "property_id", property_id),
        "property_name": getattr(p, "property_name", None),
        "project_name": getattr(p, "project_name", None),
        "project_name_pl": getattr(p, "project_name_pl", None),
        "project_name_en": getattr(p, "project_name_en", None) or getattr(p, "property_name", None),
        "slug_pl": getattr(p, "slug_pl", None),
        "slug_en": getattr(p, "slug_en", None),
        "subtitle_pl": getattr(p, "subtitle_pl", None),
        "subtitle_en": getattr(p, "subtitle_en", None),
        "property_type": getattr(p, "property_type", None),
        "status": getattr(p, "status", None),
        "completion_status": getattr(p, "completion_status", None) or getattr(p, "status", None),
        "completion_date": getattr(p, "completion_date", None),
        "location": {
            "country": getattr(p, "country", "Thailand"),
            "city": getattr(p, "city", None) or getattr(p, "province", None),
            "province": getattr(p, "province", None),
            "district": getattr(p, "district", None),
            "area": getattr(p, "area", None),
            "address": getattr(p, "address", None),
            "address_en": getattr(p, "address_en", None) or getattr(p, "address", None),
            "address_pl": getattr(p, "address_pl", None),
            "search_location": getattr(p, "search_location", None),
            "neighborhood_lifestyle": getattr(p, "neighborhood_lifestyle", None),
            "latitude": getattr(p, "latitude", None),
            "longitude": getattr(p, "longitude", None)
        },
        "developer": {
            "name": getattr(p, "developer_name", None),
            "description": getattr(p, "developer_description", None)
        },
        "pricing": {
            "display": getattr(p, "price_display", None),
            "price_from": float(p.price_numeric_min) if getattr(p, "price_numeric_min", None) is not None else None,
            "price_to": float(p.price_numeric_max) if getattr(p, "price_numeric_max", None) is not None else None,
            "numeric_min": float(p.price_numeric_min) if getattr(p, "price_numeric_min", None) is not None else None,
            "numeric_max": float(p.price_numeric_max) if getattr(p, "price_numeric_max", None) is not None else None,
            "currency": getattr(p, "currency", "THB"),
            "expected_roi": getattr(p, "expected_roi", None),
            "rental_yield": getattr(p, "rental_yield", None),
            "note": getattr(p, "price_note", None)
        },
        "details": {
            "bedrooms": getattr(p, "bedrooms_display", None),
            "bathrooms": getattr(p, "bathrooms", None),
            "area": getattr(p, "area_display", None),
            "units": getattr(p, "units", None),
            "total_units": getattr(p, "units", None),
            "available_units": getattr(p, "available_units", None),
            "area_from_sqm": float(p.area_sqm_min) if getattr(p, "area_sqm_min", None) is not None else None,
            "area_to_sqm": float(p.area_sqm_max) if getattr(p, "area_sqm_max", None) is not None else None,
        },
        "descriptions": {
            "short_description_pl": getattr(p, "short_description_pl", None),
            "full_description_pl": getattr(p, "description_pl", None),
            "short_description_en": getattr(p, "short_description_en", None),
            "full_description_en": getattr(p, "description_en", None) or getattr(p, "description", None),
        },
        "description": getattr(p, "description", None),
        "investment_info": getattr(p, "investment_information", None),
        "rental_info": getattr(p, "rental_information", None),
        "payment_info": getattr(p, "payment_information", None),
        "amenities": getattr(p, "amenities", []),
        "features": getattr(p, "features", []),
        "highlights": getattr(p, "highlights", []),
        "tags": getattr(p, "tags", []),
        "media": img_urls,
        "images": img_urls,
        "video_url": getattr(p, "video_url", None),
        "layouts": getattr(p, "layouts", []),
        "seo": {
            "seo_title_pl": getattr(p, "seo_title_pl", None),
            "seo_title_en": getattr(p, "seo_title_en", None),
            "seo_description_pl": getattr(p, "seo_description_pl", None),
            "seo_description_en": getattr(p, "seo_description_en", None),
        },
        "publish": {
            "active": getattr(p, "is_public", True),
            "featured": getattr(p, "is_featured", False),
            "top_list": getattr(p, "is_top_list", False),
        },
        "websites": getattr(p, "websites", ["tajlandia.com"]),
        "detail_page_url": getattr(p, "detail_page_url", None)
    }

# Root route aliases for Section 13 public endpoints
app.get("/branding", tags=["System"], include_in_schema=False)(get_branding)
app.get("/properties", tags=["Properties"], include_in_schema=False)(list_properties)
app.get("/properties/{property_id}", tags=["Properties"], include_in_schema=False)(get_property)
app.get("/api/v1/health", tags=["System"], include_in_schema=False)(health_check)


# Dedicated Direct Media Proxy Endpoint for Frontend Websites
@app.get("/api/v1/media/{full_path:path}", tags=["Media"])
@app.get("/media/{full_path:path}", tags=["Media"], include_in_schema=False)
def serve_public_media_asset(full_path: str):
    import os
    import urllib.parse
    import httpx
    from fastapi.responses import Response, FileResponse

    # Public, unauthenticated: only image files under the media folder (or image objects in the bucket).
    # Previously "../" segments or an absolute path made this read any file on the server.
    clean = (full_path or "").replace("\\", "/")
    parts = [p for p in clean.split("/") if p not in ("", ".")]
    if (
        not parts
        or any(p == ".." or ":" in p for p in parts)
        or clean.startswith("/")
        or not parts[-1].lower().endswith((".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif"))
    ):
        raise HTTPException(status_code=404, detail="Media asset not found")
    full_path = "/".join(parts)

    local_base = os.path.realpath(os.path.join(getattr(settings, "REPORT_STORAGE_DIR", "data/reports"), "media"))
    local_file = os.path.realpath(os.path.join(local_base, *parts))
    if local_file.startswith(local_base + os.sep) and os.path.isfile(local_file):
        lower = local_file.lower()
        media_type = (
            "image/webp" if lower.endswith(".webp")
            else "image/jpeg" if lower.endswith((".jpg", ".jpeg"))
            else "image/gif" if lower.endswith(".gif")
            else "image/avif" if lower.endswith(".avif")
            else "image/png"
        )
        return FileResponse(local_file, media_type=media_type)

    try:
        from app.services.cloud_media_storage import cloud_storage
        token = cloud_storage._get_access_token()
        bucket = cloud_storage.bucket_name
        quoted_name = urllib.parse.quote(full_path, safe="")
        
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
            
        gcs_url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quoted_name}?alt=media"
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(gcs_url, headers=headers)
            content_type = resp.headers.get("content-type", "image/jpeg")
            if resp.status_code == 200 and content_type.lower().startswith("image/"):
                return Response(
                    content=resp.content,
                    media_type=content_type,
                    headers={
                        "Cache-Control": "public, max-age=86400, stale-while-revalidate=604800",
                        "Access-Control-Allow-Origin": "*"
                    }
                )
    except Exception as e:
        logger.error(f"Error proxying media asset {full_path}: {e}")

    raise HTTPException(status_code=404, detail="Media asset not found")



# =====================================================================
# Enterprise Real Estate Document & URL Ingestion Endpoints
# =====================================================================
class IngestUrlRequest(BaseModel):
    url: str = Field(..., min_length=1, description="URL of the property listing to ingest")
    property_id: Optional[str] = None


_INGEST_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_INGEST_ALLOWED_EXTENSIONS = (".pdf", ".docx", ".xlsx", ".xls", ".csv", ".pptx", ".txt", ".html", ".htm",
                              ".jpg", ".jpeg", ".png", ".webp")


def _ingest_staff_dependency():
    # These routes write listings straight to Postgres + Firestore, so they need the same role and
    # permission as the other import paths (they were previously open to anonymous callers).
    from app.core.auth import require_admin_or_super_admin_with_sites
    return require_admin_or_super_admin_with_sites


def _check_ingest_target(property_id: Optional[str], staff: Any, db: Session) -> None:
    """An ingest that names an existing listing overwrites it: only allowed within the caller's websites."""
    if not property_id:
        return
    from app.core.permissions import PROPERTIES_IMPORT, has_permission
    from app.services.firestore_service import get_raw_property_document

    if not has_permission(staff.role, PROPERTIES_IMPORT):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Your role may not import property data.")
    pid = property_id.strip()
    try:
        existing = get_raw_property_document(pid, target="admin")
    except Exception:
        existing = None
    if existing is None and db is not None:
        existing = db.query(Property).filter(Property.property_id == pid).first()
    if existing is not None and not property_belongs_to_websites(existing, staff.selected_websites):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Property '{pid}' is not part of your selected website(s).",
        )


@app.post("/api/v1/properties/ingest-document", tags=["Properties"])
async def ingest_property_document_endpoint(
    file: UploadFile = File(...),
    property_id: Optional[str] = Form(None),
    staff: Any = Depends(_ingest_staff_dependency()),
    db: Session = Depends(get_db),
):
    from app.core.permissions import PROPERTIES_IMPORT, has_permission
    from app.services.document_ingestion_pipeline import process_and_ingest_property_document

    if not has_permission(staff.role, PROPERTIES_IMPORT):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Your role may not import property data.")
    filename = file.filename or "uploaded_document.pdf"
    if not filename.lower().endswith(_INGEST_ALLOWED_EXTENSIONS):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported file type.")
    _check_ingest_target(property_id, staff, db)
    content = await file.read(_INGEST_MAX_UPLOAD_BYTES + 1)
    if len(content) > _INGEST_MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File is larger than 25 MB.")
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The uploaded file is empty.")
    try:
        result = process_and_ingest_property_document(
            content_bytes=content,
            filename=filename,
            property_id_override=property_id,
            save_to_database=True
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    try:
        from app.services.firestore_service import invalidate_property_caches
        invalidate_property_caches()
    except Exception:
        pass
    return {"status": "success", "message": "Property ingested and synchronized successfully", "data": result}


@app.post("/api/v1/properties/ingest-url", tags=["Properties"])
def ingest_property_url_endpoint(
    req: IngestUrlRequest,
    staff: Any = Depends(_ingest_staff_dependency()),
    db: Session = Depends(get_db),
):
    from app.core.permissions import PROPERTIES_IMPORT, has_permission
    from app.services.document_ingestion_pipeline import process_and_ingest_property_url

    if not has_permission(staff.role, PROPERTIES_IMPORT):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Your role may not import property data.")
    if not req.url or not req.url.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="URL cannot be empty.")
    _check_ingest_target(req.property_id, staff, db)
    try:
        result = process_and_ingest_property_url(
            url=req.url,
            property_id_override=req.property_id,
            save_to_database=True
        )
    except ValueError as exc:
        # SSRF / domain policy refusals and unreadable pages are the caller's input, not a server error.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    try:
        from app.services.firestore_service import invalidate_property_caches
        invalidate_property_caches()
    except Exception:
        pass
    return {"status": "success", "message": "Property ingested from URL successfully", "data": result}
