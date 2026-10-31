"""part_b_schema

Revision ID: b5930472e618
Revises: a4823916f507
Create Date: 2026-09-23 01:10:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b5930472e618'
down_revision: Union[str, Sequence[str], None] = 'a4823916f507'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns(table: str) -> set:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return set()
    return {col["name"] for col in insp.get_columns(table)}


def _existing_indexes(table: str) -> set:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return set()
    return {idx["name"] for idx in insp.get_indexes(table)}


def upgrade() -> None:
    # Tables are created by the app before Alembic runs, so these columns may already exist.
    pending_cols = _existing_columns("pending_actions")
    if "status" not in pending_cols:
        op.add_column('pending_actions', sa.Column('status', sa.String(length=50), server_default='PENDING', nullable=False))
    if "conversation_id" not in pending_cols:
        op.add_column('pending_actions', sa.Column('conversation_id', sa.String(length=128), nullable=True))
    if "execution_result" not in pending_cols:
        op.add_column('pending_actions', sa.Column('execution_result', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    if "error_message" not in pending_cols:
        op.add_column('pending_actions', sa.Column('error_message', sa.Text(), nullable=True))
    if "confirmed_at" not in pending_cols:
        op.add_column('pending_actions', sa.Column('confirmed_at', sa.DateTime(), nullable=True))
    if "executed_at" not in pending_cols:
        op.add_column('pending_actions', sa.Column('executed_at', sa.DateTime(), nullable=True))
    if "ix_pending_actions_status" not in _existing_indexes("pending_actions"):
        op.create_index(op.f('ix_pending_actions_status'), 'pending_actions', ['status'], unique=False)

    audit_cols = _existing_columns("audit_logs")
    if "conversation_id" not in audit_cols:
        op.add_column('audit_logs', sa.Column('conversation_id', sa.String(length=128), nullable=True))
    if "pending_action_id" not in audit_cols:
        op.add_column('audit_logs', sa.Column('pending_action_id', sa.String(length=128), nullable=True))
    if "request_id" not in audit_cols:
        op.add_column('audit_logs', sa.Column('request_id', sa.String(length=128), nullable=True))
    audit_indexes = _existing_indexes("audit_logs")
    if "ix_audit_logs_conversation_id" not in audit_indexes:
        op.create_index(op.f('ix_audit_logs_conversation_id'), 'audit_logs', ['conversation_id'], unique=False)
    if "ix_audit_logs_pending_action_id" not in audit_indexes:
        op.create_index(op.f('ix_audit_logs_pending_action_id'), 'audit_logs', ['pending_action_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_audit_logs_pending_action_id'), table_name='audit_logs')
    op.drop_index(op.f('ix_audit_logs_conversation_id'), table_name='audit_logs')
    op.drop_column('audit_logs', 'request_id')
    op.drop_column('audit_logs', 'pending_action_id')
    op.drop_column('audit_logs', 'conversation_id')

    op.drop_index(op.f('ix_pending_actions_status'), table_name='pending_actions')
    op.drop_column('pending_actions', 'executed_at')
    op.drop_column('pending_actions', 'confirmed_at')
    op.drop_column('pending_actions', 'error_message')
    op.drop_column('pending_actions', 'execution_result')
    op.drop_column('pending_actions', 'conversation_id')
    op.drop_column('pending_actions', 'status')
