import asyncio
import logging
import os
import threading
from fastapi import FastAPI, Depends, Query, HTTPException, status, Request
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

logger = logging.getLogger(__name__)

# Background purge of expired admin undo snapshots (every 6 hours)
_snapshot_purge_task: Optional[Any] = None
_SNAPSHOT_PURGE_INTERVAL_SEC = 6 * 60 * 60

app = FastAPI(
    title="Tajlandia AI Property Intelligence Platform API",
    description="Multi-tenant backend for Public Customer Chatbot & Internal Admin Agent.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
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
        from app.core.config import assert_database_configured_or_exit, log_config_warnings
        assert_database_configured_or_exit()
        log_config_warnings()
    except RuntimeError:
        raise
    except Exception as e:
        logger.warning("Startup config checks failed (non-fatal if already asserted): %s", e)

    try:
        import sqlalchemy as sa
        from app.db.session import admin_engine, user_engine, Base
        import app.db.models  # noqa

        # Safe DDL synchronization for existing users & properties tables across both database instances
        engines_to_verify = {admin_engine, user_engine}
        schema_ok = True
        for eng in engines_to_verify:
            try:
                Base.metadata.create_all(bind=eng)
                with eng.begin() as conn:
                    conn.execute(sa.text("""
                        ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;
                        ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255);
                        ALTER TABLE users ADD COLUMN IF NOT EXISTS google_email VARCHAR(255);
                        ALTER TABLE users ADD COLUMN IF NOT EXISTS google_refresh_token TEXT;
                        ALTER TABLE users ADD COLUMN IF NOT EXISTS allowed_websites JSONB DEFAULT '[]'::jsonb;
                        ALTER TABLE users ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'active';
                        ALTER TABLE users ADD COLUMN IF NOT EXISTS domain VARCHAR(100) DEFAULT 'tajlandia.com';
                        ALTER TABLE users ADD COLUMN IF NOT EXISTS preferences JSONB DEFAULT '{}'::jsonb;
                        ALTER TABLE properties ADD COLUMN IF NOT EXISTS websites JSONB DEFAULT '[]'::jsonb;
                    """))
            except Exception as eng_e:
                schema_ok = False
                print(f"Startup notice for engine {eng}: {eng_e}")
        if schema_ok:
            print("FastAPI Startup: Verified all admin and user database tables and columns.")
        else:
            print("FastAPI Startup: Database schema was not fully applied.")
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
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register Public Chatbot & Admin Agent Routers
app.include_router(chat_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1")
app.include_router(superadmin_instructions_router, prefix="/api/v1")
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
        img_urls = [img.image_url for img in p.images]
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
                    fs_websites = fs_p.get("websites") or fs_p.get("allowed_websites")
                    if target_websites and fs_websites and not any(w in target_websites for w in fs_websites):
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

    img_urls = [img.image_url for img in p.images]
    return {
        "found": True,
        "property_id": p.property_id,
        "property_name": p.property_name,
        "project_name": p.project_name,
        "property_type": p.property_type,
        "status": p.status,
        "location": {
            "country": p.country,
            "province": p.province,
            "district": p.district,
            "area": p.area,
            "address": p.address,
            "latitude": p.latitude,
            "longitude": p.longitude
        },
        "developer": {
            "name": p.developer_name,
            "description": p.developer_description
        },
        "pricing": {
            "display": p.price_display,
            "numeric_min": float(p.price_numeric_min) if p.price_numeric_min else None,
            "currency": p.currency,
            "note": p.price_note
        },
        "details": {
            "bedrooms": p.bedrooms_display,
            "bathrooms": p.bathrooms,
            "area": p.area_display,
            "units": p.units
        },
        "description": p.description,
        "investment_info": p.investment_information,
        "rental_info": p.rental_information,
        "payment_info": p.payment_information,
        "amenities": p.amenities,
        "features": p.features,
        "highlights": p.highlights,
        "images": img_urls,
        "websites": p.websites or ["tajlandia.com"],
        "detail_page_url": p.detail_page_url
    }
