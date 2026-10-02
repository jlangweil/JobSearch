"""Thin discovery nodes: read state, call one service function, record progress, write state."""

from __future__ import annotations

from jobhunt.db import locks, queries
from jobhunt.discovery import service
from jobhunt.embeddings import EmbedderUnavailable
from jobhunt.graphs.discovery.state import DiscoveryState
from jobhunt.graphs.runtime import get_runtime


def _should_stop(state: DiscoveryState):  # noqa: ANN202
    rt = get_runtime()
    return lambda: locks.stop_requested(rt.engine, state["run_id"])


def _progress(state: DiscoveryState, node: str, counts: dict, errors: list) -> None:
    rt = get_runtime()
    queries.update_run_progress(rt.engine, state["run_id"], node=node, counts=counts, errors=errors)
    locks.heartbeat(rt.engine, locks.DISCOVERY_LOCK)


def ingest(state: DiscoveryState) -> dict:
    rt = get_runtime()
    counts, errors = dict(state.get("counts") or {}), list(state.get("errors") or [])
    _progress(state, "ingest", counts, errors)
    if state.get("skip_fetch"):
        sources = []  # "process stored jobs only": ingest just collects jobs still waiting in 'discovered'
    else:
        try:
            sources = rt.sources(state.get("sources") or None, state.get("companies") or None)
        except Exception as exc:  # misconfigured source: record and continue with nothing fetched
            errors.append({"source": None, "node": "ingest", "message": str(exc), "traceback_head": ""})
            sources = []

    def on_progress(c: dict) -> None:
        queries.update_run_progress(rt.engine, state["run_id"], counts={**counts, **c})
        locks.heartbeat(rt.engine, locks.DISCOVERY_LOCK)

    ids, c, errs = service.ingest(rt.engine, sources, state.get("since"), on_progress, _should_stop(state),
                                  posted_since=state.get("posted_since"))
    if state.get("skip_fetch") and state.get("companies"):
        ids = service.restrict_to_companies(rt.engine, rt.cfg, ids, state["companies"])
    counts.update(c)
    counts["processing"] = len(ids)  # new/changed postings plus jobs left 'discovered' by earlier runs
    errors.extend(errs)
    _progress(state, "ingest", counts, errors)
    return {"fetched_job_ids": ids, "counts": counts, "errors": errors}


def normalize(state: DiscoveryState) -> dict:
    rt = get_runtime()
    counts, errors = dict(state["counts"]), list(state["errors"])
    _progress(state, "normalize", counts, errors)
    try:
        counts["embedded"] = service.normalize(rt.engine, state["fetched_job_ids"], rt.embedder(),
                                               should_stop=_should_stop(state))
    except EmbedderUnavailable as exc:
        # Windows blocked torch: carry on without embeddings (exact-copy dedupe still runs); the jobs stay
        # un-embedded, so a later run embeds them once the block lifts.
        counts["embedded"] = 0
        errors.append({"source": None, "node": "normalize", "traceback_head": "",
                       "message": f"{exc} Near-duplicate detection was skipped this run."})
    _progress(state, "normalize", counts, errors)
    return {"counts": counts, "errors": errors}


def dedupe(state: DiscoveryState) -> dict:
    rt = get_runtime()
    counts, errors = dict(state["counts"]), list(state["errors"])
    _progress(state, "dedupe", counts, errors)
    counts["duplicates"] = service.run_dedupe(rt.engine, state["fetched_job_ids"],
                                              keep_content=rt.cfg.filters.keep_filtered_descriptions)
    _progress(state, "dedupe", counts, errors)
    return {"counts": counts}


def prefilter(state: DiscoveryState) -> dict:
    rt = get_runtime()
    counts, errors = dict(state["counts"]), list(state["errors"])
    _progress(state, "prefilter", counts, errors)
    survivors, filtered, reasons = service.run_prefilter(rt.engine, rt.cfg, state["fetched_job_ids"])
    counts["filtered"] = filtered
    counts["left_to_score"] = len(survivors)
    for k, v in reasons.items():
        counts[f"filtered_{k}"] = v
    _progress(state, "prefilter", counts, errors)
    return {"candidate_job_ids": survivors, "counts": counts}


def ghost(state: DiscoveryState) -> dict:
    rt = get_runtime()
    counts, errors = dict(state["counts"]), list(state["errors"])
    _progress(state, "ghost", counts, errors)
    ghosts = service.run_ghost(rt.engine, rt.cfg, state["candidate_job_ids"])
    _progress(state, "ghost", counts, errors)
    return {"ghost_scores": ghosts}


def score(state: DiscoveryState) -> dict:
    rt = get_runtime()
    counts, errors = dict(state["counts"]), list(state["errors"])
    counts.setdefault("scored", 0)
    counts.setdefault("above_threshold", 0)
    candidates = state.get("candidate_job_ids") or []
    without_text = service.count_without_posting_text(rt.engine, rt.cfg, candidates)
    if without_text:
        counts["need_posting_text"] = without_text
    counts["left_to_score"] = len(candidates) - without_text
    if state.get("skip_score"):
        # no model calls, but snippet-only postings still get their full text now, so a job waiting to be scored
        # shows (and later scores on) the whole posting
        waiting = [j for j in queries.get_jobs(rt.engine, candidates, status="discovered")
                   if service.has_posting_text(j, rt.cfg) or service.needs_full_text(j)]
        kept, errs = service.complete_descriptions(rt.engine, rt.cfg, waiting, should_stop=_should_stop(state))
        errors.extend(errs)
        counts["waiting_to_score"] = len(candidates) - (len(waiting) - len(kept))
        _progress(state, "score", counts, errors)
        return {"counts": counts, "errors": errors}
    _progress(state, "score", counts, errors)
    if not candidates:
        return {"counts": counts}
    base_scored = counts["scored"]

    def on_scored(n: int) -> None:
        queries.update_run_progress(rt.engine, state["run_id"], counts={**counts, "scored": base_scored + n})
        locks.heartbeat(rt.engine, locks.DISCOVERY_LOCK)

    scored, above, errs = service.run_scoring(rt.engine, rt.cfg, rt.llm(), candidates,
                                              state.get("ghost_scores") or {}, state["max_to_score"], on_scored,
                                              should_stop=_should_stop(state))
    counts["scored"] = base_scored + scored
    counts["above_threshold"] += above
    errors.extend(errs)
    _progress(state, "score", counts, errors)
    return {"counts": counts, "errors": errors}
