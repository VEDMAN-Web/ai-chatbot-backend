import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Text, Integer, Float, Boolean, 
    DateTime, ForeignKey, Numeric, Index
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.types import UserDefinedType
from app.db.session import Base
from app.core.config import settings

class PGVector(UserDefinedType):
    """
    Native PostgreSQL vector type when extension is active,
    with clean fallback for schema generation.
    """
    def __init__(self, dim=1536):
        self.dim = dim

    def get_col_spec(self, **kw):
        return f"vector({self.dim})"


class Property(Base):
    __tablename__ = "properties"

    property_id = Column(String(64), primary_key=True, index=True)
    project_id = Column(String(64), nullable=True, index=True)
    property_name = Column(String(255), nullable=False, index=True)
    project_name = Column(String(255), nullable=True)
    property_type = Column(String(64), nullable=True, index=True)  # Villa, Condominium
    status = Column(String(64), nullable=True, index=True)         # Under construction, Pre-sale, Ready to move in

    # Location
    country = Column(String(100), default="Thailand")
    province = Column(String(100), nullable=True, index=True)      # Phuket, Surat Thani
    district = Column(String(100), nullable=True, index=True)      # Bang Tao, Layan, Karon, Maenam
    subdistrict = Column(String(100), nullable=True)
    area = Column(String(100), nullable=True, index=True)          # Phuket, Samui, Koh Phangan
    address = Column(String(500), nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)

    # Developer
    developer_name = Column(String(255), nullable=True)
    developer_description = Column(Text, nullable=True)

    # Pricing
    price_display = Column(String(100), nullable=True)             # e.g., "from 8.5 million THB"
    price_numeric_min = Column(Numeric(14, 2), nullable=True, index=True)
    price_numeric_max = Column(Numeric(14, 2), nullable=True)
    currency = Column(String(10), default="THB")
    price_note = Column(Text, nullable=True)

    # Details
    bedrooms_display = Column(String(50), nullable=True)           # e.g., "2–3"
    bedrooms_min = Column(Integer, nullable=True, index=True)
    bedrooms_max = Column(Integer, nullable=True, index=True)
    bathrooms = Column(Integer, nullable=True)
    area_display = Column(String(50), nullable=True)               # e.g., "0–167 sqm"
    area_sqm_min = Column(Numeric(8, 2), nullable=True, index=True)
    area_sqm_max = Column(Numeric(8, 2), nullable=True, index=True)
    units = Column(Integer, nullable=True)
    total_floors = Column(Integer, nullable=True)

    # Description & Content
    description = Column(Text, nullable=True)
    investment_information = Column(Text, nullable=True)
    rental_information = Column(Text, nullable=True)
    payment_information = Column(Text, nullable=True)
    ownership_type = Column(String(100), nullable=True)

    # Tags & Filters
    has_pool = Column(Boolean, default=False, index=True)
    beach_access = Column(Boolean, default=False, index=True)
    sea_view = Column(Boolean, default=False, index=True)

    # JSONB Flexible structures
    amenities = Column(JSONB, default=list)
    features = Column(JSONB, default=list)
    facilities = Column(JSONB, default=list)
    highlights = Column(JSONB, default=list)
    nearby_places = Column(JSONB, default=list)
    raw_metadata = Column(JSONB, default=dict)
    websites = Column(JSONB, default=list, nullable=True)  # List of canonical sites: varsoviaestate.com, tajlandia.com, thai.estate

    detail_page_url = Column(String(500), nullable=True)
    is_public = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    images = relationship("PropertyImage", back_populates="property", cascade="all, delete-orphan")
    specs = relationship("PropertySpec", back_populates="property", uselist=False, cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_prop_search_composite", "property_type", "area", "status"),
        Index("ix_prop_amenities_gin", "amenities", postgresql_using="gin"),
        Index("ix_prop_features_gin", "features", postgresql_using="gin"),
    )


class PropertyImage(Base):
    __tablename__ = "property_images"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    property_id = Column(String(64), ForeignKey("properties.property_id", ondelete="CASCADE"), nullable=False, index=True)
    image_url = Column(String(1024), nullable=False)
    image_host = Column(String(100), default="tajlandia.com")
    alt_text = Column(String(500), nullable=True)
    image_type = Column(String(50), default="exterior")  # exterior, interior, floor_plan, aerial
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    property = relationship("Property", back_populates="images")


class PropertySpec(Base):
    __tablename__ = "property_specs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    property_id = Column(String(64), ForeignKey("properties.property_id", ondelete="CASCADE"), unique=True, nullable=False)
    
    roof_material = Column(String(255), nullable=True)
    roof_colour = Column(String(100), nullable=True)
    flooring = Column(String(255), nullable=True)
    walls = Column(String(255), nullable=True)
    doors_windows = Column(String(255), nullable=True)
    exterior_materials = Column(String(500), nullable=True)
    interior_materials = Column(String(500), nullable=True)
    architectural_style = Column(String(100), nullable=True)

    construction_details = Column(JSONB, default=dict)
    internal_notes = Column(Text, nullable=True)  # Strictly internal for Part B (Claude)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    property = relationship("Property", back_populates="specs")


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=True)
    role = Column(String(50), default="public", index=True)  # public, employee, super_admin
    domain = Column(String(100), default="tajlandia.com", index=True)
    allowed_websites = Column(JSONB, default=list, nullable=True)  # Specific sites assigned by Super Admin: varsoviaestate.com, tajlandia.com, thai.estate
    password_hash = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    status = Column(String(50), default="active", nullable=True)  # 'active', 'blocked', 'deactivated'
    google_email = Column(String(255), nullable=True, index=True)
    google_refresh_token = Column(Text, nullable=True)
    preferences = Column(JSONB, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)

    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String(255), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="sessions")


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(String(128), index=True, nullable=False)
    site_origin = Column(String(100), default="tajlandia.com")  # tajlandia.com, tajlandia.pl, thai.estate, varsoviaestate.com, admin
    agent_type = Column(String(50), default="public")          # public (GPT), admin (Claude)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    message_history = Column(JSONB, default=list)
    preferences = Column(JSONB, default=dict)
    lead_score = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    lead = relationship("Lead", back_populates="conversation", uselist=False)


class Lead(Base):
    __tablename__ = "leads"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, unique=True)
    contact_name = Column(String(255), nullable=True)
    contact_email = Column(String(255), nullable=True, index=True)
    contact_phone = Column(String(50), nullable=True)
    preferred_location = Column(String(100), nullable=True)
    preferred_type = Column(String(50), nullable=True)
    budget_min = Column(Numeric(14, 2), nullable=True)
    budget_max = Column(Numeric(14, 2), nullable=True)
    lead_score = Column(Float, default=0.0)
    buying_signals = Column(JSONB, default=list)
    status = Column(String(50), default="new", index=True)  # new, contacted, qualified
    created_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("Conversation", back_populates="lead")


class PendingAction(Base):
    __tablename__ = "pending_actions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    confirmation_token = Column(String(128), unique=True, nullable=False, index=True)
    staff_user_id = Column(String(128), nullable=False, index=True)
    tool_name = Column(String(100), nullable=False, index=True)
    action_payload = Column(JSONB, nullable=False)
    status = Column(String(50), default="PENDING", nullable=False, index=True)  # PENDING, CONFIRMED, EXECUTED, REJECTED, EXPIRED, FAILED
    conversation_id = Column(String(128), nullable=True, index=True)
    execution_result = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    confirmed_at = Column(DateTime, nullable=True)
    executed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    staff_user_id = Column(String(128), nullable=True, index=True)
    tool_name = Column(String(100), nullable=False, index=True)
    tool_arguments = Column(JSONB, default=dict)
    proposed_payload = Column(JSONB, default=dict)
    confirmed_by_staff = Column(Boolean, default=False)
    execution_result = Column(Text, nullable=True)
    status = Column(String(50), default="SUCCESS", index=True)  # SUCCESS, FAILED, CANCELLED, REJECTED, PENDING
    error_message = Column(Text, nullable=True)
    conversation_id = Column(String(128), nullable=True, index=True)
    pending_action_id = Column(String(128), nullable=True, index=True)
    request_id = Column(String(128), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
