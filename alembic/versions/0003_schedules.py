"""schedules managed from the UI; app_state key/value; runs.schedule_id

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schedules",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("enabled", sa.Integer, nullable=False, server_default="0"),
        sa.Column("mode", sa.Text, nullable=False, server_default="fetch"),  # fetch | full | stored
        sa.Column("when_kind", sa.Text, nullable=False, server_default="days"),  # days | interval | cron
        sa.Column("days", sa.Text),  # json ["mon", ...]
        sa.Column("times", sa.Text),  # json ["07:00", ...]
        sa.Column("every_hours", sa.Integer),
        sa.Column("window_start", sa.Integer),
        sa.Column("window_end", sa.Integer),
        sa.Column("cron", sa.Text),
        sa.Column("timezone", sa.Text, nullable=False),
        sa.Column("sources", sa.Text),  # json
        sa.Column("industries", sa.Text),  # json, resolved to companies when the schedule fires
        sa.Column("companies", sa.Text),  # json slugs
        sa.Column("max_to_score", sa.Integer),
        sa.Column("if_busy", sa.Text, nullable=False, server_default="wait"),  # wait | skip
        sa.Column("catch_up", sa.Integer, nullable=False, server_default="1"),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.Column("enabled_at", sa.Text),
        sa.Column("last_fired_at", sa.Text),
        sa.Column("last_run_id", sa.Integer),
        sa.Column("last_result", sa.Text),
    )
    op.create_table(
        "app_state",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text),
        sa.Column("updated_at", sa.Text, nullable=False),
    )
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("schedule_id", sa.Integer))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("schedule_id")
    op.drop_table("app_state")
    op.drop_table("schedules")
