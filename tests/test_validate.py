"""The single most important checks in the system (SPEC 7.3) — written before the implementation.

Resume bullets must cite a fact from this thread's candidate set, and every number, percentage, currency
amount and date range must appear in the cited fact. ATS-compliance rules (SPEC 7.6) are enforced by the
same validator, so model drafts and hand edits are held to the same bar."""

from __future__ import annotations

import pytest

from jobhunt.tailor import validate as v

FACTS = {
    "role-acme": {"id": "role-acme", "kind": "role", "org": "Acme Insurance", "title": "Lead Integration Architect",
                  "start_date": "2019-03", "end_date": "present", "text": "Led integration architecture at Acme.",
                  "metrics": {}, "tags": []},
    "ach-gateway": {"id": "ach-gateway", "kind": "achievement", "parent_id": "role-acme",
                    "text": "Migrated 120 partner integrations to an API gateway, cutting onboarding time 60%.",
                    "metrics": {"integrations": "120", "onboarding_reduction_pct": 60}, "tags": ["api"]},
    "ach-budget": {"id": "ach-budget", "kind": "achievement", "parent_id": "role-acme",
                   "text": "Owned a $2.5M annual integration platform budget across 3 teams.",
                   "metrics": {"budget_usd": "$2.5M", "teams": 3}, "tags": ["budget"]},
    "ach-mule": {"id": "ach-mule", "kind": "achievement", "parent_id": "role-beta",
                 "text": "Introduced MuleSoft between 2015 and 2018, reaching 99.95% uptime and 3x throughput.",
                 "metrics": {}, "tags": ["mulesoft"]},
    "role-beta": {"id": "role-beta", "kind": "role", "org": "Beta Health", "title": "Senior Engineer",
                  "start_date": "2014-01", "end_date": "2019-02", "text": "Engineer at Beta Health.", "metrics": {},
                  "tags": []},
    "ach-outside": {"id": "ach-outside", "kind": "achievement", "parent_id": "role-beta",
                    "text": "Mentored 12 engineers.", "metrics": {}, "tags": []},
    "summary-primary": {"id": "summary-primary", "kind": "summary",
                        "text": "Integration architect with 25 years of experience in insurance and health.",
                        "metrics": {}, "tags": []},
    "edu-rutgers": {"id": "edu-rutgers", "kind": "education", "org": "Rutgers University",
                    "title": "B.S. Computer Science", "end_date": "1999", "text": "B.S. Computer Science, Rutgers.",
                    "metrics": {}, "tags": []},
    "skill-integration": {"id": "skill-integration", "kind": "skill", "text": "MuleSoft, REST, API gateways",
                          "metrics": {}, "tags": ["mulesoft", "rest", "api"]},
}
CANDIDATES = {"role-acme", "ach-gateway", "ach-budget", "ach-mule", "role-beta", "summary-primary", "edu-rutgers",
              "skill-integration"}


def resume(bullets_acme: list[str], bullets_beta: list[str] | None = None, *, summary: str | None = None,
           extra: str = "") -> str:
    beta = bullets_beta if bullets_beta is not None else ["Introduced MuleSoft, reaching 99.95% uptime and 3x throughput."]
    lines = [
        "## Summary",
        summary or "Integration architect with 25 years of experience in insurance and health.",
        "",
        "## Skills",
        "Integration: MuleSoft, REST, API gateways",
        "",
        "## Experience",
        "### Lead Integration Architect | Acme Insurance | Mar 2019 – Present",
        *[f"- {b}" for b in bullets_acme],
        "",
        "### Senior Engineer | Beta Health | Jan 2014 – Feb 2019",
        *[f"- {b}" for b in beta],
        "",
        "## Education",
        "### B.S. Computer Science | Rutgers University | 1999",
    ]
    return "\n".join(lines) + extra


def run(md: str, fact_map: list[dict], **kw) -> list[dict]:  # noqa: ANN003
    return v.validate_resume(md, fact_map, FACTS, CANDIDATES, max_bullet_chars=220, max_bullets_per_role=5,
                             sections=["summary", "skills", "experience", "education", "certifications"], **kw)


GOOD_MAP = [{"bullet_index": 0, "fact_id": "ach-gateway"}, {"bullet_index": 1, "fact_id": "ach-budget"},
            {"bullet_index": 2, "fact_id": "ach-mule"}]
GOOD = resume(["Migrated 120 partner integrations to an API gateway, cutting onboarding time 60%.",
               "Owned a $2.5M integration platform budget across 3 teams."])


def problems(errors: list[dict]) -> set[str]:
    return {e["problem"] for e in errors}


# --------------------------------------------------------------------------------------------- numeric tokens


def test_token_extraction_kinds():
    toks = {(t.kind, t.value) for t in v.extract_numeric_tokens(
        "Cut costs 40% ($1.2M) for 1,200 APIs, 3x faster, 25+ teams, 2015–2018, 99.95 percent, $500K")}
    assert ("percent", 40.0) in toks
    assert ("currency", 1_200_000.0) in toks
    assert ("number", 1200.0) in toks
    assert ("multiplier", 3.0) in toks
    assert ("number", 25.0) in toks
    assert ("number", 2015.0) in toks and ("number", 2018.0) in toks
    assert ("percent", 99.95) in toks
    assert ("currency", 500_000.0) in toks


def test_token_extraction_ignores_identifiers():
    assert v.extract_numeric_tokens("Built on AWS S3, EC2, K8s and 5G networks") == []


# --------------------------------------------------------------------------------------------- the core invariant


def test_valid_resume_passes():
    assert run(GOOD, GOOD_MAP) == []


def test_fabricated_percentage_is_rejected():
    md = resume(["Migrated 120 partner integrations to an API gateway, cutting onboarding time 75%.",
                 "Owned a $2.5M integration platform budget across 3 teams."])
    errors = run(md, GOOD_MAP)
    assert problems(errors) == {"unsupported_number"}
    assert errors[0]["bullet_index"] == 0 and "75%" in errors[0]["detail"]
    assert "75%" in errors[0]["bullet_text"]


def test_fabricated_currency_count_and_multiplier_are_rejected():
    md = resume(["Migrated 150 partner integrations to an API gateway.", "Owned a $4M budget across 3 teams."],
                ["Introduced MuleSoft, reaching 99.95% uptime and 5x throughput."])
    errors = run(md, GOOD_MAP)
    details = " ".join(e["detail"] for e in errors)
    assert {e["bullet_index"] for e in errors} == {0, 1, 2}
    assert "150" in details and "$4M" in details and "5x" in details


def test_number_supported_by_a_different_fact_is_still_rejected():
    # 12 appears in ach-outside, but the bullet cites ach-gateway.
    md = resume(["Migrated 120 partner integrations and mentored 12 engineers.",
                 "Owned a $2.5M integration platform budget across 3 teams."])
    assert "unsupported_number" in problems(run(md, GOOD_MAP))


def test_fabricated_date_range_is_rejected():
    md = resume(["Migrated 120 partner integrations to an API gateway between 2016 and 2017.",
                 "Owned a $2.5M integration platform budget across 3 teams."])
    errors = run(md, GOOD_MAP)
    assert "unsupported_number" in problems(errors)
    ok = resume(["Migrated 120 partner integrations to an API gateway, cutting onboarding time 60%.",
                 "Owned a $2.5M integration platform budget across 3 teams."],
                ["Introduced MuleSoft from 2015–2018, reaching 99.95% uptime."])
    assert run(ok, GOOD_MAP) == []


def test_equivalent_number_spellings_match():
    md = resume(["Migrated 120 partner integrations, cutting onboarding time 60 percent.",
                 "Owned a $2,500,000 integration platform budget across three teams."])
    assert run(md, GOOD_MAP) == []


def test_metric_values_count_as_support():
    fact = {**FACTS["ach-gateway"], "text": "Migrated partner integrations to an API gateway."}
    facts = {**FACTS, "ach-gateway": fact}
    md = resume(["Migrated 120 partner integrations, cutting onboarding time 60%.",
                 "Owned a $2.5M integration platform budget across 3 teams."])
    assert v.validate_resume(md, GOOD_MAP, facts, CANDIDATES) == []


def test_percent_cannot_be_supported_by_a_plain_count():
    md = resume(["Migrated 120% more partner integrations.", "Owned a $2.5M integration platform budget across 3 teams."])
    assert "unsupported_number" in problems(run(md, GOOD_MAP))


# --------------------------------------------------------------------------------------------- fact references


def test_bullet_without_fact_reference_is_rejected():
    errors = run(GOOD, GOOD_MAP[1:])
    assert "missing_fact_ref" in problems(errors)
    assert any(e["bullet_index"] == 0 for e in errors)


def test_unknown_fact_is_rejected():
    fm = [{"bullet_index": 0, "fact_id": "ach-invented"}, *GOOD_MAP[1:]]
    assert "unknown_fact" in problems(run(GOOD, fm))


def test_fact_outside_candidate_set_is_rejected():
    md = resume(["Mentored 12 engineers.", "Owned a $2.5M integration platform budget across 3 teams."])
    fm = [{"bullet_index": 0, "fact_id": "ach-outside"}, *GOOD_MAP[1:]]
    assert "fact_not_in_candidates" in problems(run(md, fm))


def test_fact_map_pointing_at_missing_bullet_is_rejected():
    fm = [*GOOD_MAP, {"bullet_index": 9, "fact_id": "ach-budget"}]
    assert "orphan_fact_map_entry" in problems(run(GOOD, fm))


def test_summary_numbers_must_appear_in_candidate_facts():
    md = resume(["Migrated 120 partner integrations to an API gateway, cutting onboarding time 60%.",
                 "Owned a $2.5M integration platform budget across 3 teams."],
                summary="Integration architect with 30 years of experience.")
    errors = run(md, GOOD_MAP)
    assert "unsupported_number_in_summary" in problems(errors)


# --------------------------------------------------------------------------------------------- hand edits


def test_hand_edit_annotations_round_trip_and_are_validated_identically():
    annotated = v.annotate_resume(GOOD, GOOD_MAP)
    assert "{fact:ach-gateway}" in annotated
    clean, fm = v.split_resume_annotations(annotated)
    assert clean == GOOD and fm == GOOD_MAP
    tampered = annotated.replace("cutting onboarding time 60%", "cutting onboarding time 90%")
    clean, fm = v.split_resume_annotations(tampered)
    errors = run(clean, fm)
    assert problems(errors) == {"unsupported_number"} and "90%" in errors[0]["detail"]


def test_hand_edit_without_annotation_is_missing_fact_ref():
    edited = v.annotate_resume(GOOD, GOOD_MAP).replace(" {fact:ach-budget}", "")
    clean, fm = v.split_resume_annotations(edited)
    assert "missing_fact_ref" in problems(run(clean, fm))


# --------------------------------------------------------------------------------------------- ATS compliance


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (lambda md: md.replace("## Skills", "## Core Competencies ⭐"), "nonstandard_section_heading"),
        (lambda md: "# Jane Doe\n" + md, "contains_name_header"),
        (lambda md: md.replace("## Skills\n", "## Skills\n| a | b |\n|---|---|\n"), "table"),
        (lambda md: md + "\n![logo](logo.png)", "image"),
        (lambda md: md + "\n<div>hi</div>", "html"),
        (lambda md: md.replace("Integration: MuleSoft", "Integration ✔ MuleSoft"), "special_character"),
        (lambda md: md.replace("- Owned a", "- **Owned** a"), "inline_formatting"),
        (lambda md: md.replace("| Mar 2019 – Present", "| since 2019"), "role_heading_format"),
        (lambda md: md.replace("## Summary", "## Experience\n### A | B | 2001 – 2002\n## Summary"), "section_order"),
    ],
)
def test_ats_violations_are_rejected(mutate, problem):
    assert problem in problems(run(mutate(GOOD), GOOD_MAP))


def test_role_and_education_headings_must_match_facts():
    invented_employer = GOOD.replace("Beta Health | Jan 2014", "Gamma Bank | Jan 2014")
    assert "unknown_role" in problems(run(invented_employer, GOOD_MAP))
    stretched_dates = GOOD.replace("Jan 2014 – Feb 2019", "Jan 2011 – Feb 2019")
    assert "role_dates_mismatch" in problems(run(stretched_dates, GOOD_MAP))
    invented_degree = GOOD.replace("Rutgers University | 1999", "Princeton University | 1999")
    assert "unknown_education" in problems(run(invented_degree, GOOD_MAP))


def test_ats_reverse_chronological_order():
    md = GOOD.replace("### Lead Integration Architect | Acme Insurance | Mar 2019 – Present",
                      "### Lead Integration Architect | Acme Insurance | Mar 2009 – Feb 2012")
    assert "not_reverse_chronological" in problems(run(md, GOOD_MAP))


def test_ats_bullet_length_and_count():
    long_bullet = "Migrated 120 partner integrations to an API gateway " + "and more " * 30
    md = resume([long_bullet, "Owned a $2.5M integration platform budget across 3 teams."])
    assert "bullet_too_long" in problems(run(md, GOOD_MAP))
    many = resume(["Owned a $2.5M integration platform budget across 3 teams."] * 6)
    fm = [{"bullet_index": i, "fact_id": "ach-budget"} for i in range(6)] + [{"bullet_index": 6, "fact_id": "ach-mule"}]
    assert "too_many_bullets" in problems(run(many, fm))


def test_keyword_coverage_report():
    report = v.keyword_coverage(GOOD, [{"text": "MuleSoft", "kind": "must", "keywords": ["mulesoft", "kafka"]},
                                       {"text": "APIs", "kind": "nice", "keywords": ["api gateway"]}])
    assert report["matched"] == ["api gateway", "mulesoft"]
    assert report["missing_must"] == ["kafka"]
    assert 0 < report["percent"] < 100


# --------------------------------------------------------------------------------------------- cover letter


COVER = """Dear Hiring Manager,

I am applying for the Integration Architect role.

At Acme I migrated 120 partner integrations to an API gateway, cutting onboarding time 60%.

I would welcome the chance to discuss the role.

Sincerely,"""


def test_cover_letter_valid_and_fabrications_rejected():
    fm = [{"paragraph_index": 1, "fact_id": "ach-gateway"}]
    assert v.validate_cover(COVER, fm, FACTS, CANDIDATES, company_notes=None) == []
    bad = COVER.replace("60%", "80%")
    assert "unsupported_number" in problems(v.validate_cover(bad, fm, FACTS, CANDIDATES, company_notes=None))
    assert "missing_fact_ref" in problems(v.validate_cover(COVER, [], FACTS, CANDIDATES, company_notes=None))


def test_cover_letter_company_numbers_may_come_from_company_notes():
    md = COVER.replace("I am applying for the Integration Architect role.",
                       "Your team serves 4 million members.")
    fm = [{"paragraph_index": 1, "fact_id": "ach-gateway"}]
    assert "unsupported_number" in problems(v.validate_cover(md, fm, FACTS, CANDIDATES, company_notes=None))
    assert v.validate_cover(md, fm, FACTS, CANDIDATES, company_notes="Serves 4 million members.") == []


def test_cover_annotations_round_trip():
    fm = [{"paragraph_index": 1, "fact_id": "ach-gateway"}]
    clean, parsed = v.split_cover_annotations(v.annotate_cover(COVER, fm))
    assert clean == COVER and parsed == fm
