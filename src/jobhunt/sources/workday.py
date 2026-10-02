"""Workday adapter — the public "CXS" JSON API that every *.myworkdayjobs.com career site uses.

  list:   POST https://{host}/wday/cxs/{tenant}/{site}/jobs  {"searchText", "limit", "offset", "appliedFacets"}
  detail: GET  https://{host}/wday/cxs/{tenant}/{site}{externalPath}

Large employers list thousands of postings, so Workday is query-driven: each board is searched with
`sources.workday.queries` (or the company's own `queries`). Only the site paths a tenant's robots.txt
marks `Allow:` should be configured; `discover_sites()` reads them.

Source-side narrowing: every tenant exposes facets (job category/family, country, ...) with tenant-specific
names and value ids. One facet request per board discovers them; `choose_facets()` picks the category facet
values matching `category_patterns` (e.g. Technology, Information Technology, Data) and the country values
matching `country_patterns` (United States), and every search on that board is sent with those facets
applied. When a tenant has no such facet, or no value matches, that dimension is left unfiltered rather
than excluding everything.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx

from jobhunt.sources.base import KnownJob, RateLimiter, RawJob, TitleGate, default_client, error_record

NAME = "workday"
PAGE_SIZE = 20  # the CXS API rejects limits above 20


@dataclass
class WorkdayBoard:
    company_name: str
    host: str  # e.g. pru.wd5.myworkdayjobs.com
    tenant: str  # e.g. pru
    site: str  # e.g. Careers
    queries: list[str] = field(default_factory=list)
    category_patterns: list[str] | None = None  # per-company override; [] disables category filtering

    @property
    def base(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}"

    @property
    def public_base(self) -> str:
        return f"https://{self.host}/{self.site}"


_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")


def parse_board_token(token: str) -> tuple[str, str, str]:
    """Accepts 'pru.wd5.myworkdayjobs.com/Careers' or a full career-site URL
    ('https://pru.wd5.myworkdayjobs.com/en-US/Careers/jobs'). Returns (host, tenant, site)."""
    t = token.strip()
    if "://" not in t:
        t = "https://" + t
    u = urlparse(t)
    host = u.netloc.lower()
    parts = [p for p in u.path.split("/") if p]
    if parts and _LOCALE.match(parts[0]):
        parts = parts[1:]
    if not host.endswith(".myworkdayjobs.com") or not parts:
        raise ValueError(f"not a Workday board token: {token!r} (expected <tenant>.wdN.myworkdayjobs.com/<site>)")
    tenant = host.split(".")[0]
    return host, tenant, parts[0]


def parse_posted_on(text: str | None, now: datetime | None = None) -> tuple[datetime | None, int | None]:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago' -> (approx date, days).

    "30+ Days Ago" is Workday's catch-all for everything older than a month, so it says almost nothing about
    the real age: a job reposted last week and one left up since spring look identical. The date still comes
    back (as a lower bound, for display), but the *age* is returned as unknown so nothing is dropped over it.
    """
    if not text:
        return None, None
    now = now or datetime.now(UTC)
    t = text.lower()
    if "today" in t:
        days = 0
    elif "yesterday" in t:
        days = 1
    else:
        m = re.search(r"(\d+)\+?\s*days?", t)
        if not m:
            return None, None
        days = int(m.group(1))
        if "+" in t:
            return now - timedelta(days=days), None     # "at least this old", which is not an age
    return now - timedelta(days=days), days


_CATEGORY_FACET = re.compile(r"job.?famil|categor|classification|function|career.?area|job.?area|department",
                             re.IGNORECASE)
_NOT_CATEGORY = re.compile(r"sub|skill|level|type|time|location|company", re.IGNORECASE)
_COUNTRY_FACET = re.compile(r"country", re.IGNORECASE)

DEFAULT_CATEGORY_PATTERNS = [
    r"technolog", r"information", r"(?-i:\bIT\b)", r"\bdata\b", r"digital", r"software", r"engineer", r"architect",
    r"cyber", r"security", r"cloud", r"infrastructure", r"integration", r"agile", r"analytics",
]
DEFAULT_COUNTRY_PATTERNS = [r"united states", r"\bUSA?\b"]


def _leaf_facets(facets: Sequence[dict]) -> Iterator[dict]:
    """Facets can be nested in groups (e.g. locationMainGroup -> locationCountry). Yield the leaf facets."""
    for f in facets or ():
        values = f.get("values") or []
        if values and isinstance(values[0], dict) and "facetParameter" in values[0]:
            yield from _leaf_facets(values)
        elif f.get("facetParameter"):
            yield f


def choose_facets(facets: Sequence[dict], category_patterns: Sequence[str] | None,
                  country_patterns: Sequence[str] | None) -> tuple[dict[str, list[str]], list[str]]:
    """Return (appliedFacets, human-readable notes) for one board."""
    applied: dict[str, list[str]] = {}
    notes: list[str] = []
    done = {"category": not category_patterns, "country": not country_patterns}
    wanted = {"category": bool(category_patterns), "country": bool(country_patterns)}
    for f in _leaf_facets(facets):
        key = f"{f.get('facetParameter')} {f.get('descriptor') or ''}"
        if not done["category"] and _CATEGORY_FACET.search(key) and not _NOT_CATEGORY.search(key):
            kind, patterns = "category", category_patterns
        elif not done["country"] and _COUNTRY_FACET.search(key):
            kind, patterns = "country", country_patterns
        else:
            continue
        done[kind] = True
        label = f.get("descriptor") or f["facetParameter"]
        matched = [v for v in f.get("values") or [] if v.get("id")
                   and any(re.search(p, v.get("descriptor") or "", re.IGNORECASE) for p in patterns or ())]
        if matched:
            applied[f["facetParameter"]] = [v["id"] for v in matched]
            notes.append(f"{kind} ({label}): " + ", ".join(v.get("descriptor") or "?" for v in matched))
        else:
            notes.append(f"{kind} facet {label!r} has no matching value; not filtering on it")
    for kind in ("category", "country"):
        if wanted[kind] and not done[kind]:
            notes.append(f"no {kind} facet on this site; not filtering on it")
    return applied, notes


def _parse_start_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


class WorkdaySource:
    name = NAME

    def __init__(
        self,
        boards: Sequence[WorkdayBoard],
        *,
        queries: Sequence[str] = (),
        client: httpx.Client | None = None,
        rate_limit_per_sec: float = 2.0,
        max_pages_per_query: int = 5,
        known: KnownJob | None = None,
        title_gate: TitleGate | None = None,
        category_patterns: Sequence[str] | None = None,
        country_patterns: Sequence[str] | None = None,
    ) -> None:
        self.boards = list(boards)
        self.category_patterns = list(category_patterns) if category_patterns is not None else None
        self.country_patterns = list(country_patterns) if country_patterns is not None else None
        self.facet_notes: dict[str, list[str]] = {}
        self.queries = [q for q in queries if q is not None] or [""]
        self.client = client or default_client()
        self.limiter = RateLimiter(rate_limit_per_sec)
        self.max_pages = max_pages_per_query
        self.known = known
        self.title_gate = title_gate
        self.errors: list[dict] = []
        self.stats: dict[str, int] = {"listed": 0, "detail_fetched": 0, "skipped_old": 0, "sightings": 0,
                                      "listing_only": 0}

    # -- http
    def _post(self, url: str, body: dict) -> dict:
        self.limiter.wait()
        r = self.client.post(url, json=body, headers={"Accept": "application/json", "Content-Type": "application/json"})
        r.raise_for_status()
        return r.json()

    def _get(self, url: str) -> dict:
        self.limiter.wait()
        r = self.client.get(url, headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()

    # -- fetch
    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        for board in self.boards:
            try:
                yield from self._fetch_board(board, since)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                self.errors.append(error_record(NAME, f"{board.host}/{board.site}: {exc}", company=board.company_name,
                                                exc=exc))

    def board_facets(self, board: WorkdayBoard) -> dict[str, list[str]]:
        cats = board.category_patterns if board.category_patterns is not None else self.category_patterns
        if not cats and not self.country_patterns:
            return {}
        data = self._post(f"{board.base}/jobs", {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
        applied, notes = choose_facets(data.get("facets") or [], cats, self.country_patterns)
        self.facet_notes[f"{board.company_name} ({board.host}/{board.site})"] = notes
        return applied

    def list_postings(self, board: WorkdayBoard, query: str,
                      applied: dict[str, list[str]] | None = None) -> Iterator[dict]:
        offset = 0
        for _ in range(self.max_pages):
            data = self._post(f"{board.base}/jobs", {"appliedFacets": applied or {}, "limit": PAGE_SIZE,
                                                     "offset": offset, "searchText": query})
            postings = data.get("jobPostings") or []
            yield from postings
            total = int(data.get("total") or 0)
            offset += PAGE_SIZE
            if not postings or offset >= total:
                break

    def _fetch_board(self, board: WorkdayBoard, since: datetime | None) -> Iterator[RawJob]:
        seen: set[str] = set()
        window_days = None
        if since is not None:
            window_days = (datetime.now(UTC) - since).total_seconds() / 86400.0
        applied = self.board_facets(board)
        for query in board.queries or self.queries:
            for posting in self.list_postings(board, query, applied):
                path = posting.get("externalPath")
                if not path or path in seen:
                    continue
                seen.add(path)
                self.stats["listed"] += 1
                posted_guess, days = parse_posted_on(posting.get("postedOn"))
                if window_days is not None and days is not None and days > window_days + 1:
                    self.stats["skipped_old"] += 1
                    continue
                source_job_id = f"{board.tenant}/{path.rstrip('/').split('/')[-1]}"
                url = f"{board.public_base}{path}"
                if self.known and self.known(NAME, source_job_id):
                    self.stats["sightings"] += 1
                    yield RawJob(NAME, source_job_id, board.company_name, posting.get("title") or "",
                                 posting.get("locationsText"), "", url, url, posted_guess, None, None,
                                 {"sighting_only": True})
                    continue
                if self.title_gate and not self.title_gate(posting.get("title") or ""):
                    self.stats["listing_only"] += 1
                    yield RawJob(NAME, source_job_id, board.company_name, posting.get("title") or "",
                                 posting.get("locationsText"), "", url, url, posted_guess, None, None,
                                 {"listing_only": True})
                    continue
                try:
                    detail = self._get(f"{board.base}{path}")
                except httpx.HTTPError as exc:
                    self.errors.append(error_record(NAME, f"detail {url}: {exc}", company=board.company_name, exc=exc))
                    continue
                self.stats["detail_fetched"] += 1
                yield self.map_detail(board, posting, detail, source_job_id, url, posted_guess)

    @staticmethod
    def map_detail(board: WorkdayBoard, posting: dict, detail: dict, source_job_id: str, url: str,
                   posted_guess: datetime | None) -> RawJob:
        info = detail.get("jobPostingInfo") or {}
        locations = [info.get("location")] + list(info.get("additionalLocations") or [])
        location = "; ".join(x for x in locations if x) or posting.get("locationsText")
        hiring_org = (detail.get("hiringOrganization") or {}).get("name")
        public_url = info.get("externalUrl") or url
        return RawJob(
            source=NAME,
            source_job_id=source_job_id,
            company_name=board.company_name,
            title=info.get("title") or posting.get("title") or "",
            location=location,
            description_html=info.get("jobDescription") or "",
            url=public_url,
            apply_url=public_url,
            posted_at=_parse_start_date(info.get("startDate")) or posted_guess,
            comp_min=None,
            comp_max=None,
            extra={
                "workplace_type": info.get("remoteType"),
                "employment_hint": info.get("timeType"),
                "req_id": info.get("jobReqId") or (posting.get("bulletFields") or [None])[0],
                "hiring_organization": hiring_org,
                "tenant": board.tenant,
                "site": board.site,
            },
        )


def discover_sites(url_or_host: str, client: httpx.Client | None = None,
                   category_patterns: Sequence[str] | None = None,
                   country_patterns: Sequence[str] | None = None) -> list[dict]:
    """Read a tenant's robots.txt and report the public (Allow:) career sites with posting counts, plus the
    facets the adapter would apply to each."""
    t = url_or_host if "://" in url_or_host else "https://" + url_or_host
    host = urlparse(t).netloc.lower()
    tenant = host.split(".")[0]
    client = client or default_client()
    r = client.get(f"https://{host}/robots.txt")
    r.raise_for_status()
    out = []
    for site in re.findall(r"^Allow:\s*/([^/\s]+)/", r.text, re.MULTILINE):
        try:
            j = client.post(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                            json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                            headers={"Accept": "application/json"})
            payload = j.json() if j.status_code == 200 else {}
            total = payload.get("total")
            _, notes = choose_facets(payload.get("facets") or [], category_patterns, country_patterns)
        except (httpx.HTTPError, ValueError):
            total, notes = None, []
        out.append({"board_token": f"{host}/{site}", "site": site, "total": total, "facets": notes})
    return out
