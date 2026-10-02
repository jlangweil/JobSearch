"""App shell data for every full page: sidebar sections with live counts, the running run and scoring provider."""

from __future__ import annotations

from fastapi import Request

from jobhunt.graphs.runtime import get_runtime

TOP_MATCH_URL = "/queue?status=scored&min_score=70&sort=total"
_QUEUE_VIEWS = ("scored", "discovered", "shortlisted", "in_workflow", "ready")


def _item(label: str, href: str, icon: str, *, badge: object = None, active: bool = False, hint: str = "") -> dict:
    return {"label": label, "href": href, "icon": icon, "badge": badge, "active": active, "hint": hint}


def shell(request: Request) -> dict:
    from jobhunt.db import queries
    from jobhunt.services import schedules

    r = get_runtime()
    counts = queries.status_counts(r.engine)
    running = queries.list_runs(r.engine, limit=1, status="running")
    path = request.url.path
    status = request.query_params.get("status", "")
    top = path == "/queue" and status == "scored" and request.query_params.get("min_score") == "70"
    queue = path == "/queue"
    sections = [
        {"title": None, "items": [_item("Home", "/home", "home", active=path in ("/", "/home"))]},
        {"title": "Jobs", "items": [
            _item("Top matches", TOP_MATCH_URL, "star", active=top, hint="scored 70 or more"),
            _item("Waiting to score", "/queue?status=discovered&sort=seen", "inbox", badge=counts.get("discovered"),
                  active=queue and status == "discovered"),
            _item("Shortlisted", "/queue?status=shortlisted&sort=total", "bookmark", badge=counts.get("shortlisted"),
                  active=queue and status == "shortlisted"),
            _item("Tailoring", "/queue?status=in_workflow&sort=seen", "pen", badge=counts.get("in_workflow"),
                  active=queue and status == "in_workflow", hint="resume and cover letter in progress"),
            _item("Ready to apply", "/queue?status=ready&sort=seen", "send", badge=counts.get("ready"),
                  active=queue and status == "ready", hint="documents approved; apply and confirm"),
            _item("All jobs", "/queue?status=any&min_score=0&sort=seen", "list",
                  active=queue and not top and status not in _QUEUE_VIEWS[1:]),
            _item("Search", "/search", "search", active=path == "/search"),
            _item("Add a job", "/jobs/new", "plus", active=path.startswith("/jobs/new"),
                  hint="from a link or pasted text"),
        ]},
        {"title": "Apply", "items": [
            _item("Resumes & letters", "/documents", "copy", active=path == "/documents",
                  hint="every tailored resume and cover letter, with downloads"),
            _item("Applications", "/applications", "send", badge=counts.get("applied"),
                  active=path.startswith("/applications")),
            _item("Fact bank", "/facts", "book", active=path.startswith("/facts")),
            _item("Keyword heatmap", "/keywords", "flame", active=path.startswith("/keywords"),
                  hint="what your best-matching jobs ask for, and your gaps"),
            _item("Auto-apply", "/apply", "robot", badge="beta", active=path.startswith("/apply"),
                  hint="fill Workday applications in a browser"),
        ]},
        {"title": "Discover", "items": [
            _item("Runs", "/runs", "play", badge="live" if running else None,
                  active=path.startswith("/runs") or path.startswith("/dryruns")),
            _item("Schedules", "/schedules", "clock", badge="paused" if schedules.is_paused(r.engine) else None,
                  active=path.startswith("/schedules")),
            _item("Companies", "/companies", "building", active=path.startswith("/companies")),
        ]},
        {"title": "Setup", "items": [
            _item("Settings", "/settings", "gear", active=path == "/settings"),
            _item("config.yaml", "/settings/config", "code", active=path == "/settings/config"),
        ]},
    ]
    cfg = r.cfg
    return {"sections": sections, "running": running[0] if running else None, "provider": cfg.scoring.provider,
            "api_key_set": bool(cfg.secrets.ANTHROPIC_API_KEY), "local_model": cfg.local_llm.model,
            "claude_model": cfg.scoring.model,
            "full_path": path + (("?" + request.url.query) if request.url.query else "")}


PALETTE = {
    "pages": [
        ["Home", "/home", "Overview, next steps, upcoming schedules"],
        ["Top matches", TOP_MATCH_URL, "Scored 70 or more"],
        ["Waiting to score", "/queue?status=discovered&sort=seen", "Saved jobs not scored yet"],
        ["Shortlisted jobs", "/queue?status=shortlisted&sort=total", ""],
        ["Tailoring (in workflow)", "/queue?status=in_workflow&sort=seen", "Resume / cover letter in progress"],
        ["Ready to apply", "/queue?status=ready&sort=seen", "Documents approved; apply and confirm"],
        ["Resumes & letters", "/documents", "All tailored documents, downloads, regenerate"],
        ["Auto-apply (beta)", "/apply", "Workday sign-ins, your form answers, runs"],
        ["All jobs", "/queue?status=any&min_score=0&sort=seen", ""],
        ["Filtered-out jobs", "/queue?status=filtered_out&sort=seen", "Why jobs were dropped"],
        ["Search jobs", "/search", "Keyword + semantic search"],
        ["Add a job", "/jobs/new", "From a link or pasted posting text; tailor, score or mark applied"],
        ["Applications", "/applications", "Applied jobs, outcomes, follow-ups"],
        ["Fact bank", "/facts", "Career facts used for scoring and resumes"],
        ["Keyword heatmap", "/keywords", "Keywords your top-scoring jobs ask for, and your gaps"],
        ["Edit facts.yaml", "/facts/edit", ""],
        ["Skill levels", "/facts/skills", "Rate your skills 1-10; scoring weighs the job's stack by them"],
        ["Runs", "/runs", "Start a run, run history"],
        ["Schedules", "/schedules", "Automatic runs: enable, pause, edit"],
        ["Companies", "/companies", "Boards searched; add or deactivate companies"],
        ["Settings", "/settings", ""],
        ["Edit config.yaml", "/settings/config", "Profile, filters, search terms"],
    ],
    "actions": [
        ["Start a full run", "/runs?mode=full", "Fetch, filter and score"],
        ["Fetch & save (no scoring)", "/runs?mode=fetch", "Free; score later"],
        ["Score waiting jobs", "/runs?mode=stored", "Score jobs already saved"],
        ["Jobs from the latest run", "/queue?added=latest&status=scored&min_score=0&sort=total",
         "Only jobs first retrieved in the most recent run"],
        ["Rescore jobs", "/queue?status=scored&min_score=0&sort=total#rescore", "Re-run scoring on a filtered list"],
        ["New schedule", "/schedules/new", ""],
        ["Pause or resume all schedules", "/schedules", ""],
        ["Scoring focus: boost / avoid keywords", "/settings#focus", "e.g. MuleSoft high, cybersecurity low"],
        ["Language models: Claude or local", "/settings#llm", "Test the local model"],
        ["Resume tailoring: complete or focused", "/settings#tailoring", "How much of your history each resume keeps"],
        ["Scoring weights", "/settings#weights", ""],
        ["LLM spend", "/settings#spend", ""],
        ["Back up the database", "/settings#maintenance", ""],
        ["Add a company", "/companies", ""],
        ["Adzuna job search settings", "/settings#adzuna", "API keys, search terms, area"],
    ],
}
