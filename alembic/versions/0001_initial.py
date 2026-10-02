"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("ats_type", sa.Text),
        sa.Column("board_token", sa.Text),
        sa.Column("active", sa.Integer, server_default="1"),
        sa.Column("notes", sa.Text),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("source_job_id", sa.Text, nullable=False),
        sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id")),
        sa.Column("company_name_raw", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("location_raw", sa.Text),
        sa.Column("remote_type", sa.Text),
        sa.Column("employment_type", sa.Text),
        sa.Column("description_text", sa.Text, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("apply_url", sa.Text),
        sa.Column("comp_min", sa.Integer),
        sa.Column("comp_max", sa.Integer),
        sa.Column("comp_source", sa.Text),
        sa.Column("posted_at", sa.Text),
        sa.Column("first_seen_at", sa.Text, nullable=False),
        sa.Column("last_seen_at", sa.Text, nullable=False),
        sa.Column("seen_count", sa.Integer, server_default="1"),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("embedding", sa.BLOB),
        sa.Column("dedupe_group", sa.Integer),
        sa.Column("status", sa.Text, nullable=False, server_default="discovered"),
        sa.Column("status_reason", sa.Text),
        sa.Column("snooze_until", sa.Text),
        sa.Column("thread_id", sa.Text),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.UniqueConstraint("source", "source_job_id", name="uq_jobs_source_job"),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_dedupe_group", "jobs", ["dedupe_group"])
    op.create_index("ix_jobs_posted_at", "jobs", ["posted_at"])
    op.create_index("ix_jobs_thread_id", "jobs", ["thread_id"])

    op.create_table(
        "job_scores",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("scored_at", sa.Text, nullable=False),
        sa.Column("model", sa.Text),
        sa.Column("rubric_version", sa.Text),
        sa.Column("total", sa.REAL),
        sa.Column("skill_match", sa.Integer),
        sa.Column("domain_fit", sa.Integer),
        sa.Column("seniority_fit", sa.Integer),
        sa.Column("comp_fit", sa.Integer),
        sa.Column("logistics_fit", sa.Integer),
        sa.Column("red_flags", sa.Text),
        sa.Column("gaps", sa.Text),
        sa.Column("evidence", sa.Text),
        sa.Column("rationale", sa.Text),
        sa.Column("ghost_score", sa.Integer),
        sa.Column("low_confidence", sa.Integer, server_default="0"),
    )
    op.create_index("ix_job_scores_job_id", "job_scores", ["job_id"])

    op.create_table(
        "decisions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("total_at_decision", sa.REAL),
        sa.Column("created_at", sa.Text, nullable=False),
    )
    op.create_index("ix_decisions_job_id", "decisions", ["job_id"])

    op.create_table(
        "facts",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("parent_id", sa.Text),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("variants", sa.Text),
        sa.Column("metrics", sa.Text),
        sa.Column("tags", sa.Text),
        sa.Column("org", sa.Text),
        sa.Column("title", sa.Text),
        sa.Column("start_date", sa.Text),
        sa.Column("end_date", sa.Text),
        sa.Column("embedding", sa.BLOB),
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("markdown", sa.Text, nullable=False),
        sa.Column("fact_map", sa.Text),
        sa.Column("fact_ids", sa.Text),
        sa.Column("coverage", sa.Text),
        sa.Column("approved", sa.Integer, server_default="0"),
        sa.Column("file_path", sa.Text),
        sa.Column("created_at", sa.Text, nullable=False),
    )
    op.create_index("ix_documents_job_id", "documents", ["job_id"])

    op.create_table(
        "answers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("question_text", sa.Text, nullable=False),
        sa.Column("embedding", sa.BLOB),
        sa.Column("answer_text", sa.Text, nullable=False),
        sa.Column("kind", sa.Text),
        sa.Column("last_used_at", sa.Text),
        sa.Column("use_count", sa.Integer, server_default="0"),
    )

    op.create_table(
        "applications",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("method", sa.Text, nullable=False),
        sa.Column("submitted_at", sa.Text, nullable=False),
        sa.Column("confirmation_ref", sa.Text),
        sa.Column("document_ids", sa.Text),
        sa.Column("follow_up_at", sa.Text),
        sa.Column("outcome", sa.Text, server_default="none"),
        sa.Column("notes", sa.Text),
    )
    op.create_index("ix_applications_job_id", "applications", ["job_id"])

    op.create_table(
        "runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("thread_id", sa.Text),
        sa.Column("started_at", sa.Text, nullable=False),
        sa.Column("finished_at", sa.Text),
        sa.Column("trigger", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("sources", sa.Text),
        sa.Column("counts", sa.Text),
        sa.Column("errors", sa.Text),
        sa.Column("current_node", sa.Text),
    )

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job_id", sa.Integer),
        sa.Column("purpose", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("prompt_version", sa.Text),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("cost_usd", sa.REAL),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("created_at", sa.Text, nullable=False),
    )

    op.create_table(
        "locks",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("acquired_at", sa.Text, nullable=False),
        sa.Column("pid", sa.Integer, nullable=False),
        sa.Column("note", sa.Text),
    )

    # SPEC 4.7: external-content FTS5 index kept in sync by triggers.
    op.execute(
        "CREATE VIRTUAL TABLE jobs_fts USING fts5("
        "title, company_name_raw, description_text, content='jobs', content_rowid='id')"
    )
    op.execute(
        "CREATE TRIGGER jobs_fts_ai AFTER INSERT ON jobs BEGIN "
        "INSERT INTO jobs_fts(rowid, title, company_name_raw, description_text) "
        "VALUES (new.id, new.title, new.company_name_raw, new.description_text); END"
    )
    op.execute(
        "CREATE TRIGGER jobs_fts_ad AFTER DELETE ON jobs BEGIN "
        "INSERT INTO jobs_fts(jobs_fts, rowid, title, company_name_raw, description_text) "
        "VALUES ('delete', old.id, old.title, old.company_name_raw, old.description_text); END"
    )
    op.execute(
        "CREATE TRIGGER jobs_fts_au AFTER UPDATE OF title, company_name_raw, description_text ON jobs BEGIN "
        "INSERT INTO jobs_fts(jobs_fts, rowid, title, company_name_raw, description_text) "
        "VALUES ('delete', old.id, old.title, old.company_name_raw, old.description_text); "
        "INSERT INTO jobs_fts(rowid, title, company_name_raw, description_text) "
        "VALUES (new.id, new.title, new.company_name_raw, new.description_text); END"
    )


def downgrade() -> None:
    for trig in ("jobs_fts_au", "jobs_fts_ad", "jobs_fts_ai"):
        op.execute(f"DROP TRIGGER IF EXISTS {trig}")
    op.execute("DROP TABLE IF EXISTS jobs_fts")
    for table in (
        "locks", "llm_calls", "runs", "applications", "answers", "documents",
        "facts", "decisions", "job_scores", "jobs", "companies",
    ):
        op.drop_table(table)
