"""Duplicate grouping (SPEC 6.3): exact content hash, or cosine >= 0.93 AND same company slug AND
token_set_ratio(title) >= 85. Canonical: company ATS > aggregator > email, then earliest posted_at."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from rapidfuzz import fuzz

from jobhunt.db import vectors
from jobhunt.sources.normalize import slugify
from jobhunt.sources.registry import source_rank

COSINE_THRESHOLD = 0.93
TITLE_RATIO_THRESHOLD = 85


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def plan(new_ids: Sequence[int], window: Sequence[dict]) -> list[tuple[int, int, bool]]:
    """Return [(job_id, group_id, mark_duplicate)] for every job whose grouping should be written.

    `window` holds rows from queries.window_jobs (the rolling 60-day set, including the new jobs)."""
    by_id = {r["id"]: r for r in window}
    new = [i for i in new_ids if i in by_id and by_id[i]["status"] == "discovered"]
    if not new:
        return []
    uf = _UnionFind()
    for r in window:
        if r.get("dedupe_group"):
            uf.union(r["dedupe_group"], r["id"])

    by_hash: dict[str, list[int]] = {}
    for r in window:
        by_hash.setdefault(r["content_hash"], []).append(r["id"])
    for jid in new:
        for other in by_hash.get(by_id[jid]["content_hash"], []):
            if other != jid:
                uf.union(other, jid)

    ids, mat = vectors.matrix(window)
    if len(ids):
        index = {jid: i for i, jid in enumerate(ids)}
        slugs = [slugify(by_id[j]["company_name_raw"]) for j in ids]
        for jid in new:
            if jid not in index:
                continue
            row = mat[index[jid]]
            sims = mat @ row
            for k in np.nonzero(sims >= COSINE_THRESHOLD)[0]:
                other = ids[int(k)]
                if other == jid or slugs[int(k)] != slugs[index[jid]]:
                    continue
                ratio = fuzz.token_set_ratio(by_id[jid]["title"].lower(), by_id[other]["title"].lower())
                if ratio >= TITLE_RATIO_THRESHOLD:
                    uf.union(other, jid)

    groups: dict[int, list[int]] = {}
    for r in window:
        groups.setdefault(uf.find(r["id"]), []).append(r["id"])

    out: list[tuple[int, int, bool]] = []
    new_set = set(new)
    for members in groups.values():
        if not new_set.intersection(members):
            continue

        def key(jid: int) -> tuple:
            r = by_id[jid]
            progressed = 0 if r["status"] not in ("discovered", "duplicate") else 1
            return (progressed, source_rank(r["source"]), r.get("posted_at") or "9999", jid)

        candidates = [m for m in members if by_id[m]["status"] != "duplicate"] or members
        canonical = min(candidates, key=key)
        for m in members:
            r = by_id[m]
            if m == canonical:
                if r.get("dedupe_group") != canonical:
                    out.append((m, canonical, False))
            elif r["status"] == "discovered":
                out.append((m, canonical, True))
            elif r.get("dedupe_group") != canonical:
                out.append((m, canonical, False))
    return out
