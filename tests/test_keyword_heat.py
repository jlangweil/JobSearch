"""The keyword heatmap: what the best-scoring postings ask for, and whether your background covers it."""

from __future__ import annotations

from fastapi.testclient import TestClient

from jobhunt.db import queries
from jobhunt.discovery import keyword_heat
from jobhunt.sources.normalize import normalize_raw
from tests.conftest import raw_job
from tests.test_web_control import _app, _engine, ui_cfg  # noqa: F401  (fixture re-export)


def _analysis(stack: list[tuple[str, str, str]], comps: list[tuple[str, bool]] = ()) -> dict:
    return {"stack": [{"technology": t, "importance": imp, "has": has} for t, imp, has in stack],
            "competencies": [{"competency": c, "importance": "required", "held": held} for c, held in comps]}


def test_keywords_are_counted_once_per_posting_and_spellings_merge():
    rows, jobs = keyword_heat.build([
        {"analysis": _analysis([("AWS", "required", "recent"), ("aws", "preferred", "recent"), ("Java 17", "required", "no")],
                               [("Stakeholder_Management", True)])},
        {"analysis": _analysis([("AWS", "preferred", "earlier"), ("Java", "required", "recent")],
                               [("stakeholder management", False)])},
        {"analysis": {}},                                       # nothing to count: not a job in the total
    ])
    by = {(r["keyword"], r["kind"]): r for r in rows}
    assert jobs == 2
    assert by[("aws", "technology")] == {"keyword": "aws", "kind": "technology", "label": "AWS", "jobs": 2,
                                         "required": 1, "have": 2, "share": 1.0}
    assert by[("java", "technology")]["jobs"] == 2 and by[("java", "technology")]["have"] == 1
    assert by[("stakeholder management", "competency")]["jobs"] == 2
    assert by[("stakeholder management", "competency")]["have"] == 1
    assert keyword_heat.normalize("OpenID Connect (OIDC)") == "openid connect"


def test_the_heatmap_follows_the_scores(cfg, engine):
    """Only jobs at or above the line count, and a rebuild after scoring picks up the new ones."""
    cfg.scoring.heatmap_min_score = 75
    rows = [(1, 80.0, [("Kafka", "required", "recent")]), (2, 60.0, [("Cobol", "required", "no")])]
    for n, total, stack in rows:
        rec = normalize_raw(raw_job(n, title=f"Integration Lead {n}"))
        rec["source_job_id"] = f"job-{n}"
        _, job_id = queries.upsert_job(engine, rec)
        queries.record_score(engine, job_id, {"model": "x", "rubric_version": "v9", "total": total, "skill_match": 70,
                                              "role_fit": 70, "seniority_fit": 70, "domain_fit": 70, "comp_fit": 50,
                                              "logistics_fit": 70, "rationale": "ok", "ghost_score": 0,
                                              "analysis": _analysis(stack)})
    meta = keyword_heat.rebuild(engine, cfg)
    assert meta["jobs"] == 1 and [r["label"] for r in queries.keyword_heat(engine)] == ["Kafka"]


def test_the_heatmap_page(ui_cfg, hashing_embedder):  # noqa: F811
    with TestClient(_app(ui_cfg, hashing_embedder)) as client:
        engine = _engine()
        queries.replace_keyword_heat(engine, [
            {"keyword": "aws", "kind": "technology", "label": "AWS", "jobs": 9, "required": 6, "have": 9, "share": 0.9},
            {"keyword": "kubernetes", "kind": "technology", "label": "Kubernetes", "jobs": 4, "required": 2, "have": 0,
             "share": 0.4}])
        page = client.get("/keywords").text
        assert "Keyword heatmap" in page and "AWS" in page and "Kubernetes" in page
        assert "Gaps worth a look" in page                      # Kubernetes: asked for often, not in your background
        assert 'href="/keywords"' in client.get("/home").text   # in the sidebar
        r = client.post("/keywords/rebuild", follow_redirects=False)
        assert r.status_code == 303 and "Recounted" in r.headers["location"]
