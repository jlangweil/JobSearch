"""Ashby public posting API."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime

import httpx

from jobhunt.db.engine import parse_iso
from jobhunt.sources.base import RateLimiter, RawJob, default_client, error_record

NAME = "ashby"
URL = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"


class AshbySource:
    name = NAME

    def __init__(self, boards: Sequence[tuple[str, str]], *, client: httpx.Client | None = None,
                 rate_limit_per_sec: float = 1.0) -> None:
        self.boards = list(boards)
        self.client = client or default_client()
        self.limiter = RateLimiter(rate_limit_per_sec)
        self.errors: list[dict] = []

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        for company, token in self.boards:
            try:
                self.limiter.wait()
                r = self.client.get(URL.format(token=token))
                r.raise_for_status()
                payload = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                self.errors.append(error_record(NAME, f"{token}: {exc}", company=company, exc=exc))
                continue
            for job in payload.get("jobs", []):
                if job.get("isListed") is False:
                    continue
                yield self.map_job(company, job)

    @staticmethod
    def map_job(company: str, job: dict) -> RawJob:
        comp_min = comp_max = None
        interval = None
        for comp in ((job.get("compensation") or {}).get("summaryComponents") or []):
            if comp.get("compensationType") == "Salary" and (comp.get("currencyCode") or "USD") == "USD":
                comp_min, comp_max = comp.get("minValue"), comp.get("maxValue")
                interval = "hourly" if "HOUR" in (comp.get("interval") or "").upper() else None
                break
        locations = [job.get("location")] + [x.get("location") for x in job.get("secondaryLocations") or []]
        workplace = job.get("workplaceType") or ("Remote" if job.get("isRemote") else None)
        return RawJob(
            source=NAME,
            source_job_id=str(job["id"]),
            company_name=company,
            title=job.get("title") or "",
            location="; ".join(x for x in locations if x) or None,
            description_html=job.get("descriptionHtml") or job.get("descriptionPlain") or "",
            url=job.get("jobUrl") or "",
            apply_url=job.get("applyUrl") or job.get("jobUrl"),
            posted_at=parse_iso(job.get("publishedAt")),
            comp_min=int(comp_min) if comp_min else None,
            comp_max=int(comp_max) if comp_max else None,
            extra={
                "workplace_type": workplace,
                "employment_hint": job.get("employmentType"),
                "team": job.get("team") or job.get("department"),
                "comp_interval": interval,
            },
        )
