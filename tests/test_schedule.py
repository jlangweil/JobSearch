"""Schedules managed from the UI: triggers (weekday semantics), firing modes, busy/catch-up behaviour, and the pages."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from jobhunt.db import locks, queries
from jobhunt.graphs import runtime
from jobhunt.services import schedules
from jobhunt.services.scheduler import apply_schedules, build_scheduler
from jobhunt.web.app import create_app
from tests.conftest import FakeLLM, FakeSource, raw_job
from tests.test_web_control import _wait, ui_cfg  # noqa: F401  (fixture re-export)

NY = ZoneInfo("America/New_York")
SATURDAY = datetime(2026, 9, 12, 8, 0, tzinfo=NY)


def _values(**kw) -> dict:  # noqa: ANN003
    base = {"name": "t", "enabled": True, "mode": "fetch", "when_kind": "days", "days": ["mon", "tue", "wed", "thu",
            "fri"], "times": ["07:00"], "every_hours": 3, "window_start": 8, "window_end": 17, "cron": "",
            "timezone": "America/New_York", "sources": [], "industries": [], "companies": [], "max_to_score": None,
            "if_busy": "wait", "catch_up": True, "notes": ""}
    return base | kw


def test_weekday_triggers_use_cron_meaning_not_apscheduler_numbering():
    days = [d.strftime("%a %H:%M") for d in schedules.upcoming(_values(), count=3, after=SATURDAY)]
    assert days == ["Mon 07:00", "Tue 07:00", "Wed 07:00"]
    cron = [d.strftime("%a") for d in schedules.upcoming(_values(when_kind="cron", cron="0 7 * * 1-5"), 5, SATURDAY)]
    assert cron == ["Mon", "Tue", "Wed", "Thu", "Fri"]  # APScheduler's from_crontab would say Tue-Sat
    assert [d.strftime("%a") for d in schedules.upcoming(_values(when_kind="cron", cron="30 6 * * 0,7"), 2,
                                                         SATURDAY)] == ["Sun", "Sun"]
    wrap = schedules.upcoming(_values(when_kind="cron", cron="0 9 * * fri-mon"), 4, SATURDAY)
    assert [d.strftime("%a") for d in wrap] == ["Sat", "Sun", "Mon", "Fri"]
    two = schedules.upcoming(_values(days=["sat"], times=["07:00", "18:30"]), 2, SATURDAY)
    assert [d.strftime("%H:%M") for d in two] == ["18:30", "07:00"]
    hourly = schedules.upcoming(_values(when_kind="interval", days=["mon"], every_hours=4, window_start=8,
                                        window_end=17), 4, SATURDAY)
    assert [d.strftime("%a %H") for d in hourly] == ["Mon 08", "Mon 12", "Mon 16", "Mon 08"]
    assert schedules.describe(_values(times=["07:00", "18:30"])) == "Weekdays at 7:00 AM, 6:30 PM"
    assert schedules.describe(_values(when_kind="interval", days=list(schedules.DAYS))) == \
        "Every day, every 3 hours from 8:00 AM to 5:00 PM"
    with pytest.raises(schedules.ScheduleError):
        schedules.build_trigger(_values(when_kind="cron", cron="0 7 * *"))
    with pytest.raises(schedules.ScheduleError, match="time zone"):
        schedules.build_trigger(_values(timezone="Mars/Olympus"))


def test_catch_up_finds_a_missed_run_only_after_enable():
    now = datetime(2026, 9, 14, 9, 0, tzinfo=NY)  # Monday 09:00, 07:00 run missed
    s = _values(created_at="2026-09-01T00:00:00Z", enabled_at="2026-09-01T00:00:00Z", last_fired_at=None)
    assert schedules.last_missed(s, now).strftime("%a %H:%M") == "Mon 07:00"
    assert schedules.last_missed(s | {"last_fired_at": "2026-09-14T11:05:00Z"}, now) is None  # 07:05 EDT
    assert schedules.last_missed(s | {"enabled_at": "2026-09-14T12:30:00Z"}, now) is None  # enabled 08:30


def _app(ui_cfg, hashing_embedder, jobs=None, llm=None):  # noqa: ANN001, ANN202, F811
    return create_app(ui_cfg, start_scheduler=False, runtime_factories={
        "llm_factory": lambda: llm or FakeLLM(), "embedder_factory": lambda: hashing_embedder,
        "sources_factory": lambda names: [FakeSource("workday", jobs or [])]})


def test_fetch_only_schedule_fires_run_linked_to_schedule(ui_cfg, hashing_embedder):  # noqa: F811
    llm = FakeLLM()
    jobs = [raw_job(1, title="Delivery Lead", company="Prudential", location="Remote (US)")]
    with TestClient(_app(ui_cfg, hashing_embedder, jobs, llm)):
        rt = runtime.get_runtime()
        sid = queries.create_schedule(rt.engine, _values(name="Pru fetch", companies=["prudential"]))
        run_id = schedules.fire(rt, sid)
        _wait(lambda: queries.get_run(rt.engine, run_id)["status"] != "running")
        run = queries.get_run(rt.engine, run_id)
        assert run["trigger"] == "schedule" and run["schedule_id"] == sid and run["status"] == "complete"
        assert run["counts"]["waiting_to_score"] == 1 and run["counts"]["left_to_score"] == 1 and llm.calls == 0
        s = queries.get_schedule(rt.engine, sid)
        assert s["last_run_id"] == run_id and s["last_result"].startswith("started run")
        assert queries.status_counts(rt.engine) == {"discovered": 1}
        assert queries.runs_for_schedule(rt.engine, sid)[0]["id"] == run_id

        stored = queries.create_schedule(rt.engine, _values(name="score", mode="stored"))
        queries.set_schedule_enabled(rt.engine, sid, False)
        assert schedules.fire(rt, sid) is None  # disabled: the trigger does nothing
        run2 = schedules.fire(rt, stored)
        _wait(lambda: queries.get_run(rt.engine, run2)["status"] != "running")
        assert queries.get_run(rt.engine, run2)["counts"]["scored"] == 1 and llm.calls == 1
        assert schedules.fire(rt, stored) is None
        assert "no saved jobs" in queries.get_schedule(rt.engine, stored)["last_result"]


def test_busy_schedule_waits_or_skips_and_pause_blocks_triggers(ui_cfg, hashing_embedder):  # noqa: F811
    with TestClient(_app(ui_cfg, hashing_embedder)):
        rt = runtime.get_runtime()
        build_scheduler(rt)
        wait = queries.create_schedule(rt.engine, _values(name="wait"))
        skip = queries.create_schedule(rt.engine, _values(name="skip", if_busy="skip"))
        apply_schedules(rt)
        assert {j.id for j in rt.scheduler.get_jobs()} >= {f"schedule:{wait}", f"schedule:{skip}"}
        locks.acquire(rt.engine, locks.DISCOVERY_LOCK, note="another run")
        try:
            assert schedules.fire(rt, wait) is None and schedules.fire(rt, skip) is None
            assert queries.get_schedule(rt.engine, wait)["last_result"].startswith("waiting")
            assert rt.scheduler.get_job(f"schedule:{wait}:retry") is not None
            assert queries.get_schedule(rt.engine, skip)["last_result"].startswith("skipped")
        finally:
            locks.release(rt.engine, locks.DISCOVERY_LOCK)
        schedules.set_paused(rt, True)
        assert rt.scheduler.get_job(f"schedule:{wait}") is None
        assert schedules.fire(rt, wait) is None  # paused
        schedules.set_paused(rt, False)
        assert rt.scheduler.get_job(f"schedule:{wait}") is not None


def test_schedule_pages_create_toggle_edit_run_now_delete(ui_cfg, hashing_embedder):  # noqa: F811
    jobs = [raw_job(1, title="Delivery Lead", company="Cigna", location="Remote (US)")]
    with TestClient(_app(ui_cfg, hashing_embedder, jobs)) as c:
        rt = runtime.get_runtime()
        build_scheduler(rt)
        page = c.get("/schedules").text
        assert "No schedules yet" in page and "Weekday morning fetch" in page
        assert "Nightly scoring" in c.get("/schedules/new?preset=nightly-scoring").text

        bad = c.post("/schedules", data={"name": "Morning", "mode": "fetch", "when_kind": "days", "times": "07:00"})
        assert bad.status_code == 422 and "Pick at least one day" in bad.text and 'value="Morning"' in bad.text

        form = {"name": "Morning", "enabled": "on", "mode": "fetch", "when_kind": "days", "days": ["mon", "wed"],
                "times": ["06:45", "17:00"], "timezone": "America/New_York", "industries": ["health"],
                "companies": ["prudential"], "if_busy": "skip", "catch_up": "on"}
        r = c.post("/schedules", data=form, follow_redirects=False)
        assert r.status_code == 303 and "enabled" in r.headers["location"]
        s = queries.list_schedules(rt.engine)[0]
        assert s["days"] == ["mon", "wed"] and s["times"] == ["06:45", "17:00"] and s["industries"] == ["health"]
        assert rt.scheduler.get_job(f"schedule:{s['id']}") is not None
        listing = c.get("/schedules").text
        assert "Mon, Wed at 6:45 AM, 5:00 PM" in listing and "Coming up" in listing

        preview = c.post("/schedules/preview", data=form | {"when_kind": "cron", "cron": "0 8 * * 1-5"}).text
        assert "Custom: 0 8 * * 1-5" in preview and "8:00 AM" in preview

        row = c.post(f"/schedules/{s['id']}/toggle", data={"enabled": "0"}, headers={"HX-Request": "true"})
        assert row.status_code == 200 and f'id="schedule-{s["id"]}"' in row.text and ">off<" in row.text
        assert rt.scheduler.get_job(f"schedule:{s['id']}") is None

        edit = c.get(f"/schedules/{s['id']}").text
        assert 'value="Morning"' in edit and "Recent runs from this schedule" in edit
        r = c.post(f"/schedules/{s['id']}", data=form | {"name": "Morning v2", "mode": "full", "max_to_score": "5"},
                   follow_redirects=False)
        assert r.status_code == 303
        assert queries.get_schedule(rt.engine, s["id"])["mode"] == "full"

        r = c.post(f"/schedules/{s['id']}/run", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/runs/")
        run_id = int(r.headers["location"].split("/runs/")[1].split("?")[0])
        _wait(lambda: queries.get_run(rt.engine, run_id)["status"] != "running")
        assert "Morning v2" in c.get("/runs").text  # run history names the schedule

        assert "Morning v2 (copy)" in c.get(f"/schedules/new?copy={s['id']}").text
        c.post(f"/schedules/{s['id']}/delete")
        assert queries.list_schedules(rt.engine) == []
        c.post("/schedules/pause", data={"paused": "1"})
        assert schedules.is_paused(rt.engine) and "All schedules are paused" in c.get("/schedules").text


def test_enabled_config_schedule_is_imported_once(ui_cfg, hashing_embedder):  # noqa: F811
    ui_cfg.schedule.enabled = True
    ui_cfg.schedule.discovery_cron = "15 6 * * 1-5"
    ui_cfg.schedule.mode = "fetch"
    with TestClient(_app(ui_cfg, hashing_embedder)):
        rt = runtime.get_runtime()
        build_scheduler(rt)
        build_scheduler(rt)
        rows = queries.list_schedules(rt.engine)
        assert len(rows) == 1 and rows[0]["cron"] == "15 6 * * 1-5" and rows[0]["enabled"]
        nxt = schedules.upcoming(rows[0], 1, SATURDAY)[0]
        assert nxt.strftime("%a %H:%M") == "Mon 06:15" and nxt - SATURDAY < timedelta(days=3)
