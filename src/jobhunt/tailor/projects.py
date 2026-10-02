"""The resume's Projects section, written by code rather than by the model, so it is always there when it matters.

A project is a `kind: project` fact (title = the project's name, org = its link, text = what it is, tags = its
technologies and topics), with its accomplishments as `kind: achievement` facts whose parent_id is the project.

For each posting:
- a project is relevant when at least one of its tags appears in the posting (include: relevant), or always
  (include: always); the most relevant come first, up to `max_projects`;
- its accomplishments are ordered by how many of the posting's words they carry, up to `max_bullets`, each in the
  fact's own wording (the text, or the shortest variant within the bullet length limit);
- a "Technologies:" line lists the project's tags, the ones the posting asks for first, so its keywords are always
  on the page.

Every word comes from the fact bank, so nothing here can add an unsupported claim; the validator checks the section
against the facts all the same (a hand-edited draft goes through the same check).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from jobhunt.tailor import validate as v

TECH_LABEL = "Technologies"
_LINE_LABEL = re.compile(rf"^{TECH_LABEL}\s*:\s*", re.IGNORECASE)


def project_facts(facts: Sequence[Mapping]) -> list[Mapping]:
    return [f for f in facts if f.get("kind") == "project"]


def project_achievements(facts: Sequence[Mapping]) -> dict[str, list[Mapping]]:
    """Accomplishments per project id, in fact-bank order."""
    ids = {f["id"] for f in project_facts(facts)}
    out: dict[str, list[Mapping]] = {}
    for f in facts:
        if f.get("kind") == "achievement" and f.get("parent_id") in ids:
            out.setdefault(str(f["parent_id"]), []).append(f)
    return out


def _hits(terms: Sequence[str], posting_norm: str) -> list[str]:
    return [t for t in terms if v._norm(t) and v._word_in(v._norm(t), posting_norm)]


def _wording(fact: Mapping, max_chars: int | None) -> str | None:
    options = [str(fact.get("text") or "").strip(),
               *sorted((str(x).strip() for x in fact.get("variants") or []), key=len)]
    return next((t for t in options if t and (not max_chars or len(t) <= max_chars)), None)


def _line_tags(tags: Sequence[str]) -> list[str]:
    """The tags for the Technologies line, without a broad one another tag already says ("AI" next to "Agentic AI",
    "APIs" next to "REST APIs"). The broad tags still count for relevance."""
    tags = list(dict.fromkeys(str(t).strip() for t in tags if str(t).strip()))
    norms = [v._norm(t) for t in tags]
    return [t for t, n in zip(tags, norms, strict=True)
            if not any(o != n and v._word_in(n, o) for o in norms)]


def select(facts: Sequence[Mapping], posting: str, *, include: str = "relevant", max_projects: int = 3) -> list[dict]:
    """[{project, matched (its tags, or its accomplishments' tags, that the posting uses)}], most relevant first."""
    if include == "never":
        return []
    posting_norm = v._norm(posting)
    by_project = project_achievements(facts)
    scored = []
    for order, p in enumerate(project_facts(facts)):
        # the project's tags, then its accomplishments' tags (one spelling per term)
        terms: dict[str, str] = {}
        for t in [*(p.get("tags") or []), *(t for a in by_project.get(str(p["id"]), []) for t in a.get("tags") or [])]:
            terms.setdefault(v._norm(t), str(t).strip())
        matched = _hits(list(terms.values()), posting_norm)
        if matched or include == "always":
            scored.append((-len(matched), order, {"project": p, "matched": matched}))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [x[2] for x in scored[:max_projects or None]]


def section_lines(facts: Sequence[Mapping], posting: str, *, include: str = "relevant", max_projects: int = 3,
                  max_bullets: int = 2, max_bullet_chars: int | None = None) -> list[str]:
    """The markdown for '## Projects' (heading included), or [] when no project is relevant."""
    chosen = select(facts, posting, include=include, max_projects=max_projects)
    if not chosen:
        return []
    posting_norm = v._norm(posting)
    words = set(posting_norm.split())
    by_project = project_achievements(facts)
    lines = [f"## {v.SECTION_TITLES['projects']}"]
    for c in chosen:
        p = c["project"]
        heading = " | ".join(x for x in (str(p.get("title") or "").strip(), str(p.get("org") or "").strip()) if x)
        lines.append(f"### {heading}")
        achievements = by_project.get(str(p["id"]), [])
        ranked = sorted(enumerate(achievements), key=lambda ia: (
            -(len(_hits(list(ia[1].get("tags") or []), posting_norm))
              + len(words & set(v._norm(ia[1].get("text")).split())) / 50), ia[0]))
        bullets = [w for _, a in ranked if (w := _wording(a, max_bullet_chars))][:max_bullets or None]
        if not bullets and (text := str(p.get("text") or "").strip()):
            bullets = [text]  # a project with no accomplishments: its description is the bullet
        lines += [f"- {b}" for b in bullets]
        tags = _line_tags(p.get("tags") or [])
        if tags:
            # posting keywords first, including a tag that holds one ("AWS Textract" when the posting says AWS)
            hit = [v._norm(m) for m in c["matched"]]
            first = [t for t in tags if any(h == v._norm(t) or v._word_in(h, v._norm(t)) for h in hit)]
            lines.append(f"{TECH_LABEL}: " + ", ".join(first + [t for t in tags if t not in first]))
        lines.append("")
    return lines


def apply(markdown: str, facts: Sequence[Mapping], posting: str, *, sections: Sequence[str], include: str = "relevant",
          max_projects: int = 3, max_bullets: int = 2, max_bullet_chars: int | None = None,
          plain: bool = False) -> str:
    """Replace any Projects section in the draft with the generated one, placed where `sections` puts it."""
    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    # drop a Projects section the model (or an earlier pass) wrote
    out: list[str] = []
    skipping = False
    for line in lines:
        if line.startswith("## "):
            skipping = v._section_key(line[3:]) == "projects"
        if not skipping:
            out.append(line)
    if "projects" not in sections:
        return "\n".join(out)
    block = section_lines(facts, posting, include=include, max_projects=max_projects, max_bullets=max_bullets,
                          max_bullet_chars=max_bullet_chars)
    if not block:
        return "\n".join(out)
    if plain:
        block = [v.plain_characters(x) for x in block]
    later = set(list(sections)[list(sections).index("projects") + 1:])
    at = next((i for i, ln in enumerate(out) if ln.startswith("## ") and v._section_key(ln[3:]) in later), None)
    if at is None:
        while out and not out[-1].strip():
            out.pop()
        return "\n".join([*out, "", *block]).rstrip() + "\n"
    return "\n".join([*out[:at], *block, *out[at:]])


def errors(doc_lines: Sequence[str], facts: Sequence[Mapping]) -> list[dict]:
    """Check a Projects section (as parsed: the lines under '## Projects') against the project facts."""
    problems: list[dict] = []
    projects = {v._norm(p.get("title")): p for p in project_facts(facts)}
    by_project = project_achievements(facts)
    current: Mapping | None = None

    def err(problem: str, detail: str) -> None:
        problems.append({"bullet_index": -1, "bullet_text": "", "problem": problem, "detail": detail})

    for raw in doc_lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("### "):
            name = line[4:].split("|")[0]
            current = projects.get(v._norm(name))
            if current is None:
                err("unknown_project", f"'{name.strip()}' does not match any project fact")
            continue
        if current is None:
            err("stray_text", f"Projects may only contain '### project' headings, bullets and a "
                              f"'{TECH_LABEL}:' line: {line[:80]!r}")
            continue
        own = [current, *by_project.get(str(current["id"]), [])]
        if m := v._BULLET.match(line):
            said = v._norm(m.group("text"))
            allowed = {v._norm(x) for f in own for x in [f.get("text"), *(f.get("variants") or [])] if x}
            if said not in allowed:
                err("unsupported_project_bullet", f"{m.group('text')[:80]!r} is not the wording of a "
                                                  f"{current.get('title')} fact")
        elif _LINE_LABEL.match(line):
            corpora = v.fact_corpora(own)
            for term in (t.strip() for t in re.split(r"[,;]", _LINE_LABEL.sub("", line))):
                if term and not v.term_supported(term, corpora):
                    err("unsupported_skill", f"technology {term!r} is not in the {current.get('title')} facts")
        else:
            err("stray_text", f"Projects may only contain '### project' headings, bullets and a "
                              f"'{TECH_LABEL}:' line: {line[:80]!r}")
    return problems
