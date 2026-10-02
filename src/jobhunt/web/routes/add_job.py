from __future__ import annotations

from fastapi import APIRouter, Request

from jobhunt.db import queries
from jobhunt.db.locks import LockHeld
from jobhunt.services import manual_import
from jobhunt.web.common import redirect, render, rt

router = APIRouter()


def _page(request: Request, **ctx):  # noqa: ANN003, ANN202
    r = rt()
    cfg = r.cfg
    replace = ctx.pop("replace", None)
    base = {"draft": None, "error": None, "url": "", "text": "", "replace": replace,
            "tailor_ready": cfg.tailoring.provider == "local" or bool(cfg.secrets.ANTHROPIC_API_KEY),
            "tailor_label": (f"local {cfg.local_llm.model}" if cfg.tailoring.provider == "local"
                             else f"Claude {cfg.tailoring.model}"),
            "cfg_scoring_provider": cfg.scoring.provider, "api_key_set": bool(cfg.secrets.ANTHROPIC_API_KEY),
            "shortlist_threshold": cfg.scoring.shortlist_threshold,
            "today": __import__("datetime").date.today().isoformat()}
    base.update(ctx)
    status = 422 if ctx.get("error") and ctx.get("draft") is None and request.method == "POST" else 200
    return render(request, "add_job.html", base, status_code=status)


@router.get("/jobs/new")
def add_job_form(request: Request):  # noqa: ANN201
    replace = request.query_params.get("replace")
    job = queries.get_job(rt().engine, int(replace)) if replace and replace.isdigit() else None
    return _page(request, replace=job, url=(job or {}).get("url") or "")


@router.post("/jobs/new/read")
async def read_posting(request: Request):  # noqa: ANN201
    form = await request.form()
    r = rt()
    url, text = str(form.get("url") or "").strip(), str(form.get("text") or "").strip()
    replace_id = str(form.get("replace_job_id") or "")
    replace = queries.get_job(r.engine, int(replace_id)) if replace_id.isdigit() else None
    try:
        if text:
            draft = manual_import.read_text(text, url)
        elif url:
            draft = manual_import.read_url(r, url)
        else:
            raise manual_import.ImportProblem("paste a link to the posting, or the posting text")
        draft = manual_import.fill_missing_with_model(r, draft)
        if replace is None:
            draft = manual_import.find_existing(r, draft)
        else:
            for key in ("title", "company", "location"):
                draft[key] = draft[key] or replace.get({"company": "company_name_raw", "location": "location_raw"}
                                                        .get(key, key)) or ""
    except manual_import.ImportProblem as exc:
        return _page(request, error=str(exc), url=url, text=text, replace=replace)
    return _page(request, draft=draft, url=url, text=text, replace=replace)


@router.post("/jobs/new")
async def save_posting(request: Request):  # noqa: ANN201
    form = await request.form()
    data = {k: str(v) for k, v in form.items()}
    r = rt()
    try:
        job_id, outcome = manual_import.save(r, data)
    except manual_import.ImportProblem as exc:
        draft = {**manual_import.read_text("x" * 80), **data, "description": data.get("description", ""),
                 "notes": [], "existing_job_id": None}
        replace = queries.get_job(r.engine, int(data["replace_job_id"])) if data.get("replace_job_id", "").isdigit() \
            else None
        return _page(request, draft=draft, error=str(exc), replace=replace)
    try:
        target, message = manual_import.next_step(r, job_id, data)
    except (manual_import.ImportProblem, queries.InvalidTransition, LockHeld, ValueError) as exc:
        return redirect(f"/jobs/{job_id}", error=f"Saved as job #{job_id}, but the next step didn't run: {exc}")
    prefix = {"updated": "Updated the existing job. ", "existing": "This job was already saved. "}.get(outcome, "")
    return redirect(target, message=prefix + message)
