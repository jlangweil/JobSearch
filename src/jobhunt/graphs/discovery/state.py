from __future__ import annotations

from typing import TypedDict


class DiscoveryState(TypedDict, total=False):
    run_id: int
    sources: list[str]
    since: str | None  # ISO-8601 UTC
    fetched_job_ids: list[int]
    candidate_job_ids: list[int]
    counts: dict[str, int]
    errors: list[dict]  # {source, node, message, traceback_head}
    max_to_score: int
    # Addition: ghost scores computed by the `ghost` node for the `score` node (keys are str job ids).
    ghost_scores: dict[str, int]
    # Addition: optional company-name filter for small test runs (`jobhunt run --companies`).
    companies: list[str]
    # Addition: "process stored jobs only" — skip fetching; filter and score jobs already waiting in 'discovered'.
    skip_fetch: bool
    # Addition: "fetch & save without scoring" — stop after ghost; survivors stay 'discovered' for later scoring.
    skip_score: bool
    posted_since: str | None  # this run only: leave out postings dated before this (ISO); None = no limit
