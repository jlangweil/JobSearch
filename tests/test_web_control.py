"""Web UI control surface: run modes, company selection, stop/resume, dry runs, company/config/facts editing,
single-job scoring and prompt preview, maintenance."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import text

from jobhunt.config import PROJECT_ROOT, load_config, set_config
from jobhunt.db import locks, migrate, queries
from jobhunt.db.engine import dispose_engines, utcnow_iso
from jobhunt.embeddings import set_embedder
from jobhunt.graphs import runtime
from jobhunt.llm.client import set_llm
from jobhunt.services import companies as company_service
from jobhunt.sources.normalize import normalize_raw
from jobhunt.sources.registry import match_companies
from jobhunt.web.app import create_app
from tests.conftest import FakeLLM, FakeSource, raw_job


@pytest.fixture
def ui_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The shipped config.example.yaml (flow-style company lines intact) with every path moved into tmp_path."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with open(PROJECT_ROOT / "config.example.yaml", encoding="utf-8") as fh:
        text = fh.read()
    data = tmp_path / "data"
    for key, value in {"db": data / "jobhunt.db", "checkpoints": data / "checkpoints.db", "facts": data / "facts.yaml",
                       "output_root": tmp_path / "output", "exports": tmp_path / "exports",
                       "resume_template": data / "resume_template.docx"}.items():
        text, n = __import__("re").subn(rf'(?m)^  {key}: "[^"]*"', f'  {key}: "{value.as_posix()}"', text)
        assert n == 1, key
    text = __import__("re").sub(r'(?m)^  soffice: .*$', "  soffice: null", text)
    # the owner's own switches (Adzuna on, the web app opened to their network) must not leak into tests
    text = __import__("re").sub(r"(?m)^(  adzuna:\n    enabled: )true", r"\g<1>false", text)
    text = __import__("re").sub(r'(?m)^(web:\n  host: )"[^"]*"', r'\g<1>"127.0.0.1"', text)
    text = __import__("re").sub(r"(?m)^(auto_apply:\n  enabled: )true", r"\g<1>false", text)  # nor the beta switch
    # the test postings are in New Jersey: make that the commute area, whatever the template uses as its example
    text, n = __import__("re").subn(r"(?m)^  local_location_patterns:\n(?:    - .*\n)+",
                                    "  local_location_patterns:\n    - '\\\\bNJ\\\\b|New Jersey'\n", text)
    assert n == 1
    text = __import__("re").sub(r"(?m)^  local_states: \[.*\]$", "  local_states: [NJ, NY, PA]", text)
    path = tmp_path / "config.yaml"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    cfg = load_config(path)
    cfg.secrets.ANTHROPIC_API_KEY = None
    migrate.upgrade(cfg)
    set_config(cfg)
    yield cfg
    runtime.reset_runtime()
    set_config(None)
    set_llm(None)
    set_embedder(None)
    dispose_engines()


class SlowSource(FakeSource):
    def fetch(self, since):  # noqa: ANN001
        self.fetch_calls += 1
        for job in self.jobs:
            time.sleep(0.03)
            yield job


def _app(cfg, hashing_embedder, sources=None):  # noqa: ANN001, ANN202
    return create_app(cfg, start_scheduler=False, runtime_factories={
        "llm_factory": FakeLLM, "embedder_factory": lambda: hashing_embedder,
        "sources_factory": (lambda names: sources) if sources is not None else (lambda names: [])})


def _wait(predicate, timeout: float = 60) -> None:  # noqa: ANN001
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("timed out")


def _engine():  # noqa: ANN202
    return runtime.get_runtime().engine


# --------------------------------------------------------------------------------------------- selection


def test_company_matching_prefers_exact_slug():
    rows = [{"name": "Salesforce", "slug": "salesforce"}, {"name": "MuleSoft (Salesforce)", "slug": "mulesoft-salesforce"},
            {"name": "Lincoln Financial", "slug": "lincoln-financial"}]
    assert [c["name"] for c in match_companies(rows, ["salesforce"])] == ["Salesforce"]
    assert [c["name"] for c in match_companies(rows, ["Lincoln Financial"])] == ["Lincoln Financial"]
    assert len(match_companies(rows, ["sales"])) == 2  # substring fallback


def test_runs_page_lists_modes_industries_and_preselected_companies(ui_cfg, hashing_embedder):
    with TestClient(_app(ui_cfg, hashing_embedder)) as c:
        page = c.get("/runs?mode=dry&company=prudential&company=cigna").text
    assert 'value="dry" checked' in page
    assert 'name="industries" value="insurance"' in page
    assert 'value="prudential" checked' in page and 'value="cigna" checked' in page
    assert 'value="travelers" checked' not in page
    sel = company_service.resolve_selection(ui_cfg, ["pharma"], ["prudential"])
    assert "prudential" in sel and "merck" in sel and "cigna" not in sel


# --------------------------------------------------------------------------------------------- run modes


def test_score_stored_jobs_only_mode_fetches_nothing(ui_cfg, hashing_embedder):
    src = FakeSource("workday", [raw_job(1)])
    with TestClient(_app(ui_cfg, hashing_embedder, [src])) as c:
        _, job_id = queries.upsert_job(_engine(), normalize_raw(raw_job(7, title="Principal Integration Architect",
                                                                        company="Prudential", location="Remote (US)")))
        # a waiting job from a company outside the selection is left alone
        _, other = queries.upsert_job(_engine(), normalize_raw(raw_job(8, title="Principal Integration Architect",
                                                                       company="Pfizer", location="Remote (US)")))
        r = c.post("/runs", data={"mode": "stored", "max_to_score": "5", "industries": "insurance"},
                   follow_redirects=False)
        run_id = int(r.headers["location"].rsplit("/", 1)[1])
        _wait(lambda: queries.get_run(_engine(), run_id)["status"] != "running")
        run = queries.get_run(_engine(), run_id)
        assert run["status"] == "complete", run["errors"]
        assert src.fetch_calls == 0 and run["counts"]["scored"] == 1
        assert queries.get_job(_engine(), job_id)["status"] == "scored"
        assert queries.get_job(_engine(), other)["status"] == "discovered"
        page = c.get(f"/runs/{run_id}").text
        assert "score stored jobs only" in page and "prudential" in page  # options recorded (industry → companies)


def test_stop_a_running_run_then_resume(ui_cfg, hashing_embedder):
    jobs = [raw_job(100 + i, title=f"Principal API Architect {i}", company=f"Slow Co {i}", location="Remote (US)")
            for i in range(200)]
    src = SlowSource("workday", jobs)
    with TestClient(_app(ui_cfg, hashing_embedder, [src])) as c:
        run_id = int(c.post("/runs", data={"mode": "full"}, follow_redirects=False).headers["location"].rsplit("/", 1)[1])
        _wait(lambda: (queries.get_run(_engine(), run_id)["counts"] or {}).get("fetched", 0) >= 25)
        assert "Stop run" in c.get(f"/runs/{run_id}").text
        r = c.post(f"/runs/{run_id}/stop", follow_redirects=False)
        assert "Stop+requested" in r.headers["location"]
        _wait(lambda: queries.get_run(_engine(), run_id)["status"] != "running")
        run = queries.get_run(_engine(), run_id)
        assert run["status"] == "failed" and any("stopped by user" in e["message"] for e in run["errors"])
        assert run["counts"]["fetched"] < 200
        assert queries.lock_get(_engine(), "stop:run-1") is None
        assert queries.lock_get(_engine(), locks.DISCOVERY_LOCK) is None
        assert "Resume from last checkpoint" in c.get(f"/runs/{run_id}").text
        c.post(f"/runs/{run_id}/resume")
        _wait(lambda: queries.get_run(_engine(), run_id)["status"] != "running", timeout=90)
        run = queries.get_run(_engine(), run_id)
        assert run["status"] == "complete", run["errors"]
        assert run["counts"]["scored"] > 0


def test_dry_run_from_the_browser_writes_nothing(ui_cfg, hashing_embedder):
    jobs = [raw_job(i, title=t, company=f"Co {i}", location="Remote (US)")
            for i, t in enumerate(["Principal Integration Architect", "Registered Nurse", "Delivery Lead"])]
    with TestClient(_app(ui_cfg, hashing_embedder, [FakeSource("workday", jobs)])) as c:
        r = c.post("/runs", data={"mode": "dry", "companies": ["prudential"]}, follow_redirects=False)
        assert r.headers["location"] == "/dryruns/1"
        _wait(lambda: runtime.get_runtime().dry_runs[1]["status"] != "running")
        page = c.get("/dryruns/1").text
        assert "complete" in page and "fetched=3 filtered=1" in page and "survived 2" in page
        assert "Do this as a full run" in page
        assert queries.status_counts(_engine()) == {} and queries.list_runs(_engine()) == []


# --------------------------------------------------------------------------------------------- companies


def test_pause_and_add_companies_edit_config_with_backup(ui_cfg, hashing_embedder):
    with TestClient(_app(ui_cfg, hashing_embedder)) as c:
        assert "Prudential" in c.get("/companies?industry=insurance").text
        r = c.post("/companies/active", data={"name": "Prudential", "active": "false", "next": "/companies"},
                   follow_redirects=False)
        assert "paused" in r.headers["location"], r.headers["location"]
        text = ui_cfg.config_path.read_text(encoding="utf-8")
        assert '{name: "Prudential", ats_type: workday, board_token: pru.wd5.myworkdayjobs.com/Careers, ' \
               'industry: insurance, active: false}' in text
        rt = runtime.get_runtime()
        assert next(x for x in rt.cfg.companies if x.name == "Prudential").active is False
        assert not any(x["name"] == "Prudential" for x in queries.list_companies(rt.engine))  # DB synced
        assert list((rt.cfg.paths.exports / "config-backups").glob("config-*.yaml"))
        c.post("/companies/active", data={"name": "Prudential", "active": "true"})
        assert '{name: "Prudential", ats_type: workday, board_token: pru.wd5.myworkdayjobs.com/Careers, ' \
               'industry: insurance}' in ui_cfg.config_path.read_text(encoding="utf-8")

        before = ui_cfg.config_path.read_text(encoding="utf-8")
        bad = c.post("/companies", data={"name": "Bad Co", "ats_type": "workday", "board_token": "example.com/x"},
                     follow_redirects=False)
        assert "error=" in bad.headers["location"]
        assert ui_cfg.config_path.read_text(encoding="utf-8") == before
        ok = c.post("/companies", data={"name": "Horizon BCBS", "ats_type": "workday", "industry": "Health",
                                        "board_token": "https://horizon.wd1.myworkdayjobs.com/en-US/Careers/jobs",
                                        "queries": "integration, delivery lead"}, follow_redirects=False)
        assert "added" in ok.headers["location"], ok.headers["location"]
        added = next(x for x in runtime.get_runtime().cfg.companies if x.name == "Horizon BCBS")
        assert added.board_token == "horizon.wd1.myworkdayjobs.com/Careers" and added.industry == "health"
        assert added.queries == ["integration", "delivery lead"]
        dup = c.post("/companies", data={"name": "horizon bcbs", "ats_type": "lever", "board_token": "x"},
                     follow_redirects=False)
        assert "already+configured" in dup.headers["location"]


def test_probe_service_reads_a_board(ui_cfg, hashing_embedder):
    def handler(request: httpx.Request) -> httpx.Response:
        if "greenhouse" in request.url.host:
            return httpx.Response(200, json={"jobs": [{"id": 1}, {"id": 2}]})
        return httpx.Response(404)

    with TestClient(_app(ui_cfg, hashing_embedder)):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        rt = runtime.get_runtime()
        good = company_service.probe(rt, "greenhouse", "oscar", client=client)
        assert good["ok"] and good["boards"][0]["total"] == 2
        assert not company_service.probe(rt, "lever", "nobody", client=client)["ok"]


# --------------------------------------------------------------------------------------------- config / facts editors


def test_config_editor_validates_and_applies(ui_cfg, hashing_embedder):
    with TestClient(_app(ui_cfg, hashing_embedder)) as c:
        text = c.get("/settings/config").text
        assert "Validate &amp; save" in text
        original = ui_cfg.config_path.read_text(encoding="utf-8")
        broken = original.replace("max_per_run: 150", "max_per_run: lots")
        r = c.post("/settings/config", data={"text": broken})
        assert r.status_code == 422 and "Not saved" in r.text and "max_per_run" in r.text
        bad_regex = original.replace("dealbreaker_patterns: [", "dealbreaker_patterns: ['([unclosed', ")
        r = c.post("/settings/config", data={"text": bad_regex})
        assert r.status_code == 422 and "invalid regex" in r.text
        assert ui_cfg.config_path.read_text(encoding="utf-8") == original
        changed = original.replace("max_per_run: 150", "max_per_run: 77").replace("\n", "\r\n")
        r = c.post("/settings/config", data={"text": changed}, follow_redirects=False)
        assert r.status_code == 303 and "saved+and+applied" in r.headers["location"]
        assert runtime.get_runtime().cfg.scoring.max_per_run == 77
        assert "\r\n" not in ui_cfg.config_path.read_text(encoding="utf-8")
        moved = original.replace('  exports: "', '  exports: "D:/elsewhere/').replace("max_per_run: 150", "max_per_run: 77")
        r = c.post("/settings/config", data={"text": moved}, follow_redirects=False)
        assert "restart" in r.headers["location"]
        assert "elsewhere" not in str(runtime.get_runtime().cfg.paths.exports)  # running app keeps its paths


def test_skill_levels_can_be_rated_and_added_from_the_fact_bank(ui_cfg, hashing_embedder):
    shutil.copy(PROJECT_ROOT / "data" / "facts.example.yaml", ui_cfg.paths.facts)
    with TestClient(_app(ui_cfg, hashing_embedder)) as c:
        c.post("/facts/sync")
        assert 'href="/facts/skills"' in c.get("/facts").text     # the tab
        page = c.get("/facts/skills").text
        assert "Skill levels" in page and "MuleSoft" in page and "0 of" in page
        import re
        rows = dict(re.findall(r'name="skill_(\d+)" value="([^"]+)"', page))
        index = {skill: i for i, skill in rows.items()}
        form = {f"skill_{i}": skill for i, skill in rows.items()}
        form |= {f"label_{i}": skill for i, skill in rows.items()}
        form[f"level_{index['aws']}"] = "5"
        form |= {"new_label": "Kubernetes", "new_level": "3"}
        r = c.post("/facts/skills", data=form, follow_redirects=False)
        assert r.status_code == 303 and "2+skills+rated" in r.headers["location"]
        assert queries.skill_level_map(_engine()) == {"aws": 5, "kubernetes": 3}
        page = c.get("/facts/skills").text
        assert "Added by you" in page and "Kubernetes" in page and "2 of" in page
        # clearing a rating removes it; a hand-added skill is removed with its button
        form[f"level_{index['aws']}"] = ""
        form.pop("new_label"), form.pop("new_level")
        c.post("/facts/skills", data=form)
        assert queries.skill_level_map(_engine()) == {"kubernetes": 3}
        c.post("/facts/skills/remove", data={"skill": "kubernetes"})
        assert queries.skill_level_map(_engine()) == {}


def test_facts_editor_and_sync(ui_cfg, hashing_embedder):
    shutil.copy(PROJECT_ROOT / "data" / "facts.example.yaml", ui_cfg.paths.facts)
    with TestClient(_app(ui_cfg, hashing_embedder)) as c:
        assert "Edit facts.yaml" in c.get("/facts").text
        r = c.post("/facts/sync", follow_redirects=False)
        assert "synced" in r.headers["location"]
        original = ui_cfg.paths.facts.read_text(encoding="utf-8")
        broken = original.replace("parent_id: role-example-current", "parent_id: role-nope", 1)
        r = c.post("/facts/edit", data={"text": broken})
        assert r.status_code == 422 and "role-nope" in r.text
        assert ui_cfg.paths.facts.read_text(encoding="utf-8") == original
        added = original.rstrip("\n") + '\n\n  - id: cert-new\n    kind: cert\n    title: "New Cert"\n    text: "New Cert"\n'
        r = c.post("/facts/edit", data={"text": added}, follow_redirects=False)
        assert "saved+and+synced" in r.headers["location"]
        assert any(f["id"] == "cert-new" for f in queries.list_facts(_engine()))
        assert list((ui_cfg.paths.exports / "facts-backups").glob("facts-*.yaml"))


# --------------------------------------------------------------------------------------------- single job


def test_score_one_job_and_preview_prompt(ui_cfg, hashing_embedder):
    shutil.copy(PROJECT_ROOT / "data" / "facts.example.yaml", ui_cfg.paths.facts)
    with TestClient(_app(ui_cfg, hashing_embedder)) as c:
        from jobhunt.facts import bank

        bank.sync(_engine(), ui_cfg, hashing_embedder)
        _, job_id = queries.upsert_job(_engine(), normalize_raw(raw_job(3, location="Remote (US)")))
        assert "Score now" in c.get("/queue?status=discovered&sort=seen").text
        prompt = c.get(f"/jobs/{job_id}/prompt").text
        assert "===== SYSTEM PROMPT (cached" in prompt and "MOST RECENT ROLE" in prompt
        r = c.post(f"/jobs/{job_id}/score", follow_redirects=False)
        assert "Scoring+in+the+background" in r.headers["location"]
        _wait(lambda: queries.get_job(_engine(), job_id)["status"] == "scored")
        _wait(lambda: not c.get(f"/jobs/{job_id}").text.count("scoring…"))
        page = c.get(f"/jobs/{job_id}").text
        assert "Re-score" in page and "role fit" in page
        c.post(f"/jobs/{job_id}/score")
        _wait(lambda: len(queries.score_history(_engine(), job_id)) == 2)
        # a filtered-out job has no stored description and cannot be scored
        _, gone = queries.upsert_job(_engine(), normalize_raw(raw_job(4, title="Registered Nurse")))
        queries.set_job_status(_engine(), gone, "filtered_out", "title")
        queries.strip_job_content(_engine(), [gone])
        r = c.post(f"/jobs/{gone}/score", follow_redirects=False)
        assert "cannot+be+scored" in r.headers["location"]


# --------------------------------------------------------------------------------------------- maintenance


def test_compact_refused_while_busy_and_fresh_database_is_created(ui_cfg, hashing_embedder, tmp_path):
    with TestClient(_app(ui_cfg, hashing_embedder)) as c:
        with _engine().begin() as conn:
            conn.exec_driver_sql("INSERT INTO locks (name, acquired_at, pid) VALUES ('thread:job-9', ?, 1)",
                                 (utcnow_iso(),))
        assert "busy" in c.post("/settings/compact", follow_redirects=False).headers["location"]
        with _engine().begin() as conn:
            conn.exec_driver_sql("DELETE FROM locks")
        assert "compacted" in c.post("/settings/compact", follow_redirects=False).headers["location"]
    runtime.reset_runtime()
    dispose_engines()
    fresh = ui_cfg.model_copy(deep=True)
    fresh.paths.db = tmp_path / "fresh" / "jobhunt.db"
    fresh.paths.checkpoints = tmp_path / "fresh" / "checkpoints.db"
    with TestClient(_app(fresh, hashing_embedder)) as c:
        assert c.get("/queue?status=scored").status_code == 200
        assert c.get("/companies").status_code == 200
    assert yaml.safe_load(ui_cfg.config_path.read_text(encoding="utf-8"))["companies"]


def test_fetch_and_save_then_review_then_score_only_chosen_company(ui_cfg, hashing_embedder):
    jobs = [raw_job(1, title="Principal Integration Architect", company="Prudential", location="Remote (US)"),
            raw_job(2, title="Delivery Lead", company="Cigna", location="Remote (US)"),
            raw_job(3, title="Registered Nurse", company="Cigna", location="Remote (US)")]
    llm = FakeLLM()
    app = create_app(ui_cfg, start_scheduler=False, runtime_factories={
        "llm_factory": lambda: llm, "embedder_factory": lambda: hashing_embedder,
        "sources_factory": lambda names: [FakeSource("workday", jobs)]})
    with TestClient(app) as c:
        # 1) fetch & save without scoring
        r = c.post("/runs", data={"mode": "fetch", "companies": ["prudential", "cigna"]}, follow_redirects=False)
        run_id = int(r.headers["location"].rsplit("/", 1)[1])
        _wait(lambda: queries.get_run(_engine(), run_id)["status"] != "running")
        run = queries.get_run(_engine(), run_id)
        assert run["status"] == "complete" and run["counts"]["waiting_to_score"] == 2 and llm.calls == 0
        assert "saved and waiting to be scored" in c.get(f"/runs/{run_id}").text
        # 2) review them unscored
        queue = c.get("/queue?status=discovered&sort=company").text
        assert "Principal Integration Architect" in queue and "Delivery Lead" in queue and "Registered Nurse" not in queue
        assert "waiting to be scored" in queue and "Score now" in queue
        # 3) score only Cigna's, from the queue banner (company filter → selection)
        assert "Score these" in c.get("/queue?status=discovered&company=Cigna&sort=company").text
        r = c.post("/runs", data={"mode": "stored", "companies": "Cigna", "max_to_score": "5"}, follow_redirects=False)
        run2 = int(r.headers["location"].rsplit("/", 1)[1])
        _wait(lambda: queries.get_run(_engine(), run2)["status"] != "running")
        statuses = {j["company_name_raw"]: j["status"] for j in
                    queries.list_queue(_engine(), queries.QueueFilters(status="any"), paginate=False)[0]
                    if j["status"] != "filtered_out"}
        assert statuses == {"Cigna": "scored", "Prudential": "discovered"} and llm.calls == 1


def test_a_score_later_run_still_gets_adzuna_jobs_their_full_posting(ui_cfg, hashing_embedder, monkeypatch):
    """Seen live: a "fetch & save" run left Adzuna jobs as 500-character snippets, because the full text was only
    fetched just before scoring. It needs no model, so it happens in that mode too."""
    from jobhunt.sources import adzuna

    snippet = raw_job(1, title="Integration Lead", company="ADP", location="Remote (US)", source="adzuna",
                      description="Project Advisor - a short snippet…", extra={"description_truncated": True})
    monkeypatch.setattr(adzuna, "fetch_full_description",
                        lambda job, client: adzuna.FullText("The whole posting. " * 80, False, "from the page"))
    llm = FakeLLM()
    app = create_app(ui_cfg, start_scheduler=False, runtime_factories={
        "llm_factory": lambda: llm, "embedder_factory": lambda: hashing_embedder,
        "sources_factory": lambda names: [FakeSource("adzuna", [snippet])]})
    with TestClient(app) as c:
        r = c.post("/runs", data={"mode": "fetch"}, follow_redirects=False)
        run_id = int(r.headers["location"].rsplit("/", 1)[1])
        _wait(lambda: queries.get_run(_engine(), run_id)["status"] != "running")
        assert queries.get_run(_engine(), run_id)["status"] == "complete" and llm.calls == 0
        job = next(j for j in queries.list_queue(_engine(), queries.QueueFilters(status="any"), paginate=False)[0]
                   if j["company_name_raw"] == "ADP")
        full = queries.get_job(_engine(), job["id"])
        assert full["status"] == "discovered" and full["full_description"] == 1
        assert full["description_text"].startswith("The whole posting.")


def test_a_run_can_leave_out_jobs_posted_before_a_date(ui_cfg, hashing_embedder):
    """"Get new jobs since last week": postings dated before the run's date are not stored or scored; undated ones
    are kept; an older job already waiting from an earlier run waits on."""
    from datetime import UTC, datetime, timedelta

    jobs = [raw_job(1, title="Integration Lead", company="Prudential", location="Remote (US)", posted_days_ago=2),
            raw_job(2, title="Delivery Lead", company="Prudential", location="Remote (US)", posted_days_ago=20)]
    undated = raw_job(3, title="Platform Lead", company="Prudential", location="Remote (US)")
    jobs.append(undated.__class__(**{**undated.__dict__, "posted_at": None}))
    llm = FakeLLM()
    app = create_app(ui_cfg, start_scheduler=False, runtime_factories={
        "llm_factory": lambda: llm, "embedder_factory": lambda: hashing_embedder,
        "sources_factory": lambda names: [FakeSource("workday", jobs)]})
    with TestClient(app) as c:
        engine = _engine()
        old = normalize_raw(raw_job(9, title="Architect Lead", company="Prudential", location="Remote (US)",
                                    posted_days_ago=40))
        _, waiting = queries.upsert_job(engine, old)
        week_ago = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%d")
        r = c.post("/runs", data={"mode": "fetch", "posted_since": week_ago}, follow_redirects=False)
        run_id = int(r.headers["location"].rsplit("/", 1)[1])
        _wait(lambda: queries.get_run(_engine(), run_id)["status"] != "running")
        run = queries.get_run(engine, run_id)
        assert run["status"] == "complete" and run["counts"]["older_than_date"] == 1
        titles = {j["title"] for j in queries.list_queue(engine, queries.QueueFilters(status="any"), paginate=False)[0]}
        assert {"Integration Lead", "Platform Lead"} <= titles and "Delivery Lead" not in titles
        assert run["counts"]["waiting_to_score"] == 2                 # the 40-day-old waiting job is not in this run
        assert queries.get_job(engine, waiting)["status"] == "discovered"
        assert "Posted before your date" in c.get(f"/runs/{run_id}").text
        assert 'name="posted_since"' in c.get("/runs").text
        bad = c.post("/runs", data={"mode": "fetch", "posted_since": "not-a-date"}, follow_redirects=False)
        assert "date" in bad.headers["location"].lower()


def test_sqlalchemy_c_extension_is_not_loaded():
    import sqlalchemy.util._has_cy as has_cy

    assert has_cy.HAS_CYEXTENSION is False  # avoids Smart App Control blocking the unsigned .pyd


def test_blocked_dll_gives_a_clear_embedding_error(monkeypatch):
    import builtins

    from jobhunt.embeddings import EmbedderUnavailable, TransformersEmbedder

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if name == "transformers":
            raise ImportError("DLL load failed while importing _x: An Application Control policy has blocked this file.")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(EmbedderUnavailable, match="Smart App Control"):
        TransformersEmbedder("BAAI/bge-small-en-v1.5").embed(["x"])


def test_scipy_and_sklearn_are_not_needed():
    import importlib.util

    assert importlib.util.find_spec("sentence_transformers") is None
    assert importlib.util.find_spec("scipy") is None and importlib.util.find_spec("sklearn") is None


def test_network_access_needs_a_password_and_writes_host_and_env(ui_cfg):
    from jobhunt.services import settings as settings_service

    with TestClient(create_app(ui_cfg, start_scheduler=False)) as c:
        rt = runtime.get_runtime()
        page = c.get("/settings").text
        assert 'id="network"' in page and "this PC only" in page and "netsh advfirewall" in page
        bad = c.post("/settings/network", data={"lan": "on", "port": "8000"}, follow_redirects=False)
        assert "set+a+password+first" in bad.headers["location"]
        short = c.post("/settings/network", data={"lan": "on", "web_password": "short", "port": "8000"},
                       follow_redirects=False)
        assert "at+least+8+characters" in short.headers["location"]
        r = c.post("/settings/network", data={"lan": "on", "web_password": "laptop-access-1", "port": "8001"},
                   follow_redirects=False)
        assert "Restart" in r.headers["location"]
        assert rt.cfg.web.host == "0.0.0.0" and rt.cfg.web.port == 8001  # noqa: S104
        env = (ui_cfg.config_path.parent / ".env").read_text(encoding="utf-8")
        assert "WEB_PASSWORD=laptop-access-1" in env
        assert 'host: "0.0.0.0"' in ui_cfg.config_path.read_text(encoding="utf-8")
        view = settings_service.network_view(rt)
        assert view["lan"] and view["password_set"] and view["firewall"].endswith("profile=private")
        c.post("/settings/network", data={"port": "8000"}, follow_redirects=False)  # unticked: back to local only
        assert runtime.get_runtime().cfg.web.host == "127.0.0.1"


def test_the_queue_can_show_just_one_runs_scored_jobs(ui_cfg, hashing_embedder):
    """After a run, "show me what this run scored". A score carries no run id, so the run's own window of time
    is what identifies its work."""

    def score(engine, job_id, total, scored_at):  # noqa: ANN001, ANN202
        queries.record_score(engine, job_id, {
            "model": "claude-x", "rubric_version": "v4", "total": total, "skill_match": 80, "domain_fit": 80,
            "role_fit": 80, "seniority_fit": 80, "comp_fit": 50, "logistics_fit": 70, "focus_fit": 50,
            "red_flags": [], "gaps": [], "evidence": [], "rationale": "ok", "ghost_score": 10,
            "low_confidence": 0, "focus_hits": [], "scored_at": scored_at})

    with TestClient(_app(ui_cfg, hashing_embedder)) as client:
        engine = _engine()
        run_id = queries.create_run(engine, trigger="manual", sources=["workday"])
        _, first = queries.upsert_job(engine, normalize_raw(raw_job(1, title="Integration Architect")))
        _, second = queries.upsert_job(engine, normalize_raw(raw_job(2, title="Platform Lead")))
        score(engine, first, 88.0, utcnow_iso())
        score(engine, second, 61.0, utcnow_iso())
        queries.update_run_progress(engine, run_id, status="complete", finished=True)

        assert queries.run_scored_count(engine, run_id) == 2
        page = client.get(f"/queue?run={run_id}&status=any&min_score=0&sort=total").text
        assert "Integration Architect" in page and "Platform Lead" in page

        # a job scored after the run finished is not that run's work
        _, third = queries.upsert_job(engine, normalize_raw(raw_job(3, title="Delivery Manager")))
        score(engine, third, 90.0, "2027-01-01T00:00:00Z")
        assert queries.run_scored_count(engine, run_id) == 2
        assert "Delivery Manager" not in client.get(f"/queue?run={run_id}&status=any&min_score=0").text
        # and it is there without the filter
        assert "Delivery Manager" in client.get("/queue?status=any&min_score=0").text


def test_the_queue_can_show_just_the_jobs_the_latest_run_added(ui_cfg, hashing_embedder):
    """A job counts as added by a run when it was first retrieved while that run was going. ?added=latest follows
    the newest run, so the link keeps working after the next run."""
    with TestClient(_app(ui_cfg, hashing_embedder)) as client:
        engine = _engine()
        _, old = queries.upsert_job(engine, normalize_raw(raw_job(1, title="Integration Architect")))
        first = queries.create_run(engine, trigger="manual", sources=["workday"])
        _, a = queries.upsert_job(engine, normalize_raw(raw_job(2, title="Platform Lead")))
        queries.update_run_progress(engine, first, status="complete", finished=True)
        second = queries.create_run(engine, trigger="manual", sources=["workday"])
        _, b = queries.upsert_job(engine, normalize_raw(raw_job(3, title="Delivery Manager")))
        queries.update_run_progress(engine, second, status="complete", finished=True)
        queries.update_run_progress(engine, first, counts={"new": 1})
        queries.update_run_progress(engine, second, counts={"new": 1})
        # a later run that only scored the backlog added nothing, so "latest" still means the second run
        catch_up = queries.create_run(engine, trigger="manual", sources=[])
        queries.update_run_progress(engine, catch_up, counts={"new": 0, "scored": 2})
        assert queries.latest_run_id(engine) == second
        with engine.begin() as c:  # real runs are minutes apart; this one ran in a single second
            for run_id, start, end in ((first, "2026-09-01T10:00:00Z", "2026-09-01T10:30:00Z"),
                                       (second, "2026-09-02T10:00:00Z", "2026-09-02T10:30:00Z")):
                c.execute(text("UPDATE runs SET started_at = :s, finished_at = :e WHERE id = :id"),
                          {"s": start, "e": end, "id": run_id})
            for job_id, seen in ((old, "2020-01-01T00:00:00Z"), (a, "2026-09-01T10:05:00Z"),
                                 (b, "2026-09-02T10:05:00Z")):
                c.execute(text("UPDATE jobs SET first_seen_at = :t WHERE id = :id"), {"t": seen, "id": job_id})

        latest = client.get("/queue?added=latest&status=any").text
        # a minimum of 0 is no minimum: jobs not scored yet still show
        assert "Delivery Manager" in client.get("/queue?added=latest&status=any&min_score=0").text
        assert "Delivery Manager" in latest and "Platform Lead" not in latest and "Integration Architect" not in latest
        earlier = client.get(f"/queue?added={first}&status=any").text
        assert "Platform Lead" in earlier and "Delivery Manager" not in earlier
        everything = client.get("/queue?status=any").text
        assert all(t in everything for t in ("Integration Architect", "Platform Lead", "Delivery Manager"))
        assert 'value="latest" selected' in latest  # the filter shows what it's set to


def test_the_queue_filters_by_when_jobs_were_scored_and_retrieved(ui_cfg, hashing_embedder):
    """"Scored" and "Retrieved": any time (the default), or the last 24 hours / 3 / 7 / 14 / 30 days."""
    from jobhunt.db.engine import iso_shift

    with TestClient(_app(ui_cfg, hashing_embedder)) as client:
        engine = _engine()
        _, fresh = queries.upsert_job(engine, normalize_raw(raw_job(1, title="Integration Architect")))
        _, old = queries.upsert_job(engine, normalize_raw(raw_job(2, title="Platform Lead")))
        for job_id, when in ((fresh, iso_shift(-0.5)), (old, iso_shift(-20))):
            queries.record_score(engine, job_id, {
                "model": "claude-x", "rubric_version": "v7", "total": 80.0, "skill_match": 80, "domain_fit": 80,
                "role_fit": 80, "seniority_fit": 80, "comp_fit": 50, "logistics_fit": 70, "focus_fit": 50,
                "red_flags": [], "gaps": [], "evidence": [], "rationale": "ok", "ghost_score": 10,
                "low_confidence": 0, "focus_hits": [], "scored_at": when})
        with engine.begin() as c:
            c.execute(text("UPDATE jobs SET first_seen_at = :t WHERE id = :id"), {"t": iso_shift(-40), "id": old})

        page = client.get("/queue?status=any&min_score=0").text
        assert "Integration Architect" in page and "Platform Lead" in page          # default: any time
        assert 'id="f-scored"' in page and 'id="f-retrieved"' in page
        day = client.get("/queue?status=any&min_score=0&scored=1").text
        assert "Integration Architect" in day and "Platform Lead" not in day
        month = client.get("/queue?status=any&min_score=0&scored=30").text
        assert "Integration Architect" in month and "Platform Lead" in month       # scored 20 days ago
        recent = client.get("/queue?status=any&min_score=0&retrieved=30").text
        assert "Integration Architect" in recent and "Platform Lead" not in recent  # retrieved 40 days ago
        assert '<option value="30" selected>last 30 days</option>' in recent
        assert "Platform Lead" in client.get("/queue?status=any&min_score=0&scored=bogus").text   # ignored
        assert "scored today" in page and "scored 20d ago" in page                  # under each score


def test_the_queue_shows_when_each_job_was_retrieved(ui_cfg, hashing_embedder):
    with TestClient(_app(ui_cfg, hashing_embedder)) as client:
        queries.upsert_job(_engine(), normalize_raw(raw_job(1, title="Integration Architect")))
        page = client.get("/queue?status=any&min_score=0&sort=seen").text
        assert "Retrieved" in page                      # the column is there and sortable
        assert "sort=seen_asc" in page
