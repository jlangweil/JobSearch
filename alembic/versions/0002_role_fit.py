"""job_scores.role_fit: leadership/delivery vs hands-on fit

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("job_scores") as batch:
        batch.add_column(sa.Column("role_fit", sa.Integer))


def downgrade() -> None:
    with op.batch_alter_table("job_scores") as batch:
        batch.drop_column("role_fit")
