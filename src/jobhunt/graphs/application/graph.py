"""application_graph (SPEC 7.2): one long-lived, checkpointed thread per job with human review gates."""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from jobhunt.graphs.application import nodes
from jobhunt.graphs.application.state import ApplicationState


def thread_id_for(job_id: int) -> str:
    return f"job-{job_id}"


def thread_config(job_id: int) -> dict:
    return {"configurable": {"thread_id": thread_id_for(job_id)}}


def build_graph(checkpointer: BaseCheckpointSaver):  # noqa: ANN201
    g = StateGraph(ApplicationState)
    g.add_node("parse_requirements", nodes.parse_requirements)
    g.add_node("retrieve_facts", nodes.retrieve_facts)
    g.add_node("review_ats", nodes.review_ats, destinations=("draft_resume", "review_ats", END))  # Amendment A4
    g.add_node("draft_resume", nodes.draft_resume)
    g.add_node("validate_resume", nodes.validate_resume, destinations=("review_resume", "draft_resume"))
    g.add_node("review_resume", nodes.review_resume,
               destinations=("draft_cover_letter", "draft_resume", "validate_resume", "review_resume", END))
    g.add_node("draft_cover_letter", nodes.draft_cover_letter)
    g.add_node("validate_cover", nodes.validate_cover, destinations=("review_cover", "draft_cover_letter"))
    g.add_node("review_cover", nodes.review_cover,
               destinations=("render_documents", "draft_cover_letter", "validate_cover", "review_cover"))
    g.add_node("render_documents", nodes.render_documents)
    g.add_node("approve_and_apply", nodes.approve_and_apply,
               destinations=("record_application", "draft_resume", "approve_and_apply"))
    g.add_node("record_application", nodes.record_application)

    g.add_edge(START, "parse_requirements")
    g.add_edge("parse_requirements", "retrieve_facts")
    # ATS Super Match asks which keywords and headline may be added before the first draft
    g.add_conditional_edges("retrieve_facts", lambda s: "review_ats" if s.get("generation_mode") == "ats_super_match"
                            else "draft_resume", ["review_ats", "draft_resume"])
    g.add_edge("draft_resume", "validate_resume")
    g.add_edge("draft_cover_letter", "validate_cover")
    g.add_edge("render_documents", "approve_and_apply")
    g.add_edge("record_application", END)
    return g.compile(checkpointer=checkpointer)
