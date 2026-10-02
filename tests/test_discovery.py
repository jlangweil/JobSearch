from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from jobhunt.db import locks, queries, vectors
from jobhunt.db.engine import utcnow_iso
from jobhunt.discovery import dedupe, ghost, prefilter, scoring, service
from jobhunt.graphs.discovery import graph as dg
from jobhunt.graphs.runtime import init_runtime
from jobhunt.llm.schemas import Evidence, JobScore
from jobhunt.sources.normalize import normalize_raw
from tests.conftest import FakeLLM, FakeSource, raw_job

# --------------------------------------------------------------------------------------------- prefilter


def rec(**kw) -> dict:  # noqa: ANN003
    return normalize_raw(raw_job(1, **kw))


@pytest.mark.parametrize(
    ("kwargs", "expected_prefix"),
    [
        ({}, None),
        ({"title": "Junior Integration Developer"}, "title"),
        ({"title": "Account Executive"}, "title"),  # blocklist
        # age alone no longer knocks a job out (filters.stale_action: keep) — see the staleness tests below
        ({"posted_days_ago": 45}, None),
        ({"title": "Contract Integration Architect"}, "employment"),
        ({"comp": (100000, 140000)}, "comp"),
        ({"location": "Austin, TX", "description": "<p>Hybrid role in Austin.</p>"}, "logistics"),
        ({"location": "Remote - India"}, "location"),
        ({"description": "<p>Must hold an active TS/SCI clearance. Remote.</p>", "location": "Remote"}, "dealbreaker"),
        ({"location": "Remote (US)"}, None),
    ],
)
def test_prefilter_rules(cfg, kwargs, expected_prefix):
    reason = prefilter.knockout_reason(rec(**kwargs), cfg)
    if expected_prefix is None:
        assert reason is None
    else:
        assert reason is not None and reason.startswith(expected_prefix), reason


# --------------------------------------------------------------------------------------------- dedupe


def _window_row(i, source, company, title, h, emb, status="discovered", posted="2026-09-01T00:00:00Z"):  # noqa: ANN001
    return {"id": i, "source": source, "company_name_raw": company, "title": title, "content_hash": h,
            "embedding": vectors.to_blob(emb), "posted_at": posted, "status": status, "dedupe_group": None}


def test_dedupe_exact_hash_and_semantic_match_prefers_ats_source():
    base = [1.0] + [0.0] * 383
    near = [0.98, 0.1] + [0.0] * 382
    far = [0.0, 1.0] + [0.0] * 382
    window = [
        _window_row(1, "adzuna", "Acme", "Integration Architect", "h1", base, posted="2026-08-01T00:00:00Z"),
        _window_row(2, "workday", "Acme", "Integration Architect (Hybrid)", "h2", near),
        _window_row(3, "greenhouse", "Acme", "Integration Architect", "h1", base),
        _window_row(4, "workday", "Other Co", "Integration Architect", "h4", base),
        _window_row(5, "workday", "Acme", "Payroll Specialist", "h5", far),
    ]
    plan = {jid: (group, dup) for jid, group, dup in dedupe.plan([1, 2, 3, 4, 5], window)}
    canonical = plan[2][0]
    assert canonical in (2, 3)  # an ATS source wins over the aggregator
    assert plan[1] == (canonical, True)
    assert {plan[2][1], plan[3][1]} == {True, False}
    assert 4 not in plan or plan[4] == (4, False)  # different company never merges
    assert 5 not in plan or plan[5][1] is False


# --------------------------------------------------------------------------------------------- ghost


def test_ghost_score_signals():
    old = {"id": 1, "title": "Architect", "company_name_raw": "Acme", "posted_at": "2026-06-01T00:00:00Z",
           "first_seen_at": "2026-06-02T00:00:00Z", "seen_count": 60, "description_text": "work",
           "comp_min": None, "comp_max": None, "dedupe_group": 1}
    similar = [{"id": 2, "title": "Architect", "company_name_raw": "Acme", "dedupe_group": 2}]
    score, reasons = ghost.ghost_score(old, similar_titles=similar, staffing_slugs=["acme"],
                                       now="2026-09-14T00:00:00Z")
    assert score >= 80 and len(reasons) >= 5
    fresh = {**old, "posted_at": "2026-09-12T00:00:00Z", "first_seen_at": "2026-09-12T00:00:00Z", "seen_count": 1,
             "comp_min": 180000, "description_text": "Join the platform team, reporting to the hiring manager."}
    assert ghost.ghost_score(fresh, now="2026-09-14T00:00:00Z")[0] == 0


# --------------------------------------------------------------------------------------------- scoring enforcement


def test_scoring_drops_fabricated_quotes_caps_unsupported_and_computes_total(cfg):
    job = {"id": 7, **rec()}
    parsed = JobScore(
        skill_match=90, role_fit=88, domain_fit=85, seniority_fit=95, comp_fit=40, logistics_fit=72, red_flags=[],
        gaps=[],
        evidence=[
            Evidence(claim="api", quote="own   the API\nplatform", dimension="skill_match"),  # whitespace-normalized
            Evidence(claim="fake", quote="leads a team of 400 engineers", dimension="seniority_fit"),
            Evidence(claim="fake2", quote="unlimited PTO", dimension="domain_fit"),
        ],
        rationale="ok",
    )
    row = scoring.enforce(parsed, job, cfg, ghost=20, truncated=False)
    assert row["skill_match"] == 90
    assert row["domain_fit"] == 69 and row["seniority_fit"] == 69 and row["logistics_fit"] == 69
    assert row["role_fit"] == 69  # no surviving role_fit evidence
    assert row["dropped_evidence"] == 2 and row["low_confidence"] == 1
    subs = {k: row[k] for k in queries.WEIGHT_KEYS}
    assert row["total"] == scoring.compute_total(subs, cfg.scoring.weights, 20, cfg.scoring.ghost_penalty)
    assert row["total"] < scoring.compute_total(subs, cfg.scoring.weights, 0, cfg.scoring.ghost_penalty)


POSTING_V5 = ("<p>Engineering Manager, Integration Platform. You will manage a team of 8 engineers, own delivery of "
              "the roadmap and coach engineers through code reviews. Required: MuleSoft, AWS, Kubernetes and CI/CD. "
              "Preferred: C#, Terraform.</p>")
BACKGROUND_V5 = ("Solution Lead - Enterprise Integration. Migrated IBM WMB services to MuleSoft CloudHub. "
                 "Deployed APIs to AWS EKS. Jenkins CI/CD pipelines. Earlier: .NET and C# web applications.")


def _v5_score(stack, role, **subs):  # noqa: ANN001, ANN003, ANN202
    base = {"skill_match": 50, "role_fit": 50, "domain_fit": 60, "seniority_fit": 60, "comp_fit": 50,
            "logistics_fit": 60}
    return JobScore(**{**base, **subs}, stack=stack, role=role, red_flags=[], gaps=[], evidence=[], rationale="ok")


def test_skill_match_is_the_checked_share_of_the_posting_stack(cfg):
    """Rubric v5: the model lists the posting's technologies and says which the candidate has; code keeps only what
    the posting names, corrects claims against the background, and computes skill_match from the list."""
    from jobhunt.llm.schemas import StackItem

    job = {"id": 9, **rec(description=POSTING_V5)}
    stack = [
        StackItem(technology="MuleSoft", importance="required", level_required="working", candidate_has="recent", candidate_quote="MuleSoft CloudHub"),
        StackItem(technology="AWS", importance="required", level_required="working", candidate_has="recent", candidate_quote="AWS EKS"),
        # claimed without support: the background never mentions it, so it counts as missing
        StackItem(technology="Kubernetes", importance="required", level_required="working", candidate_has="recent", candidate_quote="k8s expert"),
        StackItem(technology="CI/CD", importance="required", level_required="working", candidate_has="no"),  # the background names it
        StackItem(technology="C#", importance="preferred", level_required="working", candidate_has="earlier", candidate_quote=".NET and C#"),
        StackItem(technology="Terraform", importance="preferred", level_required="working", candidate_has="no"),
        StackItem(technology="Snowflake", importance="required", level_required="working", candidate_has="no"),  # not in the posting: dropped
    ]
    row = scoring.enforce(_v5_score(stack, None, skill_match=95), job, cfg, ghost=0, truncated=False,
                          background=BACKGROUND_V5)
    checked = {t["technology"]: t["has"] for t in row["analysis"]["stack"]}
    assert checked == {"MuleSoft": "recent", "AWS": "recent", "Kubernetes": "no", "CI/CD": "earlier", "C#": "earlier",
                       "Terraform": "no"}
    assert row["analysis"]["stack_dropped"] == 1 and row["analysis"]["skill_from_stack"]
    # required x2, preferred x1; recent 1.0, earlier 0.8: (2 + 2 + 0 + 1.6 + 0.8 + 0) / 10
    assert row["skill_match"] == 64


def test_a_true_but_unrelated_quote_does_not_prove_a_technology(cfg):
    """Seen live: qwen claimed CAN and Wireshark by quoting real, unrelated background sentences."""
    from jobhunt.llm.schemas import StackItem

    posting = ("<p>Automotive test lead. Required: CAN, Wireshark, Python. You can expect to test vehicle "
               "networks.</p>")
    background = "Built a .NET web application and XML/XSL hub for routing transactions. Scripts in Python."
    job = {"id": 9, **rec(description=posting)}
    stack = [StackItem(technology="CAN", importance="required", level_required="working", candidate_has="earlier",
                       candidate_quote="XML/XSL hub for routing transactions"),
             StackItem(technology="Wireshark", importance="required", level_required="working", candidate_has="earlier",
                       candidate_quote="Built a .NET web application"),
             StackItem(technology="Python", importance="required", level_required="working", candidate_has="recent",
                       candidate_quote="Scripts in Python")]
    row = scoring.enforce(_v5_score(stack, None), job, cfg, ghost=0, truncated=False, background=background)
    assert {t["technology"]: t["has"] for t in row["analysis"]["stack"]} == {"CAN": "no", "Wireshark": "no",
                                                                              "Python": "recent"}
    # and the English word "can" in a background is not the CAN bus
    assert not scoring._background_names("I can lead teams", "CAN")
    assert scoring._background_names("Tuned CAN bus traffic", "CAN")


def test_a_concept_whose_words_the_background_uses_gets_partial_credit(cfg):
    assert scoring._background_covers("Designed cloud migration and AWS infrastructure", "Cloud Infrastructure")
    assert not scoring._background_covers("Designed cloud migration", "Cloud Infrastructure")
    assert not scoring._background_covers("Kafka and Kubernetes", "Kubernetes")  # one word: needs the name itself
    assert not scoring._background_covers("security reviews", "Zero-trust security policies")


def test_too_short_a_stack_leaves_the_model_estimate(cfg):
    from jobhunt.llm.schemas import StackItem

    job = {"id": 9, **rec(description=POSTING_V5)}
    stack = [StackItem(technology="AWS", importance="required", level_required="working", candidate_has="recent", candidate_quote="AWS EKS")]
    row = scoring.enforce(_v5_score(stack, None, skill_match=62), job, cfg, ghost=0, truncated=False,
                          background=BACKGROUND_V5)
    assert row["skill_match"] == 62 and not row["analysis"]["skill_from_stack"]


@pytest.mark.parametrize(("role_type", "hands_on", "expected"), [
    ("technical_lead", "medium", 95), ("delivery_lead", "low", 95), ("technical_lead", "high", 85),
    ("people_manager", "low", 88), ("architect", "medium", 75), ("individual_contributor", "high", 45),
])
def test_role_fit_comes_from_the_kind_of_job(cfg, role_type, hands_on, expected):
    from jobhunt.llm.schemas import RoleAnalysis

    job = {"id": 9, **rec(description=POSTING_V5)}
    role = RoleAnalysis(role_type=role_type, hands_on=hands_on, quote="manage a team of 8 engineers")
    row = scoring.enforce(_v5_score([], role, role_fit=40), job, cfg, ghost=0, truncated=False,
                          background=BACKGROUND_V5)
    assert row["role_fit"] == expected == row["analysis"]["role"]["role_fit"]
    assert row["analysis"]["role"]["verified"]


def test_an_unquoted_role_classification_is_capped(cfg):
    from jobhunt.llm.schemas import RoleAnalysis

    job = {"id": 9, **rec(description=POSTING_V5)}
    role = RoleAnalysis(role_type="people_manager", hands_on="low", quote="direct reports across three countries")
    row = scoring.enforce(_v5_score([], role), job, cfg, ghost=0, truncated=False, background=BACKGROUND_V5)
    assert row["role_fit"] == scoring.EVIDENCE_CAP and not row["analysis"]["role"]["verified"]


def test_the_analysis_is_stored_with_the_score(cfg, engine):
    from jobhunt.llm.schemas import RoleAnalysis, StackItem

    _, job_id = queries.upsert_job(engine, rec(description=POSTING_V5))
    job = {"id": job_id, **rec(description=POSTING_V5)}
    stack = [StackItem(technology=t, importance="required", level_required="working", candidate_has="recent", candidate_quote=q)
             for t, q in (("MuleSoft", "MuleSoft CloudHub"), ("AWS", "AWS EKS"), ("CI/CD", "Jenkins CI/CD"))]
    role = RoleAnalysis(role_type="technical_lead", hands_on="low", quote="own delivery of the roadmap")
    row = scoring.enforce(_v5_score(stack, role), job, cfg, ghost=0, truncated=False, background=BACKGROUND_V5)
    queries.record_score(engine, job_id, row, set_scored=False)
    stored = queries.latest_score(engine, job_id)
    # three items, all had: 100 from the list, blended 60/40 with the model's own estimate of 50
    assert stored["skill_match"] == 80 and stored["role_fit"] == scoring.ROLE_FIT["technical_lead"]["low"]
    assert stored["analysis"]["model_skill_match"] == 50
    assert stored["analysis"]["role"]["role_type"] == "technical_lead"
    assert [t["technology"] for t in stored["analysis"]["stack"]] == ["MuleSoft", "AWS", "CI/CD"]


# --------------------------------------------------------------------------------------------- graph


def _mixed_jobs(n_good: int = 3) -> list:
    # Distinct companies so dedupe (same company + similar title) does not merge the synthetic postings.
    jobs = [raw_job(i, title=f"Principal Integration Architect {i}", company=f"Insurer {i}") for i in range(n_good)]
    # the clear majority should be knocked out deterministically
    jobs += [raw_job(100 + i, title=f"Registered Nurse {i}", company=f"Hospital {i}") for i in range(4)]
    jobs += [raw_job(200 + i, title=f"Integration Lead {i}", company=f"Texas Co {i}", location="Dallas, TX",
                     description="<p>Onsite in Dallas. Reports to the hiring manager.</p>") for i in range(3)]
    # these three are only *old*, which is no longer a reason to drop anything: they survive and get scored
    jobs += [raw_job(300 + i, title=f"Platform Architect {i}", company=f"Old Co {i}", posted_days_ago=90)
             for i in range(3)]
    return jobs


def test_discovery_run_filters_majority_scores_rest(cfg, engine, hashing_embedder):
    src = FakeSource("workday", _mixed_jobs())
    llm = FakeLLM()
    rt = init_runtime(cfg, llm_factory=lambda: llm, embedder_factory=lambda: hashing_embedder,
                      sources_factory=lambda names: [src, FakeSource("lever", [], fail=RuntimeError("board down"))])
    run_id, initial = dg.start_run(rt, trigger="cli", sources=None, max_to_score=None)
    result = dg.execute(rt, run_id, initial)
    counts = result["counts"]
    assert counts["fetched"] == 13 and counts["new"] == 13
    assert counts["filtered"] / counts["fetched"] > 0.5      # the nurses and the Dallas roles
    assert counts["scored"] == 6                             # three good ones, and three that are merely old
    run = queries.get_run(engine, run_id)
    assert run["status"] == "complete" and run["current_node"] == "done"
    assert any("board down" in e["message"] for e in run["errors"])  # failing source recorded, run continued
    statuses = queries.status_counts(engine)
    assert statuses == {"filtered_out": 7, "scored": 6}
    score = queries.latest_score(engine, queries.list_queue(engine, queries.QueueFilters())[0][0]["id"])
    assert score["evidence"] and all(e["quote"] != "this text is not in the posting" for e in score["evidence"])
    assert queries.lock_get(engine, locks.DISCOVERY_LOCK) is None
    assert [t["node"] for t in dg.timeline(rt, run_id)] == list(dg.NODES)


def test_killed_run_resumes_from_checkpoint_without_refetching(cfg, engine, hashing_embedder, monkeypatch):
    src = FakeSource("workday", _mixed_jobs())
    llm = FakeLLM()
    rt = init_runtime(cfg, llm_factory=lambda: llm, embedder_factory=lambda: hashing_embedder,
                      sources_factory=lambda names: [src])
    real = service.run_scoring

    def crash(*a, **kw):  # noqa: ANN002, ANN003
        raise KeyboardInterrupt("process killed during scoring")

    monkeypatch.setattr(service, "run_scoring", crash)
    run_id, initial = dg.start_run(rt, trigger="cli", sources=None, max_to_score=None)
    with pytest.raises(KeyboardInterrupt):
        dg.execute(rt, run_id, initial)
    assert queries.get_run(engine, run_id)["status"] == "failed"
    assert src.fetch_calls == 1
    assert rt.graph("discovery").get_state(dg.thread_config(run_id)).next == ("score",)

    monkeypatch.setattr(service, "run_scoring", real)
    dg.prepare_resume(rt, run_id)
    assert queries.get_run(engine, run_id)["finished_at"] is None
    result = dg.execute(rt, run_id, None)
    assert src.fetch_calls == 1  # ingest was not re-run
    assert result["counts"]["scored"] == 6
    assert queries.get_run(engine, run_id)["status"] == "complete"


def test_orphaned_running_run_is_marked_failed_and_resumable(cfg, engine, hashing_embedder, monkeypatch):
    src = FakeSource("workday", _mixed_jobs())
    rt = init_runtime(cfg, llm_factory=FakeLLM, embedder_factory=lambda: hashing_embedder,
                      sources_factory=lambda names: [src])
    monkeypatch.setattr(service, "run_scoring", lambda *a, **k: (_ for _ in ()).throw(SystemExit(1)))
    run_id, initial = dg.start_run(rt, trigger="cli", sources=None, max_to_score=None)
    with pytest.raises(SystemExit):
        dg.execute(rt, run_id, initial)
    # simulate a hard kill: status still 'running', lock row left behind by a dead pid
    queries.update_run_progress(engine, run_id, status="running")
    with engine.begin() as c:
        c.exec_driver_sql("INSERT INTO locks (name, acquired_at, pid) VALUES ('discovery_run', ?, 999999)",
                          (utcnow_iso(),))
    assert not locks.is_held(engine, locks.DISCOVERY_LOCK)  # the lock row exists but its holder is dead
    assert dg.reconcile_orphans(rt) == 1  # so the 'running' run is an orphan and is marked failed
    assert queries.get_run(engine, run_id)["status"] == "failed"
    monkeypatch.undo()
    dg.prepare_resume(rt, run_id)  # reclaims the dead lock, then resumes
    assert dg.execute(rt, run_id, None)["counts"]["scored"] == 6


def test_second_run_is_refused_while_one_is_in_progress(cfg, engine, hashing_embedder):
    rt = init_runtime(cfg, llm_factory=FakeLLM, embedder_factory=lambda: hashing_embedder,
                      sources_factory=lambda names: [])
    # Another live process holds the lock (use our parent pid, which is alive).
    with engine.begin() as c:
        c.exec_driver_sql("INSERT INTO locks (name, acquired_at, pid, note) VALUES ('discovery_run', ?, ?, 'serve')",
                          (utcnow_iso(), os.getppid()))
    with pytest.raises(locks.LockHeld):
        dg.start_run(rt, trigger="cli", sources=None, max_to_score=None)
    assert queries.list_runs(engine) == []

    from jobhunt import cli

    result = CliRunner().invoke(cli.app, ["--config", str(cfg.config_path), "run"])
    assert result.exit_code == 2 and "already in progress" in result.output


def test_dry_run_needs_no_api_key_and_writes_nothing(cfg, engine, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cfg.secrets.ANTHROPIC_API_KEY is None
    lines: list[str] = []
    summary = service.dry_run(cfg, [FakeSource("workday", _mixed_jobs())], None, echo=lines.append)
    assert summary["workday"]["counts"] == {"fetched": 13, "filtered": 7, "survived": 6}
    assert queries.status_counts(engine) == {}
    assert queries.list_runs(engine) == []

    import jobhunt.sources.registry as registry
    from jobhunt import cli

    monkeypatch.setattr(registry, "build_sources", lambda *a, **k: [FakeSource("workday", _mixed_jobs())])
    result = CliRunner().invoke(cli.app, ["--config", str(cfg.config_path), "run", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "fetched=13 filtered=7" in result.output


def test_jobs_stranded_in_discovered_by_an_earlier_run_are_processed_next_run(cfg, engine, hashing_embedder):
    # An earlier run stored these and died before prefilter; the next run only *sights* them.
    from jobhunt.sources.normalize import normalize_raw as nr

    stranded = [raw_job(500, title="Registered Nurse", company="Hospital X"),
                raw_job(501, title="Principal Integration Architect", company="Insurer X")]
    for j in stranded:
        queries.upsert_job(engine, nr(j))
    rt = init_runtime(cfg, llm_factory=FakeLLM, embedder_factory=lambda: hashing_embedder,
                      sources_factory=lambda names: [FakeSource("workday", stranded)])
    run_id, initial = dg.start_run(rt, trigger="cli", sources=None, max_to_score=None)
    result = dg.execute(rt, run_id, initial)
    assert result["counts"]["seen"] == 2 and result["counts"]["new"] == 0
    assert queries.status_counts(engine) == {"filtered_out": 1, "scored": 1}


def test_filtered_and_duplicate_rows_drop_description_and_embedding(cfg, engine, hashing_embedder):
    jobs = _mixed_jobs()
    jobs.append(raw_job(900, title="Principal Integration Architect 0", company="Insurer 0", source="adzuna"))
    rt = init_runtime(cfg, llm_factory=FakeLLM, embedder_factory=lambda: hashing_embedder,
                      sources_factory=lambda names: [FakeSource("workday", jobs)])
    run_id, initial = dg.start_run(rt, trigger="cli", sources=None, max_to_score=None)
    dg.execute(rt, run_id, initial)
    rows = queries.get_jobs(engine, queries.job_ids_with_status(engine, "filtered_out"))
    assert rows and all(r["description_text"] == "" and r["embedding"] is None for r in rows)
    assert all(r["title"] and r["url"] and r["status_reason"] for r in rows)
    for r in queries.get_jobs(engine, queries.job_ids_with_status(engine, "scored")):
        assert r["description_text"] and r["embedding"] is not None


def test_keep_filtered_descriptions_option(cfg, engine, hashing_embedder):
    cfg.filters.keep_filtered_descriptions = True
    rt = init_runtime(cfg, llm_factory=FakeLLM, embedder_factory=lambda: hashing_embedder,
                      sources_factory=lambda names: [FakeSource("workday", _mixed_jobs())])
    run_id, initial = dg.start_run(rt, trigger="cli", sources=None, max_to_score=None)
    dg.execute(rt, run_id, initial)
    rows = queries.get_jobs(engine, queries.job_ids_with_status(engine, "filtered_out"))
    assert rows and all(r["description_text"] for r in rows)


@pytest.mark.parametrize(
    ("location", "remote_type", "kept", "why"),
    [
        ("Austin, TX; Dallas, TX", "hybrid", False, "no office in the commute area"),
        ("Austin, TX; Dallas, TX; Parsippany, NJ", "hybrid", True, "one office of three is local"),
        ("New York, NY | Newark, NJ", "hybrid", True, "pipe-separated offices"),
        ("Newark, DE; New Castle, DE", "hybrid", False, "right town name, wrong state"),
        ("Madison, WI", "hybrid", False, "Madison is in Wisconsin"),
        ("Whippany, New Jersey", "unknown", True, "state spelled out"),
        ("2002 Summit Boulevard NE, Atlanta, GA", "hybrid", False, "street name is not the town"),
        ("Prudential Tower, 655 Broad Street, Newark, NJ; Scranton, PA", "hybrid", True, "address plus town"),
        ("Bloomfield, CT; United States Work at Home", "hybrid", True, "one option is work from home"),
        ("Bengaluru, India; Parsippany, NJ", "hybrid", True, "excluded country, but a local office too"),
        ("Bengaluru, India; London, UK", "hybrid", False, "every office is in an excluded country"),
        ("Remote - India", "remote", False, "remote, but only within an excluded country"),
    ],
)
def test_each_office_is_judged_on_its_own(cfg, location, remote_type, kept, why):
    job = {**rec(location=location), "remote_type": remote_type}
    reason = prefilter.knockout_reason(job, cfg)
    assert (reason is None) is kept, f"{why}: {location!r} -> {reason}"


def test_recheck_filtered_reopens_jobs_the_current_filters_accept(cfg, engine):
    far = rec(title="Integration Delivery Lead", location="Austin, TX")
    near = rec(title="Integration Delivery Lead", location="Parsippany, NJ")
    near["source_job_id"], far["remote_type"], near["remote_type"] = "job-near", "hybrid", "hybrid"
    ids = [queries.upsert_job(engine, r)[1] for r in (far, near)]
    for job_id in ids:
        queries.set_job_status(engine, job_id, "filtered_out", "logistics: hybrid, none within commute area")
    queries.strip_job_content(engine, ids)

    preview = service.recheck_filtered(engine, cfg, apply=False)
    assert preview["checked"] == 2 and preview["now_pass"] == 1 and preview["reopened"] == 0
    result = service.recheck_filtered(engine, cfg)
    assert result["reopened"] == 1
    statuses = {queries.get_job(engine, i)["status"] for i in ids}
    assert statuses == {"filtered_out", "discovered"}
    # a re-opened job has no description any more, so the adapter is told to fetch the posting again
    assert queries.job_exists(engine, far["source"], far["source_job_id"]) is True
    assert queries.job_exists(engine, near["source"], "job-near") is False


# --------------------------------------------------------------------------------------- stale postings


def test_a_capped_posted_label_is_not_an_age():
    """Workday files everything older than a month under "30+ Days Ago", so a job reposted last week looks
    exactly like one left up since spring. The date is kept as a lower bound; the age is unknown."""
    from datetime import UTC, datetime

    from jobhunt.sources.workday import parse_posted_on

    now = datetime(2026, 9, 21, tzinfo=UTC)
    when, days = parse_posted_on("Posted 30+ Days Ago", now)
    assert days is None                      # unknown, so nothing is dropped over it
    assert when is not None and when.date().isoformat() == "2026-08-22"

    assert parse_posted_on("Posted 12 Days Ago", now)[1] == 12      # a real relative age still counts
    assert parse_posted_on("Posted Today", now)[1] == 0
    assert parse_posted_on("Posted Yesterday", now)[1] == 1


def test_an_old_posting_is_kept_and_scored_on_its_merits(cfg):
    """A job's posted date says little about whether it is live: boards renew listings without changing it, and
    one the owner found on LinkedIn "2 weeks ago" was recorded here as 107 days old."""
    from jobhunt.discovery.prefilter import knockout_reason
    from jobhunt.sources.normalize import normalize_raw
    from tests.conftest import iso_days_ago, raw_job

    old = normalize_raw(raw_job(1, title="Director, Tech Lead - PGIM Fixed Income (Hybrid - Newark, NJ)",
                                location="Newark, NJ", posted_days_ago=107))
    cfg.filters.stale_action = "keep"
    assert knockout_reason(old, cfg) is None

    cfg.filters.stale_action = "filter"
    reason = knockout_reason(old, cfg)
    assert reason and reason.startswith("age:")

    # nothing else changes: a job that fails another rule still fails it
    cfg.filters.stale_action = "keep"
    far = normalize_raw(raw_job(2, location="Austin, TX", posted_days_ago=107))
    assert (knockout_reason(far, cfg) or "").startswith(("location", "logistics"))
    assert iso_days_ago(1) is not None


# --------------------------------------------------------------------------------------------- skill levels


FACTS_FOR_SKILLS = [
    {"id": "skill-cloud", "kind": "skill", "text": "Cloud and Infrastructure: AWS (EC2, S3, Lambda)",
     "tags": ["aws", "ec2", "s3", "lambda"]},
    {"id": "skill-int", "kind": "skill", "text": "Integration and APIs: MuleSoft Anypoint Platform, Kafka",
     "tags": ["mulesoft", "mulesoft anypoint platform", "kafka", "aws"]},
    {"id": "role-a", "kind": "role", "text": "Led integration work", "tags": ["mulesoft", "oauth"]},
    {"id": "ach-a", "kind": "achievement", "parent_id": "role-a", "text": "Secured APIs with OAuth",
     "tags": ["oauth", "kafka"]},
]


def test_the_skill_list_comes_from_the_fact_bank():
    from jobhunt.facts import skills

    saved = [{"skill": "aws", "label": "AWS", "level": 5, "custom": 0},
             {"skill": "kubernetes", "label": "Kubernetes", "level": 3, "custom": 1}]
    items = skills.catalog(FACTS_FOR_SKILLS, saved)
    by = {it["skill"]: it for it in items}
    # grouped under the skill fact's own heading, spelled the way the facts spell it, each skill once
    assert by["aws"]["category"] == "Cloud and Infrastructure" and by["aws"]["label"] == "AWS"
    assert by["mulesoft"]["label"] == "MuleSoft" and by["mulesoft"]["category"] == "Integration and APIs"
    assert [it["skill"] for it in items].count("aws") == 1
    # tags on roles and accomplishments that no skill fact lists
    assert by["oauth"]["category"] == skills.EXPERIENCE and by["oauth"]["uses"] == 2
    # ratings and hand-added skills
    assert by["aws"]["level"] == 5 and by["ec2"]["level"] is None
    assert by["kubernetes"]["category"] == skills.CUSTOM and by["kubernetes"]["custom"]
    assert "- AWS: 5/10 (working)" in skills.prompt_text({"aws": 5}, {"aws": "AWS"})


def test_a_rating_matches_the_technologies_it_covers():
    levels = {"aws": 5, "mulesoft anypoint platform": 9, "java": 6}
    assert scoring.level_for("AWS", levels) == 5
    assert scoring.level_for("AWS Lambda", levels) == 5              # a rated skill named inside it
    assert scoring.level_for("MuleSoft", levels) == 9                # named by a rated skill
    assert scoring.level_for("JavaScript", levels) is None           # "java" is not JavaScript
    assert scoring.level_for("Kubernetes", levels) is None
    assert scoring.level_for("Java 17", levels) == 6                # a version number doesn't hide it


def test_versions_and_bracketed_acronyms_still_match_the_background():
    assert scoring.name_variants("OpenID Connect (OIDC)") == ["OpenID Connect (OIDC)", "OpenID Connect", "OIDC"]
    assert scoring._background_names("Secured APIs with OAuth and OIDC", "OAuth 2.0")
    assert scoring._background_names("Secured APIs with OAuth and OIDC", "OpenID Connect (OIDC)")
    assert not scoring._background_names("Secured APIs with OAuth", "SAML 2.0")


def test_your_rating_against_the_depth_a_posting_needs_sets_the_credit():
    """AWS rated 5 on a posting that needs expert AWS is about half a match, not a full one."""
    stack = [{"technology": "AWS", "importance": "required", "level_required": "expert", "has": "recent"},
             {"technology": "MuleSoft", "importance": "required", "level_required": "advanced", "has": "recent"},
             {"technology": "Kafka", "importance": "preferred", "level_required": "working", "has": "earlier"}]
    # unrated: recent counts as 6 and older use as 4, against the depth needed (expert 9, advanced 7, working 5)
    unrated = scoring.stack_skill_match(stack, {})
    assert unrated == round(100 * (2 * 6 / 9 + 2 * 6 / 7 + 4 / 5) / 5)
    rated = scoring.stack_skill_match(stack, {"aws": 5, "mulesoft": 9})
    assert rated == round(100 * (2 * 5 / 9 + 2 * 1.0 + 4 / 5) / 5)        # 9 >= advanced (7): full credit
    # at working level an unrated skill earns what it always did: recent 1.0, older 0.8
    working = [{**k, "level_required": "working"} for k in stack]
    assert scoring.stack_skill_match(working, {}) == round(100 * (2 + 2 + 0.8) / 5)
    # a rated skill counts even where the facts never mention it
    missing = [{**stack[0], "has": "no", "level_required": "working"}] * 3
    assert scoring.stack_skill_match(missing, {"aws": 5}) == 100


def test_categories_are_not_technologies():
    """Seen live: qwen listed "Agile, Automation, Cloud" for a virtualization job, and "Cloud" took the rating for
    "Cloud Migration"."""
    assert scoring.level_for("Cloud", {"cloud migration": 7}) is None
    assert scoring.level_for("MuleSoft", {"mulesoft anypoint platform": 9}) == 9   # a specific name still matches
    stack = [{"technology": t, "importance": "preferred", "level_required": "working", "has": "recent"}
             for t in ("Agile", "Automation", "Cloud", "Java")]
    assert scoring.stack_skill_match(stack, {}) is None                 # two real items left: too few to score


def test_a_short_stack_is_blended_with_the_model_estimate():
    stack = [{"technology": t, "importance": "required", "level_required": "working", "has": "recent"}
             for t in ("Java", "Python", "Kafka")]
    assert scoring.stack_skill_match(stack, {}) == 100                  # no estimate to blend with
    assert scoring.stack_skill_match(stack, {}, model_estimate=40) == 76  # 3/5 list, 2/5 model
    five = stack + [{**stack[0], "technology": t} for t in ("Kubernetes", "Terraform")]
    assert scoring.stack_skill_match(five, {}, model_estimate=40) == 100  # five items stand on their own


def test_changing_a_rating_updates_stored_scores_without_a_model(cfg, engine):
    from jobhunt.llm.schemas import RoleAnalysis, StackItem

    _, job_id = queries.upsert_job(engine, rec(description=POSTING_V5))
    job = {"id": job_id, **rec(description=POSTING_V5)}
    stack = [StackItem(technology=t, importance="required", level_required=lvl, candidate_has="recent",
                       candidate_quote=q)
             for t, q, lvl in (("MuleSoft", "MuleSoft CloudHub", "advanced"), ("AWS", "AWS EKS", "expert"),
                               ("CI/CD", "Jenkins CI/CD", "working"))]
    role = RoleAnalysis(role_type="technical_lead", hands_on="low", quote="own delivery of the roadmap")
    row = scoring.enforce(_v5_score(stack, role), job, cfg, ghost=0, truncated=False, background=BACKGROUND_V5)
    queries.record_score(engine, job_id, row, set_scored=False)
    # unrated: MuleSoft 6/7 (advanced), AWS 6/9 (expert), CI/CD full; three items, blended with the model's 50
    before = 0.6 * 100 * (6 / 7 + 6 / 9 + 1) / 3 + 0.4 * 50
    assert queries.latest_score(engine, job_id)["skill_match"] == round(before)

    queries.save_skill_levels(engine, [{"skill": "aws", "label": "AWS", "level": 5}])
    service.recompute_totals(engine, cfg)
    after = queries.latest_score(engine, job_id)
    assert after["skill_match"] == round(0.6 * 100 * (6 / 7 + 5 / 9 + 1) / 3 + 0.4 * 50)
    assert after["total"] < row["total"]


def test_a_stored_stack_of_categories_no_longer_props_up_a_score(cfg, engine):
    """Scores made before categories were dropped: "Agile, Automation, Cloud" gave a 100 that recompute can't
    re-derive (the model's own estimate wasn't kept), so it falls to the evidence cap until re-scored."""
    _, job_id = queries.upsert_job(engine, rec(description=POSTING_V5))
    stack = [{"technology": t, "importance": "preferred", "level_required": "working", "has": "recent",
              "claimed": "recent", "candidate_quote": ""} for t in ("Agile", "Automation", "Cloud")]
    row = {"model": "local:qwen3:8b", "rubric_version": "v6", "total": 90.0, "skill_match": 100, "role_fit": 92,
           "seniority_fit": 69, "domain_fit": 60, "comp_fit": 50, "logistics_fit": 60, "rationale": "ok",
           "ghost_score": 0, "analysis": {"stack": stack, "skill_from_stack": True, "role": None}}
    queries.record_score(engine, job_id, row, set_scored=False)
    service.recompute_totals(engine, cfg)
    assert queries.latest_score(engine, job_id)["skill_match"] == scoring.EVIDENCE_CAP


# --------------------------------------------------------------------------------------------- competencies (v8)

TPM_POSTING = ("<p>Lead Technical Program Manager - Strategic Initiatives and Delivery Office. You will drive technical "
               "program management across engineering teams, own stakeholder management with architects and business "
               "partners, and manage risk and dependencies. Technical fluency with Java and AWS preferred.</p>")
TPM_BACKGROUND = ("Led platform migration of 150+ legacy applications to modern platforms, managing offshore teams of "
                  "up to 10 developers. Coordinated with 10+ teams including EIS, Infrastructure, Security. "
                  "Built REST APIs in Java on AWS.")


def test_leadership_skills_are_competencies_not_technologies():
    """Seen live: a delivery role's "Technical Program Management" and "Stakeholder Management" landed in the
    technology stack, were marked missing because the facts never use those words, and sank its score to 58."""
    for name in ("Technical Program Management", "Stakeholder Management", "Project Lifecycle Management",
                 "Cross-functional collaboration", "Risk Management", "AI Capabilities"):
        assert scoring.is_competency(name), name
    for name in ("MuleSoft", "AWS Lambda", "API Management", "Kafka", "Java 17", "Jira"):
        assert not scoring.is_competency(name), name


def test_a_competency_counts_on_evidence_not_on_matching_words(cfg):
    from jobhunt.llm.schemas import Competency

    items = [Competency(competency="technical program management", importance="required",
                        candidate_evidence="Led platform migration of 150+ legacy applications"),
             Competency(competency="stakeholder management", importance="required",
                        candidate_evidence="Coordinated with 10+ teams"),
             Competency(competency="risk and dependencies", importance="preferred",
                        candidate_evidence="Owned the enterprise risk register"),        # not in the background
             Competency(competency="budget ownership", importance="required",
                        candidate_evidence="Led platform migration")]                    # not in the posting
    kept = scoring.check_competencies(items, TPM_POSTING, TPM_BACKGROUND)
    assert [(k["competency"], k["held"]) for k in kept] == [
        ("technical program management", True), ("stakeholder management", True), ("risk and dependencies", False)]
    assert scoring.competency_match(kept) == round(100 * (2 + 2) / 5)


@pytest.mark.parametrize(("role_type", "expected"), [
    ("delivery_lead", round(0.7 * 100 + 0.3 * 40)),        # a delivery role is judged mostly on leading delivery
    ("individual_contributor", round(0.2 * 100 + 0.8 * 40)),
])
def test_competencies_and_technologies_mix_by_the_kind_of_job(role_type, expected):
    stack = [{"technology": t, "importance": "required", "level_required": "working", "has": h}
             for t, h in (("Java", "recent"), ("Kubernetes", "no"), ("Terraform", "no"), ("AWS", "no"),
                          ("Go", "recent"))]
    comps = [{"competency": c, "importance": "required", "held": True, "evidence": "x"}
             for c in ("program management", "stakeholder management")]
    analysis = {"stack": stack, "skill_from_stack": True, "competencies": comps, "role": {"role_type": role_type}}
    assert scoring.analysis_skill_match(analysis, {}) == expected


def test_a_delivery_role_is_scored_on_its_competencies(cfg):
    """The owner's case end to end: role fit high, and skills no longer sunk by leadership words it can't match."""
    from jobhunt.llm.schemas import Competency, JobScoreV8, RoleAnalysis, StackItem

    job = {"id": 11, **rec(title="Lead Technical Program Manager", description=TPM_POSTING)}
    score = JobScoreV8(
        stack=[StackItem(technology=t, importance="required", level_required="expert", candidate_has="no")
               for t in ("Technical Program Management", "Stakeholder Management")]
        + [StackItem(technology="Java", importance="preferred", level_required="working", candidate_has="recent",
                     candidate_quote="Built REST APIs in Java")],
        role=RoleAnalysis(role_type="delivery_lead", hands_on="low", quote="drive technical program management"),
        competencies=[Competency(competency="technical program management", importance="required",
                                 candidate_evidence="Led platform migration of 150+ legacy applications"),
                      Competency(competency="stakeholder management", importance="required",
                                 candidate_evidence="Coordinated with 10+ teams")],
        skill_match=69, role_fit=50, domain_fit=60, seniority_fit=60, comp_fit=50, logistics_fit=60,
        red_flags=[], gaps=[], evidence=[], rationale="ok")
    row = scoring.enforce(score, job, cfg, ghost=0, truncated=False, background=TPM_BACKGROUND)
    assert [t["technology"] for t in row["analysis"]["stack"]] == ["Java"]   # competencies left the stack
    assert row["role_fit"] == scoring.ROLE_FIT["delivery_lead"]["low"]
    assert row["skill_match"] == round(0.7 * 100 + 0.3 * 69)                 # all held; stack too short -> estimate


def test_scores_made_before_v8_are_recomputed_as_they_were_made():
    """New weights apply going forward: a pre-v8 stack keeps its leadership items when totals are recomputed."""
    stack = [{"technology": t, "importance": "required", "level_required": "working", "has": h}
             for t, h in (("Technical Program Management", "no"), ("Stakeholder Management", "earlier"),
                          ("Java", "recent"), ("AWS", "recent"))]
    old = {"stack": stack, "skill_from_stack": True, "model_skill_match": 60, "role": {"role_type": "delivery_lead"}}
    assert scoring.analysis_skill_match(old, {}) == scoring.stack_skill_match(stack, {}, 60, drop_competencies=False)
    assert scoring.analysis_skill_match({**old, "competencies": []}, {}) is None     # v8: two technologies left


# --------------------------------------------------------------------------------------------- industry and role


def test_integration_technical_and_delivery_leads_rank_highest_and_hands_on_is_acceptable():
    table = scoring.ROLE_FIT
    leads = min(table["technical_lead"]["high"], table["delivery_lead"]["high"])        # even hands-on leads
    assert leads > table["people_manager"]["high"] and table["technical_lead"]["low"] >= table["people_manager"]["low"]
    assert table["people_manager"]["low"] > table["architect"]["low"] > table["individual_contributor"]["low"]
    assert table["individual_contributor"]["high"] >= 45                                  # ok, not preferred


def test_insurance_employers_score_highest_on_industry(cfg):
    from jobhunt.config import CompanyConfig

    cfg.companies = [CompanyConfig(name="Prudential", ats_type="workday", board_token="x", industry="insurance"),
                     CompanyConfig(name="Guidewire", ats_type="workday", board_token="y", industry="insurtech"),
                     CompanyConfig(name="Merck", ats_type="workday", board_token="z", industry="pharma")]
    cfg.scoring.industry_by_employer = {"JPMorgan Chase": "financial"}
    cfg.scoring.industry_scores = {"insurance": 100, "insurtech": 95, "financial": 75, "health": 60, "unknown": 50,
                                   "other": 40}
    assert scoring.industry_fit("Prudential", cfg) == 100
    assert scoring.industry_fit("Guidewire", cfg) == 95
    assert scoring.industry_fit("JPMorgan Chase Bank, N.A.", cfg) == 75        # the longest listed name it starts with
    assert scoring.industry_fit("Merck", cfg) == 40                            # a listed industry without a score
    assert scoring.industry_fit("Acme Assurance Co", cfg) == 100               # not listed; the name says insurance
    assert scoring.industry_fit("Snowflake", cfg) == 50                        # unknown


def test_new_role_and_industry_rules_apply_to_stored_scores_without_a_model(cfg, engine):
    from jobhunt.config import CompanyConfig

    cfg.companies = [CompanyConfig(name="Acme Insurance", ats_type="workday", board_token="x", industry="insurance")]
    cfg.scoring.weights = {**cfg.scoring.weights, "industry_fit": 0.15}
    _, job_id = queries.upsert_job(engine, rec(company="Acme Insurance"))
    queries.record_score(engine, job_id, {
        "model": "local:qwen3:8b", "rubric_version": "v8", "total": 50.0, "skill_match": 70, "role_fit": 65,
        "seniority_fit": 69, "domain_fit": 60, "comp_fit": 50, "logistics_fit": 60, "rationale": "ok", "ghost_score": 0,
        "analysis": {"stack": [], "competencies": [], "skill_from_stack": False,
                     "role": {"role_type": "technical_lead", "hands_on": "high", "verified": True, "role_fit": 65}}},
        set_scored=False)
    service.recompute_totals(engine, cfg)
    after = queries.latest_score(engine, job_id)
    assert after["role_fit"] == 85 and after["industry_fit"] == 100 and after["skill_match"] == 70
    assert after["total"] > 50.0


# --------------------------------------------------------------------------------------------- hybrid out of state


def _hybrid(location: str) -> dict:
    return {"title": "Integration Lead", "location_raw": location, "remote_type": "hybrid", "employment_type": "fte"}


@pytest.mark.parametrize(("location", "kept"), [
    ("Parsippany, NJ", True),
    ("WI Madison; MA Boston", False),                                          # a commute-town name in another state
    ("San Antonio Home Office I", False),                                      # USAA's campus, not working from home
    ("Remote Flex - North Carolina; Remote Flex - Mississippi", False),        # remote only within those states
    ("Work At Home-Connecticut; Work At Home-Texas", False),
    ("Newark, NJ; Work At Home-Connecticut", True),                            # a local office is enough
    ("New York, NY (HQ); San Francisco, CA; Remote (US)", True),               # a US-wide remote option
])
def test_hybrid_jobs_in_other_states_are_removed(cfg, location, kept):
    cfg.filters.local_states = ["NJ", "NY", "PA"]
    cfg.filters.local_location_patterns = [r"Parsippany|Newark|Madison", r"\bNJ\b", r"New York"]
    assert (prefilter.knockout_reason(_hybrid(location), cfg) is None) is kept


def test_every_state_name_is_known():
    for name, code in (("Mississippi", "MS"), ("Remote - New Mexico", "NM"), ("Idaho", "ID"), ("Vermont", "VT")):
        assert code in prefilter.states_in(name)


def test_recheck_applies_todays_filters_both_ways(cfg, engine):
    cfg.filters.local_states = ["NJ", "NY", "PA"]
    cfg.filters.local_location_patterns = [r"Parsippany|Newark", r"\bNJ\b"]
    _, stays = queries.upsert_job(engine, rec(location="Parsippany, NJ"))
    out_rec = rec(location="San Antonio Home Office I")
    out_rec.update(source_job_id="job-out", remote_type="hybrid")
    _, goes = queries.upsert_job(engine, out_rec)
    queries.set_job_status(engine, goes, "scored")
    # knocked out for a clearance requirement, description since dropped: must not come back
    clear_rec = rec(location="Parsippany, NJ")
    clear_rec.update(source_job_id="job-clear", description_text="")
    _, cleared = queries.upsert_job(engine, clear_rec)
    queries.set_job_status(engine, cleared, "filtered_out", "dealbreaker: description matches 'clearance required'")

    result = service.recheck_filtered(engine, cfg)
    assert result["removed"] == 1 and result["reopened"] == 0
    assert queries.get_job(engine, goes)["status"] == "filtered_out"
    assert queries.get_job(engine, goes)["status_reason"].startswith("logistics: hybrid")
    assert queries.get_job(engine, stays)["status"] == "discovered"
    assert queries.get_job(engine, cleared)["status"] == "filtered_out"


# --------------------------------------------------------------------------------------------- ideal job


def test_ideal_fit_maps_similarity_to_the_ideal_job_onto_0_to_100(cfg):
    import numpy as np

    from jobhunt.db import vectors

    cfg.scoring.ideal_job.low, cfg.scoring.ideal_job.high = 0.60, 0.80
    ideal = np.zeros(vectors.DIM, dtype=np.float32)
    ideal[0] = 1.0

    def job_at(cos: float) -> bytes:
        v = np.zeros(vectors.DIM, dtype=np.float32)
        v[0], v[1] = cos, (1 - cos * cos) ** 0.5
        return vectors.to_blob(v)

    assert scoring.ideal_fit(job_at(0.90), ideal, cfg) == 100
    assert scoring.ideal_fit(job_at(0.70), ideal, cfg) == 50
    assert scoring.ideal_fit(job_at(0.50), ideal, cfg) == 0
    assert scoring.ideal_fit(None, ideal, cfg) is None and scoring.ideal_fit(job_at(0.7), None, cfg) is None


def test_the_ideal_job_is_the_configured_role_and_reaches_stored_scores(cfg, engine, hashing_embedder):
    """Measured against the owner's ideal role (its text and accomplishments), without calling a model."""
    from jobhunt.db import vectors

    facts = [{"id": "role-ideal", "kind": "role", "text": "Led MuleSoft integration delivery with offshore developers",
              "org": "Acme Insurance Inc.", "title": "Project Lead"},
             {"id": "ach-ideal", "kind": "achievement", "parent_id": "role-ideal",
              "text": "Delivered Mule APIs replacing legacy system calls"}]
    queries.replace_facts(engine, [{**f, "embedding": vectors.to_blob(hashing_embedder.embed([f["text"]])[0])}
                                   for f in facts])
    cfg.scoring.ideal_job.role = "role-ideal"
    cfg.scoring.ideal_job.low, cfg.scoring.ideal_job.high = 0.0, 1.0  # the offline hashing embedder runs low
    cfg.scoring.weights = {**cfg.scoring.weights, "ideal_fit": 0.15}
    assert service.ideal_vector(engine, cfg) is not None

    _, near = queries.upsert_job(engine, rec(title="Integration Lead",
                                             description="<p>Lead MuleSoft integration delivery with offshore "
                                                         "developers, replacing legacy system calls with Mule APIs.</p>"))
    far_rec = rec(title="Paid Media Manager", company="Other Co",
                  description="<p>Run paid social campaigns and media budgets.</p>")
    far_rec["source_job_id"] = "job-far"
    _, far = queries.upsert_job(engine, far_rec)
    for job_id in (near, far):
        text = queries.get_job(engine, job_id)["description_text"]
        queries.update_job_normalized(engine, job_id, company_id=None,
                                      embedding=vectors.to_blob(hashing_embedder.embed([text])[0]))
        queries.record_score(engine, job_id, {"model": "x", "rubric_version": "v8", "total": 50.0, "skill_match": 60,
                                              "role_fit": 60, "seniority_fit": 60, "domain_fit": 60, "comp_fit": 50,
                                              "logistics_fit": 60, "rationale": "ok", "ghost_score": 0},
                             set_scored=False)
    service.recompute_totals(engine, cfg)
    close, distant = (queries.latest_score(engine, j)["ideal_fit"] for j in (near, far))
    assert close is not None and distant is not None and close > distant


def test_the_title_allowlist_no_longer_filters_it_informs_scoring(cfg):
    """Seen live: "Managing Engineer" didn't match the word "manager" and four Allstate roles were dropped. The
    allowlist's terms now go to the scoring model to read by meaning; only the blocklist knocks titles out."""
    cfg.filters.title_allowlist = [r"\b(architect|lead|manager|head of)\b"]
    for title in ("Managing Engineer", "Senior Data Engineer", "Engineering Lead"):
        assert prefilter.title_reason(title, cfg) is None
    assert prefilter.title_reason("Junior Integration Developer", cfg)                  # the blocklist still applies
    assert scoring.target_title_terms(cfg.filters.title_allowlist) == "architect, lead, manager, head of"
    prompt = scoring.build_system_prompt(cfg, {}, [], False)
    if cfg.scoring.rubric_version not in ("v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8"):
        assert "architect, lead, manager, head of" in prompt and "MEANING" in prompt


def test_a_run_scores_its_newest_finds_before_an_older_backlog(cfg, engine):
    """Seen live: a run spent its scoring on 700 re-opened older jobs and left all 54 of its own new ones unscored."""
    from sqlalchemy import text

    old_rec, new_rec = rec(title="Integration Lead"), rec(title="Delivery Lead")
    new_rec["source_job_id"] = "job-new"
    _, old = queries.upsert_job(engine, old_rec)
    _, new = queries.upsert_job(engine, new_rec)
    with engine.begin() as c:
        c.execute(text("UPDATE jobs SET first_seen_at = '2026-09-15T00:00:00Z' WHERE id = :id"), {"id": old})
    scored, _, _ = service.run_scoring(engine, cfg, FakeLLM(), [old, new], {}, max_to_score=1)
    assert scored == 1
    assert queries.get_job(engine, new)["status"] == "scored" and queries.get_job(engine, old)["status"] == "discovered"


# --------------------------------------------------------------------------------------------- title checks


@pytest.mark.parametrize(("title", "role_type", "expected"), [
    ("VP of Demand Generation", "delivery_lead", scoring.NON_TECH_ROLE_FIT),          # not technology work
    ("Strategic Analytics Account Manager", "delivery_lead", scoring.NON_TECH_ROLE_FIT),
    ("Software Engineer (All Levels)", "technical_lead", scoring.ROLE_FIT["individual_contributor"]["low"]),
    ("Lead Technical Program Manager", "delivery_lead", scoring.ROLE_FIT["delivery_lead"]["low"]),
    ("Managing Engineer (C#/ASP.NET)", "technical_lead", scoring.ROLE_FIT["technical_lead"]["low"]),
    ("MuleSoft Architect", "technical_lead", scoring.ROLE_FIT["architect"]["low"]),        # an architect role
])
def test_the_title_checks_the_models_role_classification(title, role_type, expected):
    """Seen live: qwen called "VP of Demand Generation" a delivery lead and "Software Engineer (All Levels)" a
    technical lead, and both scored near the top."""
    role = {"role_type": role_type, "hands_on": "low", "verified": True}
    assert scoring.role_fit_from(role, title, []) == expected


def test_a_non_technology_job_gets_no_credit_for_leadership_competencies():
    comps = [{"competency": c, "importance": "required", "held": True, "evidence": "x"}
             for c in ("stakeholder management", "cross-functional collaboration")]
    analysis = {"stack": [], "skill_from_stack": False, "model_skill_match": 40, "competencies": comps,
                "role": {"role_type": "delivery_lead"}}
    assert scoring.analysis_skill_match(analysis, {}, "Lead Technical Program Manager") == round(0.7 * 100 + 0.3 * 40)
    assert scoring.analysis_skill_match(analysis, {}, "VP of Demand Generation") is None   # the model's estimate stands


# --------------------------------------------------------------------------------------------- prompt caching


def test_the_scoring_system_prompt_is_the_same_for_every_job_so_it_caches(cfg):
    """Everything that varies by job goes in the user message; the system prompt (rubric + your background) is sent
    with cache_control, so after the first job it is read from Claude's prompt cache at a tenth of the price."""
    background = {"summary": "Integration lead", "recent": "### Project Lead at Acme Insurance"}
    assert scoring.build_system_prompt(cfg, background, [], True) == scoring.build_system_prompt(cfg, background, [],
                                                                                                    False)
    snippet = scoring.user_message({**rec(title="Integration Lead"), "id": 1}, truncated=True)
    assert snippet.startswith("Note: this posting's description is a truncated snippet")
    seen = {}

    class Recorder(FakeLLM):
        def structured(self, schema, **kw):  # noqa: ANN001, ANN003, ANN201
            seen.update(kw)
            return super().structured(schema, **{k: v for k, v in kw.items() if k != "cache_system"})

    scoring.score_job({**rec(), "id": 1}, cfg, Recorder(), ghost=0, background=background, calibration=[],
                      truncated=False)
    assert seen["cache_system"] is True


def test_cached_input_is_billed_at_a_tenth_and_writes_at_a_quarter_more():
    from jobhunt.llm.client import cost_usd

    plain = cost_usd("claude-haiku-4-5", 10_000, 1_000)
    assert plain == round((10_000 * 1.0 + 1_000 * 5.0) / 1e6, 6)
    assert cost_usd("claude-haiku-4-5", 10_000, 1_000, cache_read=9_000) == round(
        (1_000 * 1.0 + 9_000 * 0.1 + 1_000 * 5.0) / 1e6, 6)
    assert cost_usd("claude-haiku-4-5", 10_000, 1_000, cache_write=9_000) == round(
        (1_000 * 1.0 + 9_000 * 1.25 + 1_000 * 5.0) / 1e6, 6)

