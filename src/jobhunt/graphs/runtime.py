"""Process-wide runtime: config, engine, LangGraph checkpointer, bounded background executor, and the
injectable service factories (LLM, embedder, sources) that graph nodes use."""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from langgraph.checkpoint.sqlite import SqliteSaver
from sqlalchemy import Engine

from jobhunt.config import AppConfig, get_config
from jobhunt.db import queries
from jobhunt.db.engine import get_engine
from jobhunt.embeddings import Embedder, get_embedder
from jobhunt.llm.client import LLMClient
from jobhunt.sources.base import JobSource
from jobhunt.sources.registry import build_sources

log = logging.getLogger(__name__)


def open_checkpointer(cfg: AppConfig) -> SqliteSaver:
    path = cfg.paths.checkpoints
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA synchronous = NORMAL")
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


@dataclass
class Runtime:
    cfg: AppConfig
    engine: Engine
    checkpointer: SqliteSaver
    executor: ThreadPoolExecutor
    llm_factory: Callable[[], LLMClient] | None = None
    embedder_factory: Callable[[], Embedder] | None = None
    sources_factory: Callable[..., list[JobSource]] | None = None
    # Last background failure per thread id (shown on the workflow/run pages until the next attempt).
    failures: dict[str, str] = field(default_factory=dict)
    # Web-started dry runs (in memory only; they write nothing): id -> {status, lines, started, error}.
    dry_runs: dict[int, dict] = field(default_factory=dict)
    scheduler: object | None = None
    local_http_client: object | None = None  # injectable httpx.Client for the local model (tests)
    llm_test: dict = field(default_factory=dict)  # last "Test local model" result shown on Settings
    batches: dict[int, dict] = field(default_factory=dict)  # bulk rescoring jobs started from the queue (in memory)
    _router: object | None = None
    _graphs: dict[str, object] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def llm(self, provider: str | None = None) -> LLMClient:
        """The model client. `provider` ("anthropic" | "local") overrides the configured choice for one action."""
        if self.llm_factory:
            return self.llm_factory()
        from jobhunt.llm.client import RoutingLLM

        with self._lock:
            if self._router is None:
                self._router = RoutingLLM(self.engine, lambda: self.cfg, local_client=self.local_http_client)
        return self._router.with_provider(provider)  # type: ignore[union-attr]

    def embedder(self) -> Embedder:
        return self.embedder_factory() if self.embedder_factory else get_embedder(self.cfg.embeddings.model)

    def sources(self, names: Sequence[str] | None, companies: Sequence[str] | None = None) -> list[JobSource]:
        if self.sources_factory:
            return self.sources_factory(names)
        from jobhunt.discovery.prefilter import title_reason

        return build_sources(self.cfg, names, engine=self.engine,
                             known=lambda s, i: queries.job_exists(self.engine, s, i),
                             title_gate=lambda t: title_reason(t, self.cfg) is None, only_companies=companies)

    def dry_sources(self, names: Sequence[str] | None, companies: Sequence[str] | None = None) -> list[JobSource]:
        """Sources for a dry run: built from config (not the DB), never marking anything as already seen."""
        if self.sources_factory:
            return self.sources_factory(names)
        from jobhunt.discovery.prefilter import title_reason

        return build_sources(self.cfg, names, engine=None, title_gate=lambda t: title_reason(t, self.cfg) is None,
                             only_companies=companies)

    def graph(self, name: str):  # noqa: ANN201
        with self._lock:
            if name not in self._graphs:
                if name == "discovery":
                    from jobhunt.graphs.discovery.graph import build_graph
                elif name == "application":
                    from jobhunt.graphs.application.graph import build_graph
                else:
                    raise KeyError(name)
                self._graphs[name] = build_graph(self.checkpointer)
            return self._graphs[name]

    def submit(self, fn: Callable[[], object]) -> Future:
        return self.executor.submit(fn)

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)
        try:
            self.checkpointer.conn.close()
        except sqlite3.Error:
            pass


_RUNTIME: Runtime | None = None
_RUNTIME_LOCK = threading.Lock()


def init_runtime(cfg: AppConfig | None = None, **factories) -> Runtime:  # noqa: ANN003
    global _RUNTIME
    cfg = cfg or get_config()
    with _RUNTIME_LOCK:
        if _RUNTIME is not None:
            _RUNTIME.close()
        _RUNTIME = Runtime(
            cfg=cfg,
            engine=get_engine(cfg.paths.db),
            checkpointer=open_checkpointer(cfg),
            executor=ThreadPoolExecutor(max_workers=max(1, cfg.web.executor_workers), thread_name_prefix="graph"),
            **factories,
        )
        return _RUNTIME


def get_runtime() -> Runtime:
    if _RUNTIME is None:
        return init_runtime()
    return _RUNTIME


def reset_runtime() -> None:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is not None:
            _RUNTIME.close()
        _RUNTIME = None
