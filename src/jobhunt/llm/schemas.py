"""Pydantic models for structured LLM outputs. Kept simple (no numeric constraints) so they convert cleanly
to Claude's native JSON-schema structured output; ranges are clamped in code."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Dimension = Literal["skill_match", "role_fit", "domain_fit", "seniority_fit", "comp_fit", "logistics_fit"]


class Evidence(BaseModel):
    claim: str = Field(description="What this quote demonstrates about fit.")
    quote: str = Field(description="A short verbatim substring copied exactly from the posting.")
    candidate_quote: str = Field(default="", description="For skill_match/domain_fit: a short phrase copied verbatim "
                                 "from the candidate background proving the candidate has this. Otherwise empty.")
    # Addition to SPEC 6.6: which subscore the evidence supports, so the ">=70 needs evidence" cap is enforceable.
    dimension: Dimension


class RedFlag(BaseModel):
    flag: str
    severity: Literal["low", "medium", "high"]
    quote: str = Field(description="Verbatim substring of the posting, or empty string if not applicable.")


class StackItem(BaseModel):
    """Rubric v5: one technology the posting asks for, checked against the candidate's background."""

    technology: str = Field(description="A technology, platform, language, tool or practice named in the posting, "
                            "copied verbatim and kept short (e.g. 'MuleSoft', 'AWS', 'Kubernetes', 'CI/CD').")
    importance: Literal["required", "preferred"]
    level_required: Literal["basic", "working", "advanced", "expert"] = Field(
        description="How deep the posting needs this: basic (familiarity), working (uses it day to day), advanced "
                    "(designs and leads with it), expert (the authority on it: architect-level, deep or "
                    "certified-professional expertise, many years).")
    candidate_has: Literal["recent", "earlier", "no"] = Field(
        description="recent: shown in the candidate's two most recent roles; earlier: only in older roles, skills "
                    "or certifications; no: the background does not show it.")
    candidate_quote: str = Field(default="", description="A short phrase copied verbatim from the candidate "
                                 "background that shows it. Empty when candidate_has is no.")


class Competency(BaseModel):
    """Rubric v8: a leadership or delivery competency the posting asks for (not a technology)."""

    competency: str = Field(description="What the posting asks for, in a few words taken from the posting, e.g. "
                            "'technical program management', 'stakeholder management', 'leading engineering teams'.")
    importance: Literal["required", "preferred"]
    candidate_evidence: str = Field(default="", description="A short phrase copied verbatim from the candidate "
                                    "background that shows this competency (it need not use the same words). Empty "
                                    "when the background shows no such experience.")


class RoleAnalysis(BaseModel):
    """Rubric v5: what kind of job this is, from the posting's responsibilities (not its title alone)."""

    role_type: Literal["people_manager", "technical_lead", "delivery_lead", "architect", "individual_contributor",
                       "other"] = Field(
        description="people_manager: manages engineers (hiring, performance, growth); technical_lead: leads a team's "
                    "technical direction and delivery; delivery_lead: owns delivery of programs/projects (delivery, "
                    "program or project manager); architect: designs solutions without leading a team; "
                    "individual_contributor: mainly builds things personally; other: not a technology delivery job.")
    hands_on: Literal["low", "medium", "high"] = Field(
        description="How much of the job is personally writing code or building: low = mostly leading/directing, "
                    "high = mostly building.")
    quote: str = Field(description="A short verbatim substring of the posting that best shows the role type.")


class JobScore(BaseModel):
    # Rubric v5 analysis, first so the model works through the posting before it picks numbers. Absent on older
    # rubrics, where the model's own skill_match / role_fit stand.
    stack: list[StackItem] = Field(default_factory=list)
    role: RoleAnalysis | None = None
    competencies: list[Competency] = Field(default_factory=list)  # rubric v8
    skill_match: int = Field(description="0-100")
    role_fit: int = Field(description="0-100: leadership/delivery role vs hands-on individual contributor")
    domain_fit: int = Field(description="0-100")
    seniority_fit: int = Field(description="0-100")
    comp_fit: int = Field(description="0-100")
    logistics_fit: int = Field(description="0-100")
    red_flags: list[RedFlag]
    gaps: list[str]
    evidence: list[Evidence]
    rationale: str = Field(description="At most 3 sentences.")



class JobScoreV5(JobScore):
    """Rubric v5 makes the analysis required: a small model skips optional fields."""

    stack: list[StackItem]
    role: RoleAnalysis


class JobScoreV8(JobScoreV5):
    """Rubric v8 adds the posting's leadership and delivery competencies, also required."""

    competencies: list[Competency]

class PostingFields(BaseModel):
    """Basic facts pulled from a pasted posting (manual job import)."""

    title: str
    company: str
    location: str
    work_arrangement: Literal["remote", "hybrid", "onsite", "unknown"]
    salary_min: int = Field(description="Yearly USD; 0 if not stated")
    salary_max: int = Field(description="Yearly USD; 0 if not stated")


class Requirement(BaseModel):
    text: str
    kind: Literal["must", "nice"]
    keywords: list[str] = Field(description="2-6 lowercase ATS keywords/phrases taken from the posting.")


class RequirementList(BaseModel):
    requirements: list[Requirement]


class BulletFactRef(BaseModel):
    bullet_index: int = Field(description="0-based index of the bullet within the Experience section.")
    fact_id: str


class ResumeDraft(BaseModel):
    markdown: str
    fact_map: list[BulletFactRef]


class ParagraphFactRef(BaseModel):
    paragraph_index: int = Field(description="0-based index of the body paragraph.")
    fact_id: str


class CoverDraft(BaseModel):
    markdown: str
    fact_map: list[ParagraphFactRef]
