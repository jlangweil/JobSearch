from __future__ import annotations

from fastapi import APIRouter, Request

from jobhunt.db import queries
from jobhunt.services import scheduler as scheduler_service
from jobhunt.services import schedules
from jobhunt.web.common import render, rt

router = APIRouter()


@router.get("/home")
def home(request: Request):  # noqa: ANN201
    r = rt()
    cfg = r.cfg
    counts = queries.status_counts(r.engine)
    top, top_total = queries.list_queue(r.engine, queries.QueueFilters(status="scored", min_score=70, sort="total"))
    facts = queries.list_facts(r.engine)
    attention = []
    if cfg.scoring.provider == "anthropic" and not cfg.secrets.ANTHROPIC_API_KEY:
        attention.append(("Scoring is set to Claude but ANTHROPIC_API_KEY isn't set.", "/settings#llm",
                          "Language models"))
    if cfg.tailoring.provider == "anthropic" and not cfg.secrets.ANTHROPIC_API_KEY:
        attention.append(("Resume and cover-letter tailoring needs ANTHROPIC_API_KEY in .env.", "/settings#llm",
                          "Language models"))
    if not facts:
        attention.append(("The fact bank is empty, so scores and resumes have nothing to go on.", "/facts",
                          "Fact bank"))
    if not cfg.focus.boost and not cfg.focus.avoid:
        attention.append(("No scoring focus set: tell the scorer what to rank high and what to push down.",
                          "/settings#focus", "Scoring focus"))
    if counts.get("discovered"):
        attention.append((f"{counts['discovered']} saved jobs are waiting to be scored.",
                          "/queue?status=discovered&sort=seen", "Review"))
    from jobhunt.services import workflow

    labels = {"review_resume": "has a resume draft to review", "review_cover": "has a cover letter to review",
              "approve_and_apply": "is ready to approve and apply"}
    for w in workflow.needs_attention(r):
        job = w["job"]
        what = labels.get(w["gate"]) or f"stopped at “{(w['stage'] or 'a step').replace('_', ' ')}”: open it and Retry"
        attention.insert(0, (f"Tailoring for {job['title']} at {job['company_name_raw']} {what}.",
                             f"/jobs/{job['id']}/workflow", "Open"))
    failed = [x for x in queries.list_runs(r.engine, limit=5) if x["status"] == "failed"]
    if failed:
        attention.append((f"Run #{failed[0]['id']} failed and can be resumed.", f"/runs/{failed[0]['id']}", "Open"))
    return render(request, "home.html", {
        "counts": counts, "top": top[:8], "top_total": top_total, "attention": attention,
        "runs": queries.list_runs(r.engine, limit=6), "upcoming": scheduler_service.next_runs(r, limit=5),
        "paused": schedules.is_paused(r.engine), "schedule_count": len(queries.list_schedules(r.engine)),
        "applied_recent": counts.get("applied", 0) + counts.get("responded", 0),
    })
