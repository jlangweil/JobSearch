"""Schedules managed from the UI (stored in the `schedules` table, fired by the in-process scheduler).

Each schedule starts an ordinary discovery run with trigger "schedule":
  fetch  → fetch, filter and save; jobs wait as `discovered` for manual scoring (free)
  full   → fetch, filter, save and score
  stored → no fetching; score jobs already waiting (e.g. overnight with the local model)

Weekday note: APScheduler 3 numbers weekdays from Monday = 0, unlike cron (Sunday = 0). Triggers are therefore built
with day *names*, and numeric weekdays in a custom cron expression are converted from cron meaning first.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger

from jobhunt.config import AppConfig
from jobhunt.db import queries
from jobhunt.db.engine import parse_iso
from jobhunt.graphs.runtime import Runtime

log = logging.getLogger(__name__)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_LABELS = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}
CRON_DOW = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")  # cron numbering; 7 is also Sunday
MODES = {
    "fetch": "Fetch & save only",
    "full": "Full run (fetch + score)",
    "stored": "Score waiting jobs only",
}
WHEN_KINDS = ("days", "interval", "cron")
RETRY_MINUTES = 10
MAX_RETRIES = 12  # keep trying for two hours when another run is in progress
CATCH_UP_HOURS = 12
PAUSED_KEY = "schedules_paused"
COMMON_TIMEZONES = ("America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
                    "America/Los_Angeles", "UTC", "Europe/London")

PRESETS: dict[str, dict] = {
    "morning-fetch": {"name": "Weekday morning fetch", "mode": "fetch", "when_kind": "days",
                      "days": ["mon", "tue", "wed", "thu", "fri"], "times": ["07:00"],
                      "notes": "Fetch new postings before work; score the interesting ones by hand."},
    "twice-daily-fetch": {"name": "Twice-daily fetch", "mode": "fetch", "when_kind": "days", "days": list(DAYS),
                          "times": ["07:00", "17:30"]},
    "nightly-scoring": {"name": "Nightly scoring of waiting jobs", "mode": "stored", "when_kind": "days",
                        "days": list(DAYS), "times": ["23:00"], "max_to_score": 200,
                        "notes": "Scores whatever is still waiting, e.g. with the free local model overnight."},
    "weekly-full": {"name": "Weekly full run", "mode": "full", "when_kind": "days", "days": ["sun"],
                    "times": ["20:00"], "max_to_score": 150},
    "hourly-workday": {"name": "Every 3 hours during the workday", "mode": "fetch", "when_kind": "interval",
                       "days": ["mon", "tue", "wed", "thu", "fri"], "every_hours": 3, "window_start": 8,
                       "window_end": 17},
}


class ScheduleError(ValueError):
    pass


# --------------------------------------------------------------------------------------------- defaults & forms


def default_values(cfg: AppConfig, preset: str | None = None) -> dict:
    values = {"name": "", "enabled": True, "mode": "fetch", "when_kind": "days", "days": list(DAYS[:5]),
              "times": ["07:00"], "every_hours": 3, "window_start": 8, "window_end": 17, "cron": "0 7 * * 1-5",
              "timezone": cfg.schedule.timezone, "sources": [], "industries": [], "companies": [],
              "max_to_score": None, "if_busy": "wait", "catch_up": True, "notes": ""}
    values.update(PRESETS.get(preset or "", {}))
    return values


def copy_values(schedule: Mapping) -> dict:
    values = {k: schedule.get(k) for k in queries.SCHEDULE_FIELDS}
    values["name"] = f"{schedule['name']} (copy)"
    values["enabled"] = False
    return values


def _ints(value: object, lo: int, hi: int, label: str, errors: list[str], default: int | None = None) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        errors.append(f"{label} must be a whole number")
        return default
    if not lo <= n <= hi:
        errors.append(f"{label} must be between {lo} and {hi}")
    return n


def _times(raw: Sequence[str], errors: list[str]) -> list[str]:
    out: set[str] = set()
    for chunk in raw:
        for part in re.split(r"[,\s]+", chunk or ""):
            if not part:
                continue
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", part)
            if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                errors.append(f"time {part!r} must look like 07:00 or 18:30")
                continue
            out.add(f"{int(m.group(1)):02d}:{m.group(2)}")
    return sorted(out)


def values_from_form(cfg: AppConfig, form) -> tuple[dict, list[str]]:  # noqa: ANN001  (starlette FormData)
    """Parse and validate the schedule form. Always returns the values so the form can be re-shown as typed."""
    from jobhunt.sources.normalize import slugify
    from jobhunt.sources.registry import ALL_SOURCES

    errors: list[str] = []
    get = lambda k: str(form.get(k) or "").strip()  # noqa: E731
    known_slugs = {slugify(c.name) for c in cfg.companies}
    known_industries = {(c.industry or "other") for c in cfg.companies}
    v = {
        "name": get("name")[:80],
        "enabled": form.get("enabled") in ("on", "true", "1"),
        "mode": get("mode") if get("mode") in MODES else "fetch",
        "when_kind": get("when_kind") if get("when_kind") in WHEN_KINDS else "days",
        "days": [d for d in DAYS if d in form.getlist("days")],
        "times": _times(form.getlist("times"), errors),
        "every_hours": _ints(get("every_hours"), 1, 12, "Repeat every (hours)", errors, 3),
        "window_start": _ints(get("window_start"), 0, 23, "From hour", errors, 8),
        "window_end": _ints(get("window_end"), 0, 23, "Until hour", errors, 17),
        "cron": re.sub(r"\s+", " ", get("cron")),
        "timezone": get("timezone") or cfg.schedule.timezone,
        "sources": [s for s in form.getlist("sources") if s in ALL_SOURCES],
        "industries": sorted({i for i in form.getlist("industries") if i in known_industries}),
        "companies": sorted({s for s in form.getlist("companies") if s in known_slugs}),
        "max_to_score": _ints(get("max_to_score"), 1, 5000, "Max jobs to score", errors),
        "if_busy": get("if_busy") if get("if_busy") in ("wait", "skip") else "wait",
        "catch_up": form.get("catch_up") in ("on", "true", "1"),
        "notes": get("notes")[:500],
    }
    if not v["name"]:
        errors.append("Give the schedule a name")
    if v["when_kind"] in ("days", "interval") and not v["days"]:
        errors.append("Pick at least one day")
    if v["when_kind"] == "days" and not v["times"] and not any("time" in e for e in errors):
        errors.append("Add at least one time")
    if v["when_kind"] == "interval" and (v["window_end"] or 0) < (v["window_start"] or 0):
        errors.append("“Until hour” must be the same as or later than “From hour”")
    if not errors:
        try:
            build_trigger(v)
        except ScheduleError as exc:
            errors.append(str(exc))
    return v, errors


# --------------------------------------------------------------------------------------------- triggers


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"unknown time zone {name!r} (examples: America/New_York, America/Chicago)") from exc


def _cron_dow_to_names(field: str) -> str:
    """Cron weekday field (0/7 = Sunday) → APScheduler day names, expanding ranges and steps."""
    if field in ("*", "?"):
        return "*"
    names: list[str] = []
    for part in field.lower().split(","):
        base, _, step_s = part.partition("/")
        step = int(step_s) if step_s.isdigit() else 1
        if base in ("*", ""):
            lo, hi = 0, 6
        elif "-" in base:
            a, b = base.split("-", 1)
            lo, hi = _dow_num(a), _dow_num(b)
            if hi < lo:  # e.g. fri-mon wraps past Sunday
                hi += 7
        else:
            lo = hi = _dow_num(base)
        names += [CRON_DOW[n % 7] for n in range(lo, hi + 1, step)]
    ordered = [d for d in DAYS if d in names]
    if not ordered:
        raise ScheduleError(f"weekday field {field!r} matches no days")
    return ",".join(ordered)


def _dow_num(token: str) -> int:
    token = token.strip()
    if token.isdigit() and 0 <= int(token) <= 7:
        return int(token) % 7
    if token[:3] in CRON_DOW:
        return CRON_DOW.index(token[:3])
    raise ScheduleError(f"weekday {token!r} should be 0-7 or sun…sat")


def crontab_trigger(expr: str, tz: ZoneInfo) -> CronTrigger:
    fields = (expr or "").split()
    if len(fields) != 5:
        raise ScheduleError("cron needs 5 fields: minute hour day-of-month month day-of-week (e.g. 0 7 * * 1-5)")
    minute, hour, dom, month, dow = fields
    try:
        return CronTrigger(minute=minute, hour=hour, day=dom, month=month, day_of_week=_cron_dow_to_names(dow),
                           timezone=tz)
    except ScheduleError:
        raise
    except (ValueError, TypeError) as exc:
        raise ScheduleError(f"invalid cron {expr!r}: {exc}") from exc


def build_trigger(s: Mapping) -> BaseTrigger:
    tz = _zone(s.get("timezone") or "UTC")
    kind = s.get("when_kind") or "days"
    days = ",".join(d for d in DAYS if d in (s.get("days") or [])) or "*"
    if kind == "cron":
        return crontab_trigger(s.get("cron") or "", tz)
    if kind == "interval":
        start, end = int(s.get("window_start") or 0), int(s.get("window_end") or 23)
        step = int(s.get("every_hours") or 1)
        return CronTrigger(day_of_week=days, hour=f"{start}-{end}/{step}", minute=0, timezone=tz)
    times = s.get("times") or []
    if not times:
        raise ScheduleError("add at least one time")
    triggers = [CronTrigger(day_of_week=days, hour=int(t[:2]), minute=int(t[3:5]), timezone=tz) for t in times]
    return triggers[0] if len(triggers) == 1 else OrTrigger(triggers)


def upcoming(s: Mapping, count: int = 5, after: datetime | None = None) -> list[datetime]:
    trigger = build_trigger(s)
    tz = _zone(s.get("timezone") or "UTC")
    now = after or datetime.now(tz)
    out: list[datetime] = []
    prev = None
    while len(out) < count:
        nxt = trigger.get_next_fire_time(prev, now)
        if nxt is None:
            break
        out.append(nxt)
        prev, now = nxt, nxt + timedelta(seconds=1)
    return out


def last_missed(s: Mapping, now: datetime | None = None) -> datetime | None:
    """Most recent fire time within CATCH_UP_HOURS that has not run since the schedule was enabled/last fired."""
    tz = _zone(s.get("timezone") or "UTC")
    now = now or datetime.now(tz)
    baseline = max(filter(None, (parse_iso(s.get("last_fired_at")), parse_iso(s.get("enabled_at")),
                                 parse_iso(s.get("created_at")))), default=None)
    start = now - timedelta(hours=CATCH_UP_HOURS)
    if baseline and baseline > start:
        start = baseline
    missed = None
    for when in upcoming(s, count=50, after=start + timedelta(seconds=1)):
        if when > now:
            break
        missed = when
    return missed


# --------------------------------------------------------------------------------------------- descriptions


def _clock(hhmm: str) -> str:
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def describe_days(days: Sequence[str]) -> str:
    chosen = [d for d in DAYS if d in days]
    if len(chosen) == 7:
        return "Every day"
    if chosen == list(DAYS[:5]):
        return "Weekdays"
    if chosen == ["sat", "sun"]:
        return "Weekends"
    return ", ".join(DAY_LABELS[d] for d in chosen)


def describe(s: Mapping) -> str:
    kind = s.get("when_kind") or "days"
    if kind == "cron":
        return f"Custom: {s.get('cron')}"
    days = describe_days(s.get("days") or [])
    if kind == "interval":
        step = int(s.get("every_hours") or 1)
        start = _clock(f"{int(s.get('window_start') or 0):02d}:00")
        end = _clock(f"{int(s.get('window_end') or 0):02d}:00")
        return f"{days}, every {step} hour{'s' if step != 1 else ''} from {start} to {end}"
    return f"{days} at " + ", ".join(_clock(t) for t in (s.get("times") or []))


def scope(s: Mapping) -> str:
    parts = []
    if s.get("industries"):
        parts.append(", ".join(s["industries"]))
    if s.get("companies"):
        n = len(s["companies"])
        parts.append(f"{n} compan{'y' if n == 1 else 'ies'}" if n > 3 else ", ".join(s["companies"]))
    text = " + ".join(parts) or "all active companies"
    if s.get("sources"):
        text += " · " + ", ".join(s["sources"])
    return text


def fmt_when(dt: datetime | None) -> str:
    if dt is None:
        return ""
    now = datetime.now(dt.tzinfo)
    day = "today" if dt.date() == now.date() else ("tomorrow" if dt.date() == (now + timedelta(days=1)).date()
                                                  else dt.strftime("%a %b %d"))
    return f"{day} {_clock(dt.strftime('%H:%M'))}"


# --------------------------------------------------------------------------------------------- state & firing


def is_paused(engine) -> bool:  # noqa: ANN001
    return queries.get_state(engine, PAUSED_KEY) == "1"


def set_paused(rt: Runtime, paused: bool) -> None:
    from jobhunt.services import scheduler

    queries.set_state(rt.engine, PAUSED_KEY, "1" if paused else "0")
    scheduler.apply_schedules(rt)


def save(rt: Runtime, values: dict, schedule_id: int | None = None) -> int:
    from jobhunt.services import scheduler

    if schedule_id is None:
        schedule_id = queries.create_schedule(rt.engine, values)
    else:
        queries.update_schedule(rt.engine, schedule_id, values)
    scheduler.apply_schedules(rt)
    return schedule_id


def set_enabled(rt: Runtime, schedule_id: int, enabled: bool) -> None:
    from jobhunt.services import scheduler

    queries.set_schedule_enabled(rt.engine, schedule_id, enabled)
    scheduler.apply_schedules(rt)


def delete(rt: Runtime, schedule_id: int) -> None:
    from jobhunt.services import scheduler

    queries.delete_schedule(rt.engine, schedule_id)
    scheduler.apply_schedules(rt)


def fire(rt: Runtime, schedule_id: int, *, attempt: int = 0, manual: bool = False) -> int | None:
    """Start the run a schedule describes. Returns the run id, or None when skipped/deferred (reason recorded).

    `manual` (Run now) ignores enabled/paused and raises instead of retrying, so the page can show why."""
    from jobhunt.db.locks import LockHeld
    from jobhunt.services import runs, scheduler
    from jobhunt.services.companies import resolve_selection

    s = queries.get_schedule(rt.engine, schedule_id)
    if s is None or (not manual and (not s["enabled"] or is_paused(rt.engine))):
        return None
    record = lambda msg, **kw: queries.record_schedule_result(rt.engine, schedule_id, msg, **kw)  # noqa: E731
    companies = resolve_selection(rt.cfg, s["industries"], s["companies"])
    if (s["industries"] or s["companies"]) and not companies:
        record("skipped: none of its companies or industries are active any more")
        return None
    if s["mode"] == "stored" and not queries.job_ids_with_status(rt.engine, "discovered"):
        record("skipped: no saved jobs were waiting to be scored")
        return None
    try:
        run_id = runs.start_background(rt, trigger="schedule", sources=s["sources"] or None,
                                       max_to_score=s["max_to_score"], companies=companies,
                                       skip_fetch=s["mode"] == "stored", skip_score=s["mode"] == "fetch")
    except LockHeld as exc:
        if manual:
            raise
        if s["if_busy"] == "wait" and attempt < MAX_RETRIES:
            scheduler.retry_later(rt, schedule_id, attempt + 1)
            record(f"waiting: another run is in progress; retry {attempt + 1}/{MAX_RETRIES} in {RETRY_MINUTES} min")
        else:
            record(f"skipped: another run was in progress ({exc})")
        return None
    except Exception as exc:
        log.exception("schedule %s failed to start a run", schedule_id)
        record(f"failed to start: {exc}")
        if manual:
            raise
        return None
    record(f"started run #{run_id}" + (" (Run now)" if manual else ""), run_id=run_id)
    return run_id


def import_legacy(rt: Runtime) -> int | None:
    """One-time: an enabled `schedule:` block in config.yaml becomes a row, so nothing silently stops running."""
    legacy = rt.cfg.schedule
    if (not legacy.enabled or queries.get_state(rt.engine, "legacy_schedule_imported")
            or queries.list_schedules(rt.engine)):
        return None
    values = default_values(rt.cfg) | {
        "name": "Imported from config.yaml", "mode": legacy.mode, "when_kind": "cron", "cron": legacy.discovery_cron,
        "timezone": legacy.timezone, "companies": list(legacy.companies), "max_to_score": legacy.max_to_score,
        "notes": "Created from the old schedule: block in config.yaml. Manage schedules here now."}
    try:
        build_trigger(values)
    except ScheduleError as exc:
        log.warning("config.yaml schedule not imported: %s", exc)
        return None
    schedule_id = queries.create_schedule(rt.engine, values)
    queries.set_state(rt.engine, "legacy_schedule_imported", "1")
    return schedule_id
