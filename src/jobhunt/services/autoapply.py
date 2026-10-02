"""Auto-apply (beta) orchestration: gather what a run needs, drive the browser on the background executor,
and record everything it did.

Everything that touches the database or the config lives here; `apply/workday.py` only knows about a page.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from jobhunt.apply import secrets_store, workday
from jobhunt.apply.profile import ApplyProfile, load_profile, normalize, save_profile, seed_from_contact
from jobhunt.config import AppConfig
from jobhunt.db import locks, queries
from jobhunt.db.engine import iso_shift, utcnow_iso
from jobhunt.facts import bank
from jobhunt.graphs.application.nodes import FOLLOW_UP_DAYS
from jobhunt.graphs.runtime import Runtime

log = logging.getLogger(__name__)
LOCK = "auto_apply"           # one browser session at a time: two runs would fight over the same tenant login


class AutoApplyError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------- profile & credentials


def profile_for(cfg: AppConfig) -> ApplyProfile:
    """The stored profile, pre-filled from facts.yaml `contact:` the first time so it is not empty."""
    path = cfg.paths.apply_profile
    if not path.exists():
        profile = seed_from_contact(bank.contact(cfg))
        save_profile(path, profile)
        return profile
    return load_profile(path)


def save_profile_text(cfg: AppConfig, profile: ApplyProfile) -> Path:
    return save_profile(cfg.paths.apply_profile, profile)


def save_credential(rt: Runtime, *, host: str, username: str, password: str, notes: str | None = None) -> None:
    host = (host or "").strip().lower() or queries.ANY_HOST
    if not username.strip():
        raise AutoApplyError("a username (the email you sign in with) is required")
    enc = secrets_store.protect(password, mode=rt.cfg.auto_apply.store_passwords) if password else None
    queries.upsert_credential(rt.engine, host=host, username=username.strip(), password_enc=enc, notes=notes)


def credential_for(rt: Runtime, host: str) -> workday.Credential:
    """The account to sign in with. A site you have used before has its own row; a new one borrows the shared
    '*' username, and in per-site mode gets a freshly generated password that is saved once the account exists."""
    row = queries.credential_for(rt.engine, host)
    if not row:
        raise AutoApplyError(f"no sign-in saved for {host}. Add one on the Auto-apply page (a single '*' entry "
                             "covers every Workday site if you reuse one account).")
    try:
        password = secrets_store.password_for(row.get("password_enc"))
    except secrets_store.CredentialError as exc:
        raise AutoApplyError(str(exc)) from exc
    return workday.Credential(row["username"], password)


def account_saver(rt: Runtime, host: str, on_event):  # noqa: ANN001, ANN201
    """Remember an account the run just created, so the next application on that tenant only has to sign in."""

    def save(username: str, password: str) -> None:
        try:
            queries.upsert_credential(rt.engine, host=host, username=username,
                                      password_enc=secrets_store.protect(
                                          password, mode=rt.cfg.auto_apply.store_passwords),
                                      ats_type="workday", notes="created by auto-apply")
            on_event("account_created", host, f"signed up as {username}; password saved (encrypted)")
        except Exception as exc:   # the account exists either way: say so loudly rather than losing the password
            log.exception("could not save the credential for %s", host)
            on_event("account_created", host,
                     f"signed up as {username}, but the password could not be saved: {exc}")

    return save


# --------------------------------------------------------------------------------------- new tenants


PASSWORD_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"
PASSWORD_DIGITS = "23456789"
PASSWORD_SYMBOLS = "!@#$%*?-_"


def generate_password(length: int = 18) -> str:
    """A password that satisfies every Workday tenant policy we have seen: upper, lower, digit, symbol, long.
    You never need to type it — it is saved encrypted the moment the account is created."""
    import secrets

    pools = (PASSWORD_ALPHABET.lower(), PASSWORD_ALPHABET.upper(), PASSWORD_DIGITS, PASSWORD_SYMBOLS)
    chars = [secrets.choice(pool) for pool in pools]
    everything = "".join(pools)
    chars += [secrets.choice(everything) for _ in range(max(length - len(chars), 4))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def credentials_view(rt: Runtime) -> list[dict]:
    """The saved sign-ins, each with the password itself when it can be read back — which is the point of
    storing them plainly: a password jobhunt generated for a new site is no use if nobody can ever see it."""
    rows = []
    for row in queries.list_credentials_with_secret(rt.engine):
        blob = row.pop("password_enc", None)
        row["has_password"] = bool(blob)
        row["plain"] = secrets_store.is_plain(blob)
        # only a plainly-stored password is displayed: an encrypted one is readable on this machine, but showing
        # it on a page that is reachable over the network would undo the reason for encrypting it
        row["password"] = secrets_store.reveal(blob) if row["plain"] else ""
        rows.append(row)
    return rows


def account_plan(rt: Runtime, host: str) -> dict:
    """How a run will get in: the account it will use, and whether it has to be created first."""
    row = queries.credential_for(rt.engine, host)
    own = bool(row and row.get("host") == host)
    return {
        "username": (row or {}).get("username", ""),
        "shared": bool(row) and not own,
        "known_site": own,
        "will_create": bool(row) and not own,
        "per_site_password": rt.cfg.auto_apply.password_mode == "per_site",
        "mailbox_ready": mailbox_ready(rt.cfg),
    }


def mailbox_advice(error: str) -> str:
    """Turn a mail-server refusal into the thing to actually do about it."""
    if "application-specific password" in error.lower() or "app password" in error.lower():
        return (f"{error} — Gmail will not accept your account password over IMAP. Create a 16-character app "
                "password (Google Account → Security → 2-Step Verification → App passwords) and save that on "
                "Settings → Job alert emails.")
    if "authenticationfailed" in error.lower() or "invalid credentials" in error.lower():
        return f"{error} — check the address and password on Settings → Job alert emails."
    return error


def mailbox_ready(cfg: AppConfig) -> bool:
    """A new tenant usually emails a verification code; we can only read it if the mailbox is set up."""
    s = cfg.secrets
    return bool((cfg.email.host or s.IMAP_HOST) and s.IMAP_USER and s.IMAP_PASSWORD)


def _email_account(cfg: AppConfig):  # noqa: ANN202
    from jobhunt.sources.email_alerts import EmailAccount

    s = cfg.secrets
    return EmailAccount(host=cfg.email.host or s.IMAP_HOST or "", user=s.IMAP_USER or "",
                        password=s.IMAP_PASSWORD or "", port=cfg.email.port or 993,
                        folder=cfg.email.folder or "INBOX")


def verification_reader(rt: Runtime, host: str, on_event) -> Callable[[], str | None] | None:  # noqa: ANN001
    """A callable the driver uses to fetch the emailed code, or None when no mailbox is connected."""
    from datetime import UTC, datetime

    from jobhunt.apply import verify_email

    if not (rt.cfg.auto_apply.read_verification_email and mailbox_ready(rt.cfg)):
        return None
    account = _email_account(rt.cfg)
    wait = rt.cfg.auto_apply.verification_wait_seconds

    def read() -> str | None:
        asked_at = datetime.now(UTC)
        on_event("verification", "mailbox", f"watching {account.user} for up to {wait}s")
        try:
            code = verify_email.wait_for_code(account, asked_at, host_hint=host, timeout_s=wait)
        except verify_email.VerificationError as exc:
            on_event("verification", "mailbox", str(exc))
            raise AutoApplyError(mailbox_advice(str(exc))) from exc
        on_event("verification", "mailbox", "code found" if code else "no code arrived in time")
        return code

    return read


def link_reader(rt: Runtime, host: str, on_event):  # noqa: ANN001, ANN201
    """Reads the activation link some tenants email instead of a code."""
    from datetime import UTC, datetime

    from jobhunt.apply import verify_email

    if not (rt.cfg.auto_apply.read_verification_email and mailbox_ready(rt.cfg)):
        return None
    account = _email_account(rt.cfg)
    wait = rt.cfg.auto_apply.verification_wait_seconds

    def read() -> str | None:
        asked_at = datetime.now(UTC)
        on_event("verification", "mailbox", f"watching {account.user} for an activation link (up to {wait}s)")
        try:
            link = verify_email.wait_for_link(account, asked_at, host_hint=host, timeout_s=wait)
        except verify_email.VerificationError as exc:
            on_event("verification", "mailbox", str(exc))
            raise AutoApplyError(mailbox_advice(str(exc))) from exc
        on_event("verification", "mailbox", "link found" if link else "no activation email arrived in time")
        return link

    return read


# --------------------------------------------------------------------------------------- run inputs


def experience_entries(cfg: AppConfig, limit: int) -> tuple[list[dict], list[dict]]:
    """Work history and education straight from the fact bank, newest first — the same source as the resume,
    so what Workday stores matches what you uploaded."""
    try:
        ff = bank.load_yaml(cfg.paths.facts)
    except bank.FactBankError:
        return [], []
    roles, schools = [], []
    for f in ff.facts:
        if f.kind == "role":
            roles.append({
                "title": _plain(f.title or ""), "org": f.org or "", "location": _location_tag(f.tags),
                "start": _mmyyyy(f.start_date), "end": _mmyyyy(f.end_date), "text": (f.text or "")[:1900],
            })
        elif f.kind == "education":
            # a form asks for these as three separate things: the school, the *level* of the degree (a dropdown
            # of "Bachelor's Degree", "Master's Degree"), and the subject
            schools.append({"org": f.org or "", "school": f.org or "", "title": _plain(f.title or ""),
                            "degree": _degree_name(f.title or "") or _degree_level(f.title or ""),
                            "degree_candidates": _degree_candidates(f.title or ""),
                            "degree_full": _plain(f.title or ""),
                            # education From/To are year boxes; a fact that carries dates fills them
                            "start": _mmyyyy(f.start_date), "end": _mmyyyy(f.end_date),
                            "field_of_study": _field_of_study(f.title or "")})
    roles.sort(key=lambda r: r["start"][-4:] + r["start"][:2], reverse=True)
    return roles[:limit], schools


def _plain(title: str) -> str:
    """Workday's own fields do not want a dash-joined title any more than its resume parser does."""
    return re.sub(r"\s+[–—-]\s+", ", ", title).strip()


def _mmyyyy(value: object) -> str:
    s = str(value or "").strip()
    if s.lower() in ("present", "current", ""):
        return ""
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})", s):
        return f"{int(m.group(2)):02d}/{m.group(1)}"
    if re.fullmatch(r"\d{4}", s):
        return s
    return s


def _location_tag(tags: Sequence[str]) -> str:
    """Role facts tag their office as "clinton nj" / "roseland nj"; title-case it back into a location."""
    for t in tags or []:
        if m := re.fullmatch(r"([a-z .'-]+)\s+([a-z]{2})", str(t).strip(), re.IGNORECASE):
            return f"{m.group(1).title().strip()}, {m.group(2).upper()}"
    return ""


DEGREE_LEVELS = (
    (r"\bph\.?d\b|\bdoctor|\bdba\b|\bedd\b", "Doctorate"),
    (r"\bm\.?b\.?a\b|\bmaster|\bm\.?s\.?\b|\bm\.?a\.?\b|\bmsc\b|\bm\.?eng\b", "Master's Degree"),
    (r"\bbachelor|\bb\.?s\.?\b|\bb\.?a\.?\b|\bbsc\b|\bb\.?eng\b", "Bachelor's Degree"),
    (r"\bassociate|\ba\.?a\.?\b|\ba\.?s\.?\b", "Associate's Degree"),
    (r"\bdiploma|\bcertificate\b", "Certificate"),
    (r"\bhigh school|\bged\b", "High School"),
)


DEGREE_NAMES = (
    (r"\bmaster of science\b|\bm\.?s\.?\b|\bmsc\b", "Master of Science", ("MS", "M.S.", "Master's Degree")),
    (r"\bmaster of arts\b|\bm\.?a\.?\b", "Master of Arts", ("MA", "M.A.", "Master's Degree")),
    (r"\bm\.?b\.?a\b|master of business", "Master of Business Administration", ("MBA", "Master's Degree")),
    (r"\bbachelor of science\b|\bb\.?s\.?\b|\bbsc\b", "Bachelor of Science",
     ("BS", "B.S.", "Bachelor's Degree")),
    (r"\bbachelor of arts\b|\bb\.?a\.?\b", "Bachelor of Arts", ("BA", "B.A.", "Bachelor's Degree")),
    (r"\bph\.?d\b|doctor of philosophy", "Doctor of Philosophy", ("PhD", "Ph.D.", "Doctorate")),
    (r"\bassociate\b", "Associate of Science", ("AS", "Associate's Degree")),
)


def _degree_name(title: str) -> str:
    """The degree as a dropdown lists it: "Bachelor of Science", not "Bachelor's Degree"."""
    for pattern, name, _ in DEGREE_NAMES:
        if re.search(pattern, title or "", re.IGNORECASE):
            return name
    return ""


def _degree_candidates(title: str) -> list[str]:
    """Every spelling worth trying in a Degree dropdown, best first — tenants abbreviate these differently."""
    out: list[str] = []
    for pattern, name, aliases in DEGREE_NAMES:
        if re.search(pattern, title or "", re.IGNORECASE):
            out = [name, *aliases]
            break
    level = _degree_level(title)
    for extra in (level, _plain(title)):
        if extra and extra not in out:
            out.append(extra)
    return out


def _degree_level(title: str) -> str:
    """The level a Degree dropdown lists, out of the degree's full name: "Master of Science (MS) Information
    Systems" is a Master's Degree. Passing the whole title matches no option at all."""
    text = (title or "").lower()
    for pattern, level in DEGREE_LEVELS:
        if re.search(pattern, text):
            return level
    return ""


def _field_of_study(title: str) -> str:
    """"Master of Science (MS) Information Systems" -> "Information Systems"; "MBA" -> "" (no subject named)."""
    cleaned = re.sub(r"\([^)]*\)", " ", title or "")                     # drop "(MS)", "(BS)"
    cleaned = re.sub(r"\b(bachelor|master|doctor)[a-z]*(?:'s)?\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(ph\.?d|mba|m\.?s|b\.?s|m\.?a|b\.?a|msc|bsc|associate|degree|diploma)\b", " ",
                     cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bof\s+(science|arts|engineering|business administration|business)\b", " ", cleaned,
                     flags=re.IGNORECASE)
    cleaned = re.split(r",", cleaned)[0]
    cleaned = re.sub(r"\b(in|of)\b", " ", cleaned, flags=re.IGNORECASE)
    return " ".join(cleaned.split()).strip()


SKILL_SPLIT = re.compile(r"\s*[,;]\s*")
# a list often starts with its own heading: "Methodologies: Agile, Scrum"
SKILL_LABEL = re.compile(r"^[A-Za-z][A-Za-z /&+-]{2,30}:\s*")


def skill_list(cfg: AppConfig, limit: int = 20) -> list[str]:
    """Skills to offer a form's skills picker, taken from the fact bank in the order you wrote them.

    Skill facts hold comma-separated lists ("Team Leadership, Enterprise Integration, API Architecture"), which
    is exactly what these pickers want one at a time. Role tags fill in behind them. Slashes and "and" are left
    alone: "CI/CD" and "Code Quality and Reviews" are single terms, not lists.
    """
    try:
        ff = bank.load_yaml(cfg.paths.facts)
    except bank.FactBankError:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        phrase = SKILL_LABEL.sub("", " ".join(str(value or "").split())).strip(" .")
        if not phrase or len(phrase) < 2 or len(phrase) > 40 or phrase.lower() in seen:
            return
        seen.add(phrase.lower())
        out.append(phrase)

    for f in ff.facts:
        if f.kind == "skill":
            for part in SKILL_SPLIT.split(f.text or ""):
                add(part)
    for f in ff.facts:                       # then the tags, which name tools and platforms
        if f.kind in ("skill", "role"):
            for tag in f.tags or []:
                add(str(tag).title() if str(tag).islower() else str(tag))
    return out[:limit]


def documents_for(rt: Runtime, job_id: int) -> tuple[Path | None, Path | None]:
    """The newest rendered resume and cover letter for this job."""
    best: dict[str, Path] = {}
    for doc in queries.documents_for_job(rt.engine, job_id):          # already ordered kind, version DESC
        if doc["kind"] in best:
            continue
        # `file_paths` is the decoded list; `file_path` is the raw JSON text, and iterating *that* walks it one
        # character at a time — on Windows Path("\\").exists() is true, so it "found" a resume called "\"
        for candidate in doc.get("file_paths") or []:
            path = Path(candidate)
            if path.exists():
                best[doc["kind"]] = path
                break
    return best.get("resume"), best.get("cover_letter")


def preflight(rt: Runtime, job_id: int) -> dict:
    """What a run would use, and what is missing — rendered as the confirmation panel before you start one."""
    cfg = rt.cfg
    job = queries.get_job(rt.engine, job_id)
    if not job:
        raise AutoApplyError(f"job {job_id} not found")
    url = job.get("apply_url") or job.get("url") or ""
    host = workday.tenant_host(url)
    resume, cover = documents_for(rt, job_id)
    cred = queries.credential_for(rt.engine, host)
    blockers = []
    if not cfg.auto_apply.enabled:
        blockers.append("auto-apply is switched off (Settings → Auto-apply)")
    if not workday.is_workday(url):
        blockers.append(f"this posting is not on Workday ({host or 'no apply URL'}); only Workday is supported so far")
    if not cred:
        blockers.append(f"no sign-in saved for {host or 'this site'} — add one (use '*' to reuse one account "
                        "on every Workday site)")
    if not resume:
        blockers.append("no rendered resume for this job — run the tailoring workflow first")
    roles, schools = experience_entries(cfg, cfg.auto_apply.max_work_experience)
    return {
        "job": job, "host": host, "apply_url": url, "resume": str(resume) if resume else None,
        "cover_letter": str(cover) if cover else None, "credential": cred, "blockers": blockers,
        "roles": roles, "education": schools, "profile": profile_for(cfg),
        "skills": skill_list(cfg, cfg.auto_apply.max_skills),
        "open_questions": queries.list_apply_questions(rt.engine, unanswered_only=True),
        "auto_submit": cfg.auto_apply.auto_submit, "headless": cfg.auto_apply.headless,
        "account": account_plan(rt, host),
        "fill_work_experience": cfg.auto_apply.fill_work_experience,
        "apply_mode": cfg.auto_apply.apply_mode,
        "runs": queries.list_apply_runs(rt.engine, job_id=job_id, limit=5),
    }


# --------------------------------------------------------------------------------------- the run itself


def start(rt: Runtime, job_id: int) -> int:
    """Queue a run on the background executor. Returns the apply_runs id immediately."""
    pre = preflight(rt, job_id)
    if pre["blockers"]:
        raise AutoApplyError("; ".join(pre["blockers"]))
    cred = credential_for(rt, pre["host"])
    run_id = queries.start_apply_run(rt.engine, job_id=job_id, host=pre["host"])
    locks.acquire(rt.engine, LOCK, note=f"auto-apply job {job_id}", stale_minutes=30)
    rt.executor.submit(_work, rt, run_id, job_id, pre, cred)
    return run_id


def _work(rt: Runtime, run_id: int, job_id: int, pre: dict, cred: workday.Credential) -> None:
    from playwright.sync_api import sync_playwright

    cfg = rt.cfg
    events: list[dict] = []
    shots: list[str] = []
    shot_dir = cfg.paths.apply_artifacts / f"job-{job_id}" / f"run-{run_id}"

    def event(kind: str, label: str, detail: str = "") -> None:
        events.append({"at": utcnow_iso(), "kind": kind, "label": label[:200], "detail": str(detail)[:400]})
        queries.update_apply_run(rt.engine, run_id, events=events)

    def unknown(label: str, kind: str, options: Sequence[str], stage: str) -> None:
        queries.add_apply_question(rt.engine, run_id=run_id, job_id=job_id, host=pre["host"], stage=stage,
                                   label=label, norm_label=normalize(label), kind=kind, options=list(options))

    ctx = workday.ApplyContext(
        profile=pre["profile"], credential=cred,
        resume_path=Path(pre["resume"]) if pre["resume"] else None,
        cover_letter_path=Path(pre["cover_letter"]) if pre["cover_letter"] and cfg.auto_apply.upload_cover_letter
        else None,
        experience=pre["roles"], education=pre["education"], skills=pre.get("skills") or [],
        stored_answers=queries.stored_question_answers(rt.engine),
        auto_submit=cfg.auto_apply.auto_submit, stop_on_unknown=cfg.auto_apply.stop_on_unknown,
        max_work_experience=cfg.auto_apply.max_work_experience,
        wait_for_human_seconds=0 if cfg.auto_apply.headless else cfg.auto_apply.wait_for_human_seconds,
        create_accounts=cfg.auto_apply.create_accounts,
        known_account=bool(pre["account"].get("known_site")),
        fill_work_experience=cfg.auto_apply.fill_work_experience,
        apply_mode=cfg.auto_apply.apply_mode,
        strong_password=generate_password if cfg.auto_apply.password_mode != "shared" else None,
        prefer_generated_password=cfg.auto_apply.password_mode != "shared",
        verification_code=verification_reader(rt, pre["host"], event),
        verification_link=link_reader(rt, pre["host"], event),
        on_account_created=account_saver(rt, pre["host"], event),
        on_event=event, on_unknown=unknown,
    )
    status, error, result = "failed", None, {}
    recorded = False

    def finish() -> None:
        """Write the outcome and let go of the lock. Safe to call twice; the second call does nothing."""
        nonlocal recorded
        if recorded:
            return
        recorded = True
        totals = result.get("totals") or {}
        queries.update_apply_run(
            rt.engine, run_id, status=status, stage=result.get("stage") or "", error=error,
            submitted=1 if result.get("submitted") else 0,
            filled=int(totals.get("filled", 0)), unknown=int(totals.get("unknown", 0) + totals.get("no_option", 0)),
            events=events, screenshots=shots)
        queries.touch_credential(rt.engine, pre["host"])
        locks.release(rt.engine, LOCK)
        if result.get("submitted"):
            _record_applied(rt, job_id, run_id)

    try:
        shot_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            args = [
                # Chrome's own password/autofill bubbles can cover a submit button or overwrite a password box
                "--disable-features=PasswordManagerEnabled,AutofillServerCommunication,"
                "PasswordGeneration,AutofillEnableAccountWalletStorage",
                "--disable-save-password-bubble", "--no-default-browser-check", "--no-first-run",
            ]
            if not cfg.auto_apply.headless:
                args.append("--start-maximized")
            browser = pw.chromium.launch(headless=cfg.auto_apply.headless,
                                         slow_mo=cfg.auto_apply.slow_mo_ms or 0, args=args)
            # A visible run uses the real window (no_viewport), so a wide monitor means a wide page and fewer
            # fields parked below the fold; a headless one gets a tall window of its own.
            context = (browser.new_context(accept_downloads=False, no_viewport=True)
                       if not cfg.auto_apply.headless
                       else browser.new_context(accept_downloads=False,
                                                viewport={"width": 1600, "height": 1200}))
            context.set_default_timeout(cfg.auto_apply.timeout_seconds * 1000)
            page = context.new_page()

            def shot(name: str) -> None:
                if not cfg.auto_apply.keep_screenshots:
                    return
                safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name)[:60]
                path = shot_dir / f"{len(shots):02d}-{safe}.png"
                try:
                    page.screenshot(path=str(path), full_page=True)
                    shots.append(str(path))
                    queries.update_apply_run(rt.engine, run_id, screenshots=shots)
                except Exception as exc:                        # a screenshot is never worth failing a run over
                    log.debug("screenshot failed: %s", exc)

            try:
                ctx.on_screenshot = shot          # so a pause can show you what it is looking at
                result = workday.run(page, ctx, pre["apply_url"], shot=shot)
                status = "submitted" if result.get("submitted") else "review"
            except workday.ApplyStopped as stop:
                status = ("needs_input" if stop.reason in ("needs_input", "captcha", "verification")
                          else "failed")
                error = str(stop)
                result = {"stage": stop.stage, "totals": {}}
                event("stopped", stop.reason, str(stop))
                shot(f"stopped-{stop.reason}")
            finally:
                # A run that did not finish leaves its window open: the page as it stands is the only way to
                # see what stopped it, and a screenshot cannot be scrolled, inspected or taken over.
                if not cfg.auto_apply.headless and status in ("failed", "needs_input", "review"):
                    minutes = cfg.auto_apply.keep_open_minutes
                    event("waiting", "browser",
                          f"leaving this window open for up to {minutes} min — close it when you are done")
                    finish()                       # record the outcome now; do not hold the run or the lock
                    _wait_until_closed(page, minutes)
                with suppress(Exception):
                    context.close()
                    browser.close()
    except Exception as exc:
        if "TargetClosed" in type(exc).__name__ or "has been closed" in str(exc):
            status, error = "needs_input", "the browser window was closed, so the run stopped there"
        else:
            log.exception("auto-apply run %s failed", run_id)
            status, error = "failed", f"{type(exc).__name__}: {exc}"
    finally:
        finish()


def _wait_until_closed(page, minutes: int) -> None:  # noqa: ANN001
    """Hold the browser open until you close the window (or the time runs out)."""
    deadline = time.monotonic() + max(minutes, 0) * 60
    while time.monotonic() < deadline:
        try:
            if page.is_closed():
                return
            page.wait_for_timeout(2000)
        except Exception:      # the window went away, which is exactly the signal we are waiting for
            return


def _record_applied(rt: Runtime, job_id: int, run_id: int) -> None:
    """A submitted application goes into the applications tracker like any other."""
    try:
        docs = [d["id"] for d in queries.documents_for_job(rt.engine, job_id) if d.get("file_path")]
        queries.record_application(
            rt.engine, job_id=job_id, method="auto_apply", confirmation_ref=None, document_ids=docs,
            follow_up_at=iso_shift(FOLLOW_UP_DAYS),
            notes=f"submitted by auto-apply run {run_id} on {datetime.now(UTC):%Y-%m-%d}")
    except Exception as exc:       # the application went in; a tracker hiccup must not look like a failed apply
        log.warning("auto-apply submitted job %s but could not record it: %s", job_id, exc)
        queries.update_apply_run(rt.engine, run_id, error=f"submitted, but not recorded: {exc}")


# --------------------------------------------------------------------------------------- answering questions


def answer_question(rt: Runtime, question_id: int, answer: str, *, save_to_profile: bool = True) -> dict:
    """Store an answer. It is reused by label from then on, so each question is only ever asked once."""
    row = queries.answer_apply_question(rt.engine, question_id, answer)
    if row and save_to_profile:
        profile = profile_for(rt.cfg)
        label = row["label"]
        if not any(normalize(q.match) == normalize(label) for q in profile.questions):
            from jobhunt.apply.profile import CustomAnswer
            profile.questions.append(CustomAnswer(match=label, answer=answer))
            save_profile_text(rt.cfg, profile)
    return row or {}


def status_view(rt: Runtime, run_id: int) -> dict:
    run = queries.get_apply_run(rt.engine, run_id)
    if not run:
        raise AutoApplyError(f"run {run_id} not found")
    run["questions"] = queries.list_apply_questions(rt.engine, run_id=run_id)
    run["open_questions"] = [q for q in run["questions"] if not q.get("answer")]
    return run
