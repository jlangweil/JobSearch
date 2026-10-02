"""Offline adapter tests against recorded fixtures. Nothing here touches the network."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from jobhunt.sources import adzuna, ashby, greenhouse, lever, successfactors, workday
from jobhunt.sources.normalize import normalize_raw
from tests.conftest import load_fixture_json, load_fixture_text


def client(handler) -> httpx.Client:  # noqa: ANN001
    def guarded(request: httpx.Request) -> httpx.Response:
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(guarded))


# --------------------------------------------------------------------------------------------- workday


def workday_handler(request: httpx.Request) -> httpx.Response:
    listing = load_fixture_json("workday", "pru_list.json")
    details = load_fixture_json("workday", "pru_details.json")
    path = request.url.path
    assert request.url.host == "pru.wd5.myworkdayjobs.com"
    if request.method == "POST" and path == "/wday/cxs/pru/Careers/jobs":
        body = json.loads(request.content)
        assert body["limit"] <= 20
        return httpx.Response(200, json=listing if body["offset"] == 0 else {"total": 3, "jobPostings": []})
    prefix = "/wday/cxs/pru/Careers"
    if request.method == "GET" and path.startswith(prefix):
        ext = path[len(prefix):]
        if ext in details:
            return httpx.Response(200, json=details[ext])
    return httpx.Response(404)


def test_workday_board_token_parsing():
    assert workday.parse_board_token("https://pru.wd5.myworkdayjobs.com/en-US/Careers/jobs") == (
        "pru.wd5.myworkdayjobs.com", "pru", "Careers")
    assert workday.parse_board_token("cigna.wd5.myworkdayjobs.com/cignacareers") == (
        "cigna.wd5.myworkdayjobs.com", "cigna", "cignacareers")
    with pytest.raises(ValueError):
        workday.parse_board_token("https://example.com/Careers")


def test_workday_posted_on_parsing():
    now = datetime(2026, 9, 14, tzinfo=UTC)
    assert workday.parse_posted_on("Posted Today", now)[1] == 0
    assert workday.parse_posted_on("Posted Yesterday", now)[1] == 1
    assert workday.parse_posted_on("Posted 5 Days Ago", now)[1] == 5
    # "30+ Days Ago" is Workday's bucket for everything older than a month, so it is not an age at all: the
    # date comes back as a lower bound and the age as unknown, and nothing is dropped over it
    when, days = workday.parse_posted_on("Posted 30+ Days Ago", now)
    assert days is None
    assert when.date().isoformat() == "2026-08-15"


def test_workday_maps_fixture_jobs():
    board = workday.WorkdayBoard("Prudential", "pru.wd5.myworkdayjobs.com", "pru", "Careers")
    src = workday.WorkdaySource([board], queries=["architect"], client=client(workday_handler),
                                rate_limit_per_sec=0)
    jobs = list(src.fetch(datetime.now(UTC) - timedelta(days=3650)))
    assert src.errors == []
    assert len(jobs) == 3
    j = jobs[0]
    assert j.source == "workday" and j.source_job_id.startswith("pru/")
    assert j.company_name == "Prudential"
    assert j.url.startswith("https://pru.wd5.myworkdayjobs.com/")
    assert j.posted_at is not None
    rec = normalize_raw(j)
    assert len(rec["description_text"]) > 500
    assert "<p" not in rec["description_text"]
    assert rec["employment_type"] == "fte"
    assert "’" in rec["description_text"]  # curly apostrophe from the real posting survives


def test_workday_known_postings_become_sightings_without_detail_requests():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return workday_handler(request)

    board = workday.WorkdayBoard("Prudential", "pru.wd5.myworkdayjobs.com", "pru", "Careers")
    src = workday.WorkdaySource([board], client=client(handler), rate_limit_per_sec=0, known=lambda s, i: True)
    jobs = list(src.fetch(None))
    assert all(j.extra.get("sighting_only") for j in jobs)
    assert "GET" not in calls
    assert src.stats["sightings"] == 3


def test_workday_board_failure_is_recorded_not_raised():
    board = workday.WorkdayBoard("Broken", "nope.wd1.myworkdayjobs.com", "nope", "X")
    src = workday.WorkdaySource([board], client=client(lambda r: httpx.Response(422)), rate_limit_per_sec=0)
    assert list(src.fetch(None)) == []
    assert len(src.errors) == 1 and "nope" in src.errors[0]["message"]


# --------------------------------------------------------------------------------------------- successfactors


def test_successfactors_listing_table_and_tile_layouts():
    rows, total = successfactors.parse_listing(load_fixture_text("successfactors", "lincoln_search.html"))
    assert len(rows) == 9 and total == 9
    assert all(r["href"].startswith("/job/") and r["id"].isdigit() and r["title"] for r in rows)
    tiles, _ = successfactors.parse_listing(load_fixture_text("successfactors", "boehringer_tiles_search.html"))
    assert len(tiles) >= 1


def test_successfactors_detail_parsing():
    d = successfactors.parse_detail(load_fixture_text("successfactors", "lincoln_detail.html"))
    assert d["posted_at"] is not None
    assert len(d["description_html"]) > 500
    assert d["location"]


def test_successfactors_source_end_to_end_offline():
    search = load_fixture_text("successfactors", "lincoln_search.html")
    detail = load_fixture_text("successfactors", "lincoln_detail.html")
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        assert not request.url.path.startswith("/services/")  # disallowed by robots.txt
        if request.url.path == "/search/":
            start = int(parse_qs(urlparse(str(request.url)).query).get("startrow", ["0"])[0])
            return httpx.Response(200, text=search if start == 0 else "<html></html>")
        return httpx.Response(200, text=detail)

    board = successfactors.SuccessFactorsBoard("Lincoln Financial", "jobs.lincolnfinancial.com")
    src = successfactors.SuccessFactorsSource([board], queries=["architect"], client=client(handler),
                                              rate_limit_per_sec=0)
    jobs = list(src.fetch(None))
    assert src.errors == []
    assert len(jobs) == 9
    rec = normalize_raw(jobs[0])
    assert rec["source_job_id"].startswith("jobs.lincolnfinancial.com/")
    assert rec["description_text"]


# --------------------------------------------------------------------------------------------- json boards


def test_greenhouse_mapping():
    payload = load_fixture_json("greenhouse", "betterment.json")
    src = greenhouse.GreenhouseSource([("Betterment", "betterment")],
                                      client=client(lambda r: httpx.Response(200, json=payload)),
                                      rate_limit_per_sec=0)
    jobs = list(src.fetch(None))
    assert len(jobs) == 3 and jobs[0].source == "greenhouse"
    rec = normalize_raw(jobs[0])
    assert rec["description_text"] and "&lt;" not in rec["description_text"]


def test_lever_mapping():
    payload = load_fixture_json("lever", "veeva.json")
    src = lever.LeverSource([("Veeva", "veeva")], client=client(lambda r: httpx.Response(200, json=payload)),
                            rate_limit_per_sec=0)
    jobs = list(src.fetch(None))
    assert len(jobs) == 3
    assert jobs[0].url.startswith("https://jobs.lever.co/")
    assert jobs[0].posted_at is not None


def test_ashby_mapping():
    payload = load_fixture_json("ashby", "ramp.json")
    src = ashby.AshbySource([("Ramp", "ramp")], client=client(lambda r: httpx.Response(200, json=payload)),
                            rate_limit_per_sec=0)
    jobs = list(src.fetch(None))
    assert 1 <= len(jobs) <= 3
    assert all(j.source == "ashby" and j.url for j in jobs)


def test_adzuna_mapping_marks_truncated():
    payload = load_fixture_json("adzuna", "search.json")
    src = adzuna.AdzunaSource(app_id="x", app_key="y", queries=["integration architect"], location="NJ", pages=1,
                              client=client(lambda r: httpx.Response(200, json=payload)), rate_limit_per_sec=0)
    jobs = list(src.fetch(None))
    assert len(jobs) == 2
    rec = normalize_raw(jobs[0])
    assert rec["description_truncated"] is True
    assert rec["comp_min"] == 170000 and rec["employment_type"] == "fte"
    assert "–" in rec["title"]


def test_http_failure_in_json_board_is_recorded():
    src = greenhouse.GreenhouseSource([("Gone", "gone")], client=client(lambda r: httpx.Response(404)),
                                      rate_limit_per_sec=0)
    assert list(src.fetch(None)) == []
    assert src.errors and src.errors[0]["source"] == "greenhouse"


def test_title_gate_skips_detail_requests_for_irrelevant_titles():
    gets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            gets.append(request.url.path)
        return workday_handler(request)

    board = workday.WorkdayBoard("Prudential", "pru.wd5.myworkdayjobs.com", "pru", "Careers")
    src = workday.WorkdaySource([board], client=client(handler), rate_limit_per_sec=0, title_gate=lambda t: False)
    jobs = list(src.fetch(None))
    assert len(jobs) == 3 and all(j.extra.get("listing_only") and j.description_html == "" for j in jobs)
    assert gets == [] and src.stats["listing_only"] == 3


# --------------------------------------------------------------------------------------------- workday facets


@pytest.mark.parametrize(("tenant", "expect_category", "expect_country"), [
    ("pru", ["Technology", "Data Science & Analytics"], True),
    ("cigna", ["Information Technology", "Data & Analytics"], True),
    ("aig", ["IT - Information Technology", "DT - Data"], True),
    ("humana", ["Technology - Product & Solution Delivery"], False),  # Humana has no country facet
    ("travelers", ["Software Engineering", "Data & Analytics", "Architecture"], True),
    ("jj", [], False),
])
def test_workday_facet_selection_on_real_tenants(tenant, expect_category, expect_country):
    facets = load_fixture_json("workday", "facets.json")[tenant]["facets"]
    applied, notes = workday.choose_facets(facets, workday.DEFAULT_CATEGORY_PATTERNS, workday.DEFAULT_COUNTRY_PATTERNS)
    text = " | ".join(notes)
    for name in expect_category:
        assert name in text, text
    for bad in ("Sales", "Claims", "Actuarial", "Clinical", "Underwriting"):
        assert f"{bad}," not in text.split("country")[0], text
    assert ("United States of America" in text) == expect_country, text
    assert all(isinstance(v, list) and v for v in applied.values())


def test_workday_facet_selection_never_filters_to_nothing():
    facets = load_fixture_json("workday", "facets.json")["pru"]["facets"]
    applied, notes = workday.choose_facets(facets, ["no-such-category"], ["Atlantis"])
    assert applied == {}
    assert sum("not filtering" in n for n in notes) == 2


def test_workday_applies_facets_to_every_search():
    facets = load_fixture_json("workday", "facets.json")["pru"]["facets"]
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content)
            bodies.append(body)
            if body["limit"] == 1:
                return httpx.Response(200, json={"total": 182, "facets": facets, "jobPostings": []})
        return workday_handler(request)

    board = workday.WorkdayBoard("Prudential", "pru.wd5.myworkdayjobs.com", "pru", "Careers")
    src = workday.WorkdaySource([board], queries=["architect", "api"], client=client(handler), rate_limit_per_sec=0,
                                category_patterns=workday.DEFAULT_CATEGORY_PATTERNS,
                                country_patterns=workday.DEFAULT_COUNTRY_PATTERNS, title_gate=lambda t: False)
    list(src.fetch(None))
    searches = [b for b in bodies if b["limit"] == 20]
    assert len(searches) == 2
    for b in searches:
        assert set(b["appliedFacets"]) == {"CF-EEB-Job_classification_ID_Extended", "locationCountry"}
    assert any("category" in n for n in next(iter(src.facet_notes.values())))
    # a company-level override of [] turns category filtering off for that board only
    bodies.clear()
    board.category_patterns = []
    list(src.fetch(None))
    assert all(set(b["appliedFacets"]) == {"locationCountry"} for b in bodies if b["limit"] == 20)
