"""discovery_graph: ingest -> normalize -> dedupe -> prefilter -> ghost -> score -> END, checkpointed so a
failed run resumes from its last completed node."""

from __future__ import annotations

import logging
import os
from datetime import datetime

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from jobhunt.db import locks, queries
from jobhunt.db.engine import iso_shift, parse_iso
from jobhunt.graphs.discovery import nodes
from jobhunt.graphs.discovery.state import DiscoveryState
from jobhunt.graphs.runtime import Runtime

log = logging.getLogger(__name__)

NODES = ("ingest", "normalize", "dedupe", "prefilter", "ghost", "score")


def build_graph(checkpointer: BaseCheckpointSaver):  # noqa: ANN201
    g = StateGraph(DiscoveryState)
    for name in NODES:
        g.add_node(name, getattr(nodes, name))
    g.add_edge(START, NODES[0])
    for a, b in zip(NODES, NODES[1:], strict=False):
        g.add_edge(a, b)
    g.add_edge(NODES[-1], END)
    return g.compile(checkpointer=checkpointer)


def thread_config(run_id: int) -> dict:
    return {"configurable": {"thread_id": f"run-{run_id}"}}


class RunNotResumable(RuntimeError):
    pass


def _mark_orphans_failed(rt: Runtime) -> int:
    n = 0
    for run in queries.list_runs(rt.engine, limit=20, status="running"):
        errors = run["errors"] + [{"source": None, "node": run.get("current_node"),
                                   "message": "process exited before the run finished", "traceback_head": ""}]
        queries.update_run_progress(rt.engine, run["id"], status="failed", errors=errors, finished=True)
        n += 1
    return n


def reconcile_orphans(rt: Runtime) -> int:
    """Runs left 'running' by a process that died are marked failed so they can be resumed."""
    if locks.is_held(rt.engine, locks.DISCOVERY_LOCK, rt.cfg.paths.lock_stale_minutes):
        return 0
    return _mark_orphans_failed(rt)


def start_run(rt: Runtime, *, trigger: str, sources: list[str] | None,
              max_to_score: int | None, companies: list[str] | None = None,
              skip_fetch: bool = False, skip_score: bool = False,
              posted_since: str | None = None) -> tuple[int, DiscoveryState]:
    """Acquire the discovery lock and create the run row. Raises LockHeld if a run is in progress.
    Returns (run_id, initial_state) for `execute`."""
    locks.acquire(rt.engine, locks.DISCOVERY_LOCK, note=f"{trigger} pid {os.getpid()}",
                  stale_minutes=rt.cfg.paths.lock_stale_minutes)
    try:
        _mark_orphans_failed(rt)  # we hold the lock, so anything still 'running' is orphaned
        run_id = queries.create_run(rt.engine, trigger, sources or [])
    except Exception:
        locks.release(rt.engine, locks.DISCOVERY_LOCK)
        raise
    rt.failures.pop(f"run-{run_id}", None)
    initial: DiscoveryState = {
        "run_id": run_id,
        "sources": list(sources or []),
        # a run date later than the usual window narrows what the boards are asked for
        "since": max(iso_shift(-rt.cfg.filters.max_age_days), posted_since or ""),
        "posted_since": posted_since or None,
        "fetched_job_ids": [],
        "candidate_job_ids": [],
        "counts": {},
        "errors": [],
        "max_to_score": int(max_to_score or rt.cfg.scoring.max_per_run),
        "ghost_scores": {},
        "companies": list(companies or []),
        "skip_fetch": bool(skip_fetch),
        "skip_score": bool(skip_score),
    }
    return run_id, initial


def prepare_resume(rt: Runtime, run_id: int) -> None:
    run = queries.get_run(rt.engine, run_id)
    if run is None:
        raise RunNotResumable(f"run {run_id} not found")
    reconcile_orphans(rt)
    run = queries.get_run(rt.engine, run_id)
    assert run is not None
    if run["status"] != "failed":
        raise RunNotResumable(f"run {run_id} is {run['status']}; only failed runs can be resumed")
    snapshot = rt.graph("discovery").get_state(thread_config(run_id))
    if not snapshot.next:
        raise RunNotResumable(f"run {run_id} has no pending checkpoint to resume from")
    locks.acquire(rt.engine, locks.DISCOVERY_LOCK, note=f"resume run {run_id} pid {os.getpid()}",
                  stale_minutes=rt.cfg.paths.lock_stale_minutes)
    locks.clear_stop(rt.engine, run_id)
    queries.update_run_progress(rt.engine, run_id, status="running", reopen=True)


def execute(rt: Runtime, run_id: int, initial: DiscoveryState | None = None) -> dict:
    """Run the graph from `initial`, or resume from the last checkpoint when `initial` is None.
    Assumes the discovery lock is held; always releases it."""
    graph = rt.graph("discovery")
    cfg = thread_config(run_id)
    try:
        result = graph.invoke(initial, cfg)
        queries.update_run_progress(rt.engine, run_id, node="done", counts=result.get("counts", {}),
                                    errors=result.get("errors", []), status="complete", finished=True)
        return result
    except BaseException as exc:
        log.exception("discovery run %s failed", run_id)
        run = queries.get_run(rt.engine, run_id) or {"errors": []}
        errors = list(run.get("errors") or []) + [{"source": None, "node": run.get("current_node"),
                                                   "message": f"{type(exc).__name__}: {exc}",
                                                   "traceback_head": ""}]
        queries.update_run_progress(rt.engine, run_id, errors=errors, status="failed", finished=True)
        rt.failures[f"run-{run_id}"] = str(exc)
        raise
    finally:
        locks.clear_stop(rt.engine, run_id)
        locks.release(rt.engine, locks.DISCOVERY_LOCK)


def run_options(rt: Runtime, run_id: int) -> dict:
    """The options a run was started with (from its first checkpoint)."""
    values = rt.graph("discovery").get_state(thread_config(run_id)).values or {}
    return {"companies": values.get("companies") or [], "skip_fetch": bool(values.get("skip_fetch")),
            "skip_score": bool(values.get("skip_score")),
            "max_to_score": values.get("max_to_score"), "sources": values.get("sources") or []}


def timeline(rt: Runtime, run_id: int) -> list[dict]:
    """Node timeline with durations, derived from checkpoint history (each checkpoint marks a node boundary)."""
    graph = rt.graph("discovery")
    history = list(graph.get_state_history(thread_config(run_id)))
    history.reverse()  # oldest first
    run = queries.get_run(rt.engine, run_id) or {}
    out = []
    prev_ts: datetime | None = parse_iso(run.get("started_at"))
    for i, snap in enumerate(history):
        if not snap.next or snap.next[0] not in NODES:
            continue
        finished = parse_iso(history[i + 1].created_at) if i + 1 < len(history) else None
        started = parse_iso(snap.created_at) or prev_ts
        out.append({
            "node": snap.next[0],
            "started_at": snap.created_at,
            "finished_at": history[i + 1].created_at if finished else None,
            "seconds": round((finished - started).total_seconds(), 1) if finished and started else None,
        })
        prev_ts = finished
    return out
