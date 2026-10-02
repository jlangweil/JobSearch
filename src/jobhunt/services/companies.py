"""Company management from the web UI. config.yaml stays the source of truth: every change edits the file
(one flow-style line per company), re-validates the whole config, reloads it and syncs the companies table."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Sequence

import httpx
from sqlalchemy import Engine

from jobhunt.config import ATS_TYPES, AppConfig
from jobhunt.db import queries
from jobhunt.graphs.runtime import Runtime
from jobhunt.sources.normalize import slugify
from jobhunt.sources.registry import companies_from_config

SEARCHABLE_ATS = ("workday", "successfactors", "phenom", "eightfold", "greenhouse", "lever", "ashby")
_SLUG_TOKEN = re.compile(r"^[A-Za-z0-9_.\-]+$")


class CompanyError(ValueError):
    pass


def sync_config_companies(engine: Engine, cfg: AppConfig) -> dict:
    """Upsert companies from config; companies no longer in config are deactivated."""
    rows = companies_from_config(cfg)
    for r in rows:
        queries.upsert_company(engine, name=r["name"], slug=r["slug"], ats_type=r["ats_type"],
                               board_token=r["board_token"], notes=r["notes"], active=r["active"])
    off = queries.deactivate_companies_except(engine, [r["slug"] for r in rows])
    return {"synced": len(rows), "active": sum(r["active"] for r in rows), "deactivated": off,
            "by_ats": dict(Counter(r["ats_type"] for r in rows))}


def validate_token(ats: str, token: str | None) -> str:
    from jobhunt.sources import eightfold, phenom, successfactors, workday

    token = (token or "").strip()
    if ats not in ATS_TYPES:
        raise CompanyError(f"unknown ATS {ats!r}")
    if ats in SEARCHABLE_ATS and not token:
        raise CompanyError(f"{ats} companies need a board token")
    try:
        if ats == "workday":
            host, _tenant, site = workday.parse_board_token(token)
            return f"{host}/{site}"
        if ats == "successfactors":
            return successfactors.parse_board_token(token)
        if ats == "phenom":
            host, locale = phenom.parse_board_token(token)
            return f"{host}/{locale}"
        if ats == "eightfold":
            host, domain = eightfold.parse_board_token(token)
            return f"{host}|{domain}"
    except ValueError as exc:
        raise CompanyError(str(exc)) from exc
    if token and not _SLUG_TOKEN.match(token):
        raise CompanyError(f"{ats} board token should look like 'companyname' (the slug in the board URL)")
    return token


def overview(rt: Runtime, *, q: str | None = None, industry: str | None = None, ats: str | None = None,
             active: str | None = None) -> list[dict]:
    stats = {r["company_name_raw"]: r for r in queries.company_job_stats(rt.engine)}
    out = []
    for c in rt.cfg.companies:
        if q and q.lower() not in c.name.lower():
            continue
        if industry and (c.industry or "other") != industry:
            continue
        if ats and c.ats_type != ats:
            continue
        if active in ("yes", "no") and c.active != (active == "yes"):
            continue
        s = stats.get(c.name, {})
        out.append({"name": c.name, "slug": slugify(c.name), "ats_type": c.ats_type, "board_token": c.board_token,
                    "industry": c.industry or "", "active": c.active, "queries": c.queries,
                    "jobs": s.get("jobs", 0), "scored": s.get("scored", 0), "pipeline": s.get("pipeline", 0),
                    "last_seen": s.get("last_seen")})
    return out


def industries(cfg: AppConfig, active_only: bool = True) -> list[tuple[str, int]]:
    counts = Counter((c.industry or "other") for c in cfg.companies if c.active or not active_only)
    return sorted(counts.items())


def _company_line_pattern(name: str) -> re.Pattern[str]:
    n = re.escape(name)
    return re.compile(rf"^(?P<line>[ \t]*-[ \t]*\{{[^\n]*\bname:[ \t]*(?:\"{n}\"|'{n}'|{n})[ \t]*[,}}][^\n]*)$",
                      re.MULTILINE)


def set_active(rt: Runtime, name: str, active: bool) -> None:
    from jobhunt.services import config_edit

    text = config_edit.read_text(rt)
    matches = list(_company_line_pattern(name).finditer(text))
    if len(matches) != 1:
        raise CompanyError(f"could not find a single one-line entry for {name!r} in config.yaml — "
                           "edit it on the config page instead")
    line = matches[0].group("line")
    if re.search(r"\bactive:\s*(true|false)", line):
        new_line = re.sub(r"\bactive:\s*(true|false)", f"active: {'true' if active else 'false'}", line)
    elif active:
        new_line = line
    else:
        new_line = line[: line.rindex("}")].rstrip() + ", active: false}" + line[line.rindex("}") + 1:]
    if active:
        new_line = re.sub(r",\s*active:\s*true", "", new_line)
    config_edit.save_text(rt, text[: matches[0].start("line")] + new_line + text[matches[0].end("line"):])


def add(rt: Runtime, *, name: str, ats: str, token: str, industry: str | None = None,
        queries_text: str | None = None) -> str:
    from jobhunt.services import config_edit

    name = (name or "").strip()
    if not name:
        raise CompanyError("a company name is required")
    if any(slugify(c.name) == slugify(name) for c in rt.cfg.companies):
        raise CompanyError(f"{name!r} is already configured (same slug {slugify(name)!r})")
    token = validate_token(ats, token)
    parts = [f"name: {json.dumps(name, ensure_ascii=False)}", f"ats_type: {ats}"]
    if token:
        parts.append(f"board_token: {json.dumps(token)}")
    if industry and industry.strip():
        parts.append(f"industry: {json.dumps(industry.strip().lower())}")
    terms = [t.strip() for t in (queries_text or "").split(",") if t.strip()]
    if terms:
        parts.append(f"queries: {json.dumps(terms, ensure_ascii=False)}")
    text = config_edit.read_text(rt)
    config_edit.save_text(rt, text.rstrip("\n") + "\n  - {" + ", ".join(parts) + "}\n")
    return slugify(name)


def probe(rt: Runtime, ats: str, target: str, client: httpx.Client | None = None) -> dict:
    """Check a career site before adding it. Returns {ok, message, boards:[{board_token, total, facets}]}."""
    from jobhunt.sources import eightfold, phenom, successfactors, workday
    from jobhunt.sources.base import default_client

    client = client or default_client(timeout=20)
    target = (target or "").strip()
    try:
        if ats == "phenom":
            host, locale = phenom.parse_board_token(target)
            src = phenom.PhenomSource([], client=client, rate_limit_per_sec=0)
            board = phenom.PhenomBoard("probe", host, locale)
            page_id = src.page_id(board)
            data = src._widgets(board, src._search_body(board, page_id, "", 0, 1, {})).get("refineSearch") or {}
            total = data.get("totalHits")
            _, notes = phenom.choose_fields((data.get("data") or {}).get("aggregations") or [], None,
                                            workday.DEFAULT_COUNTRY_PATTERNS)
            return {"ok": total is not None, "boards": [{"board_token": f"{host}/{locale}", "total": total,
                                                         "facets": notes}] if total is not None else [],
                    "message": f"{total} postings" if total is not None else "not a readable Phenom career site"}
        if ats == "eightfold":
            host, domain = eightfold.parse_board_token(target)
            data = client.get(f"https://{host}/api/apply/v2/jobs", params={"domain": domain, "start": 0, "num": 1},
                              headers={"Accept": "application/json"}).json()
            total = data.get("count")
            return {"ok": total is not None, "boards": [{"board_token": f"{host}|{domain}", "total": total,
                                                         "facets": []}] if total is not None else [],
                    "message": f"{total} postings" if total is not None else
                    "not a readable Eightfold site (check the host and domain)"}
        if ats == "workday":
            extra = (rt.cfg.sources.get("workday").model_extra or {}) if rt.cfg.sources.get("workday") else {}
            boards = workday.discover_sites(
                target, client=client,
                category_patterns=extra.get("category_patterns", workday.DEFAULT_CATEGORY_PATTERNS),
                country_patterns=extra.get("country_patterns", workday.DEFAULT_COUNTRY_PATTERNS))
            ok = any(b["total"] is not None for b in boards)
            return {"ok": ok, "boards": boards,
                    "message": f"{len(boards)} public site(s) in robots.txt" if boards else
                    "no public career sites found (check the tenant host, e.g. pru.wd5.myworkdayjobs.com)"}
        if ats == "successfactors":
            host = successfactors.parse_board_token(target)
            page = client.get(f"https://{host}/search/?q=&sortColumn=referencedate&sortDirection=desc").text
            rows, total = successfactors.parse_listing(page)
            return {"ok": bool(rows), "boards": [{"board_token": host, "total": total, "facets": []}] if rows else [],
                    "message": f"{len(rows)} postings on the first page, {total} total" if rows else
                    "not a readable SuccessFactors career site"}
        urls = {"greenhouse": "https://boards-api.greenhouse.io/v1/boards/{t}/jobs",
                "lever": "https://api.lever.co/v0/postings/{t}?mode=json",
                "ashby": "https://api.ashbyhq.com/posting-api/job-board/{t}"}
        if ats not in urls:
            raise CompanyError(f"cannot probe {ats!r}")
        token = validate_token(ats, target)
        r = client.get(urls[ats].format(t=token))
        if r.status_code != 200:
            return {"ok": False, "boards": [], "message": f"HTTP {r.status_code} — no public board named {token!r}"}
        data = r.json()
        total = len(data) if isinstance(data, list) else len(data.get("jobs", []))
        return {"ok": True, "boards": [{"board_token": token, "total": total, "facets": []}],
                "message": f"{total} open postings"}
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "boards": [], "message": f"probe failed: {exc}"}


def resolve_selection(cfg: AppConfig, industries_sel: Sequence[str], slugs: Sequence[str]) -> list[str] | None:
    """Web run form → company slugs (None = every active company)."""
    active = [c for c in cfg.companies if c.active]
    chosen = [s for s in slugs if s]
    if industries_sel:
        wanted = set(industries_sel)
        chosen += [slugify(c.name) for c in active if (c.industry or "other") in wanted]
    return sorted(set(chosen)) or None
