"""Phenom adapter: career sites built on Phenom People (e.g. careers.marsh.com, careers.cencora.com).

  page:   GET  https://{host}/{country}/{lang}/search-results           (once per board: reads phApp.pageId)
  list:   POST https://{host}/widgets  {"ddoKey": "refineSearch", "keywords", "from", "size", "selected_fields"}
  detail: POST https://{host}/widgets  {"ddoKey": "jobDetail", "jobSeqNo"}

These are the same JSON calls the public site makes. Phenom robots.txt files disallow */apply and */px-widgets, not
/widgets or /job/. Like Workday, boards are query-driven; the site's own `country` and `category` facets narrow the
search when values match `country_patterns` / `category_patterns` (a site without a match is left unfiltered).
Note: many Phenom sites front a Workday tenant (the apply link goes to myworkdayjobs.com). If that tenant is already
configured as a Workday company, don't add the Phenom site too.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx

from jobhunt.db.engine import parse_iso
from jobhunt.sources.base import KnownJob, RateLimiter, RawJob, TitleGate, default_client, error_record

NAME = "phenom"
PAGE_SIZE = 50
_PAGE_ID = re.compile(r"""(?:phApp\.pageId\s*=\s*|"pageId"\s*:\s*)["']([^"']+)["']""")
_LOCALE_PATH = re.compile(r"^[a-z]{2,8}/[a-z]{2}$", re.IGNORECASE)


@dataclass
class PhenomBoard:
    company_name: str
    host: str  # careers.marsh.com
    locale_path: str = "us/en"  # "{country}/{lang}" as in the site URL, e.g. global/en
    queries: list[str] = field(default_factory=list)
    category_patterns: list[str] | None = None

    @property
    def country(self) -> str:
        return self.locale_path.split("/")[0].lower()

    @property
    def lang(self) -> str:
        return f"{self.locale_path.split('/')[1].lower()}_{self.country}"

    @property
    def site(self) -> str:
        return f"https://{self.host}/{self.locale_path}"


def parse_board_token(token: str) -> tuple[str, str]:
    """'careers.marsh.com/global/en' or a site URL ('https://careers.cencora.com/us/en/search-results').
    Returns (host, 'country/lang'); the locale defaults to us/en."""
    t = token.strip()
    if "://" not in t:
        t = "https://" + t
    u = urlparse(t)
    if not u.hostname or "." not in u.hostname:
        raise ValueError(f"phenom board token {token!r} should look like careers.example.com/us/en")
    parts = [p for p in u.path.split("/") if p]
    locale = "/".join(parts[:2]) if len(parts) >= 2 and _LOCALE_PATH.match("/".join(parts[:2])) else "us/en"
    return u.hostname.lower(), locale.lower()


def choose_fields(aggregations: Sequence[dict], category_patterns: Sequence[str] | None,
                  country_patterns: Sequence[str] | None) -> tuple[dict[str, list[str]], list[str]]:
    selected: dict[str, list[str]] = {}
    notes: list[str] = []
    by_field = {a.get("field"): a.get("value") or {} for a in aggregations or []}
    for fname, patterns in (("country", country_patterns), ("category", category_patterns)):
        if not patterns:
            continue
        values = by_field.get(fname)
        if not values:
            notes.append(f"no {fname} facet on this site; not filtering on it")
            continue
        names = list(values) if isinstance(values, dict) else [v.get("key") for v in values if isinstance(v, dict)]
        matched = [n for n in names if n and any(re.search(p, n, re.IGNORECASE) for p in patterns)]
        if matched:
            selected[fname] = matched
            notes.append(f"{fname}: " + ", ".join(matched))
        else:
            notes.append(f"{fname} facet has no matching value; not filtering on it")
    return selected, notes


class PhenomSource:
    name = NAME

    def __init__(self, boards: Sequence[PhenomBoard], *, queries: Sequence[str] = (),
                 client: httpx.Client | None = None, rate_limit_per_sec: float = 1.0, max_pages_per_query: int = 3,
                 known: KnownJob | None = None, title_gate: TitleGate | None = None,
                 category_patterns: Sequence[str] | None = None, country_patterns: Sequence[str] | None = None) -> None:
        self.boards = list(boards)
        self.queries = [q for q in queries if q is not None] or [""]
        self.client = client or default_client()
        self.limiter = RateLimiter(rate_limit_per_sec)
        self.max_pages = max_pages_per_query
        self.known = known
        self.title_gate = title_gate
        self.category_patterns = list(category_patterns) if category_patterns is not None else None
        self.country_patterns = list(country_patterns) if country_patterns is not None else None
        self.facet_notes: dict[str, list[str]] = {}
        self.errors: list[dict] = []
        self.stats: dict[str, int] = {"listed": 0, "detail_fetched": 0, "skipped_old": 0, "sightings": 0,
                                      "listing_only": 0}

    # -- http
    def _widgets(self, board: PhenomBoard, body: dict) -> dict:
        self.limiter.wait()
        r = self.client.post(f"https://{board.host}/widgets", json=body,
                             headers={"Accept": "application/json", "Content-Type": "application/json",
                                      "Referer": f"{board.site}/search-results"})
        r.raise_for_status()
        return r.json()

    def page_id(self, board: PhenomBoard) -> str:
        self.limiter.wait()
        r = self.client.get(f"{board.site}/search-results")
        r.raise_for_status()
        m = _PAGE_ID.search(r.text)
        return m.group(1) if m else "page1"

    def _search_body(self, board: PhenomBoard, page_id: str, query: str, offset: int, size: int,
                     selected: dict[str, list[str]]) -> dict:
        return {"lang": board.lang, "deviceType": "desktop", "country": board.country, "pageName": "search-results",
                "ddoKey": "refineSearch", "sortBy": "", "subsearch": "", "from": offset, "jobs": True,
                "counts": True, "all_fields": ["category", "country"], "size": size, "clearAll": False,
                "jdsource": "facets", "isSliderEnable": False, "pageId": page_id, "siteType": "external",
                "keywords": query, "global": True, "selected_fields": selected, "locationData": {}}

    def list_postings(self, board: PhenomBoard, page_id: str, query: str,
                      selected: dict[str, list[str]]) -> Iterator[dict]:
        offset = 0
        for _ in range(self.max_pages):
            data = (self._widgets(board, self._search_body(board, page_id, query, offset, PAGE_SIZE, selected))
                    .get("refineSearch") or {})
            jobs = (data.get("data") or {}).get("jobs") or []
            yield from jobs
            offset += PAGE_SIZE
            if not jobs or offset >= int(data.get("totalHits") or 0):
                break

    def detail(self, board: PhenomBoard, page_id: str, job_seq: str) -> dict:
        body = {"lang": board.lang, "deviceType": "desktop", "country": board.country, "pageName": "job",
                "ddoKey": "jobDetail", "jobSeqNo": job_seq, "pageId": page_id, "siteType": "external"}
        return ((self._widgets(board, body).get("jobDetail") or {}).get("data") or {}).get("job") or {}

    # -- fetch
    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        for board in self.boards:
            try:
                yield from self._fetch_board(board, since)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                self.errors.append(error_record(NAME, f"{board.host}: {exc}", company=board.company_name, exc=exc))

    def _fetch_board(self, board: PhenomBoard, since: datetime | None) -> Iterator[RawJob]:
        page_id = self.page_id(board)
        cats = board.category_patterns if board.category_patterns is not None else self.category_patterns
        selected: dict[str, list[str]] = {}
        if cats or self.country_patterns:
            probe = self._widgets(board, self._search_body(board, page_id, "", 0, 1, {})).get("refineSearch") or {}
            selected, notes = choose_fields((probe.get("data") or {}).get("aggregations") or [], cats,
                                            self.country_patterns)
            self.facet_notes[f"{board.company_name} ({board.host})"] = notes
        cutoff = since - timedelta(days=1) if since else None
        seen: set[str] = set()
        for query in board.queries or self.queries:
            for posting in self.list_postings(board, page_id, query, selected):
                seq = posting.get("jobSeqNo")
                if not seq or seq in seen:
                    continue
                seen.add(seq)
                self.stats["listed"] += 1
                posted = _posted(posting)
                if cutoff and posted and posted < cutoff:
                    self.stats["skipped_old"] += 1
                    continue
                url = f"{board.site}/job/{seq}"
                title = posting.get("title") or ""
                location = _location(posting)
                if self.known and self.known(NAME, seq):
                    self.stats["sightings"] += 1
                    yield RawJob(NAME, seq, board.company_name, title, location, "", url,
                                 posting.get("applyUrl") or url, posted, None, None, {"sighting_only": True})
                    continue
                if self.title_gate and not self.title_gate(title):
                    self.stats["listing_only"] += 1
                    yield RawJob(NAME, seq, board.company_name, title, location, "", url,
                                 posting.get("applyUrl") or url, posted, None, None, {"listing_only": True})
                    continue
                try:
                    job = self.detail(board, page_id, seq)
                except (httpx.HTTPError, ValueError) as exc:
                    self.errors.append(error_record(NAME, f"detail {url}: {exc}", company=board.company_name, exc=exc))
                    continue
                self.stats["detail_fetched"] += 1
                yield self.map_job(board, posting, job, url)

    @staticmethod
    def map_job(board: PhenomBoard, posting: dict, job: dict, url: str) -> RawJob:
        merged = {**posting, **{k: v for k, v in job.items() if v not in (None, "", [])}}
        remote = str(merged.get("isRemote") or "").lower() in ("yes", "true", "1")
        return RawJob(
            source=NAME,
            source_job_id=merged["jobSeqNo"],
            company_name=board.company_name,
            title=merged.get("title") or "",
            location=_location(merged) + ("; Remote" if remote and "remote" not in _location(merged).lower() else ""),
            description_html=merged.get("description") or merged.get("descriptionTeaser") or "",
            url=url,
            apply_url=merged.get("applyUrl") or url,
            posted_at=_posted(merged),
            comp_min=None,
            comp_max=None,
            extra={"workplace_type": "remote" if remote else None, "employment_hint": merged.get("type"),
                   "req_id": merged.get("jobId") or merged.get("reqId"), "team": merged.get("category")},
        )


def _posted(posting: dict) -> datetime | None:
    raw = posting.get("postedDate") or posting.get("dateCreated")
    if not raw:
        return None
    dt = parse_iso(str(raw).replace("+0000", "+00:00"))
    return dt.astimezone(UTC) if dt else None


def _location(posting: dict) -> str:
    multi = []
    for x in posting.get("multi_location") or []:  # strings in search results, objects in some job details
        if isinstance(x, dict):
            x = x.get("location") or x.get("cityStateCountry") or ", ".join(
                str(x[k]) for k in ("city", "state", "country") if x.get(k))
        if x:
            multi.append(str(x))
    return "; ".join(dict.fromkeys(multi)) if multi else str(posting.get("location") or
                                                            posting.get("cityStateCountry") or "")
