"""Hybrid search (SPEC 4.7): FTS5 bm25 union embedding cosine, ranked 0.5 * normalized_bm25 + 0.5 * cosine."""

from __future__ import annotations

from jobhunt.db import queries, vectors
from jobhunt.db.engine import iso_shift
from jobhunt.graphs.runtime import Runtime

SEMANTIC_WINDOW_DAYS = 60
CANDIDATES = 200


def hybrid_search(rt: Runtime, q: str, limit: int = 50) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return []
    scores: dict[int, dict[str, float]] = {}
    fts = queries.fts_search(rt.engine, q, CANDIDATES)
    if fts:
        vals = [-rank for _, rank in fts]  # bm25(): lower is better
        hi, lo = max(vals), min(vals)
        for (job_id, _), v in zip(fts, vals, strict=True):
            scores.setdefault(job_id, {})["bm25"] = (v - lo) / (hi - lo) if hi > lo else 1.0

    rows = queries.search_embedding_rows(rt.engine, iso_shift(-SEMANTIC_WINDOW_DAYS))
    ids, mat = vectors.matrix(rows)
    if ids:
        qv = rt.embedder().embed([q])[0]
        for idx, cos in vectors.top_k(mat, qv, CANDIDATES):
            scores.setdefault(ids[idx], {})["cosine"] = max(0.0, min(1.0, cos))

    if not scores:
        return []
    ranked = sorted(scores.items(), key=lambda kv: -(0.5 * kv[1].get("bm25", 0) + 0.5 * kv[1].get("cosine", 0)))
    top = dict(ranked[:limit])
    jobs, _ = queries.list_queue(rt.engine, queries.QueueFilters(status="any", job_ids=list(top)), paginate=False)
    for j in jobs:
        s = top[j["id"]]
        j["search_score"] = round(0.5 * s.get("bm25", 0) + 0.5 * s.get("cosine", 0), 3)
        j["bm25_norm"] = round(s.get("bm25", 0), 3)
        j["cosine"] = round(s.get("cosine", 0), 3)
    jobs.sort(key=lambda j: -j["search_score"])
    return jobs
