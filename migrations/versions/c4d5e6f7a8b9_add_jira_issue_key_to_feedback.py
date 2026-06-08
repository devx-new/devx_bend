from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, None] = 'b3c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'feedback_items',
        sa.Column('jira_issue_key', sa.String(50), nullable=True, index=True),
    )


def downgrade() -> None:
    op.drop_column('feedback_items', 'jira_issue_key')
