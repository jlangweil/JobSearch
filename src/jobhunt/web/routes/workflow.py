from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse

from jobhunt.apply import workday
from jobhunt.db import queries
from jobhunt.db.locks import LockHeld
from jobhunt.services import workflow
from jobhunt.web.common import redirect, render, rt

router = APIRouter()


def _context(job_id: int) -> dict:
    job = queries.get_job(rt().engine, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return {"job": job, "wf": workflow.view(rt(), job_id), "cfg_resume_mode": rt().cfg.tailoring.resume_mode,
            "ats_max_words": rt().cfg.tailoring.ats.max_words,
            "workday": workday.is_workday(job.get("apply_url") or job.get("url"))}


@router.get("/jobs/{job_id}/workflow")
def workflow_page(request: Request, job_id: int):  # noqa: ANN201
    return render(request, "workflow.html", _context(job_id))


@router.get("/jobs/{job_id}/workflow/status")
def workflow_status(request: Request, job_id: int):  # noqa: ANN201
    return render(request, "fragments/draft_panel.html", _context(job_id))


@router.post("/jobs/{job_id}/workflow/resume")
def workflow_resume(
    request: Request,
    job_id: int,
    action: str = Form(...),
    instructions: str = Form(""),
    editor: str = Form(""),
    kind: str = Form("resume"),
    method: str = Form("deep_link"),
    confirmation_ref: str = Form(""),
    notes: str = Form(""),
    keyword: list[str] = Form([]),  # noqa: B008
    use_headline: str = Form(""),
    headline: str = Form(""),
):  # noqa: ANN201
    r = rt()
    try:
        if action == "retry":
            workflow.retry(r, job_id)
        else:
            if action == "accept":
                payload: dict = {"action": "accept"}
            elif action == "revise":
                payload = {"action": "revise", "instructions": instructions}
            elif action == "edit":
                payload = workflow.edit_payload("resume" if kind == "resume" else "cover", editor)
            elif action == "abandon":
                payload = {"action": "abandon"}
            elif action == "confirm":
                payload = {"action": "confirm", "method": method or "deep_link",
                           "confirmation_ref": confirmation_ref or None, "notes": notes or None}
            elif action == "apply":  # ATS Super Match: the keywords and headline you ticked
                payload = {"action": "apply", "keywords": keyword, "use_headline": use_headline == "1",
                           "headline": headline}
            elif action == "skip":
                payload = {"action": "skip"}
            elif action == "back_to_resume":
                payload = {"action": "back_to_resume", "instructions": instructions or None}
            else:
                raise workflow.WorkflowError(f"unknown action {action!r}")
            workflow.resume(r, job_id, payload)
    except (workflow.WorkflowError, LockHeld) as exc:
        return redirect(f"/jobs/{job_id}/workflow", error=str(exc))
    return redirect(f"/jobs/{job_id}/workflow")


@router.get("/documents")
def documents_page(request: Request):  # noqa: ANN201
    rows = queries.tailored_jobs(rt().engine)
    for row in rows:
        for kind in ("resume", "cover_letter"):
            doc = row.get(kind)
            if doc:
                doc["files"] = [{"index": i, "name": Path(p).name, "ext": Path(p).suffix.lstrip(".").upper(),
                                 "exists": Path(p).exists()} for i, p in enumerate(doc["file_paths"])]
    return render(request, "documents.html", {"rows": rows, "output_root": str(rt().cfg.paths.output_root)})


@router.get("/documents/{doc_id}")
def document(request: Request, doc_id: int):  # noqa: ANN201
    doc = queries.get_document(rt().engine, doc_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    job = queries.get_job(rt().engine, doc["job_id"])
    files = [{"index": i, "name": Path(p).name, "exists": Path(p).exists()} for i, p in enumerate(doc["file_paths"])]
    return render(request, "document.html", {"doc": doc, "job": job, "files": files})


@router.post("/documents/{doc_id}/rerender")
def rerender_document(doc_id: int):  # noqa: ANN201
    from jobhunt.tailor.render import RenderError

    try:
        files = workflow.rerender_document(rt(), doc_id)
    except (workflow.WorkflowError, RenderError) as exc:
        return redirect(f"/documents/{doc_id}", error=str(exc))
    return redirect(f"/documents/{doc_id}",
                    message=f"Rewrote {', '.join(Path(f).name for f in files)} with the current template and role "
                            "layout. The text is unchanged.")


@router.post("/documents/{doc_id}/open")
def open_document(request: Request, doc_id: int, index: int = Form(0), next: str = Form("")):  # noqa: A002, ANN201
    """Open the Word file on this computer. From another device on the network it would open on the server, so
    those requests get the download instead."""
    back = next if next.startswith("/") else f"/documents/{doc_id}"
    if (request.client.host if request.client else "") not in ("127.0.0.1", "::1", "localhost", "testclient"):
        return redirect(f"/documents/{doc_id}/files/{index}")
    try:
        path = workflow.open_document(rt(), doc_id, index)
    except workflow.WorkflowError as exc:
        return redirect(back, error=str(exc))
    return redirect(back, message=f"Opened {path.name} in Word.")


@router.get("/documents/{doc_id}/files/{index}")
def document_file(doc_id: int, index: int) -> FileResponse:
    doc = queries.get_document(rt().engine, doc_id)
    if doc is None or index >= len(doc["file_paths"]):
        raise HTTPException(404, "file not found")
    path = Path(doc["file_paths"][index]).resolve()
    root = rt().cfg.paths.output_root.resolve()
    if root not in path.parents or not path.exists():
        raise HTTPException(404, "file not found")
    return FileResponse(path, filename=path.name)
