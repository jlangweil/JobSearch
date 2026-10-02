from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request

from jobhunt.apply import workday
from jobhunt.db import queries
from jobhunt.db.locks import LockHeld
from jobhunt.services import scoring_actions, triage, workflow
from jobhunt.web.common import is_htmx, redirect, render, rt, safe_next

router = APIRouter()


def _row(job_id: int) -> dict | None:
    rows, _ = queries.list_queue(rt().engine, queries.QueueFilters(status="any", job_ids=[job_id]), paginate=False)
    return rows[0] if rows else None


def _row_response(request: Request, job_id: int, error: str | None = None):  # noqa: ANN202
    return render(request, "fragments/queue_rows.html", {"rows": [r for r in [_row(job_id)] if r], "row_error": error,
                                                         "row_error_id": job_id, "params": {}})


@router.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: int):  # noqa: ANN201
    engine = rt().engine
    job = queries.get_job(engine, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return render(request, "job_detail.html", {
        "job": job,
        "score": scoring_actions.latest_score_view(rt(), job_id),
        "history": queries.score_history(engine, job_id),
        "siblings": queries.dedupe_siblings(engine, job),
        "decisions": queries.decisions_for_job(engine, job_id),
        "applications": queries.applications_for_job(engine, job_id),
        "documents": queries.documents_for_job(engine, job_id),
        "subscores": queries.WEIGHT_KEYS,
        "scoring_now": scoring_actions.is_scoring(rt(), job_id),
        "score_failure": rt().failures.get(f"score-{job_id}"),
        "score_notice": rt().failures.get(f"notice-{job_id}"),
        "api_key_set": bool(rt().cfg.secrets.ANTHROPIC_API_KEY),
        "cfg": rt().cfg,
        "workday": workday.is_workday(job.get("apply_url") or job.get("url")),
    })


@router.post("/jobs/{job_id}/score")
def score_job(request: Request, job_id: int, next: str = Form(""), provider: str = Form("")):  # noqa: A002, ANN201
    try:
        scoring_actions.score_background(rt(), job_id, provider if provider in ("anthropic", "local") else None)
    except (scoring_actions.ScoringError, LockHeld) as exc:
        if is_htmx(request):
            return _row_response(request, job_id, str(exc))
        return redirect(safe_next(next, f"/jobs/{job_id}"), error=str(exc))
    if is_htmx(request):
        return _row_response(request, job_id, "scoring in the background — refresh in a few seconds")
    return redirect(safe_next(next, f"/jobs/{job_id}"), message="Scoring in the background (a few seconds). "
                                                                 "Refresh to see the result.")


@router.get("/jobs/{job_id}/prompt")
def scoring_prompt(request: Request, job_id: int):  # noqa: ANN201
    job = queries.get_job(rt().engine, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    try:
        prompt, error = scoring_actions.prompt_preview(rt(), job_id), None
    except scoring_actions.ScoringError as exc:
        prompt, error = "", str(exc)
    return render(request, "prompt.html", {"job": job, "prompt": prompt, "prompt_error": error})


@router.post("/jobs/{job_id}/shortlist")
def shortlist(request: Request, job_id: int, reason: str = Form(""), emphasis: str = Form(""),
              company_notes: str = Form(""), next: str = Form(""), force: str = Form(""),  # noqa: A002
              generation_mode: str = Form("")):  # noqa: ANN201
    try:
        # check the posting is still up first, unless this is "Tailor anyway"
        triage.shortlist(rt(), job_id, reason=reason or None, emphasis=emphasis or None,
                         company_notes=company_notes or None, verify=not force,
                         generation_mode=generation_mode or None)
    except (triage.TriageError, queries.InvalidTransition, workflow.WorkflowError, LockHeld) as exc:
        if is_htmx(request):
            return _row_response(request, job_id, str(exc))
        return redirect(safe_next(next, f"/jobs/{job_id}"), error=str(exc))
    if is_htmx(request):  # go to the workflow page; the job leaves the scored lists once shortlisted
        from fastapi.responses import Response

        return Response(status_code=204, headers={"HX-Redirect": f"/jobs/{job_id}/workflow"})
    return redirect(f"/jobs/{job_id}/workflow")


@router.post("/jobs/{job_id}/reject")
def reject(request: Request, job_id: int, reason: str = Form(""), next: str = Form("")):  # noqa: A002, ANN201
    try:
        triage.reject(rt(), job_id, reason)
    except (triage.TriageError, queries.InvalidTransition) as exc:
        if is_htmx(request):
            return _row_response(request, job_id, str(exc))
        return redirect(safe_next(next, f"/jobs/{job_id}"), error=str(exc))
    if is_htmx(request):
        return _row_response(request, job_id)
    return redirect(safe_next(next, f"/jobs/{job_id}"))


@router.post("/jobs/{job_id}/snooze")
def snooze(request: Request, job_id: int, until: str = Form(""), reason: str = Form(""),
           next: str = Form("")):  # noqa: A002, ANN201
    try:
        triage.snooze(rt(), job_id, until, reason or None)
    except (triage.TriageError, queries.InvalidTransition) as exc:
        if is_htmx(request):
            return _row_response(request, job_id, str(exc))
        return redirect(safe_next(next, f"/jobs/{job_id}"), error=str(exc))
    if is_htmx(request):
        return _row_response(request, job_id)
    return redirect(safe_next(next, f"/jobs/{job_id}"))
