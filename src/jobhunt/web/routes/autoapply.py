"""Auto-apply (beta): the apply-profile editor, saved sign-ins, starting a run and watching it."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from jobhunt.apply.profile import ApplyProfile, CustomAnswer
from jobhunt.db import queries
from jobhunt.services import autoapply
from jobhunt.web.common import is_htmx, redirect, render, rt

router = APIRouter()


@router.get("/apply")
def hub(request: Request):  # noqa: ANN201
    r = rt()
    return render(request, "auto_apply.html", {
        "cfg": r.cfg,
        "profile": autoapply.profile_for(r.cfg),
        "credentials": autoapply.credentials_view(r),
        "store_mode": r.cfg.auto_apply.store_passwords,
        "runs": queries.list_apply_runs(r.engine, limit=25),
        "questions": queries.list_apply_questions(r.engine),
        "open_questions": queries.list_apply_questions(r.engine, unanswered_only=True),
    })


@router.post("/apply/profile")
async def save_profile(request: Request):  # noqa: ANN201
    """The whole profile posts as one form; `questions` come back as parallel match/answer lists."""
    form = await request.form()
    r = rt()
    data = autoapply.profile_for(r.cfg).model_dump()
    for dotted, value in form.multi_items():
        if "." not in dotted or dotted.startswith("q_"):
            continue
        section, _, key = dotted.partition(".")
        if section in data and key in data[section]:
            data[section][key] = (value == "on") if isinstance(data[section][key], bool) else str(value).strip()
    for section in ("identity", "links", "eligibility", "eeo", "signature"):
        for key, current in data[section].items():                 # unchecked boxes do not post at all
            if isinstance(current, bool) and f"{section}.{key}" not in form:
                data[section][key] = False
    matches = form.getlist("q_match")
    answers = form.getlist("q_answer")
    data["questions"] = [CustomAnswer(match=m.strip(), answer=a.strip()).model_dump()
                         for m, a in zip(matches, answers, strict=False) if m.strip()]
    autoapply.save_profile_text(r.cfg, ApplyProfile.model_validate(data))
    return redirect("/apply", message="apply profile saved")


@router.post("/apply/credentials")
async def save_credential(request: Request):  # noqa: ANN201
    form = await request.form()
    try:
        autoapply.save_credential(rt(), host=str(form.get("host") or ""), username=str(form.get("username") or ""),
                                  password=str(form.get("password") or ""), notes=str(form.get("notes") or "") or None)
    except Exception as exc:
        return redirect("/apply", error=str(exc))
    plain = rt().cfg.auto_apply.store_passwords == "plain"
    return redirect("/apply", message="sign-in saved "
                    + ("(stored as typed, and shown on this page)" if plain
                       else "(encrypted for this Windows account)"))


@router.post("/apply/credentials/{cred_id}/delete")
def delete_credential(cred_id: int):  # noqa: ANN201
    queries.delete_credential(rt().engine, cred_id)
    return redirect("/apply", message="sign-in removed")


@router.get("/jobs/{job_id}/auto-apply")
def preflight(request: Request, job_id: int):  # noqa: ANN201
    try:
        view = autoapply.preflight(rt(), job_id)
    except autoapply.AutoApplyError as exc:
        return redirect(f"/jobs/{job_id}", error=str(exc))
    return render(request, "auto_apply_start.html", {"v": view, "job": view["job"]})


@router.post("/jobs/{job_id}/auto-apply")
def start(job_id: int):  # noqa: ANN201
    try:
        run_id = autoapply.start(rt(), job_id)
    except autoapply.AutoApplyError as exc:
        return redirect(f"/jobs/{job_id}/auto-apply", error=str(exc))
    return redirect(f"/apply/runs/{run_id}", message="a browser window is opening — you can watch and take over")


@router.get("/apply/runs/{run_id}")
def run_detail(request: Request, run_id: int):  # noqa: ANN201
    try:
        view = autoapply.status_view(rt(), run_id)
    except autoapply.AutoApplyError as exc:
        return redirect("/apply", error=str(exc))
    template = "fragments/apply_run.html" if is_htmx(request) else "auto_apply_run.html"
    return render(request, template, {"run": view})


@router.post("/apply/questions/{question_id}")
async def answer(request: Request, question_id: int):  # noqa: ANN201
    form = await request.form()
    autoapply.answer_question(rt(), question_id, str(form.get("answer") or "").strip(),
                              save_to_profile=form.get("save_to_profile") != "off")
    back = str(form.get("next") or "/apply")
    return redirect(back if back.startswith("/") else "/apply", message="answer saved — it will be reused from now on")


@router.get("/apply/runs/{run_id}/shot/{index}")
def screenshot(run_id: int, index: int):  # noqa: ANN201
    run = queries.get_apply_run(rt().engine, run_id)
    shots = (run or {}).get("screenshots") or []
    if not run or index >= len(shots) or not Path(shots[index]).exists():
        return redirect(f"/apply/runs/{run_id}", error="that screenshot is gone")
    return FileResponse(shots[index], media_type="image/png")
