"""Job scoring (SPEC 6.6): prompt assembly, one structured LLM call, and code-enforced post-processing."""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Mapping, Sequence

from jobhunt.config import AppConfig
from jobhunt.db.engine import utcnow_iso
from jobhunt.db.queries import SUBSCORES, WEIGHT_KEYS
from jobhunt.discovery import focus as focus_mod
from jobhunt.llm.client import LLMClient, load_prompt
from jobhunt.llm.schemas import Competency, JobScore, JobScoreV5, JobScoreV8, RoleAnalysis, StackItem
from jobhunt.sources.normalize import normalize_ws_for_match

log = logging.getLogger(__name__)

EVIDENCE_CAP = 69

# Rubric v5 turns the model's analysis into two subscores in code, so they rest on things that can be checked.
# role_fit by the kind of job and how hands-on it is. PROPOSED values: yours to tune.
# Integration, technical and delivery leads rank highest, hands-on ones too; people managers just below; hands-on
# engineering is acceptable but not preferred.
ROLE_FIT: dict[str, dict[str, int]] = {
    "technical_lead": {"low": 95, "medium": 95, "high": 85},
    "delivery_lead": {"low": 95, "medium": 92, "high": 82},
    "people_manager": {"low": 88, "medium": 88, "high": 78},
    "architect": {"low": 75, "medium": 75, "high": 70},
    "individual_contributor": {"low": 50, "medium": 50, "high": 45},
    "other": {"low": 20, "medium": 20, "high": 20},
}
# skill_match is the weighted share of the posting's stack the candidate has: a required technology counts twice a
# preferred one, and one used only in older roles counts 0.8.
STACK_WEIGHT = {"required": 2.0, "preferred": 1.0}
STACK_CREDIT = {"recent": 1.0, "earlier": 0.8, "no": 0.0}
MIN_STACK_ITEMS = 3  # fewer checked technologies than this is too little to score on; the model's number stands
# Below this many, the list score is blended with the model's own skill estimate (3 items: 60% list, 40% model):
# a small model sometimes lists three vague items, and three matches shouldn't make a 100.
FULL_STACK_ITEMS = 5
# Categories, not technologies. A posting's "cloud technologies" says nothing checkable, and "Cloud" would otherwise
# pick up a rating for "Cloud Migration". Dropped from the stack; blocked from matching a longer rated skill.
GENERIC_STACK = {
    "cloud", "cloud technologies", "cloud technology", "cloud platform", "cloud platforms", "cloud computing",
    "cloud services", "cloud infrastructure", "automation", "programming", "programming languages", "scripting",
    "software development", "software engineering", "development", "engineering", "databases", "database",
    "technology", "technologies", "tools", "tooling", "systems", "infrastructure", "integration", "integrations",
    "security", "data", "design", "architecture", "testing", "analytics", "ai", "ai/ml", "operating systems",
}
# Leadership and delivery words: an item made of these is a competency, not a technology ("Technical Program
# Management", "Stakeholder Management"). Rubric v8 scores competencies on their own list, by evidence rather than by
# name, and keeps them out of the technology stack.
COMPETENCY_WORDS = {
    "management", "manager", "leadership", "leading", "lead", "stakeholder", "stakeholders", "communication",
    "communications", "delivery", "planning", "strategy", "strategic", "collaboration", "cross-functional",
    "mentoring", "mentorship", "coaching", "governance", "program", "programs", "project", "projects", "execution",
    "prioritization", "negotiation", "presentation", "organizational", "problem-solving", "analytical", "lifecycle",
    "roadmap", "roadmaps", "influence", "change", "risk", "budget", "budgeting", "portfolio", "vendor", "vendors",
    "capabilities", "skills", "ability", "experience", "team", "teams", "people", "hiring", "partnership",
}
# How much of skill_match comes from competencies (the rest from technologies), by the kind of job: a delivery or
# people-leadership role is mostly judged on leading delivery; a hands-on one on its stack. PROPOSED values.
COMPETENCY_SHARE = {"people_manager": 0.7, "delivery_lead": 0.7, "technical_lead": 0.5, "architect": 0.3,
                    "individual_contributor": 0.2, "other": 0.5}
MIN_COMPETENCIES = 2

# An unrated skill the background shows is taken as this level, then compared with the depth the posting needs: at
# working level or below it earns the old credit (recent 1.0, earlier 0.8); an expert requirement earns 6/9 or 4/9.
UNRATED_LEVEL = {"recent": 6, "earlier": 4}
# A self-rated skill (Fact bank → Skill levels, 1-10) replaces the recent/earlier credit: it earns its rating over
# the level the posting needs, up to full credit. AWS rated 5 against an expert requirement earns 5/9.
REQUIRED_LEVEL = {"basic": 3, "working": 5, "advanced": 7, "expert": 9}


def _bullets(items: Sequence[str]) -> str:
    return "\n".join(f"- {x}" for x in items) or "- (none)"


def _sort_date(value: str | None) -> str:
    if not value or str(value).lower() == "present":
        return "9999-99"
    s = str(value)
    return s if len(s) >= 7 else f"{s}-00"


def _span(fact: Mapping) -> str:
    end = fact.get("end_date") or "present"
    return f"{fact.get('start_date') or '?'} to {end}"


def format_background(facts: Sequence[Mapping], cfg: AppConfig) -> dict[str, str]:
    """Candidate background for the scoring prompt, most recent first. Pure function over fact rows."""
    wanted = set(cfg.profile.summary_fact_ids)
    summaries = [f for f in facts if f.get("kind") == "summary"]
    chosen = [f for f in summaries if f["id"] in wanted] or summaries
    roles = sorted((f for f in facts if f.get("kind") == "role"),
                   key=lambda f: (_sort_date(f.get("end_date")), _sort_date(f.get("start_date"))), reverse=True)
    achievements: dict[str, list[Mapping]] = {}
    for f in facts:
        if f.get("kind") == "achievement" and f.get("parent_id"):
            achievements.setdefault(f["parent_id"], []).append(f)

    detail = max(0, cfg.scoring.recent_roles_in_detail)
    full_earlier = getattr(cfg.scoring, "earlier_roles", "full") == "full"
    recent_blocks, earlier, technologies = [], [], []
    for i, role in enumerate(roles):
        header = f"{role.get('title')} — {role.get('org')} ({_span(role)})"
        role_achievements = achievements.get(role["id"], [])
        # the role's own tags count too (e.g. oauth on a role whose bullets never say it)
        tags = Counter(t.strip() for a in [role, *role_achievements] for t in (a.get("tags") or []) if t and t.strip())
        if tags:
            top = sorted(tags, key=lambda t: (-tags[t], t.lower()))[:40]
            technologies.append(f"- {role.get('org')} ({_span(role)}): {', '.join(top)}")
        if i < detail or full_earlier:
            label = ("MOST RECENT ROLE (weigh most heavily)" if i == 0 else "PREVIOUS ROLE") if i < detail \
                else "EARLIER ROLE"
            lines = [f"### {label}: {header}"]
            if role.get("text"):
                lines.append(str(role["text"]))
            for a in role_achievements[: cfg.scoring.max_achievements_per_role]:
                lines.append(f"- {a['text']}")
            (recent_blocks if i < detail else earlier).append("\n".join(lines))
        else:
            earlier.append(f"- {header}")

    def listing(kind: str, field: str = "text") -> str:
        items = [str(f.get(field) or f.get("text")) for f in facts if f.get("kind") == kind]
        return "\n".join(f"- {x}" for x in items) or "- (none)"

    return {
        "summary": "\n".join(str(f["text"]) for f in chosen) or "(no summary facts)",
        "recent": "\n\n".join(recent_blocks) or "(no roles in the fact bank)",
        "earlier": ("\n\n" if full_earlier else "\n").join(earlier) or "- (none)",
        "technologies": "\n".join(technologies) or "- (none)",
        "certifications": listing("cert", "title"),
        "skills": listing("skill"),
        "education": listing("education"),
    }


def target_title_terms(patterns: Sequence[str]) -> str:
    """The title allowlist's alternatives as plain words: r"\b(architect|lead|head of)\b" -> "architect, lead,
    head of"."""
    terms: list[str] = []
    for p in patterns:
        body = re.sub(r"\\b|[()^$]|\(\?:", "", p)
        terms += [t.replace("\\", "").strip() for t in body.split("|") if t.strip()]
    return ", ".join(dict.fromkeys(terms)) or "(none)"


def build_system_prompt(cfg: AppConfig, background: Mapping[str, str], calibration: Sequence[dict],
                        truncated: bool) -> str:
    p = cfg.profile
    cal = ""
    if calibration:
        lines = ["", "## Calibration: the candidate's past decisions (learn from these)"]
        for ex in calibration:
            lines.append(f"- {ex['action'].upper()} (scored {ex['total_at_decision']:.0f}): "
                         f"{ex['title']} at {ex['company_name_raw']} — reason: {ex.get('reason') or 'none given'}")
        cal = "\n".join(lines)
    note = ""  # per-job notes go in the user message (user_message), so this prompt is the same for every job
    return load_prompt("score_job", cfg.scoring.rubric_version).safe_substitute(
        headline=p.headline, years=p.years_experience or "unspecified", location_base=p.locations.base,
        location_accept=", ".join(p.locations.accept), seniority=", ".join(p.seniority_band),
        comp_floor=f"{p.comp_floor:,}" if p.comp_floor else "unspecified",
        employment_types=", ".join(p.employment_types), summary=background.get("summary", ""),
        recent=background.get("recent", ""), earlier=background.get("earlier", ""),
        certifications=background.get("certifications", ""), skills=background.get("skills", ""),
        education=background.get("education", ""), technologies=background.get("technologies", ""),
        target_roles=", ".join(p.target_roles) or "(not specified)", role_preference=p.role_preference or "(none)",
        must_haves=_bullets(cfg.must_haves), nice_to_haves=_bullets(cfg.nice_to_haves),
        dealbreakers=_bullets(cfg.dealbreakers), truncation_note=note, calibration=cal,
        focus_boost=_bullets(cfg.focus.boost), focus_avoid=_bullets(cfg.focus.avoid),
        skill_levels=background.get("skill_levels", "- (none rated)"),
        target_titles=target_title_terms(cfg.filters.title_allowlist),
    )


def user_message(job: dict, truncated: bool) -> str:
    """The per-job part of a scoring request. Everything that varies by job is here, after the cached system
    prompt."""
    note = ("Note: this posting's description is a truncated snippet. Be conservative and prefer lower "
            "confidence.\n\n" if truncated else "")
    return note + posting_text(job)


def posting_text(job: dict) -> str:
    def money(v: int | None) -> str:
        return f"${v:,}" if v else "?"

    comp = "not posted"
    if job.get("comp_min") or job.get("comp_max"):
        comp = f"{money(job.get('comp_min'))} - {money(job.get('comp_max'))} ({job.get('comp_source')})"
    return (
        f"Title: {job['title']}\nCompany: {job['company_name_raw']}\nLocation: {job.get('location_raw') or 'unknown'}\n"
        f"Remote type: {job.get('remote_type')}\nEmployment type: {job.get('employment_type')}\n"
        f"Compensation: {comp}\nPosted: {job.get('posted_at') or 'unknown'}\n\n"
        f"Description:\n{job.get('description_text') or ''}"
    )


def compute_total(subscores: dict, weights: dict[str, float], ghost: int, ghost_penalty: float,
                  cap: float | None = None) -> float:
    # Subscores missing from a row (e.g. role_fit on scores made before rubric v2) are left out and the
    # remaining weights renormalized, rather than counted as zero.
    present = [k for k in WEIGHT_KEYS if subscores.get(k) is not None]
    wsum = sum(max(0.0, float(weights.get(k, 0))) for k in present)
    if wsum <= 0:
        return 0.0
    base = sum(max(0.0, float(weights.get(k, 0))) * float(subscores[k]) for k in present) / wsum
    total = base * (1.0 - ghost_penalty * (ghost or 0) / 100.0)
    if cap is not None:
        total = min(total, float(cap))
    return round(total, 1)


_INSURER_NAME = re.compile(r"insur|assuran|reinsur|casualty|underwrit|annuit", re.IGNORECASE)


def _slug(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def industry_of(company_name: str | None, cfg: AppConfig) -> str | None:
    """The employer's industry from `companies:` (the longest configured name the job's company starts with, so
    "JPMorgan Chase Bank, N.A." is "JPMorgan Chase"); else "insurance" when the name says so; else None."""
    slug = _slug(company_name)
    best = ""
    industry = None
    named = [(c.name, c.industry) for c in cfg.companies] + list(cfg.scoring.industry_by_employer.items())
    for name, ind in named:
        cs = _slug(name)
        if cs and ind and (slug == cs or slug.startswith(cs + "-")) and len(cs) > len(best):
            best, industry = cs, ind
    if industry is None and _INSURER_NAME.search(company_name or ""):
        industry = "insurance"
    return industry


def industry_fit(company_name: str | None, cfg: AppConfig) -> int:
    scores = cfg.scoring.industry_scores
    industry = industry_of(company_name, cfg)
    if industry is None:
        return int(scores.get("unknown", 50))
    return int(scores.get(industry, scores.get("other", 40)))


def ideal_fit(job_embedding: bytes | None, ideal, cfg: AppConfig) -> int | None:  # noqa: ANN001
    """How close the posting is to the ideal job: cosine of the two (normalized) embeddings, mapped linearly from
    `low` (0) to `high` (100). None when either side is missing."""
    from jobhunt.db import vectors

    vec = vectors.from_blob(job_embedding) if job_embedding is not None else None
    if vec is None or ideal is None:
        return None
    c = cfg.scoring.ideal_job
    cos = float(vec @ ideal)
    return int(round(100 * min(1.0, max(0.0, (cos - c.low) / max(1e-6, c.high - c.low)))))


# Code checks on the model's role classification, from the title and the verified stack. A small model calls
# "VP of Demand Generation" a delivery lead and "Software Engineer (All Levels)" a technical lead.
_TECH_TITLE = re.compile(
    r"\b(technical|technology|technologies|tech|engineer\w*|software|developer|development|dev|data|cloud|platform|"
    r"architect\w*|integration\w*|api|apis|systems?|infrastructure|devops|sre|security|cyber\w*|digital|"
    r"applications?|automation|mulesoft|java|\.net|aws|ai|ml|analytics|erp|sap|salesforce|it)\b", re.IGNORECASE)
_LEAD_TITLE = re.compile(
    r"\b(lead|leader|leads|manager|managing|management|head|director|vp|vice president|chief|principal|"
    r"supervisor|owner)\b", re.IGNORECASE)
LEADERSHIP_TYPES = {"technical_lead", "delivery_lead", "people_manager"}
MIN_TECH_STACK = 5         # a posting naming this many verified technologies is a technology job whatever its title
NON_TECH_ROLE_FIT = 25     # role_fit ceiling for a job that isn't technology work


# Business functions that aren't technology work, unless the title is also plainly technical
_NON_TECH_TITLE = re.compile(
    r"\b(account manager|account executive|account director|sales|marketing|demand generation|customer success|"
    r"client advisor|business partner|recruit\w*|talent|legal|counsel|paralegal|finance|financial analyst|"
    r"accounting|communications|public relations|brand|merchandis\w*|procurement|payroll)\b", re.IGNORECASE)
_STRONG_TECH_TITLE = re.compile(
    r"\b(engineer\w*|software|developer|architect\w*|technical|technology|integration\w*|platform|devops|cloud|"
    r"infrastructure|api|apis|data engineer\w*)\b", re.IGNORECASE)


def is_tech_job(title: str | None, stack: Sequence[Mapping] | None) -> bool:
    title = title or ""
    if _NON_TECH_TITLE.search(title) and not _STRONG_TECH_TITLE.search(title):
        return False
    return bool(_TECH_TITLE.search(title)) or len(stack or []) >= MIN_TECH_STACK


def role_fit_from(role: Mapping | None, title: str | None = None, stack: Sequence[Mapping] | None = None
                  ) -> int | None:
    """role_fit from a role classification under the current ROLE_FIT table, checked against the title: a job
    that isn't technology work is capped low, and a leadership type needs a leadership word in the title (otherwise
    it is scored as the hands-on role the title describes)."""
    if not role or role.get("role_type") not in ROLE_FIT or role.get("hands_on") not in ("low", "medium", "high"):
        return None
    role_type, hands_on = role["role_type"], role["hands_on"]
    if title is not None and role_type in LEADERSHIP_TYPES and not _LEAD_TITLE.search(title):
        # no leadership word in the title: an architect title is an architect role, anything else hands-on
        role_type = "architect" if re.search(r"\barchitect", title, re.IGNORECASE) else "individual_contributor"
    value = ROLE_FIT[role_type][hands_on]
    if title is not None and not is_tech_job(title, stack):
        value = min(value, NON_TECH_ROLE_FIT)
    return value if role.get("verified") else min(value, EVIDENCE_CAP)


def focus_for(job: Mapping, cfg: AppConfig) -> tuple[int, dict, float | None]:
    fit, hits = focus_mod.evaluate(job.get("title"), job.get("description_text"), cfg.focus.boost, cfg.focus.avoid)
    return fit, hits, (cfg.focus.avoid_cap if hits["avoid"] else None)


def background_text(background: Mapping[str, str]) -> str:
    return normalize_ws_for_match("\n".join(str(v) for v in background.values()))


def _in_background(quote: str, haystack: str) -> bool:
    """Is this quote in the text, exactly or nearly (90% partial match for 8+ characters)? Used for both the
    candidate's background and the posting."""
    from rapidfuzz import fuzz

    q = normalize_ws_for_match(quote)
    if len(q) < 3:
        return False
    return q in haystack or (len(q) >= 8 and fuzz.partial_ratio(q, haystack) >= 90)


def _names(haystack_lower: str, term: str, min_len: int = 1) -> bool:
    """Does the text name this term as a whole word? Handles "C#", ".NET", "Node.js", "CI/CD"."""
    t = normalize_ws_for_match(term).lower()
    if len(t) < min_len:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", haystack_lower) is not None


def name_variants(tech: str) -> list[str]:
    """Ways a posting's technology may be named elsewhere: "OpenID Connect (OIDC)" -> itself, "OpenID Connect" and
    "OIDC"; "OAuth 2.0" -> itself and "OAuth"; "Java 17" -> "Java"."""
    t = normalize_ws_for_match(tech)
    out = [t]
    if m := re.fullmatch(r"(.+?)\s*\(([^)]+)\)", t):
        out += [m.group(1).strip(), m.group(2).strip()]
    for v in list(out):
        bare = re.sub(r"\s+v?\d+(\.\d+)*$", "", v).strip()
        if bare and bare != v:
            out.append(bare)
    return list(dict.fromkeys(x for x in out if x))


def _background_names(background: str, tech: str) -> bool:
    """A short all-capitals name (CAN, ROS, SQL) must match with its capitals, or the word "can" would be CAN."""
    for t in name_variants(tech):
        if len(t) <= 4 and t.isupper():
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])", background):
                return True
        elif _names(background.lower(), t, min_len=3):
            return True
    return False


# Words too general to show that a quote is about a particular technology.
GENERIC_WORDS = {"and", "the", "for", "with", "tools", "tool", "patterns", "pattern", "experience", "systems",
                 "system", "platform", "platforms", "services", "service", "development", "based", "using", "modern",
                 "practices", "frameworks", "framework", "technologies", "technology", "solutions", "design"}


def _significant_words(tech: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9#+./-]+", tech.lower()) if len(w) >= 3 and w not in GENERIC_WORDS]


def _background_covers(background: str, tech: str) -> bool:
    """A multi-word concept ("Cloud Infrastructure", "Distributed Systems") whose every meaningful word the
    background uses, though not as that exact phrase. Worth partial credit, not full."""
    words = _significant_words(tech)
    return len(words) >= 2 and all(_names(background.lower(), w) for w in words)


def _quote_names(quote: str, tech: str) -> bool:
    """Does the candidate quote mention the technology itself? A real sentence about something else doesn't count."""
    q = quote.lower()
    return any(_names(q, w) for w in _significant_words(tech))


def level_for(tech: str, levels: Mapping[str, int]) -> int | None:
    """The owner's rating that covers this technology: an exact name, else the longest rated skill named inside it
    ("AWS" for "AWS Lambda") or naming it ("MuleSoft Anypoint Platform" for "MuleSoft")."""
    variants = [v.lower() for v in name_variants(tech)]
    for t in variants:
        if t in levels:
            return levels[t]
    # a rated skill named inside the technology ("AWS" in "AWS Lambda"); or, for a specific technology only, a longer
    # rated skill naming it ("MuleSoft" in "MuleSoft Anypoint Platform", but not "Cloud" in "Cloud Migration")
    hits = [k for k in levels if len(k) >= 2 and any(
        _names(t, k) or (t not in GENERIC_STACK and _names(k, t)) for t in variants)]
    return levels[max(hits, key=len)] if hits else None


def is_generic(tech: str) -> bool:
    return normalize_ws_for_match(tech).lower() in GENERIC_STACK


_COMPETENCY_FILLER = {"and", "of", "the", "for", "with", "technical", "technology", "business", "end-to-end",
                      "senior", "enterprise", "complex", "large-scale", "global", "ai", "effective", "strong"}


def is_competency(tech: str) -> bool:
    """Every word a leadership/delivery word (or filler like "and", "technical"): a competency, not a technology."""
    words = re.findall(r"[a-z][a-z-]*", normalize_ws_for_match(tech).lower())
    return bool(words) and any(w in COMPETENCY_WORDS for w in words) and all(
        w in COMPETENCY_WORDS or w in _COMPETENCY_FILLER for w in words)


def stack_credit(item: Mapping, levels: Mapping[str, int]) -> float:
    """Your level over the level the posting needs, up to 1. A rating wins; otherwise recent use counts as 6 and
    older use as 4 (UNRATED_LEVEL); not having it is 0."""
    level = level_for(item["technology"], levels)
    if level is None:
        level = UNRATED_LEVEL.get(item["has"], 0)
    return min(1.0, level / REQUIRED_LEVEL[item.get("level_required") or "working"])


def stack_skill_match(items: Sequence[Mapping], levels: Mapping[str, int],
                      model_estimate: int | None = None, *, drop_competencies: bool = True) -> int | None:
    """skill_match from a checked stack; None when it is too short to score on. A short list is blended with the
    model's own estimate when there is one. `drop_competencies=False` recomputes a pre-v8 score as it was made."""
    items = [k for k in items if not is_generic(k["technology"])
             and not (drop_competencies and is_competency(k["technology"]))]
    if len(items) < MIN_STACK_ITEMS:
        return None
    weight = sum(STACK_WEIGHT[k["importance"]] for k in items)
    got = sum(STACK_WEIGHT[k["importance"]] * stack_credit(k, levels) for k in items)
    score = 100 * got / weight
    if model_estimate is not None and len(items) < FULL_STACK_ITEMS:
        share = len(items) / FULL_STACK_ITEMS
        score = share * score + (1 - share) * model_estimate
    return round(score)


def check_stack(items: Sequence[StackItem], posting: str, background: str,
                levels: Mapping[str, int] | None = None,
                model_estimate: int | None = None) -> tuple[list[dict], int | None, int]:
    """Verify the model's technology checklist and compute skill_match from it.

    A technology the posting doesn't name is dropped. "Has it" must be backed by the background: either it names
    the technology, or the candidate quote is really in the background and mentions the technology (a small model
    will otherwise cite any true sentence). A "no" for something the background names becomes "earlier".
    Returns (checked items, skill_match or None when too few items, number dropped)."""
    levels = levels or {}
    posting_l = posting.lower()
    kept: list[dict] = []
    seen: set[str] = set()
    dropped = 0
    for it in items:
        tech = normalize_ws_for_match(it.technology)
        if not tech or tech.lower() in seen:
            continue
        if is_generic(tech) or is_competency(tech):
            dropped += 1
            log.info("stack item %r is a category or competency, not a technology; dropped", tech[:60])
            continue
        if not _names(posting_l, tech):
            dropped += 1
            log.info("stack item %r is not named in the posting; dropped", tech[:60])
            continue
        seen.add(tech.lower())
        named = _background_names(background, tech)
        quoted = (bool(it.candidate_quote.strip()) and _quote_names(it.candidate_quote, tech)
                  and _in_background(it.candidate_quote, background))
        has = it.candidate_has
        if has != "no" and not (named or quoted):
            has = "no"
        elif has == "no" and named:
            has = "earlier"
        if has == "no" and _background_covers(background, tech):
            has = "earlier"
        kept.append({"technology": tech, "importance": it.importance, "level_required": it.level_required,
                     "claimed": it.candidate_has, "has": has, "candidate_quote": it.candidate_quote if quoted else "",
                     "your_level": level_for(tech, levels)})
    return kept, stack_skill_match(kept, levels, model_estimate), dropped


def check_competencies(items: Sequence[Competency], posting: str, background: str) -> list[dict]:
    """Keep the competencies the posting really asks for; each counts as held only when its evidence is really in the
    candidate's background (the words needn't match the posting's: leading "150+ application migrations" is program
    delivery)."""
    posting_l = posting.lower()
    kept: list[dict] = []
    seen: set[str] = set()
    for it in items:
        name = normalize_ws_for_match(it.competency)
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        words = [w for w in re.findall(r"[a-z][a-z-]{2,}", name.lower()) if w not in {"and", "the", "with", "for"}]
        if not words or sum(w in posting_l for w in words) < max(1, len(words) - 1):
            log.info("competency %r is not in the posting; dropped", name[:60])
            continue
        evidence = it.candidate_evidence.strip()
        held = bool(evidence) and _in_background(evidence, background)
        kept.append({"competency": name, "importance": it.importance, "held": held,
                     "evidence": evidence if held else ""})
    return kept


def competency_match(items: Sequence[Mapping]) -> int | None:
    if len(items) < MIN_COMPETENCIES:
        return None
    weight = sum(STACK_WEIGHT[k["importance"]] for k in items)
    got = sum(STACK_WEIGHT[k["importance"]] * (1.0 if k["held"] else 0.0) for k in items)
    return round(100 * got / weight)


def analysis_skill_match(analysis: Mapping, levels: Mapping[str, int], title: str | None = None) -> int | None:
    """skill_match from a stored analysis: the technology stack and (rubric v8) the competencies, mixed by the kind
    of job. None when neither list is long enough to score on. Leadership competencies count only for technology
    jobs: "stakeholder management" fits a sales job as well as a delivery one."""
    if title is not None and not is_tech_job(title, analysis.get("stack")):
        analysis = {**analysis, "competencies": []}
    v8 = "competencies" in analysis  # scores made before v8 keep their stack as it was scored
    tech = stack_skill_match(analysis.get("stack") or [], levels, analysis.get("model_skill_match"),
                             drop_competencies=v8)
    comp = competency_match(analysis.get("competencies") or [])
    if comp is None:
        return tech
    if tech is None:  # no usable stack: the competencies, steadied by the model's own estimate
        model = analysis.get("model_skill_match")
        return round(0.7 * comp + 0.3 * model) if model is not None else comp
    share = COMPETENCY_SHARE.get(str((analysis.get("role") or {}).get("role_type") or "other"), 0.5)
    return round(share * comp + (1 - share) * tech)


def check_role(role: RoleAnalysis | None, posting: str) -> dict | None:
    """role_fit from the kind of job. Without a quote the posting really contains, it can't go above the cap."""
    if role is None:
        return None
    value = ROLE_FIT[role.role_type][role.hands_on]
    verified = _in_background(role.quote, posting)
    if not verified:
        value = min(value, EVIDENCE_CAP)
    return {"role_type": role.role_type, "hands_on": role.hands_on, "quote": role.quote if verified else "",
            "verified": verified, "role_fit": value}


def enforce(score: JobScore, job: dict, cfg: AppConfig, *, ghost: int, truncated: bool,
            model_label: str | None = None, background: str | None = None,
            levels: Mapping[str, int] | None = None, ideal=None) -> dict:  # noqa: ANN001
    """Code-level guarantees: evidence quotes are real substrings, unsupported highs are capped,
    and total is computed here, never by the model.

    `background` (normalized candidate background) turns on the rubric-v3 check: skill_match/domain_fit evidence only
    counts when its candidate_quote is found in the candidate's own background."""
    haystack = normalize_ws_for_match(posting_text(job))
    kept, dropped = [], []
    for ev in score.evidence:
        # near-verbatim counts: a small model often changes a word ("with" for "implementing") in a real quote
        (kept if _in_background(ev.quote, haystack) else dropped).append(ev)
    for ev in dropped:
        log.info("job %s: dropped fabricated evidence quote %r", job.get("id"), ev.quote[:120])
    low_conf = bool(truncated) or (len(score.evidence) > 0 and len(dropped) > len(score.evidence) / 2)

    unbacked = 0
    supporting = kept
    if background is not None:
        supporting = []
        for ev in kept:
            if ev.dimension in ("skill_match", "domain_fit") and not _in_background(ev.candidate_quote, background):
                unbacked += 1
                log.info("job %s: %s evidence has no candidate backing (%r)", job.get("id"), ev.dimension,
                         ev.candidate_quote[:80])
                continue
            supporting.append(ev)
    supported = {ev.dimension for ev in supporting}
    subs = {}
    for k in SUBSCORES:
        v = max(0, min(100, int(getattr(score, k))))
        if v >= 70 and k not in supported:
            v = EVIDENCE_CAP
        subs[k] = v

    analysis = None
    if background is not None and (score.stack or score.role is not None):
        model_skill = subs["skill_match"]  # the model's own estimate, after the evidence cap
        stack, stack_score, stack_dropped = check_stack(score.stack, haystack, background, levels, model_skill)
        role = check_role(score.role, haystack)
        if role is not None:
            role["role_fit"] = role_fit_from(role, job.get("title") or "", stack)
            subs["role_fit"] = role["role_fit"]
        analysis = {"stack": stack, "stack_dropped": stack_dropped, "skill_from_stack": stack_score is not None,
                    "model_skill_match": model_skill, "role": role}
        if isinstance(score, JobScoreV8):
            analysis["competencies"] = check_competencies(score.competencies, haystack, background)
        combined = analysis_skill_match(analysis, levels or {}, job.get("title") or "")
        if combined is not None:
            subs["skill_match"] = combined

    red_flags = []
    for rf in score.red_flags:
        q = normalize_ws_for_match(rf.quote)
        red_flags.append({"flag": rf.flag, "severity": rf.severity,
                          "quote": rf.quote if q and q in haystack else ""})
    fit, hits, cap = focus_for(job, cfg)
    industry = industry_fit(job.get("company_name_raw"), cfg)
    ideal_value = ideal_fit(job.get("embedding"), ideal, cfg)
    gaps = list(score.gaps)
    if hits["avoid"]:
        gaps.insert(0, f"Title matches an area to avoid ({', '.join(hits['avoid'])}); total capped at {cap:g}")
    if unbacked:
        gaps.append(f"{unbacked} skill/domain claim(s) not backed by your background were ignored")
    return {
        "scored_at": utcnow_iso(),
        "model": model_label or cfg.scoring.model,
        "rubric_version": cfg.scoring.rubric_version,
        "total": compute_total({**subs, "focus_fit": fit, "industry_fit": industry, "ideal_fit": ideal_value},
                               cfg.scoring.weights, ghost,
                               cfg.scoring.ghost_penalty,
                               cap),
        **subs,
        "focus_fit": fit,
        "industry_fit": industry,
        "ideal_fit": ideal_value,
        "focus_hits": hits,
        "red_flags": red_flags,
        "gaps": gaps,
        "evidence": [ev.model_dump() for ev in kept],
        "rationale": score.rationale.strip(),
        "ghost_score": int(ghost),
        "low_confidence": 1 if low_conf else 0,
        "dropped_evidence": len(dropped),
        "analysis": analysis,
    }


def score_job(job: dict, cfg: AppConfig, llm: LLMClient, *, ghost: int, background: Mapping[str, str],
              calibration: Sequence[dict], truncated: bool, levels: Mapping[str, int] | None = None,
              ideal=None) -> dict:  # noqa: ANN001
    """`levels`: the owner's 1-10 skill ratings (lowercase skill -> level); they weigh the checked stack."""
    system = build_system_prompt(cfg, background, calibration, truncated)
    version = cfg.scoring.rubric_version
    schema = (JobScore if version in ("v1", "v2", "v3", "v4") else JobScoreV5 if version in ("v5", "v6", "v7")
              else JobScoreV8)
    parsed = llm.structured(schema, system=system, user=user_message(job, truncated), purpose="score",
                            model=cfg.scoring.model, prompt_version=f"score_job.{cfg.scoring.rubric_version}",
                            job_id=job["id"], max_tokens=8192, cache_system=True)
    label = llm.model_label("score") if hasattr(llm, "model_label") else cfg.scoring.model
    check = background_text(background) if cfg.scoring.rubric_version not in ("v1", "v2") else None
    return enforce(parsed, job, cfg, ghost=ghost, truncated=truncated, model_label=label, background=check,
                   levels=levels, ideal=ideal)


def explain(job: dict, cfg: AppConfig, *, ghost: int, background: Mapping[str, str], calibration: Sequence[dict],
            truncated: bool) -> str:
    system = build_system_prompt(cfg, background, calibration, truncated)
    return (f"===== SYSTEM PROMPT (cached: the same for every job) =====\n{system}\n\n"
            f"===== USER MESSAGE (this job) =====\n{user_message(job, truncated)}\n")
