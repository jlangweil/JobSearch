"""In-process APScheduler (started by `jobhunt serve`). Schedules live in the database; see services/schedules.py."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from jobhunt.db import queries
from jobhunt.graphs.runtime import Runtime
from jobhunt.services import schedules

log = logging.getLogger(__name__)
PREFIX = "schedule:"


def build_scheduler(rt: Runtime) -> BackgroundScheduler:
    sched = BackgroundScheduler()
    sched.add_job(lambda: queries.wake_snoozed(rt.engine), CronTrigger(minute=5), id="wake_snoozed",
                  replace_existing=True)
    rt.scheduler = sched
    schedules.import_legacy(rt)
    apply_schedules(rt, startup=True)
    return sched


def apply_schedules(rt: Runtime, *, startup: bool = False) -> None:
    """(Re)register every enabled schedule. Safe to call after any change; pending retries are kept."""
    sched = rt.scheduler
    if sched is None:
        return
    for job in sched.get_jobs():
        if job.id.startswith(PREFIX) and ":retry" not in job.id:
            sched.remove_job(job.id)
    if schedules.is_paused(rt.engine):
        log.info("schedules are paused")
        return
    for s in queries.list_schedules(rt.engine):
        if not s["enabled"]:
            continue
        try:
            trigger = schedules.build_trigger(s)
        except schedules.ScheduleError as exc:
            log.warning("schedule %s (%s) not registered: %s", s["id"], s["name"], exc)
            continue
        grace = schedules.CATCH_UP_HOURS * 3600 if s["catch_up"] else 120
        sid = s["id"]
        sched.add_job(lambda sid=sid: schedules.fire(rt, sid), trigger, id=f"{PREFIX}{sid}", name=s["name"],
                      replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=grace)
        if startup and s["catch_up"] and schedules.last_missed(s) is not None:
            log.info("schedule %s missed a run while the server was off; running it in a minute", s["name"])
            sched.add_job(lambda sid=sid: schedules.fire(rt, sid), DateTrigger(datetime.now().astimezone()
                                                                               + timedelta(minutes=1)),
                          id=f"{PREFIX}{sid}:catchup", replace_existing=True)


def retry_later(rt: Runtime, schedule_id: int, attempt: int) -> None:
    sched = rt.scheduler
    if sched is None:
        return
    when = datetime.now().astimezone() + timedelta(minutes=schedules.RETRY_MINUTES)
    sched.add_job(lambda: schedules.fire(rt, schedule_id, attempt=attempt), DateTrigger(when),
                  id=f"{PREFIX}{schedule_id}:retry", replace_existing=True)


def active(rt: Runtime) -> bool:
    return rt.scheduler is not None and bool(getattr(rt.scheduler, "running", False))


def next_runs(rt: Runtime, limit: int = 8) -> list[dict]:
    """Upcoming fires across enabled schedules (computed from the definitions, so it works in any process)."""
    if schedules.is_paused(rt.engine):
        return []
    out = []
    for s in queries.list_schedules(rt.engine):
        if not s["enabled"]:
            continue
        try:
            for when in schedules.upcoming(s, count=limit):
                out.append({"when": when, "schedule": s})
        except schedules.ScheduleError:
            continue
    out.sort(key=lambda e: e["when"])
    return out[:limit]
