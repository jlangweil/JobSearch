"""Phenom and Eightfold adapters against fixtures recorded from careers.humana.com and careers.newyorklife.com
(2026-09-15). Nothing here touches the network."""

from __future__ import annotations

import json

import httpx
import pytest

from jobhunt.services.companies import CompanyError, validate_token
from jobhunt.sources import eightfold, phenom, workday
from jobhunt.sources.normalize import normalize_raw
from tests.conftest import load_fixture_json

SEARCH_PAGE = '<html><script>phApp.pageId = "page31"; phApp.ddo = {};</script></html>'


def test_phenom_tokens():
    assert phenom.parse_board_token("careers.marsh.com/global/en") == ("careers.marsh.com", "global/en")
    assert phenom.parse_board_token("https://careers.cencora.com/us/en/search-results") == ("careers.cencora.com",
                                                                                            "us/en")
    assert phenom.parse_board_token("careers.humana.com") == ("careers.humana.com", "us/en")
    board = phenom.PhenomBoard("Marsh", "careers.marsh.com", "global/en")
    assert board.lang == "en_global" and board.country == "global"
    assert validate_token("phenom", "https://careers.marsh.com/global/en/") == "careers.marsh.com/global/en"
    assert validate_token("eightfold", "careers.newyorklife.com") == "careers.newyorklife.com|newyorklife.com"
    with pytest.raises(CompanyError):
        validate_token("eightfold", "acme.eightfold.ai")


def test_phenom_search_facets_title_gate_and_detail():
    search = load_fixture_json("phenom", "humana_search.json")
    detail = load_fixture_json("phenom", "humana_job.json")
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "careers.humana.com"
        if request.method == "GET" and request.url.path == "/us/en/search-results":
            return httpx.Response(200, text=SEARCH_PAGE)
        assert request.method == "POST" and request.url.path == "/widgets"
        body = json.loads(request.content)
        bodies.append(body)
        assert body["pageId"] == "page31" and body["lang"] == "en_us"
        if body["ddoKey"] == "jobDetail":
            return httpx.Response(200, json=detail)
        return httpx.Response(200, json=search)

    src = phenom.PhenomSource([phenom.PhenomBoard("Humana", "careers.humana.com", "us/en")], queries=["integration"],
                              client=httpx.Client(transport=httpx.MockTransport(handler)), rate_limit_per_sec=0,
                              title_gate=lambda t: "Salesforce" not in t,
                              country_patterns=workday.DEFAULT_COUNTRY_PATTERNS)
    jobs = list(src.fetch(None))
    assert src.errors == [] and len(jobs) == 3
    searches = [b for b in bodies if b["ddoKey"] == "refineSearch" and b["size"] == phenom.PAGE_SIZE]
    assert searches[0]["selected_fields"] == {"country": ["United States of America"]}
    assert searches[0]["keywords"] == "integration"
    listing_only = [j for j in jobs if j.extra.get("listing_only")]
    assert [j.title for j in listing_only] == ["Lead Enterprise Salesforce Architect"]
    full = next(j for j in jobs if j.title == "M&A Integration Technology Lead")
    rec = normalize_raw(full)
    assert full.source_job_id == "HUMHUMUSR423292EXTERNALENUS"
    assert full.url == "https://careers.humana.com/us/en/job/HUMHUMUSR423292EXTERNALENUS"
    assert full.apply_url.startswith("https://humana.wd5.myworkdayjobs.com/")
    assert rec["remote_type"] == "remote" and rec["employment_type"] == "fte"
    assert "IT integration" in rec["description_text"] and full.posted_at is not None
    assert "Louisville, Kentucky" in full.location
    assert src.facet_notes["Humana (careers.humana.com)"] == ["country: United States of America"]


def test_eightfold_listing_paging_and_detail():
    search = load_fixture_json("eightfold", "nyl_search.json")
    detail = load_fixture_json("eightfold", "nyl_job.json")
    seen_starts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "careers.newyorklife.com"
        assert request.url.params["domain"] == "newyorklife.com"
        if request.url.path == "/api/apply/v2/jobs":
            seen_starts.append(request.url.params["start"])
            page = search if request.url.params["start"] == "0" else {"count": search["count"], "positions": []}
            return httpx.Response(200, json=page)
        if request.url.path == f"/api/apply/v2/jobs/{detail['id']}":
            return httpx.Response(200, json=detail)
        return httpx.Response(200, json={"id": int(request.url.path.rsplit("/", 1)[1]), "job_description": "<p>x</p>"})

    src = eightfold.EightfoldSource([eightfold.EightfoldBoard("New York Life", "careers.newyorklife.com",
                                                              "newyorklife.com")],
                                    queries=["integration"], client=httpx.Client(transport=httpx.MockTransport(handler)),
                                    rate_limit_per_sec=0, max_pages_per_query=3)
    jobs = list(src.fetch(None))
    assert src.errors == [] and len(jobs) == 3 and seen_starts == ["0", "10"]
    first = next(j for j in jobs if j.source_job_id == f"newyorklife.com/{detail['id']}")
    rec = normalize_raw(first)
    assert first.title == "Senior Associate - Integration Technical Lead"
    assert rec["remote_type"] == "hybrid" and first.location.startswith("New York, New York, United States")
    assert first.url.startswith("https://careers.newyorklife.com/careers/job/") and len(rec["description_text"]) > 1000
    assert first.posted_at is not None and first.extra["req_id"] == "94496"
