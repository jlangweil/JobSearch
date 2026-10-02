"""Score or re-score a single job from the web UI (background), and preview the exact scoring prompt."""

from __future__ import annotations

import logging

from jobhunt.db import locks, queries
from jobhunt.discovery import scoring, service
from jobhunt.graphs.runtime import Runtime
from jobhunt.sources.registry import is_truncated

log = logging.getLogger(__name__)


class ScoringError(ValueError):
    pass


def _lock_name(job_id: int) -> str:
    return f"score:job-{job_id}"


def _scorable(rt: Runtime, job_id: int) -> dict:
    job = queries.get_job(rt.engine, job_id)
    if job is None:
        raise ScoringError(f"job {job_id} not found")
    if not job.get("description_text"):
        raise ScoringError("this job's description is not stored (it was filtered out or is a duplicate), "
                           "so it cannot be scored")
    return job


def prompt_preview(rt: Runtime, job_id: int) -> str:
    job = _scorable(rt, job_id)
    return scoring.explain(job, rt.cfg, ghost=service.run_ghost_for(rt.engine, rt.cfg, job),
                           background=service.candidate_background(rt.engine, rt.cfg),
                           calibration=service.calibration(rt.engine), truncated=is_truncated(job))


def is_scoring(rt: Runtime, job_id: int) -> bool:
    return locks.is_held(rt.engine, _lock_name(job_id), rt.cfg.paths.lock_stale_minutes)


def score_background(rt: Runtime, job_id: int, provider: str | None = None, then=None) -> None:  # noqa: ANN001
    """Score one job in the background. `then(row)` runs after a successful score (e.g. tailor if it scores high)."""
    _scorable(rt, job_id)
    name = _lock_name(job_id)
    locks.acquire(rt.engine, name, note=f"scoring job {job_id}", stale_minutes=rt.cfg.paths.lock_stale_minutes)
    key = f"score-{job_id}"
    rt.failures.pop(key, None)

    def work() -> None:
        try:
            row = service.rescore_one(rt.engine, rt.cfg, rt.llm(provider), job_id)
            if then is not None:
                then(row)
        except Exception as exc:  # shown on the job page
            log.exception("scoring job %s failed", job_id)
            rt.failures[key] = f"{type(exc).__name__}: {exc}"
        finally:
            locks.release(rt.engine, name)

    rt.submit(work)


# --------------------------------------------------------------------------------------------- bulk rescoring

BATCH_LOCK = "score:batch"
MAX_BATCH = 2000


def start_batch(rt: Runtime, job_ids: list[int], *, provider: str | None, label: str) -> int:
    """Score or re-score a list of jobs in the background with the chosen model. One batch at a time.
    Jobs keep their status (a 'discovered' job becomes 'scored'); every attempt is added to score history."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from jobhunt.db.engine import utcnow_iso

    ids = [i for i in dict.fromkeys(job_ids)][:MAX_BATCH]
    if not ids:
        raise ScoringError("no jobs with a stored description match these filters")
    llm = rt.llm(provider)
    locks.acquire(rt.engine, BATCH_LOCK, note=f"rescoring {len(ids)} jobs",
                  stale_minutes=rt.cfg.paths.lock_stale_minutes)
    batch_id = max(rt.batches, default=0) + 1
    entry = {"id": batch_id, "label": label, "status": "running", "total": len(ids), "done": 0, "failed": 0,
             "raised": 0, "lowered": 0, "errors": [], "changes": [], "started_at": utcnow_iso(), "finished_at": None,
             "stop": False, "model": llm.model_label("score") if hasattr(llm, "model_label") else provider}
    rt.batches[batch_id] = entry

    def one(job_id: int) -> tuple[int, dict | None, dict]:
        before = queries.latest_score(rt.engine, job_id)
        row = service.rescore_one(rt.engine, rt.cfg, llm, job_id)
        return job_id, before, row

    def work() -> None:
        try:
            if hasattr(llm, "preflight"):
                llm.preflight("score")
            workers = llm.concurrency("score") if hasattr(llm, "concurrency") else rt.cfg.scoring.concurrency
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                futures = {pool.submit(one, job_id): job_id for job_id in ids}
                for fut in as_completed(futures):
                    if entry["stop"]:
                        for f in futures:
                            f.cancel()  # queued jobs are dropped; ones already running finish
                    if fut.cancelled():
                        continue
                    job_id = futures[fut]
                    try:
                        _, before, row = fut.result()
                        old = before["total"] if before else None
                        entry["changes"].append({"id": job_id, "old": old, "new": row["total"]})
                        if old is not None and row["total"] > old:
                            entry["raised"] += 1
                        elif old is not None and row["total"] < old:
                            entry["lowered"] += 1
                    except Exception as exc:  # recorded per job; the batch continues
                        entry["failed"] += 1
                        entry["errors"].append(f"job {job_id}: {type(exc).__name__}: {exc}"[:300])
                    entry["done"] += 1
                    locks.heartbeat(rt.engine, BATCH_LOCK)
            entry["status"] = "stopped" if entry["stop"] else "complete"
        except Exception as exc:  # e.g. local model unreachable
            log.exception("batch %s failed", batch_id)
            entry["errors"].insert(0, f"{type(exc).__name__}: {exc}")
            entry["status"] = "failed"
        finally:
            entry["finished_at"] = utcnow_iso()
            locks.release(rt.engine, BATCH_LOCK)

    rt.submit(work)
    return batch_id


def stop_batch(rt: Runtime, batch_id: int) -> None:
    entry = rt.batches.get(batch_id)
    if entry is None or entry["status"] != "running":
        raise ScoringError(f"batch {batch_id} is not running")
    entry["stop"] = True


def latest_score_view(rt: Runtime, job_id: int) -> dict | None:
    """The latest score, with each checked technology showing the owner's current rating (ratings change after
    scoring; the stored skill_match is kept in step by recompute_totals)."""
    score = queries.latest_score(rt.engine, job_id)
    stack = ((score or {}).get("analysis") or {}).get("stack") or []
    if stack:
        levels = queries.skill_level_map(rt.engine)
        for t in stack:
            t["your_level"] = scoring.level_for(t["technology"], levels)
    return score
