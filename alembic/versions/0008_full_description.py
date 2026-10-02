"""jobs.full_description: 1 once a snippet-only posting (Adzuna) has its full text, fetched or pasted

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("full_description", sa.Integer, nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("full_description")
