from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from jobhunt.db import locks, queries
from jobhunt.db.engine import iso_date_to_iso, utcnow_iso
from jobhunt.db.locks import LockHeld
from jobhunt.graphs.discovery import graph as dg
from jobhunt.services import companies as company_service
from jobhunt.services import runs as run_service
from jobhunt.sources.normalize import slugify
from jobhunt.sources.registry import ALL_SOURCES, enabled_sources
from jobhunt.web.common import redirect, render, rt

router = APIRouter()

MODES = ("full", "fetch", "dry", "stored")


@router.get("/runs")
def runs(request: Request):  # noqa: ANN201
    r = rt()
    cfg = r.cfg
    preselected = set(request.query_params.getlist("company"))
    grouped: dict[str, list[dict]] = {}
    for c in cfg.companies:
        if c.active:
            grouped.setdefault(c.industry or "other", []).append(
                {"name": c.name, "slug": slugify(c.name), "ats": c.ats_type, "checked": slugify(c.name) in preselected})
    pending = len(queries.job_ids_with_status(r.engine, "discovered"))
    return render(request, "runs.html", {
        "runs": queries.list_runs(r.engine, limit=50), "all_sources": ALL_SOURCES, "enabled": enabled_sources(cfg),
        "schedule_names": {s["id"]: s["name"] for s in queries.list_schedules(r.engine)},
        "max_per_run": cfg.scoring.max_per_run, "today": utcnow_iso()[:10],
        "industries": company_service.industries(cfg), "grouped": grouped,
        "preselected": preselected, "mode": request.query_params.get("mode", "full"), "pending": pending,
        "dry_runs": sorted(r.dry_runs.values(), key=lambda d: -d["id"]),
        "api_key_set": bool(cfg.secrets.ANTHROPIC_API_KEY),
    })


@router.post("/runs")
async def start_run(request: Request):  # noqa: ANN201
    form = await request.form()
    r = rt()
    mode = form.get("mode") if form.get("mode") in MODES else "full"
    sources = [s for s in form.getlist("sources") if s in ALL_SOURCES] or None
    selected = company_service.resolve_selection(r.cfg, form.getlist("industries"), form.getlist("companies"))
    try:
        max_to_score = int(form.get("max_to_score") or 0) or None
    except ValueError:
        max_to_score = None
    try:  # "only jobs posted since" (YYYY-MM-DD from a date picker); blank = no limit
        posted_since = iso_date_to_iso(str(form.get("posted_since"))) if form.get("posted_since") else None
    except ValueError:
        return redirect("/runs", error="That date isn't valid; pick one with the date picker (YYYY-MM-DD).")
    try:
        if mode == "dry":
            dry_id = run_service.start_dry_run(r, sources=sources, companies=selected)
            return redirect(f"/dryruns/{dry_id}")
        run_id = run_service.start_background(r, trigger="web", sources=sources, max_to_score=max_to_score,
                                              companies=selected, skip_fetch=mode == "stored",
                                              skip_score=mode == "fetch", posted_since=posted_since)
    except LockHeld as exc:
        return redirect("/runs", error=f"A discovery run is already in progress: {exc}")
    except (run_service.RunError, ValueError) as exc:
        return redirect("/runs", error=str(exc))
    return redirect(f"/runs/{run_id}")


# --------------------------------------------------------------------------------------------- dry runs


def _dry(dry_id: int) -> dict:
    entry = rt().dry_runs.get(dry_id)
    if entry is None:
        raise HTTPException(404, "dry run not found (dry runs are kept in memory until the server restarts)")
    return entry


@router.get("/dryruns/{dry_id}")
def dry_run_page(request: Request, dry_id: int):  # noqa: ANN201
    return render(request, "dry_run.html", {"dry": _dry(dry_id)})


@router.get("/dryruns/{dry_id}/progress")
def dry_run_progress(request: Request, dry_id: int):  # noqa: ANN201
    return render(request, "fragments/dry_progress.html", {"dry": _dry(dry_id)})


@router.post("/dryruns/{dry_id}/stop")
def dry_run_stop(dry_id: int):  # noqa: ANN201
    try:
        run_service.stop_dry_run(rt(), dry_id)
    except run_service.RunError as exc:
        return redirect(f"/dryruns/{dry_id}", error=str(exc))
    return redirect(f"/dryruns/{dry_id}", message="stopping after the current request")


# --------------------------------------------------------------------------------------------- run detail


def _run_context(run_id: int) -> dict:
    r = rt()
    run = queries.get_run(r.engine, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return {"run": run, "timeline": dg.timeline(r, run_id), "nodes": dg.NODES, "options": dg.run_options(r, run_id),
            "stop_requested": locks.stop_requested(r.engine, run_id),
            "scored_count": queries.run_scored_count(r.engine, run_id),
            "waiting_now": len(queries.job_ids_with_status(r.engine, "discovered")),
            "filter_reasons": sorted(((k[len("filtered_"):].replace("_", " "), v) for k, v in run["counts"].items()
                                      if k.startswith("filtered_")), key=lambda kv: -kv[1])}


@router.get("/runs/{run_id}")
def run_detail(request: Request, run_id: int):  # noqa: ANN201
    return render(request, "run_detail.html", _run_context(run_id))


@router.get("/runs/{run_id}/progress")
def run_progress(request: Request, run_id: int):  # noqa: ANN201
    return render(request, "fragments/run_progress.html", _run_context(run_id))


@router.post("/runs/{run_id}/resume")
def resume_run(run_id: int):  # noqa: ANN201
    try:
        run_service.resume_background(rt(), run_id)
    except (LockHeld, dg.RunNotResumable) as exc:
        return redirect(f"/runs/{run_id}", error=str(exc))
    return redirect(f"/runs/{run_id}")


@router.post("/runs/{run_id}/stop")
def stop_run(run_id: int):  # noqa: ANN201
    try:
        run_service.request_stop(rt(), run_id)
    except run_service.RunError as exc:
        return redirect(f"/runs/{run_id}", error=str(exc))
    return redirect(f"/runs/{run_id}", message="Stop requested — the run stops at its next check (usually within "
                                               "seconds) and can be resumed later.")
