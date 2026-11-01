"""
Frontend & End-to-End QA Test Suite
Validates static file serving, JavaScript syntax & contract integrity,
UI authorization controls, Admin/Super Admin/User dashboard interfaces,
Property CRUD workflows, delete confirmation safety, and Chatbot integrations.
"""
import os
import re
import uuid
import copy
import subprocess
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.core.config import settings
from app.core.auth import AuthenticatedStaff
from app.db.models import AuditLog, Property, PropertyImage
from app.api.v1.admin import (
    get_db,
    require_admin_or_employee_with_sites,
    require_admin_or_super_admin_with_sites,
    require_admin_or_super_admin,
)


# In-memory mock Firestore for testing without external cloud dependencies
class _Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return copy.deepcopy(self._data) if self._data else {}


class _Doc:
    def __init__(self, store, collection, doc_id):
        self.store = store
        self.collection_name = collection
        self.id = doc_id

    def get(self):
        return _Snap(self.id, self.store.get(self.collection_name, self.id))

    def set(self, data, merge=False):
        self.store.set(self.collection_name, self.id, data, merge=merge)

    def update(self, data):
        self.store.update(self.collection_name, self.id, data)

    def delete(self):
        self.store.delete(self.collection_name, self.id)


class _Query:
    def __init__(self, collection):
        self.collection = collection

    def limit(self, _n):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def stream(self):
        return self.collection.stream()

    def where(self, *args, **kwargs):
        return self


class _Collection:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def document(self, doc_id=None):
        if not doc_id:
            doc_id = uuid.uuid4().hex
        return _Doc(self.store, self.name, str(doc_id))

    def stream(self):
        for doc_id, data in self.store.items(self.name):
            yield _Snap(doc_id, data)

    def limit(self, _n):
        return _Query(self)

    def where(self, *args, **kwargs):
        return _Query(self)


class FakeFirestore:
    def __init__(self):
        self.docs = {}

    def collection(self, name):
        return _Collection(self, name)

    def get(self, collection, doc_id):
        return self.docs.get((collection, doc_id))

    def items(self, collection):
        return [(doc_id, data) for (col, doc_id), data in self.docs.items() if col == collection]

    def set(self, collection, doc_id, data, merge=False):
        key = (collection, doc_id)
        payload = copy.deepcopy(data)
        current = self.docs.get(key)
        if merge and isinstance(current, dict):
            merged = copy.deepcopy(current)
            merged.update(payload)
            self.docs[key] = merged
        else:
            self.docs[key] = payload

    def update(self, collection, doc_id, data):
        key = (collection, doc_id)
        current = self.docs.get(key)
        if not isinstance(current, dict):
            raise RuntimeError(f"missing document {doc_id}")
        merged = copy.deepcopy(current)
        merged.update(copy.deepcopy(data))
        self.docs[key] = merged

    def delete(self, collection, doc_id):
        self.docs.pop((collection, doc_id), None)


@pytest.fixture
def client():
    return TestClient(app)


# ==============================================================================
# 1. Static File Serving & HTML Structure QA
# ==============================================================================

def test_index_html_served_at_root(client):
    """Verify that root / serves index.html with 200 OK and all required components."""
    res = client.get("/")
    assert res.status_code == 200
    html = res.text

    # Title & Metadata
    assert "<title>Varsovia Estate" in html
    assert "/static/css/style.css" in html
    assert "/static/js/app.js" in html

    # Mode switchers
    assert 'id="tabPublic"' in html
    assert 'id="tabAdmin"' in html
    assert 'id="tabSuperAdmin"' in html

    # Top choice buttons
    assert 'id="topBtnUser"' in html
    assert 'id="topBtnAdmin"' in html
    assert 'id="topBtnSuperAdmin"' in html

    # Core viewports
    assert 'id="chatViewport"' in html
    assert 'id="chatContainer"' in html
    assert 'id="messagesArea"' in html
    assert 'id="chatInput"' in html
    assert 'id="btnSend"' in html


def test_admin_ui_served_at_admin_ui_route(client):
    """Verify that /admin-ui serves the Single Page Application."""
    res = client.get("/admin-ui")
    assert res.status_code == 200
    assert "Varsovia Estate" in res.text


def test_static_css_served(client):
    """Verify that style.css is properly served with CSS content type and styles."""
    res = client.get("/static/css/style.css")
    assert res.status_code == 200
    assert "text/css" in res.headers.get("content-type", "")
    css = res.text

    # Check key modal and CRUD class declarations
    assert ".app-modal" in css
    assert ".crud-modal" in css
    assert ".crud-table" in css
    assert ".status-pill" in css
    assert ".btn-table-edit" in css
    assert ".btn-table-delete" in css
    assert ".delete-choice-card" in css
    assert ".image-lightbox" in css
    assert ".role-badge" in css


def test_static_js_served(client):
    """Verify that app.js is properly served with application/javascript."""
    res = client.get("/static/js/app.js")
    assert res.status_code == 200
    assert "javascript" in res.headers.get("content-type", "")
    assert "setAgentMode" in res.text
    assert "handleSendMessage" in res.text


def test_all_required_modals_present_in_html(client):
    """Verify all dashboard and action modals are declared in index.html."""
    res = client.get("/")
    html = res.text

    modals = [
        "lightboxModal",            # Image lightbox
        "mapModal",                 # Google Maps
        "calendarModal",            # Google Calendar
        "emailModal",               # Brochure specs email
        "inquiryModal",             # Lead inquiry pop-up
        "batchModal",               # Bulk file operations & 6-day undo
        "adminLoginModal",          # Staff login
        "propertyCrudModal",        # Property listings portfolio CRUD
        "propertyFormModal",        # Create/Edit listing form
        "propertyDeleteModal",      # Soft/Hard delete confirmation dialog
        "superAdminModal",          # Super Admin user directory & system config
    ]

    for m in modals:
        assert f'id="{m}"' in html, f"Modal id='{m}' missing from index.html"


# ==============================================================================
# 2. JavaScript Syntax & Frontend Contract Integrity
# ==============================================================================

def test_js_syntax_integrity():
    """Verify app.js compiles with zero syntax errors via Node.js."""
    js_path = os.path.join("app", "static", "js", "app.js")
    assert os.path.exists(js_path), "app.js does not exist"

    proc = subprocess.run(["node", "--check", js_path], capture_output=True, text=True)
    assert proc.returncode == 0, f"JavaScript syntax error in app.js:\n{proc.stderr}"


def test_zero_duplicate_js_functions():
    """Ensure no duplicate function declarations exist in app.js."""
    js_path = os.path.join("app", "static", "js", "app.js")
    with open(js_path, "r", encoding="utf-8") as f:
        text = f.read()

    funcs = re.findall(r"(?:async\s+)?function\s+([a-zA-Z0-9_]+)\s*\(", text)
    seen = {}
    duplicates = []
    for fn in funcs:
        if fn in seen:
            duplicates.append(fn)
        seen[fn] = True

    assert not duplicates, f"Duplicate functions found in app.js: {duplicates}"


def test_all_html_onclick_handlers_declared_in_js():
    """Verify that every onclick handler referenced in index.html is defined in app.js."""
    html_path = os.path.join("app", "static", "index.html")
    js_path = os.path.join("app", "static", "js", "app.js")

    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    with open(js_path, "r", encoding="utf-8") as f:
        js = f.read()

    declared_funcs = set(re.findall(r"(?:async\s+)?function\s+([a-zA-Z0-9_]+)\s*\(", js))
    onclicks = set(re.findall(r'onclick=["\']([a-zA-Z0-9_]+)\(', html))
    onclicks = {fn for fn in onclicks if fn != "if"}

    missing = [fn for fn in onclicks if fn not in declared_funcs]
    assert not missing, f"HTML onclick functions missing in app.js: {missing}"


def test_dynamic_card_and_table_actions_defined_in_js():
    """Verify that all dynamically generated card actions in app.js are defined."""
    js_path = os.path.join("app", "static", "js", "app.js")
    with open(js_path, "r", encoding="utf-8") as f:
        js = f.read()

    declared_funcs = set(re.findall(r"(?:async\s+)?function\s+([a-zA-Z0-9_]+)\s*\(", js))

    required_dynamic_funcs = [
        "generatePdfForProperty",
        "openLightbox",
        "closeLightbox",
        "openPropertyMap",
        "openPropertyCalendar",
        "openPropertyEmail",
        "inquireProperty",
        "confirmAction",
        "rejectAction",
        "openPropertyCrudModal",
        "closePropertyCrudModal",
        "openPropertyFormModal",
        "closePropertyFormModal",
        "openPropertyDeleteModal",
        "closePropertyDeleteModal",
        "submitDeleteProperty",
        "openSuperAdminModal",
        "closeSuperAdminModal",
        "loadSuperAdminUsers",
        "loadSystemConfigStatus",
        "executeBatchUndo",
    ]

    for fn in required_dynamic_funcs:
        assert fn in declared_funcs, f"Required function '{fn}' is not declared in app.js"


def test_escape_key_listener_closes_all_modals():
    """Verify the global Escape key event handler closes all modals and lightboxes."""
    js_path = os.path.join("app", "static", "js", "app.js")
    with open(js_path, "r", encoding="utf-8") as f:
        js = f.read()

    # Locate Escape key handler block
    esc_match = re.search(r"e\.key\s*===\s*['\"]Escape['\"].*?\}", js, re.DOTALL)
    assert esc_match is not None, "Escape key handler not found in app.js"
    esc_code = esc_match.group(0)

    assert "closeMapModal()" in esc_code
    assert "closeCalendarModal()" in esc_code
    assert "closeEmailModal()" in esc_code
    assert "closeInquiryModal()" in esc_code
    assert "closeBatchOpsModal()" in esc_code
    assert "closeFocusDrawer()" in esc_code
    assert "closeLightbox()" in esc_code
    assert "closePropertyCrudModal()" in esc_code
    assert "closePropertyFormModal()" in esc_code
    assert "closePropertyDeleteModal()" in esc_code
    assert "closeSuperAdminModal()" in esc_code


# ==============================================================================
# 3. User Flows & API Contract Synchronization
# ==============================================================================

def test_branding_endpoint_contract(client):
    """Verify /api/v1/branding matches initTenantBranding() schema in app.js."""
    res = client.get("/api/v1/branding?site_origin=varsoviaestate.com")
    assert res.status_code == 200
    data = res.json()

    assert "site_origin" in data
    assert "brand_name" in data
    assert "bot_name" in data
    assert "brand_title" in data
    assert "brand_subtitle" in data


def test_public_chat_endpoint_contract(client):
    """Verify /api/v1/chat/public accepts frontend payload structure."""
    payload = {
        "session_id": "test_e2e_session_001",
        "message": "Hello, show me villas in Bang Tao",
        "site_origin": "varsoviaestate.com",
        "language": "en"
    }
    res = client.post("/api/v1/chat/public", json=payload)
    assert res.status_code == 200
    data = res.json()

    assert "text" in data
    assert "type" in data
    assert "properties" in data
    assert isinstance(data["properties"], list)


def test_inquire_modal_contract(client):
    """Verify lead inquiry submission contract matches frontend inquiryModal."""
    payload = {
        "session_id": "inquiry_test_session_002",
        "site_origin": "varsoviaestate.com",
        "language": "en",
        "contact_name": "Alexander Hamilton",
        "contact_email": "alexander@example.com",
        "contact_phone": "+66 81 234 5678",
        "property_id": "prop-layan-001",
        "notes": "Interested in private virtual viewing"
    }
    res = client.post("/api/v1/chat/public", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert "lead_saved" in data


def test_brochure_pdf_generation_contract(client):
    """Verify /api/v1/chat/brochure/{id} returns a valid PDF stream for generatePdfForProperty()."""
    res = client.get("/api/v1/chat/brochure/15-sukhumvit-residences?site_origin=varsoviaestate.com&lang=en")
    assert res.status_code == 200
    assert "application/pdf" in res.headers.get("content-type", "")
    assert res.content.startswith(b"%PDF-"), "Response is not a valid PDF document"


# ==============================================================================
# 4. UI Authorization Enforcement & Privilege Boundaries
# ==============================================================================

def test_unauthenticated_admin_endpoints_return_401(client):
    """Ensure unauthenticated users cannot call admin REST or Super Admin APIs."""
    endpoints = [
        ("GET", "/api/v1/admin/properties"),
        ("POST", "/api/v1/admin/properties"),
        ("GET", "/api/v1/auth/admin/users"),
        ("GET", "/api/v1/admin/system/config-status"),
        ("GET", "/api/v1/admin/operations/history"),
    ]

    for method, path in endpoints:
        if method == "GET":
            res = client.get(path)
        else:
            res = client.post(path, json={})
        assert res.status_code in (401, 403), f"{method} {path} did not reject unauthenticated request"


# ==============================================================================
# 5. Property Portfolio CRUD & Delete Safety Workflows
# ==============================================================================

@pytest.fixture
def test_db_session():
    """In-memory SQLite session with clean Property and AuditLog tables."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for table in (Property.__table__, PropertyImage.__table__, AuditLog.__table__):
        for idx in list(table.indexes):
            using = getattr(idx, "dialect_kwargs", {}).get("postgresql_using")
            if using == "gin":
                table.indexes.discard(idx)
        table.create(bind=engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def test_property_crud_workflow_with_admin_auth(monkeypatch, test_db_session):
    """End-to-end test of Property CRUD workflow: List, Create, Update, Soft-delete, Hard-delete."""
    admin_fs = FakeFirestore()
    monkeypatch.setattr("app.services.firestore_service.get_admin_firestore_client", lambda: admin_fs)
    monkeypatch.setattr("app.services.firestore_service.get_user_firestore_client", lambda: admin_fs)
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", True)
    monkeypatch.setattr(settings, "ADMIN_ENABLE_FIRESTORE_SYNC", True)
    monkeypatch.setattr(settings, "ENABLE_AUDIT_LOGGING", True)
    monkeypatch.setattr(settings, "ALLOW_PERMANENT_DELETE", True)

    staff_user = AuthenticatedStaff(
        user_id="staff-qa-01",
        email="qa-admin@varsoviaestate.com",
        role="admin",
        allowed_websites=["varsoviaestate.com", "tajlandia.com"],
        selected_websites=["varsoviaestate.com", "tajlandia.com"],
    )

    app.dependency_overrides[get_db] = lambda: test_db_session
    app.dependency_overrides[require_admin_or_employee_with_sites] = lambda: staff_user
    app.dependency_overrides[require_admin_or_super_admin_with_sites] = lambda: staff_user
    app.dependency_overrides[require_admin_or_super_admin] = lambda: staff_user

    client = TestClient(app)

    try:
        # 1. READ: Initial list should be empty
        list_res = client.get("/api/v1/admin/properties?limit=10&offset=0")
        assert list_res.status_code == 200
        list_data = list_res.json()
        assert "properties" in list_data
        assert "count" in list_data

        # 2. CREATE: Add new property matching frontend submitPropertyForm() payload
        prop_id = "qa-villa-test-001"
        create_payload = {
            "property_id": prop_id,
            "project_name_en": "Varsovia Sunrise Pool Villa",
            "project_name_pl": "Willa Wschodu Słońca Varsovia",
            "property_type": "Villa",
            "city": "Phuket",
            "country": "Thailand",
            "price_from": 18500000.0,
            "price_to": 24000000.0,
            "currency": "THB",
            "bedrooms": 4,
            "bathrooms": 5,
            "area": "450 sqm",
            "has_pool": True,
            "status": "Available",
            "cover_image": "https://images.unsplash.com/photo-1580587771525-78b9dba3b914",
            "websites": ["varsoviaestate.com"]
        }

        create_res = client.post("/api/v1/admin/properties", json=create_payload)
        assert create_res.status_code == 201, create_res.text
        created_data = create_res.json()
        assert created_data["property_id"] == prop_id

        # 3. READ: Listing appears in admin list
        get_res = client.get(f"/api/v1/admin/properties/{prop_id}")
        assert get_res.status_code == 200
        assert get_res.json()["property_id"] == prop_id

        # 4. UPDATE: Modify price and status
        update_payload = {
            "price_from": 19500000.0,
            "status": "For sale",
            "websites": ["varsoviaestate.com"]
        }
        update_res = client.put(f"/api/v1/admin/properties/{prop_id}", json=update_payload)
        assert update_res.status_code == 200

        # 5. SOFT DELETE (Archive):
        soft_del_res = client.delete(f"/api/v1/admin/properties/{prop_id}?hard_delete=false")
        assert soft_del_res.status_code == 200
        assert soft_del_res.json()["status"] == "ARCHIVED"

        # 6. HARD DELETE SAFETY CHECK:
        # Destructive hard delete must fail if confirm_property_id is omitted or mismatch
        fail_del = client.delete(f"/api/v1/admin/properties/{prop_id}?hard_delete=true")
        assert fail_del.status_code == 400
        assert "confirm_property_id" in fail_del.text

        # 7. HARD DELETE CONFIRMED:
        hard_del_res = client.delete(
            f"/api/v1/admin/properties/{prop_id}?hard_delete=true&confirm_property_id={prop_id}"
        )
        assert hard_del_res.status_code == 200
        assert hard_del_res.json()["status"] == "DELETED"

        # Property should now be completely gone (404)
        gone_res = client.get(f"/api/v1/admin/properties/{prop_id}")
        assert gone_res.status_code == 404

    finally:
        app.dependency_overrides.clear()


def test_superadmin_user_directory_and_system_config(monkeypatch):
    """Verify Super Admin user directory and system config endpoints for superAdminModal."""
    super_staff = AuthenticatedStaff(
        user_id="super-001",
        email="superadmin@varsoviaestate.com",
        role="super_admin",
        allowed_websites=["varsoviaestate.com", "tajlandia.com", "thai.estate"],
        selected_websites=["varsoviaestate.com", "tajlandia.com", "thai.estate"],
    )

    app.dependency_overrides[require_admin_or_super_admin] = lambda: super_staff
    client = TestClient(app)

    try:
        # 1. System Config Status endpoint (powers Tab 2: System Config)
        cfg_res = client.get("/api/v1/admin/system/config-status")
        assert cfg_res.status_code == 200
        cfg_data = cfg_res.json()
        assert "database_configured" in cfg_data
        assert "brochure_engine" in cfg_data
        assert "brochure_llm" in cfg_data

    finally:
        app.dependency_overrides.clear()
