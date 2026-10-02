"""Job-alert emails (LinkedIn, Indeed and anything else that mails you listings).

Nothing is scraped: these are the alert emails those sites send *you*. jobhunt opens the mailbox read-only over IMAP,
looks only at messages from the senders (and/or in the folder) you configure, and files each posting it finds.

Alert emails carry a title, company, location and a tracking link, but not the job description, so imported jobs are
listing-only: they show up in "Waiting to score" with a **Paste the posting** action. Their links usually lead to the
employer's own careers page, which "Add a job" can read in full. LinkedIn and Indeed pages themselves are never
fetched (CLAUDE.md).
"""

from __future__ import annotations

import email
import html
import imaplib
import logging
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from urllib.parse import parse_qs, unquote, urlparse

from jobhunt.sources.base import RawJob, error_record
from jobhunt.sources.normalize import collapse_whitespace, slugify, strip_html

NAME = "email"
log = logging.getLogger(__name__)

DEFAULT_SENDERS = ("jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com", "jobs-listings@linkedin.com",
                   "alert@indeed.com", "noreply@indeed.com", "invitations@indeed.com",
                   "donotreply@match.indeed.com", "noreply@glassdoor.com", "jobs-noreply@ziprecruiter.com")

_ANCHOR = re.compile(r"<a\b[^>]*href=[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<inner>.*?)</a>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_PROVIDERS: dict[str, re.Pattern[str]] = {
    "linkedin": re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?P<id>\d+)", re.I),
    "indeed": re.compile(r"indeed\.com/(?:rc/clk|pagead/clk|viewjob|job)\b", re.I),
    "glassdoor": re.compile(r"glassdoor\.[a-z.]+/(?:job-listing|partner/jobListing)", re.I),
    "ziprecruiter": re.compile(r"ziprecruiter\.com/(?:c|jobs|job)/", re.I),
}
# LinkedIn puts "Company · New York, NY" on one line; Indeed uses separate lines for company, location and pay.
_COMPANY_LOCATION = re.compile(r"^(?P<company>[^·|]{2,60}?)\s*(?:·|\||–|—|\s-\s)\s*(?P<location>[^·|]{2,60})$")
_LOCATION = re.compile(r"(^|,\s*)(remote|hybrid|on-?site|anywhere)\b|^[A-Za-z][A-Za-z .'\-]{1,40},\s*[A-Z]{2}\b", re.I)
_SALARY = re.compile(r"\$\s?(?P<low>[\d,]+(?:\.\d+)?)(?:\s*(?:-|–|—|to)\s*\$?\s?(?P<high>[\d,]+(?:\.\d+)?))?"
                     r"(?P<unit>\s*(?:a|per)\s*(?:year|hour|month|week)|\s*(?:annually|yearly|hourly))?", re.I)
_RATING = re.compile(r"\s*\d\.\d\s*(?:★|\*|out of 5(?: stars)?)?\s*$")
_NOISE = re.compile(r"^(view job|apply now|see all jobs|unsubscribe|actively recruiting|easy apply|promoted|new|"
                    r"\d+ (?:new )?jobs?|be an early applicant|.*\bago\b)$", re.I)


@dataclass
class EmailAccount:
    host: str
    user: str
    password: str
    port: int = 993
    folder: str = "INBOX"
    senders: list[str] = field(default_factory=lambda: list(DEFAULT_SENDERS))
    subject_patterns: list[str] = field(default_factory=list)
    max_messages: int = 200
    max_age_days: int = 14


# --------------------------------------------------------------------------------------------- parsing


def _text(value: str) -> str:
    return collapse_whitespace(html.unescape(_TAG.sub(" ", value or "")))


def provider_for(url: str) -> str | None:
    return next((name for name, pattern in _PROVIDERS.items() if pattern.search(url)), None)


def job_id_for(provider: str, url: str, *, title: str = "", company: str = "") -> str:
    """A stable id per posting, so the same job in tomorrow's digest is recognised instead of duplicated.

    Tracking links change between emails, so the id comes from the provider's own job key where there is one
    (LinkedIn job id, Indeed `jk`), and otherwise from the title and company."""
    if provider == "linkedin":
        m = _PROVIDERS["linkedin"].search(url)
        if m:
            return f"linkedin/{m.group('id')}"
    elif provider == "indeed":
        query = parse_qs(urlparse(url).query)
        key = (query.get("jk") or query.get("vjk") or [""])[0]
        if key:
            return f"indeed/{key}"
    else:
        tail = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
        if len(tail) > 8 and not tail.isdigit():
            return f"{provider}/{tail[:80]}"
    if title:
        return f"{provider}/{slugify(f'{title} {company}')[:80]}"
    return f"{provider}/{urlparse(url).path[:80]}"


def _subject_company(subject: str) -> str | None:
    """LinkedIn: "Integration Lead: Prudential is hiring"; Indeed: "new jobs for … at Acme"."""
    m = re.search(r"\bat\s+([A-Z][\w&.,'\- ]{2,50}?)(?:\s+(?:in|is|are)\b|$)", subject)
    return m.group(1).strip(" ,.") if m else None


def _money(value: str | None) -> int | None:
    try:
        return int(round(float(str(value).replace(",", ""))))
    except (TypeError, ValueError):
        return None


def parse_pay(line: str) -> dict:
    """"$151,300 - $261,500 a year" -> {comp_min, comp_max, comp_interval}. Hourly rates keep their interval."""
    m = _SALARY.search(line)
    if not m:
        return {}
    low, high = _money(m.group("low")), _money(m.group("high"))
    unit = (m.group("unit") or "").lower()
    if low is None:
        return {}
    hourly = "hour" in unit or (not unit and low < 500)
    if not hourly and "month" not in unit and "week" not in unit and low < 1000:
        return {}  # a rating or some other stray number, not pay
    return {"comp_min": low, "comp_max": high or low,
            "comp_interval": "hourly" if hourly else ("monthly" if "month" in unit else None)}


def _describe_block(lines: Sequence[str]) -> dict:
    """Company / location / pay / snippet from the lines under one job link, in whatever order they appear."""
    out: dict = {"company": None, "location": None, "snippet": None}
    for line in lines[:8]:
        text = line.strip()
        if not text or _NOISE.match(text):
            continue
        pair = _COMPANY_LOCATION.match(text)
        if pair and out["company"] is None and out["location"] is None and "$" not in text:
            out["company"], out["location"] = pair.group("company").strip(), pair.group("location").strip()
            continue
        pay = parse_pay(text) if "$" in text else {}
        if pay:
            out.update(pay)
            continue
        if out["location"] is None and len(text) <= 60 and _LOCATION.search(text):
            out["location"] = text
            continue
        if out["company"] is None and len(text) <= 60:
            out["company"] = _RATING.sub("", text).strip(" ·-–—,")
            continue
        if out["snippet"] is None and len(text) >= 60:
            out["snippet"] = text
    return out


def parse_alert(html_body: str, subject: str = "") -> list[dict]:
    """Job postings found in one alert email (a digest holds many): title, company, location, pay, snippet, link."""
    found: dict[str, dict] = {}
    anchors = list(_ANCHOR.finditer(html_body or ""))
    for i, m in enumerate(anchors):
        url = html.unescape(m.group("href")).strip()
        provider = provider_for(url)
        title = _text(m.group("inner"))
        if not provider or not title or _NOISE.match(title) or len(title) > 120:
            continue
        tail = html_body[m.end(): anchors[i + 1].start() if i + 1 < len(anchors) else m.end() + 800]
        block = _describe_block(strip_html(tail).split("\n"))
        company = block["company"] or _subject_company(subject) or provider.title()
        key = job_id_for(provider, url, title=title, company=company)
        if key in found:
            continue
        found[key] = {"provider": provider, "url": url, "title": title, "company": company,
                      "location": block["location"], "snippet": block["snippet"],
                      "comp_min": block.get("comp_min"), "comp_max": block.get("comp_max"),
                      "comp_interval": block.get("comp_interval")}
    return list(found.values())


def message_html(msg: email.message.Message) -> str:
    parts: list[str] = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart":
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            body = part.get_payload(decode=True) or b""
            text = body.decode(charset, errors="replace")
        except (LookupError, ValueError):
            continue
        if part.get_content_type() == "text/html":
            parts.append(text)
        elif part.get_content_type() == "text/plain" and not parts:
            parts.append(re.sub(r"(https?://\S+)", r'<a href="\1">\1</a>', html.escape(text)))
    return "\n".join(parts)


def header(msg: email.message.Message, name: str) -> str:
    raw = msg.get(name, "")
    try:
        return str(make_header(decode_header(raw)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return str(raw)


# --------------------------------------------------------------------------------------------- source


class EmailAlertSource:
    name = NAME

    def __init__(self, account: EmailAccount, *, connect=None, known=None) -> None:  # noqa: ANN001
        self.account = account
        self._connect = connect or self._imap
        self.known = known
        self.errors: list[dict] = []
        self.stats: dict[str, int] = {"messages": 0, "matched": 0, "postings": 0, "sightings": 0}

    def _imap(self):  # noqa: ANN202
        client = imaplib.IMAP4_SSL(self.account.host, self.account.port)
        client.login(self.account.user, self.account.password)
        return client

    def _search(self, client, since: datetime) -> list[bytes]:  # noqa: ANN001
        folder = self.account.folder or "INBOX"
        status, _ = client.select(f'"{folder}"', readonly=True)  # read-only: nothing is marked, moved or deleted
        if status != "OK":
            raise RuntimeError(f"mailbox folder {folder!r} not found")
        status, data = client.search(None, "SINCE", since.strftime("%d-%b-%Y"))
        if status != "OK":
            raise RuntimeError("the mail server refused the search")
        return (data[0] or b"").split()[-self.account.max_messages:]

    def wanted(self, from_addr: str, subject: str) -> bool:
        senders = [s.lower().strip() for s in self.account.senders if s.strip()]
        addr = parseaddr(from_addr)[1].lower()
        if senders and any(s in addr or (s.startswith("@") and addr.endswith(s)) for s in senders):
            return True
        return any(re.search(p, subject, re.I) for p in self.account.subject_patterns if p.strip())

    def messages(self, since: datetime) -> Iterator[tuple[email.message.Message, datetime | None]]:
        client = self._connect()
        try:
            for uid in self._search(client, since):
                status, data = client.fetch(uid, "(BODY.PEEK[])")  # PEEK: leaves the message unread
                if status != "OK" or not data or not isinstance(data[0], tuple):
                    continue
                self.stats["messages"] += 1
                msg = email.message_from_bytes(data[0][1])
                if not self.wanted(header(msg, "From"), header(msg, "Subject")):
                    continue
                self.stats["matched"] += 1
                try:
                    sent = parsedate_to_datetime(msg.get("Date", ""))
                except (TypeError, ValueError):
                    sent = None
                yield msg, sent.astimezone(UTC) if sent and sent.tzinfo else sent
        finally:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        cutoff = since or datetime.now(UTC) - timedelta(days=self.account.max_age_days)
        cutoff = max(cutoff, datetime.now(UTC) - timedelta(days=self.account.max_age_days))
        try:
            messages = list(self.messages(cutoff))
        except Exception as exc:  # one clear error; the run continues with other sources
            self.errors.append(error_record(NAME, f"{self.account.host}: {exc}", exc=exc))
            return
        for msg, sent in messages:
            subject = header(msg, "Subject")
            try:
                postings = parse_alert(message_html(msg), subject)
            except Exception as exc:  # a malformed email must not stop the rest
                self.errors.append(error_record(NAME, f"could not read {subject[:80]!r}: {exc}", exc=exc))
                continue
            for p in postings:
                self.stats["postings"] += 1
                source_job_id = job_id_for(p["provider"], p["url"], title=p["title"], company=p["company"])
                if self.known and self.known(NAME, source_job_id):
                    self.stats["sightings"] += 1
                    yield RawJob(NAME, source_job_id, p["company"], p["title"], p["location"], "", p["url"],
                                 p["url"], sent, None, None, {"sighting_only": True})
                    continue
                snippet = f"<p>{html.escape(p['snippet'])}</p>" if p.get("snippet") else ""
                yield RawJob(NAME, source_job_id, p["company"], p["title"], p["location"], snippet, p["url"],
                             p["url"], sent, p.get("comp_min"), p.get("comp_max"),
                             {"listing_only": True, "alert_provider": p["provider"], "alert_subject": subject[:200],
                              "comp_interval": p.get("comp_interval"), "description_truncated": bool(snippet)})


def check_account(account: EmailAccount, *, connect=None, days: int = 14) -> dict:  # noqa: ANN001
    """Connect, count matching messages and show what would be imported. Never changes the mailbox."""
    src = EmailAlertSource(account, connect=connect)
    try:
        jobs = list(src.fetch(datetime.now(UTC) - timedelta(days=days)))
    except Exception as exc:  # noqa: BLE001 - reported on the page
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}", "samples": []}
    if src.errors:
        return {"ok": False, "message": src.errors[0]["message"], "samples": []}
    samples = [f"{j.title} · {j.company_name}{f' · {j.location}' if j.location else ''}" for j in jobs[:6]]
    return {"ok": True, "samples": samples, "stats": src.stats,
            "message": (f"Read {src.stats['messages']} message(s) from the last {days} days, "
                        f"{src.stats['matched']} from your alert senders, and found "
                        f"{src.stats['postings']} posting(s).")}
