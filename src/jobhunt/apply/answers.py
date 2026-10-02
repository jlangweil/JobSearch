"""Answers bank: reusable responses to common application questions (loaded from facts.yaml `answers:`)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Engine

from jobhunt.db import queries, vectors
from jobhunt.embeddings import Embedder

COMMON_QUESTIONS = [
    "Are you legally authorized to work in the United States?",
    "Will you now or in the future require visa sponsorship?",
    "What are your salary expectations?",
    "When can you start / what is your notice period?",
    "Are you willing to work hybrid or on-site?",
    "How did you hear about this position?",
]


def suggest(engine: Engine, embedder: Embedder, questions: Sequence[str] = COMMON_QUESTIONS,
            min_score: float = 0.55) -> list[dict]:
    rows = queries.list_answers(engine, with_embedding=True)
    if not rows:
        return [{"question": q, "answer": None, "score": 0.0, "answer_id": None} for q in questions]
    ids, mat = vectors.matrix(rows)
    by_id = {r["id"]: r for r in rows}
    out = []
    for q, qv in zip(questions, embedder.embed(list(questions)), strict=True):
        best = vectors.top_k(mat, qv, 1)
        if best and best[0][1] >= min_score:
            r = by_id[ids[best[0][0]]]
            out.append({"question": q, "answer": r["answer_text"], "score": round(best[0][1], 3), "answer_id": r["id"]})
        else:
            out.append({"question": q, "answer": None, "score": round(best[0][1], 3) if best else 0.0,
                        "answer_id": None})
    return out
