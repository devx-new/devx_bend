"""add is_verified to users

Revision ID: h4f2a1b3c9d7
Revises: g9c1d2e3f4a5
Create Date: 2026-07-14 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'h4f2a1b3c9d7'
down_revision = 'g9c1d2e3f4a5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('is_verified', sa.Boolean(), nullable=False, server_default='false'),
    )


def downgrade() -> None:
    op.drop_column('users', 'is_verified')
