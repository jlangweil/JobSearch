"""Greenhouse Job Board API (public, unauthenticated)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime

import httpx

from jobhunt.db.engine import parse_iso
from jobhunt.sources.base import RateLimiter, RawJob, default_client, error_record

NAME = "greenhouse"
URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


class GreenhouseSource:
    name = NAME

    def __init__(self, boards: Sequence[tuple[str, str]], *, client: httpx.Client | None = None,
                 rate_limit_per_sec: float = 1.0) -> None:
        self.boards = list(boards)  # (company_name, board_token)
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
                yield self.map_job(company, job)

    @staticmethod
    def map_job(company: str, job: dict) -> RawJob:
        departments = [d.get("name") for d in job.get("departments") or [] if d.get("name")]
        posted = job.get("first_published") or job.get("updated_at")
        return RawJob(
            source=NAME,
            source_job_id=str(job["id"]),
            company_name=company,
            title=job.get("title") or "",
            location=(job.get("location") or {}).get("name"),
            description_html=job.get("content") or "",
            url=job.get("absolute_url") or "",
            apply_url=job.get("absolute_url"),
            posted_at=parse_iso(posted),
            comp_min=None,
            comp_max=None,
            extra={"team": ", ".join(departments) or None},
        )
