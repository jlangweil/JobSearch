"""Triage is a database state transition, not a graph (SPEC 1.1). Every action lands in `decisions`."""

from __future__ import annotations

from concurrent.futures import Future

from jobhunt.db import queries
from jobhunt.db.engine import iso_date_to_iso
from jobhunt.graphs.runtime import Runtime
from jobhunt.services import workflow
from jobhunt.sources import liveness


class TriageError(ValueError):
    pass


def _total(rt: Runtime, job_id: int) -> float | None:
    score = queries.latest_score(rt.engine, job_id)
    return score["total"] if score else None


class PostingClosed(TriageError):
    """The board no longer lists the posting; the job has been marked closed."""


def check_posting(rt: Runtime, job: dict) -> liveness.Liveness:
    """Before tailoring: is the posting still up? A clear "gone" marks the job closed and raises PostingClosed;
    an unclear answer lets tailoring go ahead."""
    result = liveness.check(job)
    if result.closed:
        queries.set_job_status(rt.engine, job["id"], "closed", f"posting closed: {result.detail}")
        raise PostingClosed(f"This posting looks closed ({result.detail}), so nothing was tailored and the job is "
                            "marked closed. If you know it's still open, use Tailor anyway on the job page.")
    return result


def shortlist(rt: Runtime, job_id: int, *, reason: str | None = None, emphasis: str | None = None,
              company_notes: str | None = None, verify: bool = False,
              generation_mode: str | None = None) -> Future:
    """`verify`: check the posting is still up first (see check_posting)."""
    job = queries.get_job(rt.engine, job_id)
    if job is None:
        raise TriageError(f"job {job_id} not found")
    if verify and job["status"] != "closed":
        check_posting(rt, job)
    queries.set_job_status(rt.engine, job_id, "shortlisted", reason)
    queries.insert_decision(rt.engine, job_id, "shortlist", reason, _total(rt, job_id))
    return workflow.start(rt, job_id, emphasis=emphasis, company_notes=company_notes,
                          generation_mode=generation_mode)


def reject(rt: Runtime, job_id: int, reason: str) -> None:
    if not (reason or "").strip():
        raise TriageError("a reason is required to reject (it trains the scorer)")
    queries.set_job_status(rt.engine, job_id, "rejected", reason.strip())
    queries.insert_decision(rt.engine, job_id, "reject", reason.strip(), _total(rt, job_id))


def snooze(rt: Runtime, job_id: int, until: str, reason: str | None = None) -> None:
    try:
        until_iso = iso_date_to_iso(until)
    except ValueError as exc:
        raise TriageError("snooze needs a date (YYYY-MM-DD)") from exc
    queries.set_job_status(rt.engine, job_id, "snoozed", reason, snooze_until=until_iso)
    queries.insert_decision(rt.engine, job_id, "snooze", reason or f"until {until}", _total(rt, job_id))
