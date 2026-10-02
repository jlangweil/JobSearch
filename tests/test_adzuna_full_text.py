"""Adzuna's API gives 500-character snippets; the full posting is read from the job's Adzuna details page before
scoring. Offline: the two pages are captured fixtures (a live posting and an expired one)."""

from __future__ import annotations

import httpx

from jobhunt.db import queries
from jobhunt.discovery import service
from jobhunt.sources import adzuna
from jobhunt.sources.normalize import normalize_raw
from jobhunt.sources.registry import is_truncated
from tests.conftest import load_fixture_text, raw_job

LIVE = load_fixture_text("adzuna", "details_live.html")
EXPIRED = load_fixture_text("adzuna", "details_expired.html")
LAND = "https://www.adzuna.com/land/ad/5882914725?se=x&utm_medium=api&utm_source=8bed4e85&v=ABC"


def _snippet_job(i: int = 1, ad: str = "5882914725") -> dict:
    rec = normalize_raw(raw_job(i, title="Senior Lead Software Engineer", company="JPMorgan Chase", source="adzuna",
                                description="This is your chance to shape an AI-first Developer Portal…"))
    rec.update(source_job_id=ad, url=LAND, apply_url=LAND, description_truncated=True)  # as the adapter flags it
    return rec


def test_the_details_page_is_the_robots_allowed_one():
    """robots.txt disallows /land/ad/ (the API's own links); /details/ is allowed."""
    assert adzuna.details_url({"source_job_id": "5882914725", "url": LAND}) == \
        "https://www.adzuna.com/details/5882914725"
    assert adzuna.details_url({"source_job_id": "5882914725", "url": "https://www.adzuna.co.uk/land/ad/1"}) == \
        "https://www.adzuna.co.uk/details/5882914725"
    assert adzuna.details_url({"source_job_id": "not-an-id", "url": LAND}) is None


def test_the_full_description_is_read_from_a_live_page():
    result = adzuna.parse_details(LIVE)
    assert not result.gone and result.text
    assert result.text.startswith("This is your chance to shape an AI-first Developer Portal")
    assert len(result.text) > 3000 and "$175,750.00 - $260,000.00" in result.text   # the pay the snippet cut off
    assert "<" not in result.text


def test_an_expired_page_says_the_job_is_gone():
    result = adzuna.parse_details(EXPIRED)
    assert result.gone and result.text is None


def test_before_scoring_snippets_become_full_postings_and_gone_jobs_close(cfg, engine):
    _, live_id = queries.upsert_job(engine, _snippet_job(1, "5882914725"))
    _, gone_id = queries.upsert_job(engine, _snippet_job(2, "5879561629"))
    _, fail_id = queries.upsert_job(engine, _snippet_job(3, "1111111111"))
    other = normalize_raw(raw_job(4, source="greenhouse"))
    _, other_id = queries.upsert_job(engine, other)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/land/ad/" not in str(request.url)                       # never the disallowed links
        if request.url.path.endswith("/5882914725"):
            return httpx.Response(200, text=LIVE)
        if request.url.path.endswith("/5879561629"):
            return httpx.Response(404, text=EXPIRED)                     # an expired page answers 404
        return httpx.Response(503)

    jobs = queries.get_jobs(engine, [live_id, gone_id, fail_id, other_id], status="discovered")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        kept, errors = service.complete_descriptions(engine, cfg, jobs, client=client)

    assert {j["id"] for j in kept} == {live_id, fail_id, other_id}
    live = queries.get_job(engine, live_id)
    assert live["full_description"] == 1 and len(live["description_text"]) > 3000 and not is_truncated(live)
    assert queries.get_job(engine, gone_id)["status"] == "closed"
    assert is_truncated(queries.get_job(engine, fail_id)) and len(errors) == 1      # left as a snippet
    assert queries.get_job(engine, other_id)["full_description"] == 0              # other boards untouched


def test_the_next_run_does_not_put_the_snippet_back(cfg, engine):
    rec = _snippet_job()
    _, job_id = queries.upsert_job(engine, rec)
    queries.set_full_description(engine, job_id, "the whole posting " * 50)
    assert queries.upsert_job(engine, rec)[0] == "seen"                            # same snippet: kept
    edited = {**rec, "description_text": "an edited snippet", "content_hash": "different"}
    assert queries.upsert_job(engine, edited)[0] == "seen"                         # a new snippet doesn't win
    assert queries.get_job(engine, job_id)["description_text"].startswith("the whole posting")


def test_the_switch_turns_it_off(cfg, engine):
    cfg.sources["adzuna"].model_extra["fetch_full_description"] = False
    _, job_id = queries.upsert_job(engine, _snippet_job())
    jobs = queries.get_jobs(engine, [job_id], status="discovered")
    kept, errors = service.complete_descriptions(engine, cfg, jobs, client=httpx.Client(
        transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError("no fetch")))))
    assert kept == jobs and errors == []
