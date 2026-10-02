from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from jobhunt.db import queries
from jobhunt.db.locks import LockHeld
from jobhunt.services import companies as company_service
from jobhunt.services import scheduler as scheduler_service
from jobhunt.services import schedules
from jobhunt.sources.normalize import slugify
from jobhunt.sources.registry import ALL_SOURCES, enabled_sources
from jobhunt.web.common import is_htmx, redirect, render, rt

router = APIRouter()


def _get(schedule_id: int) -> dict:
    s = queries.get_schedule(rt().engine, schedule_id)
    if s is None:
        raise HTTPException(404, "schedule not found")
    return s


def _decorate(s: dict) -> dict:
    s = dict(s)
    s["describe"] = schedules.describe(s)
    s["scope"] = schedules.scope(s)
    s["mode_label"] = schedules.MODES.get(s["mode"], s["mode"])
    try:
        nxt = schedules.upcoming(s, count=1)
        s["next"] = nxt[0] if nxt else None
        s["trigger_error"] = None
    except schedules.ScheduleError as exc:
        s["next"], s["trigger_error"] = None, str(exc)
    return s


def _form_context(values: dict, errors: list[str], schedule: dict | None = None) -> dict:
    cfg = rt().cfg
    grouped: dict[str, list[dict]] = {}
    chosen = set(values.get("companies") or [])
    for c in cfg.companies:
        if c.active:
            slug = slugify(c.name)
            grouped.setdefault(c.industry or "other", []).append(
                {"name": c.name, "slug": slug, "ats": c.ats_type, "checked": slug in chosen})
    preview = None
    try:
        preview = {"describe": schedules.describe(values), "next": schedules.upcoming(values, count=5)}
    except (schedules.ScheduleError, ValueError, TypeError, IndexError):
        preview = None
    return {"v": values, "errors": errors, "schedule": schedule, "grouped": dict(sorted(grouped.items())),
            "industries": company_service.industries(cfg), "all_sources": ALL_SOURCES,
            "enabled": enabled_sources(cfg), "days": schedules.DAYS, "day_labels": schedules.DAY_LABELS,
            "modes": schedules.MODES, "timezones": schedules.COMMON_TIMEZONES, "preview": preview,
            "max_per_run": cfg.scoring.max_per_run, "presets": schedules.PRESETS,
            "runs": queries.runs_for_schedule(rt().engine, schedule["id"]) if schedule else []}


@router.get("/schedules")
def schedule_list(request: Request):  # noqa: ANN201
    r = rt()
    rows = [_decorate(s) for s in queries.list_schedules(r.engine)]
    return render(request, "schedules.html", {
        "rows": rows, "paused": schedules.is_paused(r.engine), "upcoming": scheduler_service.next_runs(r, limit=8),
        "scheduler_active": scheduler_service.active(r), "presets": schedules.PRESETS,
        "waiting": len(queries.job_ids_with_status(r.engine, "discovered")),
    })


@router.get("/schedules/new")
def schedule_new(request: Request):  # noqa: ANN201
    r = rt()
    copy = request.query_params.get("copy")
    if copy and copy.isdigit():
        values = schedules.copy_values(_get(int(copy)))
    else:
        values = schedules.default_values(r.cfg, request.query_params.get("preset"))
    return render(request, "schedule_form.html", _form_context(values, []))


@router.post("/schedules/preview")
async def schedule_preview(request: Request):  # noqa: ANN201
    values, errors = schedules.values_from_form(rt().cfg, await request.form())
    errors = [e for e in errors if not e.startswith("Give the schedule a name")]
    ctx = _form_context(values, errors)
    return render(request, "fragments/schedule_preview.html", ctx)


@router.post("/schedules")
async def schedule_create(request: Request):  # noqa: ANN201
    r = rt()
    values, errors = schedules.values_from_form(r.cfg, await request.form())
    if errors:
        return render(request, "schedule_form.html", _form_context(values, errors), status_code=422)
    schedule_id = schedules.save(r, values)
    state = "enabled" if values["enabled"] else "saved (disabled)"
    return redirect("/schedules", message=f"Schedule “{values['name']}” {state}.", highlight=schedule_id)


@router.post("/schedules/pause")
async def schedule_pause(request: Request):  # noqa: ANN201
    form = await request.form()
    paused = form.get("paused") == "1"
    schedules.set_paused(rt(), paused)
    return redirect("/schedules", message="All schedules paused. Nothing runs automatically until you resume."
                    if paused else "Schedules resumed.")


@router.get("/schedules/{schedule_id}")
def schedule_edit(request: Request, schedule_id: int):  # noqa: ANN201
    s = _get(schedule_id)
    values = {k: s.get(k) for k in queries.SCHEDULE_FIELDS}
    return render(request, "schedule_form.html", _form_context(values, [], _decorate(s)))


@router.post("/schedules/{schedule_id}")
async def schedule_update(request: Request, schedule_id: int):  # noqa: ANN201
    r = rt()
    s = _get(schedule_id)
    values, errors = schedules.values_from_form(r.cfg, await request.form())
    if errors:
        return render(request, "schedule_form.html", _form_context(values, errors, _decorate(s)), status_code=422)
    schedules.save(r, values, schedule_id)
    return redirect("/schedules", message=f"Schedule “{values['name']}” saved.", highlight=schedule_id)


@router.post("/schedules/{schedule_id}/toggle")
async def schedule_toggle(request: Request, schedule_id: int):  # noqa: ANN201
    r = rt()
    s = _get(schedule_id)
    form = await request.form()
    enabled = form.get("enabled") == "1" if "enabled" in form else not s["enabled"]
    schedules.set_enabled(r, schedule_id, enabled)
    if is_htmx(request):
        row = _decorate(queries.get_schedule(r.engine, schedule_id))  # type: ignore[arg-type]
        return render(request, "fragments/schedule_row.html", {"s": row, "paused": schedules.is_paused(r.engine)})
    return redirect("/schedules", message=f"“{s['name']}” {'enabled' if enabled else 'disabled'}.")


@router.post("/schedules/{schedule_id}/run")
def schedule_run_now(schedule_id: int):  # noqa: ANN201
    s = _get(schedule_id)
    try:
        run_id = schedules.fire(rt(), schedule_id, manual=True)
    except LockHeld as exc:
        return redirect("/schedules", error=f"Another run is in progress, so “{s['name']}” didn't start: {exc}")
    except Exception as exc:  # noqa: BLE001 - shown to the user
        return redirect("/schedules", error=f"“{s['name']}” could not start: {exc}")
    if run_id is None:
        latest = queries.get_schedule(rt().engine, schedule_id) or {}
        return redirect("/schedules", message=f"“{s['name']}”: {latest.get('last_result') or 'nothing to do'}.")
    return redirect(f"/runs/{run_id}", message=f"Started “{s['name']}” now.")


@router.post("/schedules/{schedule_id}/delete")
def schedule_delete(schedule_id: int):  # noqa: ANN201
    s = _get(schedule_id)
    schedules.delete(rt(), schedule_id)
    return redirect("/schedules", message=f"Deleted “{s['name']}”. Its past runs stay in Run history.")
