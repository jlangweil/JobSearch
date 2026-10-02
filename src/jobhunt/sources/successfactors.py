"""SAP SuccessFactors adapter — Recruiting Marketing ("RMK" / Career Site Builder) career sites.

These sites (e.g. careers.swissre.com, jobs.lincolnfinancial.com) have no public JSON API. The adapter
reads only paths their robots.txt allows:

  list:   GET https://{domain}/search/?q={query}&sortColumn=referencedate&sortDirection=desc&startrow={n}
  detail: GET https://{domain}/job/{slug}/{id}/

(`/services/` — including the RSS feed — is disallowed by robots.txt on these sites, so it is not used.)
Listing pages come in two layouts (table rows and tiles); both carry `a.jobTitle-link` anchors, which is
what is parsed. Date and location come from schema.org microdata on the detail page.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import quote_plus, unquote, urlparse

import httpx

from jobhunt.sources.base import KnownJob, RateLimiter, RawJob, TitleGate, default_client, error_record

NAME = "successfactors"


@dataclass
class SuccessFactorsBoard:
    company_name: str
    domain: str  # e.g. careers.swissre.com
    queries: list[str] = field(default_factory=list)
    locale: str = "en_US"


def parse_board_token(token: str) -> str:
    t = token.strip()
    if "://" not in t:
        t = "https://" + t
    host = urlparse(t).netloc.lower()
    if not host or "." not in host:
        raise ValueError(f"not a SuccessFactors career-site domain: {token!r}")
    return host


_ANCHOR = re.compile(r"<a\b[^>]*class=\"[^\"]*jobTitle-link[^\"]*\"[^>]*>(?P<title>.*?)</a>", re.S | re.I)
_HREF = re.compile(r"href=\"(?P<href>/job/[^\"]+?/(?P<id>\d+)/?)\"", re.I)
_TOTAL = re.compile(r"of\s*<b>\s*(\d[\d,]*)\s*</b>", re.I)
_TAG = re.compile(r"<[^>]+>")


def parse_listing(page_html: str) -> tuple[list[dict], int | None]:
    """Return ([{id, href, title}], total_or_None) from a /search/ results page."""
    out: dict[str, dict] = {}
    for m in _ANCHOR.finditer(page_html):
        tag_open = page_html[m.start(): m.start() + page_html[m.start():].find(">") + 1]
        h = _HREF.search(tag_open)
        if not h:
            continue
        job_id = h.group("id")
        if job_id in out:
            continue
        title = html.unescape(_TAG.sub("", m.group("title"))).strip()
        out[job_id] = {"id": job_id, "href": h.group("href"), "title": re.sub(r"\s+", " ", title)}
    t = _TOTAL.search(page_html)
    return list(out.values()), (int(t.group(1).replace(",", "")) if t else None)


class _DetailParser(HTMLParser):
    VOID = {"br", "img", "hr", "meta", "input", "link", "wbr", "source", "area", "col", "embed", "param"}
    KEEP = {"p", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "div", "b", "strong", "em", "i"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.meta: dict[str, str] = {}
        self.title_parts: list[str] = []
        self.desc_parts: list[str] = []
        self._desc_depth = 0
        self._in_title = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        a = {k: (v or "") for k, v in attrs}
        prop = a.get("itemprop")
        if tag == "meta" and prop and "content" in a:
            self.meta.setdefault(prop, html.unescape(a["content"]))
        if self._desc_depth:
            if tag not in self.VOID:
                self._desc_depth += 1
            if tag in self.KEEP:
                self.desc_parts.append(f"<{tag}>")
            return
        if "jobdescription" in a.get("class", "").split():
            self._desc_depth = 1
            return
        if prop == "title" and tag == "span":
            self._in_title = 1

    def handle_endtag(self, tag: str) -> None:
        if self._desc_depth:
            if tag in self.KEEP and tag not in self.VOID:
                self.desc_parts.append(f"</{tag}>")
            if tag not in self.VOID:
                self._desc_depth -= 1
            return
        if self._in_title and tag == "span":
            self._in_title = 0

    def handle_data(self, data: str) -> None:
        if self._desc_depth:
            self.desc_parts.append(html.escape(html.unescape(data), quote=False))
        elif self._in_title:
            self.title_parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.handle_data(f"&#{name};")


def parse_detail(page_html: str) -> dict:
    p = _DetailParser()
    p.feed(page_html)
    p.close()
    locality = ", ".join(x for x in (p.meta.get("addressLocality"), p.meta.get("addressRegion"),
                                     p.meta.get("addressCountry")) if x)
    return {
        "title": html.unescape(" ".join("".join(p.title_parts).split())),
        "description_html": "".join(p.desc_parts).strip(),
        "posted_at": parse_sf_date(p.meta.get("datePosted")),
        "location": locality or None,
        "employment_hint": p.meta.get("employmentType"),
    }


def parse_sf_date(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip()
    for fmt in ("%a %b %d %H:%M:%S %Z %Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%d %b %Y", "%b %d, %Y"):
        try:
            dt = datetime.strptime(v, fmt)
            return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        except ValueError:
            continue
    return None


class SuccessFactorsSource:
    name = NAME

    def __init__(
        self,
        boards: Sequence[SuccessFactorsBoard],
        *,
        queries: Sequence[str] = (),
        client: httpx.Client | None = None,
        rate_limit_per_sec: float = 1.0,
        max_pages_per_query: int = 4,
        known: KnownJob | None = None,
        title_gate: TitleGate | None = None,
    ) -> None:
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

    def _get(self, url: str) -> str:
        self.limiter.wait()
        r = self.client.get(url, headers={"Accept": "text/html"})
        r.raise_for_status()
        return r.text

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        for board in self.boards:
            try:
                yield from self._fetch_board(board, since)
            except (httpx.HTTPError, ValueError) as exc:
                self.errors.append(error_record(NAME, f"{board.domain}: {exc}", company=board.company_name, exc=exc))

    def _fetch_board(self, board: SuccessFactorsBoard, since: datetime | None) -> Iterator[RawJob]:
        seen: set[str] = set()
        for query in board.queries or self.queries:
            startrow = 0
            for _ in range(self.max_pages):
                url = (f"https://{board.domain}/search/?q={quote_plus(query)}&locale={board.locale}"
                       f"&sortColumn=referencedate&sortDirection=desc&startrow={startrow}")
                rows, total = parse_listing(self._get(url))
                new_rows = [r for r in rows if r["id"] not in seen]
                if not new_rows:
                    break
                stop = False
                for row in new_rows:
                    seen.add(row["id"])
                    self.stats["listed"] += 1
                    job = self._job_for_row(board, row)
                    if job is None:
                        continue
                    if since is not None and job.posted_at is not None and job.posted_at < since:
                        self.stats["skipped_old"] += 1
                        stop = True  # results are sorted newest first
                        continue
                    yield job
                startrow += len(rows)
                if stop or (total is not None and startrow >= total):
                    break

    def _job_for_row(self, board: SuccessFactorsBoard, row: dict) -> RawJob | None:
        source_job_id = f"{board.domain}/{row['id']}"
        url = f"https://{board.domain}{row['href']}"
        if self.known and self.known(NAME, source_job_id):
            self.stats["sightings"] += 1
            return RawJob(NAME, source_job_id, board.company_name, row["title"], None, "", url, url, None, None, None,
                          {"sighting_only": True})
        if self.title_gate and not self.title_gate(row["title"]):
            self.stats["listing_only"] += 1
            return RawJob(NAME, source_job_id, board.company_name, row["title"], None, "", url, url, None, None, None,
                          {"listing_only": True})
        try:
            detail = parse_detail(self._get(url))
        except httpx.HTTPError as exc:
            self.errors.append(error_record(NAME, f"detail {url}: {exc}", company=board.company_name, exc=exc))
            return None
        self.stats["detail_fetched"] += 1
        return self.map_detail(board, row, detail)

    @staticmethod
    def map_detail(board: SuccessFactorsBoard, row: dict, detail: dict) -> RawJob:
        url = f"https://{board.domain}{row['href']}"
        slug_location = unquote(row["href"].split("/")[2]).split("-")[0] if row["href"].count("/") >= 3 else None
        return RawJob(
            source=NAME,
            source_job_id=f"{board.domain}/{row['id']}",
            company_name=board.company_name,
            title=detail.get("title") or row["title"],
            location=detail.get("location") or slug_location,
            description_html=detail.get("description_html") or "",
            url=url,
            apply_url=url,
            posted_at=detail.get("posted_at"),
            comp_min=None,
            comp_max=None,
            extra={"employment_hint": detail.get("employment_hint"), "domain": board.domain},
        )
