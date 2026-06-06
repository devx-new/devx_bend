"""add pgvector embedding and weekly digests

Revision ID: a1b2c3d4e5f6
Revises: e2ecee1d2254
Create Date: 2026-06-06 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'e2ecee1d2254'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Enable pgvector extension (idempotent)
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Add embedding column to feedback_items as vector(384) — idempotent via DO block
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'feedback_items' AND column_name = 'embedding'
            ) THEN
                ALTER TABLE feedback_items ADD COLUMN embedding vector(384);
            END IF;
        END
        $$;
    """)

    # Create ivfflat index for fast cosine similarity search (idempotent)
    # NOTE: index requires at least 1 row to be created; run after seeding data.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_feedback_items_embedding "
        "ON feedback_items USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    )

    # Create weekly_digests table (idempotent)
    op.execute("""
        CREATE TABLE IF NOT EXISTS weekly_digests (
            id          VARCHAR(36)  NOT NULL,
            tenant_id   VARCHAR(36)  NOT NULL,
            report_markdown TEXT     NOT NULL,
            period_start TIMESTAMPTZ NOT NULL,
            period_end   TIMESTAMPTZ NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (id),
            FOREIGN KEY (tenant_id) REFERENCES tenants (id)
        )
    """)

    # Indexes (idempotent)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_weekly_digests_tenant_id "
        "ON weekly_digests (tenant_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_weekly_digests_created_at "
        "ON weekly_digests (created_at)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_weekly_digests_created_at'), table_name='weekly_digests')
    op.drop_index(op.f('ix_weekly_digests_tenant_id'), table_name='weekly_digests')
    op.drop_table('weekly_digests')

    op.execute("DROP INDEX IF EXISTS ix_feedback_items_embedding")
    op.drop_column('feedback_items', 'embedding')
