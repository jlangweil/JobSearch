from __future__ import annotations

from fastapi import APIRouter, Form, Request

from jobhunt.config import ATS_TYPES
from jobhunt.services import companies as company_service
from jobhunt.services.config_edit import ConfigError
from jobhunt.web.common import redirect, render, rt, safe_next

router = APIRouter()


def _filters(request: Request) -> dict:
    p = request.query_params
    return {k: p.get(k, "") for k in ("q", "industry", "ats", "active")}


@router.get("/companies")
def companies(request: Request):  # noqa: ANN201
    r = rt()
    f = _filters(request)
    rows = company_service.overview(r, q=f["q"] or None, industry=f["industry"] or None, ats=f["ats"] or None,
                                    active=f["active"] or None)
    probe = None
    if request.query_params.get("probe_target"):
        probe = company_service.probe(r, request.query_params.get("probe_ats", "workday"),
                                      request.query_params["probe_target"])
    return render(request, "companies.html", {
        "rows": rows, "filters": f, "ats_types": ATS_TYPES, "industries": company_service.industries(r.cfg, False),
        "probe": probe, "probe_ats": request.query_params.get("probe_ats", "workday"),
        "probe_target": request.query_params.get("probe_target", ""), "total": len(r.cfg.companies),
        "current_url": str(request.url.path) + ("?" + request.url.query if request.url.query else ""),
    })


@router.post("/companies/active")
def set_active(name: str = Form(...), active: str = Form(...), next: str = Form("")):  # noqa: A002, ANN201
    try:
        company_service.set_active(rt(), name, active == "true")
    except (company_service.CompanyError, ConfigError) as exc:
        return redirect("/companies", error=str(exc))
    return redirect(safe_next(next, "/companies"), message=f"{name} {'activated' if active == 'true' else 'paused'}")


@router.post("/companies")
def add_company(name: str = Form(""), ats_type: str = Form(...), board_token: str = Form(""),
                industry: str = Form(""), queries: str = Form("")):  # noqa: ANN201
    try:
        slug = company_service.add(rt(), name=name, ats=ats_type, token=board_token, industry=industry,
                                   queries_text=queries)
    except (company_service.CompanyError, ConfigError) as exc:
        return redirect("/companies", error=str(exc))
    return redirect("/companies", q=name, message=f"added {name} to config.yaml ({slug}). "
                                                  "Try a dry run for it from the Runs page.")


@router.post("/companies/sync")
def sync():  # noqa: ANN201
    r = rt()
    result = company_service.sync_config_companies(r.engine, r.cfg)
    return redirect("/companies", message=f"synced {result['synced']} companies ({result['active']} active) "
                                          f"from config.yaml; {result['deactivated']} no longer in config")
