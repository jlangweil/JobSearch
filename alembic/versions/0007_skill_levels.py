"""skill_levels: the owner's own 1-10 rating of each skill, used by scoring

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_levels",
        sa.Column("skill", sa.Text, primary_key=True),  # lowercase key, e.g. "aws"
        sa.Column("label", sa.Text, nullable=False),     # as shown, e.g. "AWS"
        sa.Column("level", sa.Integer),                  # 1-10; NULL = not rated
        sa.Column("custom", sa.Integer, nullable=False, server_default="0"),  # 1 = added by hand, not from facts
        sa.Column("updated_at", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("skill_levels")
