from __future__ import annotations

from typing import TypedDict


class ApplicationState(TypedDict, total=False):
    job_id: int
    emphasis: str | None
    requirements: list[dict]  # {text, kind: must|nice, keywords}
    candidate_facts: list[dict]
    resume_markdown: str | None
    resume_fact_map: list[dict]
    resume_document_id: int | None
    cover_markdown: str | None
    cover_fact_map: list[dict]
    cover_document_id: int | None
    coverage: list[dict]  # {requirement, covered_by, status}
    validation_errors: list[dict]
    draft_attempts: int
    company_notes: str | None
    rendered_paths: list[str]
    stage: str
    # Additions to SPEC 7.1 (all JSON-serializable):
    draft_origin: str  # "model" | "user" — a failed hand edit returns to review instead of re-drafting
    revision_instructions: str | None
    gate_message: str | None
    keyword_coverage: dict  # ATS keyword match report for the current resume draft
    ats_warnings: list[str]
    application_method: str | None
    confirmation_ref: str | None
    application_notes: str | None
    # [Amendment A4] ATS Super Match
    generation_mode: str  # "automated" | "ats_super_match"
    ats_headline: str | None  # the headline you approved, shown under your name
    ats_confirmed_keywords: list[str]  # keywords you ticked "I have this" for (saved to the fact bank)
