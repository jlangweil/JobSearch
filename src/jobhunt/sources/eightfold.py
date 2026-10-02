"""Eightfold adapter: career sites built on Eightfold AI (e.g. careers.newyorklife.com).

  list:   GET https://{host}/api/apply/v2/jobs?domain={domain}&query=&start=&num=10&sort_by=relevance
  detail: GET https://{host}/api/apply/v2/jobs/{id}?domain={domain}

The same JSON the public site loads. Eightfold robots.txt files list `Allow: /api/apply` (checked for New York Life
on 2026-09-15). The API returns at most 10 postings per page, so boards are query-driven with a page cap.
Board token: "{host}|{domain}", e.g. careers.newyorklife.com|newyorklife.com. When the domain is omitted it is the
host without a leading careers./jobs. label.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx

from jobhunt.sources.base import KnownJob, RateLimiter, RawJob, TitleGate, default_client, error_record

NAME = "eightfold"
PAGE_SIZE = 10
_WORKPLACE = {"remote": "remote", "remote_local": "remote", "hybrid": "hybrid", "onsite": "onsite",
              "on_site": "onsite", "in_office": "onsite"}


@dataclass
class EightfoldBoard:
    company_name: str
    host: str
    domain: str
    queries: list[str] = field(default_factory=list)

    @property
    def api(self) -> str:
        return f"https://{self.host}/api/apply/v2/jobs"


def parse_board_token(token: str) -> tuple[str, str]:
    raw, _, domain = token.strip().partition("|")
    t = raw.strip()
    if "://" not in t:
        t = "https://" + t
    host = (urlparse(t).hostname or "").lower()
    if not host or "." not in host:
        raise ValueError(f"eightfold board token {token!r} should look like careers.example.com|example.com")
    domain = domain.strip().lower()
    if not domain:
        if host.endswith(".eightfold.ai"):
            raise ValueError("for *.eightfold.ai sites give the company domain too: host|example.com")
        labels = host.split(".")
        domain = ".".join(labels[1:]) if labels[0] in ("careers", "jobs", "career", "apply") else host
    return host, domain


class EightfoldSource:
    name = NAME

    def __init__(self, boards: Sequence[EightfoldBoard], *, queries: Sequence[str] = (),
                 client: httpx.Client | None = None, rate_limit_per_sec: float = 1.0, max_pages_per_query: int = 5,
                 known: KnownJob | None = None, title_gate: TitleGate | None = None) -> None:
        self.boards = list(boards)
        self.queries = [q for q in queries if q is not None] or [""]
        self.client = client or default_client()
        self.limiter = RateLimiter(rate_limit_per_sec)
        self.max_pages = max_pages_per_query
        self.known = known
        self.title_gate = title_gate
        self.errors: list[dict] = []
        self.stats: dict[str, int] = {"listed": 0, "detail_fetched": 0, "skipped_old": 0, "sightings": 0,
                                      "listing_only": 0}

    def _get(self, url: str, params: dict) -> dict:
        self.limiter.wait()
        r = self.client.get(url, params=params, headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()

    def list_postings(self, board: EightfoldBoard, query: str) -> Iterator[dict]:
        start = 0
        for _ in range(self.max_pages):
            data = self._get(board.api, {"domain": board.domain, "query": query, "start": start, "num": PAGE_SIZE,
                                         "sort_by": "relevance"})
            positions = data.get("positions") or []
            yield from positions
            start += PAGE_SIZE
            if not positions or start >= int(data.get("count") or 0):
                break

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        for board in self.boards:
            try:
                yield from self._fetch_board(board, since)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                self.errors.append(error_record(NAME, f"{board.host}: {exc}", company=board.company_name, exc=exc))

    def _fetch_board(self, board: EightfoldBoard, since: datetime | None) -> Iterator[RawJob]:
        cutoff = since - timedelta(days=1) if since else None
        seen: set[str] = set()
        for query in board.queries or self.queries:
            for p in self.list_postings(board, query):
                if p.get("id") is None or str(p["id"]) in seen:
                    continue
                seen.add(str(p["id"]))
                self.stats["listed"] += 1
                posted = _epoch(p.get("t_create"))
                if cutoff and posted and posted < cutoff:
                    self.stats["skipped_old"] += 1
                    continue
                source_job_id = f"{board.domain}/{p['id']}"
                url = p.get("canonicalPositionUrl") or f"https://{board.host}/careers/job/{p['id']}"
                title = p.get("posting_name") or p.get("name") or ""
                location = _location(p)
                if self.known and self.known(NAME, source_job_id):
                    self.stats["sightings"] += 1
                    yield RawJob(NAME, source_job_id, board.company_name, title, location, "", url, url, posted,
                                 None, None, {"sighting_only": True})
                    continue
                if self.title_gate and not self.title_gate(title):
                    self.stats["listing_only"] += 1
                    yield RawJob(NAME, source_job_id, board.company_name, title, location, "", url, url, posted,
                                 None, None, {"listing_only": True})
                    continue
                try:
                    detail = self._get(f"{board.api}/{p['id']}", {"domain": board.domain})
                except (httpx.HTTPError, ValueError) as exc:
                    self.errors.append(error_record(NAME, f"detail {url}: {exc}", company=board.company_name, exc=exc))
                    continue
                self.stats["detail_fetched"] += 1
                yield self.map_job(board, p, detail)

    @staticmethod
    def map_job(board: EightfoldBoard, posting: dict, detail: dict) -> RawJob:
        merged = {**posting, **{k: v for k, v in detail.items() if v not in (None, "", [])}}
        url = merged.get("canonicalPositionUrl") or f"https://{board.host}/careers/job/{merged['id']}"
        workplace = _WORKPLACE.get(str(merged.get("work_location_option") or "").lower())
        return RawJob(
            source=NAME,
            source_job_id=f"{board.domain}/{merged['id']}",
            company_name=board.company_name,
            title=merged.get("posting_name") or merged.get("name") or "",
            location=_location(merged) + (f"; {workplace.title()}" if workplace in ("remote", "hybrid") else ""),
            description_html=merged.get("job_description") or "",
            url=url,
            apply_url=url,
            posted_at=_epoch(merged.get("t_create")),
            comp_min=None,
            comp_max=None,
            extra={"workplace_type": workplace, "req_id": merged.get("display_job_id") or merged.get("ats_job_id"),
                   "team": merged.get("department")},
        )


def _epoch(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC) if value else None  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError):
        return None


def _location(p: dict) -> str:
    locs = [str(x).replace(",", ", ") for x in p.get("locations") or [] if x]
    return "; ".join(locs) if locs else str(p.get("location") or "").replace(",", ", ")
