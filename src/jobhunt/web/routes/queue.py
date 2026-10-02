from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from jobhunt.db import queries
from jobhunt.db.engine import utcnow_iso
from jobhunt.db.export import xlsx_bytes
from jobhunt.db.locks import LockHeld
from jobhunt.services import scoring_actions
from jobhunt.services.search import hybrid_search
from jobhunt.web.common import is_htmx, redirect, render, rt

router = APIRouter()

DEFAULT_QUEUE = "/queue?status=scored&min_score=70&sort=total"
FILTER_KEYS = ("q", "min_score", "max_ghost", "status", "remote", "employment", "company", "source", "scorer",
               "run", "added", "scored", "retrieved", "sort", "page")
# "last N" choices for the Scored and Retrieved filters: query value -> (days, label)
WITHIN = {"1": (1, "last 24 hours"), "3": (3, "last 3 days"), "7": (7, "last 7 days"), "14": (14, "last 14 days"),
          "30": (30, "last 30 days")}
STATUSES = ["scored", "shortlisted", "in_workflow", "ready", "applied", "responded", "snoozed", "rejected",
            "abandoned", "closed", "discovered", "filtered_out", "duplicate", "any"]


def _float(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _added_run(value: str) -> int | None:
    """?added=latest follows whichever run is newest, so a bookmark keeps meaning "the last run"."""
    if value == "latest":
        return queries.latest_run_id(rt().engine) or -1  # no runs yet: match nothing rather than everything
    return int(_float(value)) if _float(value) else None


def filters_from(request: Request) -> tuple[queries.QueueFilters, dict]:
    return filters_from_mapping(request.query_params)


def filters_from_mapping(p) -> tuple[queries.QueueFilters, dict]:  # noqa: ANN001
    params = {k: p.get(k, "") for k in FILTER_KEYS}
    f = queries.QueueFilters(
        q=params["q"] or None,
        min_score=_float(params["min_score"]),
        max_ghost=int(_float(params["max_ghost"])) if _float(params["max_ghost"]) is not None else None,
        status=params["status"] or "scored",
        remote=params["remote"] or None,
        employment=params["employment"] or None,
        company=params["company"] or None,
        source=params["source"] or None,
        scorer=params["scorer"] if params["scorer"] in ("claude", "local") else None,
        scored_in_run=int(_float(params["run"])) if _float(params["run"]) else None,
        added_in_run=_added_run(params["added"]),
        scored_within_days=WITHIN[params["scored"]][0] if params["scored"] in WITHIN else None,
        retrieved_within_days=WITHIN[params["retrieved"]][0] if params["retrieved"] in WITHIN else None,
        sort=params["sort"] if params["sort"] in queries.QUEUE_SORTS else "total",
        page=max(1, int(_float(params["page"]) or 1)),
    )
    return f, params


@router.get("/queue")
def queue(request: Request):  # noqa: ANN201
    if not request.query_params:
        return redirect(DEFAULT_QUEUE)
    f, params = filters_from(request)
    rows, total = queries.list_queue(rt().engine, f)
    running_batch = next((b for b in rt().batches.values() if b["status"] == "running"), None)
    ctx = {"rows": rows, "total": total, "params": params, "filters": f, "statuses": STATUSES,
           "running_batch": running_batch, "api_key_set": bool(rt().cfg.secrets.ANTHROPIC_API_KEY),
           "cfg_provider": rt().cfg.scoring.provider, "recent_runs": queries.list_runs(rt().engine, limit=10),
           "within": WITHIN,
           "sorts": queries.SORT_LABELS, "pages": max(1, -(-total // f.page_size)),
           "current_url": str(request.url.path) + "?" + str(request.url.query)}
    if is_htmx(request) and request.headers.get("HX-Target") == "queue-rows":
        return render(request, "fragments/queue_rows.html", ctx)
    return render(request, "queue.html", ctx)


@router.get("/queue/export")
def queue_export(request: Request) -> Response:
    f, _ = filters_from(request)
    rows, _ = queries.list_queue(rt().engine, f, paginate=False)
    name = f"jobhunt-queue-{utcnow_iso()[:10]}.xlsx"
    return Response(xlsx_bytes(rows), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/search")
def search(request: Request):  # noqa: ANN201
    q = request.query_params.get("q", "")
    results = hybrid_search(rt(), q) if q.strip() else []
    return render(request, "search.html", {"q": q, "rows": results})


# --------------------------------------------------------------------------------------------- bulk rescoring


@router.post("/scoring/batches")
async def start_rescore(request: Request):  # noqa: ANN201
    form = await request.form()
    r = rt()
    back = "/queue?" + str(form.get("filters") or "")
    f, params = filters_from_mapping({k: str(form.get(k) or "") for k in FILTER_KEYS})
    rows, _ = queries.list_queue(r.engine, f, paginate=False)
    try:
        limit = max(1, int(form.get("limit") or len(rows) or 1))
    except ValueError:
        limit = len(rows)
    ids = [row["id"] for row in rows if row.get("description_text")][:limit]
    provider = str(form.get("provider") or "")
    provider = provider if provider in ("anthropic", "local") else None
    if provider == "anthropic" and not r.cfg.secrets.ANTHROPIC_API_KEY:
        return redirect(back, error="Claude needs ANTHROPIC_API_KEY in .env (then restart jobhunt serve).")
    label = ", ".join(f"{k} {v}" for k, v in params.items() if v and k != "page") or "all jobs"
    try:
        batch_id = scoring_actions.start_batch(r, ids, provider=provider, label=label)
    except LockHeld:
        return redirect(back, error="A rescoring batch is already running; its link is at the top of the queue.")
    except Exception as exc:  # noqa: BLE001 - e.g. no matching jobs
        return redirect(back, error=str(exc))
    return redirect(f"/scoring/batches/{batch_id}")


def _batch(batch_id: int) -> dict:
    from fastapi import HTTPException

    entry = rt().batches.get(batch_id)
    if entry is None:
        raise HTTPException(404, "batch not found (batches are kept in memory until the server restarts)")
    return entry


@router.get("/scoring/batches/{batch_id}")
def batch_page(request: Request, batch_id: int):  # noqa: ANN201
    return render(request, "batch.html", {"b": _batch(batch_id)})


@router.get("/scoring/batches/{batch_id}/progress")
def batch_progress(request: Request, batch_id: int):  # noqa: ANN201
    return render(request, "fragments/batch_progress.html", {"b": _batch(batch_id)})


@router.post("/scoring/batches/{batch_id}/stop")
def batch_stop(batch_id: int):  # noqa: ANN201
    try:
        scoring_actions.stop_batch(rt(), batch_id)
    except scoring_actions.ScoringError as exc:
        return redirect(f"/scoring/batches/{batch_id}", error=str(exc))
    return redirect(f"/scoring/batches/{batch_id}", message="Stopping: jobs already being scored will finish.")
