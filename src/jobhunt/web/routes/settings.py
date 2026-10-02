from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Form, Request

from jobhunt.db import queries
from jobhunt.db.engine import iso_shift
from jobhunt.services import config_edit
from jobhunt.services import scheduler as scheduler_service
from jobhunt.services import settings as settings_service
from jobhunt.sources.registry import enabled_sources
from jobhunt.web.common import redirect, render, rt

router = APIRouter()



@router.get("/settings")
def settings(request: Request):  # noqa: ANN201
    r = rt()
    cfg = r.cfg
    spend = queries.spend_by_day(r.engine, iso_shift(-30))
    by_day: dict[str, float] = {}
    for row in spend:
        by_day[row["day"]] = round(by_day.get(row["day"], 0.0) + (row["cost_usd"] or 0.0), 4)
    return render(request, "settings.html", {
        "cfg": cfg,
        "weights": cfg.scoring.weights,
        "subscores": queries.WEIGHT_KEYS,
        "spend": spend,
        "spend_by_day": by_day,
        "total_spend": queries.total_spend(r.engine),
        "enabled_sources": enabled_sources(cfg),
        "companies_by_ats": Counter(c.ats_type for c in cfg.companies if c.active),
        "status_counts": queries.status_counts(r.engine),
        "source_counts": queries.source_counts(r.engine),
        "next_scheduled": scheduler_service.next_runs(r, limit=1),
        "db_mb": round(cfg.paths.db.stat().st_size / 1e6, 1) if cfg.paths.db.exists() else 0,
        "locks": queries.list_locks(r.engine),
        "api_key_set": bool(cfg.secrets.ANTHROPIC_API_KEY),
        "test": r.llm_test,
        "adzuna": settings_service.adzuna_view(r),
        "email": settings_service.email_view(r),
        "network": settings_service.network_view(r),
        "auto_apply": settings_service.auto_apply_view(r),
    })


@router.post("/settings/weights")
async def weights(request: Request):  # noqa: ANN201
    form = await request.form()
    try:
        n = settings_service.update_weights(rt(), {k: str(v) for k, v in form.items()})
    except (settings_service.SettingsError, ValueError) as exc:
        return redirect("/settings", error=str(exc))
    return redirect("/settings", message=f"weights saved to config.yaml; recomputed {n} score totals")


@router.post("/settings/backup")
def backup():  # noqa: ANN201
    try:
        out = settings_service.backup(rt())
    except (FileExistsError, OSError) as exc:
        return redirect("/settings", error=str(exc))
    return redirect("/settings", message=f"backup written to {out}")


@router.post("/settings/compact")
def compact():  # noqa: ANN201
    try:
        before, after = settings_service.compact(rt())
    except settings_service.SettingsError as exc:
        return redirect("/settings", error=str(exc))
    return redirect("/settings", message=f"database compacted: {before:.1f} MB -> {after:.1f} MB")


@router.get("/settings/config")
def config_page(request: Request):  # noqa: ANN201
    r = rt()
    return render(request, "config_edit.html", {"text": config_edit.read_text(r), "path": str(r.cfg.config_path),
                                                "problems": None})


@router.post("/settings/config")
def save_config(request: Request, text: str = Form(...)):  # noqa: ANN201
    r = rt()
    try:
        notes = config_edit.save_text(r, text)
    except config_edit.ConfigError as exc:
        # Re-render with the submitted text so nothing typed is lost; the file on disk is unchanged.
        return render(request, "config_edit.html", {"text": text, "path": str(r.cfg.config_path),
                                                    "problems": str(exc).splitlines()}, status_code=422)
    return redirect("/settings/config", message="config.yaml saved and applied. " + " · ".join(notes))


@router.post("/settings/llm")
async def save_llm(request: Request):  # noqa: ANN201
    form = await request.form()
    try:
        notes = settings_service.update_llm(rt(), {k: str(v) for k, v in form.items()})
    except settings_service.SettingsError as exc:
        return redirect("/settings", error=str(exc)[:800])
    cfg = rt().cfg
    return redirect("/settings", message=f"Language models saved: scoring → {cfg.scoring.provider}, tailoring → "
                                         f"{cfg.tailoring.provider}. " + " · ".join(notes))


@router.post("/settings/llm/test")
def test_llm():  # noqa: ANN201
    settings_service.start_llm_test(rt())
    return redirect("/settings")


@router.get("/settings/llm/test")
def llm_test_status(request: Request):  # noqa: ANN201
    return render(request, "fragments/llm_test.html", {"test": rt().llm_test})



@router.post("/settings/scoring-provider")
async def scoring_provider(request: Request):  # noqa: ANN201
    from jobhunt.web.common import safe_next

    form = await request.form()
    target = safe_next(str(form.get("next") or ""), "/home")
    try:
        label = settings_service.set_scoring_provider(rt(), str(form.get("provider") or ""))
    except settings_service.SettingsError as exc:
        return redirect(target.split("#")[0], error=str(exc))
    return redirect(target.split("#")[0], message=f"Scoring now uses {label}.")


@router.post("/settings/focus")
async def save_focus(request: Request):  # noqa: ANN201
    form = await request.form()
    try:
        notes = settings_service.update_focus(rt(), {k: str(v) for k, v in form.items()})
    except settings_service.SettingsError as exc:
        return redirect("/settings#focus", error=str(exc)[:800])
    return redirect("/settings#focus", message="Scoring focus saved. " + " · ".join(notes))


@router.post("/settings/focus/preview")
async def preview_focus(request: Request):  # noqa: ANN201
    form = await request.form()
    return render(request, "fragments/focus_preview.html",
                  {"p": settings_service.focus_preview(rt(), {k: str(v) for k, v in form.items()})})


@router.post("/settings/adzuna")
async def save_adzuna(request: Request):  # noqa: ANN201
    form = await request.form()
    try:
        notes = settings_service.update_adzuna(rt(), {k: str(v) for k, v in form.items()})
    except settings_service.SettingsError as exc:
        return redirect("/settings#adzuna", error=str(exc)[:800])
    state = "enabled" if rt().cfg.sources.get("adzuna") and rt().cfg.sources["adzuna"].enabled else "saved (disabled)"
    return redirect("/settings#adzuna", message=f"Adzuna {state}. " + " · ".join(notes))


@router.post("/settings/adzuna/test")
def adzuna_test():  # noqa: ANN201
    result = settings_service.test_adzuna(rt())
    rt().llm_test["adzuna"] = result
    if result["ok"]:
        return redirect("/settings#adzuna", message=result["message"])
    return redirect("/settings#adzuna", error=f"Adzuna test failed: {result['message']}")


@router.post("/settings/tailoring")
async def save_tailoring(request: Request):  # noqa: ANN201
    form = await request.form()
    try:
        notes = settings_service.update_tailoring(rt(), {k: str(v) for k, v in form.items()})
    except settings_service.SettingsError as exc:
        return redirect("/settings#tailoring", error=str(exc)[:800])
    return redirect("/settings#tailoring", message=f"Resume tailoring saved ({rt().cfg.tailoring.resume_mode} "
                                                   "resumes). Applies to the next draft. " + " · ".join(notes))


@router.post("/settings/email")
async def save_email(request: Request):  # noqa: ANN201
    form = await request.form()
    try:
        notes = settings_service.update_email(rt(), {k: str(v) for k, v in form.items()})
    except settings_service.SettingsError as exc:
        return redirect("/settings#email", error=str(exc)[:800])
    state = "on" if rt().cfg.email.enabled else "off"
    return redirect("/settings#email", message=f"Job alert emails saved ({state}). " + " · ".join(notes))


@router.post("/settings/email/test")
def email_test():  # noqa: ANN201
    result = settings_service.test_email(rt())
    rt().llm_test["email"] = result
    if result["ok"]:
        return redirect("/settings#email", message=result["message"])
    return redirect("/settings#email", error=f"Mailbox test failed: {result['message']}")


@router.post("/settings/recheck-filtered")
def recheck_filtered():  # noqa: ANN201
    result = settings_service.recheck_filtered(rt())
    removed = (f" Filtered out {result['removed']:,} open jobs that today's filters drop." if result["removed"]
               else "")
    if not result["reopened"]:
        return redirect("/settings#maintenance",
                        message=f"Re-checked {result['checked']:,} filtered jobs; today's filters drop them all too."
                                + removed)
    return redirect("/queue?status=discovered&sort=seen",
                    message=f"Re-opened {result['reopened']:,} of {result['checked']:,} filtered jobs; the next run "
                            "downloads their postings again, then they can be scored." + removed)


@router.post("/settings/network")
async def save_network(request: Request):  # noqa: ANN201
    form = await request.form()
    try:
        settings_service.update_network(rt(), {k: str(v) for k, v in form.items()})
    except settings_service.SettingsError as exc:
        return redirect("/settings#network", error=str(exc)[:800])
    view = settings_service.network_view(rt())
    where = f"other devices at {view['url']}" if view["lan"] else "this PC only"
    return redirect("/settings#network",
                    message=f"Saved: {where}. Restart `jobhunt serve` for it to take effect"
                            + (f", and allow the port once: {view['firewall']}" if view["lan"] else "."))


@router.post("/settings/auto-apply")
async def auto_apply(request: Request):  # noqa: ANN201
    form = await request.form()
    try:
        notes = settings_service.update_auto_apply(rt(), {k: str(v) for k, v in form.items()})
    except settings_service.SettingsError as exc:
        return redirect("/settings#auto-apply", error=str(exc))
    return redirect("/settings#auto-apply", message="; ".join(notes) or "auto-apply settings saved")
