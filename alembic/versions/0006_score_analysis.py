"""job_scores.analysis: rubric v5's checked technology stack and role classification (JSON)

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-23
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("job_scores") as batch:
        batch.add_column(sa.Column("analysis", sa.Text))


def downgrade() -> None:
    with op.batch_alter_table("job_scores") as batch:
        batch.drop_column("analysis")
