"""The Projects section: relevant projects and their keywords are always on a tailored resume, word for word from the
fact bank, and never turn into Experience bullets."""

from __future__ import annotations

from jobhunt.db import queries
from jobhunt.facts import bank
from jobhunt.llm.schemas import ResumeDraft
from jobhunt.services import triage, workflow
from jobhunt.tailor import drafting, projects, validate
from tests.test_application import app_rt, wait  # noqa: F401  (fixture re-export)

FACTS = [
    {"id": "proj-agent", "kind": "project", "title": "Agentic Flight Planner", "org": "github.com/me/flight",
     "text": "AI agent for GO/NO-GO flight decisions.", "tags": ["Python", "LangGraph", "Agentic AI", "AI"]},
    {"id": "ach-agent-1", "kind": "achievement", "parent_id": "proj-agent",
     "text": "Built a LangGraph agent that calls weather and fuel tools.", "tags": ["langgraph", "tool calling"]},
    {"id": "ach-agent-2", "kind": "achievement", "parent_id": "proj-agent",
     "text": "Added a human-in-the-loop checkpoint persisted in PostgreSQL.", "tags": ["postgresql"],
     "variants": ["Added a PostgreSQL-backed human approval step."]},
    {"id": "proj-rag", "kind": "project", "title": "Maintenance RAG", "org": "github.com/me/rag",
     "text": "RAG over scanned maintenance logs.", "tags": ["RAG", "FAISS", "AWS Textract", "AWS"]},
    {"id": "ach-rag-1", "kind": "achievement", "parent_id": "proj-rag",
     "text": "Built a RAG system over scanned logs with FAISS.", "tags": ["rag", "faiss"]},
    {"id": "proj-cobol", "kind": "project", "title": "COBOL Formatter", "org": "",
     "text": "A COBOL pretty-printer.", "tags": ["COBOL", "JCL"]},
]
SECTIONS = ["summary", "skills", "experience", "projects", "education", "certifications"]
DRAFT = "\n".join(["## Summary", "Leader.", "", "## Experience", "### Lead | Acme | Jan 2020 - Present",
                   "- Did things.", "", "## Education", "### B.S. | Example University | 1999", ""])


def test_relevant_projects_are_chosen_and_ordered_by_how_much_the_posting_asks_for():
    posting = "We want Python and LangGraph for Agentic AI; AWS a plus."
    chosen = projects.select(FACTS, posting)
    assert [c["project"]["id"] for c in chosen] == ["proj-agent", "proj-rag"]   # COBOL: nothing in common
    assert chosen[0]["matched"] == ["Python", "LangGraph", "Agentic AI", "AI"]
    assert [c["project"]["id"] for c in projects.select(FACTS, posting, include="always")][-1] == "proj-cobol"
    assert projects.select(FACTS, posting, include="never") == []
    assert len(projects.select(FACTS, posting, max_projects=1)) == 1


def test_the_section_uses_the_facts_words_and_puts_posting_keywords_first():
    lines = projects.section_lines(FACTS, "PostgreSQL and AWS experience; RAG preferred", max_bullets=1)
    md = "\n".join(lines)
    assert lines[0] == "## Projects"
    assert "### Maintenance RAG | github.com/me/rag" in md
    # the accomplishment that matches the posting (PostgreSQL) comes first; one bullet per project here
    assert "- Added a human-in-the-loop checkpoint persisted in PostgreSQL." in md
    assert "Built a LangGraph agent" not in md
    # posting keywords lead the Technologies line; "AWS" is dropped next to the more specific "AWS Textract"
    assert "Technologies: RAG, AWS Textract, FAISS" in md
    assert "Technologies: Python, LangGraph, Agentic AI" in md  # "AI" folded into "Agentic AI"


def test_apply_places_the_section_and_replaces_one_the_model_wrote():
    model_wrote = DRAFT.replace("## Education", "## Projects\n### Made Up | x\n- Invented.\n\n## Education")
    out = projects.apply(model_wrote, FACTS, "Python LangGraph", sections=SECTIONS)
    assert "Made Up" not in out and "Invented" not in out
    heads = [ln for ln in out.split("\n") if ln.startswith("## ")]
    assert heads == ["## Summary", "## Experience", "## Projects", "## Education"]
    assert projects.apply(out, FACTS, "Python LangGraph", sections=SECTIONS) == out   # idempotent
    # not in the configured sections, or nothing relevant: no section
    assert "## Projects" not in projects.apply(DRAFT, FACTS, "Python", sections=["summary", "experience"])
    assert "## Projects" not in projects.apply(DRAFT, FACTS, "Salesforce admin", sections=SECTIONS)
    # no later section in the draft: appended at the end
    tail = projects.apply("## Experience\n### Lead | Acme | Jan 2020 - Present\n- x", FACTS, "FAISS",
                          sections=SECTIONS)
    assert tail.rstrip().endswith("Technologies: FAISS, RAG, AWS Textract")


def test_the_validator_accepts_the_generated_section_and_rejects_edits():
    out = projects.apply(DRAFT, FACTS, "Python LangGraph RAG", sections=SECTIONS)
    section = validate.parse_resume(out).section_lines["projects"]
    assert projects.errors(section, FACTS) == []
    tampered = [ln.replace("calls weather", "calls 40 weather") if ln.startswith("- Built a LangGraph") else ln
                for ln in section]
    tampered += ["### Secret Project", "Technologies: Python, Kubernetes"]
    problems = {(e["problem"], e["detail"][:30]) for e in projects.errors(tampered, FACTS)}
    assert {p for p, _ in problems} == {"unsupported_project_bullet", "unknown_project", "stray_text"}
    tech = [ln for ln in section if ln.startswith("Technologies:")][0] + ", Kubernetes"
    assert [e["problem"] for e in projects.errors(["### Agentic Flight Planner", tech], FACTS)] == ["unsupported_skill"]
    # the full validator runs the same check, and project bullets are not Experience bullets
    doc = validate.parse_resume(out)
    assert all(b.role_heading == "Lead | Acme | Jan 2020 - Present" for b in doc.bullets)


def test_project_facts_load_and_their_accomplishments_stay_out_of_experience(app_rt, engine, hashing_embedder):  # noqa: F811
    ids = {f["id"] for f in queries.list_facts(engine)}
    assert {"project-example", "ach-example-toolkit"} <= ids
    toolkit = next(f for f in queries.list_facts(engine) if f["id"] == "ach-example-toolkit")
    candidates, coverage = bank.retrieve(engine, hashing_embedder,
                                         [{"text": toolkit["text"], "kind": "must", "keywords": ["api testing"]}],
                                         include_all=True)
    cids = {c["id"] for c in candidates}
    assert "project-example" in cids and "ach-example-toolkit" not in cids
    assert any("ach-example-toolkit" in c["covered_by"] or "project-example" in c["covered_by"] for c in coverage)


def test_a_tailored_resume_gets_the_projects_section(app_rt, engine):  # noqa: F811
    rt, llm, job_id = app_rt
    wait(triage.shortlist(rt, job_id, reason="fit"))
    v = workflow.view(rt, job_id)
    md = v["state"]["resume_markdown"]
    assert "## Projects" in md and "### Example Integration Toolkit | github.com/your-name/example-toolkit" in md
    assert "Technologies:" in md and v["gate"]["errors"] == []


def test_the_model_is_told_not_to_write_projects(cfg):
    seen = {}

    class Capture:
        def structured(self, schema, *, system, **kw):  # noqa: ANN001, ANN003
            seen["system"] = system
            return ResumeDraft(markdown="## Experience", fact_map=[])

    drafting.draft_resume({"id": 1, "title": "Lead", "company_name_raw": "Acme", "description_text": "Python"}, cfg, Capture(), requirements=[],
                          candidate_facts=FACTS, emphasis=None, revision=None, errors=[])
    system = seen["system"]
    assert "added after your draft automatically: do not write one" in system
    assert "'## Projects'" not in system.split("\n\n## Projects\n")[0]  # not a section the model writes
