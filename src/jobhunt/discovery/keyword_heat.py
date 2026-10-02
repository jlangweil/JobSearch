"""Keyword heatmap: what the best-scoring postings ask for, and whether your background covers it.

Built from the analysis each score stores (rubric v5+): the posting's verified technology stack (required or preferred,
and whether you have each) and, from v8, its leadership and delivery competencies. Rebuilt after every score, so the
heatmap follows your list as it changes.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence

from sqlalchemy import Engine

from jobhunt.config import AppConfig
from jobhunt.db import queries
from jobhunt.db.engine import utcnow_iso

log = logging.getLogger(__name__)

META_KEY = "keyword_heat_meta"


def normalize(term: str) -> str:
    """One key per keyword: "Stakeholder_Management" and "stakeholder management" are the same; so are "Java 17"
    and "Java", and "OpenID Connect (OIDC)" and "OpenID Connect"."""
    t = re.sub(r"[_\s]+", " ", str(term or "")).strip().lower()
    t = re.sub(r"\s*\([^)]*\)\s*$", "", t)              # a trailing (ACRONYM)
    t = re.sub(r"\s+v?\d+(\.\d+)*$", "", t)              # a trailing version
    return t.strip(" .,-")


def build(analyses: Sequence[Mapping]) -> tuple[list[dict], int]:
    """(rows, jobs counted) from the stored analyses of the top-scoring jobs. Each posting counts a keyword once."""
    jobs = 0
    count: dict[tuple[str, str], int] = Counter()
    required: dict[tuple[str, str], int] = Counter()
    have: dict[tuple[str, str], int] = Counter()
    spellings: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for row in analyses:
        a = row.get("analysis")
        a = json.loads(a) if isinstance(a, str) else (a or {})
        items: dict[tuple[str, str], tuple[str, bool, bool]] = {}
        for s in a.get("stack") or []:
            key = (normalize(s.get("technology")), "technology")
            items.setdefault(key, (s.get("technology") or "", s.get("importance") == "required",
                                   s.get("has") not in (None, "no")))
        for c in a.get("competencies") or []:
            key = (normalize(c.get("competency")), "competency")
            items.setdefault(key, (c.get("competency") or "", c.get("importance") == "required", bool(c.get("held"))))
        if not items:
            continue
        jobs += 1
        for key, (spelling, req, held) in items.items():
            if not key[0]:
                continue
            count[key] += 1
            required[key] += int(req)
            have[key] += int(held)
            spellings[key][spelling.strip()] += 1
    rows = [{"keyword": k, "kind": kind, "label": _label(spellings[(k, kind)]), "jobs": n,
             "required": required[(k, kind)], "have": have[(k, kind)], "share": round(n / jobs, 4) if jobs else 0.0}
            for (k, kind), n in count.items()]
    rows.sort(key=lambda r: (-r["jobs"], -r["required"], r["label"].lower()))
    return rows, jobs


def _label(spellings: Counter) -> str:
    """The most common spelling, preferring one with capitals ("AWS" over "aws")."""
    ranked = sorted(spellings.items(), key=lambda kv: (-kv[1], not any(ch.isupper() for ch in kv[0]), kv[0]))
    return ranked[0][0].replace("_", " ") if ranked else ""


def rebuild(engine: Engine, cfg: AppConfig) -> dict:
    """Recount from the current scores and store the result. Returns the summary saved alongside it."""
    rows, jobs = build(queries.top_score_analyses(engine, cfg.scoring.heatmap_min_score))
    queries.replace_keyword_heat(engine, rows)
    meta = {"jobs": jobs, "keywords": len(rows), "min_score": cfg.scoring.heatmap_min_score, "built_at": utcnow_iso()}
    queries.set_state(engine, META_KEY, json.dumps(meta))
    return meta


def refresh(engine: Engine, cfg: AppConfig) -> None:
    """After a score: rebuild, never letting the heatmap break scoring."""
    try:
        rebuild(engine, cfg)
    except Exception:  # noqa: BLE001 - a display aid; scoring goes on regardless
        log.exception("keyword heatmap rebuild failed")


def meta(engine: Engine) -> dict:
    raw = queries.get_state(engine, META_KEY)
    return json.loads(raw) if raw else {}
