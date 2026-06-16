"""add clickup_task_id to feedback

Revision ID: f7a8b9c0d1e2
Revises: d5e6f7a8b9c0
Create Date: 2026-06-16 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'f7a8b9c0d1e2'
down_revision = 'd5e6f7a8b9c0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('feedback_items', sa.Column('clickup_task_id', sa.String(50), nullable=True))
    op.create_index('ix_feedback_items_clickup_task_id', 'feedback_items', ['clickup_task_id'])


def downgrade() -> None:
    op.drop_index('ix_feedback_items_clickup_task_id', table_name='feedback_items')
    op.drop_column('feedback_items', 'clickup_task_id')
