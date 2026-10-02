"""Mechanical repairs for a model's resume draft that failed validation (the common, fixable failures).

Most failed drafts fail for reasons with an exact, safe fix, and asking the model again costs a full draft:
- a bullet that added a posting keyword, a number or formatting its fact doesn't support, or ran too long, goes
  back to its fact's own wording (the fact's text, or its shortest variant that fits);
- roles out of date order are sorted newest first, bullets moving with their roles;
- a skills line with no label gets one;
- an accomplishment left out (resume_mode: complete) is added under its role in its fact's words;
- a summary sentence claiming a posting keyword the facts don't support is dropped (never the last sentence).

Every repair makes the draft more literal, never less, and the caller validates the result again: nothing here can
make an unsupported claim pass. Anything needing judgment (a claim in the summary, a bullet citing no fact, an
unknown role) is left for the model. Pure functions over markdown, the fact map and fact rows.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

from jobhunt.tailor import validate as v

BULLET_FIXABLE = {"unsupported_keyword", "unsupported_number", "bullet_too_long", "inline_formatting"}
# problems that mean the Experience section can't be safely rebuilt
STRUCTURE_BLOCKERS = {"orphan_fact_map_entry", "bullet_outside_role", "stray_text", "role_heading_format",
                      "missing_experience", "duplicate_section"}
_OMITTED = re.compile(r"fact '([^']+)'")
_SUMMARY_CLAIM = re.compile(r"the summary claims '([^']+)'")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def _drop_summary_claims(lines: list[str], terms: Sequence[str]) -> list[str]:
    """Drop each summary sentence that uses one of these terms, keeping at least one sentence. Returns the terms
    handled; `lines` is edited in place."""
    idx = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
    start = next((i for i in idx if v._section_key(lines[i][3:]) == "summary"), None)
    if start is None:
        return []
    end = next((i for i in idx if i > start), len(lines))
    body = " ".join(ln.strip() for ln in lines[start + 1:end] if ln.strip())
    sentences = [x for x in _SENTENCE.split(body) if x.strip()]
    keep = [x for x in sentences if not any(v._word_in(v._norm(t), v._norm(x)) for t in terms)]
    if not keep or len(keep) == len(sentences):
        return []
    lines[start + 1:end] = [" ".join(keep), ""]
    return [t for t in terms if not v._word_in(v._norm(t), v._norm(" ".join(keep)))]


def _fact_wording(fact: Mapping, max_chars: int | None) -> str | None:
    """The fact's own text, or its shortest variant, whichever fits the length limit."""
    options = [str(fact.get("text") or "").strip(), *sorted((str(x).strip() for x in fact.get("variants") or []),
                                                            key=len)]
    for text in options:
        if text and (not max_chars or len(text) <= max_chars):
            return text
    return None


def _role_sort_key(heading: str) -> tuple[int, int] | None:
    parts = [x.strip() for x in heading.split("|")]
    m = v._HEADING_DATES.match(parts[-1]) if len(parts) >= 3 else None
    if m is None:
        return None
    start = v._heading_date(m, "s") or (0, 0)
    end = (9999, 12) if m.group("present") else (v._heading_date(m, "e") or (0, 0))
    return (end[0] * 12 + (end[1] or 12), start[0] * 12 + (start[1] or 1))


def repair_resume(markdown: str, fact_map: Sequence[Mapping], facts_by_id: Mapping[str, Mapping],
                  candidate_ids: Iterable[str], errors: Sequence[Mapping], *,
                  max_bullet_chars: int | None = None, ats: Mapping | None = None) -> tuple[str, list[dict], list[str]]:
    """Returns (markdown, fact_map, notes). With nothing to repair it returns its inputs unchanged and no notes.
    `ats` (ATS Super Match) adds: plain characters, and trimming to the per-role bullet limits and the length cap
    (the model orders each role's bullets best first, so the last ones go)."""
    candidates = set(candidate_ids)
    unchanged = (markdown, list(fact_map), [])
    problems = {str(e.get("problem")) for e in errors}
    if not problems or problems & STRUCTURE_BLOCKERS:
        return unchanged

    notes: list[str] = []
    if "fancy_characters" in problems:
        markdown = v.plain_characters(markdown)
        notes.append("replaced curly quotes and long dashes with plain characters")
    lines = (markdown or "").replace("\r\n", "\n").split("\n")

    # ---- skills lines without a label
    if "skills_line_without_label" in problems:
        section = None
        used = set()
        for line in lines:
            if line.startswith("## "):
                section = v._section_key(line[3:])
            elif section == "skills" and ":" in line:
                used.add(line.split(":", 1)[0].strip().lower())
        section = None
        for i, line in enumerate(lines):
            if line.startswith("## "):
                section = v._section_key(line[3:])
            elif section == "skills" and line.strip() and ":" not in line:
                label = "Core Competencies" if "core competencies" not in used else "Additional Skills"
                used.add(label.lower())
                body = v._BULLET.sub(lambda m: m.group("text"), line).strip()
                lines[i] = f"{label}: {body}"
                notes.append(f"labelled a Skills line '{label}'")

    # ---- summary sentences claiming what the facts don't support
    claims = [m.group(1) for e in errors
              if e.get("problem") == "unsupported_keyword" and int(e.get("bullet_index", -1)) < 0
              and (m := _SUMMARY_CLAIM.search(str(e.get("detail") or "")))]
    if claims and (dropped := _drop_summary_claims(lines, claims)):
        notes.append(f"dropped the summary sentence claiming {', '.join(repr(t) for t in dropped)}")

    # ---- the Experience section as role blocks
    start = next((i for i, ln in enumerate(lines) if ln.startswith("## ") and v._section_key(ln[3:]) == "experience"),
                 None)
    if start is None:
        return unchanged
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    by_index: dict[int, list[dict]] = {}
    for entry in fact_map:
        by_index.setdefault(int(entry.get("bullet_index", -1)), []).append(dict(entry))
    blocks: list[dict] = []
    n = 0
    for line in lines[start + 1:end]:
        if line.startswith("### "):
            blocks.append({"heading": line[4:].strip(), "items": []})
        elif m := v._BULLET.match(line):
            if not blocks:
                return unchanged
            blocks[-1]["items"].append({"text": m.group("text"), "entries": by_index.get(n, [])})
            n += 1
        elif line.strip():
            return unchanged

    # ---- bullets that go back to their fact's own words
    bullet_problems: dict[int, set[str]] = {}
    for e in errors:
        if int(e.get("bullet_index", -1)) >= 0:
            bullet_problems.setdefault(int(e["bullet_index"]), set()).add(str(e.get("problem")))
    flat = [item for b in blocks for item in b["items"]]
    for idx, probs in bullet_problems.items():
        if idx >= len(flat) or not probs <= BULLET_FIXABLE:
            continue
        item = flat[idx]
        ids = [str(e.get("fact_id")) for e in item["entries"]]
        if len(ids) != 1 or ids[0] not in candidates or ids[0] not in facts_by_id:
            continue
        wording = _fact_wording(facts_by_id[ids[0]], max_bullet_chars)
        if wording is None or wording == item["text"]:
            continue
        item["text"] = wording
        notes.append(f"bullet for {ids[0]} put back in your fact's own words ({', '.join(sorted(probs))})")

    # ---- accomplishments left out
    for e in errors:
        if e.get("problem") != "omitted_fact" or not (m := _OMITTED.search(str(e.get("detail") or ""))):
            continue
        fid = m.group(1)
        fact = facts_by_id.get(fid) or {}
        role = facts_by_id.get(str(fact.get("parent_id") or "")) or {}
        wording = _fact_wording(fact, max_bullet_chars)
        org = v._norm(role.get("org"))
        home = next((b for b in blocks if len(parts := b["heading"].split("|")) >= 2 and v._norm(parts[1]) == org),
                    None)
        if fid not in candidates or wording is None or not org or home is None:
            continue
        home["items"].append({"text": wording, "entries": [{"bullet_index": -1, "fact_id": fid}]})
        notes.append(f"added the missing accomplishment {fid} under {role.get('org')}")

    # ---- ATS: bullets per role by recency, then the length cap
    if ats and problems & {"too_many_bullets_for_role", "too_long"}:
        limits = list(ats.get("role_bullet_limits") or [])
        dropped = 0
        if limits:
            for i, b in enumerate(blocks):
                cap = limits[min(i, len(limits) - 1)]
                if len(b["items"]) > cap:
                    dropped += len(b["items"]) - cap
                    b["items"] = b["items"][:cap]
        cap_words = int(ats.get("max_words") or 0)

        def words() -> int:
            body = "\n".join([*lines[:start + 1], *(f"### {b['heading']}" for b in blocks),
                               *(f"- {it['text']}" for b in blocks for it in b["items"]), *lines[end:]])
            return v.word_count(body)

        while cap_words and words() > cap_words:
            # the oldest role that still has more than one bullet gives up its last one
            victim = next((b for b in reversed(blocks) if len(b["items"]) > 1), None)
            if victim is None:
                break
            victim["items"].pop()
            dropped += 1
        if dropped:
            notes.append(f"trimmed {dropped} lower-priority bullets to fit the per-role limits and the page limit")

    # ---- roles newest first
    if "not_reverse_chronological" in problems:
        keys = [_role_sort_key(b["heading"]) for b in blocks]
        if all(k is not None for k in keys):
            order = sorted(range(len(blocks)), key=lambda i: keys[i], reverse=True)
            if order != list(range(len(blocks))):
                blocks = [blocks[i] for i in order]
                notes.append("put the roles in date order, most recent first")

    if not notes:
        return unchanged

    # ---- rebuild the section and renumber the fact map
    section: list[str] = []
    new_map: list[dict] = []
    i = 0
    for b in blocks:
        section.append(f"### {b['heading']}")
        for item in b["items"]:
            section.append(f"- {item['text']}")
            new_map.extend({**entry, "bullet_index": i} for entry in item["entries"])
            i += 1
        section.append("")
    rebuilt = lines[:start + 1] + section + lines[end:]
    return "\n".join(rebuilt), new_map, notes
