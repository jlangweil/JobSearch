"""Smart App Control blocking torch must not end a run: it carries on without embeddings."""

from __future__ import annotations

import pytest

from jobhunt import embeddings
from jobhunt.embeddings import EmbedderUnavailable, TransformersEmbedder


def _blocked_import(*_a, **_k):
    exc = OSError(22, "An Application Control policy has blocked this file. Error loading shm.dll")
    exc.winerror = 4551
    raise exc


def test_a_blocked_torch_dll_reads_as_unavailable(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        return _blocked_import() if name == "torch" else real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(EmbedderUnavailable):
        TransformersEmbedder("x").embed(["a posting"])


def test_blocked_detection():
    exc = OSError("An Application Control policy has blocked this file.")
    assert embeddings._blocked(exc)
    assert not embeddings._blocked(OSError("disk full"))


def test_the_normalize_step_carries_on(monkeypatch):
    from jobhunt.graphs.discovery import nodes

    class Blocked:
        def embed(self, texts):
            raise EmbedderUnavailable("Windows Smart App Control blocked a file.")

    class Rt:
        engine = None
        def embedder(self):
            return Blocked()

    monkeypatch.setattr(nodes, "get_runtime", lambda: Rt())
    monkeypatch.setattr(nodes, "_progress", lambda *a, **k: None)
    monkeypatch.setattr(nodes.service, "normalize", lambda engine, ids, emb, **k: emb.embed(["x"]))
    out = nodes.normalize({"counts": {}, "errors": [], "fetched_job_ids": [1]})
    assert out["counts"]["embedded"] == 0
    assert "Smart App Control" in out["errors"][0]["message"] and out["errors"][0]["node"] == "normalize"
