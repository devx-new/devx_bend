"""add widget_keys table

Revision ID: g9c1d2e3f4a5
Revises: 824ef5e766f1
Create Date: 2026-06-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'g9c1d2e3f4a5'
down_revision = '824ef5e766f1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'widget_keys',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('tenant_id', sa.String(36), nullable=False),
        sa.Column('label', sa.String(255), nullable=False),
        sa.Column('key_prefix', sa.String(20), nullable=False),
        sa.Column('key_hash', sa.String(64), nullable=False),
        sa.Column('allowed_origins', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='active'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('key_hash', name='uq_widget_keys_key_hash'),
    )
    op.create_index('ix_widget_keys_tenant_id', 'widget_keys', ['tenant_id'])


def downgrade() -> None:
    op.drop_index('ix_widget_keys_tenant_id', table_name='widget_keys')
    op.drop_table('widget_keys')
