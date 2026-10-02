"""job_scores.industry_fit: code-computed employer industry fit (insurance first)

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-30
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("job_scores") as batch:
        batch.add_column(sa.Column("industry_fit", sa.Integer))


def downgrade() -> None:
    with op.batch_alter_table("job_scores") as batch:
        batch.drop_column("industry_fit")
