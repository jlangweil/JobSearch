"""Source adapter interface (SPEC 5.1). Adapters are pure fetch-and-map: no filtering, no DB, no LLM."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import httpx

USER_AGENT = "jobhunt/0.1 (personal job search; low-rate polling of public job boards)"


@dataclass(frozen=True)
class RawJob:
    source: str
    source_job_id: str
    company_name: str
    title: str
    location: str | None
    description_html: str
    url: str
    apply_url: str | None
    posted_at: datetime | None
    comp_min: int | None
    comp_max: int | None
    extra: dict = field(default_factory=dict)


class JobSource(Protocol):
    name: str
    errors: list[dict]

    def fetch(self, since: datetime | None) -> Iterator[RawJob]: ...


# Callable an adapter may be given to ask "do we already have this posting?" so it can skip an
# expensive detail request. It is injected by the registry; adapters never touch the DB themselves.
KnownJob = Callable[[str, str], bool]

# Callable a query-driven adapter may be given to ask "is this title worth downloading the full posting for?".
# When it says no, the adapter yields a listing-only RawJob (extra["listing_only"]) and skips the detail
# request; the prefilter still records and knocks the row out. Injected by the registry from prefilter rules.
TitleGate = Callable[[str], bool]


class RateLimiter:
    """Minimum spacing between requests for one adapter instance."""

    def __init__(self, per_sec: float) -> None:
        self.interval = 1.0 / per_sec if per_sec and per_sec > 0 else 0.0
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._last + self.interval - now
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


def default_client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
    )


def error_record(source: str, message: str, *, company: str | None = None, exc: BaseException | None = None) -> dict:
    import traceback

    head = ""
    if exc is not None:
        head = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-800:]
    return {"source": source, "node": "ingest", "company": company, "message": message, "traceback_head": head}
