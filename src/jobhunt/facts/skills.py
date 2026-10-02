"""The skills named in the fact bank, for the owner to rate 1-10 (Fact bank → Skill levels).

Skill facts give the main list, grouped by their own heading ("Cloud and Infrastructure: AWS (EC2, ...)"). Tags on
roles and achievements that no skill fact lists follow as "From your experience". Pure functions over fact rows.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence

EXPERIENCE = "From your experience"
CUSTOM = "Added by you"

# How the scale reads, shown on the page and to the scoring model.
LEVEL_BANDS = ((1, 3, "basic"), (4, 6, "working"), (7, 8, "advanced"), (9, 10, "expert"))


def band(level: int) -> str:
    return next(name for lo, hi, name in LEVEL_BANDS if lo <= level <= hi)


def key(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().lower()


def _label(tag: str, texts: Sequence[str]) -> str:
    """The tag as the facts spell it ("aws" -> "AWS", "mulesoft" -> "MuleSoft"), else as tagged."""
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(tag)}(?![A-Za-z0-9])", re.IGNORECASE)
    found = [m.group(0) for t in texts for m in pattern.finditer(t)]
    # the capitalised spelling where there is one ("Microservices" over a lowercase mention in running text)
    return next((f for f in found if any(ch.isupper() for ch in f)), found[0] if found else tag)


def _category(fact: Mapping) -> str:
    head, sep, _ = str(fact.get("text") or "").partition(":")
    return head.strip() if sep and len(head) <= 40 else "Core competencies"


def catalog(facts: Sequence[Mapping], saved: Sequence[Mapping] = ()) -> list[dict]:
    """[{skill, label, category, uses, level, custom}] in display order: skill-fact groups, then experience tags,
    then skills added by hand. `uses` counts the roles and accomplishments tagged with it. `saved` rows (from
    skill_levels) supply ratings and hand-added skills."""
    texts = [str(f.get("text") or "") for f in facts]
    used = Counter(key(t) for f in facts if f.get("kind") in ("role", "achievement")
                   for t in (f.get("tags") or []) if t and t.strip())
    ratings = {r["skill"]: r for r in saved}
    out: list[dict] = []
    seen: set[str] = set()

    def add(tag: str, category: str, custom: bool = False) -> None:
        k = key(tag)
        if not k or k in seen:
            return
        seen.add(k)
        r = ratings.get(k) or {}
        out.append({"skill": k, "label": r.get("label") or _label(tag.strip(), texts), "category": category,
                    "uses": used.get(k, 0), "level": r.get("level"), "custom": custom})

    for f in facts:
        if f.get("kind") == "skill":
            for t in f.get("tags") or []:
                add(t, _category(f))
    for t in sorted(used, key=lambda t: (-used[t], t)):
        add(t, EXPERIENCE)
    for r in saved:
        if r.get("custom"):
            add(r["label"], CUSTOM, custom=True)
    return out


def prompt_text(levels: Mapping[str, int], labels: Mapping[str, str] | None = None) -> str:
    """Rated skills for the scoring prompt, strongest first."""
    labels = labels or {}
    items = sorted(levels.items(), key=lambda kv: (-kv[1], kv[0]))
    return "\n".join(f"- {labels.get(k, k)}: {v}/10 ({band(v)})" for k, v in items) or "- (none rated)"
