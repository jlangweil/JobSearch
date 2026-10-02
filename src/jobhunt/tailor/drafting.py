"""LLM-backed tailoring steps: requirement parsing, resume and cover-letter drafting."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence

from jobhunt.config import AppConfig
from jobhunt.discovery.scoring import posting_text
from jobhunt.llm.client import LLMClient, load_prompt
from jobhunt.llm.schemas import CoverDraft, RequirementList, ResumeDraft
from jobhunt.tailor.validate import SECTION_TITLES

PROMPT_VERSION = "v1"
REQUIREMENTS_PROMPT_VERSION = "v2"  # keywords must be skill phrases, not verbs (2026-09-18)
RESUME_PROMPT_VERSION = "v3"  # complete, keyword-tailored resumes; no "Key skills for this role" line (2026-09-18)


# Output budgets. Claude's reasoning counts against max_tokens too, so these are far above the visible output size.
# A complete resume (70+ bullets) used 16-20k tokens at medium effort on 2026-09-15; calls above 16k stream.
REQUIREMENTS_MAX_TOKENS = 8000
RESUME_MAX_TOKENS = 48000
COVER_MAX_TOKENS = 12000

def _fact_payload(facts: Sequence[Mapping]) -> str:
    keep = ("id", "kind", "parent_id", "org", "title", "start_date", "end_date", "text", "variants", "metrics", "tags",
            "relevance", "matches")
    return json.dumps([{k: f.get(k) for k in keep if f.get(k) not in (None, [], {})} for f in facts],
                      ensure_ascii=False, indent=1)


def _errors_block(errors: Sequence[Mapping]) -> str:
    if not errors:
        return ""
    lines = ["## Your previous draft was rejected by the validator. Fix every problem:"]
    for e in errors:
        where = f"bullet {e['bullet_index']}" if e.get("bullet_index", -1) >= 0 else "document"
        lines.append(f"- [{where}] {e['problem']}: {e['detail']}" + (f" — {e['bullet_text']!r}" if e.get("bullet_text")
                                                                     else ""))
    return "\n".join(lines)


def parse_requirements(job: Mapping, cfg: AppConfig, llm: LLMClient) -> list[dict]:
    system = load_prompt("parse_requirements", REQUIREMENTS_PROMPT_VERSION).safe_substitute()
    out = llm.structured(RequirementList, system=system, user=posting_text(dict(job)), purpose="requirements",
                         model=cfg.tailoring.model,
                         prompt_version=f"parse_requirements.{REQUIREMENTS_PROMPT_VERSION}",
                         job_id=job["id"], max_tokens=REQUIREMENTS_MAX_TOKENS)
    return [r.model_dump() for r in out.requirements][:20]


_YEARS = re.compile(r"\d+\s*\+?\s*(?:years?|yrs?)\b", re.IGNORECASE)
# Single words a requirement parser often emits that read as filler in a skills list. A phrase built around one
# ("end-to-end testing", "release cutover") is fine — only the bare word is dropped.
FILLER_KEYWORDS = {
    "accountability", "alignment", "analysis", "blockers", "collaboration", "communication", "coordination",
    "cutover", "delivery", "dependencies", "deployment", "deployments", "design", "development", "documentation",
    "escalation", "execution", "feedback", "governance", "implementation", "improvement", "initiatives",
    "innovation", "leadership", "maintenance", "management", "mentoring", "migration", "monitoring", "onboarding",
    "operations", "optimization", "ownership", "partnership", "performance", "planning", "prioritization",
    "quality", "reliability", "reporting", "requirements", "roadmap", "scalability", "security", "stakeholders",
    "strategy", "support", "testing", "training", "troubleshooting", "validate", "validation", "velocity",
}


def _is_skill_phrase(keyword: str, posting: str) -> bool:
    """Keep what belongs in a Skills section: multi-word phrases, and single words the posting itself writes as a
    name or acronym (API, Azure, MuleSoft, CI/CD). Drop bare filler like "validate" or "cutover"."""
    kw = keyword.strip()
    if len(kw) < 2 or _YEARS.search(kw):
        return False
    if len(kw.split()) > 1 or "/" in kw or "-" in kw:
        return True
    if kw.lower() not in FILLER_KEYWORDS:
        return True
    return bool(re.search(rf"(?<![A-Za-z0-9])(?:{re.escape(kw.upper())}|{re.escape(kw.capitalize())})"
                          rf"(?![a-z0-9])", posting))


def posting_keywords(job: Mapping, requirements: Sequence[Mapping], facts: Sequence[Mapping]) -> tuple[list, list]:
    """(supported, unsupported) posting keywords, without years-of-experience phrases, spelled as in the posting."""
    from jobhunt.tailor.validate import keyword_support

    text = f"{job.get('title') or ''}\n{job.get('description_text') or ''}"
    supported, unsupported = keyword_support(requirements, facts)

    def tidy(items: Sequence[Mapping]) -> list[dict]:
        out = []
        for e in items:
            if not _is_skill_phrase(e["keyword"], text):
                continue
            m = re.search(rf"(?<![A-Za-z0-9]){re.escape(e['keyword'])}(?![A-Za-z0-9])", text, re.IGNORECASE)
            out.append({**e, "keyword": m.group(0) if m else e["keyword"]})
        return out

    return tidy(supported), tidy(unsupported)


def _keywords_block(items: Sequence[Mapping]) -> str:
    return "\n".join(f"- {e['keyword']} ({e['kind']})" for e in items) or "- (none)"


def _previous_block(previous: tuple[str, Sequence[Mapping]] | None, errors: Sequence[Mapping]) -> str:
    if not previous or not errors or not previous[0]:
        return ""
    md, fact_map = previous
    return ("\n\n## Your previous draft\nReturn this draft with ONLY the problems above fixed (reword or remove the "
            "offending words, add any missing fact as a bullet in its role, fix the fact_map). Keep everything else "
            "exactly as it is.\n\n```markdown\n" + md + "\n```\n\nIts fact_map: "
            + json.dumps(list(fact_map), ensure_ascii=False))


def _checklist(facts: Sequence[Mapping]) -> str:
    roles = [f for f in facts if f.get("kind") == "role"]
    lines = []
    for r in roles:
        ids = [f["id"] for f in facts if f.get("kind") == "achievement" and f.get("parent_id") == r["id"]]
        listed = ", ".join(ids) if ids else f"(no achievements: one bullet from the role fact {r['id']})"
        lines.append(f"- {r.get('title')} | {r.get('org')}: {listed}")
    return "\n".join(lines)


def draft_resume(job: Mapping, cfg: AppConfig, llm: LLMClient, *, requirements: Sequence[Mapping],
                 candidate_facts: Sequence[Mapping], emphasis: str | None, revision: str | None,
                 errors: Sequence[Mapping], previous: tuple[str, Sequence[Mapping]] | None = None,
                 ats: Mapping | None = None) -> tuple[str, list[dict]]:
    """`previous` (the draft the validator just rejected) turns a retry into a repair: fix only the listed problems
    instead of drafting from scratch, so a long resume converges instead of trading one mistake for another."""
    t = cfg.tailoring
    supported, unsupported = posting_keywords(job, requirements, candidate_facts)
    if t.resume_mode == "complete" and not ats:
        coverage_rules = ("This is a COMPLETE resume: every role and every accomplishment fact below must appear. "
                          "Length is not a problem. Tailoring comes from order, emphasis and wording, never from "
                          "leaving experience out.")
        completeness = ("6. Every achievement fact must be used for exactly one bullet (the validator checks), unless "
                        "the candidate's revision instructions explicitly ask to leave something out. Tick them off "
                        "against this checklist (ids per role; order within a role is yours):\n"
                        + _checklist(candidate_facts))
    else:
        coverage_rules = ("This is a FOCUSED resume: use the accomplishments that best match the posting (highest "
                          "`relevance` first), and at least two per role where available.")
        completeness = ""
    limits = []
    if t.max_bullets_per_role:
        limits.append(f"At most {t.max_bullets_per_role} bullets per role.")
    limits.append(f"Each bullet at most {t.max_bullet_chars} characters." if t.max_bullet_chars else "")
    system = load_prompt("draft_resume", RESUME_PROMPT_VERSION).safe_substitute(
        sections=", ".join(f"'## {SECTION_TITLES[s]}'" for s in t.sections
                           if s in SECTION_TITLES and s != "projects"),
        coverage_rules=coverage_rules, completeness_rule=completeness, bullet_limits=" ".join(x for x in limits if x),
        supported_keywords=_keywords_block(supported), unsupported_keywords=_keywords_block(unsupported),
        facts=_fact_payload(candidate_facts), requirements=json.dumps(list(requirements), ensure_ascii=False, indent=1),
        emphasis=f"## Emphasis requested by the candidate\n{emphasis}" if emphasis else "",
        revision=f"## Revision instructions from the candidate\n{revision}" if revision else "",
        errors=_errors_block(errors) + _previous_block(previous, errors),
    )
    if ats:
        system += _ats_block(ats)
    if "projects" in t.sections and any(f.get("kind") == "project" for f in candidate_facts):
        system += ("\n\n## Projects\nA '## Projects' section is added after your draft automatically: do not write "
                   "one, and never turn a project into an Experience bullet. The project facts (kind: project) and "
                   "their technologies (tags) do count as the candidate's experience for the Summary and the Skills "
                   "lines, so use their keywords there where the posting asks for them.")
    user = f"Tailor the resume for this posting:\n\n{posting_text(dict(job))}"
    out = llm.structured(ResumeDraft, system=system, user=user, purpose="draft_resume", model=t.model,
                         prompt_version=f"draft_resume.{RESUME_PROMPT_VERSION}", job_id=job["id"],
                         max_tokens=RESUME_MAX_TOKENS)
    return out.markdown.strip(), [r.model_dump() for r in out.fact_map]


def _ats_block(ats: Mapping) -> str:
    limits = list(ats.get("role_bullet_limits") or [])
    per_role = ", ".join(f"role {i + 1}: {n}" for i, n in enumerate(limits)) + (" (and so on)" if limits else "")
    weak = ", ".join(sorted(ats.get("weak_openers") or []))
    return f"""

## ATS Super Match rules (applicant tracking systems first, then a recruiter's 10-second read)
1. Three pages at most: under {ats.get("max_words") or 1500} words in total. Use the room: a thin resume loses.
2. Bullets per role, newest role first, at most: {per_role}. Choose and order each role's bullets, best first, by:
   a. the candidate's major projects and programs (named platforms, migrations, modernizations, initiatives), each
      with the technology stack it used (e.g. "MuleSoft, AWS EKS, Kafka"), so the history of what was built and with
      what stays visible;
   b. the biggest wins: measurable results (a number, percentage, dollar amount, scale, team size);
   c. the posting's requirements and keywords.
   A bullet that does all three comes first. Drop only what is minor, repeated, or unrelated to the posting.
3. Put the posting's keywords everywhere the facts support them: the headline and summary, the Skills lines (posting
   keywords first), and the bullets (worked in naturally where a fact describes that work).
4. Start every bullet with a strong, accurate action verb (Led, Delivered, Designed, Directed, Built, Partnered with,
   Drove ...). Never start with: {weak}. When a fact says "helped" do X, write what the candidate actually did, e.g.
   "Partnered with ... to X" or "Co-led X", never overstating it as sole ownership.
5. Spell out an acronym the first time it appears, then use it: "Disaster Recovery (DR)", "Recovery Time Objective
   (RTO)". Use the posting's exact spelling for its keywords.
6. Skills: the posting's keywords that the facts support come first in each line, spelled as in the posting, including
   the ones in the "Additional Skills" fact.
7. Plain characters only: straight quotes and a plain hyphen, no curly quotes or long dashes."""


def draft_cover(job: Mapping, cfg: AppConfig, llm: LLMClient, *, candidate_facts: Sequence[Mapping],
                company_notes: str | None, emphasis: str | None, revision: str | None,
                errors: Sequence[Mapping]) -> tuple[str, list[dict]]:
    system = load_prompt("draft_cover", PROMPT_VERSION).safe_substitute(
        facts=_fact_payload(candidate_facts), posting=posting_text(dict(job))[:12000],
        company_notes=company_notes or "(none — make no company-specific claims)",
        emphasis=f"## Emphasis requested by the candidate\n{emphasis}" if emphasis else "",
        revision=f"## Revision instructions from the candidate\n{revision}" if revision else "",
        errors=_errors_block(errors),
    )
    out = llm.structured(CoverDraft, system=system, user="Write the cover letter.", purpose="draft_cover",
                         model=cfg.tailoring.model, prompt_version=f"draft_cover.{PROMPT_VERSION}", job_id=job["id"],
                         max_tokens=COVER_MAX_TOKENS)
    return out.markdown.strip(), [r.model_dump() for r in out.fact_map]
