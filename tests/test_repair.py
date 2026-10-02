"""Mechanical repairs of a resume draft that failed validation, so the common failures don't cost a redraft.

A repair only ever makes a draft more literal: a bullet goes back to its fact's own wording, roles are put in date
order, a skills line gets a label, a missing accomplishment is added in its fact's words. The repaired draft is
validated again like any other; nothing here can make an unsupported claim pass."""

from __future__ import annotations

from jobhunt.tailor import repair
from jobhunt.tailor import validate as v
from tests.test_validate import CANDIDATES, FACTS, GOOD_MAP, resume, run


def _fix(md: str, fm: list[dict], errors: list[dict]):  # noqa: ANN202
    return repair.repair_resume(md, fm, FACTS, CANDIDATES, errors, max_bullet_chars=220)


def test_a_bullet_with_an_added_posting_keyword_goes_back_to_its_facts_wording():
    md = resume(["Migrated 120 partner integrations to an API gateway, cutting onboarding time 60%, "
                 "strengthening security compliance.",
                 "Owned a $2.5M integration platform budget across 3 teams."])
    errors = run(md, GOOD_MAP, unsupported_terms=["security compliance"])
    assert {e["problem"] for e in errors} == {"unsupported_keyword"}
    fixed, fm, notes = _fix(md, GOOD_MAP, errors)
    assert FACTS["ach-gateway"]["text"] in fixed and "security compliance" not in fixed
    assert fm == GOOD_MAP
    assert run(fixed, fm, unsupported_terms=["security compliance"]) == []
    assert len(notes) == 1 and "ach-gateway" in notes[0]


def test_a_bullet_with_a_number_its_fact_lacks_goes_back_too():
    md = resume(["Migrated 150 partner integrations to an API gateway.",
                 "Owned a $2.5M integration platform budget across 3 teams."])
    errors = run(md, GOOD_MAP)
    assert {e["problem"] for e in errors} == {"unsupported_number"}
    fixed, fm, _ = _fix(md, GOOD_MAP, errors)
    assert run(fixed, fm) == []


def test_roles_out_of_order_are_sorted_newest_first_with_their_bullets():
    good = resume(["Migrated 120 partner integrations to an API gateway, cutting onboarding time 60%.",
                   "Owned a $2.5M integration platform budget across 3 teams."])
    lines = good.split("\n")
    acme = lines.index("### Lead Integration Architect | Acme Insurance | Mar 2019 – Present")
    beta = lines.index("### Senior Engineer | Beta Health | Jan 2014 – Feb 2019")
    edu = lines.index("## Education")
    swapped = "\n".join(lines[:acme] + lines[beta:edu] + lines[acme:beta] + lines[edu:])
    fm = [{"bullet_index": 0, "fact_id": "ach-mule"}, {"bullet_index": 1, "fact_id": "ach-gateway"},
          {"bullet_index": 2, "fact_id": "ach-budget"}]
    errors = run(swapped, fm)
    assert {e["problem"] for e in errors} == {"not_reverse_chronological"}
    fixed, fixed_map, notes = _fix(swapped, fm, errors)
    assert run(fixed, fixed_map) == []
    assert sorted(fixed_map, key=lambda e: e["bullet_index"]) == GOOD_MAP
    assert any("order" in n for n in notes)


def test_an_unlabelled_skills_line_gets_a_label():
    md = resume(["Migrated 120 partner integrations to an API gateway, cutting onboarding time 60%.",
                 "Owned a $2.5M integration platform budget across 3 teams."])
    md = md.replace("Integration: MuleSoft, REST, API gateways", "MuleSoft, REST\nIntegration: API gateways")
    errors = run(md, GOOD_MAP)
    assert {e["problem"] for e in errors} == {"skills_line_without_label"}
    fixed, fm, notes = _fix(md, GOOD_MAP, errors)
    assert "Core Competencies: MuleSoft, REST" in fixed
    assert run(fixed, fm) == []


def test_a_missing_accomplishment_is_added_under_its_role():
    md = resume(["Migrated 120 partner integrations to an API gateway, cutting onboarding time 60%."])
    fm = [{"bullet_index": 0, "fact_id": "ach-gateway"}, {"bullet_index": 1, "fact_id": "ach-mule"}]
    errors = run(md, fm, require_fact_ids=["ach-gateway", "ach-budget", "ach-mule"])
    assert {e["problem"] for e in errors} == {"omitted_fact"}
    fixed, fixed_map, _ = _fix(md, fm, errors)
    assert run(fixed, fixed_map, require_fact_ids=["ach-gateway", "ach-budget", "ach-mule"]) == []
    acme = fixed.split("### Senior Engineer")[0]
    assert FACTS["ach-budget"]["text"] in acme          # under Acme, where the fact belongs


def test_a_too_long_bullet_falls_back_to_its_fact_when_that_fits():
    long = "Migrated 120 partner integrations to an API gateway, cutting onboarding time 60%, " + "x" * 200
    md = resume([long, "Owned a $2.5M integration platform budget across 3 teams."])
    errors = run(md, GOOD_MAP)
    assert "bullet_too_long" in {e["problem"] for e in errors}
    fixed, fm, _ = _fix(md, GOOD_MAP, errors)
    assert run(fixed, fm) == []


def test_a_summary_sentence_claiming_an_unsupported_keyword_is_dropped():
    bullets = ["Migrated 120 partner integrations to an API gateway, cutting onboarding time 60%.",
               "Owned a $2.5M integration platform budget across 3 teams."]
    md = resume(bullets, summary="Integration architect with 25 years of experience in insurance and health. "
                                 "Known for deep security compliance expertise.")
    errors = run(md, GOOD_MAP, unsupported_terms=["security compliance"])
    assert {e["problem"] for e in errors} == {"unsupported_keyword"}
    fixed, fm, notes = _fix(md, GOOD_MAP, errors)
    assert "security compliance" not in fixed and "25 years of experience" in fixed
    assert run(fixed, fm, unsupported_terms=["security compliance"]) == [] and "summary" in notes[0]


def test_what_needs_judgment_is_left_for_the_model():
    """A one-sentence summary making the claim, or a bullet citing no fact, can't be fixed mechanically."""
    md = resume(["Migrated 120 partner integrations to an API gateway, cutting onboarding time 60%.",
                 "Owned a $2.5M integration platform budget across 3 teams."],
                summary="Integration architect with deep security compliance experience.")
    errors = run(md, GOOD_MAP, unsupported_terms=["security compliance"])
    assert {e["problem"] for e in errors} == {"unsupported_keyword"}
    assert _fix(md, GOOD_MAP, errors) == (md, GOOD_MAP, [])

    no_ref = [GOOD_MAP[0], GOOD_MAP[2]]
    errors = run(resume(["Migrated 120 partner integrations to an API gateway, cutting onboarding time 60%.",
                         "Owned budget."]), no_ref)
    assert "missing_fact_ref" in {e["problem"] for e in errors}
    fixed, fm, notes = _fix(resume(["Migrated 120 partner integrations to an API gateway, cutting onboarding "
                                    "time 60%.", "Owned budget."]), no_ref, errors)
    assert notes == [] and fm == no_ref


def test_repairs_never_create_errors_of_their_own():
    """Every fact's own text passes the checks that sent a bullet back to it."""
    for fid in ("ach-gateway", "ach-budget", "ach-mule"):
        assert v.unsupported_tokens(FACTS[fid]["text"], v.fact_tokens(FACTS[fid])) == []


# --------------------------------------------------------------------------------------------- ATS Super Match

ATS = {"role_bullet_limits": [1, 1], "max_words": 1000, "weak_openers": ["helped", "worked", "assisted"]}


def test_the_ats_rules_are_checked_only_in_ats_mode():
    md = resume(["Helped migrate 120 partner integrations to an API gateway, cutting onboarding time 60%.",
                 "Owned a $2.5M integration platform budget across 3 teams."]).replace("Mar 2019 – Present",
                                                                                       "Mar 2019 – Present")
    md = md.replace("Integration architect with", "Integration architect \u2013 with")
    assert {e["problem"] for e in run(md, GOOD_MAP)} == set()                         # automated: unchanged
    problems = {e["problem"] for e in run(md, GOOD_MAP, ats=ATS)}
    assert {"too_many_bullets_for_role", "weak_opener", "fancy_characters"} <= problems
    long = resume(["Migrated 120 partner integrations to an API gateway, cutting onboarding time 60%."],
                  summary="Integration architect with 25 years of experience. " + "Insurance and health. " * 400)
    assert "too_long" in {e["problem"] for e in run(long, GOOD_MAP[:1] + [{"bullet_index": 1, "fact_id": "ach-mule"}],
                                                     ats=ATS)}


def test_ats_repairs_trim_to_the_limits_and_use_plain_characters():
    md = resume(["Migrated 120 partner integrations to an API gateway, cutting onboarding time 60%.",
                 "Owned a $2.5M integration platform budget across 3 teams."],
                summary="Integration architect with 25 years of experience in insurance and health \u2013 really.")
    errors = run(md, GOOD_MAP, ats=ATS)
    fixed, fm, notes = repair.repair_resume(md, GOOD_MAP, FACTS, CANDIDATES, errors, max_bullet_chars=220, ats=ATS)
    assert "\u2013" not in fixed and "\u2013" in md
    assert [e["fact_id"] for e in sorted(fm, key=lambda e: e["bullet_index"])] == ["ach-gateway", "ach-mule"]
    assert run(fixed, fm, ats=ATS) == []
    assert any("plain characters" in n for n in notes) and any("trimmed 1" in n for n in notes)

