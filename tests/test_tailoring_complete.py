"""Complete, keyword-tailored resumes: every accomplishment kept, posting keywords backed by facts, regeneration and
the documents page."""

from __future__ import annotations

from fastapi.testclient import TestClient

from jobhunt.db import queries
from jobhunt.facts import bank
from jobhunt.graphs import runtime
from jobhunt.llm.schemas import BulletFactRef, ResumeDraft
from jobhunt.services import triage, workflow
from jobhunt.tailor import drafting, validate
from tests.test_application import GOOD_MAP, GOOD_RESUME, app_rt, wait  # noqa: F401  (fixture re-export)

FACTS = [
    {"id": "skill-int", "kind": "skill", "text": "MuleSoft Anypoint Platform, REST/SOAP API Design, Kafka, IBM MQ"},
    {"id": "ach-1", "kind": "achievement", "parent_id": "role-1",
     "text": "Developed REST APIs and led API development standards for 40 services.", "tags": ["api development"]},
    {"id": "ach-2", "kind": "achievement", "parent_id": "role-1", "text": "Migrated 150+ legacy applications.",
     "tags": ["migration"]},
]


def test_keywords_are_split_into_supported_and_unsupported():
    corpora = validate.fact_corpora(FACTS)
    assert validate.term_supported("MuleSoft", corpora)
    assert validate.term_supported("API development", corpora)  # words together in one fact
    assert validate.term_supported("APIs", corpora)
    assert not validate.term_supported("Kafka migration", corpora)  # words only in different facts
    assert not validate.term_supported("Kubernetes", corpora)
    reqs = [{"text": "Own the integration platform", "kind": "must", "keywords": ["MuleSoft", "Kubernetes"]},
            {"text": "Modernize", "kind": "nice", "keywords": ["legacy applications", "mulesoft"]}]
    supported, unsupported = validate.keyword_support(reqs, FACTS)
    assert [e["keyword"] for e in supported] == ["MuleSoft", "legacy applications"]
    assert [e["keyword"] for e in unsupported] == ["Kubernetes"]


def test_validator_requires_every_accomplishment_once(app_rt, engine, hashing_embedder):  # noqa: F811
    candidates, _ = bank.retrieve(engine, hashing_embedder, [{"text": "MuleSoft", "kind": "must",
                                                              "keywords": ["mulesoft"]}], top_k=1, include_all=True)
    achievements = [c for c in candidates if c["kind"] == "achievement"]
    assert len(achievements) == 3 and achievements[0]["relevance"] >= achievements[-1]["relevance"]
    assert all("matches" in c for c in achievements)
    focused, _ = bank.retrieve(engine, hashing_embedder, [{"text": "MuleSoft", "kind": "must",
                                                           "keywords": ["mulesoft"]}], top_k=1)
    assert sum(c["kind"] == "achievement" for c in focused) == 1

    facts = {c["id"]: c for c in candidates}
    ids = [c["id"] for c in achievements]
    partial = [m.model_dump() for m in GOOD_MAP[:2]] + [{"bullet_index": 2, "fact_id": GOOD_MAP[0].fact_id}]
    errors = validate.validate_resume(GOOD_RESUME, partial, facts, facts, require_fact_ids=ids)
    problems = {e["problem"] for e in errors}
    assert {"omitted_fact", "repeated_fact"} <= problems
    ok = validate.validate_resume(GOOD_RESUME, [m.model_dump() for m in GOOD_MAP], facts, facts, require_fact_ids=ids)
    assert not [e for e in ok if e["problem"] in ("omitted_fact", "repeated_fact")]


def test_prompt_asks_for_complete_resume_with_supported_keywords(cfg):
    seen = {}

    class Capture:
        def structured(self, schema, **kw):  # noqa: ANN001, ANN003
            seen.update(kw)
            return ResumeDraft(markdown="## Summary\nx", fact_map=[BulletFactRef(bullet_index=0, fact_id="ach-1")])

    reqs = [{"text": "Own MuleSoft", "kind": "must", "keywords": ["MuleSoft", "Kubernetes"]}]
    drafting.draft_resume({"id": 1, "title": "Lead", "company_name_raw": "Acme", "description_text": "x"}, cfg,
                          Capture(), requirements=reqs, candidate_facts=FACTS, emphasis=None, revision=None, errors=[])
    system = seen["system"]
    assert seen["prompt_version"] == "draft_resume.v3" and "COMPLETE resume" in system
    assert "Every achievement fact must be used" in system
    assert "Key skills for this role" in system and 'Do NOT add a line labelled "Key skills for this role"' in system
    supported = system.split("## Supported posting keywords")[1].split("##")[0]
    unsupported = system.split("## Keywords not supported")[1].split("##")[0]
    assert "MuleSoft (must)" in supported and "Kubernetes" in unsupported and "Kubernetes" not in supported
    assert '"relevance"' not in system or "relevance" in system  # facts payload carries relevance when present

    cfg.tailoring.resume_mode = "focused"
    drafting.draft_resume({"id": 1, "title": "Lead", "company_name_raw": "Acme", "description_text": "x"}, cfg,
                          Capture(), requirements=reqs, candidate_facts=FACTS, emphasis=None, revision=None, errors=[])
    assert "FOCUSED resume" in seen["system"] and "Every achievement fact must be used" not in seen["system"]


def test_regenerate_from_ready_and_documents_page(app_rt, cfg, engine):  # noqa: F811
    rt, llm, job_id = app_rt
    wait(triage.shortlist(rt, job_id))
    wait(workflow.resume(rt, job_id, {"action": "accept"}))
    wait(workflow.resume(rt, job_id, {"action": "accept"}))
    assert workflow.view(rt, job_id)["gate"]["gate"] == "approve_and_apply"
    assert queries.get_job(engine, job_id)["status"] == "ready"

    rows = queries.tailored_jobs(engine)
    assert rows[0]["job_id"] == job_id and rows[0]["resume"]["version"] == 1 and rows[0]["cover_letter"]["approved"]

    before = llm.calls["draft_resume"]
    wait(workflow.resume(rt, job_id, {"action": "back_to_resume", "instructions": "put MuleSoft first"}))
    v = workflow.view(rt, job_id)
    assert llm.calls["draft_resume"] == before + 1 and v["gate"]["gate"] == "review_resume"
    assert v["state"]["revision_instructions"] == "put MuleSoft first"
    assert queries.get_job(engine, job_id)["status"] == "in_workflow"
    assert max(d["version"] for d in queries.documents_for_job(engine, job_id) if d["kind"] == "resume") == 2

    from jobhunt.web.app import create_app

    runtime.reset_runtime()
    app = create_app(cfg, start_scheduler=False, runtime_factories={"llm_factory": lambda: llm,
                                                                   "embedder_factory": rt.embedder_factory})
    with TestClient(app) as c:
        page = c.get("/documents").text
        assert "Resumes &amp; letters" in page and "v2" in page and "Open workflow" in page
        home = c.get("/home").text
        assert "Ready to apply" in home and "Resumes &amp; letters" in home
        settings_page = c.get("/settings").text
        assert 'id="tailoring"' in settings_page and 'value="complete" class="mt-1" checked' in settings_page
        r = c.post("/settings/tailoring", data={"resume_mode": "focused", "max_bullets_per_role": "6",
                                                "max_bullet_chars": "280", "facts_per_requirement": "5",
                                                "max_draft_attempts": "3"}, follow_redirects=False)
        assert "focused+resumes" in r.headers["location"], r.headers["location"]
        assert runtime.get_runtime().cfg.tailoring.max_bullets_per_role == 6


def test_unsupported_posting_keywords_are_rejected_in_bullets_and_summary(app_rt, engine, hashing_embedder):  # noqa: F811
    candidates, _ = bank.retrieve(engine, hashing_embedder, [], include_all=True)
    facts = {c["id"]: c for c in candidates}
    padded = GOOD_RESUME.replace("cutting partner onboarding time 60%.",
                                 "cutting partner onboarding time 60% through coaching.")
    padded = padded.replace("PLACEHOLDER: Integration architect", "Coaching-focused integration architect")
    errors = validate.validate_resume(padded, [m.model_dump() for m in GOOD_MAP], facts, facts,
                                      unsupported_terms=["coaching", "Kubernetes"])
    hits = [e for e in errors if e["problem"] == "unsupported_keyword"]
    assert len(hits) == 2 and any("summary" in e["detail"] for e in hits)
    clean = validate.validate_resume(GOOD_RESUME, [m.model_dump() for m in GOOD_MAP], facts, facts,
                                     unsupported_terms=["coaching", "Kubernetes"])
    assert not [e for e in clean if e["problem"] == "unsupported_keyword"]

    job = {"title": "Engineering Manager", "description_text": "You bring 7+ years with AWS and CI/CD pipelines."}
    reqs = [{"text": "x", "kind": "must", "keywords": ["aws", "7+ years", "ci/cd", "kubernetes"]}]
    supported, unsupported = drafting.posting_keywords(job, reqs, FACTS + [
        {"id": "s2", "kind": "skill", "text": "AWS, Jenkins CI/CD"}])
    assert [e["keyword"] for e in supported] == ["AWS", "CI/CD"]  # posting's spelling, no years phrase
    assert [e["keyword"] for e in unsupported] == ["kubernetes"]


def test_retry_after_validation_errors_repairs_the_previous_draft(cfg):
    seen = {}

    class Capture:
        def structured(self, schema, **kw):  # noqa: ANN001, ANN003
            seen.update(kw)
            return ResumeDraft(markdown="## Summary\nx", fact_map=[])

    job = {"id": 1, "title": "Lead", "company_name_raw": "Acme", "description_text": "x"}
    errors = [{"bullet_index": 3, "bullet_text": "Led X with delivery ownership", "problem": "unsupported_keyword",
               "detail": "'delivery ownership' is not in fact 'ach-2'"}]
    drafting.draft_resume(job, cfg, Capture(), requirements=[], candidate_facts=FACTS, emphasis=None, revision=None,
                          errors=errors, previous=("## Summary\nOLD DRAFT TEXT", [{"bullet_index": 0, "fact_id": "ach-1"}]))
    assert "## Your previous draft" in seen["system"] and "OLD DRAFT TEXT" in seen["system"]
    assert "ONLY the problems above fixed" in seen["system"] and "delivery ownership" in seen["system"]
    drafting.draft_resume(job, cfg, Capture(), requirements=[], candidate_facts=FACTS, emphasis=None, revision=None,
                          errors=[], previous=("## Summary\nOLD DRAFT TEXT", []))
    assert "OLD DRAFT TEXT" not in seen["system"]  # no errors: a normal draft


def test_rerender_writes_the_docx_again_without_calling_a_model(app_rt, cfg, engine):  # noqa: F811
    from docx import Document

    rt, llm, job_id = app_rt
    wait(triage.shortlist(rt, job_id))
    wait(workflow.resume(rt, job_id, {"action": "accept"}))
    wait(workflow.resume(rt, job_id, {"action": "accept"}))
    resume_doc = next(d for d in queries.documents_for_job(engine, job_id) if d["kind"] == "resume")
    calls = dict(llm.calls)

    cfg.tailoring.role_layout = "one_line"
    files = workflow.rerender_document(rt, resume_doc["id"])
    assert llm.calls == calls and len(files) >= 1
    headings = [p.text for p in Document(files[0]).paragraphs if p.style.name == "Heading 2"]
    assert any("|" in h for h in headings)

    cfg.tailoring.role_layout = "separate_lines"
    files = workflow.rerender_document(rt, resume_doc["id"])
    lines = [(p.style.name, p.text) for p in Document(files[0]).paragraphs if p.text.strip()]
    assert ("Normal", "Example Insurance Co") in lines  # employer on its own plain line, not packed into a heading
    assert not any("Example Insurance Co" in t and "|" in t for _, t in lines)
    assert queries.get_document(engine, resume_doc["id"])["version"] == resume_doc["version"]  # same version


def test_skills_lines_need_a_group_label(app_rt, engine, hashing_embedder):  # noqa: F811
    candidates, _ = bank.retrieve(engine, hashing_embedder, [], include_all=True)
    facts = {c["id"]: c for c in candidates}
    fact_map = [m.model_dump() for m in GOOD_MAP]
    grouped = GOOD_RESUME.replace("Integration: MuleSoft, REST APIs, API gateways, Kafka, AWS",
                                  "Integration: MuleSoft, REST APIs, Kafka\nCloud: AWS, API gateways")
    assert "skills_line_without_label" not in [e["problem"] for e in
                                               validate.validate_resume(grouped, fact_map, facts, facts)]
    unlabelled = grouped.replace("Cloud: AWS, API gateways", "AWS, API gateways")
    problems = [e["problem"] for e in validate.validate_resume(unlabelled, fact_map, facts, facts)]
    assert "skills_line_without_label" in problems
    # a resume with one plain line of skills is still fine
    assert "skills_line_without_label" not in [e["problem"] for e in
                                               validate.validate_resume(GOOD_RESUME, fact_map, facts, facts)]
