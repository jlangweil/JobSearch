"""Engine factory, per-connection PRAGMAs, and the only timestamp helpers in the codebase."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, create_engine, event

ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


# ---------------------------------------------------------------------------------------------
# Timestamps: ISO-8601 UTC with Z. Nothing else in the codebase formats or parses one.
# ---------------------------------------------------------------------------------------------


def utcnow_iso() -> str:
    return datetime.now(UTC).strftime(ISO_FMT)


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime(ISO_FMT)


def parse_iso(value: str | None) -> datetime | None:
    """Parse our canonical format, and tolerate the ISO variants that source APIs emit."""
    if not value:
        return None
    s = str(value).strip()
    try:
        return datetime.strptime(s, ISO_FMT).replace(tzinfo=UTC)
    except ValueError:
        pass
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso_from_epoch_ms(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    return to_iso(datetime.fromtimestamp(float(ms) / 1000.0, tz=UTC))


def iso_shift(days: float = 0, *, base: str | None = None) -> str:
    start = parse_iso(base) if base else datetime.now(UTC)
    assert start is not None
    return to_iso(start + timedelta(days=days))  # type: ignore[return-value]


def age_days(value: str | None, *, now: str | None = None) -> float | None:
    dt = parse_iso(value)
    if dt is None:
        return None
    ref = parse_iso(now) if now else datetime.now(UTC)
    assert ref is not None
    return (ref - dt).total_seconds() / 86400.0


def iso_date_to_iso(date_str: str) -> str:
    """'YYYY-MM-DD' (form input, interpreted as UTC midnight) -> canonical ISO."""
    return to_iso(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC))  # type: ignore[return-value]


def local_display(value: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Templates only: convert stored UTC to the machine's local time."""
    dt = parse_iso(value)
    return dt.astimezone().strftime(fmt) if dt else ""


# ---------------------------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------------------------

_ENGINES: dict[str, Engine] = {}
_ENGINES_LOCK = threading.Lock()


def _apply_pragmas(dbapi_connection, _record) -> None:  # noqa: ANN001
    cur = dbapi_connection.cursor()
    try:
        cur.execute("PRAGMA journal_mode = WAL")
        cur.execute("PRAGMA busy_timeout = 10000")
        cur.execute("PRAGMA foreign_keys = ON")
        cur.execute("PRAGMA synchronous = NORMAL")
    finally:
        cur.close()


def make_engine(db_path: Path) -> Engine:
    db_path = Path(db_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite+pysqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )
    event.listen(engine, "connect", _apply_pragmas)
    return engine


def get_engine(db_path: Path) -> Engine:
    key = str(Path(db_path).resolve())
    with _ENGINES_LOCK:
        if key not in _ENGINES:
            _ENGINES[key] = make_engine(Path(key))
        return _ENGINES[key]


def dispose_engines() -> None:
    with _ENGINES_LOCK:
        for eng in _ENGINES.values():
            eng.dispose()
        _ENGINES.clear()
