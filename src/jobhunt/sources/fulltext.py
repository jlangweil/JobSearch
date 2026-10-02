"""Fetch a job's full posting from its board, for a job stored without one: an aggregator snippet (Adzuna), or a
company-board job whose text was dropped while it sat filtered out and was later re-opened.

Each board is read the way its adapter reads it: Workday's CXS job endpoint, Greenhouse's and Lever's posting APIs,
Ashby's job board (once per company, cached for the batch), Adzuna's details page. A posting the board no longer
has is reported `gone`; anything unclear leaves the job as it is.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from jobhunt.sources import adzuna, liveness
from jobhunt.sources.adzuna import FullText
from jobhunt.sources.normalize import strip_html

SUPPORTED = {"adzuna", "workday", "greenhouse", "lever", "ashby", "successfactors", "eightfold", "phenom"}


def _workday(job: dict, client: httpx.Client) -> FullText:
    api = liveness._workday_api(str(job.get("url") or ""))
    if api is None:
        return FullText(None, False, "not a Workday job link")
    r = client.get(api, headers={"Accept": "application/json"})
    if r.status_code in (404, 410):
        return FullText(None, True, "Workday no longer lists this posting")
    if r.status_code == 403:
        check = liveness._workday_listed(client, api, str(job.get("title") or ""))
        return FullText(None, check.closed, check.detail)
    if r.status_code != 200:
        return FullText(None, False, f"Workday answered HTTP {r.status_code}")
    info = (r.json() or {}).get("jobPostingInfo") or {}
    if info.get("canApply") is False:
        return FullText(None, True, "Workday no longer accepts applications for this posting")
    text = strip_html(info.get("jobDescription"))
    return FullText(text or None, False, "from Workday" if text else "Workday returned no description")


def _greenhouse_or_lever(job: dict, client: httpx.Client, kind: str, api: str) -> FullText:
    r = client.get(api)
    if r.status_code in (404, 410):
        return FullText(None, True, f"{kind.title()} no longer lists this posting")
    if r.status_code != 200:
        return FullText(None, False, f"{kind.title()} answered HTTP {r.status_code}")
    data = r.json() or {}
    if kind == "greenhouse":
        text = strip_html(data.get("content"))
    else:  # lever: an opening, titled lists (responsibilities, requirements), a closing
        parts = [data.get("descriptionPlain") or strip_html(data.get("description"))]
        for block in data.get("lists") or []:
            parts += [block.get("text") or "", strip_html(block.get("content"))]
        parts.append(data.get("additionalPlain") or strip_html(data.get("additional")))
        text = "\n\n".join(p.strip() for p in parts if p and p.strip())
    return FullText(text or None, False, f"from {kind.title()}" if text else f"{kind.title()} returned no description")


def _ashby(job: dict, client: httpx.Client, api: str, posting_id: str, cache: dict) -> FullText:
    if api not in cache:
        r = client.get(api)
        cache[api] = ({str(j.get("id")): j for j in (r.json() or {}).get("jobs") or []}
                      if r.status_code == 200 else None)
    board = cache[api]
    if board is None:
        return FullText(None, False, "couldn't read the Ashby job board")
    posting = board.get(posting_id)
    if posting is None:
        return FullText(None, True, "Ashby no longer lists this posting")
    text = posting.get("descriptionPlain") or strip_html(posting.get("descriptionHtml"))
    return FullText(text or None, False, "from Ashby" if text else "Ashby returned no description")


def _successfactors(job: dict, client: httpx.Client) -> FullText:
    """The posting's /job/ page (robots-allowed), parsed as the adapter parses it."""
    from jobhunt.sources.successfactors import parse_detail

    url = str(job.get("url") or "")
    if "/job/" not in url:
        return FullText(None, False, "not a SuccessFactors /job/ link")
    r = client.get(url)
    if r.status_code in (404, 410):
        return FullText(None, True, "SuccessFactors no longer has this posting")
    if r.status_code != 200:
        return FullText(None, False, f"SuccessFactors answered HTTP {r.status_code}")
    text = strip_html(parse_detail(r.text).get("description_html"))
    return FullText(text or None, False, "from SuccessFactors" if text else "no description on the job page")


def _eightfold(job: dict, client: httpx.Client) -> FullText:
    """The job API the adapter uses: /api/apply/v2/jobs/{id}?domain={domain}; the id is `{domain}/{id}`."""
    domain, _, pid = str(job.get("source_job_id") or "").rpartition("/")
    host = urlsplit(str(job.get("url") or "")).hostname
    if not (domain and pid and host):
        return FullText(None, False, "no Eightfold job id to look up")
    r = client.get(f"https://{host}/api/apply/v2/jobs/{pid}", params={"domain": domain},
                   headers={"Accept": "application/json"})
    if r.status_code in (404, 410):
        return FullText(None, True, "Eightfold no longer lists this posting")
    if r.status_code != 200:
        return FullText(None, False, f"Eightfold answered HTTP {r.status_code}")
    text = strip_html((r.json() or {}).get("job_description"))
    return FullText(text or None, False, "from Eightfold" if text else "Eightfold returned no description")


def _phenom(job: dict, client: httpx.Client, cache: dict) -> FullText:
    """The adapter's jobDetail widget, which needs the site's page id (read once per site, cached)."""
    from jobhunt.sources.phenom import PhenomBoard, PhenomSource, parse_board_token

    token = job.get("board_token") or urlsplit(str(job.get("url") or "")).hostname
    if not token:
        return FullText(None, False, "no Phenom site to read")
    host, locale = parse_board_token(str(token))
    board = PhenomBoard(company_name=str(job.get("company_name_raw") or ""), host=host, locale_path=locale)
    source = PhenomSource([board], client=client, rate_limit_per_sec=0)
    key = f"phenom-page:{host}"
    if key not in cache:
        cache[key] = source.page_id(board)
    posting = source.detail(board, cache[key], str(job.get("source_job_id") or ""))
    text = strip_html(posting.get("description"))
    if not posting:  # taken down, or a glitch: the job's own page says which
        page = client.get(str(job.get("url") or f"https://{host}"))
        if page.status_code in (404, 410):
            return FullText(None, True, f"Phenom no longer has this posting (its page answers {page.status_code})")
        return FullText(None, False, "Phenom returned no posting")
    return FullText(text or None, False, "from Phenom" if text else "Phenom returned no description")


def _board_from_token(job: dict) -> tuple[str, str] | None:
    """For a posting linked through the company's own careers site (e.g. betterment.com/careers/...?gh_jid=), the
    company's board token and the job id give the same API address."""
    token, jid = job.get("board_token"), str(job.get("source_job_id") or "")
    if not token or not jid:
        return None
    return {"greenhouse": ("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{jid}"),
            "lever": ("lever", f"https://api.lever.co/v0/postings/{token}/{jid}"),
            "ashby": (f"ashby:{jid}", f"https://api.ashbyhq.com/posting-api/job-board/{token}")}.get(
        str(job.get("source")))


def fetch(job: dict, client: httpx.Client, cache: dict | None = None) -> FullText:
    """The job's full posting from its board. `cache` holds per-batch lookups (an Ashby board is one request)."""
    cache = {} if cache is None else cache
    source = job.get("source")
    try:
        if source == "adzuna":
            return adzuna.fetch_full_description(job, client)
        if source == "workday":
            return _workday(job, client)
        if source == "successfactors":
            return _successfactors(job, client)
        if source == "eightfold":
            return _eightfold(job, client)
        if source == "phenom":
            return _phenom(job, client, cache)
        board = liveness._board_api(str(job.get("url") or "")) or _board_from_token(job)
        if board is None:
            return FullText(None, False, f"no {source} posting link to read")
        kind, api = board
        if kind.startswith("ashby:"):
            return _ashby(job, client, api, kind.split(":", 1)[1], cache)
        return _greenhouse_or_lever(job, client, kind, api)
    except (httpx.HTTPError, ValueError) as exc:
        return FullText(None, False, f"couldn't reach {urlsplit(str(job.get('url') or '')).hostname} "
                                     f"({type(exc).__name__})")
