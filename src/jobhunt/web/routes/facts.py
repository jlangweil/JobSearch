from __future__ import annotations

from fastapi import APIRouter, Form, Request
from pydantic import ValidationError

from jobhunt.db import queries
from jobhunt.facts import bank
from jobhunt.services import skill_levels
from jobhunt.web.common import redirect, render, rt

router = APIRouter()


@router.get("/facts")
def facts(request: Request):  # noqa: ANN201
    q = request.query_params.get("q") or None
    kind = request.query_params.get("kind") or None
    r = rt()
    rows = queries.list_facts(r.engine, kind=kind, q=q, file_order=True)
    try:
        lint = bank.lint(bank.load_yaml(r.cfg.paths.facts))
        file_error = None
    except bank.FactBankError as exc:
        lint, file_error = [], str(exc)
    return render(request, "facts.html", {"rows": rows, "q": q or "", "kind": kind or "", "kinds": bank.KINDS,
                                          "facts_path": str(r.cfg.paths.facts), "lint": lint,
                                          "file_error": file_error})


@router.post("/facts/sync")
def sync_facts():  # noqa: ANN201
    r = rt()
    try:
        n_facts, n_answers = bank.sync(r.engine, r.cfg, r.embedder())
    except bank.FactBankError as exc:
        return redirect("/facts", error=f"facts.yaml is invalid: {exc}"[:800])
    return redirect("/facts", message=f"synced {n_facts} facts and {n_answers} answers from facts.yaml")


@router.get("/facts/edit")
def edit_facts(request: Request):  # noqa: ANN201
    r = rt()
    return render(request, "facts_edit.html", {"text": bank.read_text(r.cfg), "path": str(r.cfg.paths.facts),
                                               "problems": None})


@router.post("/facts/edit")
def save_facts(request: Request, text: str = Form(...)):  # noqa: ANN201
    r = rt()
    try:
        n_facts, n_answers, backup = bank.save_text(r.engine, r.cfg, r.embedder(), text)
    except bank.FactBankError as exc:
        return render(request, "facts_edit.html", {"text": text, "path": str(r.cfg.paths.facts),
                                                   "problems": str(exc).splitlines()}, status_code=422)
    note = f" Previous version saved to {backup}." if backup else ""
    return redirect("/facts", message=f"facts.yaml saved and synced: {n_facts} facts, {n_answers} answers.{note}")


@router.post("/facts")
def add_fact(
    id: str = Form(...),  # noqa: A002
    kind: str = Form(...),
    text: str = Form(...),
    parent_id: str = Form(""),
    org: str = Form(""),
    title: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    tags: str = Form(""),
    metrics: str = Form(""),
):  # noqa: ANN201
    metric_map: dict[str, str] = {}
    for line in metrics.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip():
                metric_map[k.strip()] = v.strip()
    fact = {"id": id.strip(), "kind": kind, "text": text.strip(), "parent_id": parent_id.strip() or None,
            "org": org.strip() or None, "title": title.strip() or None, "start_date": start_date.strip() or None,
            "end_date": end_date.strip() or None, "tags": [t.strip() for t in tags.split(",") if t.strip()],
            "metrics": metric_map}
    r = rt()
    try:
        bank.add_fact(r.engine, r.cfg, r.embedder(), fact)
    except (bank.FactBankError, ValidationError, FileNotFoundError) as exc:
        return redirect("/facts", error=str(exc)[:500])
    return redirect("/facts", message=f"added {fact['id']} to facts.yaml and synced")


@router.get("/facts/skills")
def skill_levels_page(request: Request):  # noqa: ANN201
    return render(request, "skill_levels.html", skill_levels.view(rt()))


@router.post("/facts/skills")
async def save_skill_levels(request: Request):  # noqa: ANN201
    form = await request.form()
    rated, recomputed = skill_levels.save(rt(), {k: str(v) for k, v in form.items()})
    return redirect("/facts/skills", message=f"saved: {rated} skills rated; {recomputed} scores updated (no LLM calls)")


@router.post("/facts/skills/remove")
def remove_skill_level(skill: str = Form(...)):  # noqa: ANN201
    recomputed = skill_levels.remove(rt(), skill)
    return redirect("/facts/skills", message=f"removed {skill}; {recomputed} scores updated")
