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


def upgrade() -> None:
    # pending_actions extensions
    op.add_column('pending_actions', sa.Column('status', sa.String(length=50), server_default='PENDING', nullable=False))
    op.add_column('pending_actions', sa.Column('conversation_id', sa.String(length=128), nullable=True))
    op.add_column('pending_actions', sa.Column('execution_result', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('pending_actions', sa.Column('error_message', sa.Text(), nullable=True))
    op.add_column('pending_actions', sa.Column('confirmed_at', sa.DateTime(), nullable=True))
    op.add_column('pending_actions', sa.Column('executed_at', sa.DateTime(), nullable=True))
    op.create_index(op.f('ix_pending_actions_status'), 'pending_actions', ['status'], unique=False)

    # audit_logs extensions
    op.add_column('audit_logs', sa.Column('conversation_id', sa.String(length=128), nullable=True))
    op.add_column('audit_logs', sa.Column('pending_action_id', sa.String(length=128), nullable=True))
    op.add_column('audit_logs', sa.Column('request_id', sa.String(length=128), nullable=True))
    op.create_index(op.f('ix_audit_logs_conversation_id'), 'audit_logs', ['conversation_id'], unique=False)
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
