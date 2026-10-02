"""Adding a job by hand (link or pasted text), Adzuna settings from the UI, and nested config.yaml edits."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from jobhunt.db import queries
from jobhunt.graphs import runtime
from jobhunt.llm.schemas import PostingFields
from jobhunt.services import config_edit, manual_import, settings
from jobhunt.web.app import create_app
from tests.conftest import FakeSource, load_fixture_json, raw_job
from tests.test_web_control import _wait, ui_cfg  # noqa: F401  (fixture re-export)

POSTING = ("Lead Integration Engineer\nAcme Mutual — Parsippany, NJ (Hybrid)\n\nYou will own our MuleSoft API platform, "
           "lead a team of six engineers, and run the migration of 40 legacy interfaces.\n\nRequirements:\n"
           "- 8+ years of integration experience\n- MuleSoft certification preferred\nSalary: $165,000 - $190,000")


class ExtractLLM:
    calls = 0

    def structured(self, schema, **kw):  # noqa: ANN001, ANN003
        assert schema is PostingFields and kw["purpose"] == "extract_posting"
        ExtractLLM.calls += 1
        return PostingFields(title="Lead Integration Engineer", company="Acme Mutual", location="Parsippany, NJ",
                             work_arrangement="hybrid", salary_min=165000, salary_max=190000)

    def model_label(self, purpose):  # noqa: ANN001
        return "local:qwen3:8b"


def test_read_url_uses_workday_api_json_ld_and_blocks_linkedin(cfg):
    details = load_fixture_json("workday", "pru_details.json")
    path = next(iter(details))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pru.wd5.myworkdayjobs.com":
            assert request.url.path == f"/wday/cxs/pru/Careers{path}"
            return httpx.Response(200, json=details[path])
        if request.url.host == "careers.example.org":
            page = ('<html><head><script type="application/ld+json">{"@context":"https://schema.org","@graph":[{'
                    '"@type":"JobPosting","title":"Delivery Lead","hiringOrganization":{"name":"Example Health"},'
                    '"jobLocationType":"TELECOMMUTE","jobLocation":{"address":{"addressLocality":"Newark",'
                    '"addressRegion":"NJ"}},"datePosted":"2026-09-10","description":"&lt;p&gt;'
                    + "Lead delivery of API integrations. " * 20 + '&lt;/p&gt;","baseSalary":{"value":{'
                    '"minValue":150000,"maxValue":180000,"unitText":"YEAR"}}}]}</script></head></html>')
            return httpx.Response(200, text=page)
        if request.url.host == "thin.example.org":
            return httpx.Response(200, text="<html><title>Careers</title><div id=app></div></html>")
        return httpx.Response(404)

    rt = SimpleNamespace(cfg=cfg)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    wd = manual_import.read_url(rt, f"https://pru.wd5.myworkdayjobs.com/en-US/Careers{path}", client=client)
    assert wd["source"] == "workday" and wd["source_job_id"].startswith("pru/Lead--Infrastructure")
    assert wd["title"] == "Lead, Network Security Operations - Virtual" and wd["read_with"] == "Workday job API"
    assert len(wd["description"]) > 300 and wd["company"]

    ld = manual_import.read_url(rt, "careers.example.org/jobs/77", client=client)
    assert (ld["title"], ld["company"], ld["remote_type"]) == ("Delivery Lead", "Example Health", "remote")
    assert ld["comp_min"] == 150000 and "Newark, NJ" in ld["location"] and "JobPosting" in ld["read_with"]

    with pytest.raises(manual_import.ImportProblem, match="JavaScript"):
        manual_import.read_url(rt, "https://thin.example.org/job/1", client=client)
    with pytest.raises(manual_import.ImportProblem, match="(?i)paste"):
        manual_import.read_url(rt, "https://www.linkedin.com/jobs/view/42", client=client)


def _app(ui_cfg, hashing_embedder, llm=None, jobs=None):  # noqa: ANN001, ANN202, F811
    return create_app(ui_cfg, start_scheduler=False, runtime_factories={
        "llm_factory": lambda: llm or ExtractLLM(), "embedder_factory": lambda: hashing_embedder,
        "sources_factory": lambda names: [FakeSource("workday", jobs or [])]})


def test_add_job_from_pasted_text_then_applied_or_tailor(ui_cfg, hashing_embedder):  # noqa: F811
    ui_cfg.tailoring.provider = "local"
    with TestClient(_app(ui_cfg, hashing_embedder)) as c:
        rt = runtime.get_runtime()
        assert "Add a job" in c.get("/jobs/new").text
        page = c.post("/jobs/new/read", data={"text": POSTING, "url": "https://jobs.acme.test/42"}).text
        assert 'value="Lead Integration Engineer"' in page and 'value="Acme Mutual"' in page
        assert "filled in by local:qwen3:8b" in page and "MuleSoft API platform" in page

        form = {"title": "Lead Integration Engineer", "company": "Acme Mutual", "location": "Parsippany, NJ",
                "remote_type": "hybrid", "comp_min": "165000", "comp_max": "190,000", "url": "https://jobs.acme.test/42",
                "apply_url": "", "description": POSTING, "source": "manual", "next_step": "applied",
                "applied_on": "2026-09-12", "method": "manual", "confirmation_ref": "ACME-1"}
        r = c.post("/jobs/new", data=form, follow_redirects=False)
        assert r.headers["location"].startswith("/applications"), r.headers["location"]
        job = queries.list_queue(rt.engine, queries.QueueFilters(status="applied"), paginate=False)[0][0]
        assert job["source"] == "manual" and job["comp_min"] == 165000 and job["remote_type"] == "hybrid"
        apps = c.get("/applications").text
        assert "Lead Integration Engineer" in apps and "2026-09-12" in apps and "ACME-1" in apps

        again = c.post("/jobs/new/read", data={"text": POSTING, "url": "https://jobs.acme.test/42"}).text
        assert "already saved as" in again and f"job #{job['id']}" in again

        second = form | {"title": "Integration Delivery Lead", "url": "https://jobs.acme.test/43",
                         "next_step": "tailor", "emphasis": "MuleSoft"}
        r = c.post("/jobs/new", data=second, follow_redirects=False)
        assert "/workflow" in r.headers["location"], r.headers["location"]
        new_id = int(r.headers["location"].split("/")[2])
        assert queries.get_job(rt.engine, new_id)["status"] == "in_workflow"


def test_manual_jobs_survive_later_runs_and_adzuna_snippets_can_be_replaced(ui_cfg, hashing_embedder):  # noqa: F811
    onsite = raw_job(1, title="Lead Integration Engineer", company="Faraway Co", location="Dallas, TX")
    with TestClient(_app(ui_cfg, hashing_embedder, jobs=[raw_job(2, title="Staff Architect", location="Remote")])) as c:
        rt = runtime.get_runtime()
        form = {"title": onsite.title, "company": onsite.company_name, "location": "Dallas, TX", "remote_type": "onsite",
                "description": POSTING, "url": "https://faraway.test/1", "next_step": "save"}
        r = c.post("/jobs/new", data=form, follow_redirects=False)
        manual_id = int(r.headers["location"].split("/")[2].split("?")[0])
        assert queries.get_job(rt.engine, manual_id)["status_reason"] == queries.MANUAL_REASON

        r = c.post("/runs", data={"mode": "fetch"}, follow_redirects=False)
        run_id = int(r.headers["location"].rsplit("/", 1)[1])
        _wait(lambda: queries.get_run(rt.engine, run_id)["status"] != "running")
        assert queries.get_job(rt.engine, manual_id)["status"] == "discovered"  # an on-site Dallas job isn't filtered

        tailor_btn = c.get("/queue?status=discovered&sort=seen").text
        assert "Tailor without scoring" in tailor_btn

        snippet = queries.upsert_job(rt.engine, __import__("jobhunt.sources.normalize", fromlist=["x"]).normalize_raw(
            raw_job(9, title="Integration Lead", company="Snippet Co", source="adzuna",
                    description="<p>Short Adzuna snippet…</p>")))[1]
        assert "Paste the full posting" in c.get(f"/jobs/{snippet}").text
        page = c.get(f"/jobs/new?replace={snippet}").text
        assert "Updating" in page and "short snippet" in page
        r = c.post("/jobs/new", data={"title": "Integration Lead", "company": "Snippet Co", "description": POSTING,
                                      "replace_job_id": str(snippet), "source": "adzuna", "next_step": "save"},
                   follow_redirects=False)
        assert "Updated+the+existing+job" in r.headers["location"]
        assert "MuleSoft API platform" in queries.get_job(rt.engine, snippet)["description_text"]


def test_nested_config_edits_convert_flow_mappings_and_keep_comments():
    text = ('sources:\n  # keep me\n  greenhouse: {enabled: true, rate_limit_per_sec: 1}\n  adzuna: {enabled: false, '
            'pages: 2}\nweb:\n  port: 8000\n')
    out = config_edit._set_path(text, ["sources", "adzuna", "enabled"], "true")
    out = config_edit._set_path(out, ["sources", "adzuna", "queries"], '["mulesoft", "api"]')
    out = config_edit._set_path(out, ["sources", "greenhouse", "enabled"], "false")
    import yaml

    data = yaml.safe_load(out)
    assert data["sources"]["adzuna"] == {"enabled": True, "pages": 2, "queries": ["mulesoft", "api"]}
    assert data["sources"]["greenhouse"]["enabled"] is False and data["web"] == {"port": 8000}
    assert "# keep me" in out


def test_adzuna_settings_save_keys_to_env_test_and_run(ui_cfg, hashing_embedder):  # noqa: F811
    payload = load_fixture_json("adzuna", "search.json")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params.get("app_key") != "secret-key":
            return httpx.Response(401, json={"exception": "AUTH_FAIL"})
        return httpx.Response(200, json=payload)

    with TestClient(_app(ui_cfg, hashing_embedder)) as c:
        rt = runtime.get_runtime()
        page = c.get("/settings").text
        assert 'id="adzuna"' in page and "no keys yet" in page
        bad = c.post("/settings/adzuna", data={"enabled": "on", "queries": "mulesoft"}, follow_redirects=False)
        assert "App+ID" in bad.headers["location"]
        r = c.post("/settings/adzuna", data={"app_id": "my-id", "app_key": "secret-key", "enabled": "on",
                                             "queries": "mulesoft\nintegration architect", "location": "Newark, NJ",
                                             "distance_miles": "25", "max_days_old": "14", "pages": "1",
                                             "category": "it-jobs", "salary_min": "150,000",
                                             "exclude_words": "clearance intern", "title_only": "on"},
                   follow_redirects=False)
        assert "Adzuna+enabled" in r.headers["location"], r.headers["location"]
        env = (ui_cfg.config_path.parent / ".env").read_text(encoding="utf-8")
        assert "ADZUNA_APP_ID=my-id" in env and "ADZUNA_APP_KEY=secret-key" in env
        extra = rt.cfg.sources["adzuna"].model_extra
        assert rt.cfg.sources["adzuna"].enabled and extra["queries"] == ["mulesoft", "integration architect"]
        assert extra["salary_min"] == 150000 and extra["title_only"] is True
        assert rt.cfg.secrets.ADZUNA_APP_KEY == "secret-key" and "saved (…-key)" in c.get("/settings").text

        result = settings.test_adzuna(rt, client=httpx.Client(transport=httpx.MockTransport(handler)))
        assert result["ok"] and "finds 2 jobs" in result["message"] and len(result["samples"]) == 2
        p = seen[-1].url.params
        assert p["title_only"] == "mulesoft" and p["distance"] == "40" and p["what_exclude"] == "clearance intern"
        assert p["category"] == "it-jobs" and p["salary_min"] == "150000" and p["where"] == "Newark, NJ"

        r = c.post("/settings/adzuna", data={"app_id": "my-id", "app_key": "", "queries": "mulesoft"},
                   follow_redirects=False)  # blank key keeps the saved one; unticked = disabled
        assert rt.cfg.secrets.ADZUNA_APP_KEY == "secret-key" and not rt.cfg.sources["adzuna"].enabled

    from jobhunt.sources.registry import build_sources

    ui_cfg.sources["adzuna"].enabled = True
    ui_cfg.secrets.ADZUNA_APP_ID = ui_cfg.secrets.ADZUNA_APP_KEY = None
    names = [s.name for s in build_sources(ui_cfg, ["greenhouse", "adzuna"])]
    assert names == ["greenhouse"]  # missing keys skip Adzuna instead of breaking the whole run


def test_score_first_then_tailor_by_hand_or_automatically(ui_cfg, hashing_embedder):  # noqa: F811
    from tests.conftest import FakeLLM

    ui_cfg.tailoring.provider = "local"
    base = {"company": "Acme Mutual", "location": "Remote (US)", "remote_type": "remote", "description": POSTING,
            "source": "manual", "next_step": "score", "score_provider": "local"}
    with TestClient(_app(ui_cfg, hashing_embedder, llm=FakeLLM())) as c:
        rt = runtime.get_runtime()
        form = c.post("/jobs/new/read", data={"text": POSTING}).text
        assert 'value="score" class="mt-1" checked' in form and "Tailor now, without scoring" in form

        r = c.post("/jobs/new", data=base | {"title": "Lead Integration Engineer", "url": "https://acme.test/1"},
                   follow_redirects=False)
        job_id = int(r.headers["location"].split("/")[2].split("?")[0])
        _wait(lambda: queries.latest_score(rt.engine, job_id) is not None)
        _wait(lambda: "Tailor my resume &amp; cover letter" in c.get(f"/jobs/{job_id}").text)
        assert queries.get_job(rt.engine, job_id)["status"] == "scored"

        low = c.post("/jobs/new", data=base | {"title": "Integration Lead II", "url": "https://acme.test/2",
                                               "auto_tailor": "on", "tailor_min": "100"}, follow_redirects=False)
        low_id = int(low.headers["location"].split("/")[2].split("?")[0])
        _wait(lambda: "below your 100 minimum" in c.get(f"/jobs/{low_id}").text)
        assert queries.get_job(rt.engine, low_id)["status"] == "scored"

        high = c.post("/jobs/new", data=base | {"title": "Integration Lead III", "url": "https://acme.test/3",
                                                "auto_tailor": "on", "tailor_min": "10", "emphasis": "MuleSoft"},
                      follow_redirects=False)
        assert "tailoring+starts+by+itself" in high.headers["location"]
        high_id = int(high.headers["location"].split("/")[2].split("?")[0])
        _wait(lambda: queries.get_job(rt.engine, high_id)["status"] in ("in_workflow", "shortlisted"))
        assert "so tailoring started" in c.get(f"/jobs/{high_id}").text
