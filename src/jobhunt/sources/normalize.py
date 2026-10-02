"""Pure normalization of a RawJob into job columns: HTML stripping, content hash, remote/employment
derivation, comp parsing, company slug. No DB, no network, no model."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from html.parser import HTMLParser

from jobhunt.db.engine import to_iso
from jobhunt.sources.base import RawJob

MAX_DESCRIPTION_CHARS = 20_000

_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "section",
    "article", "header", "footer", "blockquote", "pre", "hr",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n• ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    raw = value
    # Greenhouse double-escapes its HTML (&lt;p&gt;); unescape once if it looks escaped.
    if "&lt;" in raw and "<" not in raw:
        raw = html.unescape(raw)
    parser = _TextExtractor()
    parser.feed(raw)
    parser.close()
    return collapse_whitespace("".join(parser.parts))


def collapse_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    lines = [re.sub(r"[ \t\f\v]+", " ", ln).strip() for ln in text.split("\n")]
    out, blank = [], False
    for ln in lines:
        if not ln:
            if not blank and out:
                out.append("")
            blank = True
        else:
            out.append(ln)
            blank = False
    return "\n".join(out).strip()


def normalize_ws_for_match(text: str) -> str:
    """Whitespace-normalized form used for substring checks (evidence quotes)."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text or "")).strip()


def content_hash(title: str, company: str, description: str) -> str:
    basis = "\n".join(normalize_ws_for_match(x).lower() for x in (title, company, description))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"&", " and ", s)
    s = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|plc|group|holdings)\b\.?", " ", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "unknown"


# --------------------------------------------------------------------------------------------- remote


def derive_remote_type(title: str, location: str | None, description: str, hint: str | None = None) -> str:
    h = (hint or "").lower().replace("-", "").replace("_", "").replace(" ", "")
    if h in ("remote", "fullyremote"):
        return "remote"
    if h == "hybrid":
        return "hybrid"
    if h in ("onsite", "inoffice", "inperson"):
        return "onsite"
    head = f"{title} {location or ''}".lower()
    if "hybrid" in head:
        return "hybrid"
    if re.search(r"\b(remote|virtual|work from home|wfh|anywhere)\b", head):
        return "remote"
    body = description[:6000].lower()
    if re.search(r"\b(fully remote|100% remote|remote[- ]first|this (is a|role is|position is) (fully )?remote|"
                 r"remote position|work from anywhere|remote eligible)\b", body):
        return "remote"
    if re.search(r"\bhybrid\b", body):
        return "hybrid"
    if re.search(r"\b(on-?site|in[- ]office|in person) (role|position|5 days|five days|required)\b", body):
        return "onsite"
    return "unknown"


# --------------------------------------------------------------------------------------------- employment


def derive_employment_type(title: str, description: str, hint: str | None = None) -> str:
    h = (hint or "").lower()
    t = title.lower()
    body = description[:6000].lower()
    if re.search(r"contract[- ]to[- ]hire|\bc2h\b|temp[- ]to[- ]perm", f"{h} {t} {body}"):
        return "c2h"
    if re.search(r"\b(contract|contractor|temporary|temp|fixed[- ]term|freelance|1099)\b", f"{h} {t}"):
        return "contract"
    if re.search(r"full[-_ ]?time|permanent|regular", h):
        return "fte"
    if re.search(r"part[-_ ]?time|intern", h):
        return "contract" if "intern" not in h else "unknown"
    if re.search(r"\bfull[- ]time\b|\bpermanent\b", body):
        return "fte"
    return "unknown"


# --------------------------------------------------------------------------------------------- comp

_AMOUNT = r"\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+(?:\.\d+)?\s?[kK]\b|\d{4,7}(?:\.\d{2})?"
_ANNUAL_RANGE = re.compile(
    rf"\$\s?(?P<a>{_AMOUNT})\s*(?:USD)?\s*(?:-|–|—|to)\s*(?:USD)?\s*\$?\s?(?P<b>{_AMOUNT})", re.IGNORECASE
)
_HOURLY_RANGE = re.compile(
    r"\$\s?(?P<a>\d{2,3}(?:\.\d{1,2})?)\s*(?:(?:/\s?|per\s)(?:hr|hour)\s*)?(?:-|–|—|to)\s*\$?\s?"
    r"(?P<b>\d{2,3}(?:\.\d{1,2})?)\s*"
    r"(?:/\s?|per\s)(?:hr|hour)", re.IGNORECASE
)
_HOURLY_SINGLE = re.compile(r"\$\s?(?P<a>\d{2,3}(?:\.\d{1,2})?)\s*(?:/\s?|per\s)(?:hr|hour)", re.IGNORECASE)


def _amount(token: str) -> float:
    t = token.replace(",", "").replace(" ", "").lower()
    if t.endswith("k"):
        return float(t[:-1]) * 1000
    return float(t)


def parse_comp(description: str, hint_min: int | float | None = None, hint_max: int | float | None = None,
               hint_interval: str | None = None) -> tuple[int | None, int | None, str]:
    """Returns (comp_min, comp_max, comp_source) in whole annual USD."""
    if hint_min or hint_max:
        mult = 2080 if (hint_interval or "").lower().startswith(("hour", "per-hour", "1 hour")) else 1
        lo = int(round(float(hint_min or hint_max) * mult))
        hi = int(round(float(hint_max or hint_min) * mult))
        return min(lo, hi), max(lo, hi), "posted" if mult == 1 else "inferred"
    text = description or ""
    for m in _HOURLY_RANGE.finditer(text):
        lo, hi = _amount(m.group("a")), _amount(m.group("b"))
        if 10 <= lo <= 500 and 10 <= hi <= 500:
            return int(round(min(lo, hi) * 2080)), int(round(max(lo, hi) * 2080)), "inferred"
    for m in _ANNUAL_RANGE.finditer(text):
        lo, hi = _amount(m.group("a")), _amount(m.group("b"))
        if 20_000 <= lo <= 2_000_000 and 20_000 <= hi <= 2_000_000:
            return int(round(min(lo, hi))), int(round(max(lo, hi))), "posted"
    m = _HOURLY_SINGLE.search(text)
    if m:
        v = _amount(m.group("a"))
        if 10 <= v <= 500:
            return int(round(v * 2080)), int(round(v * 2080)), "inferred"
    return None, None, "none"


# --------------------------------------------------------------------------------------------- record


def normalize_raw(raw: RawJob) -> dict:
    """RawJob -> dict of `jobs` columns (everything that needs no DB and no model)."""
    description = strip_html(raw.description_html)
    truncated = len(description) > MAX_DESCRIPTION_CHARS
    description = description[:MAX_DESCRIPTION_CHARS]
    extra = raw.extra or {}
    comp_min, comp_max, comp_source = parse_comp(
        description, raw.comp_min, raw.comp_max, extra.get("comp_interval")
    )
    return {
        "source": raw.source,
        "source_job_id": str(raw.source_job_id),
        "company_name_raw": raw.company_name.strip(),
        "company_slug": slugify(raw.company_name),
        "title": collapse_whitespace(raw.title),
        "location_raw": raw.location,
        "remote_type": derive_remote_type(raw.title, raw.location, description, extra.get("workplace_type")),
        "employment_type": derive_employment_type(raw.title, description, extra.get("employment_hint")),
        "description_text": description,
        "url": raw.url,
        "apply_url": raw.apply_url or raw.url,
        "comp_min": comp_min,
        "comp_max": comp_max,
        "comp_source": comp_source,
        "posted_at": to_iso(raw.posted_at),
        "content_hash": content_hash(raw.title, raw.company_name, description),
        "description_truncated": bool(extra.get("description_truncated")) or truncated,
        "team": extra.get("team"),
        "sighting_only": bool(extra.get("sighting_only")),
        "listing_only": bool(extra.get("listing_only")),
    }
