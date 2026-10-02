"""job_scores.ideal_fit: how close the posting is to the owner's ideal job (a role in the fact bank), by embedding

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-30
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("job_scores") as batch:
        batch.add_column(sa.Column("ideal_fit", sa.Integer))


def downgrade() -> None:
    with op.batch_alter_table("job_scores") as batch:
        batch.drop_column("ideal_fit")
