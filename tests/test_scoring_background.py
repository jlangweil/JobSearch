"""Scoring prompt v2: recency-ordered background, certifications, role preference, role_fit weighting."""

from __future__ import annotations

import shutil

from typer.testing import CliRunner

from jobhunt.config import PROJECT_ROOT
from jobhunt.db import queries
from jobhunt.discovery import scoring
from jobhunt.facts import bank
from jobhunt.sources.normalize import normalize_raw
from tests.conftest import raw_job

FACTS = [
    {"id": "summary-primary", "kind": "summary", "text": "Lead developer with 25+ years."},
    {"id": "role-old", "kind": "role", "org": "Old Co", "title": "Junior Developer", "start_date": "1999-06",
     "end_date": "2000-05", "text": "C++ work."},
    {"id": "role-now", "kind": "role", "org": "Now Inc.", "title": "Project Lead", "start_date": "2026-01",
     "end_date": "present", "text": "Leads modernization projects."},
    {"id": "role-prev", "kind": "role", "org": "Prev Life", "title": "Solution Lead", "start_date": "2018-09",
     "end_date": "2025-10", "text": "Led team of 8-10."},
    {"id": "ach-now", "kind": "achievement", "parent_id": "role-now", "text": "Stabilized ~200 service interfaces."},
    {"id": "ach-prev", "kind": "achievement", "parent_id": "role-prev", "text": "Pioneered the GenAI initiative."},
    {"id": "ach-old", "kind": "achievement", "parent_id": "role-old", "text": "Wrote a C++ parser."},
    {"id": "cert-aws", "kind": "cert", "title": "AWS Certified Cloud Practitioner", "text": "AWS Certified Cloud Practitioner"},
    {"id": "skill-x", "kind": "skill", "text": "MuleSoft, Kafka"},
]


def test_background_is_most_recent_first_with_certifications(cfg):
    bg = scoring.format_background(FACTS, cfg)
    recent = bg["recent"]
    assert recent.index("Project Lead — Now Inc.") < recent.index("Solution Lead — Prev Life")
    assert "MOST RECENT ROLE" in recent.split("Solution Lead")[0]
    assert "Stabilized ~200 service interfaces." in recent and "Pioneered the GenAI initiative." in recent
    assert "Wrote a C++ parser." not in recent  # older roles are one line, no accomplishments
    assert "Junior Developer — Old Co" in bg["earlier"]
    assert "AWS Certified Cloud Practitioner" in bg["certifications"]
    prompt = scoring.build_system_prompt(cfg, bg, [], truncated=False)
    assert "role_fit" in prompt and "limited hands-on coding" in prompt  # rubric text from the v2 template
    assert cfg.profile.role_preference in prompt
    assert "technical lead" in prompt and "delivery lead" in prompt
    assert prompt.index("MOST RECENT ROLE") < prompt.index("Earlier roles")
    assert "$" + "recent" not in prompt  # every placeholder filled


def test_role_fit_is_weighted_and_old_scores_without_it_still_total(cfg):
    weights = cfg.scoring.weights
    assert weights["role_fit"] >= weights["seniority_fit"]
    hands_on = {"skill_match": 90, "role_fit": 20, "seniority_fit": 80, "domain_fit": 80, "comp_fit": 50,
                "logistics_fit": 80}
    leadership = {**hands_on, "role_fit": 90}
    assert scoring.compute_total(leadership, weights, 0, 0.25) - scoring.compute_total(hands_on, weights, 0, 0.25) > 15
    legacy = {k: v for k, v in hands_on.items() if k != "role_fit"}
    legacy["role_fit"] = None
    total = scoring.compute_total(legacy, weights, 0, 0.25)
    assert 70 < total < 85  # renormalized over the subscores present, not dragged down by a missing one


def test_companies_filter_and_prompt_preview_without_llm(cfg, engine, hashing_embedder, monkeypatch):
    from jobhunt import cli
    from jobhunt.sources.registry import build_sources

    srcs = build_sources(cfg, ["workday"], engine=None, only_companies=["prudential", "cigna"])
    names = sorted(b.company_name for b in srcs[0].boards)
    assert names == ["Cigna", "Prudential"]

    shutil.copy(PROJECT_ROOT / "data" / "facts.example.yaml", cfg.paths.facts)
    bank.sync(engine, cfg, hashing_embedder)
    _, job_id = queries.upsert_job(engine, normalize_raw(raw_job(1)))
    monkeypatch.setattr("jobhunt.embeddings.get_embedder", lambda name: hashing_embedder)
    result = CliRunner().invoke(cli.app, ["--config", str(cfg.config_path), "score", "--job-id", str(job_id),
                                          "--explain", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "===== SYSTEM PROMPT (cached" in result.output and "MOST RECENT ROLE" in result.output
    assert "MuleSoft Certified Integration Architect" in result.output  # certification from the fact bank
    assert queries.list_queue(engine, queries.QueueFilters(status="any"))[0][0]["total"] is None  # nothing scored
    # facts come back in facts.yaml order, so the background keeps the resume's bullet order
    ordered = [f["id"] for f in queries.list_facts(engine, file_order=True) if f["kind"] == "achievement"]
    assert ordered == [f.id for f in bank.load_yaml(cfg.paths.facts).facts if f.kind == "achievement"]
