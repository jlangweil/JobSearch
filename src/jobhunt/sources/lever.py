"""Lever Postings API (public)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime

import httpx

from jobhunt.db.engine import iso_from_epoch_ms, parse_iso
from jobhunt.sources.base import RateLimiter, RawJob, default_client, error_record

NAME = "lever"
URL = "https://api.lever.co/v0/postings/{token}?mode=json"


class LeverSource:
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
            for job in payload if isinstance(payload, list) else []:
                yield self.map_job(company, job)

    @staticmethod
    def map_job(company: str, job: dict) -> RawJob:
        cats = job.get("categories") or {}
        parts = [job.get("description") or ""]
        for block in job.get("lists") or []:
            parts.append(f"<h3>{block.get('text', '')}</h3><ul>{block.get('content', '')}</ul>")
        parts.append(job.get("additional") or "")
        salary = job.get("salaryRange") or {}
        interval = (salary.get("interval") or "").lower()
        return RawJob(
            source=NAME,
            source_job_id=str(job["id"]),
            company_name=company,
            title=job.get("text") or "",
            location=cats.get("location") or ", ".join(cats.get("allLocations") or []) or None,
            description_html="\n".join(parts),
            url=job.get("hostedUrl") or "",
            apply_url=job.get("applyUrl") or job.get("hostedUrl"),
            posted_at=parse_iso(iso_from_epoch_ms(job.get("createdAt"))),
            comp_min=salary.get("min") if (salary.get("currency") or "USD") == "USD" else None,
            comp_max=salary.get("max") if (salary.get("currency") or "USD") == "USD" else None,
            extra={
                "workplace_type": job.get("workplaceType"),
                "employment_hint": cats.get("commitment"),
                "team": cats.get("team") or cats.get("department"),
                "comp_interval": "hourly" if "hour" in interval else None,
            },
        )
