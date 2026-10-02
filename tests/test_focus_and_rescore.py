"""Scoring focus (boost/avoid, candidate-backed evidence), bulk rescoring, the provider switch, the run funnel and
the new app shell."""

from __future__ import annotations

from fastapi.testclient import TestClient

from jobhunt.db import queries
from jobhunt.discovery import focus, scoring
from jobhunt.graphs import runtime
from jobhunt.llm.schemas import Evidence, JobScore
from jobhunt.sources.normalize import normalize_raw
from jobhunt.web.app import create_app
from tests.conftest import FakeLLM, FakeSource, raw_job
from tests.test_web_control import _wait, ui_cfg  # noqa: F401  (fixture re-export)


def test_focus_phrases_boost_titles_and_cap_avoided_areas():
    boost, avoid = ["mulesoft", "api", "tech lead", "migration"], ["cybersecurity", "information security", "cyber"]
    fit, hits = focus.evaluate("MuleSoft Tech-Lead", "Own APIs and a mainframe migration.", boost, avoid)
    assert fit == 20 + 40 + 20 and hits["title"] == ["mulesoft", "tech lead"] and hits["description"] == ["api",
                                                                                                          "migration"]
    fit, hits = focus.evaluate("Director, Cyber Risk Services (Information Security)", "API integration", boost, avoid)
    assert fit == 0 and hits["avoid"] == ["information security", "cyber"]
    assert focus.evaluate("Rapid application developer", "", ["api"], [])[0] == 20  # whole words only


def _score(**kw) -> JobScore:  # noqa: ANN003
    ev = [Evidence(claim="api", quote="own the API platform", dimension="skill_match", candidate_quote="MuleSoft"),
          Evidence(claim="made up", quote="insurance business", dimension="domain_fit",
                   candidate_quote="15 years of cyber risk governance")]
    return JobScore(**({"skill_match": 90, "role_fit": 60, "domain_fit": 90, "seniority_fit": 60, "comp_fit": 50,
                        "logistics_fit": 60, "red_flags": [], "gaps": [], "evidence": ev, "rationale": "x"} | kw))


def test_enforce_requires_candidate_backing_and_applies_avoid_cap(cfg):
    cfg.focus.boost, cfg.focus.avoid, cfg.focus.avoid_cap = ["integration"], ["cyber"], 40
    job = {"id": 1, **normalize_raw(raw_job(1, title="Integration Lead"))}
    background = scoring.background_text({"skills": "- MuleSoft Anypoint, AWS Certified Cloud Practitioner"})
    row = scoring.enforce(_score(), job, cfg, ghost=0, truncated=False, background=background)
    assert row["skill_match"] == 90  # backed by "MuleSoft" in the background
    assert row["domain_fit"] == 69  # claim not in the candidate's background: capped
    assert row["focus_fit"] == 60 + 10 * 0 and row["focus_hits"]["title"] == ["integration"]
    assert any("not backed by your background" in g for g in row["gaps"])

    cyber = {"id": 2, **normalize_raw(raw_job(2, title="Director, Cyber Risk"))}
    row = scoring.enforce(_score(role_fit=95), cyber, cfg, ghost=0, truncated=False, background=background)
    assert row["total"] <= 40 and row["focus_fit"] == 0 and "capped at 40" in row["gaps"][0]


def _app(ui_cfg, hashing_embedder, jobs, llm):  # noqa: ANN001, ANN202, F811
    return create_app(ui_cfg, start_scheduler=False, runtime_factories={
        "llm_factory": lambda: llm, "embedder_factory": lambda: hashing_embedder,
        "sources_factory": lambda names: [FakeSource("workday", jobs)]})


def test_focus_settings_recompute_totals_and_bulk_rescore_and_provider_switch(ui_cfg, hashing_embedder):  # noqa: F811
    llm = FakeLLM()
    jobs = [raw_job(1, title="MuleSoft Integration Lead", company="Prudential", location="Remote (US)"),
            raw_job(2, title="Director, Cybersecurity Programs", company="Cigna", location="Remote (US)")]
    with TestClient(_app(ui_cfg, hashing_embedder, jobs, llm)) as c:
        rt = runtime.get_runtime()
        r = c.post("/runs", data={"mode": "full"}, follow_redirects=False)
        run_id = int(r.headers["location"].rsplit("/", 1)[1])
        _wait(lambda: queries.get_run(rt.engine, run_id)["status"] != "running")
        page = c.get(f"/runs/{run_id}").text
        assert "Left to score" in page and "Remaining" in page and "Postings fetched" in page

        rows, _ = queries.list_queue(rt.engine, queries.QueueFilters(status="scored", min_score=0), paginate=False)
        by_title = {row["title"]: row for row in rows}
        cyber = by_title["Director, Cybersecurity Programs"]
        assert cyber["total"] <= ui_cfg.focus.avoid_cap and cyber["focus_fit"] == 0
        assert by_title["MuleSoft Integration Lead"]["focus_fit"] >= 60

        assert "Scoring focus" in c.get("/settings").text
        preview = c.post("/settings/focus/preview", data={"boost": "mulesoft", "avoid": "", "avoid_cap": "45"}).text
        assert "would be capped" in preview and "MuleSoft Integration Lead" in preview
        r = c.post("/settings/focus", data={"boost": "mulesoft\nintegration", "avoid": "", "avoid_cap": "45"},
                   follow_redirects=False)
        assert "recomputed+focus+and+totals" in r.headers["location"], r.headers["location"]
        assert 'boost: ["mulesoft", "integration"]' in ui_cfg.config_path.read_text(encoding="utf-8")
        assert queries.latest_score(rt.engine, cyber["id"])["total"] > ui_cfg.focus.avoid_cap  # cap lifted, no LLM
        calls = llm.calls

        queue = c.get("/queue?status=scored&min_score=0&sort=total").text
        assert "Rescore these 2 jobs" in queue
        r = c.post("/scoring/batches", data={"status": "scored", "min_score": "0", "sort": "total",
                                             "provider": "local", "limit": "2"}, follow_redirects=False)
        batch_url = r.headers["location"]
        assert batch_url.startswith("/scoring/batches/")
        _wait(lambda: rt.batches[1]["status"] != "running")
        assert rt.batches[1]["done"] == 2 and rt.batches[1]["failed"] == 0 and llm.calls == calls + 2
        assert len(queries.score_history(rt.engine, cyber["id"])) == 2
        assert "Biggest changes" in c.get(batch_url).text

        r = c.post("/settings/scoring-provider", data={"provider": "anthropic", "next": "/queue?status=scored"},
                   follow_redirects=False)
        assert "ANTHROPIC_API_KEY" in r.headers["location"] and r.headers["location"].startswith("/queue")
        r = c.post("/settings/scoring-provider", data={"provider": "local", "next": "//evil.test/"},
                   follow_redirects=False)
        assert r.headers["location"].startswith("/home") and rt.cfg.scoring.provider == "local"


def test_app_shell_home_nav_and_palette(ui_cfg, hashing_embedder):  # noqa: F811
    with TestClient(_app(ui_cfg, hashing_embedder, [], FakeLLM())) as c:
        assert c.get("/", follow_redirects=False).headers["location"] == "/home"
        home = c.get("/home").text
        for text in ("Top matches (70+)", "Quick actions", "Waiting to score", "palette-data", "data-theme-cycle",
                     'class="dark"', "Scoring</span>"):
            assert text in home, text
        queue = c.get("/queue?status=discovered&sort=seen").text
        assert 'aria-current="page"' in queue.split("Waiting to score")[0][-400:]
        assert c.get("/static/app.js").status_code == 200 and "html.dark" in c.get("/static/app.css").text
        assert '/static/app.css?v=' in home  # versioned, so a changed stylesheet is never served from cache


def test_scores_show_which_model_scored_and_queue_filters_by_it(ui_cfg, hashing_embedder):  # noqa: F811
    from jobhunt.sources.normalize import normalize_raw

    with TestClient(_app(ui_cfg, hashing_embedder, [], FakeLLM())) as c:
        engine = runtime.get_runtime().engine
        ids = []
        for i, model in enumerate(["claude-haiku-4-5-20251001", "local:qwen3:8b"]):
            _, job_id = queries.upsert_job(engine, normalize_raw(raw_job(40 + i, title=f"Integration Lead {i}",
                                                                         company=f"Co {i}")))
            queries.record_score(engine, job_id, {"total": 80.0, "skill_match": 80, "role_fit": 80, "domain_fit": 80,
                                                  "seniority_fit": 80, "comp_fit": 80, "logistics_fit": 80,
                                                  "focus_fit": 60, "model": model})
            ids.append(job_id)
        page = c.get("/queue?status=scored&min_score=0&sort=total").text
        assert 'class="scorer scorer-claude" title="scored by claude-haiku-4-5-20251001">Claude<' in page
        assert 'class="scorer scorer-local" title="scored by qwen3:8b">Local<' in page
        local_only = c.get("/queue?status=scored&min_score=0&sort=total&scorer=local").text
        assert "Integration Lead 1" in local_only and "Integration Lead 0" not in local_only
        claude_only = c.get("/queue?status=scored&min_score=0&sort=total&scorer=claude").text
        assert "Integration Lead 0" in claude_only and "Integration Lead 1" not in claude_only
        assert "scorer-lg" in c.get(f"/jobs/{ids[1]}").text and ">Local<" in c.get("/home").text


def test_queue_sorts_by_clicking_column_headers(ui_cfg, hashing_embedder):  # noqa: F811
    from jobhunt.sources.normalize import normalize_raw

    with TestClient(_app(ui_cfg, hashing_embedder, [], FakeLLM())) as c:
        engine = runtime.get_runtime().engine
        for i, total in enumerate([55.0, 91.0, 73.0]):
            _, job_id = queries.upsert_job(engine, normalize_raw(raw_job(60 + i, title=f"Sorted Job {i}",
                                                                         company=f"Sort Co {i}")))
            queries.record_score(engine, job_id, {"total": total, "skill_match": 70, "focus_fit": 50})
        _, unscored = queries.upsert_job(engine, normalize_raw(raw_job(70, title="Sorted Job unscored",
                                                                       company="Sort Co 9")))

        def order(url: str) -> list[str]:
            text = c.get(url).text
            return sorted(["Sorted Job 0", "Sorted Job 1", "Sorted Job 2", "Sorted Job unscored"], key=text.find)

        page = c.get("/queue?status=any&min_score=&sort=seen").text
        assert "Score: high to low" in page and 'href="/queue?status=any&amp;sort=total"' in page
        assert order("/queue?status=any&sort=total") == ["Sorted Job 1", "Sorted Job 2", "Sorted Job 0",
                                                         "Sorted Job unscored"]
        by_score = c.get("/queue?status=any&sort=total").text
        assert 'aria-sort="descending"' in by_score and "sort=total_asc" in by_score  # click again to reverse
        assert order("/queue?status=any&sort=total_asc")[:3] == ["Sorted Job 0", "Sorted Job 2", "Sorted Job 1"]
        assert order("/queue?status=any&sort=total_asc")[-1] == "Sorted Job unscored"  # unscored stays last
        for key in queries.QUEUE_SORTS:
            assert c.get(f"/queue?status=any&sort={key}").status_code == 200
