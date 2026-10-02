"""Auto-apply (beta): answer matching, credential storage, and a full wizard run against an offline tenant.

The browser tests drive Playwright over `tests/fixtures/apply/`, never a real employer site. They skip when
Chromium is not installed, so the suite still runs on a machine that has not done `playwright install`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from jobhunt.apply import forms, secrets_store, workday
from jobhunt.apply.profile import (
    ApplyProfile,
    CustomAnswer,
    answer_for,
    choose_option,
    field_key,
    load_profile,
    normalize,
    save_profile,
    seed_from_contact,
)
from jobhunt.db import queries
from jobhunt.services import autoapply

FIXTURES = Path(__file__).parent / "fixtures" / "apply"
TENANT = FIXTURES / "tenant"
NEW_TENANT = FIXTURES / "newtenant"


def chromium_missing() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return True
    try:
        with sync_playwright() as pw:
            return not Path(pw.chromium.executable_path).exists()
    except Exception:
        return True


needs_browser = pytest.mark.skipif(chromium_missing(), reason="Chromium is not installed (playwright install)")


FACTS = """
contact:
  name: "Jane Doe"
  location: "Parsippany, NJ"
  email: "jane@example.test"
  phone: "555-555-0100"
  linkedin: "linkedin.com/in/example/"
facts:
  - id: role-nyl
    kind: role
    org: "Example Life"
    title: "Solution Lead – Enterprise Integration"
    start_date: 2018-09
    end_date: 2025-10
    text: "Led enterprise integration."
    tags: [clinton nj, mulesoft]
  - id: role-cvs
    kind: role
    org: "Example Health"
    title: "Advisor, Applications"
    start_date: 2017-02
    end_date: 2018-09
    text: "Web applications developer."
    tags: [florham park nj, .net]
  - id: edu-drexel
    kind: education
    org: "Drexel University"
    title: "Master of Science (MS) Information Systems"
    text: "Drexel University | MS Information Systems"
"""


def _write_facts(cfg) -> None:  # noqa: ANN001
    cfg.paths.facts.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.facts.write_text(FACTS, encoding="utf-8")


@pytest.fixture
def profile() -> ApplyProfile:
    p = ApplyProfile()
    p.identity.first_name, p.identity.last_name = "Jane", "Doe"
    p.identity.email, p.identity.phone = "jane@example.test", "555-555-0100"
    p.identity.address_line_1, p.identity.city = "1 Main St", "Parsippany"
    p.identity.state, p.identity.postal_code = "New Jersey", "07054"
    p.eligibility.desired_salary = "150000"
    p.eeo.veteran_status = "I do not wish to answer"
    return p


# --------------------------------------------------------------------------------------- matching answers


@pytest.mark.parametrize(("label", "key"), [
    ("First Name*", "identity.first_name"),
    ("Legal Name — Last Name (required)", "identity.last_name"),
    ("Are you legally authorized to work in the United States?", "eligibility.authorized_to_work"),
    ("Do you have the legal right to work in the US?", "eligibility.authorized_to_work"),
    ("Will you now or in the future require sponsorship for employment visa status?",
     "eligibility.requires_sponsorship"),
    ("Have you ever worked for Allstate?", "eligibility.former_employee"),
    ("How Did You Hear About Us?", "eligibility.how_did_you_hear"),
    ("LinkedIn Profile URL", "links.linkedin"),
    ("Veteran Status", "eeo.veteran_status"),
    ("Please check one of the boxes below regarding disability", "eeo.disability"),
    ("Phone Device Type", "identity.phone_type"),
    ("Desired salary", "eligibility.desired_salary"),
])
def test_form_labels_map_to_profile_fields(label, key):
    """Tenants word the same question differently, so labels are matched by pattern, not exact text."""
    assert field_key(label) == key


def test_unknown_questions_are_not_guessed(profile):
    assert field_key("Which of our company values resonates with you most?") is None
    assert answer_for(profile, "Which of our company values resonates with you most?")[0] is None


def test_your_own_answers_beat_the_built_in_rules(profile):
    profile.questions.append(CustomAnswer(match="how did you hear", answer="Referred by a colleague"))
    assert answer_for(profile, "How Did You Hear About Us?*")[0] == "Referred by a colleague"
    profile.questions.append(CustomAnswer(match="/company values/", answer="Integrity"))
    assert answer_for(profile, "Which of our company values resonates with you most?")[0] == "Integrity"


@pytest.mark.parametrize(("answer", "options", "expected"), [
    ("No", ["Yes", "No"], "No"),
    ("Yes", ["Yes", "No", "I prefer not to say"], "Yes"),
    ("I do not wish to answer",
     ["I identify as one or more of the classifications of a protected veteran", "I am not a protected veteran",
      "I don't wish to answer"], "I don't wish to answer"),
    ("New Jersey", ["New Jersey", "New York", "Pennsylvania"], "New Jersey"),
    ("Company website", ["Job Board", "Company website", "LinkedIn"], "Company website"),
    # nothing close enough: a disclosure form is the wrong place to guess
    ("Yes", ["I am not a protected veteran", "I don't wish to answer"], None),
    ("Male", [], None),
])
def test_option_matching_picks_or_refuses(answer, options, expected):
    assert choose_option(answer, options) == expected


def test_normalize_ignores_decoration():
    assert normalize("First Name*") == normalize("  first   name (required) ") == "first name"


def test_profile_seeds_from_the_fact_bank_contact(tmp_path, cfg):
    from jobhunt.facts import bank

    _write_facts(cfg)
    p = seed_from_contact(bank.contact(cfg))
    assert p.identity.first_name and p.signature.full_name
    path = save_profile(tmp_path / "apply_profile.yaml", p)
    assert "questions:" in path.read_text(encoding="utf-8")
    assert load_profile(path).identity.first_name == p.identity.first_name


# --------------------------------------------------------------------------------------- credentials


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_passwords_are_encrypted_at_rest_and_round_trip():
    stored = secrets_store.protect("correct horse battery staple")
    assert "correct horse" not in stored and stored.startswith("dpapi:")
    assert secrets_store.unprotect(stored) == "correct horse battery staple"
    with pytest.raises(secrets_store.CredentialError):
        secrets_store.unprotect("plaintext-password")


def test_environment_password_wins_over_the_stored_one(monkeypatch):
    monkeypatch.setenv(secrets_store.ENV_PASSWORD, "from-env")
    assert secrets_store.password_for(None) == "from-env"


def test_credentials_fall_back_to_the_shared_account(engine):
    queries.upsert_credential(engine, host="*", username="me@example.test", password_enc="dpapi:x")
    queries.upsert_credential(engine, host="nyl.wd1.myworkdayjobs.com", username="nyl@example.test",
                              password_enc="dpapi:y")
    assert queries.credential_for(engine, "nyl.wd1.myworkdayjobs.com")["username"] == "nyl@example.test"
    assert queries.credential_for(engine, "other.wd5.myworkdayjobs.com")["username"] == "me@example.test"


def test_editing_a_username_keeps_the_saved_password(engine):
    queries.upsert_credential(engine, host="*", username="old@example.test", password_enc="dpapi:secret")
    queries.upsert_credential(engine, host="*", username="new@example.test", password_enc=None)
    row = queries.credential_for(engine, "anything.myworkdayjobs.com")
    assert (row["username"], row["password_enc"]) == ("new@example.test", "dpapi:secret")


# --------------------------------------------------------------------------------------- questions


def test_a_question_is_only_ever_asked_once(engine):
    first = queries.add_apply_question(engine, run_id=None, job_id=None, host="a.test", stage="Questions",
                                       label="Which of our values resonates?", norm_label="which of our values",
                                       kind="text", options=[])
    queries.answer_apply_question(engine, first, "Integrity")
    again = queries.add_apply_question(engine, run_id=None, job_id=None, host="b.test", stage="Questions",
                                       label="Which of our values resonates?", norm_label="which of our values",
                                       kind="text", options=[])
    assert again == first
    assert queries.stored_question_answers(engine) == {"which of our values": "Integrity"}
    assert queries.list_apply_questions(engine, unanswered_only=True) == []


def test_work_history_comes_from_the_fact_bank(cfg):
    _write_facts(cfg)
    roles, education = autoapply.experience_entries(cfg, 6)
    assert roles, "the fact bank should provide roles to type into the form"
    newest = roles[0]
    assert set(newest) >= {"title", "org", "start", "end", "location", "text"}
    assert "–" not in newest["title"]                      # a dash in a title confuses the parser: use a comma
    assert all(r["start"] == "" or r["start"][2] == "/" for r in roles)   # MM/YYYY


# --------------------------------------------------------------------------------------- the browser


@needs_browser
def test_scan_reads_every_field_and_skips_the_furniture(profile):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((FIXTURES / "workday_my_information.html").as_uri())
        fields = {f.label: f for f in forms.scan(page)}
        assert "First Name" in fields and "Postal Code" in fields
        assert not any("Search" in label for label in fields)       # search boxes are not application questions
        assert fields["State"].kind == "select" and "New Jersey" in fields["State"].options
        assert fields["Veteran Status"].kind == "radio" and len(fields["Veteran Status"].options) == 3
        assert fields["Resume/CV"].kind == "file"
        browser.close()


@needs_browser
def test_fill_writes_answers_and_reports_what_it_could_not_answer(profile, tmp_path):
    from playwright.sync_api import sync_playwright

    resume = tmp_path / "resume.docx"
    resume.write_bytes(b"PK\x03\x04 not really a docx")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((FIXTURES / "workday_my_information.html").as_uri())
        results = []
        for f in forms.scan(page):
            if f.kind == "file":
                results.append(forms.fill(f, str(resume)))
                continue
            answer, _ = answer_for(profile, f.label)
            results.append(forms.fill(f, answer) if answer else forms.FillResult(f.label, f.kind, "unknown"))
        by_label = {r.label: r for r in results}
        assert page.input_value("#fn") == "Jane"
        assert page.input_value("#st") == "New Jersey"
        assert page.inner_text("#src-btn").strip() == "Company website"   # custom dropdown
        assert page.is_checked("#sp-n")                                    # "No" out of a Yes/No radio group
        assert page.is_checked("#v3")                                      # "I don't wish to answer"
        assert page.is_checked("#tc")                                      # consent checkbox
        assert page.evaluate("document.getElementById('cv').files[0].name") == "resume.docx"
        assert by_label["Which of our company values resonates with you most?"].status == "unknown"
        browser.close()


def _run_tenant(*, auto_submit: bool, stop_on_unknown: bool, profile: ApplyProfile, tmp_path: Path,
                extra_answers: dict | None = None) -> tuple[dict | None, list, list, Exception | None]:
    from playwright.sync_api import sync_playwright

    resume = tmp_path / "Resume.docx"
    resume.write_bytes(b"PK\x03\x04 resume")
    events: list[tuple] = []
    unknowns: list[tuple] = []
    ctx = workday.ApplyContext(
        profile=profile, credential=workday.Credential("me@example.test", "a-long-password"),
        resume_path=resume,
        experience=[{"title": "Solution Lead, Enterprise Integration", "org": "Example Life",
                     "location": "Clinton, NJ", "start": "09/2018", "end": "10/2025", "text": "Led integration."}],
        stored_answers=extra_answers or {},
        auto_submit=auto_submit, stop_on_unknown=stop_on_unknown,
        on_event=lambda kind, label, detail: events.append((kind, label, detail)),
        on_unknown=lambda label, kind, options, stage: unknowns.append((label, kind, stage)),
    )
    result, error = None, None
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            result = workday.run(page, ctx, (TENANT / "index.html").as_uri(), shot=lambda name: None)
        except workday.ApplyStopped as exc:
            error = exc
        finally:
            browser.close()
    return result, events, unknowns, error


@needs_browser
def test_a_run_stops_on_the_review_page_when_auto_submit_is_off(profile, tmp_path):
    """The default: fill everything, stop for the human. Nothing is submitted."""
    profile.questions.append(CustomAnswer(match="company values", answer="Integrity"))
    result, events, unknowns, error = _run_tenant(auto_submit=False, stop_on_unknown=True, profile=profile,
                                                  tmp_path=tmp_path)
    assert error is None, error
    assert result["submitted"] is False and result["stage"] == "review"
    assert "My Information" in result["stages"] and "Application Questions" in result["stages"]
    assert result["totals"]["filled"] >= 8
    assert not unknowns
    assert any(kind == "stopped" for kind, _, _ in events)


@needs_browser
def test_a_run_submits_only_when_you_have_turned_that_on(profile, tmp_path):
    profile.questions.append(CustomAnswer(match="company values", answer="Integrity"))
    result, events, _, error = _run_tenant(auto_submit=True, stop_on_unknown=True, profile=profile, tmp_path=tmp_path)
    assert error is None, error
    assert result["submitted"] is True and result["stage"] == "submitted"
    assert any(kind == "submitted" for kind, _, _ in events)


@needs_browser
def test_an_unanswerable_question_stops_the_run_and_is_recorded(profile, tmp_path):
    result, _, unknowns, error = _run_tenant(auto_submit=True, stop_on_unknown=True, profile=profile,
                                             tmp_path=tmp_path)
    assert result is None and isinstance(error, workday.ApplyStopped)
    assert error.reason == "needs_input"
    assert any("company values" in label.lower() for label, _, _ in unknowns)


@needs_browser
def test_an_answer_you_saved_earlier_is_reused(profile, tmp_path):
    """Answer a question once on the questions page and the next run fills it in by itself."""
    answers = {normalize("Which of our company values resonates with you most?"): "Integrity"}
    result, _, unknowns, error = _run_tenant(auto_submit=False, stop_on_unknown=True, profile=profile,
                                             tmp_path=tmp_path, extra_answers=answers)
    assert error is None and not unknowns
    assert result["stage"] == "review"


@needs_browser
def test_work_history_is_typed_in_rather_than_parsed_from_the_resume(profile, tmp_path):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "experience.html").as_uri())
        ctx = workday.ApplyContext(
            profile=profile, credential=workday.Credential("me@example.test", "pw"),
            experience=[{"title": "Solution Lead, Enterprise Integration", "org": "Example Life",
                         "location": "Clinton, NJ", "start": "09/2018", "end": "10/2025",
                         "text": "Led enterprise integration."}])
        assert workday.fill_experience(page, ctx) == 1
        panel = page.locator('[data-automation-id="panel"]').first
        assert panel.locator('input[name="company"]').input_value() == "Example Life"
        assert panel.locator('input[name="title"]').input_value() == "Solution Lead, Enterprise Integration"
        assert panel.locator('input[name="from"]').input_value() == "09/2018"
        assert panel.locator('input[name="to"]').input_value() == "10/2025"
        browser.close()


@needs_browser
def test_a_captcha_stops_the_run_instead_of_being_worked_around(tmp_path):
    from playwright.sync_api import sync_playwright

    page_html = tmp_path / "captcha.html"
    page_html.write_text('<h2>Sign In</h2><iframe title="reCAPTCHA challenge" src="https://recaptcha.test/x"></iframe>',
                         encoding="utf-8")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(page_html.as_uri())
        with pytest.raises(workday.ApplyStopped) as exc:
            workday.check_captcha(page, "sign in")
        assert exc.value.reason == "captcha"
        browser.close()


# --------------------------------------------------------------------------------------- service and web


def test_is_workday_recognizes_tenant_urls():
    assert workday.is_workday("https://nyl.wd1.myworkdayjobs.com/en-US/NYL/job/Clinton-NJ/Lead_JR123")
    assert workday.is_workday("https://x.wd5.myworkdaysite.com/recruiting/x/careers/job/1")
    assert not workday.is_workday("https://boards.greenhouse.io/acme/jobs/1")
    assert not workday.is_workday(None)
    assert workday.tenant_host("https://nyl.wd1.myworkdayjobs.com/x") == "nyl.wd1.myworkdayjobs.com"


# --------------------------------------------------------------------------------------- a site you have never used


def _sign_in_run(page_url: str, *, profile: ApplyProfile, password: str = "my-usual-password",
                 code: str | None = "123456", create_accounts: bool = True, per_site: bool = True):
    """Drive just the account part of a run against one of the offline tenants."""
    from playwright.sync_api import sync_playwright

    events: list[tuple] = []
    created: list[tuple] = []
    ctx = workday.ApplyContext(
        profile=profile, credential=workday.Credential("me@example.test", password),
        create_accounts=create_accounts,
        strong_password=(lambda: "Gen3rated-Str0ng!pw") if per_site else None,
        verification_code=(lambda: code) if code is not None else None,
        on_account_created=lambda user, pw: created.append((user, pw)),
        on_event=lambda kind, label, detail: events.append((kind, label, detail)),
    )
    outcome, error = None, None
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(page_url)
        workday.start_application(page, ctx)
        try:
            outcome = workday.sign_in(page, ctx)
        except workday.ApplyStopped as exc:
            error = exc
        url = page.url
        browser.close()
    return outcome, created, events, error, url


@needs_browser
def test_a_site_you_have_used_before_is_not_registered_again(profile):
    """Every Workday sign-in page offers "Create Account"; that is not a reason to register a second time."""
    outcome, created, events, error, url = _sign_in_run((TENANT / "index.html").as_uri(), profile=profile,
                                                        password="a-long-password")
    assert error is None and outcome == "signed in"
    assert created == []
    assert not any(kind == "account_created" for kind, _, _ in events)
    assert url.endswith("information.html")


@needs_browser
def test_a_new_site_gets_an_account_created_and_the_password_saved(profile):
    outcome, created, events, error, url = _sign_in_run((NEW_TENANT / "index.html").as_uri(), profile=profile)
    assert error is None, error
    assert outcome == "created account"
    assert created and created[0][0] == "me@example.test"
    assert created[0][1] == "Gen3rated-Str0ng!pw"      # the tenant refused the usual one; a strong one was used
    assert any(kind == "password_policy" for kind, _, _ in events)
    assert url.endswith("information.html")            # the verification code got us into the application


@needs_browser
def test_a_strict_password_policy_without_generation_stops_with_the_reason(profile):
    outcome, created, _, error, _ = _sign_in_run((NEW_TENANT / "index.html").as_uri(), profile=profile,
                                                 per_site=False)
    assert outcome is None and created == []
    assert error.reason == "create_account"
    assert "12 characters" in str(error)               # the site's own words, so the fix is obvious


@needs_browser
def test_without_a_mailbox_a_verification_step_stops_the_run_clearly(profile):
    outcome, created, _, error, _ = _sign_in_run((NEW_TENANT / "index.html").as_uri(), profile=profile, code=None)
    assert outcome is None and error.reason == "verification"
    assert "mailbox" in str(error).lower()
    # the account exists from the moment the site accepts the form, so its password is saved even though the
    # verification step could not be finished — losing it would mean a password reset to get back in
    assert created == [("me@example.test", "Gen3rated-Str0ng!pw")]


@needs_browser
def test_a_code_that_never_arrives_stops_rather_than_guessing(profile):
    outcome, _, _, error, _ = _sign_in_run((NEW_TENANT / "index.html").as_uri(), profile=profile, code="")
    assert outcome is None and error.reason == "verification"


@needs_browser
def test_account_creation_switched_off_stops_instead_of_registering(profile):
    outcome, created, _, error, _ = _sign_in_run((NEW_TENANT / "index.html").as_uri(), profile=profile,
                                                 create_accounts=False)
    assert outcome is None and created == []
    assert error.reason == "no_account"


@needs_browser
def test_an_account_that_already_exists_falls_back_to_signing_in(profile):
    """Someone (you, by hand) already registered: creation fails, so sign in with the stored password instead."""
    from playwright.sync_api import sync_playwright

    events: list[tuple] = []
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "a-long-password"),
                              on_event=lambda kind, label, detail: events.append((kind, label, detail)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((NEW_TENANT / "create_exists.html").as_uri())
        outcome = workday.create_account(page, ctx)
        url = page.url
        browser.close()
    assert outcome == "signed in (the account already existed)"
    assert any(kind == "account_exists" for kind, _, _ in events)
    assert url.endswith("information.html")


@needs_browser
def test_a_hidden_second_form_does_not_swallow_the_sign_in(profile):
    """The bug the first beta hit on a real tenant: sign-in and create-account panels both in the DOM, so the
    details went into the invisible copy, the hidden button was "clicked", and the run stalled in silence."""
    from playwright.sync_api import sync_playwright

    events: list[tuple] = []
    created: list[tuple] = []
    ctx = workday.ApplyContext(
        profile=profile, credential=workday.Credential("me@example.test", "a-long-password"),
        strong_password=lambda: "Gen3rated-Str0ng!pw", verification_code=lambda: "654321",
        on_account_created=lambda user, pw: created.append((user, pw)),
        on_event=lambda kind, label, detail: events.append((kind, label, detail)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((NEW_TENANT / "login_both_forms.html").as_uri())
        outcome = workday.sign_in(page, ctx)
        url = page.url
        browser.close()

    # the fixture only reports "no account" once the *visible* email field has a value and its own button is
    # clicked, so this message is proof that the hidden copy was not the one being filled
    assert any("could not find an account" in detail for _, _, detail in events)
    assert outcome == "created account"
    assert created == [("me@example.test", "a-long-password")]
    assert url.endswith("information.html")                    # verification code accepted, into the application


@needs_browser
def test_visible_only_element_lookup(profile):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((NEW_TENANT / "login_both_forms.html").as_uri())
        email = workday.visible(page, '[data-automation-id="email"]')
        assert email is not None and email.get_attribute("id") == "s-em"   # not the hidden create-account copy
        assert workday.visible(page, '[data-automation-id="verifyPassword"]') is None
        assert workday.visible(page, "#nothing-like-this") is None
        browser.close()


@needs_browser
def test_a_dead_submit_button_is_reported_not_ignored(profile, tmp_path):
    """A click that changes nothing must show up in the run log, rather than looking like a successful step."""
    from playwright.sync_api import sync_playwright

    page_html = tmp_path / "dead.html"
    page_html.write_text('<h2>Sign In</h2><label for="e">Email</label><input id="e" data-automation-id="email">'
                         '<label for="p">Password</label><input id="p" type="password">'
                         '<button data-automation-id="signInSubmitButton">Sign In</button>', encoding="utf-8")
    events: list[tuple] = []
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "pw"),
                              create_accounts=False,
                              on_event=lambda kind, label, detail: events.append((kind, label, detail)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(page_html.as_uri())
        with pytest.raises(workday.ApplyStopped) as exc:
            workday.sign_in(page, ctx)
        browser.close()
    assert exc.value.reason == "no_account"
    assert any("did nothing" in detail for _, _, detail in events)
    assert any(kind == "click" for kind, _, _ in events)        # every click attempt is in the log


@needs_browser
def test_typing_reaches_a_controlled_field_that_ignores_fill(profile):
    """Workday's inputs only commit on real key events; a programmatic fill leaves the button disabled."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((NEW_TENANT / "login_controlled.html").as_uri())
        page.locator("#em").fill("me@example.test")          # what the first beta did
        page.locator("#pw").fill("a-long-password")
        assert page.locator("#btn").is_disabled()            # ... and why nothing happened

        assert workday.type_into(page.locator("#em"), "me@example.test")
        assert workday.type_into(page.locator("#pw"), "a-long-password")
        assert page.locator("#btn").is_enabled()             # the form now agrees it has been filled in
        browser.close()


@needs_browser
def test_a_controlled_sign_in_form_is_actually_submitted(profile):
    """End to end over the same page: sign in is attempted for real, the site says there is no account, and the
    run moves on to creating one instead of stalling."""
    from playwright.sync_api import sync_playwright

    events: list[tuple] = []
    created: list[tuple] = []
    ctx = workday.ApplyContext(
        profile=profile, credential=workday.Credential("me@example.test", "a-long-password"),
        strong_password=lambda: "Gen3rated-Str0ng!pw", verification_code=lambda: "654321",
        on_account_created=lambda user, pw: created.append((user, pw)),
        on_event=lambda kind, label, detail: events.append((kind, label, detail)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((NEW_TENANT / "login_controlled.html").as_uri())
        outcome = workday.sign_in(page, ctx)
        url = page.url
        browser.close()

    assert any("could not find an account" in detail for _, _, detail in events)   # the click really happened
    assert not any(kind == "stalled" for kind, _, _ in events)
    assert outcome == "created account" and created
    assert url.endswith("information.html")


@needs_browser
def test_a_form_that_will_not_submit_is_never_recorded_as_an_account(profile, tmp_path):
    """The First American failure: both clicks did nothing, and the run recorded an account that did not exist."""
    from playwright.sync_api import sync_playwright

    dead = tmp_path / "dead_create.html"
    dead.write_text(
        '<h2>Create Account</h2>'
        '<label for="e">Email Address</label><input id="e" data-automation-id="email">'
        '<label for="p">Password</label><input id="p" type="password" data-automation-id="password">'
        '<label for="v">Verify New Password</label><input id="v" type="password" data-automation-id="verifyPassword">'
        '<button data-automation-id="createAccountSubmitButton" disabled>Create Account</button>', encoding="utf-8")
    events: list[tuple] = []
    created: list[tuple] = []
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "a-long-password"),
                               on_account_created=lambda u, pw: created.append((u, pw)),
                               on_event=lambda kind, label, detail: events.append((kind, label, detail)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(dead.as_uri())
        with pytest.raises(workday.ApplyStopped) as exc:
            workday.create_account(page, ctx)
        browser.close()

    assert created == []                      # nothing is saved for an account that was never created
    assert exc.value.reason == "create_account"
    assert "would not submit" in str(exc.value)
    assert any("disabled" in detail for _, _, detail in events)   # and the log says exactly why


@needs_browser
def test_a_site_that_opens_at_create_account_is_handled(profile):
    """First American's shape: Apply lands on Create Account, with no sign-in button in the DOM at all."""
    from playwright.sync_api import sync_playwright

    events: list[tuple] = []
    created: list[tuple] = []
    ctx = workday.ApplyContext(
        profile=profile, credential=workday.Credential("me@example.test", "a-long-password"),
        verification_code=lambda: "654321",
        on_account_created=lambda user, pw: created.append((user, pw)),
        on_event=lambda kind, label, detail: events.append((kind, label, detail)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((NEW_TENANT / "create_first.html").as_uri())
        assert workday.panel(page) == "create"
        outcome = workday.sign_in(page, ctx)
        url = page.url
        browser.close()

    assert outcome == "created account"
    assert created == [("me@example.test", "a-long-password")]
    assert url.endswith("information.html")
    # the sign-in attempt used the sign-in panel, and its "no account" message was read from a plain paragraph
    assert any("could not find an account" in detail for _, _, detail in events)
    assert not any("did nothing" in detail for _, _, detail in events)


@needs_browser
def test_panel_detection(profile):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((NEW_TENANT / "create_first.html").as_uri())
        assert workday.panel(page) == "create"
        page.locator('[data-automation-id="signInLink"]').click()
        page.wait_for_timeout(200)
        assert workday.panel(page) == "signin"
        page.goto((TENANT / "information.html").as_uri())
        assert workday.panel(page) == "none"
        browser.close()


@needs_browser
def test_an_error_outside_the_usual_containers_is_still_read(profile):
    """Tenants put validation messages wherever they like; a stalled run with no explanation is the worst case."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<h2>Create Account</h2><p>The passwords you entered do not match.</p>")
        assert "do not match" in workday.page_complaint(page)
        page.set_content("<h2>Create Account</h2><p>Everything is fine here.</p>")
        assert workday.page_complaint(page) == ""
        page.set_content('<div data-automation-id="errorMessage">An account already exists with this email.</div>')
        assert "already exists" in workday.page_complaint(page)
        browser.close()


@needs_browser
def test_a_login_page_is_never_mistaken_for_the_review_page(profile):
    """An earlier run reported "review" while still sitting on Create Account/Sign In."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((NEW_TENANT / "create_first.html").as_uri())
        assert workday.is_review(page) is False
        page.goto((TENANT / "review.html").as_uri())
        assert workday.is_review(page) is True
        browser.close()


@pytest.mark.parametrize("message", [
    # First American's real answer to a failed sign-in: it covers no-account, wrong-password and locked at once
    "You may have entered the wrong email address or password or your account might be locked.",
    "We could not find an account with that email address.",
    "The email or password you entered is incorrect.",
    "Invalid username or password. Please try again.",
    "Sorry, we are unable to sign you in.",
])
def test_every_way_a_site_refuses_a_sign_in_leads_to_creating_the_account(message):
    assert workday._login_rejected(message)


@pytest.mark.parametrize("message", [
    "Your session has expired.",
    "This job posting is no longer available.",
    "We are performing maintenance. Please come back later.",
])
def test_an_unrelated_problem_is_not_treated_as_a_missing_account(message):
    assert not workday._login_rejected(message)


@needs_browser
def test_a_generic_credential_rejection_goes_on_to_create_the_account(profile, tmp_path):
    """The First American run: the site refuses the sign-in without saying why, so the run creates an account
    rather than stopping — which is the only way to find out whether one existed."""
    from playwright.sync_api import sync_playwright

    site = tmp_path / "vague.html"
    site.write_text(f"""
        <h2 data-automation-id="pageHeader">Sign In</h2>
        <label for="e">Email Address</label><input id="e" data-automation-id="email">
        <label for="p">Password</label><input id="p" type="password" data-automation-id="password">
        <button data-automation-id="signInSubmitButton" onclick="show()">Sign In</button>
        <a data-automation-id="createAccountLink" href="{(NEW_TENANT / "create.html").as_uri()}">Create Account</a>
        <p id="m" hidden>You may have entered the wrong email address or password or your account might be
           locked.</p>
        <script>function show() {{ document.getElementById('m').hidden = false; }}</script>
        """, encoding="utf-8")
    events: list[tuple] = []
    created: list[tuple] = []
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "a-long-password"),
                               strong_password=lambda: "Gen3rated-Str0ng!pw", verification_code=lambda: "654321",
                               on_account_created=lambda u, pw: created.append((u, pw)),
                               on_event=lambda kind, label, detail: events.append((kind, label, detail)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(site.as_uri())
        outcome = workday.sign_in(page, ctx)
        url = page.url
        browser.close()

    assert outcome == "created account" and created
    assert url.endswith("information.html")
    assert any("might be locked" in detail for _, _, detail in events)   # the site's own words are in the log


@needs_browser
def test_a_click_swallowed_by_an_overlay_still_gets_through(profile):
    """The First American stall: the form was filled, the button enabled, the click "successful" — and nothing
    happened, because a forced click goes to whatever covers the button and still reports success."""
    from playwright.sync_api import sync_playwright

    events: list[tuple] = []
    created: list[tuple] = []
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "a-long-password"),
                               verification_code=lambda: "654321",
                               on_account_created=lambda u, pw: created.append((u, pw)),
                               on_event=lambda kind, label, detail: events.append((kind, label, detail)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((NEW_TENANT / "create_covered.html").as_uri())
        outcome = workday.create_account(page, ctx)
        url = page.url
        browser.close()

    assert outcome == "created account" and created
    assert url.endswith("information.html")
    assert any("covered by" in detail for _, _, detail in events)        # the obstruction is named in the log
    assert any("moved on" in detail for _, _, detail in events)          # and so is the strategy that worked


# --------------------------------------------------------------------------------------- pressing submit once


SLOW_FORM = """
<h2 data-automation-id="pageHeader">Create Account</h2>
<style>#shield { position: absolute; z-index: 3; }</style>
<form method="POST" action="/register">
  <label for="em">Email Address</label><input id="em" data-automation-id="email" name="email">
  <label for="pw">Password</label><input id="pw" type="password" data-automation-id="password" name="password">
  <button id="go" data-automation-id="createAccountSubmitButton" type="submit">Create Account</button>
</form>
<script>
  // a click shield over the button, the way Workday's click_filter sits over its own
  const b = document.getElementById('go').getBoundingClientRect();
  const shield = document.createElement('div');
  shield.id = 'shield';
  Object.assign(shield.style, {left: b.left + 'px', top: b.top + 'px', width: b.width + 'px',
                               height: b.height + 'px', background: 'rgba(0,0,0,0.01)'});
  document.body.appendChild(shield);
</script>
"""


def _serve(page, posts: list, *, delay: float = 2.0):  # noqa: ANN001
    """Answer requests in-process: the form page on GET, and a slow answer on POST."""
    import time as _time

    def handler(route, request):  # noqa: ANN001
        if request.method == "POST":
            posts.append(request.url)
            _time.sleep(delay)          # Workday takes a moment to answer; the page looks untouched meanwhile
            route.fulfill(status=200, content_type="text/html",
                          body="<h2 data-automation-id='pageHeader'>My Information</h2>")
            return
        route.fulfill(status=200, content_type="text/html", body=SLOW_FORM)

    page.route("**/*", handler)


@needs_browser
def test_a_slow_form_is_pressed_once_not_five_times(profile):
    """The First American bug: the press worked, but the page still looked untouched while the POST was in
    flight, so it was judged a failure and retried — five registrations from one attempt."""
    from playwright.sync_api import sync_playwright

    posts: list[str] = []
    events: list[tuple] = []
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "a-long-password"),
                              on_event=lambda kind, label, detail: events.append((kind, label, detail)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        _serve(page, posts)
        page.goto("http://tenant.test/apply")
        moved = workday.submit_and_confirm(page, ctx, ['[data-automation-id="createAccountSubmitButton"]'],
                                           stage="create account")
        browser.close()

    assert moved is True
    assert len(posts) == 1, f"the form was submitted {len(posts)} times"
    assert any("the form was sent" in detail for _, _, detail in events)
    assert not any("stalled" == kind for kind, _, _ in events)


@needs_browser
def test_a_press_that_reaches_nothing_is_still_reported(profile):
    """The other half: a button that genuinely does nothing must not be called a success."""
    from playwright.sync_api import sync_playwright

    events: list[tuple] = []
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "pw"),
                              on_event=lambda kind, label, detail: events.append((kind, label, detail)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<h2>Create Account</h2>"
                         "<button data-automation-id='createAccountSubmitButton'>Create Account</button>")
        assert workday.submit_and_confirm(page, ctx, ['[data-automation-id="createAccountSubmitButton"]'],
                                          stage="create account") is False
        browser.close()
    assert any(kind == "stalled" for kind, _, _ in events)
    assert any("nothing was sent" in detail for _, _, detail in events)


@needs_browser
def test_a_tenant_that_emails_a_link_is_activated_and_carries_on(profile):
    """First American confirms by link, not by code: there is nothing to type, so the run reads the email,
    follows the link, and signs in with the account it just made."""
    from playwright.sync_api import sync_playwright

    events: list[tuple] = []
    link = (NEW_TENANT / "activate.html").as_uri()
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "a-long-password"),
                               verification_code=lambda: None, verification_link=lambda: link,
                               on_event=lambda kind, label, detail: events.append((kind, label, detail)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((NEW_TENANT / "check_email.html").as_uri())
        assert workday.verification_wanted(page)      # "check your email" counts, not just a code box
        assert workday.handle_verification(page, ctx, "create account") is True
        url = page.url
        browser.close()

    assert url.endswith("information.html")           # activated, signed in, and into the application
    assert any("activation link" in detail for _, _, detail in events)
    assert any("account activated" in detail for _, _, detail in events)


def test_the_activation_link_is_picked_out_of_the_real_email():
    """The message First American actually sent, including the requisition number that reads like a code."""
    from jobhunt.apply import verify_email

    body = ("Click this link to confirm your email address and complete setup for your candidate account\n"
            "https://firstam.wd1.myworkdayjobs.com/firstamericancareers/activate/13e6m277g4qukycv03t62w3cng/"
            "?redirect=%2Fen-US%2Ffirstamericancareers%2Fjob%2FEngineering-Director---Remote_R058770%2Fapply"
            "%2FapplyManually\nThe link will expire after 24 hours.")
    link = verify_email.extract_link(body, "firstam.wd1.myworkdayjobs.com")
    assert link and link.endswith("applyManually")    # the redirect back to the application is kept
    assert verify_email.extract_code(body) is None    # R058770 is not a verification code
    assert verify_email.looks_like_verification("First American <firstam@otp.workday.com>",
                                                "Verify your candidate account", "firstam.wd1.myworkdayjobs.com")


@needs_browser
def test_a_site_we_already_have_an_account_on_is_never_registered_again(profile):
    """If a sign-in is saved for this exact site, an account exists: a refused password means a wrong or locked
    password, not a missing account. Registering again cannot work — and the attempt overwrote the saved
    password with one the site had never accepted."""
    from playwright.sync_api import sync_playwright

    events: list[tuple] = []
    created: list[tuple] = []
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "a-long-password"),
                               known_account=True, strong_password=lambda: "Gen3rated-Str0ng!pw",
                               on_account_created=lambda u, pw: created.append((u, pw)),
                               on_event=lambda kind, label, detail: events.append((kind, label, detail)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((NEW_TENANT / "login.html").as_uri())    # this tenant always refuses the sign-in
        with pytest.raises(workday.ApplyStopped) as exc:
            workday.sign_in(page, ctx)
        browser.close()

    assert exc.value.reason == "password"
    assert "Forgot your password" in str(exc.value)
    assert created == []                                   # nothing saved, nothing overwritten
    assert not any(kind == "account_created" for kind, _, _ in events)


@needs_browser
def test_a_registration_that_lands_back_on_sign_in_is_not_a_success(profile, tmp_path):
    """Workday returns you to its own sign-in page when the email is already registered. That page is not the
    create panel, so an earlier version called it success and saved a password that never worked."""
    from playwright.sync_api import sync_playwright

    site = tmp_path / "back_to_signin.html"
    site.write_text(f"""
        <h2 data-automation-id="pageHeader">Create Account</h2>
        <form onsubmit="location.href='{(NEW_TENANT / "login.html").as_uri()}'; return false;">
          <label for="em">Email Address</label><input id="em" data-automation-id="email">
          <label for="pw">Password</label><input id="pw" type="password" data-automation-id="password">
          <label for="vp">Verify New Password</label><input id="vp" type="password" data-automation-id="verifyPassword">
          <button data-automation-id="createAccountSubmitButton" type="submit">Create Account</button>
        </form>
        """, encoding="utf-8")
    created: list[tuple] = []
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "a-long-password"),
                               on_account_created=lambda u, pw: created.append((u, pw)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(site.as_uri())
        with pytest.raises(workday.ApplyStopped) as exc:
            workday.create_account(page, ctx)
        browser.close()

    assert exc.value.reason == "create_account"
    assert "already an account" in str(exc.value)
    assert created == []


@needs_browser
def test_a_wizard_step_is_never_mistaken_for_a_verification_prompt(profile):
    """A run signed in fine, landed on My Information, and sat there for eight minutes waiting for an email the
    site had never sent — because the page's own wording matched the verification patterns."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "information.html").as_uri())
        assert workday.current_stage(page) == "My Information"
        assert workday.verification_wanted(page) is False

        # even wording that looks like a gate does not count while a wizard step is on screen
        page.set_content('<h2 data-automation-id="pageHeader">My Information</h2>'
                         '<p>We emailed you a copy of this application. Check your email for confirmation.</p>'
                         '<button data-automation-id="pageFooterNextButton">Save and Continue</button>')
        assert workday.verification_wanted(page) is False

        # a real gate still registers
        page.goto((NEW_TENANT / "check_email.html").as_uri())
        assert workday.verification_wanted(page) is True
        page.goto((NEW_TENANT / "verify.html").as_uri())
        assert workday.verification_wanted(page) is True
        browser.close()


# --------------------------------------------------------------------------------------- My Experience


@needs_browser
def test_the_resume_is_attached_to_a_hidden_upload_field(profile, tmp_path):
    """The input behind "Drop files here / Select files" is display:none, so the ordinary field scan never saw
    it and the resume went unattached on a real application."""
    from playwright.sync_api import sync_playwright

    resume = tmp_path / "Jane-Doe-Resume.docx"
    resume.write_bytes(b"PK resume")
    events: list[tuple] = []
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "pw"),
                               resume_path=resume,
                               on_event=lambda kind, label, detail: events.append((kind, label, detail)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "my_experience.html").as_uri())
        assert page.locator("#cv").is_visible() is False        # hidden, as on the real site
        assert workday.upload_documents(page, ctx) == 1
        assert page.evaluate("() => document.getElementById('cv').files[0].name") == "Jane-Doe-Resume.docx"
        assert workday.upload_documents(page, ctx) == 0         # already attached: never attached twice
        browser.close()
    assert any(kind == "uploaded" for kind, _, _ in events)


@needs_browser
def test_work_history_sections_are_found_by_their_heading(profile):
    """Workday builds these as role="group" around an <h4>. Guessing an automation id found nothing, so the work
    history was never typed in even though the Add button was right there."""
    from playwright.sync_api import sync_playwright

    ctx = workday.ApplyContext(
        profile=profile, credential=workday.Credential("me@example.test", "pw"),
        experience=[{"title": "Solution Lead, Enterprise Integration", "org": "Example Life",
                     "location": "Clinton, NJ", "start": "09/2018", "end": "10/2025", "text": "Led integration."},
                    {"title": "Advisor, Applications", "org": "Example Health", "location": "Florham Park, NJ",
                     "start": "02/2017", "end": "09/2018", "text": "Web applications."}],
        education=[{"org": "Drexel University", "degree": "Master of Science Information Systems",
                    "title": "Master of Science Information Systems", "field_of_study": "Information Systems"}])
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "my_experience.html").as_uri())
        assert workday._section_for(page, "work experience") is not None
        assert workday._section_for(page, "education") is not None
        assert workday.fill_experience(page, ctx) == 3           # two roles and one school
        panels = page.locator('[data-automation-id="panel"]')
        assert panels.count() == 3
        assert panels.nth(0).locator('input[name="company"]').input_value() == "Example Life"
        assert panels.nth(0).locator('input[name="from"]').input_value() == "09/2018"
        assert panels.nth(1).locator('input[name="company"]').input_value() == "Example Health"
        browser.close()


@needs_browser
def test_a_role_with_no_end_date_is_marked_as_current(profile):
    """"I currently work here" is not a question to ask: the fact bank already says which role has no end."""
    from playwright.sync_api import sync_playwright

    ctx = workday.ApplyContext(
        profile=profile, credential=workday.Credential("me@example.test", "pw"),
        experience=[{"title": "Project Lead", "org": "Acme Insurance Inc.", "location": "Springfield, IL",
                     "start": "01/2026", "end": "", "text": "Leading integration work."},
                    {"title": "Solution Lead", "org": "Example Life", "location": "Clinton, NJ",
                     "start": "09/2018", "end": "10/2025", "text": "Led integration."}])
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "my_experience.html").as_uri())
        workday.fill_experience(page, ctx)
        panels = page.locator('[data-automation-id="panel"]')
        current = panels.nth(0).locator('input[name="current"]')
        past = panels.nth(1).locator('input[name="current"]')
        assert current.is_checked() is True          # no end date: still there
        assert past.is_checked() is False
        browser.close()


@needs_browser
def test_a_url_the_site_rejects_is_reported_not_retried(profile):
    """An "Invalid LinkedIn URL" made one run press Save and Continue four times over."""
    from playwright.sync_api import sync_playwright

    profile.links.linkedin = "https://linkedin.com/in/example/"   # no www: this tenant rejects it
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "pw"))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "my_experience.html").as_uri())
        workday.fill_step(page, ctx, "My Experience")
        page.locator('[data-automation-id="pageFooterNextButton"]').click()
        page.wait_for_timeout(300)
        assert "Invalid LinkedIn URL" in workday.page_complaint(page)
        assert page.url.endswith("my_experience.html")            # it did not advance

        # with the canonical URL the same step goes through
        page.goto((TENANT / "my_experience.html").as_uri())
        profile.links.linkedin = "https://www.linkedin.com/in/example/"
        workday.fill_step(page, ctx, "My Experience")
        page.locator('[data-automation-id="pageFooterNextButton"]').click()
        page.wait_for_timeout(400)
        assert page.url.endswith("questions.html")
        browser.close()


@needs_browser
def test_the_sites_own_error_page_is_reloaded(profile):
    """Workday answers "Something went wrong — please refresh the page", and refreshing is what clears it."""
    from playwright.sync_api import sync_playwright

    events: list[tuple] = []
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "pw"),
                               on_event=lambda kind, label, detail: events.append((kind, label, detail)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<h2>Something went wrong</h2><p>Please refresh the page and then try again.</p>"
                         "<p>Error Code: VPS|33b84cec</p>")
        assert workday.recover_from_glitch(page, ctx, "My Information") is True
        assert any(kind == "retry" for kind, _, _ in events)
        page.goto((TENANT / "my_experience.html").as_uri())
        assert workday.recover_from_glitch(page, ctx) is False     # a working page is left alone
        browser.close()


def test_a_linkedin_url_is_stored_the_way_a_form_will_accept_it(tmp_path):
    from jobhunt.apply.profile import canonical_url

    assert canonical_url("linkedin.com/in/x/") == "https://www.linkedin.com/in/x/"
    assert canonical_url("https://linkedin.com/in/x/") == "https://www.linkedin.com/in/x/"
    assert canonical_url("https://www.linkedin.com/in/x/") == "https://www.linkedin.com/in/x/"
    assert canonical_url("") == ""
    p = ApplyProfile()
    p.links.linkedin = "linkedin.com/in/y"
    save_profile(tmp_path / "apply_profile.yaml", p)
    assert load_profile(tmp_path / "apply_profile.yaml").links.linkedin == "https://www.linkedin.com/in/y"


def test_rendered_documents_are_found_for_a_job(cfg, engine, tmp_path):
    """`file_path` holds JSON text and `file_paths` the decoded list. Iterating the raw string walks it one
    character at a time, and on Windows a lone backslash exists as a path — so a run "found" a resume that was
    really the root of the drive, and attached nothing."""
    from sqlalchemy import text as sql

    from jobhunt.graphs.runtime import Runtime
    from jobhunt.sources.normalize import normalize_raw
    from tests.conftest import raw_job

    _, job_id = queries.upsert_job(engine, normalize_raw(raw_job(1)))
    resume = tmp_path / "Resume.docx"
    resume.write_bytes(b"PK")
    with engine.begin() as c:
        c.execute(sql("INSERT INTO documents (job_id, kind, version, markdown, file_path, created_at) "
                      "VALUES (:j, 'resume', 1, 'x', :p, '2026-01-01T00:00:00Z')"),
                  {"j": job_id, "p": queries.dumps([str(resume)])})
    rt_ = Runtime(cfg=cfg, engine=engine, checkpointer=None, executor=None)  # type: ignore[arg-type]
    found, _ = autoapply.documents_for(rt_, job_id)
    assert found == resume


@needs_browser
def test_autofill_mode_hands_the_resume_to_the_site(profile, tmp_path):
    """The owner's preference: let the site parse the resume rather than matching every field ourselves."""
    from playwright.sync_api import sync_playwright

    resume = tmp_path / "Resume.docx"
    resume.write_bytes(b"PK resume")
    events: list[tuple] = []
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "pw"),
                               resume_path=resume, apply_mode="autofill",
                               on_event=lambda kind, label, detail: events.append((kind, label, detail)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "index_autofill.html").as_uri())
        workday.start_application(page, ctx)
        url = page.url
        browser.close()
    assert url.endswith("information.html")                    # the parse took us into the wizard
    assert any("autofill with resume" in detail for _, _, detail in events)
    assert any(kind == "uploaded" for kind, _, _ in events)


@needs_browser
def test_manual_mode_is_still_available(profile, tmp_path):
    from playwright.sync_api import sync_playwright

    resume = tmp_path / "Resume.docx"
    resume.write_bytes(b"PK resume")
    events: list[tuple] = []
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "pw"),
                               resume_path=resume, apply_mode="manual",
                               on_event=lambda kind, label, detail: events.append((kind, label, detail)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "index_autofill.html").as_uri())
        workday.start_application(page, ctx)
        url = page.url
        browser.close()
    assert url.endswith("information.html")
    assert any("applyManually" in detail for _, _, detail in events)
    assert not any(kind == "uploaded" for kind, _, _ in events)


@needs_browser
def test_a_panel_that_is_still_appearing_is_not_read_as_a_bot_trap(profile):
    """Adding a role animates its panel in; for a moment every field reports a zero-size box. One run skipped
    Job Title, Company, Location and every date that way, then stopped on the questions it had skipped."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("""
            <h2>My Experience</h2>
            <style>
              .panel { position: absolute; left: -3000px; transition: left .2s; }
              .panel.ready { position: static; left: auto; }
            </style>
            <div class="panel" id="p">
              <label for="jt">Job Title</label><input id="jt">
              <label for="co">Company</label><input id="co">
            </div>
            <script>setTimeout(() => document.getElementById('p').className = 'panel ready', 250);</script>
        """)
        labels = [f.label for f in forms.scan(page)]            # scan starts while it is still off screen
        browser.close()
    assert "Job Title" in labels and "Company" in labels


@needs_browser
def test_an_empty_dropdown_is_not_treated_as_answered(profile):
    """Workday shows "Select One" in an empty dropdown; reading that as its value left every one unset."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<label for="d">Degree</label>'
                         '<button id="d" aria-haspopup="listbox" aria-label="Degree">Select One</button>')
        field = next(f for f in forms.scan(page) if f.label == "Degree")
        assert field.filled is False
        browser.close()


def test_a_degree_gives_up_its_field_of_study():
    assert autoapply._field_of_study("Master of Science (MS) Information Systems") == "Information Systems"
    assert autoapply._field_of_study("Bachelor of Science (BS) Computer Science, Magna Cum Laude") \
        == "Computer Science"


@needs_browser
def test_a_date_pair_is_filled_from_the_group_it_sits_in(profile):
    """Workday writes From and To as a Month input and a Year input inside a labelled group. On their own those
    fields are just "Month" and "Year", so the dates never got filled in at all."""
    from playwright.sync_api import sync_playwright

    ctx = workday.ApplyContext(
        profile=profile, credential=workday.Credential("me@example.test", "pw"),
        experience=[{"title": "Solution Lead, Enterprise Integration", "org": "Example Life",
                     "location": "Clinton, NJ", "start": "09/2018", "end": "10/2025",
                     "text": "Led enterprise integration."},
                    {"title": "Project Lead", "org": "Acme Insurance Inc.", "location": "Springfield, IL",
                     "start": "01/2026", "end": "", "text": "Leading integration work."}])
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "experience_panel.html").as_uri())
        assert workday.fill_experience(page, ctx) == 2
        panels = page.locator('[data-automation-id="panel"]')

        first = panels.nth(0)
        assert first.locator('input[name="title"]').input_value() == "Solution Lead, Enterprise Integration"
        assert first.locator('input[name="company"]').input_value() == "Example Life"
        assert first.locator('input[name="location"]').input_value() == "Clinton, NJ"
        assert first.locator('input[name="from-month"]').input_value() == "09"
        assert first.locator('input[name="from-year"]').input_value() == "2018"
        assert first.locator('input[name="to-month"]').input_value() == "10"
        assert first.locator('input[name="to-year"]').input_value() == "2025"
        assert first.locator('input[name="current"]').is_checked() is False
        assert "integration" in first.locator('textarea[name="desc"]').input_value().lower()

        second = panels.nth(1)
        assert second.locator('input[name="from-month"]').input_value() == "01"
        assert second.locator('input[name="from-year"]').input_value() == "2026"
        assert second.locator('input[name="to-month"]').input_value() == ""   # still there: no end date
        assert second.locator('input[name="current"]').is_checked() is True
        browser.close()


@needs_browser
def test_a_field_knows_which_group_it_belongs_to(profile):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "experience_panel.html").as_uri())
        page.locator('[data-automation-id="Add"]').click()
        page.wait_for_timeout(300)
        groups = {(f.label, f.group) for f in forms.scan(page)}
        assert ("Month", "From") in groups
        assert ("Year", "To") in groups
        assert ("Job Title", "Job Title") not in groups      # a field's own label is not its group
        browser.close()


@needs_browser
def test_a_dropdown_whose_options_are_radio_buttons(profile):
    """Country and Country Phone Code open a panel of radios, not a listbox. Clicking the button and looking for
    [role=option] left the panel hanging open with nothing chosen."""
    from playwright.sync_api import sync_playwright

    profile.identity.country = "United States of America"
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "pw"))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "radio_dropdown.html").as_uri())
        results = workday.fill_step(page, ctx, "My Information")
        chosen = page.locator("#country-btn").inner_text().strip()
        panel_open = not page.locator("#country-panel").is_hidden()
        browser.close()

    assert chosen == "United States of America"
    assert panel_open is False               # choosing closed the panel; it is not left hanging open
    assert any(r.status == "filled" and r.label == "Country" for r in results)


@needs_browser
def test_panels_left_blank_by_the_sites_parser_are_completed(profile):
    """Autofill creates the panels; whatever it could not read stays blank. Those blanks are ours to fill, not
    questions to stop the run over."""
    from playwright.sync_api import sync_playwright

    events: list[tuple] = []
    ctx = workday.ApplyContext(
        profile=profile, credential=workday.Credential("me@example.test", "pw"),
        experience=[{"title": "Project Lead", "org": "Acme Insurance Inc.", "location": "Springfield, IL",
                     "start": "01/2026", "end": "", "text": "Leading integration work."},
                    {"title": "Solution Lead, Enterprise Integration", "org": "Example Life",
                     "location": "Clinton, NJ", "start": "09/2018", "end": "10/2025", "text": "Led integration."}],
        on_event=lambda kind, label, detail: events.append((kind, label, detail)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "experience_prefilled.html").as_uri())
        workday.fill_experience(page, ctx)
        panels = page.locator('[data-automation-id="panel"]')
        first_company = panels.nth(0).locator('input[name="company"]').input_value()
        second_title = panels.nth(1).locator('input[name="title"]').input_value()
        second_company = panels.nth(1).locator('input[name="company"]').input_value()
        second_month = panels.nth(1).locator('input[name="from-month"]').input_value()
        browser.close()

    assert first_company == "Acme Insurance Inc."                       # what the parser got right is left alone
    assert second_title == "Solution Lead, Enterprise Integration"
    assert second_company == "Example Life"                 # the blank panel is completed
    assert second_month == "09"
    assert any(kind == "kept" for kind, _, _ in events)
    assert any(kind == "completed" for kind, _, _ in events)


@needs_browser
def test_a_dropdown_under_a_click_shield_still_opens_and_is_set(profile):
    """The Country and Country Phone Code failure: the button sits under Workday's click_filter, so a plain
    click opens nothing. The run found no options, moved on, came back, and never chose anything."""
    from playwright.sync_api import sync_playwright

    profile.identity.country = "United States of America"
    profile.identity.country_phone_code = "United States of America (+1)"
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "pw"))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "shielded_dropdown.html").as_uri())
        results = {r.label: r for r in workday.fill_step(page, ctx, "My Information")}
        country = page.locator("#country-btn").inner_text().strip()
        code = page.locator("#code-btn").inner_text().strip()
        panels_open = [not page.locator(sel).is_hidden() for sel in ("#country-panel", "#code-panel")]
        browser.close()

    assert country == "United States of America"
    assert code == "United States of America (+1)"
    assert panels_open == [False, False]                     # both closed: a choice was actually made
    assert results["Country"].status == "filled"
    assert results["Country Phone Code"].status == "filled"


@needs_browser
def test_a_dropdown_that_will_not_close_is_reported_rather_than_claimed(profile):
    """A menu still on screen after choosing means nothing was set — saying "filled" there would hide it."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("""
            <span id="lab">Country</span>
            <button id="b" aria-haspopup="listbox" aria-labelledby="lab" onclick="p.hidden=false">Select One</button>
            <div id="p" hidden>
              <div role="option" onclick="return false;">United States of America</div>
              <div role="option" onclick="return false;">Canada</div>
            </div>
        """)
        profile.identity.country = "United States of America"
        field = next(f for f in forms.scan(page) if f.label == "Country")
        result = forms.fill(field, "United States of America")
        browser.close()
    assert result.status == "no_option"
    assert "stayed open" in result.detail


@needs_browser
def test_entries_are_filled_even_when_nothing_marks_them_as_panels(profile):
    """The real failure: entries with no data-automation-id="panel" and no fieldset. Identifying an entry by its
    container added several and filled none. A repeated label is what actually marks a new entry."""
    from playwright.sync_api import sync_playwright

    events: list[tuple] = []
    ctx = workday.ApplyContext(
        profile=profile, credential=workday.Credential("me@example.test", "pw"),
        experience=[{"title": "Project Lead", "org": "Acme Insurance Inc.", "location": "Springfield, IL",
                     "start": "01/2026", "end": "", "text": "Leading modernization work."},
                    {"title": "Solution Lead, Enterprise Integration", "org": "Example Life",
                     "location": "Clinton, NJ", "start": "09/2018", "end": "10/2025", "text": "Led integration."},
                    {"title": "Advisor, Applications", "org": "Example Health", "location": "Florham Park, NJ",
                     "start": "02/2017", "end": "09/2018", "text": "Web applications."}],
        on_event=lambda kind, label, detail: events.append((kind, label, detail)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "experience_unmarked.html").as_uri())
        filled = workday.fill_experience(page, ctx)
        values = page.evaluate("""() => {
            const get = (n) => (document.querySelector('[name="' + n + '"]') || {}).value;
            const checked = (n) => (document.querySelector('[name="' + n + '"]') || {}).checked;
            return {titles: [get('title-1'), get('title-2'), get('title-3')],
                    companies: [get('company-1'), get('company-2'), get('company-3')],
                    from1: [get('from-month-1'), get('from-year-1')],
                    to2: [get('to-month-2'), get('to-year-2')],
                    current1: checked('current-1'), current2: checked('current-2'),
                    desc2: get('desc-2')};
        }""")
        browser.close()

    assert filled == 3
    assert values["companies"] == ["Acme Insurance Inc.", "Example Life", "Example Health"]
    assert values["titles"][1] == "Solution Lead, Enterprise Integration"
    assert values["from1"] == ["01", "2026"]
    assert values["to2"] == ["10", "2025"]
    assert values["current1"] is True and values["current2"] is False
    assert "integration" in (values["desc2"] or "").lower()
    assert sum(1 for kind, _, _ in events if kind in ("added", "completed")) == 3


@needs_browser
def test_entries_are_split_on_a_repeated_label(profile):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "experience_unmarked.html").as_uri())
        page.locator('[data-automation-id="Add Another"]').click()
        page.locator('[data-automation-id="Add Another"]').click()
        page.wait_for_timeout(200)
        section = workday._section_for(page, "work experience")
        groups = workday._entry_groups(forms.scan(section))
        browser.close()

    assert len(groups) == 2                          # two entries, told apart by the repeat
    assert [f.label for f in groups[0]][:3] == ["Job Title", "Company", "Location"]
    assert all(workday._group_org(g) == "" for g in groups)


@needs_browser
def test_dates_are_filled_whatever_shape_the_widget_is(profile):
    """One box showing MM/YYYY, or two inputs with no labels of their own — a date can only be filled by finding
    the container labelled From or To and filling its inputs in order."""
    from playwright.sync_api import sync_playwright

    ctx = workday.ApplyContext(
        profile=profile, credential=workday.Credential("me@example.test", "pw"),
        experience=[{"title": "Solution Lead", "org": "Example Life", "location": "Clinton, NJ",
                     "start": "09/2018", "end": "10/2025", "text": "Led integration."}])
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "experience_datebox.html").as_uri())
        workday.fill_experience(page, ctx)
        values = page.evaluate("""() => {
            const v = (n) => (document.querySelector('[name="' + n + '"]') || {}).value;
            return {company: v('company-1'), from: v('from-1'),
                    to_month: v('to-month-1'), to_year: v('to-year-1')};
        }""")
        browser.close()

    assert values["company"] == "Example Life"
    assert values["from"] == "09/2018"              # a single box takes the whole thing
    assert values["to_month"] == "10"               # a pair takes it a part at a time
    assert values["to_year"] == "2025"


@needs_browser
def test_a_date_already_filled_in_is_left_alone(profile):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("""
            <div data-automation-id="dateWidget"><span>From</span>
              <input name="from" placeholder="MM/YYYY" value="01/2020"></div>
        """)
        workday.fill_date_widgets(page, {"start": "09/2018", "end": ""})
        value = page.locator('[name="from"]').input_value()
        browser.close()
    assert value == "01/2020"        # what the site's parser got is not overwritten


@needs_browser
def test_the_first_suggestion_is_taken_when_nothing_matches_exactly(profile):
    """The owner's rule: whatever the picker offers first after typing is the closest match."""
    from playwright.sync_api import sync_playwright

    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "pw"),
                               skills=["Enterprise"])
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "skills.html").as_uri())
        # the picker offers "Enterprise Integration" for "Enterprise": close, but not the same phrase
        added = workday.fill_skills(page, ctx)
        chosen = page.locator("#chosen li").all_inner_texts()
        browser.close()
    assert added == 1
    assert chosen == ["Enterprise Integration"]


@needs_browser
def test_dates_are_found_by_the_caption_above_them(profile):
    """The tenant's From and To boxes carry no labels of their own — the only clue is the caption above them,
    and its text runs straight into the next word ("Fromcalendar")."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "education_panel.html").as_uri())
        page.locator('[data-automation-id="Add Another"]').click()
        page.wait_for_timeout(200)
        pairs = [(which, el.evaluate("e => e.name")) for which, el in workday.date_inputs(page)]
        filled = workday.fill_date_widgets(page, {"start": "09/2018", "end": "10/2025"})
        values = page.evaluate("""() => { const v = n => document.querySelector('[name="' + n + '"]').value;
            return [v('rfrom-month-1'), v('rfrom-year-1'), v('rto-month-1'), v('rto-year-1')]; }""")
        browser.close()

    assert filled == 2
    assert values == ["09", "2018", "10", "2025"]
    assert {w for w, _ in pairs} == {"start", "end"}


@needs_browser
def test_a_degree_dropdown_is_tried_with_each_spelling(profile):
    """A Degree dropdown lists degree names, and tenants abbreviate them differently, so several spellings are
    offered until one is in the list."""
    from playwright.sync_api import sync_playwright

    ctx = workday.ApplyContext(
        profile=profile, credential=workday.Credential("me@example.test", "pw"),
        education=[{"school": "Drexel University", "org": "Drexel University",
                    "degree": "Master of Science", "field_of_study": "Information Systems",
                    "degree_candidates": ["Master of Science", "MS", "Master's Degree"]},
                   {"school": "Binghamton University", "org": "Binghamton University",
                    "degree": "Bachelor of Science", "field_of_study": "Computer Science",
                    "degree_candidates": ["Bachelor of Science", "BS", "Bachelor's Degree"]}])
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "education_panel.html").as_uri())
        workday.fill_experience(page, ctx)
        values = page.evaluate("""() => { const v = n => (document.querySelector('[name="' + n + '"]')||{}).value;
            return {s1: v('school-1'), d1: v('degree-1'), f1: v('field-1'),
                    s2: v('school-2'), d2: v('degree-2'), f2: v('field-2')}; }""")
        browser.close()

    assert values["s1"] == "Drexel University"
    assert values["d1"] == "Master of Science"       # not "Master's Degree", which is not in this list
    assert values["f1"] == "Information Systems"
    assert values["s2"] == "Binghamton University"
    assert values["d2"] == "Bachelor of Science"
    assert values["f2"] == "Computer Science"


def test_the_spellings_offered_for_each_degree():
    assert autoapply._degree_candidates("Master of Science (MS) Information Systems")[:3] ==         ["Master of Science", "MS", "M.S."]
    assert autoapply._degree_candidates("Bachelor of Science (BS) Computer Science")[:3] ==         ["Bachelor of Science", "BS", "B.S."]
    assert "Master's Degree" in autoapply._degree_candidates("MBA") or         "Master of Business Administration" in autoapply._degree_candidates("MBA")


@needs_browser
def test_a_step_that_reports_an_error_stops_at_once(profile, tmp_path):
    """One run pressed Save and Continue four times against "Error: The field From is required". The site's own
    words are the answer, and they come back in the run's message."""
    from playwright.sync_api import sync_playwright

    page_html = tmp_path / "stuck.html"
    page_html.write_text("""
        <h2 data-automation-id="pageHeader">My Experience</h2>
        <label for="x">Job Title</label><input id="x">
        <p id="err">Error: The field From is required and must have a value.</p>
        <button data-automation-id="pageFooterNextButton" onclick="again()">Save and Continue</button>
        <script>
          var n = 0;
          function again() {
            n += 1;
            document.getElementById('err').textContent =
              'Error: The field From is required (attempt ' + n + ') and must have a value.';
          }
        </script>
    """, encoding="utf-8")
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "pw"),
                               stop_on_unknown=False)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(page_html.as_uri())
        with pytest.raises(workday.ApplyStopped) as exc:
            workday.run(page, ctx, page_html.as_uri(), shot=lambda name: None)
        attempts = page.evaluate("() => n")
        browser.close()

    assert exc.value.reason == "form_error"
    assert "is required" in str(exc.value)            # the site's own words
    assert attempts <= 2, "it should not keep pressing Save once the site has objected"


@needs_browser
def test_a_step_that_never_advances_stops_after_a_few_tries(profile, tmp_path):
    """The backstop for a step that changes something each time but never moves on, and says nothing useful."""
    from playwright.sync_api import sync_playwright

    page_html = tmp_path / "circling.html"
    page_html.write_text("""
        <h2 data-automation-id="pageHeader">My Experience</h2>
        <label for="x">Job Title</label><input id="x">
        <p id="note">Ready.</p>
        <button data-automation-id="pageFooterNextButton" onclick="again()">Save and Continue</button>
        <script>
          var n = 0;
          function again() { n += 1; location.hash = 'try' + n; }
        </script>
    """, encoding="utf-8")
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "pw"),
                               stop_on_unknown=False)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(page_html.as_uri())
        with pytest.raises(workday.ApplyStopped) as exc:
            workday.run(page, ctx, page_html.as_uri(), shot=lambda name: None)
        attempts = page.evaluate("() => n")
        browser.close()

    assert exc.value.reason == "form_error"
    assert "would not move on" in str(exc.value)
    assert attempts <= workday.MAX_ATTEMPTS_PER_STEP + 1


@needs_browser
def test_a_masked_date_box_is_typed_as_digits(profile):
    """This tenant's job dates are one masked MM/YYYY box: you type six digits and it writes the slash itself.
    Typing "09/2018" into that produces rubbish, and the box reads back with a slash, so the check compares
    digits rather than text."""
    from playwright.sync_api import sync_playwright

    ctx = workday.ApplyContext(
        profile=profile, credential=workday.Credential("me@example.test", "pw"),
        experience=[{"title": "Solution Lead", "org": "Example Life", "location": "Clinton, NJ",
                     "start": "09/2018", "end": "10/2025", "text": "Led integration."}],
        education=[{"school": "Drexel University", "org": "Drexel University", "degree": "Master of Science",
                    "degree_candidates": ["Master of Science", "MS"], "field_of_study": "Information Systems",
                    "start": "2001", "end": "2004"}])
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "masked_dates.html").as_uri())
        workday.fill_experience(page, ctx)
        values = page.evaluate("""() => { const v = n => (document.querySelector('[name="' + n + '"]')||{}).value;
            return {frm: v('from-1'), to: v('to-1'), degree: v('degree-1'),
                    efrom: v('efrom-1'), eto: v('eto-1')}; }""")
        browser.close()

    assert values["frm"] == "09/2018"          # typed as 092018; the mask added the slash
    assert values["to"] == "10/2025"
    assert values["efrom"] == "2001"           # a year box gets only the year
    assert values["eto"] == "2004"
    assert values["degree"] == "Master of Science (M.S.)"


@needs_browser
def test_how_a_date_is_typed_depends_on_the_box(profile):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<input id="mask" placeholder="MM/YYYY"><input id="year" placeholder="YYYY" maxlength="4">'
                         '<input id="month" aria-label="Month"><input id="plain">')
        text = {name: workday.date_text_for(page.locator("#" + name), "09/2018")
                for name in ("mask", "year", "month", "plain")}
        browser.close()

    assert text["mask"] == "092018"            # the mask writes the slash itself
    assert text["year"] == "2018"
    assert text["month"] == "09"
    assert text["plain"] == "092018"


def test_the_first_matching_degree_in_the_list_is_taken():
    """"Select the first degree that starts with Master of Science"."""
    options = ["Associate of Science", "Bachelor of Science (B.S.)", "Master of Arts (M.A.)",
               "Master of Science (M.S.)", "Master of Science in Engineering"]
    assert choose_option("Master of Science", options) == "Master of Science (M.S.)"
    assert choose_option("Bachelor of Science", options) == "Bachelor of Science (B.S.)"


# --------------------------------------------------------------------------------------- skills and degrees


@needs_browser
def test_skills_are_offered_to_the_picker_and_only_matches_are_kept(profile):
    """These pickers only accept their own suggestions, so each skill is typed, matched, and dropped when the
    site does not recognise it."""
    from playwright.sync_api import sync_playwright

    events: list[tuple] = []
    ctx = workday.ApplyContext(
        profile=profile, credential=workday.Credential("me@example.test", "pw"),
        skills=["Team Leadership", "Enterprise Integration", "Underwater Basket Weaving", "Microservices"],
        on_event=lambda kind, label, detail: events.append((kind, label, detail)))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((TENANT / "skills.html").as_uri())
        added = workday.fill_skills(page, ctx)
        chosen = page.locator("#chosen li").all_inner_texts()
        left_in_box = page.locator("#sk").input_value()
        browser.close()

    assert added == 3
    assert chosen == ["Team Leadership", "Enterprise Integration", "Microservices"]
    assert "Underwater Basket Weaving" not in chosen      # not recognised, so not forced in
    assert left_in_box == ""                              # and not left typed in the box either
    assert any(kind == "skill" for kind, _, _ in events)


def test_skills_come_from_the_fact_bank_as_written(cfg):
    _write_facts(cfg)
    cfg.paths.facts.write_text(FACTS + """
  - id: skill-core
    kind: skill
    text: "Methodologies: Agile/Scrum, Team Leadership, CI/CD Pipeline Design, Code Quality and Reviews"
    tags: [mulesoft, kafka]
""", encoding="utf-8")
    skills = autoapply.skill_list(cfg, 10)
    assert "Agile/Scrum" in skills                 # a slash is part of the term, not a separator
    assert "CI/CD Pipeline Design" in skills
    assert "Code Quality and Reviews" in skills    # nor is "and"
    assert not any(s.startswith("Methodologies") for s in skills)   # the list's own heading is not a skill
    assert "Mulesoft" in skills or "mulesoft" in skills             # tags fill in behind


@pytest.mark.parametrize(("title", "level", "subject"), [
    ("Master of Science (MS) Information Systems", "Master's Degree", "Information Systems"),
    ("Bachelor of Science (BS) Computer Science, Magna Cum Laude", "Bachelor's Degree", "Computer Science"),
    ("MBA", "Master's Degree", ""),
    ("PhD Computer Engineering", "Doctorate", "Computer Engineering"),
])
def test_a_degree_is_split_into_level_and_subject(title, level, subject):
    """A form asks for the *level* from a dropdown and the subject separately; the full degree name matches no
    dropdown option at all."""
    assert autoapply._degree_level(title) == level
    assert autoapply._field_of_study(title) == subject


def test_education_entries_carry_what_a_form_asks_for(cfg):
    _write_facts(cfg)
    _, education = autoapply.experience_entries(cfg, 6)
    drexel = next(e for e in education if "Drexel" in e["school"])
    # the degree as a dropdown lists it, not the level
    assert drexel["degree"] == "Master of Science"
    assert drexel["field_of_study"] == "Information Systems"


# --------------------------------------------------------------------------------------- bot traps


@needs_browser
def test_a_honeypot_field_is_left_alone(profile):
    """First American's create form hides a field labelled "for robots only". Filling it makes the form refuse
    to submit for ever, silently — so the whole run died with "the form would not submit"."""
    from playwright.sync_api import sync_playwright

    profile.links.website = "https://github.com/example"      # the value that used to get typed into the trap
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((NEW_TENANT / "honeypot.html").as_uri())
        labels = [f.label for f in forms.scan(page)]
        traps = {t.label: t.name for t in forms.scan.last_traps}
        browser.close()

    assert any("for robots only" in t for t in traps), traps
    assert any("Personal website" in t for t in traps)         # unlabelled and off screen
    assert not any("robots" in label for label in labels)
    # the styled consent box is a real control, not a trap: its input is invisible but its label is not
    assert "I consent to the above" in labels
    assert not any("consent" in t.lower() for t in traps)


@needs_browser
def test_a_form_with_traps_still_submits(profile):
    """End to end: the trap stays empty, so the form goes through."""
    from playwright.sync_api import sync_playwright

    profile.links.website = "https://github.com/example"
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "a-long-password"),
                               verification_code=lambda: "654321")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((NEW_TENANT / "honeypot.html").as_uri())
        workday._fill_account_form(page, ctx, "Gen3rated-Str0ng!pw")
        assert page.locator("#hp1").input_value() == ""        # the trap was never touched
        assert page.locator("#offscreen").input_value() == ""
        assert page.locator("#consent").is_checked()           # the real consent box was
        workday.visible(page, '[data-automation-id="createAccountSubmitButton"]').click(force=True)
        page.wait_for_timeout(600)
        url = page.url
        browser.close()
    assert url.endswith("verify.html")


# --------------------------------------------------------------------------------------- reading the emailed code


@pytest.mark.parametrize(("body", "expected"), [
    ("Your verification code is 483920. It expires in 15 minutes.", "483920"),
    ("Use code: 12345 to verify your email", "12345"),
    ("Enter this one-time passcode 908172 on the site", "908172"),
    ("Hello,\n\n   224466\n\nis your code", "224466"),
    ("No numbers here at all", None),
])
def test_codes_are_pulled_out_of_the_email_body(body, expected):
    from jobhunt.apply import verify_email

    assert verify_email.extract_code(body) == expected


def test_only_a_fresh_verification_email_is_used(monkeypatch):
    """An old code, or a message from anyone else, must never be typed into a form."""
    from datetime import UTC, datetime, timedelta

    from jobhunt.apply import verify_email
    from jobhunt.sources.email_alerts import EmailAccount

    now = datetime.now(UTC)
    account = EmailAccount(host="imap.test", user="me@example.test", password="x")
    messages = [
        (now - timedelta(days=2), "noreply@myworkday.com", "Verify your email", "Your code is 111111"),
        (now, "recruiter@example.com", "Re: your application", "Call me on 222222"),
        (now, "noreply@acme.myworkday.com", "Verify your email address", "Your verification code is 333333"),
    ]
    monkeypatch.setattr(verify_email, "recent_messages", lambda *a, **k: iter(list(messages)))
    assert verify_email.find_code(account, now - timedelta(minutes=1), host_hint="acme.wd1.myworkdayjobs.com") \
        == "333333"


def test_a_generated_password_satisfies_the_usual_policies():
    from jobhunt.services.autoapply import generate_password

    pw = generate_password()
    assert len(pw) >= 16
    assert any(c.isupper() for c in pw) and any(c.islower() for c in pw)
    assert any(c.isdigit() for c in pw) and any(not c.isalnum() for c in pw)
    assert generate_password() != pw


def test_the_plan_says_whether_a_site_is_new(cfg, engine):
    from jobhunt.graphs.runtime import Runtime

    queries.upsert_credential(engine, host="*", username="me@example.test", password_enc="dpapi:x")
    queries.upsert_credential(engine, host="known.wd1.myworkdayjobs.com", username="me@example.test",
                              password_enc="dpapi:y")
    rt_ = Runtime(cfg=cfg, engine=engine, checkpointer=None, executor=None)  # type: ignore[arg-type]
    new = autoapply.account_plan(rt_, "brand-new.wd5.myworkdayjobs.com")
    known = autoapply.account_plan(rt_, "known.wd1.myworkdayjobs.com")
    assert new["will_create"] and not new["known_site"] and new["shared"]
    assert known["known_site"] and not known["will_create"]


def test_a_mailbox_that_will_not_log_in_says_what_to_do():
    """Gmail refuses an account password over IMAP; "the code never arrived" would send you looking in the
    wrong place entirely."""
    advice = autoapply.mailbox_advice(
        "imap.gmail.com: [ALERT] Application-specific password required: https://support.google.com/... (Failure)")
    assert "16-character app password" in advice
    assert "Job alert emails" in advice
    assert autoapply.mailbox_advice("connection reset") == "connection reset"


@needs_browser
def test_a_broken_mailbox_is_not_reported_as_a_missing_email(profile, tmp_path):
    from playwright.sync_api import sync_playwright

    def explode() -> str:
        raise RuntimeError("imap.gmail.com: Application-specific password required")

    page_html = tmp_path / "verify.html"
    page_html.write_text('<h2>Verify Your Email</h2><p>We emailed you a verification code.</p>'
                         '<label for="c">Verification Code</label>'
                         '<input id="c" data-automation-id="verificationCode">', encoding="utf-8")
    ctx = workday.ApplyContext(profile=profile, credential=workday.Credential("me@example.test", "pw"),
                              verification_code=explode)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(page_html.as_uri())
        with pytest.raises(workday.ApplyStopped) as exc:
            workday.handle_verification(page, ctx, "create account")
        browser.close()
    assert exc.value.reason == "verification"
    assert "could not read the verification code from your mailbox" in str(exc.value)
    assert "Application-specific password" in str(exc.value)


@needs_browser
def test_the_window_is_held_open_until_it_is_closed():
    """A run that did not finish leaves its window open: the page as it stands is the only way to see what
    stopped it. The wait ends the moment the window is closed, rather than running down the clock."""
    import time as clock

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<h2>My Experience</h2>")

        page.close()                       # stands in for you closing the window
        started = clock.monotonic()
        autoapply._wait_until_closed(page, minutes=5)
        waited = clock.monotonic() - started
        browser.close()

    assert waited < 5, "a closed window should end the wait at once, not after the full timeout"


def test_the_outcome_is_recorded_before_the_window_is_parked(cfg, engine, monkeypatch):
    """The run must be written and the lock let go *before* the wait, or a held-open window would block the
    next run and leave the page showing "running" for half an hour."""
    import inspect

    source = inspect.getsource(autoapply._work)
    body = source[source.index("finally:"):]
    assert body.index("finish()") < body.index("_wait_until_closed"),         "finish() must come before the window is parked"
    assert "locks.release" in inspect.getsource(autoapply._work)


# --------------------------------------------------------------------------------------- web pages


from tests.test_web_control import ui_cfg  # noqa: E402, F401  (the shipped config.yaml, paths moved to tmp_path)


@pytest.fixture
def client(ui_cfg, hashing_embedder):  # noqa: ANN001, ANN201, F811
    from fastapi.testclient import TestClient

    from tests.test_web_control import _app

    with TestClient(_app(ui_cfg, hashing_embedder)) as c:
        yield c


def test_the_auto_apply_page_shows_its_state(client):
    page = client.get("/apply").text
    assert "Auto-apply" in page and "beta" in page
    assert "No sign-ins saved yet." in page
    assert "stops before submitting" in page          # the default must be legible at a glance


def test_a_password_stored_plainly_can_be_read_back(client):
    """The owner's choice: kept as typed and shown on the page, so a password jobhunt generated for a site can
    be read and used by hand. An encrypted one that gets overwritten is gone for good."""
    from jobhunt.graphs.runtime import get_runtime

    get_runtime().cfg.auto_apply.store_passwords = "plain"
    client.post("/apply/credentials", data={"host": "*", "username": "me@example.test",
                                            "password": "a-long-password"}, follow_redirects=True)
    row = next(c for c in queries.list_credentials_with_secret(get_runtime().engine))
    assert row["password_enc"] == "plain:a-long-password"
    assert secrets_store.reveal(row["password_enc"]) == "a-long-password"
    assert "a-long-password" in client.get("/apply").text          # shown, which is the point


def test_a_password_stored_encrypted_never_appears_anywhere(client):
    from jobhunt.graphs.runtime import get_runtime

    get_runtime().cfg.auto_apply.store_passwords = "encrypted"
    client.post("/apply/credentials", data={"host": "*", "username": "me@example.test",
                                            "password": "a-long-password"}, follow_redirects=True)
    row = next(c for c in queries.list_credentials_with_secret(get_runtime().engine))
    assert "a-long-password" not in (row["password_enc"] or "")
    assert row["password_enc"].startswith("dpapi:") if sys.platform == "win32" else True
    assert "a-long-password" not in client.get("/apply").text
    # ... and it is still usable: both storage forms read back the same way
    assert secrets_store.password_for(row["password_enc"]) == "a-long-password"


def test_the_profile_form_round_trips(client, ui_cfg):  # noqa: F811
    client.post("/apply/profile", data={
        "identity.first_name": "Jane", "identity.city": "Parsippany", "eligibility.desired_salary": "150000",
        "eeo.gender": "I do not wish to answer", "q_match": ["how did you hear"], "q_answer": ["Company website"],
    }, follow_redirects=True)
    saved = load_profile(ui_cfg.paths.apply_profile)
    assert saved.identity.first_name == "Jane"
    assert saved.eligibility.desired_salary == "150000"
    assert [(q.match, q.answer) for q in saved.questions] == [("how did you hear", "Company website")]
    assert "150000" in client.get("/apply").text


def test_answering_a_question_remembers_it(client, ui_cfg):  # noqa: F811
    from jobhunt.graphs.runtime import get_runtime

    qid = queries.add_apply_question(get_runtime().engine, run_id=None, job_id=None, host="x.test", stage="Questions",
                                     label="Which of our company values resonates with you most?",
                                     norm_label="which of our company values resonates with you most", kind="text",
                                     options=[])
    client.post(f"/apply/questions/{qid}", data={"answer": "Integrity", "save_to_profile": "on"},
                follow_redirects=True)
    assert queries.stored_question_answers(get_runtime().engine)
    assert any(q.answer == "Integrity" for q in load_profile(ui_cfg.paths.apply_profile).questions)


def test_a_non_workday_job_cannot_be_auto_applied(client):
    """Only Workday is wired up so far, and the page says so rather than opening a browser at a site it
    cannot drive."""
    from jobhunt.graphs.runtime import get_runtime
    from jobhunt.sources.normalize import normalize_raw
    from tests.conftest import raw_job

    rec = normalize_raw(raw_job(1))
    rec["url"] = rec["apply_url"] = "https://boards.greenhouse.io/acme/jobs/1"
    _, job_id = queries.upsert_job(get_runtime().engine, rec)
    page = client.get(f"/jobs/{job_id}/auto-apply", follow_redirects=True).text
    assert "not on Workday" in page
    assert "Start auto-apply" in page                      # shown, but disabled by the blocker list


def test_a_workday_job_offers_the_button_and_lists_what_it_will_do(client):
    from jobhunt.graphs.runtime import get_runtime
    from jobhunt.sources.normalize import normalize_raw
    from tests.conftest import raw_job

    rt_ = get_runtime()
    rec = normalize_raw(raw_job(2))
    rec["url"] = rec["apply_url"] = "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/Parsippany/Lead_JR1"
    _, job_id = queries.upsert_job(rt_.engine, rec)
    assert "auto-apply" in client.get(f"/jobs/{job_id}").text
    page = client.get(f"/jobs/{job_id}/auto-apply").text
    assert "acme.wd1.myworkdayjobs.com" in page
    assert "no sign-in saved" in page and "no rendered resume" in page
    assert "auto-apply is switched off" in page           # the beta ships off
