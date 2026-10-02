"""Local embeddings (BAAI/bge-small-en-v1.5, 384-dim). Injectable for tests.

Computed directly with `transformers` + `torch`: CLS-token pooling, then L2 normalization — exactly what the
model's sentence-transformers configuration specifies (verified identical to within 3e-8). Not using the
sentence-transformers package avoids installing SciPy and scikit-learn, whose unsigned compiled modules Windows 11
Smart App Control blocks ("An Application Control policy has blocked this file")."""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Sequence
from typing import Protocol

import numpy as np

from jobhunt.db.vectors import DIM, normalize

MAX_TOKENS = 512


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[np.ndarray]: ...


class EmbedderUnavailable(RuntimeError):
    pass


def _blocked(exc: BaseException) -> bool:
    """Smart App Control refusing an unsigned DLL: WinError 4551 (an OSError on load, or an ImportError around it)."""
    return "Application Control" in str(exc) or getattr(exc, "winerror", None) == 4551


def _import_torch():  # noqa: ANN202
    try:
        import torch
        from transformers import AutoModel, AutoTokenizer
    except (ImportError, OSError) as exc:
        if _blocked(exc):
            raise EmbedderUnavailable(
                f"Windows Smart App Control blocked a file needed for local embeddings ({exc}). "
                "See README -> Troubleshooting.") from exc
        raise
    return torch, AutoModel, AutoTokenizer


class TransformersEmbedder:
    def __init__(self, model_name: str, batch_size: int = 32) -> None:
        self.model_name = model_name
        self.batch_size = batch_size
        self._model = None
        self._tokenizer = None
        self._lock = threading.Lock()

    def _load(self):  # noqa: ANN202
        with self._lock:
            if self._model is None:
                _, AutoModel, AutoTokenizer = _import_torch()  # noqa: N806
                self._tokenizer = AutoTokenizer.from_pretrained(self.model_name)
                self._model = AutoModel.from_pretrained(self.model_name).eval()
            return self._tokenizer, self._model

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        if not texts:
            return []
        torch, _, _ = _import_torch()
        tokenizer, model = self._load()
        out: list[np.ndarray] = []
        with torch.inference_mode():
            for i in range(0, len(texts), self.batch_size):
                batch = tokenizer(list(texts[i: i + self.batch_size]), padding=True, truncation=True,
                                  max_length=MAX_TOKENS, return_tensors="pt")
                cls = model(**batch).last_hidden_state[:, 0]
                vecs = torch.nn.functional.normalize(cls, p=2, dim=1).cpu().numpy().astype(np.float32)
                out.extend(vecs)
        return out


class HashingEmbedder:
    """Deterministic bag-of-words hashing embedder. Offline tests only — not semantically meaningful."""

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        out = []
        for t in texts:
            v = np.zeros(DIM, dtype=np.float32)
            for tok in re.findall(r"\w+", t.lower()):
                h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
                v[h % DIM] += 1.0
            out.append(normalize(v) if v.any() else v)
        return out


_EMBEDDER: Embedder | None = None


def get_embedder(model_name: str) -> Embedder:
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = TransformersEmbedder(model_name)
    return _EMBEDDER


def set_embedder(embedder: Embedder | None) -> None:
    global _EMBEDDER
    _EMBEDDER = embedder
