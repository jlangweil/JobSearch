"""Auto-apply (beta) for Workday tenants.

What it does: opens the posting, signs in (or creates the candidate account) with the credential stored for that
tenant, then walks the application wizard — My Information, My Experience, Application Questions, Voluntary
Disclosures, Self Identify, Review — filling every field it has an answer for, uploading the tailored resume, and
screenshotting each step.

What it will not do:
  * solve a captcha or any "prove you are human" check — it stops and hands the browser back to you;
  * guess an answer it does not have — an unmapped question is reported, not invented;
  * submit, unless `auto_apply.auto_submit` is explicitly turned on. The default run stops on Review.

The browser is real and (by default) visible: you can take over at any point, and whatever you type stays.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from urllib.parse import urlparse

from jobhunt.apply import forms
from jobhunt.apply import profile as profile_mod
from jobhunt.apply.profile import ApplyProfile, answer_for, normalize

NEXT_BUTTONS = ('[data-automation-id="pageFooterNextButton"]', 'button:has-text("Save and Continue")',
                'button:has-text("Continue")', 'button:has-text("Next")', 'button:has-text("Save and Close")')
CAPTCHA = ('iframe[src*="recaptcha"]', 'iframe[src*="hcaptcha"]', '[data-automation-id*="captcha"]',
           'text=/verify (that )?you are (a )?human/i', 'iframe[title*="challenge"]')
STAGE_HEADINGS = ("My Information", "My Experience", "Application Questions", "Voluntary Disclosures",
                  "Self Identify", "Review", "Disability")
MAX_STEPS = 12
MAX_ATTEMPTS_PER_STEP = 3


class ApplyStopped(RuntimeError):
    """Raised when the run cannot continue without you (captcha, unknown required question, login failure)."""

    def __init__(self, message: str, *, reason: str, stage: str = "") -> None:
        super().__init__(message)
        self.reason = reason
        self.stage = stage


@dataclass
class Credential:
    username: str
    password: str


@dataclass
class ApplyContext:
    """Everything a run needs, with the database and config kept on the service side of the boundary."""

    profile: ApplyProfile
    credential: Credential
    resume_path: Path | None = None
    cover_letter_path: Path | None = None
    experience: list[dict] = dc_field(default_factory=list)   # newest first: title, org, location, start, end, text
    skills: list[str] = dc_field(default_factory=list)        # offered to a "Type to Add Skills" picker
    education: list[dict] = dc_field(default_factory=list)
    stored_answers: dict[str, str] = dc_field(default_factory=dict)   # normalized label -> answer you saved before
    fill_work_experience: bool = True   # False: attach the resume only and let the site parse it
    apply_mode: str = "manual"          # manual | autofill (let the site read the resume first)
    prefer_generated_password: bool = False  # sign in with your saved password; register with a generated one
    create_accounts: bool = True
    known_account: bool = False     # we already hold a sign-in for this exact site, so registering again is wrong
    strong_password: Callable[[], str] | None = None            # used when a tenant refuses your usual password
    verification_code: Callable[[], str | None] | None = None   # reads the emailed code, when a mailbox is set up
    verification_link: Callable[[], str | None] | None = None   # reads the emailed activation link
    on_account_created: Callable[[str, str], None] = lambda username, password: None
    auto_submit: bool = False
    stop_on_unknown: bool = True
    max_work_experience: int = 6
    wait_for_human_seconds: int = 0     # a visible browser can pause for you to do a step it cannot
    on_event: Callable[[str, str, str], None] = lambda kind, label, detail: None
    on_unknown: Callable[[str, str, Sequence[str], str], None] = lambda label, kind, options, stage: None
    on_screenshot: Callable[[str], None] = lambda path: None

    def answer(self, label: str) -> tuple[str | None, str]:
        """A question you answered by hand once wins over the profile defaults, so a re-run never re-asks."""
        if stored := self.stored_answers.get(normalize(label)):
            return stored, "saved answer"
        return answer_for(self.profile, label)


def is_workday(url: str | None) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return host.endswith("myworkdayjobs.com") or host.endswith("myworkdaysite.com") or "/wday/" in (url or "")


def tenant_host(url: str | None) -> str:
    return (urlparse(url or "").hostname or "").lower()


def visible(scope, *selectors: str, limit: int = 8):  # noqa: ANN001, ANN201
    """The first *visible* element matching any of these selectors, or None.

    Never `.first` on a bare selector: a Workday sign-in page carries the sign-in form and the create-account form
    in the same DOM with one of them hidden, so `.first` happily fills invisible inputs and clicks a hidden
    button — the form appears untouched and the run stalls with no error to show for it.
    """
    for sel in selectors:
        try:
            loc = scope.locator(sel)
            count = loc.count()
        except Exception:
            continue
        for i in range(min(count, limit)):
            try:
                candidate = loc.nth(i)
                if candidate.is_visible():
                    return candidate
            except Exception:
                continue
    return None


def _click(target, timeout: int = 2500) -> bool:  # noqa: ANN001
    """Click something, escalating past anything drawn over it (see forms.click_through)."""
    if forms.click_through(target, timeout):
        return True
    with suppress(Exception):
        inner = target.locator("button, [role=button], a").first
        if inner.count():
            inner.click(timeout=timeout, force=True)
            return True
    return False


def _click_first(page, selectors: Sequence[str], *, timeout: int = 2500) -> str | None:  # noqa: ANN001
    for sel in selectors:
        target = visible(page, sel)
        if target is not None and _click(target, timeout):
            return sel
    return None


def type_into(field, value: str) -> bool:  # noqa: ANN001
    """Put `value` in a field the way a person would, and confirm it stuck.

    Focus rather than click: Workday's page scrolls itself back while Playwright is scrolling a field into
    view, so a click can spend twenty seconds failing with "element is outside of the viewport" — which is what
    a run looked like from the outside, scrolling up and down and never filling anything in. Focusing needs no
    viewport at all. `fill()` alone is not enough either: these are controlled components that only commit on
    real key events.
    """
    with suppress(Exception):
        field.focus()
    try:
        with suppress(Exception):
            field.fill("")                      # a spinbutton section may refuse this; typing still replaces it
        field.press_sequentially(value, delay=20)
        field.press("Tab")                      # blur: most forms validate and enable their button here
    except Exception:
        try:
            field.fill(value)                   # a widget that refuses keystrokes (a date picker, say)
        except Exception:
            return False
    try:
        typed = field.input_value()
    except Exception:
        return True                             # not a text input; assume it took
    if typed == value:
        return True
    # A masked date box takes "092018" and shows "09/2018", and a date section normalises "09" to "9": the
    # digits are what matter, and a number is compared as a number.
    digits = re.compile(r"\D+")
    left, right = digits.sub("", typed), digits.sub("", value)
    if left == right:
        return bool(right)
    return bool(left and right and left.isdigit() and right.isdigit() and int(left) == int(right))


def _invalid_fields(page) -> str:  # noqa: ANN001
    """Labels of anything the page has marked invalid — the usual reason a submit button stays dead."""
    try:
        bad = page.locator('[aria-invalid="true"], [data-automation-id="errorIcon"]')
        labels = []
        for i in range(min(bad.count(), 5)):
            with suppress(Exception):
                labels.append(forms.clean_label(bad.nth(i).evaluate(forms.LABEL_JS))[:60])
        return ", ".join(x for x in labels if x)
    except Exception:
        return ""


def _page_state(page) -> tuple[str, str, bool]:  # noqa: ANN001
    """A cheap fingerprint of the page, to tell whether a click actually did something."""
    try:
        return page.url, page_complaint(page), _on_login(page)
    except Exception:
        return "", "", False


WATCH_EVENTS_JS = """
() => {
  if (window.__jobhunt_watch) { window.__jobhunt_events = []; return true; }
  window.__jobhunt_watch = true;
  window.__jobhunt_events = [];
  const note = (e) => {
    const t = e.target;
    const id = (t.getAttribute && t.getAttribute('data-automation-id'))
        || (t.className ? '.' + String(t.className).slice(0, 20) : (t.tagName || '?'));
    window.__jobhunt_events.push(e.type + '->' + id);
  };
  ['click', 'submit'].forEach(t => window.addEventListener(t, note, {capture: true, passive: true}));
  return true;
}
"""

COVERED_BY_JS = """
(el) => {
  const r = el.getBoundingClientRect();
  const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  if (!top) return 'nothing (off screen?)';
  if (top === el || el.contains(top) || top.contains(el)) return '';
  return (top.tagName || '?').toLowerCase() + (top.className ? '.' + String(top.className).slice(0, 40) : '');
}
"""

SUBMIT_FORM_JS = """
(el) => {
  const form = el.closest('form');
  if (!form) return false;
  if (form.requestSubmit) { form.requestSubmit(el.type === 'submit' ? el : undefined); return true; }
  form.submit();
  return true;
}
"""


def _await_reaction(page, before, sent: list, *, seconds: float) -> bool:  # noqa: ANN001
    """Did the press do anything? True as soon as the form reaches the server, or the page changes by itself.

    Watching the DOM alone is not enough: Workday posts the form and re-renders a second or two later, so a
    check that only compares URL/error/panel reports "nothing changed" while the request is in flight. Retrying
    on that verdict is how one Create Account press became five, each a real POST to /register.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        page.wait_for_timeout(300)
        if sent:
            _settle(page, 1500)     # it went to the server; let the answer render before anyone reads the page
            return True
        if _page_state(page) != before:
            return True
    return False


def submit_and_confirm(page, ctx: ApplyContext, selectors: Sequence[str], *, stage: str,
                       enter_in=None) -> bool:  # noqa: ANN001
    """Press a submit button and confirm it did something, escalating only while nothing has.

    Two things make this harder than it sounds on a Workday form. A *forced* click skips the hit-target check,
    so when Workday's `click_filter` shield sits over the button the event goes to the shield and the call still
    reports success. And the form posts asynchronously, so the page looks untouched for a second or two after a
    press that worked perfectly. Requests are therefore watched directly: the first press that reaches the
    server wins, and no further press is attempted — a retried registration is a real second account attempt.
    """
    before = _page_state(page)
    sent: list[str] = []

    def _watch(request) -> None:  # noqa: ANN001
        if request.method != "GET":
            sent.append(request.url)

    button = None
    for sel in selectors:
        if (button := visible(page, sel)) is not None:
            break
    if button is None:
        ctx.on_event("click", stage, "no visible submit button")
        return False

    with suppress(Exception):
        if not button.is_enabled():
            ctx.on_event("click", stage, "the submit button is disabled — the form does not accept what is in it"
                         + (f" (invalid: {bad})" if (bad := _invalid_fields(page)) else ""))
    with suppress(Exception):
        page.bring_to_front()                    # a headed window that lost focus can swallow synthesized input
    with suppress(Exception):
        button.scroll_into_view_if_needed(timeout=2000)
    with suppress(Exception):
        box, view = button.bounding_box(), page.viewport_size
        covered = button.evaluate(COVERED_BY_JS)
        where = (f"button at x={int(box['x'])} y={int(box['y'])} ({int(box['width'])}x{int(box['height'])})"
                 if box else "button has no box")
        seen = f" in a {view['width']}x{view['height']} viewport" if view else ""
        ctx.on_event("click", stage, where + seen + (f", covered by <{covered}>" if covered else ", not covered"))
    with suppress(Exception):
        page.evaluate(WATCH_EVENTS_JS)           # passive: records what the page receives, blocks nothing

    strategies = (
        ("clicked it", lambda: button.click(timeout=2500)),
        ("clicked it (forced)", lambda: button.click(timeout=2500, force=True)),
        # keyboard activation needs no coordinates and cannot be intercepted by anything drawn over the button
        ("focused it and pressed Enter", lambda: (button.focus(), button.press("Enter"))),
        ("focused it and pressed Space", lambda: (button.focus(), button.press(" "))),
        ("dispatched a click event on it", lambda: button.dispatch_event("click")),
        ("called click() on it in the page", lambda: button.evaluate("el => el.click()")),
        ("submitted the form directly", lambda: button.evaluate(SUBMIT_FORM_JS)),
    )
    page.on("request", _watch)
    try:
        for attempt, (name, action) in enumerate(strategies):
            try:
                action()
            except Exception as exc:
                ctx.on_event("click", stage, f"{name}: {type(exc).__name__}")
                continue
            if _await_reaction(page, before, sent, seconds=8 if attempt == 0 else 3):
                ctx.on_event("click", stage, f"{name} — {'the form was sent' if sent else 'the page moved on'}")
                return True
            heard = ""
            with suppress(Exception):
                heard = ", ".join(page.evaluate("() => { const e = window.__jobhunt_events || []; "
                                                "window.__jobhunt_events = []; return e; }")) or "no events"
            ctx.on_event("click", stage, f"{name}: nothing changed (the page received: {heard})")
        if enter_in is not None:
            with suppress(Exception):
                enter_in.press("Enter")
                if _await_reaction(page, before, sent, seconds=5):
                    ctx.on_event("click", stage, "pressed Enter in the form — the form was sent")
                    return True
    finally:
        with suppress(Exception):
            page.remove_listener("request", _watch)
    detail = page_complaint(page) or _invalid_fields(page) or "no error shown"
    ctx.on_event("stalled", stage, f"the page did not change and nothing was sent: {detail}")
    return False


def _settle(page, ms: int = 900) -> None:  # noqa: ANN001
    """Give the page a moment to finish its own work before we read it."""
    try:
        page.wait_for_load_state("networkidle", timeout=6000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


def check_captcha(page, stage: str = "") -> None:  # noqa: ANN001
    for sel in CAPTCHA:
        try:
            if page.locator(sel).first.count() and page.locator(sel).first.is_visible():
                raise ApplyStopped("a human-verification check appeared; finish this application in the open browser",
                                   reason="captcha", stage=stage)
        except ApplyStopped:
            raise
        except Exception:
            continue


def current_stage(page) -> str:  # noqa: ANN001
    """The wizard step name, from the progress bar or the page heading."""
    for sel in ('[data-automation-id="progressBarActiveStep"]', '[data-automation-id="pageHeader"]', "h1", "h2", "h3"):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                text = forms.clean_label(loc.inner_text())[:80]
                if text:
                    for known in STAGE_HEADINGS:
                        if known.lower() in text.lower():
                            return known
                    return text
        except Exception:
            continue
    return "step"


def dismiss_cookies(page) -> None:  # noqa: ANN001
    _click_first(page, ["#onetrust-accept-btn-handler", 'button:has-text("Accept All")',
                        'button:has-text("Accept Cookies")', '[data-automation-id="legalNoticeAcceptButton"]'],
                 timeout=1500)


# ------------------------------------------------------------------------------------------------ sign in


def sign_in(page, ctx: ApplyContext) -> str:
    """Get signed in on this tenant, creating the candidate account if there is not one. Returns what it did.

    Order matters: **try to sign in first**. Every Workday sign-in panel offers a "Create Account" link, so
    treating that link as "no account here" would register again on a tenant you already use — which fails with
    "an account already exists" and leaves the run stuck.
    """
    _settle(page)
    if not _on_login(page):
        return "already signed in"
    if _do_sign_in(page, ctx, ctx.credential.password):
        return "signed in"

    error = page_complaint(page)
    if error and not _login_rejected(error) and panel(page) != "create":
        raise ApplyStopped(f"could not sign in: {error}", reason="sign_in", stage="sign in")
    if ctx.known_account:
        # We have signed up here before, so there is an account and this password is simply wrong (or the
        # account is locked after too many attempts). Registering again cannot succeed, and the last time this
        # ran it "succeeded" onto a different page and overwrote the saved password with one the site had
        # never accepted.
        raise ApplyStopped(
            f"the saved sign-in for this site was refused ({error or 'no reason given'}). Use 'Forgot your "
            f"password?' on the site to set a password for {ctx.credential.username}, then save it on the "
            "Auto-apply page. If you have just made several attempts, the account may be locked for a while.",
            reason="password", stage="sign in")
    ctx.on_event("signin_failed", "sign in",
                 f"{error} — trying to create the account, which is the only way to tell whether one exists"
                 if error else "no account on this tenant yet")
    if not ctx.create_accounts:
        raise ApplyStopped("there is no account on this Workday site yet and account creation is switched off",
                           reason="no_account", stage="sign in")
    return create_account(page, ctx)


def _on_login(page) -> bool:  # noqa: ANN001
    return visible(page, '[data-automation-id="email"]', 'input[type="password"]') is not None


def panel(page) -> str:  # noqa: ANN001
    """Which of Workday's two login panels is showing: 'signin', 'create', 'login' (can't tell), or 'none'.

    Tenants differ on which one Apply lands you at — First American opens Create Account with a "Sign In" link
    underneath, and carries no sign-in button in the DOM at all. Typing sign-in details into the create form and
    then pressing whatever submit button is lying around is how a run ends up doing nothing at all.
    """
    if visible(page, '[data-automation-id="createAccountSubmitButton"]', '[data-automation-id="verifyPassword"]'):
        return "create"
    if visible(page, '[data-automation-id="signInSubmitButton"]'):
        return "signin"
    if visible(page, '[data-automation-id="email"]', 'input[type="password"]'):
        return "login"
    return "none"


def _sign_in_form(page):  # noqa: ANN001, ANN201
    """(email, password) inputs of the panel actually on screen, or (None, None)."""
    email = visible(page, '[data-automation-id="email"]', 'input[type="email"]', 'input[name*="email" i]',
                    'input[autocomplete="username"]')
    password = visible(page, '[data-automation-id="password"]', 'input[type="password"]')
    return email, password


def _do_sign_in(page, ctx: ApplyContext, password: str) -> bool:  # noqa: ANN001
    """Fill and submit the sign-in panel. True when we end up somewhere that is no longer a login page."""
    if panel(page) == "create":                 # this tenant opens at Create Account: switch panels first
        _click_first(page, ['[data-automation-id="signInLink"]', 'a:has-text("Sign In")',
                            'a:has-text("Already have an account")'], timeout=1500)
        _settle(page, 700)
    if panel(page) == "create":
        ctx.on_event("signin", "sign in", "this site only offers Create Account")
        return False
    email_field, password_field = _sign_in_form(page)
    if email_field is None or password_field is None:
        ctx.on_event("signin", "sign in", "no visible sign-in form on this page")
        return False
    if not type_into(email_field, ctx.credential.username):
        ctx.on_event("signin", "sign in", "the email field would not accept the address")
    type_into(password_field, password)
    buttons = ['[data-automation-id="signInSubmitButton"]', 'button[data-automation-id="signInSubmitButton"]',
               'button:has-text("Sign In")']
    if panel(page) != "create":                 # never fall back to a stray submit button on a create form
        buttons.append('button[type="submit"]')
    moved = submit_and_confirm(page, ctx, buttons, stage="sign in", enter_in=password_field)
    if not moved:
        ctx.on_event("signin", "sign in", "the sign-in button did nothing — the page did not change")
    check_captcha(page, "sign in")
    handle_verification(page, ctx, "sign in")
    return not _on_login(page) and not page_complaint(page)


def _login_rejected(error: str) -> bool:
    """Did the site refuse the sign-in details (as opposed to something else going wrong)?

    Workday never says whether an account exists — First American answers a failed sign-in with "You may have
    entered the wrong email address or password or your account might be locked", which covers "no account",
    "wrong password" and "locked" in one sentence. So any credential rejection means: go and try to create the
    account. That either works (there was no account) or comes back "already exists", which is the only way to
    learn that the password is the problem — and produces a message that actually tells you what to do.
    """
    return bool(re.search(
        r"(no account|not found|does not exist|isn.t associated|invalid|incorrect|unable to sign|"
        r"could ?n.t find|could not find|wrong (?:email|password|user)|email address or password|"
        r"account (?:might be|is) locked|username or password|try again|not recognized|sign.?in failed)",
        error, re.I))


def _already_exists(error: str) -> bool:
    """Careful: "we could not find an account with that email address" is the *opposite* message, and an earlier
    version of this pattern matched it — which sent the run down the "sign in instead" path on a site where no
    account existed at all."""
    return bool(re.search(r"(already (?:exists|in use|registered|have an account)|account already|"
                          r"(?:an )?account (?:exists|is registered)|exists with (?:this|that) email)", error, re.I))


def _password_rejected(error: str) -> bool:
    return bool(re.search(r"password.*(must|requirement|not meet|at least|invalid)|"
                          r"(requirement|criteria).*password", error, re.I))


def create_account(page, ctx: ApplyContext) -> str:
    """Register on a tenant we have never applied to. The password used is reported through `on_account_created`,
    so it is saved encrypted and the next run simply signs in."""
    # Sign-in uses the password you saved. Registration is the only place a generated one belongs — signing in
    # with a password that has never been used anywhere just produces a pointless failed login.
    password = (ctx.strong_password() if ctx.prefer_generated_password and ctx.strong_password
                else ctx.credential.password)
    for attempt in range(2):
        if not _open_create_form(page):
            raise ApplyStopped("could not find the Create Account form on this site", reason="create_account",
                               stage="create account")
        _fill_account_form(page, ctx, password)
        _, password_field = _sign_in_form(page)
        moved = submit_and_confirm(page, ctx, ['[data-automation-id="createAccountSubmitButton"]',
                                               'button:has-text("Create Account")', 'button:has-text("Create")',
                                               'button[type="submit"]'],
                                   stage="create account", enter_in=password_field)
        check_captcha(page, "create account")
        error = page_complaint(page)
        if not error and moved and _on_login(page) and not verification_wanted(page):
            # The form reached the server and we are still looking at a sign-in or create panel — including the
            # site's own sign-in page, which is where Workday drops you when the email is already registered.
            # This is a failure, and it must not save a password the site never accepted.
            raise ApplyStopped(
                "the site did not accept the registration — it returned to a sign-in page. There is probably "
                f"already an account for {ctx.credential.username}: use 'Forgot your password?' on the site and "
                "save the new password on the Auto-apply page.",
                reason="create_account", stage="create account")
        if not error and not moved:
            # nothing happened at all: the account does NOT exist, so it must not be recorded as if it did
            raise ApplyStopped(
                "the Create Account form would not submit — nothing on the page changed"
                + (f" (invalid: {bad})" if (bad := _invalid_fields(page)) else "")
                + ". Finish this one in the open browser window; the details are filled in.",
                reason="create_account", stage="create account")
        if not error:
            # save first: the account exists from this moment, even if the verification step fails afterwards
            ctx.on_account_created(ctx.credential.username, password)
            ctx.credential.password = password
            handle_verification(page, ctx, "create account")
            return "created account"
        if _already_exists(error):
            # the account is already there (perhaps created by hand): sign in with what we have instead
            ctx.on_event("account_exists", "create account", error)
            if _do_sign_in(page, ctx, password):
                return "signed in (the account already existed)"
            # an account created earlier but never confirmed refuses sign-in too; its activation email is
            # still sitting in the mailbox, so try that before blaming the password
            if follow_activation_link(page, ctx, "create account") and not _on_login(page):
                return "activated the existing account"
            raise ApplyStopped(
                f"this site already has an account for {ctx.credential.username}, but the saved password was "
                "refused (it may also be locked after failed attempts). Use 'Forgot your password?' on the site, "
                "then save the new password for this site on the Auto-apply page.",
                reason="password", stage="sign in")
        if _password_rejected(error) and attempt == 0 and ctx.strong_password:
            # a tenant whose password policy is stricter than your usual one: generate a password and save it
            ctx.on_event("password_policy", "create account", error)
            password = ctx.strong_password()
            continue
        raise ApplyStopped(f"could not create an account: {error}", reason="create_account", stage="create account")
    raise ApplyStopped("could not create an account on this site", reason="create_account", stage="create account")


def _open_create_form(page) -> bool:  # noqa: ANN001
    """Get the create-account panel on screen. The hidden copy that sits in the DOM alongside sign-in does not
    count — clicking the link is what swaps them over."""
    if visible(page, '[data-automation-id="createAccountSubmitButton"]', '[data-automation-id="verifyPassword"]'):
        return True
    _click_first(page, ['[data-automation-id="createAccountLink"]', 'a:has-text("Create Account")',
                        'button:has-text("Create Account")', 'a:has-text("Sign Up")',
                        'a:has-text("Create an Account")'])
    _settle(page, 800)
    if panel(page) == "create":
        return True
    # No link back (some tenants only offer one direction): reloading returns the page to its default panel,
    # which on a site like this one is Create Account.
    with suppress(Exception):
        page.reload(wait_until="domcontentloaded")
        _settle(page, 800)
    return panel(page) == "create"


def _fill_account_form(page, ctx: ApplyContext, password: str) -> None:  # noqa: ANN001
    email_field = visible(page, '[data-automation-id="email"]', 'input[type="email"]', 'input[name*="email" i]')
    if email_field is not None:
        type_into(email_field, ctx.credential.username)
    boxes = page.locator('[data-automation-id="password"], [data-automation-id="verifyPassword"], '
                         'input[type="password"]')
    for i in range(boxes.count()):                         # password, and "verify new password"
        with suppress(Exception):
            if boxes.nth(i).is_visible():
                type_into(boxes.nth(i), password)
    for f in forms.scan(page):                             # some tenants ask for your name here too
        if f.kind in ("text", "select", "date") and not f.filled and f.locator is not email_field:
            answer, _ = ctx.answer(f.label)
            if answer:
                forms.fill(f, answer)
    for trap in getattr(forms.scan, "last_traps", []):
        ctx.on_event("skipped", trap.label[:60], f"left empty on purpose ({trap.name})")
    # Chrome's password manager can offer a generated password over these fields in a headed window; if the two
    # boxes end up different, Workday refuses to submit and says nothing at all, so check rather than assume.
    with suppress(Exception):
        values = page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('input[type=password]').forEach(el => {
              const r = el.getBoundingClientRect();
              if (r.width && r.height) out.push(el.value.length);
            });
            return out; }""")
        if len(values) > 1 and len(set(values)) > 1:
            ctx.on_event("form_error", "create account",
                         f"the password boxes hold different values ({values}) — retyping them")
            boxes2 = page.locator('input[type="password"]')
            for i in range(boxes2.count()):
                with suppress(Exception):
                    if boxes2.nth(i).is_visible():
                        type_into(boxes2.nth(i), password)

    check = visible(page, '[data-automation-id="createAccountCheckbox"]', 'input[type="checkbox"]')
    try:
        if check is not None and not check.is_checked():
            check.check()
    except Exception:
        pass


# Deliberately specific. `input[name*="code" i]` was here once and matched "Postal Code" and "Country Phone
# Code", so every Workday address form looked like a verification prompt and the run sat waiting for an email.
VERIFICATION_FIELD = ('[data-automation-id="verificationCode"]', '[data-automation-id="otp"]',
                      'input[autocomplete="one-time-code"]', 'input[name*="verification" i]',
                      'input[name*="otp" i]', 'input[id*="verificationCode" i]')


def verification_wanted(page) -> bool:  # noqa: ANN001
    """Is this page asking for an emailed code or link?

    A box to type a code into is conclusive. Otherwise the wording is only trusted on a page that is *not* part
    of the application wizard: "My Information" mentions plenty of things, and treating it as a verification
    prompt cost one run eight minutes of waiting for an email nobody had sent.
    """
    if is_wizard_step(page):
        return False                      # checked first: no field or wording may override it
    if visible(page, *VERIFICATION_FIELD):
        return True
    try:
        body = page.locator("body").inner_text()[:2000]
    except Exception:
        return False
    return bool(re.search(r"verification code|verify your (?:email|candidate account)|enter the code|"
                          r"we (?:sent|emailed|have sent) you (?:a|an|your)|check your (?:email|inbox)|"
                          r"confirm your email|complete setup for your|activation (?:email|link)", body, re.I))


def _await_human(page, seconds: int, done) -> bool:  # noqa: ANN001
    """Hold the run open while you finish a step in the browser window. True when you did."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        page.wait_for_timeout(1500)
        with suppress(Exception):
            if done():
                return True
    return False


def follow_activation_link(page, ctx: ApplyContext, stage: str) -> bool:  # noqa: ANN001
    """Open the activation link the site emailed, then sign in with the account it just activated.

    Workday's link carries its own `?redirect=` back to the application, so following it lands where the run
    left off rather than at the top of the site.
    """
    if not ctx.verification_link:
        return False
    try:
        link = ctx.verification_link()
    except Exception as exc:
        raise ApplyStopped(f"could not read the verification email from your mailbox: {exc}",
                           reason="verification", stage=stage) from exc
    if not link:
        return False
    ctx.on_event("verification", stage, "following the activation link from the email")
    page.goto(link, wait_until="domcontentloaded")
    _settle(page)
    if _on_login(page):          # activation usually drops you back at sign-in, now with a working account
        _do_sign_in(page, ctx, ctx.credential.password)
    ctx.on_event("verification", stage, "account activated")
    return True


def handle_verification(page, ctx: ApplyContext, stage: str) -> bool:
    """Deal with the code a site emails when you register.

    Reading it from your mailbox is the automatic path. Without that, the run does not fail: it waits, with the
    page open, for you to type the code in — which is a step you do once per employer, not once per application.
    """
    _settle(page, 600)
    if not verification_wanted(page):
        return False
    ctx.on_event("verification", stage, "the site is asking for an emailed code or link")
    with suppress(Exception):
        ctx.on_screenshot(f"verification-{stage}")
    # Some tenants email a one-time code to type in; First American emails an activation link instead. The page
    # only shows a code box in the first case, so the shape of the page decides which to go looking for.
    if not visible(page, *VERIFICATION_FIELD) and follow_activation_link(page, ctx, stage):
        return True

    code, mailbox_error = None, ""
    if ctx.verification_code:
        try:
            code = ctx.verification_code()
        except Exception as exc:            # a mailbox that will not log in is a different problem entirely
            mailbox_error = str(exc)
            ctx.on_event("verification", stage, mailbox_error)
    if code:
        field = None
        for sel in VERIFICATION_FIELD:
            loc = visible(page, sel)
            if loc is not None:
                field = loc
                break
        if field is not None:
            type_into(field, code)
        else:
            for f in forms.scan(page):
                if re.search(r"code", f.label, re.I):
                    forms.fill(f, code, overwrite=True)
                    break
        ctx.on_event("verification", stage, "code entered")
        submit_and_confirm(page, ctx, ['[data-automation-id="submitButton"]', 'button:has-text("Verify")',
                                       'button:has-text("Submit")', 'button:has-text("Continue")',
                                       'button[type="submit"]'], stage=stage)
        if error := page_complaint(page):
            raise ApplyStopped(f"the verification code was refused: {error}", reason="verification", stage=stage)
        return True

    if ctx.wait_for_human_seconds:
        minutes = max(1, ctx.wait_for_human_seconds // 60)
        ctx.on_event("verification", stage,
                     f"waiting up to {minutes} min for you to type the code into the browser window")
        if _await_human(page, ctx.wait_for_human_seconds, lambda: not verification_wanted(page)):
            ctx.on_event("verification", stage, "you finished it — carrying on")
            _settle(page)
            return True
        raise ApplyStopped("the verification step was not finished in time. The code is emailed to you; type it "
                           "into the browser window and start the run again.", reason="verification", stage=stage)
    raise ApplyStopped(
        (f"could not read the verification code from your mailbox: {mailbox_error}" if mailbox_error else
         "this site emailed a verification code and there is no mailbox connected to read it"),
        reason="verification", stage=stage)


ERROR_CONTAINERS = ('[data-automation-id="errorMessage"]', '[data-automation-id="formErrorMessage"]',
                    '[data-automation-id="validationMessage"]', '[data-automation-id="alert"]',
                    '[role="alert"]', '[aria-live="assertive"]', ".css-error", ".wd-error")

# `[^.]{0,160}` rather than a line-bounded match: a real message wraps in its box, and half a sentence
# ("…your account might be") is neither worth showing someone nor safe to match patterns against.
# Only used to read a *login or registration* page that reports its problem outside any known error container.
# Deliberately narrow: an ordinary form says "* Indicates a required field" and "Password must contain…", and
# matching those turned a perfectly good My Information page into "could not sign in".
ERROR_WORDING = re.compile(
    r"[^.]{0,160}\b(?:already (?:exists|in use|registered)|(?:could ?n.t|cannot|can not|unable to) (?:find|sign|locate)"
    r"|no account|not recognized|incorrect|invalid|do(?:es)? not match|wrong (?:email|password|user)|"
    r"account might be locked|does not meet|password must)[^.]{0,160}", re.IGNORECASE)


# Inside the wizard, only text that says "Error" outright is treated as a complaint.
EXPLICIT_ERROR = re.compile(r"\bErrors?\s*(?:Found|-|:)[^.\n]{0,160}", re.IGNORECASE)


def is_wizard_step(page) -> bool:  # noqa: ANN001
    """Are we inside the application itself (rather than at a sign-in, registration or verification gate)?

    The wizard's own text is full of words that read like errors and like verification prompts, so anything
    matching on free text has to ask this first.
    """
    stage = current_stage(page)
    if stage in STAGE_HEADINGS or any(h.lower() in stage.lower() for h in STAGE_HEADINGS):
        return True
    return visible(page, '[data-automation-id="pageFooterNextButton"]') is not None


def _error_text(page) -> str:  # noqa: ANN001
    """What the page is complaining about, if anything."""
    for sel in ERROR_CONTAINERS:
        with suppress(Exception):
            el = visible(page, sel)
            if el is not None and (text := forms.clean_label(el.inner_text())[:300]):
                return text
    return ""


def page_complaint(page) -> str:  # noqa: ANN001
    """What the page is refusing, if anything.

    A real error container is trusted anywhere. Scanning the visible text is only done at a gate: inside the
    wizard, a form's ordinary instructions would be read as a rejection.
    """
    if text := _error_text(page):
        return text
    with suppress(Exception):
        body = page.locator("body").inner_text()[:6000]
        # inside the wizard only an explicit "Error" counts — the rest of a form's text is full of words that
        # read like failures ("* Indicates a required field", "Password must contain…")
        if m := EXPLICIT_ERROR.search(body):
            return " ".join(m.group(0).split())[:300]
        if not is_wizard_step(page) and (m := ERROR_WORDING.search(body)):
            return " ".join(m.group(0).split())[:300]
    return ""


# ------------------------------------------------------------------------------------------------ the wizard


def start_application(page, ctx: ApplyContext) -> None:
    """Get from the posting to the first form step, by whichever route the profile asks for.

    `manual` types every field in, which is exact but depends on our own field matching. `autofill` hands the
    resume to Workday's parser first and leaves us correcting and completing what it produced — faster on a long
    work history, and only as good as the parse.
    """
    dismiss_cookies(page)
    # "Apply Manually" contains the word Apply, so a generic Apply click on the choice page picks the manual
    # route before the mode is even considered
    if not visible(page, '[data-automation-id="autofillWithResume"]', '[data-automation-id="applyManually"]'):
        _click_first(page, ['a[data-automation-id="adventureButton"]',
                            'button[data-automation-id="adventureButton"]',
                            'a:has-text("Apply")', 'button:has-text("Apply")'])
        _settle(page)
    if ctx.apply_mode == "autofill" and ctx.resume_path:
        picked = _click_first(page, ['[data-automation-id="autofillWithResume"]',
                                     'a:has-text("Autofill with Resume")', 'button:has-text("Autofill with Resume")'])
        if picked:
            ctx.on_event("choice", "apply mode", "autofill with resume")
            _settle(page)
            if upload_documents(page, ctx):
                ctx.on_event("choice", "apply mode", "resume handed to the site's parser")
                _settle(page, 2500)
            check_captcha(page, "start")
            return
        ctx.on_event("choice", "apply mode", "this site offers no autofill; applying manually")
    picked = _click_first(page, ['[data-automation-id="applyManually"]', 'a:has-text("Apply Manually")',
                                 '[data-automation-id="useMyLastApplication"]',
                                 'a:has-text("Use My Last Application")'])
    if picked:
        ctx.on_event("choice", "apply mode", picked)
    _settle(page)
    check_captcha(page, "start")


# Workday's own transient failure page. It tells you to refresh, so that is what we do.
SITE_GLITCH = re.compile(r"something went wrong|error code\s*[:|]|please refresh the page", re.IGNORECASE)


def recover_from_glitch(page, ctx: ApplyContext, stage: str = "") -> bool:  # noqa: ANN001
    """Reload when the site shows its "Something went wrong" page. True when it did."""
    with suppress(Exception):
        body = page.locator("body").inner_text()[:1500]
        if SITE_GLITCH.search(body):
            ctx.on_event("retry", stage or current_stage(page), "the site said 'Something went wrong' — reloading")
            page.reload(wait_until="domcontentloaded")
            _settle(page, 1500)
            return True
    return False


def wait_for_step(page, ctx: ApplyContext | None = None, seconds: float = 25) -> bool:  # noqa: ANN001
    """Wait for a step's form to actually render.

    Workday paints the progress bar and heading first and builds the form a second or two later, so a scan that
    starts the moment the page changes finds a page with nothing on it but the site header.
    """
    deadline = time.monotonic() + seconds
    reloads = 0
    while time.monotonic() < deadline:
        with suppress(Exception):
            if page.locator('[data-automation-id^="formField-"]').count() or visible(page, *NEXT_BUTTONS):
                _settle(page, 400)
                return True
        if reloads < 2 and ctx is not None and recover_from_glitch(page, ctx):
            reloads += 1
            continue
        page.wait_for_timeout(500)
    return False


def fill_step(page, ctx: ApplyContext, stage: str) -> list[forms.FillResult]:
    """Fill every field on the current step, recording anything we have no answer for."""
    results: list[forms.FillResult] = []
    fields = forms.scan(page)
    for trap in getattr(forms.scan, "last_traps", []):
        ctx.on_event("skipped", trap.label[:60], f"left empty on purpose ({trap.name})")
    for f in fields:
        if f.kind == "file":
            path = _file_for(f.label, ctx)
            results.append(forms.fill(f, str(path)) if path
                           else forms.FillResult(f.label, f.kind, "unknown", detail="no document to upload"))
            continue
        answer, source = ctx.answer(f.label)
        if answer is None:
            if f.filled:        # already has a value (pre-filled, or we typed it in ourselves): leave it alone
                results.append(forms.FillResult(f.label, f.kind, "unchanged", f.value))
                continue
            if source and not f.required:
                # a field we understand but have nothing for — a middle name, a phone extension. Leaving it
                # blank is the right answer; asking about it would pause the run for nothing.
                results.append(forms.FillResult(f.label, f.kind, "unchanged", detail=f"{source} is empty"))
                continue
            options = f.options or (forms.open_options(f) if f.kind == "select" else [])
            if f.kind == "select":
                forms.close_options(f)
            results.append(forms.FillResult(f.label, f.kind, "unknown", options=list(options),
                                            required=f.required))
            ctx.on_unknown(f.label, f.kind, options, stage)
            continue
        r = forms.fill(f, answer)
        r.detail = r.detail or source
        r.required = f.required
        if r.status == "no_option":
            ctx.on_unknown(f.label, f.kind, r.options, stage)
        results.append(r)
    return results


def _file_for(label: str, ctx: ApplyContext) -> Path | None:
    norm = normalize(label)
    if "cover" in norm:
        return ctx.cover_letter_path
    if any(w in norm for w in ("resume", "cv", "attach", "upload", "file")):
        return ctx.resume_path
    return ctx.resume_path


SKILL_FIELD = ('[data-automation-id="multiSelectContainer"] input', 'input[placeholder*="skill" i]',
               '[data-automation-id="skillsSection"] input')


def fill_skills(page, ctx: ApplyContext) -> int:  # noqa: ANN001
    """Offer the fact bank's skills to a "Type to Add Skills" picker.

    These pickers vary: some show suggestions as you type, Workday's wants Enter and then shows a panel of
    checkboxes to tick. Both are tried, and anything the site does not recognise is dropped. Skills are never
    worth stopping a run over, so every failure here is quiet.
    """
    if not ctx.skills:
        return 0
    field = None
    for f in forms.scan(page):
        if re.search(r"\bskills?\b", f.label, re.IGNORECASE) and f.kind in ("text", "select"):
            field = f.locator
            break
    if field is None:
        field = visible(page, *SKILL_FIELD)
    if field is None:
        return 0
    added = 0
    for skill in ctx.skills:
        with suppress(Exception):
            field.click()
            field.fill(skill)
            page.wait_for_timeout(500)
            if not _choose_skill(page, skill):
                field.press("Enter")            # Workday opens its checkbox panel on Enter
                page.wait_for_timeout(900)
                if _choose_skill(page, skill):
                    added += 1
                    ctx.on_event("skill", skill, "added")
                else:
                    field.fill("")
                    page.keyboard.press("Escape")
                continue
            added += 1
            ctx.on_event("skill", skill, "added")
            page.wait_for_timeout(250)
    if added:
        ctx.on_event("skills", "professional skills", f"{added} of {len(ctx.skills)} offered were recognised")
    return added


def _choose_skill(page, skill: str) -> bool:  # noqa: ANN001
    """Tick or click whichever suggestion matches, if any."""
    options = []
    with suppress(Exception):
        options = [forms.clean_label(t) for t in forms.option_items(page).all_inner_texts() if forms.clean_label(t)]
    if not options:
        with suppress(Exception):
            boxes = page.locator('input[type=checkbox]')
            for i in range(min(boxes.count(), 40)):
                if boxes.nth(i).is_visible():
                    options.append(forms.clean_label(boxes.nth(i).evaluate(forms.LABEL_JS)))
        options = [o for o in options if o]
    if not options:
        return False
    # The owner's rule: whatever the picker offers first after typing is the closest match, so take it when
    # nothing matches more exactly.
    pick = profile_mod.choose_option(skill, options, threshold=88) or options[0]
    return bool(pick and forms.pick_option(page, pick))


def upload_documents(page, ctx: ApplyContext) -> int:  # noqa: ANN001
    """Attach the tailored documents to this step's upload fields.

    The `input[type=file]` behind a "Drop files here / Select files" zone is deliberately invisible, so the
    ordinary field scan never sees it and the resume was never attached. Playwright can set files on a hidden
    input directly, which is also what clicking through the picker would end up doing.
    """
    attached = 0
    inputs = page.locator('input[type="file"]')
    for i in range(inputs.count()):
        el = inputs.nth(i)
        with suppress(Exception):
            if el.evaluate("e => !!(e.files && e.files.length)"):
                continue                                  # something is already attached here
            label = forms.clean_label(el.evaluate(forms.LABEL_JS))
            if label.startswith("@"):
                label = ""
            heading = el.evaluate(
                "(e) => { const s = e.closest('[role=group], section, div'); const h = s && s.querySelector('h3,h4');"
                " return h ? h.innerText : ''; }")
            path = _file_for(f"{label} {heading}".strip(), ctx)
            if not path:
                continue
            el.set_input_files(str(path))
            attached += 1
            ctx.on_event("uploaded", (label or heading or "attachment")[:60], Path(path).name)
            _settle(page, 1200)
    return attached


ORG_LABEL = re.compile(r"company|employer|organi[sz]ation|school|university|institution", re.IGNORECASE)


def _entry_groups(fields: list[forms.Field]) -> list[list[forms.Field]]:
    """Split a section's fields into one group per entry, without needing to know how the site marks a panel.

    A repeated label means a new entry has started: Job Title, Company, ... then Job Title again. Relying on
    `data-automation-id="panel"` instead meant that on a tenant which does not use it, every entry was added and
    then left blank.
    """
    groups: list[list[forms.Field]] = []
    current: list[forms.Field] = []
    seen: set[tuple[str, str]] = set()
    for f in fields:
        key = (normalize(f.label), normalize(f.group))
        # An entry holds two date pairs — Month, Year, Month, Year — so a repeated date section is not the start
        # of a new entry. Counting it as one split every role in half and left its To date unfilled.
        is_date = bool(DATE_PART.match(normalize(f.label)))
        if not is_date and key in seen:
            groups.append(current)
            current, seen = [], set()
        current.append(f)
        if not is_date:
            seen.add(key)
    if current:
        groups.append(current)
    return [g for g in groups if g]


def _group_org(group: list[forms.Field]) -> str:
    for f in group:
        if ORG_LABEL.search(f.label):
            return (f.value or "").strip()
    return ""


def _group_has_org_field(group: list[forms.Field]) -> bool:
    return any(ORG_LABEL.search(f.label) for f in group)


DATE_GROUPS = ((r"\bfrom\b|\bstart\b", "start"), (r"\bto\b|\bend\b", "end"))


DATE_INPUTS = ('input[placeholder*="MM" i]', 'input[placeholder*="YYYY" i]', 'input[placeholder*="YY" i]',
               '[role=spinbutton]', 'input[data-automation-id*="date" i]', '[data-automation-id="dateWidget"] input',
               'input[aria-label*="month" i]', 'input[aria-label*="year" i]')

# Which date an input belongs to, read off the nearest ancestor whose text starts with From/To/Start/End. The
# inputs themselves are labelled "Month" and "Year" at best, and on some tenants not at all.
DATE_CAPTION_JS = r"""
(el) => {
  let node = el;
  for (let i = 0; node && i < 7; i++) {
    const text = (node.innerText || '').replace(/\s+/g, ' ').trim();
    const m = text.match(/^(from|to|start date|end date|start|end)/i);
    if (m) return m[1].toLowerCase();
    node = node.parentElement;
  }
  return '';
}
"""


def date_inputs(scope) -> list[tuple[str, object]]:  # noqa: ANN001
    """Every date input in this scope, paired with the date it belongs to ("from" or "to"), in page order."""
    found: list[tuple[str, object]] = []
    seen: set[str] = set()
    for selector in DATE_INPUTS:
        loc = scope.locator(selector)
        with suppress(Exception):
            for i in range(min(loc.count(), 12)):
                el = loc.nth(i)
                with suppress(Exception):
                    if not el.is_visible():
                        continue
                    key = el.evaluate("e => e.name || e.id || e.getAttribute('data-automation-id') || ''")
                    if key and key in seen:
                        continue
                    seen.add(key)
                    caption = (el.evaluate(DATE_CAPTION_JS) or "").strip().lower()
                    which = "start" if caption.startswith(("from", "start")) else (
                        "end" if caption.startswith(("to", "end")) else "")
                    if which:
                        found.append((which, el))
    return found


def _date_pair_holds(parts, month: str, year: str) -> bool:  # noqa: ANN001
    """Do the two sections of a date widget hold this month and year? A section shows "9" for September, so the
    month is compared as a number rather than as text."""
    def digits(el) -> str:  # noqa: ANN001
        with suppress(Exception):
            return re.sub(r"\D", "", el.input_value() or "")
        return ""

    got_month = digits(parts[0])
    got_year = digits(parts[1]) if len(parts) > 1 else ""
    month_ok = not month or (got_month.isdigit() and int(got_month) == int(month))
    year_ok = not year or got_year == year
    return bool(month_ok and year_ok)


def date_text_for(el, value: str) -> str:  # noqa: ANN001
    """How this particular box wants the date typed.

    A masked MM/YYYY field takes the six digits and puts the slash in itself, so "09/2018" is typed as "092018".
    A year-only box (education's From and To) wants just "2018", and a month box just "09".
    """
    month, _, year = value.partition("/")
    if not year:                         # already a bare year or month
        return month
    hint = ""
    with suppress(Exception):
        hint = " ".join(str(el.get_attribute(a) or "") for a in
                        ("placeholder", "aria-label", "data-automation-id", "name")).upper()
    with suppress(Exception):
        if (el.get_attribute("maxlength") or "") == "4":
            return year
    if "MM" in hint and ("YYYY" in hint or "YY" in hint):
        return f"{month}{year}"          # the mask writes the slash
    if "YYYY" in hint or "YEAR" in hint:
        return year
    if "MM" in hint or "MONTH" in hint:
        return month
    return f"{month}{year}"


def fill_date_widgets(scope, entry: dict, ctx: ApplyContext | None = None) -> int:  # noqa: ANN001
    """Put MM/YYYY into the From and To dates, however the site builds them.

    A tenant may draw one box showing "MM/YYYY" or a Month input beside a Year input, and their own labels are
    no help — so the inputs are found first and then asked, by walking up the page, which date they sit under.
    Each one is typed into rather than set, because these are the fields that ignore a programmatic value.
    """
    filled = 0
    for which, key in (("start", "start"), ("end", "end")):
        value = str(entry.get(key) or "")
        if not value:
            continue
        month, _, year = value.partition("/")
        parts = [el for w, el in date_inputs(scope) if w == which]
        if not parts:
            continue
        # in page order, so the first box is the month and the second the year — the selector that found them
        # says nothing about which comes first
        with suppress(Exception):
            boxed = [(el.bounding_box() or {"y": 0, "x": 0}, el) for el in parts]
            parts = [el for box, el in sorted(boxed, key=lambda b: (round(b[0]["y"]), b[0]["x"]))]
        with suppress(Exception):
            if parts[0].input_value():          # already filled in (by the site's parser, or by us)
                continue
        if len(parts) >= 2 and year:
            # Workday's Month and Year are two sections of one widget: typing the six digits into the first
            # carries on into the second by itself. Filling them one at a time confuses it — the month lands
            # in the year box and comes back as "2009".
            type_into(parts[0], f"{month}{year}")
            ok = _date_pair_holds(parts, month, year)
            if not ok:                       # a widget that keeps its sections separate
                type_into(parts[0], date_text_for(parts[0], month))
                type_into(parts[1], date_text_for(parts[1], year))
                ok = _date_pair_holds(parts, month, year)
        else:
            ok = _type_single_date(parts[0], value)
        if ok:
            filled += 1
            if ctx:
                ctx.on_event("date", which, value)
        elif ctx:
            ctx.on_event("date", which, f"could not type {value}")
    return filled


def _type_single_date(el, value: str) -> bool:  # noqa: ANN001
    """Fill a date that is one box. A masked box turns "092018" into "09/2018" by itself; a plain one does not,
    and would be left holding six digits, so the separator is put in for it."""
    ok = type_into(el, date_text_for(el, value))
    with suppress(Exception):
        shown = el.input_value()
        wants_slash = "/" in (el.get_attribute("placeholder") or "")
        if "/" in value and wants_slash and "/" not in shown:
            ok = type_into(el, value)
    return ok


def _date_slots(parts: list[forms.Field]) -> list[list[forms.Field]]:
    """Group a run of date boxes into one slot per date.

    The shapes differ within a single form: a job's From may be one masked MM/YYYY box while its To is a Month
    box beside a Year box, and education has a lone Year. Chunking them in twos regardless put a job's start
    date into its end date and left the start empty.
    """
    slots: list[list[forms.Field]] = []
    index = 0
    while index < len(parts):
        current = parts[index]
        label = normalize(current.label)
        if DATE_MONTH.match(label) and index + 1 < len(parts) and DATE_YEAR.match(normalize(parts[index + 1].label)):
            slots.append([current, parts[index + 1]])
            index += 2
            continue
        slots.append([current])            # a combined MM/YYYY box, or a year on its own
        index += 1
    return slots


def _fill_group_dates(group: list[forms.Field], entry: dict) -> int:
    """Fill an entry's dates from the entry's own fields.

    Every group holds its date boxes in order — the From date, then the To date — so they can be taken straight
    from it. Searching a surrounding container instead depended on guessing which one holds the widget, and on
    a real tenant it guessed wrong and left every date blank.
    """
    parts = [f for f in group if DATE_PART.match(normalize(f.label))]
    if not parts:
        return 0
    filled = 0
    for slot, key in zip(_date_slots(parts), ("start", "end"), strict=False):
        value = str(entry.get(key) or "")
        if not value or slot[0].filled:
            continue
        month, _, year = value.partition("/")
        locators = [f.locator for f in slot]
        if len(slot) == 2 and year:
            type_into(locators[0], f"{month}{year}")     # the sections carry on from one another
            if not _date_pair_holds(locators, month, year):
                type_into(locators[0], date_text_for(locators[0], month))
                type_into(locators[1], date_text_for(locators[1], year))
            if _date_pair_holds(locators, month, year):
                filled += 1
        elif _type_single_date(locators[0], value):
            filled += 1
    return filled


def _fill_group(group: list[forms.Field], entry: dict, kind: str, scope=None) -> int:  # noqa: ANN001
    dates = _fill_group_dates(group, entry)
    filled = dates
    for f in group:
        # A Degree dropdown lists degree *names* — "Bachelor of Science", "Master of Science" — and tenants
        # abbreviate differently, so several spellings are offered until one of them is in the list.
        if re.search(r"\bdegree\b", f.label, re.IGNORECASE) and entry.get("degree_candidates"):
            for candidate in entry["degree_candidates"]:
                if forms.fill(f, candidate, overwrite=True).status == "filled":
                    filled += 1
                    break
            continue
        if DATE_PART.match(normalize(f.label)):
            continue                          # handled as a pair above
        value = _experience_value(f.label, entry, kind, f.group)
        if value and (not f.filled or f.kind in ("checkbox", "select")):
            if forms.fill(f, value, overwrite=True).status == "filled":
                filled += 1
    if scope is not None and not dates:
        # a tenant whose date boxes are not among the entry's own fields (a single MM/YYYY box, say)
        filled += fill_date_widgets(scope, entry)
    return filled


def fill_experience(page, ctx: ApplyContext) -> int:
    """Put the work history and education into the form.

    Entries may already be there — Autofill with Resume creates them from the site's own parse, and that parse
    is often partly empty. So: anything the site already got is kept, blank entries are completed, and more are
    added for whatever is still missing.
    """
    added = 0
    for kind, entries, add_labels in (
        ("work experience", ctx.experience[:ctx.max_work_experience],
         ('[data-automation-id="Add Another"]', '[data-automation-id="add"]', '[data-automation-id="Add"]',
          'button:has-text("Add Another")', 'button:has-text("Add")')),
        ("education", ctx.education, ('[data-automation-id="Add Another"]', '[data-automation-id="Add"]',
                                      'button:has-text("Add Another")', 'button:has-text("Add")')),
    ):
        section = _section_for(page, kind)
        if section is None or not entries:
            continue
        remaining = list(entries)
        just_added = False
        for _ in range(len(entries) * 2 + 2):          # each pass fills one entry or adds a place to put one
            if not remaining:
                break
            groups = [g for g in _entry_groups(forms.scan(section)) if _group_has_org_field(g)]
            for group in groups:                        # keep whatever the site's parser already got right
                if org := _group_org(group):
                    match = next((e for e in remaining if _same_org(org, str(e.get("org", "")))), None)
                    if match:
                        remaining.remove(match)
                        ctx.on_event("kept", kind, f"{org} was already filled in")
            if not remaining:
                break
            blank = next((g for g in groups if not _group_org(g)), None)
            if blank is None:
                if not _click_first(section, add_labels) and not _click_first(page, add_labels):
                    ctx.on_event("stalled", kind, "no Add button to make room for the next entry")
                    break
                _settle(page, 700)
                _wait_for_panel(section, page)
                just_added = True
                continue                                # the next pass sees the new, blank entry
            entry = remaining.pop(0)
            if _fill_group(blank, entry, kind, scope=_scope_of(blank, section)):
                added += 1
                # "completed" is one the site's parser left blank; "added" is one we made room for
                ctx.on_event("added" if just_added else "completed", kind,
                             f"{entry.get('title', '')} | {entry.get('org', '')}".strip(" |"))
                just_added = False
            else:
                ctx.on_event("stalled", kind, f"could not fill in {entry.get('org', '')}")
                break
    return added


def _scope_of(group: list[forms.Field], section):  # noqa: ANN001, ANN201
    """The smallest container holding this entry's fields, for the date pass to search."""
    for f in group:
        with suppress(Exception):
            handle = f.locator.locator(
                "xpath=ancestor::*[self::div or self::fieldset or self::section][2]")
            if handle.count():
                return handle
    return section


def _same_org(a: str, b: str) -> bool:
    x, y = forms.clean_label(a).lower(), forms.clean_label(b).lower()
    return bool(x and y and (x in y or y in x))


def _wait_for_panel(scope, page, seconds: float = 6) -> bool:  # noqa: ANN001
    """Wait until a freshly added panel's fields have a real size. Filling one mid-animation reads every field
    as zero-sized, which looks exactly like a page full of bot traps."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with suppress(Exception):
            widths = scope.evaluate(
                "(el) => [...el.querySelectorAll('input, textarea, select')].map(i => "
                "i.getBoundingClientRect().width)")
            if widths and min(widths) > 0:
                return True
        page.wait_for_timeout(250)
    return False


def _section_for(page, kind: str):  # noqa: ANN001, ANN202
    """The block of the page that holds one section, found by its heading.

    Workday builds these as `<div role="group" aria-labelledby="Work-Experience-section">` around an `<h4>`, so
    guessing an automation id (`WorkExperienceSection`) finds nothing — which is why the work history was never
    typed in even though the Add button was right there.
    """
    pattern = re.compile(r"\s*".join(re.escape(w) for w in kind.split()), re.IGNORECASE)
    for role in ("heading",):
        headings = page.get_by_role(role, name=pattern)
        with suppress(Exception):
            if headings.count():
                heading = headings.first
                for xp in ("xpath=ancestor::*[@role='group'][1]", "xpath=ancestor::div[1]", "xpath=.."):
                    container = heading.locator(xp)
                    with suppress(Exception):
                        if container.count() and container.locator("button").count():
                            return container
    for sel in (f'[data-automation-id="{kind.title().replace(" ", "")}Section"]',
                f'[aria-labelledby*="{kind.title().replace(" ", "-")}"]'):
        loc = page.locator(sel).first
        with suppress(Exception):
            if loc.count():
                return loc
    return None


def _section_has_entries(section) -> bool:  # noqa: ANN001
    try:
        return bool(section.locator('[data-automation-id="panel"], [data-automation-id="deleteButton"]').count())
    except Exception:
        return False


EXPERIENCE_FIELDS = [
    (r"job\s*title|position\s*title|^title", "title"),
    (r"company|employer|organi[sz]ation", "org"),
    (r"location", "location"),
    (r"^from|start\s*date", "start"),
    (r"^to$|end\s*date", "end"),
    (r"currently\s*work|current\s*(role|position)", "current"),
    (r"role\s*description|description|responsibilit", "text"),
    (r"school|university|institution", "school"),
    (r"degree", "degree"),                       # the *level*: "Master's Degree", from _degree_level()
    (r"field\s*of\s*study|major|concentration", "field_of_study"),
]
_EXPERIENCE = [(re.compile(p, re.IGNORECASE), key) for p, key in EXPERIENCE_FIELDS]


DATE_PART = re.compile(r"^(month|mm|year|yyyy|mm\s*[/-]\s*yyyy|mm\s*[/-]\s*yy)$", re.IGNORECASE)
DATE_MONTH = re.compile(r"^(month|mm)$", re.IGNORECASE)
DATE_YEAR = re.compile(r"^(year|yyyy)$", re.IGNORECASE)


def _experience_value(label: str, entry: dict, kind: str, group: str = "") -> str:
    """What belongs in one field of a role or education panel.

    Workday splits a date into a Month and a Year input inside a group labelled "From" or "To", so those two
    fields have to be answered from the group they sit in — on their own, "Month" says nothing about which date
    it belongs to, and the value never reached them.
    """
    norm, in_group = normalize(label), normalize(group)
    if DATE_PART.match(norm):
        if re.search(r"\bfrom\b|start", in_group):
            source = entry.get("start", "")
        elif re.search(r"^to\b|\bto\b|end", in_group):
            source = entry.get("end", "")
        else:
            return ""
        month, _, year = str(source or "").partition("/")
        if not year:                       # a bare year, or nothing at all
            return source if norm.startswith(("year", "yyyy")) else ""
        return year if norm.startswith(("year", "yyyy")) else month
    for pattern, key in _EXPERIENCE:
        if pattern.search(norm):
            value = entry.get(key)
            if key == "current":
                return "Yes" if str(entry.get("end", "")).lower() in ("", "present", "current") else "No"
            return str(value or "")
    return ""


def advance(page, ctx: ApplyContext) -> bool:
    """Press Save and Continue, confirming the step actually advanced — the same overlay that can swallow a click
    on Create Account can swallow this one, and a step that silently did not save is worse here than anywhere."""
    moved = submit_and_confirm(page, ctx, NEXT_BUTTONS, stage=current_stage(page))
    if error := page_complaint(page):
        ctx.on_event("form_error", current_stage(page), error)
    return moved


def is_review(page) -> bool:  # noqa: ANN001
    """The last step of the wizard. A sign-in page never counts, whatever buttons it happens to carry."""
    if _on_login(page):
        return False
    stage = current_stage(page).lower()
    if "review" in stage:
        return True
    return visible(page, 'button:has-text("Submit")', '[data-automation-id="submitButton"]') is not None


def submit(page, ctx: ApplyContext) -> bool:
    check_captcha(page, "review")
    if not ctx.auto_submit:
        ctx.on_event("stopped", "review", "auto_submit is off — the application is filled and waiting for you")
        return False
    if not submit_and_confirm(page, ctx, ['[data-automation-id="submitButton"]', 'button:has-text("Submit")',
                                          '[data-automation-id="pageFooterNextButton"]'], stage="review"):
        raise ApplyStopped("the Submit button on the review page did not do anything", reason="no_submit",
                           stage="review")
    ctx.on_event("submitted", "review", "submit clicked")
    return True


def run(page, ctx: ApplyContext, apply_url: str, *, shot: Callable[[str], None]) -> dict:
    """Drive one application. Returns a result dict; raises ApplyStopped when you need to take over."""
    page.goto(apply_url, wait_until="domcontentloaded")
    _settle(page)
    recover_from_glitch(page, ctx, "posting")
    check_captcha(page, "posting")
    start_application(page, ctx)
    shot("before-sign-in")
    ctx.on_event("signin", "account", sign_in(page, ctx))
    shot("signed-in")
    if _on_login(page):
        # a tenant that emails an activation link leaves you on "check your email"; follow it and sign in
        if handle_verification(page, ctx, "sign in"):
            _settle(page)
        if _on_login(page):
            _do_sign_in(page, ctx, ctx.credential.password)
        if _on_login(page):      # a silent no-op used to look like success and waste the rest of the run
            raise ApplyStopped("still on the sign-in page after signing in — the form did not accept the details. "
                               "Finish this one in the open browser window and check the saved sign-in.",
                               reason="sign_in", stage="sign in")
        shot("after-activation")

    seen: list[str] = []
    totals = {"filled": 0, "unknown": 0, "unchanged": 0, "no_option": 0, "error": 0}
    attempts: dict[str, int] = {}
    for step in range(MAX_STEPS):
        _settle(page, 500)
        check_captcha(page)
        if not wait_for_step(page, ctx):
            ctx.on_event("waiting", current_stage(page), "the form did not finish rendering")
        stage = current_stage(page)
        seen.append(stage)
        attempts[stage] = attempts.get(stage, 0) + 1
        if attempts[stage] > MAX_ATTEMPTS_PER_STEP:
            # three goes at the same step means something on it cannot be answered from here; stop and let the
            # window stay open rather than filling and pressing Save over and over
            problem = page_complaint(page) or _invalid_fields(page) or "no reason given"
            raise ApplyStopped(f"{stage} would not move on after {MAX_ATTEMPTS_PER_STEP} attempts: {problem}. "
                               "Finish this step in the open browser window.",
                               reason="form_error", stage=stage)
        if upload_documents(page, ctx):
            _settle(page, 800)
        results = fill_step(page, ctx, stage)
        if "experience" in stage.lower() and ctx.fill_work_experience:
            fill_experience(page, ctx)
            fill_skills(page, ctx)
            results += fill_step(page, ctx, stage)          # the new panels bring their own fields
        for r in results:
            totals[r.status] = totals.get(r.status, 0) + 1
            ctx.on_event(r.status, r.label, r.detail or str(r.value)[:120])
        shot(f"{step:02d}-{stage}")
        # only a *required* question is worth stopping for: an optional one can be left blank and answered later
        unresolved = [r for r in results if r.status in ("unknown", "no_option") and r.required]
        if unresolved and ctx.stop_on_unknown:
            labels = ", ".join(r.label for r in unresolved[:4])
            raise ApplyStopped(f"{len(unresolved)} question(s) need an answer: {labels}",
                               reason="needs_input", stage=stage)
        if is_review(page):
            shot("review")
            submitted = submit(page, ctx)
            return {"stages": seen, "totals": totals, "submitted": submitted,
                    "stage": "submitted" if submitted else "review"}
        if not advance(page, ctx):
            return {"stages": seen, "totals": totals, "submitted": False, "stage": stage}
        _settle(page, 600)
        if current_stage(page) == stage and (problem := page_complaint(page)):
            # the step refused to save and we cannot fix what it is complaining about; pressing Save and
            # Continue again just repeats it, which is how one run pressed it four times in a row
            raise ApplyStopped(f"{stage} would not save: {problem}", reason="form_error", stage=stage)
    return {"stages": seen, "totals": totals, "submitted": False, "stage": "gave up after too many steps"}
