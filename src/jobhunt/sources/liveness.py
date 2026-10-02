"""Is a posting still open? Checked before tailoring, so no time goes into a resume for a job that's gone.

Where a board has a public API for one posting, that answers it: Workday's CXS job endpoint (the adapter's own
detail call), Greenhouse's and Lever's posting APIs, and Ashby's job board. Anything else gets one GET of the
posting page, read for a 404/410 or a "no longer available" message. A network error or an unclear answer is
"unknown", never "closed": a job is only ever marked closed on a clear sign.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from jobhunt.sources.base import default_client

_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
# what a board shows in place of a posting that has been taken down
GONE_PHRASES = re.compile(
    r"page you are looking for (does not|doesn't|no longer) exist|job (posting )?(is )?no longer available"
    r"|(position|job|posting|requisition) (has been|is) (filled|closed|removed)|no longer accepting applications"
    r"|this job (has )?expired|job not found|posting (has )?expired",
    re.IGNORECASE)


@dataclass
class Liveness:
    status: str  # open | closed | unknown
    detail: str

    @property
    def closed(self) -> bool:
        return self.status == "closed"


def _workday_api(url: str) -> str | None:
    """https://pru.wd5.myworkdayjobs.com/en-US/Careers/job/Newark-NJ/Lead_R123 ->
    https://pru.wd5.myworkdayjobs.com/wday/cxs/pru/Careers/job/Newark-NJ/Lead_R123"""
    parts = urlsplit(url)
    host = parts.hostname or ""
    segs = [s for s in parts.path.split("/") if s]
    if segs and _LOCALE.match(segs[0]):
        segs = segs[1:]
    if host.endswith("myworkdaysite.com") and segs[:1] == ["recruiting"]:
        # the other address form: wd5.myworkdaysite.com/recruiting/{tenant}/{site}/job/...
        if len(segs) < 4 or segs[3] != "job":
            return None
        return f"https://{host}/wday/cxs/{segs[1]}/{segs[2]}/{'/'.join(segs[3:])}"
    if "myworkdayjobs.com" not in host or "job" not in segs or segs.index("job") != 1:
        return None
    return f"https://{host}/wday/cxs/{host.split('.')[0]}/{segs[0]}/{'/'.join(segs[1:])}"


def _workday_listed(client: httpx.Client, api: str, title: str) -> Liveness:
    """Search the site's public list for the posting (by title); its path must be among the results."""
    base, sep, path = api.partition("/job/")
    if not sep or not title:
        return Liveness("unknown", "Workday refused the posting")
    r = client.post(f"{base}/jobs", json={"searchText": title, "limit": 20, "offset": 0, "appliedFacets": {}},
                    headers={"Accept": "application/json"})
    if r.status_code != 200:
        return Liveness("unknown", f"Workday refused the posting and its search answered HTTP {r.status_code}")
    paths = {str(p.get("externalPath") or "") for p in (r.json() or {}).get("jobPostings") or []}
    if f"/job/{path}" in paths:
        return Liveness("unknown", "Workday lists the posting but refused its details")
    return Liveness("closed", "Workday no longer shows this posting publicly")


def _board_api(url: str) -> tuple[str, str] | None:
    """(kind, api url) for Greenhouse, Lever and Ashby postings."""
    parts = urlsplit(url)
    host, segs = parts.hostname or "", [s for s in parts.path.split("/") if s]
    if host.endswith("greenhouse.io") and len(segs) >= 3 and segs[1] == "jobs":
        return "greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{segs[0]}/jobs/{segs[2]}"
    if host == "jobs.lever.co" and len(segs) >= 2:
        return "lever", f"https://api.lever.co/v0/postings/{segs[0]}/{segs[1]}"
    if host == "jobs.ashbyhq.com" and len(segs) >= 2:
        return "ashby:" + segs[1], f"https://api.ashbyhq.com/posting-api/job-board/{segs[0]}"
    return None


def check(job: dict, client: httpx.Client | None = None) -> Liveness:
    url = str(job.get("url") or job.get("apply_url") or "")
    if not url.startswith("http"):
        return Liveness("unknown", "no posting link to check")
    own = client is None
    client = client or default_client(timeout=15.0)
    try:
        if api := _workday_api(url):
            r = client.get(api, headers={"Accept": "application/json"})
            if r.status_code in (404, 410):
                return Liveness("closed", "Workday no longer lists this posting")
            if r.status_code == 200:
                info = (r.json() or {}).get("jobPostingInfo") or {}
                if info.get("canApply") is False:
                    return Liveness("closed", "Workday shows the posting but no longer accepts applications")
                return Liveness("open", "Workday lists the posting") if info else Liveness(
                    "unknown", "Workday answered without posting details")
            if r.status_code == 403:
                # Workday refuses a posting it no longer shows publicly ("permission", S22), but a refusal alone
                # could be something else: it counts as closed only if the site's own list doesn't have it
                return _workday_listed(client, api, str(job.get("title") or ""))
            return Liveness("unknown", f"Workday answered HTTP {r.status_code}")
        if board := _board_api(url):
            kind, api = board
            r = client.get(api)
            if kind.startswith("ashby:"):
                if r.status_code != 200:
                    return Liveness("unknown", f"Ashby answered HTTP {r.status_code}")
                ids = {str(j.get("id")) for j in (r.json() or {}).get("jobs") or []}
                return (Liveness("open", "Ashby lists the posting") if kind.split(":", 1)[1] in ids
                        else Liveness("closed", "Ashby no longer lists this posting"))
            if r.status_code in (404, 410):
                return Liveness("closed", f"{kind.title()} no longer lists this posting")
            if r.status_code == 200:
                return Liveness("open", f"{kind.title()} lists the posting")
            return Liveness("unknown", f"{kind.title()} answered HTTP {r.status_code}")
        r = client.get(url)
        if r.status_code in (404, 410):
            return Liveness("closed", f"the posting page answered HTTP {r.status_code}")
        if r.status_code == 200:
            if m := GONE_PHRASES.search(r.text[:200_000]):
                return Liveness("closed", f"the posting page says “{m.group(0)}”")
            return Liveness("open", "the posting page loads")
        return Liveness("unknown", f"the posting page answered HTTP {r.status_code}")
    except (httpx.HTTPError, ValueError) as exc:
        return Liveness("unknown", f"couldn't check the link ({type(exc).__name__})")
    finally:
        if own:
            client.close()
