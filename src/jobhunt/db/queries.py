"""ALL SQL lives here. Functions take an Engine, keep write transactions short and single-purpose,
and return plain dicts (never ORM objects)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from jobhunt.db.engine import iso_shift, utcnow_iso

# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _rows(result) -> list[dict[str, Any]]:  # noqa: ANN001
    return [dict(r) for r in result.mappings().all()]


def _one(result) -> dict[str, Any] | None:  # noqa: ANN001
    r = result.mappings().first()
    return dict(r) if r else None


def _in_clause(prefix: str, values: Sequence[Any]) -> tuple[str, dict[str, Any]]:
    params = {f"{prefix}{i}": v for i, v in enumerate(values)}
    return ", ".join(f":{k}" for k in params) or "NULL", params


# ---------------------------------------------------------------------------------------------
# status lifecycle (SPEC 4.6)
# ---------------------------------------------------------------------------------------------

TRANSITIONS: dict[str, set[str]] = {
    # discovered -> shortlisted / applied and scored -> applied: [Amendment] a job you add or pick yourself can be
    # tailored without scoring, and one you already applied to elsewhere can go straight to Applications.
    # [Amendment] -> closed from discovered/scored/snoozed: the posting link was checked before tailoring and the
    # board no longer lists it. closed -> shortlisted: "tailor anyway" (the check was wrong, or it reopened).
    "discovered": {"filtered_out", "duplicate", "scored", "shortlisted", "applied", "closed"},
    # [Amendment] filtered_out -> discovered: the filters changed (e.g. commute towns or accepted states), so a
    # knocked-out posting is reconsidered. Settings -> Re-check filtered jobs.
    "filtered_out": {"discovered"},
    # [Amendment] scored -> filtered_out: the filters tightened (Settings -> Re-check filtered jobs applies today's
    # rules both ways).
    "scored": {"shortlisted", "rejected", "snoozed", "applied", "closed", "filtered_out"},
    "snoozed": {"scored", "closed"},
    "shortlisted": {"in_workflow"},
    "in_workflow": {"ready", "abandoned"},
    # ready -> in_workflow is required by the approve_and_apply `back_to_resume` edge (SPEC 7.2).
    "ready": {"applied", "in_workflow"},
    "applied": {"responded", "closed"},
    "responded": {"closed"},
    "closed": {"shortlisted"},
}


class InvalidTransition(ValueError):
    pass


def check_transition(current: str, new: str) -> None:
    if new not in TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"invalid job status transition {current!r} -> {new!r}")


def set_job_status(engine: Engine, job_id: int, new_status: str, reason: str | None = None,
                   snooze_until: str | None = None) -> None:
    with engine.begin() as c:
        row = _one(c.execute(text("SELECT status FROM jobs WHERE id = :id"), {"id": job_id}))
        if row is None:
            raise KeyError(f"job {job_id} not found")
        check_transition(row["status"], new_status)
        res = c.execute(
            text(
                "UPDATE jobs SET status = :new, status_reason = :reason, snooze_until = :snooze, "
                "updated_at = :now WHERE id = :id AND status = :old"
            ),
            {"new": new_status, "reason": reason, "snooze": snooze_until, "now": utcnow_iso(),
             "id": job_id, "old": row["status"]},
        )
        if res.rowcount != 1:
            raise InvalidTransition(f"job {job_id} changed status concurrently")


def wake_snoozed(engine: Engine, now: str | None = None) -> int:
    with engine.begin() as c:
        res = c.execute(
            text(
                "UPDATE jobs SET status = 'scored', snooze_until = NULL, updated_at = :now "
                "WHERE status = 'snoozed' AND snooze_until IS NOT NULL AND snooze_until <= :now"
            ),
            {"now": now or utcnow_iso()},
        )
        return res.rowcount


# ---------------------------------------------------------------------------------------------
# companies
# ---------------------------------------------------------------------------------------------


def upsert_company(engine: Engine, *, name: str, slug: str, ats_type: str, board_token: str | None,
                   notes: str | None = None, active: int = 1) -> int:
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO companies (name, slug, ats_type, board_token, active, notes) "
                "VALUES (:name, :slug, :ats, :token, :active, :notes) "
                "ON CONFLICT(slug) DO UPDATE SET name = excluded.name, ats_type = excluded.ats_type, "
                "board_token = excluded.board_token, active = excluded.active, notes = excluded.notes"
            ),
            {"name": name, "slug": slug, "ats": ats_type, "token": board_token, "active": active,
             "notes": notes},
        )
        return c.execute(text("SELECT id FROM companies WHERE slug = :slug"), {"slug": slug}).scalar_one()


def deactivate_companies_except(engine: Engine, slugs: Iterable[str]) -> int:
    clause, params = _in_clause("s", list(slugs))
    with engine.begin() as c:
        return c.execute(text(f"UPDATE companies SET active = 0 WHERE slug NOT IN ({clause})"), params).rowcount


def list_companies(engine: Engine, ats_type: str | None = None, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM companies WHERE 1=1"
    params: dict[str, Any] = {}
    if ats_type:
        sql += " AND ats_type = :ats"
        params["ats"] = ats_type
    if active_only:
        sql += " AND active = 1"
    with engine.connect() as c:
        return _rows(c.execute(text(sql + " ORDER BY name"), params))


def company_id_by_slug(engine: Engine, slug: str) -> int | None:
    with engine.connect() as c:
        return c.execute(text("SELECT id FROM companies WHERE slug = :s"), {"s": slug}).scalar()


# ---------------------------------------------------------------------------------------------
# jobs: ingest / normalize
# ---------------------------------------------------------------------------------------------

_JOB_CONTENT_COLS = (
    "company_name_raw", "title", "location_raw", "remote_type", "employment_type", "description_text",
    "url", "apply_url", "comp_min", "comp_max", "comp_source", "posted_at", "content_hash",
)


def job_exists(engine: Engine, source: str, source_job_id: str) -> bool:
    """Known *and* nothing more to download: a job re-opened after a filter change has lost its description, so it
    is reported as unknown and the adapter fetches the posting again."""
    with engine.connect() as c:
        row = c.execute(
            text("SELECT status, COALESCE(description_text, '') AS body FROM jobs "
                 "WHERE source = :s AND source_job_id = :i"),
            {"s": source, "i": source_job_id},
        ).first()
    if row is None:
        return False
    return not (row[0] == "discovered" and not row[1].strip())


def upsert_job(engine: Engine, rec: dict[str, Any], *, sighting_only: bool = False,
               listing_only: bool = False) -> tuple[str, int | None]:
    """Upsert one job on (source, source_job_id). Returns (outcome, job_id) where outcome is
    'new' | 'changed' | 'seen' | 'unknown_sighting'."""
    now = utcnow_iso()
    with engine.begin() as c:
        row = _one(
            c.execute(
                text("SELECT id, content_hash, full_description FROM jobs WHERE source = :s AND source_job_id = :i"),
                {"s": rec["source"], "i": rec["source_job_id"]},
            )
        )
        # the full text (fetched or pasted) of a snippet-only posting is not overwritten by the next snippet
        keep_full = bool(row and row["full_description"] and rec.get("description_truncated"))
        if row is None:
            if sighting_only:
                return "unknown_sighting", None
            cols = ("source", "source_job_id", *_JOB_CONTENT_COLS, "first_seen_at", "last_seen_at",
                    "seen_count", "status", "updated_at")
            values = {k: rec.get(k) for k in cols}
            values.update(first_seen_at=now, last_seen_at=now, seen_count=1, status="discovered", updated_at=now)
            res = c.execute(
                text(f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join(':' + k for k in cols)})"),
                values,
            )
            return "new", int(res.lastrowid)
        if not sighting_only and not listing_only and not keep_full and row["content_hash"] != rec["content_hash"]:
            sets = ", ".join(f"{k} = :{k}" for k in _JOB_CONTENT_COLS)
            params = {k: rec.get(k) for k in _JOB_CONTENT_COLS}
            params.update(id=row["id"], now=now)
            c.execute(
                text(
                    f"UPDATE jobs SET {sets}, embedding = NULL, full_description = 0, last_seen_at = :now, "
                    "seen_count = seen_count + 1, updated_at = :now WHERE id = :id"
                ),
                params,
            )
            return "changed", row["id"]
        c.execute(
            text("UPDATE jobs SET last_seen_at = :now, seen_count = seen_count + 1 WHERE id = :id"),
            {"now": now, "id": row["id"]},
        )
        return "seen", row["id"]


def get_job(engine: Engine, job_id: int) -> dict | None:
    with engine.connect() as c:
        return _one(c.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id}))


def strip_job_content(engine: Engine, ids: Sequence[int], chunk: int = 500) -> int:
    """Drop description and embedding (kept: title, company, location, URLs, status, reason, content_hash)."""
    done = 0
    for i in range(0, len(ids), chunk):
        clause, params = _in_clause("j", list(ids[i: i + chunk]))
        with engine.begin() as c:
            done += c.execute(text(f"UPDATE jobs SET description_text = '', embedding = NULL WHERE id IN ({clause}) "
                                   "AND (description_text != '' OR embedding IS NOT NULL)"), params).rowcount
    return done


def compact(engine: Engine) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.exec_driver_sql("VACUUM")


def job_ids_with_status(engine: Engine, status: str) -> list[int]:
    with engine.connect() as c:
        return [int(r[0]) for r in c.execute(text("SELECT id FROM jobs WHERE status = :s ORDER BY id"), {"s": status})]


def get_jobs(engine: Engine, ids: Sequence[int], status: str | None = None,
             columns: str = "*") -> list[dict]:
    if not ids:
        return []
    clause, params = _in_clause("j", list(ids))
    sql = f"SELECT {columns} FROM jobs WHERE id IN ({clause})"
    if status:
        sql += " AND status = :status"
        params["status"] = status
    with engine.connect() as c:
        return _rows(c.execute(text(sql + " ORDER BY id"), params))


def jobs_missing_embedding(engine: Engine, ids: Sequence[int]) -> list[dict]:
    if not ids:
        return []
    clause, params = _in_clause("j", list(ids))
    with engine.connect() as c:
        return _rows(
            c.execute(
                text(
                    "SELECT id, title, company_name_raw, description_text FROM jobs "
                    f"WHERE id IN ({clause}) AND embedding IS NULL ORDER BY id"
                ),
                params,
            )
        )


def update_job_normalized(engine: Engine, job_id: int, *, company_id: int | None, embedding: bytes | None) -> None:
    with engine.begin() as c:
        c.execute(
            text("UPDATE jobs SET company_id = :cid, embedding = :emb, updated_at = :now WHERE id = :id"),
            {"cid": company_id, "emb": embedding, "now": utcnow_iso(), "id": job_id},
        )


def set_job_thread(engine: Engine, job_id: int, thread_id: str) -> None:
    with engine.begin() as c:
        c.execute(text("UPDATE jobs SET thread_id = :t, updated_at = :now WHERE id = :id"),
                  {"t": thread_id, "now": utcnow_iso(), "id": job_id})


def window_jobs(engine: Engine, since_iso: str) -> list[dict]:
    """Jobs first seen within the window, with embeddings, for dedupe (SPEC 6.3)."""
    with engine.connect() as c:
        return _rows(
            c.execute(
                text(
                    "SELECT id, source, company_name_raw, title, content_hash, embedding, posted_at, status, "
                    "dedupe_group FROM jobs WHERE first_seen_at >= :since AND status != 'filtered_out' "
                    "ORDER BY id"
                ),
                {"since": since_iso},
            )
        )


def set_dedupe_group(engine: Engine, job_id: int, group_id: int, mark_duplicate: bool) -> None:
    with engine.begin() as c:
        if mark_duplicate:
            row = _one(c.execute(text("SELECT status FROM jobs WHERE id = :id"), {"id": job_id}))
            assert row is not None
            check_transition(row["status"], "duplicate")
            c.execute(
                text(
                    "UPDATE jobs SET dedupe_group = :g, status = 'duplicate', status_reason = :r, "
                    "updated_at = :now WHERE id = :id"
                ),
                {"g": group_id, "r": f"duplicate of job {group_id}", "now": utcnow_iso(), "id": job_id},
            )
        else:
            c.execute(text("UPDATE jobs SET dedupe_group = :g WHERE id = :id"), {"g": group_id, "id": job_id})


def company_titles_since(engine: Engine, company_names: Sequence[str], since_iso: str) -> list[dict]:
    if not company_names:
        return []
    clause, params = _in_clause("c", list(company_names))
    params["since"] = since_iso
    with engine.connect() as c:
        return _rows(
            c.execute(
                text(
                    "SELECT id, company_name_raw, title, dedupe_group FROM jobs "
                    f"WHERE company_name_raw IN ({clause}) AND first_seen_at >= :since"
                ),
                params,
            )
        )


def dedupe_siblings(engine: Engine, job: dict) -> list[dict]:
    group = job.get("dedupe_group")
    if not group:
        return []
    with engine.connect() as c:
        return _rows(
            c.execute(
                text("SELECT id, source, title, company_name_raw, status, url FROM jobs "
                     "WHERE dedupe_group = :g AND id != :id ORDER BY id"),
                {"g": group, "id": job["id"]},
            )
        )


# ---------------------------------------------------------------------------------------------
# scores
# ---------------------------------------------------------------------------------------------

SUBSCORES = ("skill_match", "role_fit", "seniority_fit", "domain_fit", "comp_fit", "logistics_fit")  # from the LLM
CODE_SUBSCORES = ("focus_fit", "industry_fit", "ideal_fit")  # computed in code: config `focus`; the employer's industry
WEIGHT_KEYS = (*SUBSCORES, *CODE_SUBSCORES)


def record_score(engine: Engine, job_id: int, row: dict[str, Any], *, set_scored: bool = True) -> int:
    """Insert one score attempt and (optionally) move the job to 'scored' — one short transaction."""
    cols = ("job_id", "scored_at", "model", "rubric_version", "total", *SUBSCORES, "red_flags", "gaps",
            "evidence", "rationale", "ghost_score", "low_confidence", "focus_fit", "focus_hits", "analysis",
            "industry_fit", "ideal_fit")
    values = {k: row.get(k) for k in cols}
    values["job_id"] = job_id
    values["scored_at"] = row.get("scored_at") or utcnow_iso()
    for k in ("red_flags", "gaps", "evidence"):
        values[k] = dumps(row.get(k) or [])
    values["focus_hits"] = dumps(row["focus_hits"]) if row.get("focus_hits") is not None else None
    values["analysis"] = dumps(row["analysis"]) if row.get("analysis") is not None else None
    with engine.begin() as c:
        res = c.execute(text(f"INSERT INTO job_scores ({', '.join(cols)}) VALUES "
                             f"({', '.join(':' + k for k in cols)})"), values)
        if set_scored:
            cur = c.execute(text("SELECT status FROM jobs WHERE id = :id"), {"id": job_id}).scalar_one()
            if cur != "scored":
                check_transition(cur, "scored")
                c.execute(text("UPDATE jobs SET status = 'scored', status_reason = NULL, updated_at = :now "
                               "WHERE id = :id"), {"now": utcnow_iso(), "id": job_id})
        return int(res.lastrowid)


def _decode_score(row: dict | None) -> dict | None:
    if row is None:
        return None
    for k in ("red_flags", "gaps", "evidence"):
        row[k] = loads(row.get(k), [])
    row["focus_hits"] = loads(row.get("focus_hits"), None)
    row["analysis"] = loads(row.get("analysis"), None)
    return row


def latest_score(engine: Engine, job_id: int) -> dict | None:
    with engine.connect() as c:
        return _decode_score(
            _one(c.execute(text("SELECT * FROM job_scores WHERE job_id = :id ORDER BY id DESC LIMIT 1"),
                           {"id": job_id}))
        )


def score_history(engine: Engine, job_id: int) -> list[dict]:
    with engine.connect() as c:
        rows = _rows(c.execute(text("SELECT * FROM job_scores WHERE job_id = :id ORDER BY id DESC"),
                               {"id": job_id}))
    return [_decode_score(r) for r in rows]  # type: ignore[misc]


def all_score_subscores(engine: Engine) -> list[dict]:
    """Every score row with what's needed to recompute focus_fit and the total (no LLM)."""
    with engine.connect() as c:
        return _rows(c.execute(text(
            f"SELECT s.id, {', '.join('s.' + k for k in SUBSCORES)}, s.ghost_score, s.analysis, s.industry_fit, "
            "s.ideal_fit, j.title, j.description_text, j.company_name_raw, j.embedding "
            "FROM job_scores s JOIN jobs j ON j.id = s.job_id")))


def update_score_totals(engine: Engine, totals: Sequence[tuple[int, float, int | None, str | None]],
                        derived: Mapping[int, Mapping[str, int]] | None = None) -> None:
    """Weights, focus, skill levels or the rules changed: rewrite the derived columns (total, focus_fit, focus_hits,
    and, per score, any of skill_match / role_fit / industry_fit that code derives). The model's own subscores are
    never touched."""
    allowed = {"skill_match", "role_fit", "industry_fit", "ideal_fit"}
    if not totals and not derived:
        return
    with engine.begin() as c:
        if totals:
            c.execute(text("UPDATE job_scores SET total = :total, focus_fit = :fit, focus_hits = :hits "
                           "WHERE id = :id"), [{"id": i, "total": t, "fit": f, "hits": h} for i, t, f, h in totals])
        for score_id, cols in (derived or {}).items():
            cols = {k: v for k, v in cols.items() if k in allowed}
            if cols:
                c.execute(text(f"UPDATE job_scores SET {', '.join(f'{k} = :{k}' for k in cols)} WHERE id = :id"),
                          {**cols, "id": score_id})



# ---------------------------------------------------------------------------------------------
# skill levels (the owner's 1-10 self-ratings)
# ---------------------------------------------------------------------------------------------


def list_skill_levels(engine: Engine) -> list[dict]:
    with engine.connect() as c:
        return _rows(c.execute(text("SELECT * FROM skill_levels ORDER BY label COLLATE NOCASE")))


def skill_level_map(engine: Engine) -> dict[str, int]:
    """Rated skills only: lowercase skill -> 1-10."""
    with engine.connect() as c:
        return {r[0]: int(r[1]) for r in c.execute(text(
            "SELECT skill, level FROM skill_levels WHERE level IS NOT NULL"))}


def save_skill_levels(engine: Engine, rows: Sequence[dict]) -> None:
    """Upsert {skill, label, level (None = not rated), custom}. One short transaction."""
    now = utcnow_iso()
    with engine.begin() as c:
        for r in rows:
            c.execute(text(
                "INSERT INTO skill_levels (skill, label, level, custom, updated_at) "
                "VALUES (:skill, :label, :level, :custom, :now) "
                "ON CONFLICT(skill) DO UPDATE SET label = excluded.label, level = excluded.level, "
                "updated_at = excluded.updated_at"),
                {"skill": r["skill"], "label": r["label"], "level": r.get("level"),
                 "custom": int(bool(r.get("custom"))), "now": now})


def delete_skill_level(engine: Engine, skill: str) -> None:
    with engine.begin() as c:
        c.execute(text("DELETE FROM skill_levels WHERE skill = :skill"), {"skill": skill})

# ---------------------------------------------------------------------------------------------
# queue / search
# ---------------------------------------------------------------------------------------------

_PAY = "COALESCE(j.comp_max, j.comp_min)"
QUEUE_SORTS = {  # unscored / unknown values always sort last
    "total": "s.total IS NULL, s.total DESC, j.id DESC",
    "total_asc": "s.total IS NULL, s.total ASC, j.id DESC",
    "seen": "j.first_seen_at DESC, j.id DESC",
    "seen_asc": "j.first_seen_at ASC, j.id ASC",
    "posted": "j.posted_at IS NULL, j.posted_at DESC, j.id DESC",
    "posted_asc": "j.posted_at IS NULL, j.posted_at ASC, j.id DESC",
    "company": "j.company_name_raw COLLATE NOCASE ASC, s.total DESC",
    "company_desc": "j.company_name_raw COLLATE NOCASE DESC, s.total DESC",
    "title": "j.title COLLATE NOCASE ASC, s.total DESC",
    "title_desc": "j.title COLLATE NOCASE DESC, s.total DESC",
    "pay": f"{_PAY} IS NULL, {_PAY} DESC, s.total DESC",
    "pay_asc": f"{_PAY} IS NULL, {_PAY} ASC, s.total DESC",
    "ghost": "s.ghost_score IS NULL, s.ghost_score ASC, s.total DESC",
    "ghost_desc": "s.ghost_score IS NULL, s.ghost_score DESC, s.total DESC",
}
SORT_LABELS = {
    "total": "Score: high to low", "total_asc": "Score: low to high",
    "seen": "Newest found", "seen_asc": "Oldest found",
    "posted": "Posted: newest", "posted_asc": "Posted: oldest", "company": "Company A–Z", "company_desc": "Company Z–A",
    "title": "Title A–Z", "title_desc": "Title Z–A", "pay": "Pay: high to low", "pay_asc": "Pay: low to high",
    "ghost": "Ghost: least likely stale", "ghost_desc": "Ghost: most likely stale",
}


@dataclass
class QueueFilters:
    q: str | None = None
    min_score: float | None = None
    max_ghost: int | None = None
    status: str | None = "scored"
    remote: str | None = None
    employment: str | None = None
    company: str | None = None
    source: str | None = None
    scorer: str | None = None  # "claude" | "local": which model produced the latest score
    sort: str = "total"
    page: int = 1
    page_size: int = 50
    job_ids: list[int] | None = field(default=None)
    scored_in_run: int | None = None   # only jobs whose score was produced while that run was going
    added_in_run: int | None = None    # only jobs first retrieved while that run was going
    scored_within_days: float | None = None     # latest score made in the last N days
    retrieved_within_days: float | None = None  # first retrieved in the last N days


def run_scored_count(engine: Engine, run_id: int) -> int:
    """How many jobs were scored while that run was running."""
    with engine.connect() as c:
        return int(c.execute(text(
            "SELECT COUNT(DISTINCT s.job_id) FROM job_scores s "
            "WHERE s.scored_at >= (SELECT started_at FROM runs WHERE id = :run) "
            "AND s.scored_at <= COALESCE((SELECT finished_at FROM runs WHERE id = :run), :now)"),
            {"run": run_id, "now": utcnow_iso()}).scalar() or 0)


def fts_query(q: str) -> str | None:
    tokens = re.findall(r"\w+", q or "", flags=re.UNICODE)
    return " ".join(f'"{t}"' for t in tokens) or None


def _queue_where(f: QueueFilters) -> tuple[str, dict[str, Any]]:
    where = ["1=1"]
    params: dict[str, Any] = {}
    if f.status and f.status != "any":
        where.append("j.status = :status")
        params["status"] = f.status
    if f.min_score:  # 0 means no minimum: it must not hide the jobs not scored yet
        where.append("s.total >= :min_score")
        params["min_score"] = f.min_score
    if f.max_ghost is not None:
        where.append("COALESCE(s.ghost_score, 0) <= :max_ghost")
        params["max_ghost"] = f.max_ghost
    if f.remote:
        where.append("j.remote_type = :remote")
        params["remote"] = f.remote
    if f.employment:
        where.append("j.employment_type = :employment")
        params["employment"] = f.employment
    if f.company:
        where.append("j.company_name_raw LIKE :company")
        params["company"] = f"%{f.company}%"
    if f.source:
        where.append("j.source = :source")
        params["source"] = f.source
    if f.scorer == "local":
        where.append("s.model LIKE 'local:%'")
    elif f.scorer == "claude":
        where.append("s.model IS NOT NULL AND s.model NOT LIKE 'local:%'")
    if f.q and (fq := fts_query(f.q)):
        where.append("j.id IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH :fts)")
        params["fts"] = fq
    if f.scored_in_run:
        # A score carries no run id, so the run's own window is what identifies its work.
        where.append("s.scored_at >= (SELECT started_at FROM runs WHERE id = :run) "
                     "AND s.scored_at <= COALESCE((SELECT finished_at FROM runs WHERE id = :run), :now)")
        params["run"] = f.scored_in_run
        params["now"] = utcnow_iso()
    if f.scored_within_days:
        where.append("s.scored_at >= :scored_since")
        params["scored_since"] = iso_shift(-f.scored_within_days)
    if f.retrieved_within_days:
        where.append("j.first_seen_at >= :retrieved_since")
        params["retrieved_since"] = iso_shift(-f.retrieved_within_days)
    if f.added_in_run:
        where.append("j.first_seen_at >= (SELECT started_at FROM runs WHERE id = :added_run) "
                     "AND j.first_seen_at <= COALESCE((SELECT finished_at FROM runs WHERE id = :added_run), :now)")
        params["added_run"] = f.added_in_run
        params["now"] = utcnow_iso()
    if f.job_ids is not None:
        clause, p = _in_clause("qid", f.job_ids)
        where.append(f"j.id IN ({clause})")
        params.update(p)
    return " AND ".join(where), params


_QUEUE_FROM = (
    "FROM jobs j LEFT JOIN job_scores s ON s.id = "
    "(SELECT MAX(id) FROM job_scores WHERE job_id = j.id)"
)


def list_queue(engine: Engine, f: QueueFilters, *, paginate: bool = True) -> tuple[list[dict], int]:
    where, params = _queue_where(f)
    order = QUEUE_SORTS.get(f.sort, QUEUE_SORTS["total"])
    sql = (
        "SELECT j.id, j.source, j.company_name_raw, j.title, j.location_raw, j.remote_type, j.employment_type, "
        "j.comp_min, j.comp_max, j.comp_source, j.posted_at, j.first_seen_at, j.status, j.status_reason, "
        "j.url, j.apply_url, j.snooze_until, j.description_text, "
        "s.total, s.skill_match, s.role_fit, s.domain_fit, s.seniority_fit, s.comp_fit, s.logistics_fit, s.focus_fit, "
        "s.industry_fit, s.ideal_fit, "
        "s.ghost_score, s.rationale, s.low_confidence, s.model AS score_model, s.scored_at "
        f"{_QUEUE_FROM} WHERE {where} ORDER BY {order}"
    )
    if paginate:
        sql += " LIMIT :limit OFFSET :offset"
        params["limit"] = f.page_size
        params["offset"] = max(0, f.page - 1) * f.page_size
    with engine.connect() as c:
        rows = _rows(c.execute(text(sql), params))
        count_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}
        total = c.execute(text(f"SELECT COUNT(*) {_QUEUE_FROM} WHERE {where}"), count_params).scalar_one()
    return rows, int(total)


def fts_search(engine: Engine, q: str, limit: int = 100) -> list[tuple[int, float]]:
    fq = fts_query(q)
    if not fq:
        return []
    with engine.connect() as c:
        rows = c.execute(
            text("SELECT rowid, bm25(jobs_fts) AS rank FROM jobs_fts WHERE jobs_fts MATCH :q "
                 "ORDER BY rank LIMIT :limit"),
            {"q": fq, "limit": limit},
        ).all()
    return [(int(r[0]), float(r[1])) for r in rows]


def search_embedding_rows(engine: Engine, since_iso: str) -> list[dict]:
    with engine.connect() as c:
        return _rows(
            c.execute(
                text("SELECT id, embedding FROM jobs WHERE embedding IS NOT NULL AND status != 'duplicate' "
                     "AND last_seen_at >= :since"),
                {"since": since_iso},
            )
        )


# ---------------------------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------------------------


def insert_decision(engine: Engine, job_id: int, action: str, reason: str | None, total: float | None) -> None:
    with engine.begin() as c:
        c.execute(
            text("INSERT INTO decisions (job_id, action, reason, total_at_decision, created_at) "
                 "VALUES (:j, :a, :r, :t, :now)"),
            {"j": job_id, "a": action, "r": reason, "t": total, "now": utcnow_iso()},
        )


def decisions_for_job(engine: Engine, job_id: int) -> list[dict]:
    with engine.connect() as c:
        return _rows(c.execute(text("SELECT * FROM decisions WHERE job_id = :j ORDER BY id DESC"), {"j": job_id}))


def count_decisions(engine: Engine) -> int:
    with engine.connect() as c:
        return int(c.execute(text("SELECT COUNT(*) FROM decisions")).scalar_one())


def calibration_examples(engine: Engine, per_side: int = 4) -> list[dict]:
    """Highest-scored rejects and lowest-scored shortlists; deterministic ordering (SPEC 6.7)."""
    base = (
        "SELECT d.action, d.reason, d.total_at_decision, j.title, j.company_name_raw, j.id AS job_id "
        "FROM decisions d JOIN jobs j ON j.id = d.job_id WHERE d.action = :a AND d.total_at_decision IS NOT NULL "
    )
    with engine.connect() as c:
        rejects = _rows(c.execute(text(base + "ORDER BY d.total_at_decision DESC, d.id ASC LIMIT :n"),
                                  {"a": "reject", "n": per_side}))
        shortlists = _rows(c.execute(text(base + "ORDER BY d.total_at_decision ASC, d.id ASC LIMIT :n"),
                                     {"a": "shortlist", "n": per_side}))
    return rejects + shortlists


# ---------------------------------------------------------------------------------------------
# facts / answers
# ---------------------------------------------------------------------------------------------

_FACT_COLS = ("id", "kind", "parent_id", "text", "variants", "metrics", "tags", "org", "title", "start_date",
              "end_date", "embedding")


def replace_facts(engine: Engine, rows: Sequence[dict[str, Any]]) -> None:
    payload = []
    for r in rows:
        v = {k: r.get(k) for k in _FACT_COLS}
        for k in ("variants", "metrics", "tags"):
            v[k] = dumps(r.get(k) if r.get(k) is not None else ([] if k != "metrics" else {}))
        payload.append(v)
    with engine.begin() as c:
        c.execute(text("DELETE FROM facts"))
        if payload:
            c.execute(text(f"INSERT INTO facts ({', '.join(_FACT_COLS)}) VALUES "
                           f"({', '.join(':' + k for k in _FACT_COLS)})"), payload)


def _decode_fact(r: dict) -> dict:
    r["variants"] = loads(r.get("variants"), [])
    r["metrics"] = loads(r.get("metrics"), {})
    r["tags"] = loads(r.get("tags"), [])
    return r


def list_facts(engine: Engine, kind: str | None = None, q: str | None = None,
               with_embedding: bool = False, file_order: bool = False) -> list[dict]:
    """file_order=True returns facts in facts.yaml order (sync inserts them in file order)."""
    cols = ", ".join(c for c in _FACT_COLS if with_embedding or c != "embedding")
    sql = f"SELECT {cols} FROM facts WHERE 1=1"
    params: dict[str, Any] = {}
    if kind:
        sql += " AND kind = :kind"
        params["kind"] = kind
    if q:
        sql += " AND (text LIKE :q OR id LIKE :q OR tags LIKE :q OR org LIKE :q)"
        params["q"] = f"%{q}%"
    with engine.connect() as c:
        order = " ORDER BY rowid" if file_order else " ORDER BY kind, id"
        return [_decode_fact(r) for r in _rows(c.execute(text(sql + order), params))]


def get_facts(engine: Engine, ids: Sequence[str]) -> list[dict]:
    if not ids:
        return []
    clause, params = _in_clause("f", list(ids))
    cols = ", ".join(c for c in _FACT_COLS if c != "embedding")
    with engine.connect() as c:
        return [_decode_fact(r) for r in _rows(c.execute(text(f"SELECT {cols} FROM facts WHERE id IN ({clause})"),
                                                         params))]


def replace_answers(engine: Engine, rows: Sequence[dict[str, Any]]) -> None:
    with engine.begin() as c:
        existing = {r["question_text"]: r for r in _rows(c.execute(
            text("SELECT question_text, last_used_at, use_count FROM answers")))}
        c.execute(text("DELETE FROM answers"))
        for r in rows:
            prev = existing.get(r["question_text"], {})
            c.execute(
                text("INSERT INTO answers (question_text, embedding, answer_text, kind, last_used_at, use_count) "
                     "VALUES (:q, :e, :a, :k, :lu, :uc)"),
                {"q": r["question_text"], "e": r.get("embedding"), "a": r["answer_text"], "k": r.get("kind"),
                 "lu": prev.get("last_used_at"), "uc": prev.get("use_count") or 0},
            )


def list_answers(engine: Engine, with_embedding: bool = False) -> list[dict]:
    cols = "*" if with_embedding else "id, question_text, answer_text, kind, last_used_at, use_count"
    with engine.connect() as c:
        return _rows(c.execute(text(f"SELECT {cols} FROM answers ORDER BY id")))


def touch_answers(engine: Engine, ids: Sequence[int]) -> None:
    if not ids:
        return
    clause, params = _in_clause("a", list(ids))
    params["now"] = utcnow_iso()
    with engine.begin() as c:
        c.execute(text(f"UPDATE answers SET use_count = use_count + 1, last_used_at = :now WHERE id IN ({clause})"),
                  params)


# ---------------------------------------------------------------------------------------------
# documents / applications
# ---------------------------------------------------------------------------------------------


def insert_document(engine: Engine, *, job_id: int, kind: str, markdown: str, fact_map: list, fact_ids: list,
                    coverage: list | None) -> int:
    with engine.begin() as c:
        version = int(c.execute(text("SELECT COALESCE(MAX(version), 0) + 1 FROM documents "
                                     "WHERE job_id = :j AND kind = :k"), {"j": job_id, "k": kind}).scalar_one())
        res = c.execute(
            text("INSERT INTO documents (job_id, kind, version, markdown, fact_map, fact_ids, coverage, approved, "
                 "created_at) VALUES (:j, :k, :v, :m, :fm, :fi, :cov, 0, :now)"),
            {"j": job_id, "k": kind, "v": version, "m": markdown, "fm": dumps(fact_map), "fi": dumps(fact_ids),
             "cov": dumps(coverage or []), "now": utcnow_iso()},
        )
        return int(res.lastrowid)


def _decode_doc(r: dict | None) -> dict | None:
    if r is None:
        return None
    r["fact_map"] = loads(r.get("fact_map"), [])
    r["fact_ids"] = loads(r.get("fact_ids"), [])
    r["coverage"] = loads(r.get("coverage"), [])
    r["file_paths"] = loads(r.get("file_path"), []) if (r.get("file_path") or "").startswith("[") else (
        [r["file_path"]] if r.get("file_path") else [])
    return r


def get_document(engine: Engine, doc_id: int) -> dict | None:
    with engine.connect() as c:
        return _decode_doc(_one(c.execute(text("SELECT * FROM documents WHERE id = :id"), {"id": doc_id})))


def documents_for_job(engine: Engine, job_id: int) -> list[dict]:
    with engine.connect() as c:
        rows = _rows(c.execute(text("SELECT * FROM documents WHERE job_id = :j ORDER BY kind, version DESC"),
                               {"j": job_id}))
    return [_decode_doc(r) for r in rows]  # type: ignore[misc]


def approve_document(engine: Engine, doc_id: int) -> None:
    with engine.begin() as c:
        c.execute(text("UPDATE documents SET approved = 1 WHERE id = :id"), {"id": doc_id})


def set_document_files(engine: Engine, doc_id: int, paths: Sequence[str]) -> None:
    with engine.begin() as c:
        c.execute(text("UPDATE documents SET file_path = :p WHERE id = :id"), {"p": dumps(list(paths)), "id": doc_id})


def record_application(engine: Engine, *, job_id: int, method: str, confirmation_ref: str | None,
                       document_ids: list[int], follow_up_at: str, notes: str | None,
                       submitted_at: str | None = None) -> int:
    """Insert the application and move the job to 'applied' in one transaction."""
    with engine.begin() as c:
        cur = c.execute(text("SELECT status FROM jobs WHERE id = :id"), {"id": job_id}).scalar_one()
        check_transition(cur, "applied")
        res = c.execute(
            text("INSERT INTO applications (job_id, method, submitted_at, confirmation_ref, document_ids, "
                 "follow_up_at, outcome, notes) VALUES (:j, :m, :now, :ref, :docs, :fu, 'none', :notes)"),
            {"j": job_id, "m": method, "now": submitted_at or utcnow_iso(), "ref": confirmation_ref,
             "docs": dumps(document_ids), "fu": follow_up_at, "notes": notes},
        )
        c.execute(text("UPDATE jobs SET status = 'applied', updated_at = :now WHERE id = :id"),
                  {"now": utcnow_iso(), "id": job_id})
        return int(res.lastrowid)


def list_applications(engine: Engine, outcome: str | None = None, needs_follow_up: bool = False,
                      now: str | None = None) -> list[dict]:
    sql = ("SELECT a.*, j.title, j.company_name_raw, j.url, j.apply_url, j.status AS job_status "
           "FROM applications a JOIN jobs j ON j.id = a.job_id WHERE 1=1")
    params: dict[str, Any] = {}
    if outcome:
        sql += " AND a.outcome = :outcome"
        params["outcome"] = outcome
    if needs_follow_up:
        sql += " AND a.outcome = 'none' AND a.follow_up_at IS NOT NULL AND a.follow_up_at <= :now"
        params["now"] = now or utcnow_iso()
    with engine.connect() as c:
        rows = _rows(c.execute(text(sql + " ORDER BY a.submitted_at DESC"), params))
    for r in rows:
        r["document_ids"] = loads(r.get("document_ids"), [])
    return rows


def get_application(engine: Engine, app_id: int) -> dict | None:
    with engine.connect() as c:
        return _one(c.execute(text("SELECT * FROM applications WHERE id = :id"), {"id": app_id}))


def applications_for_job(engine: Engine, job_id: int) -> list[dict]:
    with engine.connect() as c:
        return _rows(c.execute(text("SELECT * FROM applications WHERE job_id = :j ORDER BY id DESC"), {"j": job_id}))


def set_application_outcome(engine: Engine, app_id: int, outcome: str, notes: str | None) -> None:
    with engine.begin() as c:
        app = _one(c.execute(text("SELECT job_id, notes FROM applications WHERE id = :id"), {"id": app_id}))
        if app is None:
            raise KeyError(f"application {app_id} not found")
        merged = "\n".join(x for x in (app.get("notes"), notes) if x)
        c.execute(text("UPDATE applications SET outcome = :o, notes = :n WHERE id = :id"),
                  {"o": outcome, "n": merged or None, "id": app_id})
        job_status = c.execute(text("SELECT status FROM jobs WHERE id = :j"), {"j": app["job_id"]}).scalar_one()
        target = {"rejected": "closed", "screen": "responded", "interview": "responded",
                  "offer": "responded"}.get(outcome)
        if target and target != job_status and target in TRANSITIONS.get(job_status, set()):
            c.execute(text("UPDATE jobs SET status = :s, updated_at = :now WHERE id = :j"),
                      {"s": target, "now": utcnow_iso(), "j": app["job_id"]})


# ---------------------------------------------------------------------------------------------
# runs / llm calls
# ---------------------------------------------------------------------------------------------


def create_run(engine: Engine, trigger: str, sources: list[str]) -> int:
    with engine.begin() as c:
        res = c.execute(
            text("INSERT INTO runs (started_at, trigger, status, sources, counts, errors, current_node) "
                 "VALUES (:now, :t, 'running', :s, '{}', '[]', 'queued')"),
            {"now": utcnow_iso(), "t": trigger, "s": dumps(sources)},
        )
        run_id = int(res.lastrowid)
        c.execute(text("UPDATE runs SET thread_id = :th WHERE id = :id"), {"th": f"run-{run_id}", "id": run_id})
        return run_id


def update_run_progress(engine: Engine, run_id: int, *, node: str | None = None, counts: dict | None = None,
                        errors: list | None = None, status: str | None = None, finished: bool = False,
                        reopen: bool = False) -> None:
    sets, params = [], {"id": run_id}
    if node is not None:
        sets.append("current_node = :node")
        params["node"] = node
    if counts is not None:
        sets.append("counts = :counts")
        params["counts"] = dumps(counts)
    if errors is not None:
        sets.append("errors = :errors")
        params["errors"] = dumps(errors)
    if status is not None:
        sets.append("status = :status")
        params["status"] = status
    if finished:
        sets.append("finished_at = :now")
        params["now"] = utcnow_iso()
    elif reopen:
        sets.append("finished_at = NULL")
    if not sets:
        return
    with engine.begin() as c:
        c.execute(text(f"UPDATE runs SET {', '.join(sets)} WHERE id = :id"), params)


def _decode_run(r: dict | None) -> dict | None:
    if r is None:
        return None
    r["sources"] = loads(r.get("sources"), [])
    r["counts"] = loads(r.get("counts"), {})
    r["errors"] = loads(r.get("errors"), [])
    return r


def latest_run_id(engine: Engine) -> int | None:
    """The newest run that retrieved new jobs. A scoring-only run (catching up on the backlog) adds none, and
    "the jobs from the latest run" means the last batch that came in."""
    with engine.connect() as c:
        return c.execute(text("SELECT MAX(id) FROM runs WHERE COALESCE(json_extract(counts, '$.new'), 0) > 0")
                         ).scalar() or c.execute(text("SELECT MAX(id) FROM runs")).scalar()


def get_run(engine: Engine, run_id: int) -> dict | None:
    with engine.connect() as c:
        return _decode_run(_one(c.execute(text("SELECT * FROM runs WHERE id = :id"), {"id": run_id})))


def list_runs(engine: Engine, limit: int = 50, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM runs"
    params: dict[str, Any] = {"limit": limit}
    if status:
        sql += " WHERE status = :status"
        params["status"] = status
    with engine.connect() as c:
        rows = _rows(c.execute(text(sql + " ORDER BY id DESC LIMIT :limit"), params))
    return [_decode_run(r) for r in rows]  # type: ignore[misc]


def insert_llm_call(engine: Engine, *, job_id: int | None, purpose: str, model: str, prompt_version: str | None,
                    input_tokens: int, output_tokens: int, cost_usd: float, latency_ms: int) -> None:
    with engine.begin() as c:
        c.execute(
            text("INSERT INTO llm_calls (job_id, purpose, model, prompt_version, input_tokens, output_tokens, "
                 "cost_usd, latency_ms, created_at) VALUES (:j, :p, :m, :v, :i, :o, :c, :l, :now)"),
            {"j": job_id, "p": purpose, "m": model, "v": prompt_version, "i": input_tokens, "o": output_tokens,
             "c": cost_usd, "l": latency_ms, "now": utcnow_iso()},
        )


def spend_by_day(engine: Engine, since_iso: str) -> list[dict]:
    with engine.connect() as c:
        return _rows(
            c.execute(
                text("SELECT substr(created_at, 1, 10) AS day, purpose, COUNT(*) AS calls, "
                     "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
                     "ROUND(SUM(cost_usd), 4) AS cost_usd FROM llm_calls WHERE created_at >= :since "
                     "GROUP BY day, purpose ORDER BY day DESC, purpose"),
                {"since": since_iso},
            )
        )


# ---------------------------------------------------------------------------------------------
# locks (SPEC 4.16)
# ---------------------------------------------------------------------------------------------


def lock_insert(engine: Engine, name: str, pid: int, note: str | None) -> bool:
    try:
        with engine.begin() as c:
            c.execute(text("INSERT INTO locks (name, acquired_at, pid, note) VALUES (:n, :now, :pid, :note)"),
                      {"n": name, "now": utcnow_iso(), "pid": pid, "note": note})
        return True
    except IntegrityError:
        return False


def lock_get(engine: Engine, name: str) -> dict | None:
    with engine.connect() as c:
        return _one(c.execute(text("SELECT * FROM locks WHERE name = :n"), {"n": name}))


def lock_delete(engine: Engine, name: str, *, acquired_at: str | None = None, pid: int | None = None) -> bool:
    sql = "DELETE FROM locks WHERE name = :n"
    params: dict[str, Any] = {"n": name}
    if acquired_at is not None:
        sql += " AND acquired_at = :a"
        params["a"] = acquired_at
    if pid is not None:
        sql += " AND pid = :pid"
        params["pid"] = pid
    with engine.begin() as c:
        return c.execute(text(sql), params).rowcount == 1


def lock_touch(engine: Engine, name: str, pid: int) -> None:
    with engine.begin() as c:
        c.execute(text("UPDATE locks SET acquired_at = :now WHERE name = :n AND pid = :pid"),
                  {"now": utcnow_iso(), "n": name, "pid": pid})


def list_locks(engine: Engine) -> list[dict]:
    with engine.connect() as c:
        return _rows(c.execute(text("SELECT * FROM locks ORDER BY name")))


# ---------------------------------------------------------------------------------------------
# stats / backup
# ---------------------------------------------------------------------------------------------


def status_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as c:
        return {r[0]: int(r[1]) for r in c.execute(text("SELECT status, COUNT(*) FROM jobs GROUP BY status")).all()}


def company_job_stats(engine: Engine) -> list[dict]:
    with engine.connect() as c:
        return _rows(c.execute(text(
            "SELECT company_name_raw, COUNT(*) AS jobs, SUM(status = 'scored') AS scored, "
            "SUM(status IN ('shortlisted', 'in_workflow', 'ready', 'applied', 'responded')) AS pipeline, "
            "MAX(last_seen_at) AS last_seen FROM jobs GROUP BY company_name_raw")))


def source_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as c:
        return {r[0]: int(r[1]) for r in c.execute(text("SELECT source, COUNT(*) FROM jobs GROUP BY source")).all()}


def total_spend(engine: Engine) -> float:
    with engine.connect() as c:
        return float(c.execute(text("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls")).scalar_one())


def vacuum_into(engine: Engine, out_path: Path) -> None:
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        raise FileExistsError(f"backup target already exists: {out_path}")
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.exec_driver_sql("VACUUM INTO ?", (str(out_path),))


def pragma_values(engine: Engine) -> dict[str, Any]:
    with engine.connect() as c:
        return {
            "journal_mode": c.exec_driver_sql("PRAGMA journal_mode").scalar(),
            "busy_timeout": c.exec_driver_sql("PRAGMA busy_timeout").scalar(),
            "foreign_keys": c.exec_driver_sql("PRAGMA foreign_keys").scalar(),
            "synchronous": c.exec_driver_sql("PRAGMA synchronous").scalar(),
        }


# --------------------------------------------------------------------------------------------- schedules

SCHEDULE_JSON = ("days", "times", "sources", "industries", "companies")
SCHEDULE_FIELDS = ("name", "enabled", "mode", "when_kind", "days", "times", "every_hours", "window_start",
                   "window_end", "cron", "timezone", "sources", "industries", "companies", "max_to_score", "if_busy",
                   "catch_up", "notes")


def _decode_schedule(r: dict | None) -> dict | None:
    if r is None:
        return None
    for k in SCHEDULE_JSON:
        r[k] = loads(r.get(k), [])
    r["enabled"] = bool(r["enabled"])
    r["catch_up"] = bool(r["catch_up"])
    return r


def _schedule_params(values: dict) -> dict:
    out = {k: values.get(k) for k in SCHEDULE_FIELDS}
    for k in SCHEDULE_JSON:
        out[k] = dumps(list(out[k] or []))
    out["enabled"] = int(bool(out["enabled"]))
    out["catch_up"] = int(bool(out["catch_up"]))
    return out


def list_schedules(engine: Engine) -> list[dict]:
    with engine.connect() as c:
        rows = _rows(c.execute(text(
            "SELECT s.*, r.status AS last_run_status, r.counts AS last_run_counts FROM schedules s "
            "LEFT JOIN runs r ON r.id = s.last_run_id ORDER BY s.enabled DESC, s.name COLLATE NOCASE")))
    out = [_decode_schedule(r) for r in rows]
    for r in out:
        r["last_run_counts"] = loads(r.get("last_run_counts"), {})  # type: ignore[index]
    return out  # type: ignore[return-value]


def get_schedule(engine: Engine, schedule_id: int) -> dict | None:
    with engine.connect() as c:
        return _decode_schedule(_one(c.execute(text("SELECT * FROM schedules WHERE id = :id"), {"id": schedule_id})))


def create_schedule(engine: Engine, values: dict) -> int:
    params = _schedule_params(values) | {"now": utcnow_iso()}
    cols = ", ".join(SCHEDULE_FIELDS)
    marks = ", ".join(f":{k}" for k in SCHEDULE_FIELDS)
    with engine.begin() as c:
        res = c.execute(text(f"INSERT INTO schedules ({cols}, created_at, updated_at, enabled_at) VALUES ({marks}, "
                             ":now, :now, CASE WHEN :enabled = 1 THEN :now END)"), params)
        return int(res.lastrowid)


def update_schedule(engine: Engine, schedule_id: int, values: dict) -> None:
    params = _schedule_params(values) | {"now": utcnow_iso(), "id": schedule_id}
    sets = ", ".join(f"{k} = :{k}" for k in SCHEDULE_FIELDS)
    with engine.begin() as c:
        c.execute(text(f"UPDATE schedules SET {sets}, updated_at = :now, enabled_at = CASE "
                       "WHEN :enabled = 1 AND enabled = 0 THEN :now ELSE enabled_at END WHERE id = :id"), params)


def set_schedule_enabled(engine: Engine, schedule_id: int, enabled: bool) -> None:
    with engine.begin() as c:
        c.execute(text("UPDATE schedules SET enabled_at = CASE WHEN :e = 1 AND enabled = 0 THEN :now "
                       "ELSE enabled_at END, enabled = :e, updated_at = :now WHERE id = :id"),
                  {"e": int(enabled), "now": utcnow_iso(), "id": schedule_id})


def delete_schedule(engine: Engine, schedule_id: int) -> None:
    with engine.begin() as c:
        c.execute(text("DELETE FROM schedules WHERE id = :id"), {"id": schedule_id})


def record_schedule_result(engine: Engine, schedule_id: int, result: str, *, run_id: int | None = None,
                           fired: bool = True) -> None:
    sets = "last_result = :res"
    if fired:
        sets += ", last_fired_at = :now"
    if run_id is not None:
        sets += ", last_run_id = :run"
    with engine.begin() as c:
        c.execute(text(f"UPDATE schedules SET {sets} WHERE id = :id"),
                  {"res": result, "now": utcnow_iso(), "run": run_id, "id": schedule_id})
        if run_id is not None:
            c.execute(text("UPDATE runs SET schedule_id = :sid WHERE id = :run"), {"sid": schedule_id, "run": run_id})


def runs_for_schedule(engine: Engine, schedule_id: int, limit: int = 10) -> list[dict]:
    with engine.connect() as c:
        rows = _rows(c.execute(text("SELECT * FROM runs WHERE schedule_id = :sid ORDER BY id DESC LIMIT :limit"),
                               {"sid": schedule_id, "limit": limit}))
    return [_decode_run(r) for r in rows]  # type: ignore[misc]


def top_score_analyses(engine: Engine, min_score: float) -> list[dict]:
    """The latest score of every live job scoring at least min_score, with its stored analysis."""
    with engine.connect() as c:
        return _rows(c.execute(text(
            "SELECT j.id AS job_id, s.total, s.analysis FROM jobs j JOIN job_scores s ON s.id = "
            "(SELECT MAX(id) FROM job_scores WHERE job_id = j.id) "
            "WHERE s.total >= :m AND j.status NOT IN ('filtered_out', 'duplicate', 'closed')"), {"m": min_score}))


def replace_keyword_heat(engine: Engine, rows: Sequence[dict]) -> None:
    """Swap in a freshly built heatmap: one short transaction."""
    now = utcnow_iso()
    with engine.begin() as c:
        c.execute(text("DELETE FROM keyword_heat"))
        if rows:
            c.execute(text("INSERT INTO keyword_heat (keyword, kind, label, jobs, required, have, share, updated_at) "
                           "VALUES (:keyword, :kind, :label, :jobs, :required, :have, :share, :now)"),
                      [{**r, "now": now} for r in rows])


def keyword_heat(engine: Engine, kind: str | None = None) -> list[dict]:
    sql = "SELECT * FROM keyword_heat"
    params: dict[str, Any] = {}
    if kind:
        sql += " WHERE kind = :kind"
        params["kind"] = kind
    with engine.connect() as c:
        return _rows(c.execute(text(sql + " ORDER BY jobs DESC, required DESC, label COLLATE NOCASE"), params))


def get_state(engine: Engine, key: str, default: str | None = None) -> str | None:
    with engine.connect() as c:
        row = c.execute(text("SELECT value FROM app_state WHERE key = :k"), {"k": key}).first()
    return row[0] if row else default


def set_state(engine: Engine, key: str, value: str | None) -> None:
    with engine.begin() as c:
        c.execute(text("INSERT INTO app_state (key, value, updated_at) VALUES (:k, :v, :now) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"),
                  {"k": key, "v": value, "now": utcnow_iso()})


def scores_missing_focus(engine: Engine) -> int:
    with engine.connect() as c:
        return int(c.execute(text("SELECT COUNT(*) FROM job_scores WHERE focus_fit IS NULL")).scalar_one())


# --------------------------------------------------------------------------------------------- manually added jobs

MANUAL_REASON = "added manually"


def mark_manual(engine: Engine, job_id: int) -> None:
    """Jobs you add yourself carry this status_reason while 'discovered', so runs never filter or dedupe them away."""
    with engine.begin() as c:
        c.execute(text("UPDATE jobs SET status_reason = :r WHERE id = :id AND status = 'discovered'"),
                  {"r": MANUAL_REASON, "id": job_id})


def manual_job_ids(engine: Engine, ids: Sequence[int] | None = None) -> set[int]:
    with engine.connect() as c:
        rows = c.execute(text("SELECT id FROM jobs WHERE status = 'discovered' AND status_reason = :r"),
                         {"r": MANUAL_REASON}).all()
    found = {int(r[0]) for r in rows}
    return found if ids is None else found & set(ids)


def job_id_by_source(engine: Engine, source: str, source_job_id: str) -> int | None:
    with engine.connect() as c:
        row = c.execute(text("SELECT id FROM jobs WHERE source = :s AND source_job_id = :i"),
                        {"s": source, "i": source_job_id}).first()
    return int(row[0]) if row else None


def job_id_by_url(engine: Engine, *urls: str | None) -> int | None:
    wanted = [u.strip().rstrip("/") for u in urls if u and u.strip()]
    if not wanted:
        return None
    with engine.connect() as c:
        for u in wanted:
            row = c.execute(text("SELECT id FROM jobs WHERE rtrim(url, '/') = :u OR rtrim(apply_url, '/') = :u "
                                 "ORDER BY id LIMIT 1"), {"u": u}).first()
            if row:
                return int(row[0])
    return None


def replace_job_content(engine: Engine, job_id: int, rec: dict[str, Any]) -> None:
    """Replace a job's posting content (e.g. a truncated aggregator snippet with the full text you pasted)."""
    sets = ", ".join(f"{k} = :{k}" for k in _JOB_CONTENT_COLS)
    params = {k: rec.get(k) for k in _JOB_CONTENT_COLS}
    params.update(id=job_id, now=utcnow_iso())
    with engine.begin() as c:
        c.execute(text(f"UPDATE jobs SET {sets}, embedding = NULL, full_description = 1, updated_at = :now "
                       "WHERE id = :id"), params)


def board_tokens(engine: Engine, company_ids: Iterable[int]) -> dict[int, str]:
    """company id -> its ATS board token."""
    ids = sorted({int(i) for i in company_ids if i is not None})
    if not ids:
        return {}
    clause, params = _in_clause("c", ids)
    with engine.connect() as c:
        return {int(r[0]): r[1] for r in c.execute(text(
            f"SELECT id, board_token FROM companies WHERE id IN ({clause}) AND board_token IS NOT NULL"), params)}


def set_full_description(engine: Engine, job_id: int, description: str) -> None:
    """A snippet-only posting's full text, fetched from the board. The content hash stays the snippet's, so the
    next sighting of the same snippet is 'seen', not 'changed'."""
    with engine.begin() as c:
        c.execute(text("UPDATE jobs SET description_text = :d, full_description = 1, updated_at = :now "
                       "WHERE id = :id"), {"d": description, "now": utcnow_iso(), "id": job_id})


def jobs_with_status(engine: Engine, statuses: Sequence[str]) -> list[dict]:
    clause, params = _in_clause("s", list(statuses))
    with engine.connect() as c:
        return _rows(c.execute(text(f"SELECT id, title, company_name_raw, status FROM jobs WHERE status IN ({clause}) "
                                    "ORDER BY updated_at DESC"), params))


def tailored_jobs(engine: Engine) -> list[dict]:
    """Every job with tailored documents, newest first, with its latest resume and cover letter."""
    with engine.connect() as c:
        docs = _rows(c.execute(text(
            "SELECT d.id, d.job_id, d.kind, d.version, d.approved, d.file_path, d.created_at, j.title, "
            "j.company_name_raw, j.status, j.apply_url, j.url FROM documents d JOIN jobs j ON j.id = d.job_id "
            "ORDER BY d.created_at DESC, d.id DESC")))
    jobs: dict[int, dict] = {}
    for d in docs:
        d = _decode_doc(d)  # type: ignore[assignment]
        job = jobs.setdefault(d["job_id"], {"job_id": d["job_id"], "title": d["title"],
                                            "company": d["company_name_raw"],
                                            "status": d["status"], "apply_url": d["apply_url"] or d["url"],
                                            "updated": d["created_at"], "resume": None, "cover_letter": None,
                                            "versions": 0})
        job["versions"] += 1
        if job.get(d["kind"]) is None:
            job[d["kind"]] = d
    return list(jobs.values())


def filtered_out_jobs(engine: Engine) -> list[dict]:
    """Knocked-out postings with everything the prefilter needs to judge them again (the description is gone)."""
    with engine.connect() as c:
        return _rows(c.execute(text(
            "SELECT id, title, company_name_raw, location_raw, remote_type, employment_type, comp_min, comp_max, "
            "comp_source, posted_at, description_text, status_reason FROM jobs WHERE status = 'filtered_out'")))


def reopen_jobs(engine: Engine, ids: Sequence[int], reason: str) -> int:
    if not ids:
        return 0
    clause, params = _in_clause("j", list(ids))
    with engine.begin() as c:
        return c.execute(text(f"UPDATE jobs SET status = 'discovered', status_reason = :r, updated_at = :now "
                              f"WHERE id IN ({clause}) AND status = 'filtered_out'"),
                         {**params, "r": reason, "now": utcnow_iso()}).rowcount


# ---------------------------------------------------------------------------------------------
# auto-apply (beta): credentials, runs, open questions
# ---------------------------------------------------------------------------------------------

ANY_HOST = "*"  # the credential used for any site without one of its own


def upsert_credential(engine: Engine, *, host: str, username: str, password_enc: str | None,
                      ats_type: str = "workday", notes: str | None = None) -> None:
    """A blank password_enc keeps whatever is stored, so editing the username never wipes the password."""
    now = utcnow_iso()
    with engine.begin() as c:
        row = c.execute(text("SELECT id, password_enc FROM apply_credentials WHERE host = :h"), {"h": host}).first()
        if row:
            c.execute(text("UPDATE apply_credentials SET username = :u, password_enc = :p, ats_type = :a, "
                           "notes = :n, updated_at = :now WHERE id = :id"),
                      {"u": username, "p": password_enc or row[1], "a": ats_type, "n": notes, "now": now, "id": row[0]})
        else:
            c.execute(text("INSERT INTO apply_credentials (host, ats_type, username, password_enc, notes, "
                           "created_at, updated_at) VALUES (:h, :a, :u, :p, :n, :now, :now)"),
                      {"h": host, "a": ats_type, "u": username, "p": password_enc, "n": notes, "now": now})


def list_credentials(engine: Engine) -> list[dict]:
    with engine.connect() as c:
        rows = _rows(c.execute(text("SELECT id, host, ats_type, username, notes, last_used_at, created_at, "
                                    "updated_at, password_enc IS NOT NULL AS has_password "
                                    "FROM apply_credentials ORDER BY host = '*' DESC, host")))
    return rows


def list_credentials_with_secret(engine: Engine) -> list[dict]:
    """Same as list_credentials, but including the stored password blob (for the page that shows it)."""
    with engine.connect() as c:
        return _rows(c.execute(text("SELECT * FROM apply_credentials ORDER BY host = '*' DESC, host")))


def credential_for(engine: Engine, host: str) -> dict | None:
    """The credential for this host, falling back to the shared one."""
    with engine.connect() as c:
        row = c.execute(text("SELECT * FROM apply_credentials WHERE host IN (:h, :any) "
                             "ORDER BY host = :h DESC LIMIT 1"), {"h": host, "any": ANY_HOST}).mappings().first()
    return dict(row) if row else None


def delete_credential(engine: Engine, cred_id: int) -> None:
    with engine.begin() as c:
        c.execute(text("DELETE FROM apply_credentials WHERE id = :id"), {"id": cred_id})


def touch_credential(engine: Engine, host: str) -> None:
    with engine.begin() as c:
        c.execute(text("UPDATE apply_credentials SET last_used_at = :now WHERE host IN (:h, '*')"),
                  {"now": utcnow_iso(), "h": host})


def start_apply_run(engine: Engine, *, job_id: int, host: str | None) -> int:
    with engine.begin() as c:
        res = c.execute(text("INSERT INTO apply_runs (job_id, host, status, stage, events, screenshots, started_at) "
                             "VALUES (:j, :h, 'running', 'starting', '[]', '[]', :now)"),
                        {"j": job_id, "h": host, "now": utcnow_iso()})
        return int(res.lastrowid)


def update_apply_run(engine: Engine, run_id: int, **fields: Any) -> None:
    if not fields:
        return
    for key in ("events", "screenshots"):
        if key in fields and not isinstance(fields[key], str):
            fields[key] = dumps(fields[key])
    if fields.get("status") in ("submitted", "failed", "review", "needs_input"):
        fields.setdefault("finished_at", utcnow_iso())
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    with engine.begin() as c:
        c.execute(text(f"UPDATE apply_runs SET {sets} WHERE id = :id"), {**fields, "id": run_id})


def get_apply_run(engine: Engine, run_id: int) -> dict | None:
    with engine.connect() as c:
        row = c.execute(text("SELECT r.*, j.title, j.company_name_raw, j.apply_url, j.url, j.status AS job_status "
                             "FROM apply_runs r JOIN jobs j ON j.id = r.job_id WHERE r.id = :id"),
                        {"id": run_id}).mappings().first()
    if not row:
        return None
    d = dict(row)
    d["events"] = loads(d.get("events"), [])
    d["screenshots"] = loads(d.get("screenshots"), [])
    return d


def list_apply_runs(engine: Engine, *, job_id: int | None = None, limit: int = 50) -> list[dict]:
    sql = ("SELECT r.*, j.title, j.company_name_raw FROM apply_runs r JOIN jobs j ON j.id = r.job_id")
    params: dict[str, Any] = {"limit": limit}
    if job_id:
        sql += " WHERE r.job_id = :j"
        params["j"] = job_id
    with engine.connect() as c:
        rows = _rows(c.execute(text(sql + " ORDER BY r.id DESC LIMIT :limit"), params))
    for r in rows:
        r["events"] = loads(r.get("events"), [])
        r["screenshots"] = loads(r.get("screenshots"), [])
    return rows


def add_apply_question(engine: Engine, *, run_id: int | None, job_id: int | None, host: str | None, stage: str | None,
                       label: str, norm_label: str, kind: str, options: Sequence[str]) -> int:
    """One row per distinct question. An already-answered question is returned as-is so answering it once is enough."""
    with engine.begin() as c:
        row = c.execute(text("SELECT id, answer FROM apply_questions WHERE norm_label = :n "
                             "ORDER BY answer IS NULL, id DESC LIMIT 1"), {"n": norm_label}).first()
        if row and row[1]:
            return int(row[0])
        if row:
            c.execute(text("UPDATE apply_questions SET run_id = :r, job_id = :j, host = :h, stage = :s, "
                           "options = :o WHERE id = :id"),
                      {"r": run_id, "j": job_id, "h": host, "s": stage, "o": dumps(list(options)), "id": row[0]})
            return int(row[0])
        res = c.execute(text("INSERT INTO apply_questions (run_id, job_id, host, stage, label, norm_label, kind, "
                             "options, created_at) VALUES (:r, :j, :h, :s, :l, :n, :k, :o, :now)"),
                        {"r": run_id, "j": job_id, "h": host, "s": stage, "l": label, "n": norm_label, "k": kind,
                         "o": dumps(list(options)), "now": utcnow_iso()})
        return int(res.lastrowid)


def list_apply_questions(engine: Engine, *, unanswered_only: bool = False, run_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM apply_questions WHERE 1=1"
    params: dict[str, Any] = {}
    if unanswered_only:
        sql += " AND (answer IS NULL OR answer = '')"
    if run_id:
        sql += " AND run_id = :r"
        params["r"] = run_id
    with engine.connect() as c:
        rows = _rows(c.execute(text(sql + " ORDER BY answered_at IS NOT NULL, id DESC"), params))
    for r in rows:
        r["options"] = loads(r.get("options"), [])
    return rows


def answer_apply_question(engine: Engine, question_id: int, answer: str) -> dict | None:
    with engine.begin() as c:
        c.execute(text("UPDATE apply_questions SET answer = :a, answered_at = :now WHERE id = :id"),
                  {"a": answer, "now": utcnow_iso(), "id": question_id})
        row = c.execute(text("SELECT * FROM apply_questions WHERE id = :id"), {"id": question_id}).mappings().first()
    return dict(row) if row else None


def stored_question_answers(engine: Engine) -> dict[str, str]:
    """{normalized label: answer} for every question you have answered before."""
    with engine.connect() as c:
        rows = c.execute(text("SELECT norm_label, answer FROM apply_questions WHERE answer IS NOT NULL "
                              "AND answer != '' ORDER BY id")).all()
    return {r[0]: r[1] for r in rows}
