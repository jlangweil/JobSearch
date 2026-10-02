"""Start/resume application threads in the background executor, and build the workflow view model.
Web handlers call these; they never block on a graph step."""

from __future__ import annotations

import logging
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from langgraph.types import Command

from jobhunt.db import locks, queries
from jobhunt.graphs.application.graph import thread_config, thread_id_for
from jobhunt.graphs.application.nodes import GATE_ACTIONS
from jobhunt.graphs.runtime import Runtime
from jobhunt.tailor import validate

log = logging.getLogger(__name__)


class WorkflowError(RuntimeError):
    pass


def _submit(rt: Runtime, job_id: int, graph_input: Any) -> Future:
    thread_id = thread_id_for(job_id)
    lock = locks.thread_lock_name(thread_id)
    locks.acquire(rt.engine, lock, note=f"application graph for job {job_id}",
                  stale_minutes=rt.cfg.paths.lock_stale_minutes)
    rt.failures.pop(thread_id, None)

    def work() -> None:
        try:
            rt.graph("application").invoke(graph_input, thread_config(job_id))
        except Exception as exc:  # surfaced on the workflow page; the thread can be retried
            log.exception("application thread %s failed", thread_id)
            rt.failures[thread_id] = f"{type(exc).__name__}: {exc}"
        finally:
            locks.release(rt.engine, lock)

    try:
        return rt.submit(work)
    except RuntimeError:
        locks.release(rt.engine, lock)
        raise


def start(rt: Runtime, job_id: int, *, emphasis: str | None = None, company_notes: str | None = None,
          generation_mode: str | None = None) -> Future:
    """Called right after a shortlist: shortlisted -> in_workflow and the thread starts. `generation_mode`:
    automated | ats_super_match (default: tailoring.generation_mode)."""
    job = queries.get_job(rt.engine, job_id)
    if job is None:
        raise WorkflowError(f"job {job_id} not found")
    if job["status"] != "shortlisted":
        raise WorkflowError(f"job {job_id} is {job['status']}; only shortlisted jobs start a workflow")
    thread_id = thread_id_for(job_id)
    queries.set_job_thread(rt.engine, job_id, thread_id)
    initial = {
        "job_id": job_id, "emphasis": emphasis or None, "company_notes": company_notes or None, "requirements": [],
        "candidate_facts": [], "resume_markdown": None, "resume_fact_map": [], "resume_document_id": None,
        "cover_markdown": None, "cover_fact_map": [], "cover_document_id": None, "coverage": [],
        "validation_errors": [], "draft_attempts": 0, "rendered_paths": [], "stage": "parsing_requirements",
        "draft_origin": "model", "revision_instructions": None, "gate_message": None, "keyword_coverage": {},
        "ats_warnings": [],
        "generation_mode": generation_mode if generation_mode in ("automated", "ats_super_match")
        else rt.cfg.tailoring.generation_mode,
        "ats_headline": None, "ats_confirmed_keywords": [],
    }
    queries.set_job_status(rt.engine, job_id, "in_workflow")  # before the worker can read it
    return _submit(rt, job_id, initial)


def pending_gate(rt: Runtime, job_id: int) -> dict | None:
    snap = rt.graph("application").get_state(thread_config(job_id))
    for task in snap.tasks or ():
        for intr in task.interrupts or ():
            return intr.value
    return None


def resume(rt: Runtime, job_id: int, payload: dict) -> Future:
    gate = pending_gate(rt, job_id)
    if gate is None:
        raise WorkflowError("this workflow is not waiting for a decision")
    action = payload.get("action")
    if action not in GATE_ACTIONS[gate["gate"]]:
        raise WorkflowError(f"{action!r} is not a valid action at {gate['gate']}")
    return _submit(rt, job_id, Command(resume=payload))


def retry(rt: Runtime, job_id: int) -> Future:
    snap = rt.graph("application").get_state(thread_config(job_id))
    if not snap.next or pending_gate(rt, job_id) is not None:
        raise WorkflowError("nothing to retry")
    return _submit(rt, job_id, None)


def edit_payload(kind: str, annotated: str) -> dict:
    """Turn the editor textarea (markdown with {fact:id} annotations) into a fixed-shape `edit` payload."""
    if kind == "resume":
        md, fm = validate.split_resume_annotations(annotated)
    else:
        md, fm = validate.split_cover_annotations(annotated)
    return {"action": "edit", "markdown": md, "fact_map": fm}


def view(rt: Runtime, job_id: int) -> dict:
    thread_id = thread_id_for(job_id)
    snap = rt.graph("application").get_state(thread_config(job_id))
    values = dict(snap.values or {})
    gate = pending_gate(rt, job_id)
    running = locks.is_held(rt.engine, locks.thread_lock_name(thread_id), rt.cfg.paths.lock_stale_minutes)
    failure = rt.failures.get(thread_id)
    started = bool(values)
    facts_by_id = {f["id"]: f for f in values.get("candidate_facts") or []}
    editor = ""
    editor_kind = None
    if gate and gate["gate"] == "review_resume":
        editor_kind = "resume"
        editor = validate.annotate_resume(values.get("resume_markdown") or "", values.get("resume_fact_map") or [])
    elif gate and gate["gate"] == "review_cover":
        editor_kind = "cover"
        editor = validate.annotate_cover(values.get("cover_markdown") or "", values.get("cover_fact_map") or [])
    bullets = []
    if values.get("resume_markdown"):
        fm = {e["bullet_index"]: e["fact_id"] for e in values.get("resume_fact_map") or []}
        for b in validate.parse_resume(values["resume_markdown"]).bullets:
            fid = fm.get(b.index)
            bullets.append({"index": b.index, "text": b.text, "role": b.role_heading, "fact_id": fid,
                            "fact": facts_by_id.get(fid) if fid else None})
    return {
        "started": started,
        "running": running and gate is None,
        "failure": failure,
        "can_retry": bool(snap.next) and gate is None and not running,
        "gate": gate,
        "actions": sorted(GATE_ACTIONS[gate["gate"]]) if gate else [],
        "stage": values.get("stage"),
        "state": values,
        "editor": editor,
        "editor_kind": editor_kind,
        "bullets": bullets,
        "resume_words": validate.word_count(values.get("resume_markdown") or ""),
        "done": started and not snap.next,
    }


def needs_attention(rt: Runtime) -> list[dict]:
    """Tailoring workflows waiting for you: a draft to review, or a step that stopped (e.g. an error or a restart)."""
    out = []
    for job in queries.jobs_with_status(rt.engine, ("in_workflow", "ready")):
        thread_id = thread_id_for(job["id"])
        if locks.is_held(rt.engine, locks.thread_lock_name(thread_id), rt.cfg.paths.lock_stale_minutes):
            continue
        snap = rt.graph("application").get_state(thread_config(job["id"]))
        if not snap.next:
            continue
        gate = pending_gate(rt, job["id"])
        out.append({"job": job, "gate": gate["gate"] if gate else None, "stage": (snap.values or {}).get("stage"),
                    "failure": rt.failures.get(thread_id)})
    return out


def rerender_document(rt: Runtime, doc_id: int) -> list[str]:
    """Write a saved document's .docx (and PDF) again with today's rendering settings — no model calls, no new
    version. Use it after changing the template or the role layout."""
    from jobhunt.facts import bank
    from jobhunt.tailor import render

    doc = queries.get_document(rt.engine, doc_id)
    if doc is None:
        raise WorkflowError(f"document {doc_id} not found")
    job = queries.get_job(rt.engine, doc["job_id"])
    if job is None:
        raise WorkflowError("the job for this document is gone")
    cfg = rt.cfg
    person = bank.contact(cfg)
    out = render.output_dir(cfg.paths.output_root, job["company_name_raw"], job["title"], job["id"],
                            max_path_chars=cfg.paths.max_path_chars)
    latest = max((d["version"] for d in queries.documents_for_job(rt.engine, doc["job_id"])
                  if d["kind"] == doc["kind"]), default=doc["version"])
    older = doc["version"] if doc["version"] != latest else None
    doc_path = out / render.document_filename(person.name, doc["kind"], "docx", older)
    render.markdown_to_docx(doc["markdown"] or "", doc_path, template=cfg.paths.resume_template, name=person.name,
                            contact_line=person.line(), kind=doc["kind"], role_layout=cfg.tailoring.role_layout)
    files = [str(doc_path)]
    pdf = render.render_pdf(doc_path, cfg.paths.soffice)
    if pdf:
        files.append(str(pdf))
    queries.set_document_files(rt.engine, doc_id, files)
    return files


def document_path(rt: Runtime, doc_id: int, index: int = 0) -> Path:
    """A rendered file of a document, only from inside the output folder."""
    doc = queries.get_document(rt.engine, doc_id)
    if doc is None or index >= len(doc["file_paths"]):
        raise WorkflowError("that document has no rendered file yet")
    path = Path(doc["file_paths"][index]).resolve()
    if rt.cfg.paths.output_root.resolve() not in path.parents or not path.exists():
        raise WorkflowError(f"{path.name} is not in the output folder any more; re-render it from its document page")
    return path


def open_document(rt: Runtime, doc_id: int, index: int = 0) -> Path:
    """Open the .docx in Word (its default app) on the computer running jobhunt."""
    import os
    import sys

    path = document_path(rt, doc_id, index)
    if sys.platform != "win32":
        raise WorkflowError("opening files directly works on Windows; use Download instead")
    os.startfile(str(path))  # noqa: S606 - a file this app wrote, inside its own output folder
    return path

