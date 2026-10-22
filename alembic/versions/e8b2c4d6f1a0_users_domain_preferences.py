"""users_domain_preferences

Revision ID: e8b2c4d6f1a0
Revises: c1a9f029a1b1
Create Date: 2026-10-17 18:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e8b2c4d6f1a0"
down_revision: Union[str, Sequence[str], None] = "c1a9f029a1b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("users"):
        return
    user_cols = {c["name"] for c in insp.get_columns("users")}
    if "domain" not in user_cols:
        op.add_column(
            "users",
            sa.Column("domain", sa.String(length=100), server_default="tajlandia.com", nullable=True),
        )
        op.create_index("ix_users_domain", "users", ["domain"], unique=False)
    if "preferences" not in user_cols:
        op.add_column(
            "users",
            sa.Column(
                "preferences",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=True,
            ),
        )
    if "allowed_websites" not in user_cols:
        op.add_column(
            "users",
            sa.Column(
                "allowed_websites",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'[]'::jsonb"),
                nullable=True,
            ),
        )
    if "status" not in user_cols:
        op.add_column(
            "users",
            sa.Column("status", sa.String(length=50), server_default="active", nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("users"):
        return
    user_cols = {c["name"] for c in insp.get_columns("users")}
    if "domain" in user_cols:
        op.drop_index("ix_users_domain", table_name="users")
        op.drop_column("users", "domain")
    if "preferences" in user_cols:
        op.drop_column("users", "preferences")
