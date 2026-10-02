"""application_graph nodes. Review gates use interrupt(); code before interrupt() is side-effect free because
LangGraph re-executes the node from the top when it resumes."""

from __future__ import annotations

import re

from langgraph.graph import END
from langgraph.types import Command, interrupt

from jobhunt.apply import answers, instructions
from jobhunt.db import queries
from jobhunt.db.engine import iso_shift
from jobhunt.facts import bank
from jobhunt.graphs.application.state import ApplicationState
from jobhunt.graphs.runtime import get_runtime
from jobhunt.tailor import drafting, projects, render, repair, validate

GATE_ACTIONS = {
    "review_ats": {"apply", "skip", "abandon"},  # [Amendment A4] ATS Super Match
    "review_resume": {"accept", "revise", "edit", "abandon"},
    "review_cover": {"accept", "revise", "edit"},
    "approve_and_apply": {"confirm", "back_to_resume"},
}
FOLLOW_UP_DAYS = 10


def _job(state: ApplicationState) -> dict:
    rt = get_runtime()
    job = queries.get_job(rt.engine, state["job_id"])
    if job is None:
        raise KeyError(f"job {state['job_id']} not found")
    return job


def _with_projects(state: ApplicationState, markdown: str) -> str:
    """Write the Projects section (relevant projects, their best accomplishments and technologies) into the draft."""
    rt = get_runtime()
    t = rt.cfg.tailoring
    p = t.projects
    return projects.apply(markdown, queries.list_facts(rt.engine), drafting.posting_text(dict(_job(state))),
                          sections=t.sections, include=p.include, max_projects=p.max_projects,
                          max_bullets=p.max_bullets_per_project, max_bullet_chars=t.max_bullet_chars,
                          plain=_ats(state) is not None)


def _project_facts() -> list[dict]:
    facts = queries.list_facts(get_runtime().engine)
    return projects.project_facts(facts) + [a for group in projects.project_achievements(facts).values()
                                            for a in group]


def _facts_for_validation(state: ApplicationState, referenced: list[str]) -> tuple[dict, set[str]]:
    rt = get_runtime()
    cands = {f["id"]: f for f in state.get("candidate_facts") or []}
    extra = [fid for fid in referenced if fid not in cands]
    facts = dict(cands)
    for f in queries.get_facts(rt.engine, extra):
        facts[f["id"]] = f
    return facts, set(cands)


# --------------------------------------------------------------------------------------------- requirements / facts


def parse_requirements(state: ApplicationState) -> dict:
    rt = get_runtime()
    reqs = drafting.parse_requirements(_job(state), rt.cfg, rt.llm())
    return {"requirements": reqs, "stage": "retrieving_facts"}


def _ats(state: ApplicationState) -> dict | None:
    """The ATS Super Match rules when this workflow runs in that mode, else None (automated, unchanged)."""
    if state.get("generation_mode") != "ats_super_match":
        return None
    return get_runtime().cfg.tailoring.ats.model_dump()


ATS_KEYWORDS_FACT = "ats-confirmed-keywords"


def _with_confirmed_keywords(state: ApplicationState, candidates: list[dict]) -> list[dict]:
    """ATS Super Match: the keywords you ticked for THIS job, as a skill source for this workflow only. Never written
    to facts.yaml or the fact table; the fact check accepts them because you confirmed them for this posting."""
    keywords = state.get("ats_confirmed_keywords") or []
    if not keywords:
        return candidates
    fact = {"id": ATS_KEYWORDS_FACT, "kind": "skill", "text": "Additional Skills: " + ", ".join(keywords),
            "tags": list(keywords), "variants": [], "metrics": {}, "parent_id": None, "org": None, "title": None,
            "start_date": None, "end_date": None}
    return [*(f for f in candidates if f.get("id") != ATS_KEYWORDS_FACT), fact]


def _complete(state: ApplicationState) -> bool:
    """resume_mode: complete applies to automated workflows; ATS Super Match always picks the best bullets."""
    return get_runtime().cfg.tailoring.resume_mode == "complete" and _ats(state) is None


def retrieve_facts(state: ApplicationState) -> dict:
    rt = get_runtime()
    t = rt.cfg.tailoring
    candidates, coverage = bank.retrieve(rt.engine, rt.embedder(), state.get("requirements") or [],
                                         top_k=t.facts_per_requirement, threshold=t.fact_match_threshold,
                                         include_all=_complete(state))
    return {"candidate_facts": candidates, "coverage": coverage, "stage": "drafting_resume"}


# --------------------------------------------------------------------------------------------- resume


def draft_resume(state: ApplicationState) -> dict:
    rt = get_runtime()
    t = rt.cfg.tailoring
    # Refresh the facts on every draft, so a regenerated resume uses the current fact bank and resume mode (older
    # workflows kept only the few facts retrieved when they started).
    candidates, coverage = bank.retrieve(rt.engine, rt.embedder(), state.get("requirements") or [],
                                         top_k=t.facts_per_requirement, threshold=t.fact_match_threshold,
                                         include_all=_complete(state))
    candidates = _with_confirmed_keywords(state, candidates)
    errors = state.get("validation_errors") or []
    previous = None
    if errors and state.get("draft_origin") == "model" and state.get("resume_markdown"):
        previous = (state["resume_markdown"], state.get("resume_fact_map") or [])  # retry = repair, not a new draft
    md, fm = drafting.draft_resume(
        _job(state), rt.cfg, rt.llm(), requirements=state.get("requirements") or [],
        candidate_facts=candidates, emphasis=state.get("emphasis"),
        revision=state.get("revision_instructions"), errors=errors, previous=previous, ats=_ats(state),
    )
    md = _with_projects(state, md)
    return {"resume_markdown": md, "resume_fact_map": fm, "resume_document_id": None, "draft_origin": "model",
            "candidate_facts": candidates, "coverage": coverage, "stage": "validating_resume"}


def validate_resume(state: ApplicationState) -> Command:
    rt = get_runtime()
    t = rt.cfg.tailoring
    md, fm = state.get("resume_markdown") or "", state.get("resume_fact_map") or []
    facts, cand_ids = _facts_for_validation(state, [str(e.get("fact_id")) for e in fm])
    required = None
    if _complete(state) and state.get("draft_origin") != "user" and not state.get("revision_instructions"):
        required = [fid for fid, f in facts.items() if fid in cand_ids and f.get("kind") == "achievement"]
    _, unsupported = drafting.posting_keywords(_job(state), state.get("requirements") or [],
                                               [f for fid, f in facts.items() if fid in cand_ids])

    project_rows = _project_facts()

    def check(md: str, fm: list) -> list[dict]:
        return validate.validate_resume(md, fm, facts, cand_ids, max_bullet_chars=t.max_bullet_chars,
                                        max_bullets_per_role=t.max_bullets_per_role, sections=t.sections,
                                        require_fact_ids=required,
                                        unsupported_terms=[e["keyword"] for e in unsupported], ats=_ats(state),
                                        project_facts=project_rows)

    errors = check(md, fm)
    notes: list[str] = []
    if errors and state.get("draft_origin") == "model":
        # Fix what has an exact, safe fix (a bullet back to its fact's words, roles in date order, a skills label)
        # instead of paying for another draft. The repaired draft is validated like any other, and a redraft, if
        # still needed, starts from it.
        fixed_md, fixed_fm, notes = repair.repair_resume(md, fm, facts, cand_ids, errors,
                                                         max_bullet_chars=t.max_bullet_chars, ats=_ats(state))
        if notes:
            md, fm, errors = fixed_md, fixed_fm, check(fixed_md, fixed_fm)
    kw = validate.keyword_coverage(md, state.get("requirements") or [])
    repaired = {"resume_markdown": md, "resume_fact_map": fm} if notes else {}
    if not errors:
        doc_id = queries.insert_document(
            rt.engine, job_id=state["job_id"], kind="resume", markdown=md, fact_map=fm,
            fact_ids=sorted({e["fact_id"] for e in fm}), coverage=(state.get("coverage") or []) + [{"keywords": kw}])
        return Command(goto="review_resume", update={
            **repaired, "resume_document_id": doc_id, "validation_errors": [], "draft_attempts": 0,
            "keyword_coverage": kw, "stage": "review_resume",
            "gate_message": f"Fixed automatically instead of redrafting: {'; '.join(notes)}." if notes else None})
    if state.get("draft_origin") == "user":
        return Command(goto="review_resume", update={
            "validation_errors": errors, "resume_document_id": None, "keyword_coverage": kw, "stage": "review_resume",
            "gate_message": "Your edit failed validation and was not saved. Fix the problems below."})
    attempts = int(state.get("draft_attempts") or 0) + 1
    if attempts < t.max_draft_attempts:
        return Command(goto="draft_resume", update={**repaired, "validation_errors": errors, "draft_attempts": attempts,
                                                    "stage": f"redrafting_resume (attempt {attempts + 1})"})
    return Command(goto="review_resume", update={
        **repaired, "validation_errors": errors, "draft_attempts": attempts, "resume_document_id": None,
        "keyword_coverage": kw,
        "stage": "review_resume",
        "gate_message": f"The draft failed validation {attempts} times. Revise with instructions, fix it by hand, "
                        "or abandon."})


_TITLE_NOISE = re.compile(r"\([^)]*\)|\b(?:[A-Z]{1,4}[-_]?\d{3,}[\w-]*)\b|"
                          r"\s[-\u2013|,]\s*(?:remote|hybrid|onsite|on-site)\b.*$", re.IGNORECASE)
_TITLE_WORDS = {"sr": "Senior", "sr.": "Senior", "jr": "Junior", "mgr": "Manager", "mgr.": "Manager", "dir": "Director",
                "eng": "Engineering", "vp": "VP"}


def ats_headline(title: str, supported: list[dict]) -> str:
    """A resume headline for the role: the posting title without codes and locations ("Sr." spelled out), and up to
    two of the posting's must-have keywords the facts support: "Senior IT Program Manager | Agile | Scrum"."""
    t = _TITLE_NOISE.sub("", title or "")
    words = [_TITLE_WORDS.get(w.lower(), w) for w in t.split()]
    clean = re.sub(r"[\s,|-]+$", "", " ".join(words)).strip()
    extras = [e["keyword"] for e in supported if e.get("kind") == "must"][:2]
    return " | ".join([clean, *(k[:1].upper() + k[1:] for k in extras)]) if clean else ""


def ats_proposal(state: ApplicationState) -> dict:
    """What ATS Super Match offers to add: the posting keywords no fact supports (degree requirements your education
    meets are left out), and a headline. Pure: the gate node re-runs it when resumed."""
    job = _job(state)
    facts = state.get("candidate_facts") or []
    supported, unsupported = drafting.posting_keywords(job, state.get("requirements") or [], facts)
    education = [f for f in facts if f.get("kind") == "education"]
    offers = [{"keyword": e["keyword"], "kind": e.get("kind") or "nice"} for e in unsupported
              if not bank.education_covers(e["keyword"], education)]
    return {"keywords": offers, "headline": ats_headline(job.get("title") or "", supported)}


def review_ats(state: ApplicationState) -> Command:
    """[Amendment A4] Tick the keywords you really have and the headline; only those are used, for this job only."""
    proposal = ats_proposal(state)
    payload = interrupt({"gate": "review_ats", "keywords": proposal["keywords"], "headline": proposal["headline"],
                         "message": state.get("gate_message"), "errors": []}) or {}
    action = payload.get("action")
    if action in ("apply", "skip"):
        chosen: list[str] = []
        headline = None
        if action == "apply":
            offered = {o["keyword"].lower(): o["keyword"] for o in proposal["keywords"]}
            chosen = list(dict.fromkeys(offered[k.lower()] for k in payload.get("keywords") or []
                                        if str(k).lower() in offered))
            if payload.get("use_headline"):
                headline = (str(payload.get("headline") or "").strip() or proposal["headline"] or None)
        return Command(goto="draft_resume", update={
            "ats_headline": headline, "ats_confirmed_keywords": chosen, "gate_message": None,
            "stage": "drafting_resume"})
    if action == "abandon":
        rt = get_runtime()
        job = queries.get_job(rt.engine, state["job_id"])
        if job and job["status"] == "in_workflow":
            queries.set_job_status(rt.engine, state["job_id"], "abandoned", payload.get("reason") or "abandoned")
        return Command(goto=END, update={"stage": "abandoned", "gate_message": None})
    return Command(goto="review_ats", update={"gate_message": f"Unknown action {action!r}."})


def review_resume(state: ApplicationState) -> Command:
    payload = interrupt({
        "gate": "review_resume",
        "document_id": state.get("resume_document_id"),
        "errors": state.get("validation_errors") or [],
        "message": state.get("gate_message"),
    }) or {}
    action = payload.get("action")
    if action == "accept":
        if state.get("validation_errors") or not state.get("resume_document_id"):
            return Command(goto="review_resume", update={
                "gate_message": "This draft has validation errors and cannot be accepted."})
        queries.approve_document(get_runtime().engine, state["resume_document_id"])
        return Command(goto="draft_cover_letter", update={
            "gate_message": None, "draft_attempts": 0, "validation_errors": [], "revision_instructions": None,
            "stage": "drafting_cover"})
    if action == "revise":
        return Command(goto="draft_resume", update={
            "revision_instructions": payload.get("instructions") or None, "draft_attempts": 0,
            "validation_errors": [], "gate_message": None, "stage": "drafting_resume"})
    if action == "edit":
        return Command(goto="validate_resume", update={
            "resume_markdown": payload.get("markdown") or "", "resume_fact_map": list(payload.get("fact_map") or []),
            "draft_origin": "user", "stage": "validating_resume"})
    if action == "abandon":
        rt = get_runtime()
        job = queries.get_job(rt.engine, state["job_id"])
        if job and job["status"] == "in_workflow":
            queries.set_job_status(rt.engine, state["job_id"], "abandoned", payload.get("reason") or "abandoned")
        return Command(goto=END, update={"stage": "abandoned", "gate_message": None})
    return Command(goto="review_resume", update={"gate_message": f"Unknown action {action!r}."})


# --------------------------------------------------------------------------------------------- cover letter


def draft_cover_letter(state: ApplicationState) -> dict:
    rt = get_runtime()
    md, fm = drafting.draft_cover(
        _job(state), rt.cfg, rt.llm(), candidate_facts=state.get("candidate_facts") or [],
        company_notes=state.get("company_notes"), emphasis=state.get("emphasis"),
        revision=state.get("revision_instructions"), errors=state.get("validation_errors") or [],
    )
    return {"cover_markdown": md, "cover_fact_map": fm, "cover_document_id": None, "draft_origin": "model",
            "stage": "validating_cover"}


def validate_cover(state: ApplicationState) -> Command:
    rt = get_runtime()
    md, fm = state.get("cover_markdown") or "", state.get("cover_fact_map") or []
    facts, cand_ids = _facts_for_validation(state, [str(e.get("fact_id")) for e in fm])
    errors = validate.validate_cover(md, fm, facts, cand_ids, state.get("company_notes"))
    if not errors:
        doc_id = queries.insert_document(rt.engine, job_id=state["job_id"], kind="cover_letter", markdown=md,
                                         fact_map=fm, fact_ids=sorted({e["fact_id"] for e in fm}), coverage=None)
        return Command(goto="review_cover", update={"cover_document_id": doc_id, "validation_errors": [],
                                                    "draft_attempts": 0, "gate_message": None,
                                                    "stage": "review_cover"})
    if state.get("draft_origin") == "user":
        return Command(goto="review_cover", update={
            "validation_errors": errors, "cover_document_id": None, "stage": "review_cover",
            "gate_message": "Your edit failed validation and was not saved. Fix the problems below."})
    attempts = int(state.get("draft_attempts") or 0) + 1
    if attempts < rt.cfg.tailoring.max_draft_attempts:
        return Command(goto="draft_cover_letter", update={"validation_errors": errors, "draft_attempts": attempts,
                                                          "stage": f"redrafting_cover (attempt {attempts + 1})"})
    return Command(goto="review_cover", update={
        "validation_errors": errors, "draft_attempts": attempts, "cover_document_id": None, "stage": "review_cover",
        "gate_message": f"The cover letter failed validation {attempts} times. Revise or fix it by hand."})


def review_cover(state: ApplicationState) -> Command:
    payload = interrupt({
        "gate": "review_cover",
        "document_id": state.get("cover_document_id"),
        "errors": state.get("validation_errors") or [],
        "message": state.get("gate_message"),
    }) or {}
    action = payload.get("action")
    if action == "accept":
        if state.get("validation_errors") or not state.get("cover_document_id"):
            return Command(goto="review_cover", update={
                "gate_message": "This cover letter has validation errors and cannot be accepted."})
        queries.approve_document(get_runtime().engine, state["cover_document_id"])
        return Command(goto="render_documents", update={"gate_message": None, "stage": "rendering"})
    if action == "revise":
        return Command(goto="draft_cover_letter", update={
            "revision_instructions": payload.get("instructions") or None, "draft_attempts": 0,
            "validation_errors": [], "gate_message": None, "stage": "drafting_cover"})
    if action == "edit":
        return Command(goto="validate_cover", update={
            "cover_markdown": payload.get("markdown") or "", "cover_fact_map": list(payload.get("fact_map") or []),
            "draft_origin": "user", "stage": "validating_cover"})
    return Command(goto="review_cover", update={"gate_message": f"Unknown action {action!r}."})


# --------------------------------------------------------------------------------------------- render / apply


def render_documents(state: ApplicationState) -> dict:
    rt = get_runtime()
    cfg = rt.cfg
    job = _job(state)
    person = bank.contact(cfg)
    out = render.output_dir(cfg.paths.output_root, job["company_name_raw"], job["title"], job["id"],
                            max_path_chars=cfg.paths.max_path_chars)
    paths: list[str] = []
    warnings: list[str] = []
    for kind, md_key, doc_key in (("resume", "resume_markdown", "resume_document_id"),
                                  ("cover_letter", "cover_markdown", "cover_document_id")):
        doc_path = out / render.document_filename(person.name, kind, "docx")
        render.markdown_to_docx(state.get(md_key) or "", doc_path, template=cfg.paths.resume_template,
                                name=person.name, contact_line=person.line(), kind=kind,
                                role_layout=cfg.tailoring.role_layout, headline=state.get("ats_headline"))
        problems = render.check_docx_ats(doc_path)
        blocking = [p for p in problems if not p.startswith("non-standard fonts")]
        if blocking:
            raise render.RenderError(f"{doc_path.name} is not ATS-safe: {'; '.join(blocking)} "
                                     f"(fix data/resume_template.docx)")
        warnings += [f"{doc_path.name}: {p}" for p in problems]
        files = [str(doc_path)]
        pdf = render.render_pdf(doc_path, cfg.paths.soffice)
        if pdf:
            files.append(str(pdf))
        if state.get(doc_key):
            queries.set_document_files(rt.engine, state[doc_key], files)
        paths += files
    if job["status"] == "in_workflow":
        queries.set_job_status(rt.engine, job["id"], "ready")
    return {"rendered_paths": paths, "ats_warnings": warnings, "stage": "approve_and_apply"}


def approve_and_apply(state: ApplicationState) -> Command:
    rt = get_runtime()
    job = _job(state)
    suggested = answers.suggest(rt.engine, rt.embedder())
    payload = interrupt({
        "gate": "approve_and_apply",
        "checklist": instructions.checklist(job, state.get("rendered_paths") or [], suggested),
        "rendered_paths": state.get("rendered_paths") or [],
        "message": state.get("gate_message"),
    }) or {}
    action = payload.get("action")
    if action == "confirm":
        return Command(goto="record_application", update={
            "application_method": payload.get("method") or "deep_link",
            "confirmation_ref": payload.get("confirmation_ref") or None,
            "application_notes": payload.get("notes") or None, "stage": "recording"})
    if action == "back_to_resume":
        current = queries.get_job(rt.engine, state["job_id"])
        if current and current["status"] == "ready":
            queries.set_job_status(rt.engine, state["job_id"], "in_workflow", "back to resume")
        return Command(goto="draft_resume", update={
            "draft_attempts": 0, "validation_errors": [], "rendered_paths": [], "gate_message": None,
            "revision_instructions": payload.get("instructions") or None, "stage": "drafting_resume"})
    return Command(goto="approve_and_apply", update={"gate_message": f"Unknown action {action!r}."})


def record_application(state: ApplicationState) -> dict:
    rt = get_runtime()
    job = _job(state)
    if job["status"] == "ready":
        queries.record_application(
            rt.engine, job_id=job["id"], method=state.get("application_method") or "deep_link",
            confirmation_ref=state.get("confirmation_ref"),
            document_ids=[d for d in (state.get("resume_document_id"), state.get("cover_document_id")) if d],
            follow_up_at=iso_shift(FOLLOW_UP_DAYS), notes=state.get("application_notes"))
    return {"stage": "applied"}
