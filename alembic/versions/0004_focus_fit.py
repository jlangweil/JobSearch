"""job_scores.focus_fit / focus_hits: code-computed scoring focus (boost and avoid phrases)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("job_scores") as batch:
        batch.add_column(sa.Column("focus_fit", sa.Integer))
        batch.add_column(sa.Column("focus_hits", sa.Text))


def downgrade() -> None:
    with op.batch_alter_table("job_scores") as batch:
        batch.drop_column("focus_hits")
        batch.drop_column("focus_fit")
