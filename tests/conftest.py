from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from jobhunt.config import PROJECT_ROOT, Scoring, load_config, set_config
from jobhunt.db import migrate
from jobhunt.db.engine import dispose_engines, get_engine
from jobhunt.embeddings import HashingEmbedder, set_embedder
from jobhunt.graphs import runtime
from jobhunt.llm.client import set_llm
from jobhunt.llm.schemas import Evidence, JobScore, RoleAnalysis
from jobhunt.sources.base import RawJob

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A full AppConfig pointing every path into tmp_path, with a migrated database."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with open(PROJECT_ROOT / "config.example.yaml", encoding="utf-8") as fh:   # the committed template
        raw = yaml.safe_load(fh)
    raw["paths"].update({
        "db": str(tmp_path / "data" / "jobhunt.db"),
        "checkpoints": str(tmp_path / "data" / "checkpoints.db"),
        "facts": str(tmp_path / "data" / "facts.yaml"),
        "output_root": str(tmp_path / "output"),
        "exports": str(tmp_path / "exports"),
        "resume_template": str(tmp_path / "data" / "resume_template.docx"),
        "soffice": None,
    })
    raw["filters"]["local_location_patterns"] = [r"\bNJ\b", "New Jersey"]
    raw["filters"]["local_states"] = ["NJ", "NY", "PA"]   # the test postings are in New Jersey
    if raw.get("sources", {}).get("adzuna"):
        raw["sources"]["adzuna"]["enabled"] = False  # the owner's own Adzuna switch must not leak into tests
    raw.setdefault("web", {})["host"] = "127.0.0.1"  # nor their network-access choice
    raw.setdefault("auto_apply", {})["enabled"] = False  # nor whether they have the auto-apply beta switched on
    # nor how they weigh a score: tests exercise every subscore and the ghost penalty, so they use the shipped mix
    shipped = Scoring()
    raw.setdefault("scoring", {}).update({"weights": dict(shipped.weights), "ghost_penalty": shipped.ghost_penalty})
    path = tmp_path / "config.yaml"
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh, allow_unicode=True, sort_keys=False)
    c = load_config(path)
    c.secrets.ANTHROPIC_API_KEY = None
    migrate.upgrade(c)
    set_config(c)
    yield c
    runtime.reset_runtime()
    set_config(None)
    set_llm(None)
    set_embedder(None)
    dispose_engines()


@pytest.fixture
def engine(cfg):
    return get_engine(cfg.paths.db)


def iso_days_ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def raw_job(i: int, *, title: str = "Principal Integration Architect", company: str = "Acme Insurance",
            location: str | None = "Parsippany, NJ", description: str | None = None, posted_days_ago: float = 2,
            source: str = "workday", comp: tuple[int | None, int | None] = (None, None), extra: dict | None = None
            ) -> RawJob:
    desc = description or (
        f"<p>Our integration team is hiring posting {i}. You will own the API platform and MuleSoft integrations "
        "for our insurance business. Hybrid role, full-time. Reports to the hiring manager, VP Architecture.</p>"
    )
    return RawJob(source, f"job-{i}", company, title, location, desc, f"https://example.test/jobs/{i}",
                  f"https://example.test/jobs/{i}/apply", iso_days_ago(posted_days_ago), comp[0], comp[1],
                  extra or {})


class FakeSource:
    def __init__(self, name: str, jobs: list[RawJob], fail: Exception | None = None) -> None:
        self.name = name
        self.jobs = jobs
        self.fail = fail
        self.errors: list[dict] = []
        self.fetch_calls = 0

    def fetch(self, since) -> Iterator[RawJob]:  # noqa: ANN001
        self.fetch_calls += 1
        if self.fail:
            raise self.fail
        yield from self.jobs


class FakeLLM:
    """Returns a well-formed JobScore quoting the posting; can be told to raise."""

    def __init__(self, scores: int = 80, raise_times: int = 0) -> None:
        self.scores = scores
        self.raise_times = raise_times
        self.calls = 0

    def structured(self, schema, *, system, user, purpose, model, prompt_version, job_id=None, max_tokens=4096,
                   cache_system=False):  # noqa: ANN001
        self.calls += 1
        if self.raise_times > 0:
            self.raise_times -= 1
            raise RuntimeError("simulated LLM outage")
        assert issubclass(schema, JobScore)
        quote = "own the API platform"
        analysis = {}
        if schema is not JobScore:  # rubric v5: a short stack leaves skill_match to the model; a lead role
            analysis = {"stack": [], "role": RoleAnalysis(role_type="technical_lead", hands_on="medium", quote=quote)}
            if "competencies" in schema.model_json_schema().get("required", []):
                analysis["competencies"] = []  # rubric v8
        return schema(
            **analysis,
            skill_match=self.scores, role_fit=self.scores, domain_fit=self.scores, seniority_fit=60, comp_fit=50,
            logistics_fit=75,
            red_flags=[], gaps=["no comp posted"],
            evidence=[
                Evidence(claim="API ownership", quote=quote, dimension="skill_match"),
                Evidence(claim="insurance", quote="insurance business", dimension="domain_fit"),
                Evidence(claim="made up", quote="this text is not in the posting", dimension="logistics_fit"),
            ],
            rationale="Strong integration fit.",
        )


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


def load_fixture_json(*parts: str):  # noqa: ANN201
    with open(FIXTURES.joinpath(*parts), encoding="utf-8") as fh:
        return json.load(fh)


def load_fixture_text(*parts: str) -> str:
    with open(FIXTURES.joinpath(*parts), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def hashing_embedder() -> HashingEmbedder:
    return HashingEmbedder()
