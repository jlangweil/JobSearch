"""Full postings for jobs stored without one, read from each board the way its adapter reads it. Offline: every board
answers from the adapters' captured fixtures."""

from __future__ import annotations

import httpx
import pytest

from jobhunt.db import queries
from jobhunt.discovery import service
from jobhunt.sources import fulltext
from jobhunt.sources.normalize import normalize_raw
from tests.conftest import load_fixture_json, raw_job

WD_PATH = "/job/Newark-NJ-USA/Lead--Infrastructure-Administration-Engineer_R-124588-1"
WD_URL = f"https://pru.wd5.myworkdayjobs.com/Careers{WD_PATH}"
WD_DETAIL = load_fixture_json("workday", "pru_details.json")[WD_PATH]
GREENHOUSE = load_fixture_json("greenhouse", "betterment.json")["jobs"][0]
LEVER = load_fixture_json("lever", "veeva.json")[0]
ASHBY = load_fixture_json("ashby", "ramp.json")


def boards(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith(f"/wday/cxs/pru/Careers{WD_PATH}"):
        return httpx.Response(200, json=WD_DETAIL)
    if "/wday/cxs/pru/Careers/job/" in url:
        return httpx.Response(404)
    if url == f"https://boards-api.greenhouse.io/v1/boards/betterment/jobs/{GREENHOUSE['id']}":
        return httpx.Response(200, json=GREENHOUSE)
    if url == f"https://api.lever.co/v0/postings/veeva/{LEVER['id']}":
        return httpx.Response(200, json=LEVER)
    if url.startswith("https://api.ashbyhq.com/posting-api/job-board/ramp"):
        return httpx.Response(200, json=ASHBY)
    return httpx.Response(404)


@pytest.fixture
def client():  # noqa: ANN201
    with httpx.Client(transport=httpx.MockTransport(boards)) as c:
        yield c


def test_each_board_gives_the_full_posting(client):
    wd = fulltext.fetch({"source": "workday", "url": WD_URL, "title": "Lead"}, client)
    assert wd.text and len(wd.text) > 2000 and "<" not in wd.text
    # Greenhouse double-escapes its HTML; the company's own careers link is resolved through its board token
    gh = fulltext.fetch({"source": "greenhouse", "url": GREENHOUSE["absolute_url"], "board_token": "betterment",
                         "source_job_id": str(GREENHOUSE["id"])}, client)
    assert gh.text and "About Betterment" in gh.text and "&lt;" not in gh.text and "<h2>" not in gh.text
    lv = fulltext.fetch({"source": "lever", "url": LEVER["hostedUrl"]}, client)
    assert lv.text and LEVER["descriptionPlain"].strip()[:40] in lv.text
    ab = fulltext.fetch({"source": "ashby", "url": ASHBY["jobs"][0]["jobUrl"]}, client)
    assert ab.text and len(ab.text) > 1000


def test_a_posting_the_board_no_longer_has_is_gone(client):
    assert fulltext.fetch({"source": "workday", "url": "https://pru.wd5.myworkdayjobs.com/Careers/job/X/Gone_R1"},
                          client).gone
    assert fulltext.fetch({"source": "greenhouse", "url": "https://job-boards.greenhouse.io/betterment/jobs/1"},
                          client).gone
    assert fulltext.fetch({"source": "ashby", "url": "https://jobs.ashbyhq.com/ramp/not-listed"}, client).gone


def test_reopened_jobs_without_text_are_filled_in_before_scoring(cfg, engine, client):
    """Seen live: 526 jobs re-opened after a filter change had no text (dropped while they were filtered out), so
    every one was reported as needing the posting pasted."""
    rec = normalize_raw(raw_job(1, title="Lead Infrastructure Engineer", company="Prudential"))
    rec.update(source="workday", source_job_id="pru/R-124588-1", url=WD_URL, apply_url=WD_URL, description_text="")
    _, filled = queries.upsert_job(engine, rec)
    gone = {**rec, "source_job_id": "pru/R-1", "url": "https://pru.wd5.myworkdayjobs.com/Careers/job/X/Gone_R1"}
    _, closed = queries.upsert_job(engine, gone)
    jobs = queries.get_jobs(engine, [filled, closed], status="discovered")
    assert not any(service.has_posting_text(j, cfg) for j in jobs)

    kept, errors = service.complete_descriptions(engine, cfg, jobs, client=client)
    assert [j["id"] for j in kept] == [filled] and errors == []
    job = queries.get_job(engine, filled)
    assert service.has_posting_text(job, cfg) and len(job["description_text"]) > 2000
    assert queries.get_job(engine, closed)["status"] == "closed"


def test_successfactors_eightfold_and_phenom_postings_are_read_like_their_adapters_read_them():
    """The last 18 jobs waiting for a posting came from these boards."""
    from tests.conftest import load_fixture_text

    sf_page = load_fixture_text("successfactors", "lincoln_detail.html")
    nyl = load_fixture_json("eightfold", "nyl_job.json")
    humana = load_fixture_json("phenom", "humana_job.json")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/job/Radnor-PA-Lead/123/" in url:
            return httpx.Response(200, text=sf_page)
        if url.startswith("https://careers.newyorklife.com/api/apply/v2/jobs/44064231"):
            assert request.url.params["domain"] == "newyorklife.com"
            return httpx.Response(200, json=nyl)
        if url.endswith("/us/en/search-results"):
            return httpx.Response(200, text='<script>phApp.pageId = "page21";</script>')
        if url.endswith("/widgets"):
            return httpx.Response(200, json=humana)
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        sf = fulltext.fetch({"source": "successfactors", "url": "https://jobs.lfg.com/job/Radnor-PA-Lead/123/"}, client)
        ef = fulltext.fetch({"source": "eightfold", "source_job_id": "newyorklife.com/44064231",
                             "url": "https://careers.newyorklife.com/careers/job/44064231"}, client)
        ph = fulltext.fetch({"source": "phenom", "source_job_id": "HUM123", "board_token": "careers.humana.com/us/en",
                             "url": "https://careers.humana.com/us/en/job/HUM123"}, client)
    for result in (sf, ef, ph):
        assert result.text and len(result.text) > 300 and "<" not in result.text and not result.gone


def test_a_phenom_posting_whose_page_is_gone_is_closed():
    """Seen live: Cencora's widget returns an empty job and its page answers 410 Gone for taken-down postings."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/search-results"):
            return httpx.Response(200, text="<html></html>")
        if url.endswith("/widgets"):
            return httpx.Response(200, json={"jobDetail": {"data": {"job": {}}}})
        return httpx.Response(410)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fulltext.fetch({"source": "phenom", "source_job_id": "X1", "board_token": "careers.cencora.com/us/en",
                                 "url": "https://careers.cencora.com/us/en/job/X1"}, client)
    assert result.gone

