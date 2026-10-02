"""Checking a posting is still up before tailoring. Offline: every board answers from a canned transport."""

from __future__ import annotations

import httpx
import pytest

from jobhunt.sources import liveness

WD = "https://pru.wd5.myworkdayjobs.com/en-US/Careers/job/Newark-NJ/Director--Tech-Lead_R123"


def client(handler) -> httpx.Client:  # noqa: ANN001
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_workday_links_are_checked_through_the_public_job_api():
    assert liveness._workday_api(WD) == \
        "https://pru.wd5.myworkdayjobs.com/wday/cxs/pru/Careers/job/Newark-NJ/Director--Tech-Lead_R123"
    assert liveness._workday_api("https://pru.wd5.myworkdayjobs.com/Careers/job/X_R1").endswith("/cxs/pru/Careers/job/X_R1")
    assert liveness._workday_api("https://example.com/Careers/job/X") is None
    # the other address form Workday uses
    assert liveness._workday_api("https://wd5.myworkdaysite.com/recruiting/guidewire/external/job/Remote-USA/"
                                 "Principal_JR_15006") ==         "https://wd5.myworkdaysite.com/wday/cxs/guidewire/external/job/Remote-USA/Principal_JR_15006"


@pytest.mark.parametrize(("status", "body", "expected"), [
    (404, {}, "closed"),
    (200, {"jobPostingInfo": {"title": "Director", "canApply": True}}, "open"),
    (200, {"jobPostingInfo": {"title": "Director", "canApply": False}}, "closed"),
    (200, {}, "unknown"),
    (503, {}, "unknown"),
])
def test_workday_answers(status, body, expected):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(status, json=body)

    assert liveness.check({"url": WD}, client(handler)).status == expected
    assert "/wday/cxs/pru/Careers/job/" in seen[0]


@pytest.mark.parametrize(("listed", "expected"), [(False, "closed"), (True, "unknown")])
def test_a_refused_workday_posting_is_closed_only_when_the_site_no_longer_lists_it(listed, expected):
    """Seen live: Arch Group answers 403 "permission" (S22) for a posting it has taken down."""
    path = "/job/Newark-NJ/Director--Tech-Lead_R123"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            postings = [{"externalPath": path}] if listed else [{"externalPath": "/job/London/Other_R9"}]
            return httpx.Response(200, json={"total": len(postings), "jobPostings": postings})
        return httpx.Response(403, json={"errorCode": "S22", "message": "permission denied"})

    assert liveness.check({"url": WD, "title": "Director, Tech Lead"}, client(handler)).status == expected


def test_greenhouse_lever_and_ashby_use_their_posting_apis():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "boards-api.greenhouse.io/v1/boards/zocdoc/jobs/8155444" in url:
            return httpx.Response(404)
        if "api.lever.co/v0/postings/acme/abc-123" in url:
            return httpx.Response(200, json={"id": "abc-123"})
        if "api.ashbyhq.com/posting-api/job-board/beta" in url:
            return httpx.Response(200, json={"jobs": [{"id": "live-1"}]})
        raise AssertionError(url)

    c = client(handler)
    assert liveness.check({"url": "https://job-boards.greenhouse.io/zocdoc/jobs/8155444"}, c).closed
    assert liveness.check({"url": "https://jobs.lever.co/acme/abc-123"}, c).status == "open"
    assert liveness.check({"url": "https://jobs.ashbyhq.com/beta/live-1"}, c).status == "open"
    assert liveness.check({"url": "https://jobs.ashbyhq.com/beta/gone-2"}, c).closed


@pytest.mark.parametrize(("status", "html", "expected"), [
    (404, "", "closed"),
    (200, "<h1>Sorry, the page you are looking for does not exist.</h1>", "closed"),
    (200, "<p>This position has been filled.</p>", "closed"),
    (200, "<h1>Integration Lead</h1><p>Apply now</p>", "open"),
    (500, "", "unknown"),
])
def test_other_boards_are_read_from_the_posting_page(status, html, expected):
    c = client(lambda request: httpx.Response(status, text=html))
    assert liveness.check({"url": "https://careers.example.com/job/123"}, c).status == expected


def test_a_network_failure_never_marks_a_job_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    result = liveness.check({"url": WD}, client(handler))
    assert result.status == "unknown" and not result.closed
