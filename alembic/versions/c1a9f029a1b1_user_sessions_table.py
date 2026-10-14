"""user_sessions_table

Revision ID: c1a9f029a1b1
Revises: b5930472e618
Create Date: 2026-10-07 20:25:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c1a9f029a1b1'
down_revision: Union[str, Sequence[str], None] = 'b5930472e618'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add is_active column to users table if not exists
    bind = op.get_bind()
    insp = sa.inspect(bind)
    user_cols = [c['name'] for c in insp.get_columns('users')]
    if 'is_active' not in user_cols:
        op.add_column('users', sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False))
    if 'password_hash' not in user_cols:
        op.add_column('users', sa.Column('password_hash', sa.String(length=255), nullable=True))
    if 'google_email' not in user_cols:
        op.add_column('users', sa.Column('google_email', sa.String(length=255), nullable=True))
        op.create_index('ix_users_google_email', 'users', ['google_email'])
    if 'google_refresh_token' not in user_cols:
        op.add_column('users', sa.Column('google_refresh_token', sa.Text(), nullable=True))

    # 2. Create user_sessions table if not exists
    if not insp.has_table('user_sessions'):
        op.create_table(
            'user_sessions',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('token', sa.String(length=255), unique=True, nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False)
        )
        op.create_index('ix_user_sessions_user_id', 'user_sessions', ['user_id'])
        op.create_index('ix_user_sessions_token', 'user_sessions', ['token'])
        op.create_index('ix_user_sessions_expires_at', 'user_sessions', ['expires_at'])


def downgrade() -> None:
    op.drop_table('user_sessions')
    op.drop_column('users', 'is_active')
