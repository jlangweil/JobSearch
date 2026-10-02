"""FastAPI application factory (SPEC 8)."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from jobhunt.config import AppConfig, get_config, set_config
from jobhunt.db import queries
from jobhunt.graphs.discovery.graph import reconcile_orphans
from jobhunt.graphs.runtime import get_runtime, init_runtime

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(cfg: AppConfig | None = None, *, start_scheduler: bool = True, runtime_factories: dict | None = None
               ) -> FastAPI:
    cfg = cfg or get_config()
    set_config(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        from jobhunt.db import migrate
        from jobhunt.services.companies import sync_config_companies

        migrate.upgrade(cfg)  # idempotent: the UI works on a fresh checkout without `jobhunt db upgrade`
        rt = init_runtime(cfg, **(runtime_factories or {}))
        sync_config_companies(rt.engine, rt.cfg)
        reconcile_orphans(rt)
        queries.wake_snoozed(rt.engine)
        if queries.scores_missing_focus(rt.engine):  # scores from before focus_fit existed: fill it in (no LLM)
            from jobhunt.discovery.service import recompute_totals

            log.info("recomputed focus fit for %d stored scores", recompute_totals(rt.engine, rt.cfg))
        sched = None
        if start_scheduler:
            from jobhunt.services.scheduler import build_scheduler

            sched = build_scheduler(rt)
            sched.start()
        yield
        if sched:
            sched.shutdown(wait=False)
        get_runtime().close()

    dependencies = []
    if cfg.secrets.WEB_PASSWORD:
        basic = HTTPBasic()

        def require_password(credentials: HTTPBasicCredentials = Depends(basic)) -> None:
            ok = secrets.compare_digest(credentials.password.encode("utf-8"),
                                        (cfg.secrets.WEB_PASSWORD or "").encode("utf-8"))
            if not ok:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})

        dependencies.append(Depends(require_password))

    app = FastAPI(title="jobhunt", lifespan=lifespan, dependencies=dependencies, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from jobhunt.web.routes import (
        add_job,
        applications,
        autoapply,
        companies,
        facts,
        home,
        jobs,
        keywords,
        queue,
        runs,
        schedules,
        settings,
        workflow,
    )

    for module in (home, add_job, queue, jobs, workflow, applications, autoapply, facts, keywords, runs, schedules,
                   companies, settings):
        app.include_router(module.router)

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/home", status_code=303)

    @app.exception_handler(queries.InvalidTransition)
    async def invalid_transition(request: Request, exc: queries.InvalidTransition):  # noqa: ANN202
        from jobhunt.web.common import redirect

        return redirect(request.headers.get("referer", "/queue").split("?")[0], error=str(exc))

    return app
