"""Programmatic Alembic upgrade + LangGraph checkpointer setup (`jobhunt db upgrade`)."""

from __future__ import annotations

from alembic import command
from alembic.config import Config as AlembicConfig

from jobhunt.config import PROJECT_ROOT, AppConfig


def upgrade(cfg: AppConfig) -> None:
    db = cfg.paths.db
    db.parent.mkdir(parents=True, exist_ok=True)
    acfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    acfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    acfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    acfg.attributes["db_path"] = db
    command.upgrade(acfg, "head")

    from jobhunt.graphs.runtime import open_checkpointer

    saver = open_checkpointer(cfg)
    saver.conn.close()
