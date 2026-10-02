"""Job-alert email ingestion: parsing LinkedIn/Indeed style alerts, the read-only IMAP reader, and the settings page.

The alert bodies below are SYNTHETIC, written to match the shape of those emails (anchor with the job title, then
"Company · Location"). No real mailbox or network is touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

from fastapi.testclient import TestClient

from jobhunt.db import queries
from jobhunt.graphs import runtime
from jobhunt.services import settings as settings_service
from jobhunt.sources import email_alerts
from jobhunt.sources.registry import build_sources, enabled_sources
from jobhunt.web.app import create_app
from tests.conftest import FakeLLM, FakeSource, raw_job
from tests.test_web_control import _wait, ui_cfg  # noqa: F401  (fixture re-export)

LINKEDIN = """<html><body>
<p>Your job alert for integration lead</p>
<table><tr><td>
  <a href="https://www.linkedin.com/comm/jobs/view/4123456789/?trackingId=abc">Integration Delivery Lead</a>
  <p>Prudential Financial &#183; Newark, NJ (Hybrid)</p>
  <span>2 days ago</span> <a href="https://www.linkedin.com/comm/jobs/view/4123456789/apply">Easy Apply</a>
</td></tr><tr><td>
  <a href="https://www.linkedin.com/comm/jobs/view/4987654321/?trackingId=def">Technical Lead, API Platform</a>
  <p>Acme Insurance &#183; Remote (United States)</p>
</td></tr></table>
<a href="https://www.linkedin.com/comm/jobs/search/?keywords=integration">See all jobs</a>
<a href="https://www.linkedin.com/psettings/email">Unsubscribe</a>
</body></html>"""

INDEED = """<html><body>
<a href="https://www.indeed.com/rc/clk?jk=9f8e7d6c5b4a3210&amp;from=ja">Delivery Lead - Integration</a>
<div>Garden State Mutual - Morristown, NJ</div>
<div>$170,000 - $190,000 a year</div>
<a href="https://www.indeed.com/rc/clk?jk=1122334455667788">Senior MuleSoft Engineer</a>
<div>Acme Health, Remote</div>
<a href="https://www.indeed.com/alert/unsubscribe?id=x">Unsubscribe</a>
</body></html>"""


def _message(sender: str, subject: str, html_body: str, days_ago: float = 1) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Date"] = (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%a, %d %b %Y %H:%M:%S %z")
    msg.set_content("plain text version")
    msg.add_alternative(html_body, subtype="html")
    return msg.as_bytes()


class FakeIMAP:
    """Stands in for imaplib.IMAP4_SSL; records everything the source does to the mailbox."""

    def __init__(self, messages: list[bytes], folders: tuple[str, ...] = ("INBOX", "JobAlerts")) -> None:
        self.messages = messages
        self.folders = folders
        self.selected: tuple[str, bool] | None = None
        self.calls: list[str] = []
        self.logged_out = False

    def select(self, folder, readonly=False):  # noqa: ANN001, ANN201, FBT002
        self.calls.append(f"select {folder} readonly={readonly}")
        name = folder.strip('"')
        if name not in self.folders:
            return "NO", [b"no such mailbox"]
        self.selected = (name, readonly)
        return "OK", [str(len(self.messages)).encode()]

    def search(self, charset, *criteria):  # noqa: ANN001, ANN201
        self.calls.append("search " + " ".join(criteria))
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))]

    def fetch(self, uid, spec):  # noqa: ANN001, ANN201
        self.calls.append(f"fetch {uid.decode() if isinstance(uid, bytes) else uid} {spec}")
        return "OK", [(b"1 (BODY[] {1}", self.messages[int(uid) - 1]), b")"]

    def logout(self):  # noqa: ANN201
        self.logged_out = True
        return "BYE", [b""]


def test_alert_parsing_picks_out_postings_company_and_location():
    jobs = email_alerts.parse_alert(LINKEDIN, "Integration Delivery Lead: Prudential Financial is hiring")
    assert [j["title"] for j in jobs] == ["Integration Delivery Lead", "Technical Lead, API Platform"]
    assert jobs[0]["company"] == "Prudential Financial" and jobs[0]["location"] == "Newark, NJ (Hybrid)"
    assert jobs[1]["company"] == "Acme Insurance" and jobs[1]["location"] == "Remote (United States)"
    assert email_alerts.job_id_for("linkedin", jobs[0]["url"]) == "linkedin/4123456789"

    indeed = email_alerts.parse_alert(INDEED, "new jobs for delivery lead")
    assert [j["title"] for j in indeed] == ["Delivery Lead - Integration", "Senior MuleSoft Engineer"]
    assert indeed[0]["company"] == "Garden State Mutual" and indeed[0]["location"] == "Morristown, NJ"
    assert email_alerts.job_id_for("indeed", indeed[0]["url"]) == "indeed/9f8e7d6c5b4a3210"
    assert email_alerts.provider_for("https://www.example.com/jobs/1") is None  # only known alert providers


def _account(**kw) -> email_alerts.EmailAccount:  # noqa: ANN003
    return email_alerts.EmailAccount(host="imap.test", user="me@test", password="app-password", **kw)


def test_reader_is_read_only_filters_senders_and_recognises_repeats():
    inbox = FakeIMAP([
        _message("LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>", "Your job alert", LINKEDIN),
        _message("Indeed <alert@indeed.com>", "new jobs for delivery lead", INDEED),
        _message("Aunt Mabel <mabel@example.com>", "Holiday photos", "<p>Hello!</p>"),
    ])
    src = email_alerts.EmailAlertSource(_account(), connect=lambda: inbox)
    jobs = list(src.fetch(datetime.now(UTC) - timedelta(days=7)))
    assert src.errors == [] and len(jobs) == 4 and src.stats["matched"] == 2 and src.stats["messages"] == 3
    assert all(j.extra["listing_only"] and j.description_html == "" for j in jobs)
    assert {j.source for j in jobs} == {"email"} and jobs[0].posted_at is not None
    assert inbox.selected == ("INBOX", True) and inbox.logged_out
    assert all("BODY.PEEK" in c for c in inbox.calls if c.startswith("fetch"))  # never marks mail as read
    assert not any(c.startswith(("store", "copy", "delete")) for c in inbox.calls)

    seen = email_alerts.EmailAlertSource(_account(), connect=lambda: inbox,
                                         known=lambda source, sid: sid == "linkedin/4123456789")
    again = list(seen.fetch(datetime.now(UTC) - timedelta(days=7)))
    assert [j.extra.get("sighting_only") for j in again].count(True) == 1 and seen.stats["sightings"] == 1

    missing = email_alerts.EmailAlertSource(_account(folder="Nope"), connect=lambda: inbox)
    assert list(missing.fetch(None)) == [] and "not found" in missing.errors[0]["message"]


def test_alert_jobs_are_stored_but_never_scored_on_a_title_alone(ui_cfg, hashing_embedder):  # noqa: F811
    inbox = FakeIMAP([_message("jobalerts-noreply@linkedin.com", "Your job alert", LINKEDIN)])
    llm = FakeLLM()
    ui_cfg.email.enabled = True
    ui_cfg.secrets.IMAP_HOST, ui_cfg.secrets.IMAP_USER, ui_cfg.secrets.IMAP_PASSWORD = "imap.test", "me@test", "pw"
    source = email_alerts.EmailAlertSource(email_alerts.EmailAccount("imap.test", "me@test", "pw"),
                                           connect=lambda: inbox)
    app = create_app(ui_cfg, start_scheduler=False, runtime_factories={
        "llm_factory": lambda: llm, "embedder_factory": lambda: hashing_embedder,
        "sources_factory": lambda names: [source, FakeSource("workday", [raw_job(1, title="Integration Lead",
                                                                                 company="Prudential")])]})
    with TestClient(app) as c:
        rt = runtime.get_runtime()
        r = c.post("/runs", data={"mode": "full"}, follow_redirects=False)
        run_id = int(r.headers["location"].rsplit("/", 1)[1])
        _wait(lambda: queries.get_run(rt.engine, run_id)["status"] != "running")
        run = queries.get_run(rt.engine, run_id)
        assert run["counts"]["need_posting_text"] == 2 and run["counts"]["scored"] == 1  # only the full posting
        assert llm.calls == 1
        rows, _ = queries.list_queue(rt.engine, queries.QueueFilters(status="discovered"), paginate=False)
        alerts = {j["title"]: j for j in rows if j["source"] == "email"}
        assert set(alerts) == {"Integration Delivery Lead", "Technical Lead, API Platform"}
        alert = alerts["Integration Delivery Lead"]
        assert not alert["description_text"] and alert["company_name_raw"] == "Prudential Financial"
        assert alert["url"].startswith("https://www.linkedin.com/comm/jobs/view/4123456789")
        page = c.get("/queue?status=discovered&sort=seen").text
        assert "Paste the posting" in page and f'/jobs/new?replace={alert["id"]}' in page
        assert "Need the posting pasted" in c.get(f"/runs/{run_id}").text


def test_settings_page_saves_the_mailbox_and_tests_it(ui_cfg, hashing_embedder):  # noqa: F811
    inbox = FakeIMAP([_message("alert@indeed.com", "new jobs for delivery lead", INDEED)], folders=("JobAlerts",))
    with TestClient(create_app(ui_cfg, start_scheduler=False,
                               runtime_factories={"embedder_factory": lambda: hashing_embedder})) as c:
        rt = runtime.get_runtime()
        page = c.get("/settings").text
        assert 'id="email"' in page and "not set up" in page and "jobalerts-noreply@linkedin.com" in page
        bad = c.post("/settings/email", data={"enabled": "on", "user": "", "port": "993", "max_age_days": "14",
                                              "max_messages": "200"}, follow_redirects=False)
        assert "app+password" in bad.headers["location"]
        r = c.post("/settings/email", data={"enabled": "on", "host": "imap.test", "user": "me@test",
                                            "password": "app-password", "port": "993", "folder": "JobAlerts",
                                            "senders": "alert@indeed.com\njobalerts-noreply@linkedin.com",
                                            "subject_patterns": "job alert", "max_age_days": "7",
                                            "max_messages": "50"}, follow_redirects=False)
        assert "Job+alert+emails+saved+%28on%29" in r.headers["location"], r.headers["location"]
        env = (ui_cfg.config_path.parent / ".env").read_text(encoding="utf-8")
        assert "IMAP_USER=me@test" in env and "IMAP_PASSWORD=app-password" in env and "IMAP_HOST=imap.test" in env
        assert rt.cfg.email.enabled and rt.cfg.email.folder == "JobAlerts" and rt.cfg.email.max_age_days == 7
        assert "email" in enabled_sources(rt.cfg)
        assert [s.name for s in build_sources(rt.cfg, ["email"])] == ["email"]

        result = settings_service.test_email(rt, connect=lambda: inbox)
        assert result["ok"] and "found 2 posting(s)" in result["message"]
        assert "Delivery Lead - Integration · Garden State Mutual · Morristown, NJ" in result["samples"]
        assert inbox.selected == ("JobAlerts", True)

        rt.cfg.secrets.IMAP_PASSWORD = None
        assert settings_service.test_email(rt)["ok"] is False


INDEED_DIGEST = """<html><body>
<h1>29 new technical lead jobs in Newark, NJ</h1>
<p>These job ads match your saved job alert</p>
<a href="https://www.indeed.com/rc/clk?jk=abc123def456&amp;from=ja">Lead Software Development Engineer, Consumer Domains</a>
<div>Amazon.com <span>4.1</span></div>
<div>Newark, NJ</div>
<div>$151,300 - $261,500 a year</div>
<div>This role is based in Newark, NJ. You are an innovative and passionate top software engineer looking to make a positive impact on customer experience.</div>
<div>Just posted</div>
<a href="https://www.indeed.com/pagead/clk?mo=r&amp;ad=xyz">Delivery Lead, Integration</a>
<div>Garden State Mutual</div>
<div>Newark, NJ</div>
<div>Hybrid</div>
<div>Lead a team delivering MuleSoft integrations across the policy platform for a regional insurer.</div>
<a href="https://www.indeed.com/rc/clk?jk=hourly1&amp;from=ja">Contract API Developer</a>
<div>Staffing Co</div>
<div>Remote</div>
<div>$85 - $95 an hour</div>
</body></html>"""


def test_indeed_digest_splits_into_jobs_with_company_location_and_pay():
    jobs = email_alerts.parse_alert(INDEED_DIGEST, "29 new technical lead jobs in Newark, NJ")
    assert len(jobs) == 3  # one digest email, three separate jobs
    amazon, mutual, contract = jobs
    assert amazon["company"] == "Amazon.com" and amazon["location"] == "Newark, NJ"  # rating stripped
    assert (amazon["comp_min"], amazon["comp_max"], amazon["comp_interval"]) == (151300, 261500, None)
    assert amazon["snippet"].startswith("This role is based in Newark")
    assert mutual["company"] == "Garden State Mutual" and mutual["location"] == "Newark, NJ"
    assert (contract["comp_min"], contract["comp_interval"]) == (85, "hourly")
    # a link without a job key still gets a stable id, so tomorrow's digest recognises it
    assert email_alerts.job_id_for("indeed", mutual["url"], title=mutual["title"], company=mutual["company"]) == \
        email_alerts.job_id_for("indeed", mutual["url"] + "&tk=other", title=mutual["title"], company=mutual["company"])


def test_digest_jobs_keep_their_pay_and_wait_for_the_full_posting(ui_cfg, hashing_embedder):  # noqa: F811
    from jobhunt.discovery.service import has_posting_text

    inbox = FakeIMAP([_message("alert@indeed.com", "29 new technical lead jobs", INDEED_DIGEST)])
    source = email_alerts.EmailAlertSource(email_alerts.EmailAccount("imap.test", "me@test", "pw"),
                                           connect=lambda: inbox)
    llm = FakeLLM()
    app = create_app(ui_cfg, start_scheduler=False, runtime_factories={
        "llm_factory": lambda: llm, "embedder_factory": lambda: hashing_embedder,
        "sources_factory": lambda names: [source]})
    with TestClient(app) as c:
        rt = runtime.get_runtime()
        r = c.post("/runs", data={"mode": "full"}, follow_redirects=False)
        run_id = int(r.headers["location"].rsplit("/", 1)[1])
        _wait(lambda: queries.get_run(rt.engine, run_id)["status"] != "running")
        rows, _ = queries.list_queue(rt.engine, queries.QueueFilters(status="any"), paginate=False)
        amazon = next(j for j in rows if j["company_name_raw"] == "Amazon.com")
        assert amazon["comp_min"] == 151300 and amazon["comp_max"] == 261500
        assert amazon["location_raw"] == "Newark, NJ" and amazon["description_text"].startswith("This role is based")
        assert llm.calls == 0 and not has_posting_text(amazon, rt.cfg)  # a teaser is not enough to score
        assert queries.get_run(rt.engine, run_id)["counts"].get("need_posting_text")
