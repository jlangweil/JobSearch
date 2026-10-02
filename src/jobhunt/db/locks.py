"""Cross-process advisory locks backed by the `locks` table (SPEC 4.16).

Acquire = INSERT (primary key rejects a second holder). Release = DELETE. A lock older than
`lock_stale_minutes`, or held by a process that no longer exists, may be reclaimed — and reclaiming
is always logged."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine

from jobhunt.db import queries
from jobhunt.db.engine import age_days

log = logging.getLogger(__name__)

DISCOVERY_LOCK = "discovery_run"


def thread_lock_name(thread_id: str) -> str:
    return f"thread:{thread_id}"


def _stop_name(run_id: int) -> str:
    return f"stop:run-{run_id}"


def request_stop(engine: Engine, run_id: int, note: str | None = None) -> bool:
    """Ask a discovery run to stop at its next checkpoint. Works across processes (CLI runs too)."""
    return queries.lock_insert(engine, _stop_name(run_id), os.getpid(), note or "stop requested")


def stop_requested(engine: Engine, run_id: int) -> bool:
    return queries.lock_get(engine, _stop_name(run_id)) is not None


def clear_stop(engine: Engine, run_id: int) -> None:
    queries.lock_delete(engine, _stop_name(run_id))


class LockHeld(RuntimeError):
    def __init__(self, name: str, holder: dict | None) -> None:
        self.name = name
        self.holder = holder or {}
        since = self.holder.get("acquired_at", "?")
        pid = self.holder.get("pid", "?")
        super().__init__(f"lock {name!r} is held (pid {pid} since {since}): {self.holder.get('note') or ''}".strip())


def pid_alive(pid: int) -> bool:
    """True if a process with this pid exists. Never sends a signal (os.kill terminates on Windows)."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire(engine: Engine, name: str, *, note: str | None = None, stale_minutes: int = 30) -> None:
    pid = os.getpid()
    if queries.lock_insert(engine, name, pid, note):
        return
    holder = queries.lock_get(engine, name)
    if holder is None:  # released between our insert and read
        if queries.lock_insert(engine, name, pid, note):
            return
        raise LockHeld(name, queries.lock_get(engine, name))
    age_min = (age_days(holder["acquired_at"]) or 0) * 1440
    dead = not pid_alive(int(holder["pid"]))
    if age_min >= stale_minutes or dead:
        why = "holder process is gone" if dead else f"stale for {age_min:.0f} min"
        if queries.lock_delete(engine, name, acquired_at=holder["acquired_at"]):
            log.warning("Reclaimed lock %r from pid %s (%s); note was %r", name, holder["pid"], why, holder.get("note"))
        if queries.lock_insert(engine, name, pid, note):
            return
        holder = queries.lock_get(engine, name)
    raise LockHeld(name, holder)


def release(engine: Engine, name: str) -> None:
    queries.lock_delete(engine, name)


def heartbeat(engine: Engine, name: str) -> None:
    """Refresh acquired_at so a long-running holder is not treated as stale."""
    queries.lock_touch(engine, name, os.getpid())


def is_held(engine: Engine, name: str, stale_minutes: int = 30) -> bool:
    holder = queries.lock_get(engine, name)
    if holder is None:
        return False
    age_min = (age_days(holder["acquired_at"]) or 0) * 1440
    return age_min < stale_minutes and pid_alive(int(holder["pid"]))


@contextmanager
def held(engine: Engine, name: str, *, note: str | None = None, stale_minutes: int = 30) -> Iterator[None]:
    acquire(engine, name, note=note, stale_minutes=stale_minutes)
    try:
        yield
    finally:
        release(engine, name)
