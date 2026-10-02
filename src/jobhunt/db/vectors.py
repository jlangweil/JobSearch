"""Embedding BLOB <-> numpy, brute-force cosine search. Nothing else touches an embedding BLOB.

Everything is L2-normalized on write, so cosine similarity is a dot product. No index, by design."""

from __future__ import annotations

import threading
from collections.abc import Sequence

import numpy as np

DIM = 384


def to_blob(vector: Sequence[float] | np.ndarray) -> bytes:
    v = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(v))
    if norm > 0:
        v = v / norm
    return np.asarray(v, dtype=np.float32).tobytes()


def from_blob(blob: bytes | None) -> np.ndarray | None:
    if blob is None:
        return None
    return np.frombuffer(blob, dtype=np.float32)


def normalize(vector: Sequence[float] | np.ndarray) -> np.ndarray:
    v = np.asarray(vector, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def matrix(rows: Sequence[dict], key: str = "embedding", id_key: str = "id") -> tuple[list, np.ndarray]:
    """Stack BLOBs into an (n, d) matrix, skipping rows with no embedding."""
    ids, vecs = [], []
    for r in rows:
        v = from_blob(r.get(key))
        if v is not None:
            ids.append(r[id_key])
            vecs.append(v)
    if not vecs:
        return [], np.zeros((0, DIM), dtype=np.float32)
    return ids, np.vstack(vecs)


def cosine_scores(mat: np.ndarray, query: Sequence[float] | np.ndarray) -> np.ndarray:
    if mat.shape[0] == 0:
        return np.zeros(0, dtype=np.float32)
    return mat @ normalize(query)


def top_k(mat: np.ndarray, query: Sequence[float] | np.ndarray, k: int) -> list[tuple[int, float]]:
    """Return [(row_index, score)] of the k highest cosine scores, best first."""
    scores = cosine_scores(mat, query)
    n = scores.shape[0]
    if n == 0 or k <= 0:
        return []
    k = min(k, n)
    idx = np.argpartition(-scores, k - 1)[:k]
    idx = idx[np.argsort(-scores[idx], kind="stable")]
    return [(int(i), float(scores[i])) for i in idx]


class FactMatrixCache:
    """In-memory fact-bank matrix, invalidated on `facts sync` / POST /facts (SPEC 4.3)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids: list[str] | None = None
        self._mat: np.ndarray | None = None

    def get(self, loader) -> tuple[list[str], np.ndarray]:  # noqa: ANN001
        with self._lock:
            if self._ids is None or self._mat is None:
                self._ids, self._mat = matrix(loader())
            return self._ids, self._mat

    def invalidate(self) -> None:
        with self._lock:
            self._ids, self._mat = None, None


fact_cache = FactMatrixCache()
