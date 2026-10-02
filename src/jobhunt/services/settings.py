"""Settings actions: scoring weights and backups."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from jobhunt.config import write_scoring_weights
from jobhunt.db import queries
from jobhunt.db.engine import utcnow_iso
from jobhunt.discovery.service import recompute_totals
from jobhunt.graphs.runtime import Runtime


class SettingsError(ValueError):
    pass


def update_weights(rt: Runtime, raw: Mapping[str, str]) -> int:
    weights: dict[str, float] = {}
    for key in queries.WEIGHT_KEYS:
        if key not in raw:  # a form without this field keeps the current weight
            weights[key] = float(rt.cfg.scoring.weights.get(key, 0))
            continue
        try:
            value = float(raw.get(key, ""))
        except ValueError as exc:
            raise SettingsError(f"{key} must be a number") from exc
        if value < 0:
            raise SettingsError(f"{key} must be >= 0")
        weights[key] = value
    if sum(weights.values()) <= 0:
        raise SettingsError("at least one weight must be positive")
    write_scoring_weights(rt.cfg, weights)
    return recompute_totals(rt.engine, rt.cfg)


def compact(rt: Runtime) -> tuple[float, float]:
    """VACUUM the database. Refused while a discovery run or any workflow step holds a lock."""
    held = [row["name"] for row in queries.list_locks(rt.engine) if not row["name"].startswith("stop:")]
    if held:
        raise SettingsError(f"busy ({', '.join(held)}) — try again when runs and workflow steps have finished")
    path = rt.cfg.paths.db
    before = path.stat().st_size / 1e6
    queries.compact(rt.engine)
    return before, path.stat().st_size / 1e6


def backup(rt: Runtime) -> Path:
    stamp = utcnow_iso()
    out = rt.cfg.paths.exports / f"jobhunt-backup-{stamp[:10].replace('-', '')}.db"
    if out.exists():
        out = out.with_name(f"jobhunt-backup-{stamp[:19].replace('-', '').replace(':', '').replace('T', '-')}.db")
    queries.vacuum_into(rt.engine, out)
    return out


# --------------------------------------------------------------------------------------------- language models

LLM_FIELDS = {
    "scoring_provider": ("scoring.provider", str), "tailoring_provider": ("tailoring.provider", str),
    "local_api": ("local_llm.api", str), "local_base_url": ("local_llm.base_url", str),
    "local_model": ("local_llm.model", str), "local_num_ctx": ("local_llm.num_ctx", int),
    "local_concurrency": ("local_llm.concurrency", int), "local_keep_alive": ("local_llm.keep_alive", str),
    "local_timeout_s": ("local_llm.timeout_s", float),
}


def update_llm(rt: Runtime, form: Mapping[str, str]) -> list[str]:
    from jobhunt.services import config_edit

    updates: dict[str, object] = {}
    for field, (dotted, kind) in LLM_FIELDS.items():
        raw = (form.get(field) or "").strip()
        if raw == "":
            continue
        try:
            updates[dotted] = kind(raw)
        except ValueError as exc:
            raise SettingsError(f"{field.replace('_', ' ')} must be a number") from exc
    try:
        return config_edit.set_values(rt, updates)
    except config_edit.ConfigError as exc:
        raise SettingsError(str(exc)) from exc


def start_llm_test(rt: Runtime) -> None:
    from jobhunt.llm.client import LocalLLM, check_local_model

    if rt.llm_test.get("status") == "running":
        return
    rt.llm_test.clear()
    rt.llm_test.update({"status": "running", "model": rt.cfg.local_llm.model, "base_url": rt.cfg.local_llm.base_url})

    def work() -> None:
        try:
            result = check_local_model(LocalLLM(rt.engine, lambda: rt.cfg.local_llm, rt.local_http_client))
        except Exception as exc:  # shown on the settings page
            result = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        rt.llm_test.update({**result, "status": "done"})

    rt.submit(work)



def set_scoring_provider(rt: Runtime, provider: str) -> str:
    """Top-bar switch. Claude needs the API key; the local model is checked when scoring starts."""
    from jobhunt.services import config_edit

    if provider not in ("local", "anthropic"):
        raise SettingsError("provider must be local or anthropic")
    if provider == "anthropic" and not rt.cfg.secrets.ANTHROPIC_API_KEY:
        raise SettingsError("Claude needs ANTHROPIC_API_KEY in .env (then restart jobhunt serve)")
    if rt.cfg.scoring.provider != provider:
        try:
            config_edit.set_values(rt, {"scoring.provider": provider})
        except config_edit.ConfigError as exc:
            raise SettingsError(str(exc)) from exc
    cfg = rt.cfg
    return f"local {cfg.local_llm.model}" if provider == "local" else f"Claude {cfg.scoring.model}"


def _phrases(raw: str) -> list[str]:
    import re

    out = [p.strip() for p in re.split(r"[\n,]+", raw or "") if p.strip()]
    return list(dict.fromkeys(out))[:200]


def update_focus(rt: Runtime, form: Mapping[str, str]) -> list[str]:
    from jobhunt.services import config_edit

    earlier = form.get("earlier_roles") if form.get("earlier_roles") in ("full", "titles") else "full"

    try:
        cap = int(str(form.get("avoid_cap") or "45"))
    except ValueError as exc:
        raise SettingsError("the cap must be a whole number") from exc
    if not 0 <= cap <= 100:
        raise SettingsError("the cap must be between 0 and 100")
    try:
        return config_edit.set_values(rt, {"focus.boost": _phrases(form.get("boost", "")),
                                           "focus.avoid": _phrases(form.get("avoid", "")), "focus.avoid_cap": cap,
                                           "scoring.earlier_roles": earlier})
    except config_edit.ConfigError as exc:
        raise SettingsError(str(exc)) from exc


def focus_preview(rt: Runtime, form: Mapping[str, str], limit: int = 12) -> dict:
    """What the unsaved focus lists would do to the current top jobs (pure; nothing is written)."""
    from jobhunt.discovery import focus as focus_mod
    from jobhunt.discovery import scoring

    boost, avoid = _phrases(form.get("boost", "")), _phrases(form.get("avoid", ""))
    try:
        cap = float(form.get("avoid_cap") or 45)
    except ValueError:
        cap = 45.0
    weights = rt.cfg.scoring.weights
    rows, _ = queries.list_queue(rt.engine, queries.QueueFilters(status="scored", min_score=0, sort="total",
                                                                 page_size=400))
    changes = []
    for r in rows:
        fit, hits = focus_mod.evaluate(r["title"], r.get("description_text"), boost, avoid)
        new_total = scoring.compute_total({**r, "focus_fit": fit}, weights, r.get("ghost_score") or 0,
                                          rt.cfg.scoring.ghost_penalty, cap if hits["avoid"] else None)
        changes.append({"id": r["id"], "title": r["title"], "company": r["company_name_raw"], "old": r["total"],
                        "new": new_total, "hits": hits})
    movers = sorted(changes, key=lambda c: abs((c["new"] or 0) - (c["old"] or 0)), reverse=True)[:limit]
    return {"movers": movers, "capped": sum(1 for c in changes if c["hits"]["avoid"]),
            "boosted": sum(1 for c in changes if c["hits"]["title"]), "checked": len(changes)}


# --------------------------------------------------------------------------------------------- adzuna

ADZUNA_CATEGORIES = {"": "any category", "it-jobs": "IT jobs", "consultancy-jobs": "Consultancy",
                     "engineering-jobs": "Engineering", "accounting-finance-jobs": "Accounting & finance",
                     "healthcare-nursing-jobs": "Healthcare"}


def adzuna_view(rt: Runtime) -> dict:
    scfg = rt.cfg.sources.get("adzuna")
    extra = (scfg.model_extra or {}) if scfg else {}
    key = rt.cfg.secrets.ADZUNA_APP_KEY or ""
    return {"enabled": bool(scfg and scfg.enabled), "app_id": rt.cfg.secrets.ADZUNA_APP_ID or "",
            "key_hint": f"saved (…{key[-4:]})" if key else "", "keys_set": bool(rt.cfg.secrets.ADZUNA_APP_ID and key),
            "queries": extra.get("queries") or [], "location": extra.get("location") or "",
            "distance_miles": extra.get("distance_miles") or "", "max_days_old": extra.get("max_days_old", 30),
            "pages": extra.get("pages", 2), "category": extra.get("category") or "",
            "salary_min": extra.get("salary_min") or "", "exclude_words": extra.get("exclude_words") or [],
            "title_only": bool(extra.get("title_only")), "categories": ADZUNA_CATEGORIES}


def update_adzuna(rt: Runtime, form: Mapping[str, str]) -> list[str]:
    import re

    from jobhunt.config import write_env
    from jobhunt.services import config_edit

    app_id = (form.get("app_id") or "").strip()
    app_key = (form.get("app_key") or "").strip() or (rt.cfg.secrets.ADZUNA_APP_KEY or "")
    if form.get("clear_keys") == "1":
        app_id = app_key = ""
    enabled = form.get("enabled") == "on"
    if enabled and not (app_id and app_key):
        raise SettingsError("add your Adzuna App ID and App key before enabling it (free at developer.adzuna.com)")

    def number(name: str, lo: int, hi: int, default: int | None) -> int | None:
        raw = (form.get(name) or "").strip().replace(",", "")
        if not raw:
            return default
        if not raw.isdigit() or not lo <= int(raw) <= hi:
            raise SettingsError(f"{name.replace('_', ' ')} must be a whole number from {lo} to {hi}")
        return int(raw)

    queries_ = [q.strip() for q in re.split(r"[\n,]+", form.get("queries") or "") if q.strip()]
    if enabled and not queries_:
        raise SettingsError("add at least one search term")
    env_path = (rt.cfg.config_path.parent if rt.cfg.config_path else Path(".")) / ".env"
    if app_id != (rt.cfg.secrets.ADZUNA_APP_ID or "") or app_key != (rt.cfg.secrets.ADZUNA_APP_KEY or ""):
        write_env(env_path, {"ADZUNA_APP_ID": app_id, "ADZUNA_APP_KEY": app_key})
    rt.cfg.secrets.ADZUNA_APP_ID, rt.cfg.secrets.ADZUNA_APP_KEY = app_id or None, app_key or None
    category = form.get("category") if form.get("category") in ADZUNA_CATEGORIES else ""
    updates = {
        "sources.adzuna.enabled": enabled, "sources.adzuna.queries": queries_,
        "sources.adzuna.location": (form.get("location") or "").strip(),
        "sources.adzuna.distance_miles": number("distance_miles", 1, 500, None),
        "sources.adzuna.max_days_old": number("max_days_old", 1, 90, 30),
        "sources.adzuna.pages": number("pages", 1, 10, 2), "sources.adzuna.category": category,
        "sources.adzuna.salary_min": number("salary_min", 0, 1_000_000, 0),
        "sources.adzuna.exclude_words": [w for w in re.split(r"[\s,]+", form.get("exclude_words") or "") if w],
        "sources.adzuna.title_only": form.get("title_only") == "on",
    }
    try:
        notes = config_edit.set_values(rt, updates)
    except config_edit.ConfigError as exc:
        raise SettingsError(str(exc)) from exc
    rt.cfg.secrets.ADZUNA_APP_ID, rt.cfg.secrets.ADZUNA_APP_KEY = app_id or None, app_key or None  # reload keeps .env
    return notes


def test_adzuna(rt: Runtime, client=None) -> dict:  # noqa: ANN001
    """One small search with the saved settings: proves the keys work and shows what the terms find."""
    from jobhunt.sources import adzuna
    from jobhunt.sources.registry import adzuna_source

    if not (rt.cfg.secrets.ADZUNA_APP_ID and rt.cfg.secrets.ADZUNA_APP_KEY):
        return {"ok": False, "message": "Add your App ID and App key first."}
    src = adzuna_source(rt.cfg, client=client)
    term = (src.queries or ["integration"])[0]
    try:
        data = src.search(term, 1, per_page=5)
    except adzuna.AdzunaAuthError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - shown on the page
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    samples = [f"{j.get('title')} · {(j.get('company') or {}).get('display_name')} · "
               f"{(j.get('location') or {}).get('display_name')}" for j in data.get("results", [])[:5]]
    return {"ok": True, "message": f"Keys work. “{term}” finds {data.get('count', 0):,} jobs with these settings.",
            "samples": samples}


# --------------------------------------------------------------------------------------------- resume tailoring


def update_tailoring(rt: Runtime, form: Mapping[str, str]) -> list[str]:
    from jobhunt.services import config_edit

    mode = form.get("resume_mode") if form.get("resume_mode") in ("complete", "focused") else "complete"

    def number(name: str, lo: int, hi: int) -> int:
        raw = (form.get(name) or "").strip()
        if not raw.isdigit() or not lo <= int(raw) <= hi:
            raise SettingsError(f"{name.replace('_', ' ')} must be a whole number from {lo} to {hi}")
        return int(raw)

    effort = form.get("effort") if form.get("effort") in ("low", "medium", "high") else "medium"
    layout = form.get("role_layout") if form.get("role_layout") in ("separate_lines", "one_line") \
        else "separate_lines"
    generation = (form.get("generation_mode") if form.get("generation_mode") in ("automated", "ats_super_match")
                  else rt.cfg.tailoring.generation_mode)
    updates = {"tailoring.resume_mode": mode, "tailoring.effort": effort, "tailoring.role_layout": layout,
               "tailoring.generation_mode": generation,
               "tailoring.max_bullets_per_role": number("max_bullets_per_role", 0, 40),
               "tailoring.max_bullet_chars": number("max_bullet_chars", 120, 600),
               "tailoring.facts_per_requirement": number("facts_per_requirement", 1, 20),
               "tailoring.max_draft_attempts": number("max_draft_attempts", 1, 5)}
    try:
        return config_edit.set_values(rt, updates)
    except config_edit.ConfigError as exc:
        raise SettingsError(str(exc)) from exc


# --------------------------------------------------------------------------------------------- job alert emails


def email_view(rt: Runtime) -> dict:
    from jobhunt.sources.email_alerts import DEFAULT_SENDERS

    e, s = rt.cfg.email, rt.cfg.secrets
    password = s.IMAP_PASSWORD or ""
    return {"enabled": e.enabled, "host": e.host or s.IMAP_HOST or "imap.gmail.com", "port": e.port,
            "folder": e.folder or "INBOX", "user": s.IMAP_USER or "",
            "password_hint": "saved" if password else "", "account_set": bool((e.host or s.IMAP_HOST) and s.IMAP_USER
                                                                              and password),
            "senders": list(e.senders) or list(DEFAULT_SENDERS), "using_defaults": not e.senders,
            "subject_patterns": list(e.subject_patterns), "max_age_days": e.max_age_days,
            "max_messages": e.max_messages}


def update_email(rt: Runtime, form: Mapping[str, str]) -> list[str]:
    import re

    from jobhunt.config import write_env
    from jobhunt.services import config_edit
    from jobhunt.sources.email_alerts import DEFAULT_SENDERS

    user = (form.get("user") or "").strip()
    password = (form.get("password") or "").strip() or (rt.cfg.secrets.IMAP_PASSWORD or "")
    host = (form.get("host") or "").strip()
    enabled = form.get("enabled") == "on"
    if enabled and not (host and user and password):
        raise SettingsError("mail server, address and app password are all needed before switching it on")

    def number(name: str, lo: int, hi: int) -> int:
        raw = (form.get(name) or "").strip()
        if not raw.isdigit() or not lo <= int(raw) <= hi:
            raise SettingsError(f"{name.replace('_', ' ')} must be a whole number from {lo} to {hi}")
        return int(raw)

    senders = [x.strip() for x in re.split(r"[\s,]+", form.get("senders") or "") if x.strip()]
    if sorted(senders) == sorted(DEFAULT_SENDERS):
        senders = []  # keep the built-in list, so it stays up to date
    env = (rt.cfg.config_path.parent if rt.cfg.config_path else Path(".")) / ".env"
    if (user, password, host) != (rt.cfg.secrets.IMAP_USER, rt.cfg.secrets.IMAP_PASSWORD, rt.cfg.secrets.IMAP_HOST):
        write_env(env, {"IMAP_HOST": host, "IMAP_USER": user, "IMAP_PASSWORD": password})
    rt.cfg.secrets.IMAP_HOST, rt.cfg.secrets.IMAP_USER = host or None, user or None
    rt.cfg.secrets.IMAP_PASSWORD = password or None
    updates = {"email.enabled": enabled, "email.host": host, "email.port": number("port", 1, 65535),
               "email.folder": (form.get("folder") or "INBOX").strip() or "INBOX", "email.senders": senders,
               "email.subject_patterns": [p.strip() for p in (form.get("subject_patterns") or "").split(",")
                                          if p.strip()],
               "email.max_age_days": number("max_age_days", 1, 90), "email.max_messages": number("max_messages", 10,
                                                                                                 2000)}
    try:
        notes = config_edit.set_values(rt, updates)
    except config_edit.ConfigError as exc:
        raise SettingsError(str(exc)) from exc
    rt.cfg.secrets.IMAP_HOST, rt.cfg.secrets.IMAP_USER = host or None, user or None  # reload re-reads .env
    rt.cfg.secrets.IMAP_PASSWORD = password or None
    return notes


def test_email(rt: Runtime, connect=None) -> dict:  # noqa: ANN001
    """Connect, read the last few days read-only, and report what would be imported."""
    from jobhunt.sources.email_alerts import check_account
    from jobhunt.sources.registry import email_account

    account = email_account(rt.cfg)
    if not (account.host and account.user and account.password):
        return {"ok": False, "message": "Fill in the mail server, address and app password first.", "samples": []}
    return check_account(account, connect=connect, days=min(account.max_age_days, 14))


def recheck_filtered(rt: Runtime, *, apply: bool = True) -> dict:
    """Settings → Re-check filtered jobs: apply today's filters to everything knocked out earlier."""
    from jobhunt.discovery.service import recheck_filtered as run_recheck

    return run_recheck(rt.engine, rt.cfg, apply=apply)


# --------------------------------------------------------------------------------------------- network access

LAN_HOST = "0.0.0.0"  # noqa: S104 - deliberate: reachable from other devices on your network


def lan_ips() -> list[str]:
    """This PC's addresses on the local network (what you'd type on the laptop)."""
    import socket

    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.1.1", 1))  # no packets are sent; just picks the outbound interface
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        ips |= {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def network_view(rt: Runtime) -> dict:
    web = rt.cfg.web
    ips = lan_ips()
    return {"host": web.host, "port": web.port, "lan": web.host not in ("127.0.0.1", "localhost"),
            "password_set": bool(rt.cfg.secrets.WEB_PASSWORD), "ips": ips,
            "url": f"http://{ips[0]}:{web.port}" if ips else f"http://<this-pc>:{web.port}",
            "firewall": (f'netsh advfirewall firewall add rule name="jobhunt {web.port}" dir=in action=allow '
                         f"protocol=TCP localport={web.port} profile=private")}


def update_network(rt: Runtime, form: Mapping[str, str]) -> list[str]:
    """Open the web app to your network (or close it again). A password is required while it is open."""
    from jobhunt.config import write_env
    from jobhunt.services import config_edit

    lan = form.get("lan") == "on"
    password = (form.get("web_password") or "").strip() or (rt.cfg.secrets.WEB_PASSWORD or "")
    if lan and not password:
        raise SettingsError("set a password first — the app holds your resume, contact details and job history, and "
                            "anything on your network could reach it otherwise")
    if len(password) > 0 and len(password) < 8:
        raise SettingsError("use a password of at least 8 characters")
    port = (form.get("port") or "").strip() or str(rt.cfg.web.port)
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise SettingsError("port must be a number from 1 to 65535")
    env = (rt.cfg.config_path.parent if rt.cfg.config_path else Path(".")) / ".env"
    if password != (rt.cfg.secrets.WEB_PASSWORD or ""):
        write_env(env, {"WEB_PASSWORD": password})
        rt.cfg.secrets.WEB_PASSWORD = password or None
    try:
        notes = config_edit.set_values(rt, {"web.host": LAN_HOST if lan else "127.0.0.1", "web.port": int(port)})
    except config_edit.ConfigError as exc:
        raise SettingsError(str(exc)) from exc
    rt.cfg.secrets.WEB_PASSWORD = password or None  # the reload re-reads .env
    return notes


# ------------------------------------------------------------------------------------------- auto-apply (beta)


def auto_apply_view(rt: Runtime) -> dict:
    from jobhunt.apply import secrets_store
    from jobhunt.services import autoapply

    a = rt.cfg.auto_apply
    return {
        "enabled": a.enabled, "headless": a.headless, "auto_submit": a.auto_submit,
        "stop_on_unknown": a.stop_on_unknown, "upload_cover_letter": a.upload_cover_letter,
        "account_email": a.account_email, "max_work_experience": a.max_work_experience,
        "create_accounts": a.create_accounts, "password_mode": a.password_mode,
        "fill_work_experience": a.fill_work_experience, "apply_mode": a.apply_mode,
        "read_verification_email": a.read_verification_email,
        "verification_wait_seconds": a.verification_wait_seconds,
        "mailbox_ready": autoapply.mailbox_ready(rt.cfg),
        "slow_mo_ms": a.slow_mo_ms, "timeout_seconds": a.timeout_seconds,
        "credentials": queries.list_credentials(rt.engine),
        "store_passwords": rt.cfg.auto_apply.store_passwords,
        "can_store": secrets_store.available(),
        "browser_ready": _browser_ready(),
        "profile_path": str(rt.cfg.paths.apply_profile),
        "open_questions": len(queries.list_apply_questions(rt.engine, unanswered_only=True)),
        "recent_runs": queries.list_apply_runs(rt.engine, limit=5),
        "profile": autoapply.profile_for(rt.cfg),
    }


def _browser_ready() -> str:
    """Empty when Playwright and its Chromium are both installed; otherwise the command that fixes it."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "uv pip install playwright"
    try:
        with sync_playwright() as pw:
            return "" if Path(pw.chromium.executable_path).exists() else "uv run playwright install chromium"
    except Exception:
        return "uv run playwright install chromium"


def update_auto_apply(rt: Runtime, form: Mapping[str, str]) -> list[str]:
    """Switches for the beta. `auto_submit` is the one that actually sends applications, so it is refused unless
    the feature is on and a sign-in exists."""
    from jobhunt.services import config_edit

    enabled = form.get("enabled") == "on"
    auto_submit = form.get("auto_submit") == "on"
    if auto_submit and not enabled:
        raise SettingsError("turn auto-apply on before letting it submit")
    if auto_submit and not queries.list_credentials(rt.engine):
        raise SettingsError("save a Workday sign-in first")

    def number(name: str, lo: int, hi: int) -> int:
        raw = (form.get(name) or "").strip()
        if not raw.lstrip("-").isdigit() or not lo <= int(raw) <= hi:
            raise SettingsError(f"{name.replace('_', ' ')} must be a whole number from {lo} to {hi}")
        return int(raw)

    updates = {
        "auto_apply.enabled": enabled,
        "auto_apply.auto_submit": auto_submit,
        "auto_apply.headless": form.get("headless") == "on",
        "auto_apply.stop_on_unknown": form.get("stop_on_unknown") == "on",
        "auto_apply.upload_cover_letter": form.get("upload_cover_letter") == "on",
        "auto_apply.account_email": (form.get("account_email") or "").strip(),
        "auto_apply.create_accounts": form.get("create_accounts") == "on",
        "auto_apply.fill_work_experience": form.get("fill_work_experience") == "on",
        "auto_apply.apply_mode": "autofill" if form.get("apply_mode") == "autofill" else "manual",
        "auto_apply.password_mode": "shared" if form.get("password_mode") == "shared" else "per_site",
        "auto_apply.store_passwords": ("plain" if form.get("store_passwords") == "plain" else "encrypted"),
        "auto_apply.read_verification_email": form.get("read_verification_email") == "on",
        "auto_apply.max_work_experience": number("max_work_experience", 1, 20),
        "auto_apply.timeout_seconds": number("timeout_seconds", 10, 300),
        "auto_apply.slow_mo_ms": number("slow_mo_ms", 0, 2000),
    }
    try:
        return config_edit.set_values(rt, updates)
    except config_edit.ConfigError as exc:
        raise SettingsError(str(exc)) from exc
