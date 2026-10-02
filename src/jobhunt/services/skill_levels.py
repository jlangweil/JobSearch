"""Fact bank → Skill levels: the owner rates skills 1-10; scoring weighs the technology stack by them."""

from __future__ import annotations

from collections.abc import Mapping

from jobhunt.db import queries
from jobhunt.discovery.service import recompute_totals
from jobhunt.facts import skills
from jobhunt.graphs.runtime import Runtime


def view(rt: Runtime) -> dict:
    items = skills.catalog(queries.list_facts(rt.engine, file_order=True), queries.list_skill_levels(rt.engine))
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(it["category"], []).append(it)
    return {"groups": groups, "rated": sum(1 for it in items if it["level"]), "total": len(items),
            "bands": skills.LEVEL_BANDS, "experience": skills.EXPERIENCE}


def _level(raw: str | None) -> int | None:
    try:
        v = int(str(raw or "").strip())
    except ValueError:
        return None
    return v if 1 <= v <= 10 else None


def save(rt: Runtime, form: Mapping[str, str]) -> tuple[int, int]:
    """Form rows are skill_<i>, label_<i>, level_<i>, custom_<i>; plus new_label / new_level to add one.
    Returns (skills rated, scores recomputed)."""
    rows = []
    i = 0
    while f"skill_{i}" in form:
        rows.append({"skill": form[f"skill_{i}"], "label": form.get(f"label_{i}") or form[f"skill_{i}"],
                     "level": _level(form.get(f"level_{i}")), "custom": form.get(f"custom_{i}") == "1"})
        i += 1
    new_label = (form.get("new_label") or "").strip()
    if new_label:
        rows.append({"skill": skills.key(new_label), "label": new_label, "level": _level(form.get("new_level")),
                     "custom": True})
    # only rows worth keeping: a rating, or a skill added by hand
    queries.save_skill_levels(rt.engine, [r for r in rows if r["level"] is not None or r["custom"]])
    for r in rows:
        if r["level"] is None and not r["custom"]:
            queries.delete_skill_level(rt.engine, r["skill"])
    return sum(1 for r in rows if r["level"] is not None), recompute_totals(rt.engine, rt.cfg)


def remove(rt: Runtime, skill: str) -> int:
    queries.delete_skill_level(rt.engine, skill)
    return recompute_totals(rt.engine, rt.cfg)
