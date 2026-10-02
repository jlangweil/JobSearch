"""Local model support (Ollama / OpenAI-compatible) for scoring, tested against a simulated server."""

from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from jobhunt.config import LocalLLM as LocalSettings
from jobhunt.db import queries
from jobhunt.graphs import runtime
from jobhunt.graphs.discovery import graph as dg
from jobhunt.graphs.runtime import init_runtime
from jobhunt.llm.client import LLMOutputError, LLMUnavailable, LocalLLM, RoutingLLM, check_local_model
from jobhunt.llm.schemas import JobScore
from jobhunt.sources.normalize import normalize_raw
from jobhunt.web.app import create_app
from tests.conftest import FakeSource, raw_job
from tests.test_web_control import _wait, ui_cfg  # noqa: F401  (fixture re-export)

SCORE = {
    "stack": [], "role": {"role_type": "technical_lead", "hands_on": "medium", "quote": "own the API platform"},
    "competencies": [],
    "skill_match": 82, "role_fit": 90, "domain_fit": 75, "seniority_fit": 85, "comp_fit": 50, "logistics_fit": 80,
    "red_flags": [], "gaps": ["no comp posted"],
    "evidence": [{"claim": "API ownership", "quote": "own the API platform", "dimension": "skill_match"},
                 {"claim": "leads", "quote": "Reports to the hiring manager", "dimension": "role_fit"}],
    "rationale": "Leadership-leaning integration role.",
}


class FakeOllama:
    def __init__(self, *, models=("qwen3:8b",), content=None, prompt_tokens=3000, down=False):  # noqa: ANN001
        self.models, self.prompt_tokens, self.down = list(models), prompt_tokens, down
        self.content = content if content is not None else "<think></think>\n" + json.dumps(SCORE)
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, json={"version": "0.12.3"})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        if path == "/api/chat":
            body = json.loads(request.content)
            self.bodies.append(body)
            if body["model"] not in self.models:
                return httpx.Response(404, json={"error": f'model "{body["model"]}" not found, try pulling it first'})
            return httpx.Response(200, json={"model": body["model"],
                                             "message": {"role": "assistant", "content": self.content},
                                             "done": True, "prompt_eval_count": self.prompt_tokens, "eval_count": 400})
        if path == "/v1/chat/completions":
            self.bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(SCORE)}}],
                                             "usage": {"prompt_tokens": 2500, "completion_tokens": 380}})
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "qwen3-8b"}]})
        return httpx.Response(404)


def _client(fake: FakeOllama) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(fake))


def _local(engine, fake, **settings):  # noqa: ANN001, ANN202
    s = LocalSettings(**settings)
    return LocalLLM(engine, lambda: s, _client(fake))


def _call(llm):  # noqa: ANN001, ANN202
    return llm.structured(JobScore, system="s", user="u", purpose="score", model="", prompt_version="v")


def test_ollama_structured_call_sends_schema_and_logs_zero_cost(cfg, engine):
    fake = FakeOllama()
    out = _local(engine, fake).structured(JobScore, system="sys", user="posting", purpose="score", model="",
                                          prompt_version="score_job.v2", job_id=None)
    assert out.role_fit == 90 and out.evidence[0].quote == "own the API platform"
    body = fake.bodies[0]
    assert body["model"] == "qwen3:8b" and body["stream"] is False and body["think"] is False
    assert body["format"]["properties"]["role_fit"] and body["options"]["num_ctx"] == 16384
    assert body["options"]["temperature"] == 0.0 and body["messages"][1]["content"].endswith("/no_think")
    call = queries.spend_by_day(engine, "2000-01-01T00:00:00Z")[0]
    assert call["cost_usd"] == 0 and call["input_tokens"] == 3000


def test_ollama_errors_are_clear(cfg, engine):
    with pytest.raises(LLMUnavailable, match="ollama pull qwen3:8b"):
        _call(_local(engine, FakeOllama(models=["llama3:8b"])))
    with pytest.raises(LLMOutputError, match="num_ctx"):
        _call(_local(engine, FakeOllama(prompt_tokens=16384)))
    with pytest.raises(LLMOutputError, match="did not match the schema"):
        _call(_local(engine, FakeOllama(content='{"skill_match": "high"}')))
    with pytest.raises(LLMUnavailable, match="is Ollama running"):
        _call(_local(engine, FakeOllama(down=True)))
    assert "ollama pull qwen3:8b" in _local(engine, FakeOllama(models=[])).check()["message"]
    assert not check_local_model(_local(engine, FakeOllama(down=True)))["ok"]
    assert check_local_model(_local(engine, FakeOllama(content='{"ok": true, "sentence": "JSON works."}')))["ok"]


def test_openai_compatible_server(cfg, engine):
    fake = FakeOllama()
    llm = _local(engine, fake, api="openai", base_url="http://127.0.0.1:1234", model="qwen3-8b")
    assert _call(llm).skill_match == 82
    assert fake.bodies[0]["response_format"]["json_schema"]["name"] == "JobScore"
    assert llm.check()["ok"]


def test_router_uses_local_for_scoring_without_an_api_key_and_labels_scores(cfg, engine, hashing_embedder):
    cfg.scoring.provider = "local"
    jobs = [raw_job(1, title="Principal Integration Architect", company="Insurer A", location="Remote (US)")]
    rt = init_runtime(cfg, embedder_factory=lambda: hashing_embedder,
                      sources_factory=lambda names: [FakeSource("workday", jobs)],
                      local_http_client=_client(FakeOllama()))
    assert cfg.secrets.ANTHROPIC_API_KEY is None
    run_id, initial = dg.start_run(rt, trigger="cli", sources=None, max_to_score=None)
    result = dg.execute(rt, run_id, initial)
    assert result["counts"]["scored"] == 1, result["errors"]
    job = queries.list_queue(engine, queries.QueueFilters(status="scored", min_score=0), paginate=False)[0][0]
    assert queries.latest_score(engine, job["id"])["model"] == "local:qwen3:8b"
    router = rt.llm()
    assert isinstance(router, RoutingLLM) and router.concurrency("score") == cfg.local_llm.concurrency
    assert router.provider_for("draft_resume") == "anthropic"  # tailoring stays on Claude
    assert router.with_provider("anthropic").provider_for("score") == "anthropic"


def test_scoring_run_fails_fast_when_local_server_is_down(cfg, engine, hashing_embedder):
    cfg.scoring.provider = "local"
    jobs = [raw_job(i, title=f"Principal Integration Architect {i}", company=f"Co {i}", location="Remote (US)")
            for i in range(5)]
    rt = init_runtime(cfg, embedder_factory=lambda: hashing_embedder,
                      sources_factory=lambda names: [FakeSource("workday", jobs)],
                      local_http_client=_client(FakeOllama(down=True)))
    run_id, initial = dg.start_run(rt, trigger="cli", sources=None, max_to_score=None)
    with pytest.raises(LLMUnavailable):
        dg.execute(rt, run_id, initial)
    run = queries.get_run(engine, run_id)
    assert run["status"] == "failed" and any("cannot reach" in e["message"] for e in run["errors"])
    assert len(run["errors"]) == 1  # one clear error, not one per job
    assert queries.status_counts(engine)["discovered"] == 5  # nothing lost; resume after starting Ollama


def test_settings_page_switches_provider_tests_model_and_job_page_compares(ui_cfg, hashing_embedder):  # noqa: F811
    fake = FakeOllama(models=["qwen3:8b", "qwen3:8b-q8_0"])
    app = create_app(ui_cfg, start_scheduler=False, runtime_factories={
        "embedder_factory": lambda: hashing_embedder, "local_http_client": _client(fake)})
    with TestClient(app) as c:
        page = c.get("/settings").text
        assert "Language models" in page and 'value="local"' in page and "Test local model" in page
        r = c.post("/settings/llm", data={"scoring_provider": "local", "tailoring_provider": "anthropic",
                                          "local_api": "ollama", "local_base_url": "http://127.0.0.1:11434",
                                          "local_model": "qwen3:8b-q8_0", "local_num_ctx": "20480",
                                          "local_concurrency": "3", "local_keep_alive": "1h",
                                          "local_timeout_s": "240"}, follow_redirects=False)
        assert "scoring+%E2%86%92+local" in r.headers["location"], r.headers["location"]
        text = ui_cfg.config_path.read_text(encoding="utf-8")
        assert '  provider: "local"   # anthropic (Claude' in text  # comment kept
        assert 'model: "qwen3:8b-q8_0"' in text and "num_ctx: 20480" in text
        cfg_now = runtime.get_runtime().cfg
        assert cfg_now.scoring.provider == "local" and cfg_now.local_llm.concurrency == 3
        bad = c.post("/settings/llm", data={"local_base_url": "localhost:11434"}, follow_redirects=False)
        assert "error=" in bad.headers["location"] and "http" in bad.headers["location"]

        c.post("/settings/llm/test")
        _wait(lambda: runtime.get_runtime().llm_test.get("status") == "done")
        assert "llm-test" in c.get("/settings/llm/test").text

        engine = runtime.get_runtime().engine
        _, job_id = queries.upsert_job(engine, normalize_raw(raw_job(5, title="Delivery Lead", location="Remote (US)")))
        job_page = c.get(f"/jobs/{job_id}").text
        assert "(local qwen3:8b-q8_0)" in job_page and "Score with Claude" in job_page
        assert "free, local model" in c.get("/queue?status=discovered&sort=seen").text
        c.post(f"/jobs/{job_id}/score")
        _wait(lambda: queries.latest_score(engine, job_id) is not None)
        time.sleep(0.2)
        assert queries.latest_score(engine, job_id)["model"] == "local:qwen3:8b-q8_0"
        c.post(f"/jobs/{job_id}/score", data={"provider": "anthropic"})
        _wait(lambda: "Last scoring attempt failed" in c.get(f"/jobs/{job_id}").text)
        assert "ANTHROPIC_API_KEY" in c.get(f"/jobs/{job_id}").text  # override honored; no key → clear error
