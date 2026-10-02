"""auto-apply (beta): per-site credentials, run log, and the questions a run could not answer

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "apply_credentials",
        sa.Column("id", sa.Integer, primary_key=True),
        # the ATS host these belong to ("nyl.wd1.myworkdayjobs.com"), or "*" for the account reused everywhere
        sa.Column("host", sa.Text, nullable=False, unique=True),
        sa.Column("ats_type", sa.Text, nullable=False, server_default="workday"),
        sa.Column("username", sa.Text, nullable=False),
        sa.Column("password_enc", sa.Text),          # DPAPI ciphertext; never a plaintext password
        sa.Column("notes", sa.Text),
        sa.Column("last_used_at", sa.Text),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
    )
    op.create_table(
        "apply_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("host", sa.Text),
        sa.Column("status", sa.Text, nullable=False),      # running | needs_input | review | submitted | failed
        sa.Column("stage", sa.Text),                       # the form step it reached
        sa.Column("submitted", sa.Integer, nullable=False, server_default="0"),
        sa.Column("filled", sa.Integer, nullable=False, server_default="0"),
        sa.Column("unknown", sa.Integer, nullable=False, server_default="0"),
        sa.Column("events", sa.Text),                      # JSON: [{at, stage, kind, label, detail}]
        sa.Column("screenshots", sa.Text),                 # JSON: [path, ...]
        sa.Column("error", sa.Text),
        sa.Column("started_at", sa.Text, nullable=False),
        sa.Column("finished_at", sa.Text),
    )
    op.create_index("ix_apply_runs_job", "apply_runs", ["job_id"])
    op.create_table(
        "apply_questions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("apply_runs.id")),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id")),
        sa.Column("host", sa.Text),
        sa.Column("stage", sa.Text),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("norm_label", sa.Text, nullable=False),  # matched against so the same question is asked once
        sa.Column("kind", sa.Text),                        # text | select | radio | checkbox | date | file
        sa.Column("options", sa.Text),                     # JSON list for select/radio
        sa.Column("answer", sa.Text),
        sa.Column("save_to_profile", sa.Integer, nullable=False, server_default="1"),
        sa.Column("answered_at", sa.Text),
        sa.Column("created_at", sa.Text, nullable=False),
    )
    op.create_index("ix_apply_questions_norm", "apply_questions", ["norm_label"])


def downgrade() -> None:
    op.drop_index("ix_apply_questions_norm", table_name="apply_questions")
    op.drop_table("apply_questions")
    op.drop_index("ix_apply_runs_job", table_name="apply_runs")
    op.drop_table("apply_runs")
    op.drop_table("apply_credentials")
