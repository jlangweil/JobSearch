"""Discovery runs and dry runs started from the web or the scheduler execute in the background executor."""

from __future__ import annotations

import logging
import threading

from jobhunt.db import locks, queries
from jobhunt.db.engine import iso_shift, utcnow_iso
from jobhunt.discovery import service
from jobhunt.graphs.discovery import graph as dg
from jobhunt.graphs.runtime import Runtime

log = logging.getLogger(__name__)
_DRY_LOCK = threading.Lock()
MAX_DRY_RUNS_KEPT = 10


class RunError(ValueError):
    pass


def _execute(rt: Runtime, run_id: int, initial: dict | None) -> None:
    try:
        dg.execute(rt, run_id, initial)
    except Exception:  # already recorded on the run row by dg.execute
        log.info("run %s ended with failure (recorded)", run_id)


def start_background(rt: Runtime, *, trigger: str, sources: list[str] | None = None,
                     max_to_score: int | None = None, companies: list[str] | None = None,
                     skip_fetch: bool = False, skip_score: bool = False, posted_since: str | None = None) -> int:
    """Raises LockHeld when a run is already in progress (in any process)."""
    from jobhunt.services.companies import sync_config_companies

    sync_config_companies(rt.engine, rt.cfg)
    queries.wake_snoozed(rt.engine)
    run_id, initial = dg.start_run(rt, trigger=trigger, sources=sources, max_to_score=max_to_score,
                                   companies=companies, skip_fetch=skip_fetch, skip_score=skip_score,
                                   posted_since=posted_since)
    rt.submit(lambda: _execute(rt, run_id, initial))
    return run_id


def resume_background(rt: Runtime, run_id: int) -> None:
    dg.prepare_resume(rt, run_id)
    rt.submit(lambda: _execute(rt, run_id, None))


def request_stop(rt: Runtime, run_id: int) -> None:
    run = queries.get_run(rt.engine, run_id)
    if run is None or run["status"] != "running":
        raise RunError(f"run {run_id} is not running")
    locks.request_stop(rt.engine, run_id, note="stop requested from the web")


# --------------------------------------------------------------------------------------------- dry runs


def start_dry_run(rt: Runtime, *, sources: list[str] | None, companies: list[str] | None) -> int:
    """Fetch + filter in memory for the given sources/companies. Writes nothing and calls no LLM."""
    with _DRY_LOCK:
        if any(d["status"] == "running" for d in rt.dry_runs.values()):
            raise RunError("a dry run is already in progress — wait for it or stop it")
        dry_id = max(rt.dry_runs, default=0) + 1
        entry = {"id": dry_id, "status": "running", "lines": [], "started_at": utcnow_iso(), "finished_at": None,
                 "sources": sources or [], "companies": companies or [], "stop": False, "summary": {}}
        rt.dry_runs[dry_id] = entry
        for old in sorted(rt.dry_runs)[:-MAX_DRY_RUNS_KEPT]:
            if rt.dry_runs[old]["status"] != "running":
                del rt.dry_runs[old]

    def work() -> None:
        try:
            srcs = rt.dry_sources(sources, companies)
            entry["summary"] = service.dry_run(rt.cfg, srcs, iso_shift(-rt.cfg.filters.max_age_days),
                                               echo=entry["lines"].append, should_stop=lambda: entry["stop"])
            entry["status"] = "complete"
        except service.RunCancelled:
            entry["lines"].append("stopped by user")
            entry["status"] = "stopped"
        except Exception as exc:  # shown on the dry-run page
            log.exception("dry run %s failed", dry_id)
            entry["lines"].append(f"failed: {exc}")
            entry["status"] = "failed"
        finally:
            entry["finished_at"] = utcnow_iso()

    rt.submit(work)
    return dry_id


def stop_dry_run(rt: Runtime, dry_id: int) -> None:
    entry = rt.dry_runs.get(dry_id)
    if entry is None or entry["status"] != "running":
        raise RunError(f"dry run {dry_id} is not running")
    entry["stop"] = True
