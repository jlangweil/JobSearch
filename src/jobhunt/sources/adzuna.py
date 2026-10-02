"""Adzuna aggregator (keyed API, free tier). The API's descriptions are 500-character snippets; before scoring,
the full text is read from the job's Adzuna details page (see full_description), and a job whose page says it is no
longer available is closed.

  details: GET https://www.adzuna.com/details/{id}   (robots.txt allows /details/; the API's /land/ad/ redirect
           links are disallowed and are never fetched)

  search: GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}
          ?app_id&app_key&what|title_only&where&distance(km)&max_days_old&category&salary_min&what_exclude
           &sort_by=date&results_per_page=50
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from jobhunt.db.engine import parse_iso
from jobhunt.sources.base import RateLimiter, RawJob, default_client, error_record
from jobhunt.sources.normalize import strip_html

NAME = "adzuna"
BASE = "https://api.adzuna.com/v1/api/jobs/{country}"
URL = BASE + "/search/{page}"
PAGE_SIZE = 50
KM_PER_MILE = 1.609344


class AdzunaAuthError(RuntimeError):
    pass


class AdzunaSource:
    name = NAME
    description_truncated = True

    def __init__(self, *, app_id: str, app_key: str, queries: Sequence[str], location: str | None,
                 max_days_old: int = 30, pages: int = 2, client: httpx.Client | None = None,
                 rate_limit_per_sec: float = 1.0, distance_miles: float | None = None, category: str | None = None,
                 salary_min: int | None = None, exclude_words: Sequence[str] = (), title_only: bool = False,
                 country: str = "us") -> None:
        self.app_id, self.app_key = app_id, app_key
        self.queries = [q for q in queries if q and q.strip()]
        self.location = location
        self.max_days_old = max_days_old
        self.pages = pages
        self.distance_miles = distance_miles
        self.category = category or None
        self.salary_min = salary_min or None
        self.exclude_words = [w for w in exclude_words if w and w.strip()]
        self.title_only = title_only
        self.country = country
        self.client = client or default_client()
        self.limiter = RateLimiter(rate_limit_per_sec)
        self.errors: list[dict] = []
        self.stats: dict[str, int] = {"listed": 0, "requests": 0}

    def params(self, query: str, per_page: int = PAGE_SIZE) -> dict:
        p: dict = {"app_id": self.app_id, "app_key": self.app_key, "results_per_page": per_page,
                   "max_days_old": self.max_days_old, "sort_by": "date"}
        p["title_only" if self.title_only else "what"] = query
        if self.location:
            p["where"] = self.location
            if self.distance_miles:
                p["distance"] = max(1, round(float(self.distance_miles) * KM_PER_MILE))
        if self.category:
            p["category"] = self.category
        if self.salary_min:
            p["salary_min"] = int(self.salary_min)
        if self.exclude_words:
            p["what_exclude"] = " ".join(self.exclude_words)
        return p

    def search(self, query: str, page: int = 1, per_page: int = PAGE_SIZE) -> dict:
        self.limiter.wait()
        self.stats["requests"] += 1
        r = self.client.get(URL.format(country=self.country, page=page), params=self.params(query, per_page),
                            headers={"Accept": "application/json"})
        if r.status_code in (401, 403):
            raise AdzunaAuthError("Adzuna rejected the App ID / key (check them at developer.adzuna.com)")
        r.raise_for_status()
        return r.json()

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        for query in self.queries:
            for page in range(1, self.pages + 1):
                try:
                    results = self.search(query, page).get("results", [])
                except AdzunaAuthError as exc:
                    self.errors.append(error_record(NAME, str(exc), exc=exc))
                    return
                except (httpx.HTTPError, ValueError) as exc:
                    self.errors.append(error_record(NAME, f"query {query!r} page {page}: {exc}", exc=exc))
                    break
                for job in results:
                    self.stats["listed"] += 1
                    yield self.map_job(job)
                if len(results) < PAGE_SIZE:
                    break

    @staticmethod
    def map_job(job: dict) -> RawJob:
        contract = " ".join(x for x in (job.get("contract_type"), job.get("contract_time")) if x)
        predicted = str(job.get("salary_is_predicted")) == "1"
        return RawJob(
            source=NAME,
            source_job_id=str(job["id"]),
            company_name=(job.get("company") or {}).get("display_name") or "Unknown",
            title=job.get("title") or "",
            location=(job.get("location") or {}).get("display_name"),
            description_html=job.get("description") or "",
            url=job.get("redirect_url") or "",
            apply_url=job.get("redirect_url"),
            posted_at=parse_iso(job.get("created")),
            comp_min=int(job["salary_min"]) if job.get("salary_min") and not predicted else None,
            comp_max=int(job["salary_max"]) if job.get("salary_max") and not predicted else None,
            extra={"description_truncated": True, "employment_hint": contract.replace("permanent", "full_time"),
                   "team": (job.get("category") or {}).get("label")},
        )


# --------------------------------------------------------------------------------------------- full descriptions

_DETAILS_BODY = re.compile(r'<section class="adp-body[^"]*"[^>]*>(?P<body>.*?)</section>', re.S)
_EXPIRED = re.compile(r"this job is no longer available", re.I)


@dataclass
class FullText:
    text: str | None  # the whole description, or None
    gone: bool        # the page says the job is no longer available
    detail: str


def details_url(job: dict) -> str | None:
    """The job's Adzuna details page, on the same Adzuna site its link points to."""
    ad = str(job.get("source_job_id") or "")
    if not ad.isdigit():
        return None
    host = urlsplit(str(job.get("url") or "")).hostname or ""
    if not host.endswith(("adzuna.com", "adzuna.co.uk", "adzuna.ca")):
        host = "www.adzuna.com"
    return f"https://{host}/details/{ad}"


def parse_details(html: str) -> FullText:
    if _EXPIRED.search(html):
        return FullText(None, True, "Adzuna says the job is no longer available")
    if m := _DETAILS_BODY.search(html):
        text = strip_html(m.group("body"))
        if text:
            return FullText(text, False, f"{len(text)} characters from the Adzuna details page")
    return FullText(None, False, "no description on the Adzuna details page")


def fetch_full_description(job: dict, client: httpx.Client) -> FullText:
    url = details_url(job)
    if url is None:
        return FullText(None, False, "no Adzuna id to look up")
    try:
        r = client.get(url)
    except httpx.HTTPError as exc:
        return FullText(None, False, f"couldn't reach Adzuna ({type(exc).__name__})")
    if r.status_code == 410:
        return FullText(None, True, "Adzuna says the job is gone (HTTP 410)")
    if r.status_code not in (200, 404):  # an expired job's page answers 404 but still says so
        return FullText(None, False, f"Adzuna answered HTTP {r.status_code}")
    return parse_details(r.text)
