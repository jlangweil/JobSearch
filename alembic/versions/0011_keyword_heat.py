"""keyword_heat: the keywords the best-scoring postings ask for, rebuilt whenever a job is scored

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "keyword_heat",
        sa.Column("keyword", sa.Text, primary_key=True),   # normalized, e.g. "aws"
        sa.Column("kind", sa.Text, primary_key=True),      # technology | competency
        sa.Column("label", sa.Text, nullable=False),       # as postings spell it most often, e.g. "AWS"
        sa.Column("jobs", sa.Integer, nullable=False),     # top-scoring postings that ask for it
        sa.Column("required", sa.Integer, nullable=False), # ... of which list it as required
        sa.Column("have", sa.Integer, nullable=False),     # ... of which your background covers it
        sa.Column("share", sa.REAL, nullable=False),       # jobs / all top-scoring postings
        sa.Column("updated_at", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("keyword_heat")
