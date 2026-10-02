from __future__ import annotations

from fastapi import APIRouter, Form, Request

from jobhunt.db import queries
from jobhunt.web.common import redirect, render, rt, safe_next

router = APIRouter()

OUTCOMES = ["none", "rejected", "screen", "interview", "offer"]


@router.get("/applications")
def applications(request: Request):  # noqa: ANN201
    outcome = request.query_params.get("outcome") or None
    needs = request.query_params.get("needs_follow_up") in ("1", "true", "on")
    rows = queries.list_applications(rt().engine, outcome=outcome, needs_follow_up=needs)
    return render(request, "applications.html", {"rows": rows, "outcome": outcome or "", "needs_follow_up": needs,
                                                 "outcomes": OUTCOMES})


@router.post("/applications/{app_id}/outcome")
def set_outcome(app_id: int, outcome: str = Form(...), notes: str = Form(""), next: str = Form("")):  # noqa: A002, ANN201
    if outcome not in OUTCOMES:
        return redirect("/applications", error=f"unknown outcome {outcome!r}")
    try:
        queries.set_application_outcome(rt().engine, app_id, outcome, notes or None)
    except KeyError as exc:
        return redirect("/applications", error=str(exc))
    return redirect(safe_next(next, "/applications"))
