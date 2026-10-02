"""Draft validation (SPEC 7.3 + 7.6). Enforced in code for model output and hand edits alike.

Resume:
  * every Experience bullet cites exactly one fact from this thread's candidate set;
  * every number, percentage, currency amount, multiplier and date range in a bullet appears in that fact's
    text or metrics (years may also come from the fact's own dates);
  * role/education headings, certifications and skills must be traceable to candidate facts;
  * ATS-compliance: standard section headings in configured order, plain single-column text, no tables,
    images, HTML, links, inline formatting or special symbols, consistent `Title | Org | Mon YYYY – Present`
    role headings in reverse-chronological order, bullet length/count limits.

Errors are `[{bullet_index, bullet_text, problem, detail}]`; bullet_index is -1 for document-level problems.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

# --------------------------------------------------------------------------------------------- numeric tokens

_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_MULT_WORDS = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
_MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_TOKEN_RE = re.compile(
    rf"""
    (?P<daterange>(?:{_MONTHS}\s+)?(?P<y1>(?:19|20)\d{{2}})\s*(?:-|–|—|to)\s*
        (?:(?:{_MONTHS}\s+)?(?P<y2>(?:19|20)\d{{2}})|(?:present|current|now|today))(?![\d]))
  | (?P<currency>(?:\$|USD\s?|US\$)\s?(?P<cur_num>{_NUM})\s*(?P<cur_mult>k|m|mm|b|bn|thousand|million|billion)?\b
        |(?<![\w.])(?P<cur_num2>{_NUM})\s*(?P<cur_mult2>k|m|mm|b|bn|thousand|million|billion)?\s*(?:USD|dollars)\b)
  | (?P<percent>(?<![\w.])(?P<pct_num>{_NUM})\s*(?:%|percent\b|pct\b))
  | (?P<multiplier>(?<![\w.])(?P<mul_num>{_NUM})\s*(?:x|×)(?![\w]))
  | (?P<number>(?<![\w.])(?P<plain>{_NUM})(?:\s*(?P<plain_mult>k|m|mm|b|bn|thousand|million|billion)\b)?\+?(?![\w]))
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class NumToken:
    raw: str
    kind: str  # number | percent | currency | multiplier
    value: float


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _mult(word: str | None) -> float:
    return _MULT_WORDS.get((word or "").lower(), 1.0)


def extract_numeric_tokens(text: str) -> list[NumToken]:
    out: list[NumToken] = []
    for m in _TOKEN_RE.finditer(text or ""):
        raw = m.group(0).strip()
        if m.group("daterange"):
            for y in (m.group("y1"), m.group("y2")):
                if y:
                    out.append(NumToken(raw, "number", float(y)))
        elif m.group("currency"):
            if m.group("cur_num") is not None:
                out.append(NumToken(raw, "currency", _num(m.group("cur_num")) * _mult(m.group("cur_mult"))))
            else:
                out.append(NumToken(raw, "currency", _num(m.group("cur_num2")) * _mult(m.group("cur_mult2"))))
        elif m.group("percent"):
            out.append(NumToken(raw, "percent", _num(m.group("pct_num"))))
        elif m.group("multiplier"):
            out.append(NumToken(raw, "multiplier", _num(m.group("mul_num"))))
        elif m.group("number"):
            out.append(NumToken(raw, "number", _num(m.group("plain")) * _mult(m.group("plain_mult"))))
    return out


def _metric_tokens(metrics: object) -> list[NumToken]:
    out: list[NumToken] = []
    items: Iterable[tuple[str, object]]
    if isinstance(metrics, Mapping):
        items = metrics.items()
    elif isinstance(metrics, list | tuple):
        items = (("", x) for x in metrics)
    else:
        return out
    for key, val in items:
        k = str(key).lower()
        if isinstance(val, bool):
            continue
        if isinstance(val, int | float):
            kind = "number"
            if k.endswith(("_pct", "_percent", "percent", "_pc")):
                kind = "percent"
            elif k.endswith(("_usd", "_dollars", "usd")):
                kind = "currency"
            elif k.endswith(("_x", "_multiplier")):
                kind = "multiplier"
            out.append(NumToken(str(val), kind, float(val)))
        elif isinstance(val, str):
            out.extend(extract_numeric_tokens(val))
    return out


def fact_tokens(fact: Mapping, *, include_dates: bool = True) -> list[NumToken]:
    toks = extract_numeric_tokens(fact.get("text") or "") + _metric_tokens(fact.get("metrics"))
    if include_dates:
        for d in (fact.get("start_date"), fact.get("end_date")):
            if d and (m := re.match(r"((?:19|20)\d{2})", str(d))):
                toks.append(NumToken(str(d), "number", float(m.group(1))))
    return toks


def _supported(token: NumToken, pool: Sequence[NumToken]) -> bool:
    for p in pool:
        if abs(p.value - token.value) > 1e-9 * max(1.0, abs(token.value)):
            continue
        if token.kind == "number" or token.kind == p.kind:
            return True
    return False


def unsupported_tokens(text: str, pool: Sequence[NumToken]) -> list[NumToken]:
    return [t for t in extract_numeric_tokens(text) if not _supported(t, pool)]


# --------------------------------------------------------------------------------------------- resume parsing

SECTION_TITLES = {
    "summary": "Summary",
    "skills": "Skills",
    "experience": "Experience",
    "projects": "Projects",
    "education": "Education",
    "certifications": "Certifications",
}
_BULLET = re.compile(r"^\s*[-*]\s+(?P<text>.*\S)\s*$")
_ANNOT = re.compile(r"\s*\{fact:(?P<ids>[A-Za-z0-9_.,\- ]+)\}\s*$")
_DATE = rf"(?:(?P<{{p}}m>{_MONTHS})\s+)?(?P<{{p}}y>(?:19|20)\d{{{{2}}}})"
_HEADING_DATES = re.compile(
    r"^\s*" + _DATE.format(p="s") + r"\s*(?:–|—|-|to)\s*(?:" + _DATE.format(p="e")
    + r"|(?P<present>Present|Current))\s*$",
    re.IGNORECASE,
)
_YEAR_ONLY = re.compile(r"^\s*(?:19|20)\d{2}\s*$")
_MONTH_NUM = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                          "dec"], start=1)}


@dataclass
class Bullet:
    index: int
    text: str
    role_heading: str | None


@dataclass
class ParsedResume:
    sections: list[tuple[str, int]] = field(default_factory=list)  # (heading text, line number)
    section_lines: dict[str, list[str]] = field(default_factory=dict)
    bullets: list[Bullet] = field(default_factory=list)
    role_headings: list[str] = field(default_factory=list)
    education_headings: list[str] = field(default_factory=list)
    bullets_per_role: dict[str, int] = field(default_factory=dict)
    stray_experience_lines: list[str] = field(default_factory=list)
    has_h1: bool = False


def _section_key(heading: str) -> str | None:
    h = heading.strip().lower()
    for key, title in SECTION_TITLES.items():
        if h == title.lower():
            return key
    return None


def parse_resume(markdown: str) -> ParsedResume:
    p = ParsedResume()
    current: str | None = None
    role: str | None = None
    for i, line in enumerate((markdown or "").replace("\r\n", "\n").split("\n")):
        if line.startswith("# "):
            p.has_h1 = True
            continue
        if line.startswith("## "):
            heading = line[3:].strip()
            p.sections.append((heading, i))
            current = _section_key(heading) or f"?{heading}"
            p.section_lines.setdefault(current, [])
            role = None
            continue
        if current is None:
            continue
        p.section_lines[current].append(line)
        if current == "experience":
            if line.startswith("### "):
                role = line[4:].strip()
                p.role_headings.append(role)
                p.bullets_per_role.setdefault(role, 0)
            elif m := _BULLET.match(line):
                p.bullets.append(Bullet(len(p.bullets), m.group("text"), role))
                if role is not None:
                    p.bullets_per_role[role] += 1
            elif line.strip():
                p.stray_experience_lines.append(line.strip())
        elif current == "education" and line.startswith("### "):
            p.education_headings.append(line[4:].strip())
    return p


def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


_SKILL_STOPWORDS = {"and", "or", "of", "the", "a", "an", "in", "on", "for", "with", "to", "using", "based", "&"}


def fact_corpora(facts: Iterable[Mapping]) -> list[str]:
    """One normalized text per fact (text, tags, variants, title, org) for skill/keyword support checks."""
    return [_norm(" ".join([f.get("text") or "", " ".join(f.get("tags") or []), " ".join(f.get("variants") or []),
                            f.get("title") or "", f.get("org") or ""])) for f in facts]


def _word_in(word: str, corpus: str) -> bool:
    variants = {word, word.rstrip("s"), word + "s", word + "es"}
    return any(re.search(rf"(?<![a-z0-9]){re.escape(x)}(?![a-z0-9])", corpus) for x in variants if x)


def term_supported(term: str, corpora: Sequence[str]) -> bool:
    """A skill/keyword is supported when the whole phrase appears in some fact, or (for phrases of up to four
    words) every meaningful word appears in the same single fact, e.g. "API development" backed by a fact about
    developing REST APIs. Words spread across different facts don't count."""
    nt = _norm(term)
    if not nt:
        return False
    if any(_word_in(nt, c) for c in corpora):
        return True
    words = [w for w in nt.split() if w not in _SKILL_STOPWORDS and len(w) > 1]
    if not words or len(words) > 4:
        return False
    return any(all(_word_in(w, c) for w in words) for c in corpora)


def keyword_support(requirements: Sequence[Mapping], facts: Iterable[Mapping]) -> tuple[list[dict], list[dict]]:
    """Split the posting's keywords into ones the candidate's facts support and ones they don't (no LLM)."""
    corpora = fact_corpora(facts)
    supported: dict[str, dict] = {}
    unsupported: dict[str, dict] = {}
    for req in requirements:
        for kw in req.get("keywords") or []:
            key = _norm(kw)
            if not key or key in supported or key in unsupported:
                continue
            entry = {"keyword": kw, "kind": req.get("kind") or "nice"}
            (supported if term_supported(kw, corpora) else unsupported)[key] = entry
    order = {"must": 0, "nice": 1}
    return (sorted(supported.values(), key=lambda e: order.get(e["kind"], 2)),
            sorted(unsupported.values(), key=lambda e: order.get(e["kind"], 2)))


def _parse_fact_date(value: str | None) -> tuple[int, int | None] | None:
    if not value or str(value).lower() in ("present", "current", "now"):
        return None
    m = re.match(r"((?:19|20)\d{2})(?:-(\d{1,2}))?", str(value))
    return (int(m.group(1)), int(m.group(2)) if m.group(2) else None) if m else None


def _heading_date(m: re.Match, prefix: str) -> tuple[int, int | None] | None:
    y = m.group(f"{prefix}y")
    if not y:
        return None
    mon = m.group(f"{prefix}m")
    return int(y), (_MONTH_NUM.get(mon[:3].lower()) if mon else None)


def _dates_match(heading: tuple[int, int | None] | None, fact: tuple[int, int | None] | None) -> bool:
    if heading is None or fact is None:
        return heading is None and fact is None
    if heading[0] != fact[0]:
        return False
    return heading[1] is None or fact[1] is None or heading[1] == fact[1]


# --------------------------------------------------------------------------------------------- ATS rules

_SPECIAL_BULLETS = set("•·▪▫●○◦■□◆◇►▶➤➢✓✔✗✘★☆→⇒♦")


def _special_chars(text: str) -> list[str]:
    bad = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in _SPECIAL_BULLETS or cat in ("So", "Sk", "Co", "Cs") or ch in ("️", "‍"):
            bad.append(ch)
    return sorted(set(bad))


def _doc_error(problem: str, detail: str) -> dict:
    return {"bullet_index": -1, "bullet_text": "", "problem": problem, "detail": detail}


def ats_errors_common(markdown: str) -> list[dict]:
    errors: list[dict] = []
    text = markdown or ""
    if re.search(r"^\s*\|.*\|\s*$", text, re.MULTILINE) or re.search(r"^\s*\|?\s*:?-{3,}:?\s*\|", text, re.MULTILINE):
        errors.append(_doc_error("table", "tables are not parsed reliably by applicant tracking systems"))
    if "![" in text:
        errors.append(_doc_error("image", "images are invisible to applicant tracking systems"))
    if re.search(r"<[A-Za-z/!][^>]*>", text):
        errors.append(_doc_error("html", "HTML tags are not allowed; use plain text"))
    if "```" in text:
        errors.append(_doc_error("code_block", "code blocks are not allowed"))
    if re.search(r"\[[^\]]+\]\([^)]+\)", text):
        errors.append(_doc_error("link", "markdown links render as raw syntax; write plain URLs"))
    if bad := _special_chars(text):
        errors.append(_doc_error("special_character",
                                 f"symbols/emoji confuse ATS parsers: {' '.join(bad)} (use plain '-' bullets)"))
    return errors


# --------------------------------------------------------------------------------------------- resume validation


def validate_resume(
    markdown: str,
    fact_map: Sequence[Mapping],
    facts_by_id: Mapping[str, Mapping],
    candidate_ids: Iterable[str],
    *,
    max_bullet_chars: int | None = None,
    max_bullets_per_role: int | None = None,
    sections: Sequence[str] | None = None,
    require_fact_ids: Iterable[str] | None = None,
    unsupported_terms: Sequence[str] = (),
    ats: Mapping | None = None,
    project_facts: Sequence[Mapping] | None = None,
) -> list[dict]:
    """`ats` (ATS Super Match): {role_bullet_limits, max_words, weak_openers} adds the ATS rules. `project_facts`: the
    project facts and their accomplishments a Projects section is checked against (default: those in facts_by_id)."""
    candidates = set(candidate_ids)
    doc = parse_resume(markdown)
    errors: list[dict] = ats_errors_common(markdown)

    # --- structure / ATS
    if doc.has_h1:
        errors.append(_doc_error("contains_name_header",
                                 "do not include a '# Name' header; name and contact details are added at render"))
    allowed = [s for s in (sections or SECTION_TITLES.keys()) if s in SECTION_TITLES]
    seen_keys: list[str] = []
    for heading, _line in doc.sections:
        key = _section_key(heading)
        if key is None or key not in allowed:
            errors.append(_doc_error("nonstandard_section_heading",
                                     f"'## {heading}' — use only: {', '.join(SECTION_TITLES[s] for s in allowed)}"))
            continue
        if key in seen_keys:
            errors.append(_doc_error("duplicate_section", f"'## {heading}' appears more than once"))
        seen_keys.append(key)
    order = [allowed.index(k) for k in seen_keys if k in allowed]
    if order != sorted(order):
        errors.append(_doc_error("section_order", f"sections must appear in this order: "
                                                  f"{', '.join(SECTION_TITLES[s] for s in allowed)}"))
    if "experience" not in seen_keys or not doc.bullets:
        errors.append(_doc_error("missing_experience",
                                 "an '## Experience' section with at least one bullet is required"))
    for line in doc.stray_experience_lines:
        errors.append(_doc_error("stray_text", f"Experience may only contain '### role' headings and '- ' bullets: "
                                               f"{line[:80]!r}"))

    # --- the Projects section: every word from the project facts
    if "projects" in doc.section_lines:
        from jobhunt.tailor import projects as _projects

        errors += _projects.errors(doc.section_lines["projects"],
                                   list(project_facts if project_facts is not None else facts_by_id.values()))

    # --- role headings: format, traceability, order
    role_facts = [facts_by_id[i] for i in candidates if i in facts_by_id and facts_by_id[i].get("kind") == "role"]
    ends: list[tuple[int, int]] = []
    for heading in doc.role_headings:
        parts = [x.strip() for x in heading.split("|")]
        dm = _HEADING_DATES.match(parts[-1]) if len(parts) >= 3 else None
        if dm is None:
            errors.append(_doc_error("role_heading_format",
                                     f"'### {heading}' — use '### Title | Organization | Mon YYYY – Mon YYYY|Present'"))
            continue
        start = _heading_date(dm, "s")
        end = None if dm.group("present") else _heading_date(dm, "e")
        ends.append((end or (9999, 12))[0] * 12 + ((end or (9999, 12))[1] or 12))
        org = _norm(parts[1])
        matches = [f for f in role_facts if _norm(f.get("org")) == org]
        if not matches:
            errors.append(_doc_error("unknown_role", f"'{parts[1]}' does not match any role fact in the candidate set"))
            continue
        if not any(_dates_match(start, _parse_fact_date(f.get("start_date")))
                   and _dates_match(end, _parse_fact_date(f.get("end_date"))) for f in matches):
            errors.append(_doc_error("role_dates_mismatch",
                                     f"'{parts[-1]}' for {parts[1]} does not match the role fact's dates"))
    if ends != sorted(ends, reverse=True):
        errors.append(_doc_error("not_reverse_chronological", "list roles most recent first"))

    # --- education / certifications / skills traceability
    edu_facts = [facts_by_id[i] for i in candidates if i in facts_by_id
                 and facts_by_id[i].get("kind") in ("education", "cert")]
    for heading in doc.education_headings:
        parts = [x.strip() for x in heading.split("|")]
        if len(parts) < 2:
            errors.append(_doc_error("education_heading_format",
                                     f"'### {heading}' — use '### Degree | Institution | YYYY'"))
            continue
        inst = _norm(parts[1])
        found = [f for f in edu_facts if _norm(f.get("org")) == inst]
        if not found:
            errors.append(_doc_error("unknown_education", f"'{parts[1]}' does not match any education fact"))
            continue
        if len(parts) >= 3 and parts[2]:
            pool = [t for f in found for t in fact_tokens(f)]
            for tok in unsupported_tokens(parts[2], pool):
                errors.append(_doc_error("unsupported_number", f"education year {tok.raw} not in the education fact"))
    cert_facts = [f for f in edu_facts if f.get("kind") == "cert"]
    for line in doc.section_lines.get("certifications", []):
        m = _BULLET.match(line) or (re.match(r"^\s*(?P<text>[^#\s].*\S)\s*$", line) if line.strip() else None)
        if not m:
            continue
        text = _norm(m.group("text"))
        if not any(_norm(f.get("title") or f.get("text")) and _norm(f.get("title") or f.get("text")) in text
                   for f in cert_facts):
            errors.append(_doc_error("unknown_certification", f"{m.group('text')!r} does not match a cert fact"))
    corpora = fact_corpora(f for i in candidates if (f := facts_by_id.get(i)))
    skill_lines = [ln for ln in doc.section_lines.get("skills", []) if ln.strip()]
    for line in skill_lines:
        label, sep, _ = line.partition(":")
        # One plain line of skills is fine; once the section is grouped, every line needs its own label.
        if len(skill_lines) > 1 and (not sep or not label.strip() or len(label) > 40 or "," in label):
            errors.append(_doc_error("skills_line_without_label",
                                     f"{line[:60]!r} — every Skills line starts with a short group label and a "
                                     "colon, e.g. 'Integration & APIs: …'"))
        body = line.split(":", 1)[1] if ":" in line else line
        body = _BULLET.sub(lambda mm: mm.group("text"), body)
        for skill in (s.strip() for s in re.split(r"[,;]", body)):
            if skill and _norm(skill) and not term_supported(skill, corpora):
                errors.append(_doc_error("unsupported_skill", f"skill {skill!r} does not appear in any candidate fact"))

    # --- posting keywords the candidate's facts don't support must not be claimed in the summary
    summary_norm = _norm("\n".join(doc.section_lines.get("summary", [])))
    for term in unsupported_terms:
        if _norm(term) and _word_in(_norm(term), summary_norm):
            errors.append(_doc_error("unsupported_keyword",
                                     f"the summary claims {term!r}, which none of your facts support"))

    # --- summary numbers
    summary_text = "\n".join(doc.section_lines.get("summary", []))
    pool = [t for i in candidates if i in facts_by_id for t in fact_tokens(facts_by_id[i])]
    for tok in unsupported_tokens(summary_text, pool):
        errors.append(_doc_error("unsupported_number_in_summary",
                                 f"{tok.raw} in the summary does not appear in any candidate fact"))

    # --- bullets: limits, formatting, fact references, numbers
    for role, n in doc.bullets_per_role.items():
        if max_bullets_per_role and n > max_bullets_per_role:
            errors.append(_doc_error("too_many_bullets", f"{n} bullets under '{role}' (max {max_bullets_per_role})"))
    by_index: dict[int, list[str]] = {}
    for entry in fact_map:
        idx = int(entry.get("bullet_index", -1))
        by_index.setdefault(idx, []).append(str(entry.get("fact_id")))
    for idx in sorted(by_index):
        if idx < 0 or idx >= len(doc.bullets):
            errors.append({"bullet_index": idx, "bullet_text": "", "problem": "orphan_fact_map_entry",
                           "detail": f"fact_map references bullet {idx}, but there are {len(doc.bullets)} bullets"})
    for b in doc.bullets:
        def err(problem: str, detail: str, b: Bullet = b) -> None:
            errors.append({"bullet_index": b.index, "bullet_text": b.text, "problem": problem, "detail": detail})

        if b.role_heading is None:
            err("bullet_outside_role", "every Experience bullet must sit under a '### role' heading")
        if max_bullet_chars and len(b.text) > max_bullet_chars:
            err("bullet_too_long", f"{len(b.text)} characters (max {max_bullet_chars})")
        if "**" in b.text or "__" in b.text or re.search(r"(?<![\w*])\*[^*\s][^*]*\*(?![\w*])", b.text):
            err("inline_formatting", "bold/italic markup is not allowed in bullets")
        ids = by_index.get(b.index, [])
        if not ids:
            err("missing_fact_ref", "bullet has no fact reference")
            continue
        if len(ids) > 1:
            err("multiple_fact_refs", f"bullet cites {len(ids)} facts; cite exactly one")
        fid = ids[0]
        fact = facts_by_id.get(fid)
        if fact is None:
            err("unknown_fact", f"fact {fid!r} does not exist")
            continue
        if fid not in candidates:
            err("fact_not_in_candidates", f"fact {fid!r} was not retrieved for this job")
            continue
        for tok in unsupported_tokens(b.text, fact_tokens(fact)):
            err("unsupported_number", f"{tok.raw} does not appear in fact {fid!r}")
        if unsupported_terms:
            bullet_norm, own = _norm(b.text), fact_corpora([fact])
            for term in unsupported_terms:
                if _norm(term) and _word_in(_norm(term), bullet_norm) and not term_supported(term, own):
                    err("unsupported_keyword", f"{term!r} is not in fact {fid!r}; don't add posting keywords a fact "
                                               "doesn't support")

    # --- ATS Super Match rules
    if ats:
        errors += ats_errors(markdown, doc, ats)

    # --- completeness (resume_mode: complete): every accomplishment appears once
    if require_fact_ids is not None:
        cited = [str(e.get("fact_id")) for e in fact_map]
        for fid in require_fact_ids:
            if fid not in cited:
                fact = facts_by_id.get(fid) or {}
                errors.append(_doc_error("omitted_fact", f"fact {fid!r} ({str(fact.get('text') or '')[:80]}…) is "
                                                         "missing: include every accomplishment as a bullet"))
        for fid in sorted({f for f in cited if cited.count(f) > 1}):
            errors.append(_doc_error("repeated_fact", f"fact {fid!r} is used for more than one bullet"))
    return errors


# --------------------------------------------------------------------------------------------- keyword coverage


_FANCY = re.compile("[\u2018\u2019\u201c\u201d\u2013\u2014\u2026\u00a0]")


def word_count(markdown: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][\w'./&+#-]*", markdown or ""))


def ats_errors(markdown: str, doc: ParsedResume, ats: Mapping) -> list[dict]:
    """The ATS Super Match rules: bullets per role by recency, total length, strong openers, plain characters."""
    errors: list[dict] = []
    limits = list(ats.get("role_bullet_limits") or [])
    if limits:
        for i, heading in enumerate(doc.role_headings):
            cap = limits[min(i, len(limits) - 1)]
            n = doc.bullets_per_role.get(heading, 0)
            if n > cap:
                errors.append(_doc_error("too_many_bullets_for_role",
                                         f"{n} bullets under '{heading}' (role {i + 1} by date: at most {cap})"))
    words, cap_words = word_count(markdown), int(ats.get("max_words") or 0)
    if cap_words and words > cap_words:
        errors.append(_doc_error("too_long", f"{words} words (about {words / 500:.1f} pages); keep it under "
                                             f"{cap_words} words, about {cap_words / 500:.0f} pages"))
    weak = {w.lower() for w in ats.get("weak_openers") or []}
    for b in doc.bullets:
        first = (re.findall(r"[A-Za-z]+", b.text)[:1] or [""])[0].lower()
        if first in weak:
            errors.append({"bullet_index": b.index, "bullet_text": b.text, "problem": "weak_opener",
                           "detail": f"starts with {first!r}; open with a strong, accurate action verb (e.g. Led, "
                                     "Delivered, Partnered with, Designed)"})
    if _FANCY.search(markdown or ""):
        errors.append(_doc_error("fancy_characters", "curly quotes, long dashes or ellipses: use plain ' \" - ..."))
    return errors


def plain_characters(text: str) -> str:
    return (text.replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
            .replace("\u2013", "-").replace("\u2014", "-").replace("\u2026", "...").replace("\u00a0", " "))


def keyword_coverage(markdown: str, requirements: Sequence[Mapping]) -> dict:
    text = _norm(markdown)
    matched: set[str] = set()
    missing_must: set[str] = set()
    missing_nice: set[str] = set()
    total: set[str] = set()
    for req in requirements:
        for kw in req.get("keywords") or []:
            k = _norm(kw)
            if not k:
                continue
            total.add(kw.lower())
            variants = {k, k.rstrip("s"), k + "s"}
            if any(re.search(rf"(?<![a-z0-9]){re.escape(x)}(?![a-z0-9])", text) for x in variants if x):
                matched.add(kw.lower())
            elif req.get("kind") == "must":
                missing_must.add(kw.lower())
            else:
                missing_nice.add(kw.lower())
    missing_must -= matched
    missing_nice -= matched | missing_must
    return {
        "matched": sorted(matched),
        "missing_must": sorted(missing_must),
        "missing_nice": sorted(missing_nice),
        "percent": round(100.0 * len(matched) / len(total), 1) if total else 100.0,
    }


# --------------------------------------------------------------------------------------------- cover letter

_GREETING = re.compile(r"^\s*(dear|hello|to whom)\b", re.IGNORECASE)
_SIGNOFF = re.compile(r"^\s*(sincerely|best|regards|kind regards|warm regards|thank you|respectfully)\b[^\n]{0,20}$",
                      re.IGNORECASE)
_CLAIM = re.compile(
    r"\bI\s+(?:have\s+|had\s+)?(?:\w+ed|built|led|ran|grew|drove|made|wrote|took|won|cut|set|brought|held|oversaw|"
    r"taught|began|chose|spent|delivered|own|owned)\b|\bmy\s+(?:experience|work|team|role|background|career|tenure)\b",
    re.IGNORECASE,
)


def _cover_paragraphs(markdown: str) -> list[tuple[str, int | None]]:
    paras = (markdown or "").replace("\r\n", "\n").split("\n\n")
    out: list[tuple[str, int | None]] = []
    body = 0
    for p in paras:
        if not p.strip() or _GREETING.match(p) or _SIGNOFF.match(p.strip()):
            out.append((p, None))
        else:
            out.append((p, body))
            body += 1
    return out


def validate_cover(markdown: str, fact_map: Sequence[Mapping], facts_by_id: Mapping[str, Mapping],
                   candidate_ids: Iterable[str], company_notes: str | None) -> list[dict]:
    candidates = set(candidate_ids)
    errors = ats_errors_common(markdown)
    if re.search(r"^\s*#", markdown or "", re.MULTILINE):
        errors.append(_doc_error("heading", "cover letters are plain paragraphs; no headings"))
    if re.search(r"^\s*[-*]\s+", markdown or "", re.MULTILINE):
        errors.append(_doc_error("list", "cover letters are plain paragraphs; no lists"))
    paragraphs = [(text, idx) for text, idx in _cover_paragraphs(markdown) if idx is not None]
    refs: dict[int, list[str]] = {}
    for entry in fact_map:
        refs.setdefault(int(entry.get("paragraph_index", -1)), []).append(str(entry.get("fact_id")))
    for idx in refs:
        if idx < 0 or idx >= len(paragraphs):
            errors.append({"bullet_index": idx, "bullet_text": "", "problem": "orphan_fact_map_entry",
                           "detail": f"fact_map references paragraph {idx}, but there are {len(paragraphs)}"})
    notes_pool = extract_numeric_tokens(company_notes or "")
    for text, idx in paragraphs:
        def err(problem: str, detail: str, idx: int = idx, text: str = text) -> None:
            errors.append({"bullet_index": idx, "bullet_text": text, "problem": problem, "detail": detail})

        tokens = extract_numeric_tokens(text)
        ids = refs.get(idx, [])
        pool: list[NumToken] = list(notes_pool)
        for fid in ids:
            fact = facts_by_id.get(fid)
            if fact is None:
                err("unknown_fact", f"fact {fid!r} does not exist")
            elif fid not in candidates:
                err("fact_not_in_candidates", f"fact {fid!r} was not retrieved for this job")
            else:
                pool.extend(fact_tokens(fact))
        if _CLAIM.search(text) and not ids:
            err("missing_fact_ref", "paragraph makes claims about your experience but cites no fact")
        for tok in tokens:
            if not _supported(tok, pool):
                err("unsupported_number", f"{tok.raw} does not appear in the cited facts or company notes")
    return errors


# --------------------------------------------------------------------------------------------- hand-edit annotations


def annotate_resume(markdown: str, fact_map: Sequence[Mapping]) -> str:
    ids: dict[int, list[str]] = {}
    for e in fact_map:
        ids.setdefault(int(e["bullet_index"]), []).append(str(e["fact_id"]))
    lines, out, section, n = (markdown or "").split("\n"), [], None, 0
    for line in lines:
        if line.startswith("## "):
            section = _section_key(line[3:])
        elif section == "experience" and _BULLET.match(line):
            if n in ids:
                line = f"{line.rstrip()} {{fact:{','.join(ids[n])}}}"
            n += 1
        out.append(line)
    return "\n".join(out)


def split_resume_annotations(markdown: str) -> tuple[str, list[dict]]:
    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    out, fact_map, section, n = [], [], None, 0
    for line in lines:
        if line.startswith("## "):
            section = _section_key(line[3:])
        elif section == "experience" and _BULLET.match(line):
            if m := _ANNOT.search(line):
                for fid in (x.strip() for x in m.group("ids").split(",")):
                    if fid:
                        fact_map.append({"bullet_index": n, "fact_id": fid})
                line = line[: m.start()].rstrip()
            n += 1
        out.append(line)
    return "\n".join(out), fact_map


def annotate_cover(markdown: str, fact_map: Sequence[Mapping]) -> str:
    ids: dict[int, list[str]] = {}
    for e in fact_map:
        ids.setdefault(int(e["paragraph_index"]), []).append(str(e["fact_id"]))
    out = []
    for text, idx in _cover_paragraphs(markdown):
        out.append(f"{text.rstrip()} {{fact:{','.join(ids[idx])}}}" if idx is not None and idx in ids else text)
    return "\n\n".join(out)


def split_cover_annotations(markdown: str) -> tuple[str, list[dict]]:
    out, fact_map = [], []
    stripped = []
    for p in (markdown or "").replace("\r\n", "\n").split("\n\n"):
        m = _ANNOT.search(p)
        stripped.append((p[: m.start()].rstrip() if m else p, m.group("ids") if m else None))
    joined = "\n\n".join(p for p, _ in stripped)
    body_index = {i: idx for i, (_, idx) in enumerate(_cover_paragraphs(joined))}
    for i, (p, ids) in enumerate(stripped):
        out.append(p)
        if ids and body_index.get(i) is not None:
            for fid in (x.strip() for x in ids.split(",")):
                if fid:
                    fact_map.append({"paragraph_index": body_index[i], "fact_id": fid})
    return "\n\n".join(out), fact_map
