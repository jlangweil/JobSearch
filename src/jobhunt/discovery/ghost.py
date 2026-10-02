"""Ghost score 0-100: higher = more likely stale or fake (SPEC 6.5). Deterministic."""

from __future__ import annotations

import re
from collections.abc import Sequence

from rapidfuzz import fuzz

from jobhunt.db.engine import age_days
from jobhunt.sources.normalize import slugify


def ghost_score(job: dict, *, similar_titles: Sequence[dict] = (), staffing_slugs: Sequence[str] = (),
                now: str | None = None) -> tuple[int, list[str]]:
    score, reasons = 0, []
    posted_age = age_days(job.get("posted_at"), now=now)
    seen_age = age_days(job.get("first_seen_at"), now=now) or 0.0
    seen_count = int(job.get("seen_count") or 1)

    if posted_age is not None and posted_age < 7 and seen_age > 30:
        score += 25
        reasons.append("posting date refreshed but listed for over 30 days")
    if seen_count >= 20 and (posted_age is None or seen_count > posted_age + 5):
        score += 10
        reasons.append(f"seen {seen_count} times")
    if posted_age is not None and posted_age > 45:
        score += 25
        reasons.append(f"posted {posted_age:.0f} days ago and still listed")

    title = (job.get("title") or "").lower()
    for other in similar_titles:
        if other["id"] == job["id"]:
            continue
        if other.get("dedupe_group") and other.get("dedupe_group") == job.get("dedupe_group"):
            continue
        if slugify(other["company_name_raw"]) != slugify(job["company_name_raw"]):
            continue
        if fuzz.token_set_ratio(title, (other["title"] or "").lower()) >= 95:
            score += 20
            reasons.append(f"near-identical title re-posted (job {other['id']})")
            break

    if slugify(job.get("company_name_raw") or "") in set(staffing_slugs):
        score += 20
        reasons.append("staffing agency")

    desc = (job.get("description_text") or "").lower()
    if not job.get("comp_min") and not job.get("comp_max"):
        score += 10
        reasons.append("no compensation posted")
    if not re.search(r"\bteam\b|department|group\b", desc):
        score += 5
        reasons.append("no team named")
    if not re.search(r"hiring manager|report(s|ing)? (in)?to|reports directly", desc):
        score += 5
        reasons.append("no hiring manager or reporting line")
    return min(score, 100), reasons
