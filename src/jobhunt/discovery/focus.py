"""Scoring focus (code-computed, no LLM): phrases that should rank a job higher, and title phrases for areas to avoid.

focus_fit (0-100):
  - title matches an `avoid` phrase      → 0, and the job's total is capped at `avoid_cap`
  - otherwise 20, +40 if a `boost` phrase is in the title, +10 per further distinct boost phrase in the description
    (up to +40)
Pure functions; recomputed for every stored score whenever the focus lists change.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import lru_cache

BASE = 20
TITLE_BONUS = 40
DESCRIPTION_BONUS = 10
DESCRIPTION_MAX = 40


@lru_cache(maxsize=512)
def phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Case-insensitive whole-word match; spaces/hyphens interchangeable; optional plural."""
    words = [re.escape(w) for w in re.split(r"[\s\-_/]+", phrase.strip()) if w]
    body = r"[\s\-_/]*".join(words)
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?:e?s)?(?![A-Za-z0-9])", re.IGNORECASE)


def matches(phrases: Sequence[str], text: str | None) -> list[str]:
    if not text:
        return []
    return [p for p in dict.fromkeys(p.strip() for p in phrases if p and p.strip()) if phrase_pattern(p).search(text)]


def evaluate(title: str | None, description: str | None, boost: Sequence[str],
             avoid: Sequence[str]) -> tuple[int, dict]:
    avoid_hits = matches(avoid, title)
    title_hits = matches(boost, title)
    desc_hits = [p for p in matches(boost, description) if p not in title_hits]
    hits = {"title": title_hits, "description": desc_hits, "avoid": avoid_hits}
    if avoid_hits:
        return 0, hits
    fit = BASE + (TITLE_BONUS if title_hits else 0) + min(DESCRIPTION_MAX, DESCRIPTION_BONUS * len(desc_hits))
    return min(100, fit), hits
