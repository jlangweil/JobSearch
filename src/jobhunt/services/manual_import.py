"""Add a job by hand, from a posting URL or pasted text, then save it, score it, start tailoring, or record that you
already applied.

Reading a URL, best source first:
  1. a known ATS: the same JSON APIs the adapters use (Workday, Greenhouse, Lever, Ashby, Phenom, Eightfold) or the
     SuccessFactors job page. The job gets that source's id, so later runs recognise it instead of duplicating it;
  2. schema.org JobPosting JSON-LD embedded in the page (most career sites publish it for search engines);
  3. the page's visible text.
LinkedIn, Indeed, Glassdoor and ZipRecruiter pages are never fetched (CLAUDE.md); paste their text instead.
Pasted text is kept verbatim; the model only fills in title/company/location when they're missing.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx

from jobhunt.db import queries
from jobhunt.db.engine import parse_iso, utcnow_iso
from jobhunt.graphs.runtime import Runtime
from jobhunt.sources.base import RawJob
from jobhunt.sources.normalize import normalize_raw, strip_html

log = logging.getLogger(__name__)

MANUAL_SOURCE = "manual"
BLOCKED_DOMAINS = ("linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com")
PAGE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) jobhunt/0.1 (personal job search; one page on request)"
MIN_TEXT_CHARS = 300
NEXT_STEPS = ("tailor", "score", "save", "applied")
FOLLOW_UP_DAYS = 7


class ImportProblem(ValueError):
    pass


def _client() -> httpx.Client:
    return httpx.Client(timeout=25, follow_redirects=True,
                        headers={"User-Agent": PAGE_UA, "Accept-Language": "en-US,en;q=0.9"})


def _draft(**kw) -> dict:  # noqa: ANN003
    base = {"title": "", "company": "", "location": "", "remote_type": "", "employment_hint": "", "comp_min": None,
            "comp_max": None, "url": "", "apply_url": "", "description_html": "", "description": "",
            "posted_at": "", "source": MANUAL_SOURCE, "source_job_id": "", "read_with": "", "notes": [],
            "existing_job_id": None, "existing_status": None}
    base.update({k: v for k, v in kw.items() if v is not None})
    return base


def _from_raw(raw: RawJob, read_with: str) -> dict:
    rec = normalize_raw(raw)
    return _draft(title=raw.title, company=raw.company_name, location=raw.location or "",
                  remote_type=(raw.extra or {}).get("workplace_type") or "",
                  employment_hint=(raw.extra or {}).get("employment_hint") or "", comp_min=rec["comp_min"],
                  comp_max=rec["comp_max"], url=raw.url, apply_url=raw.apply_url or raw.url,
                  description_html=raw.description_html, description=rec["description_text"],
                  posted_at=rec["posted_at"] or "", source=raw.source, source_job_id=raw.source_job_id,
                  read_with=read_with)


def _company_for(rt: Runtime, *keys: str) -> str | None:
    """The configured company whose board token (or its host) is one of `keys`."""
    wanted = {k.lower().strip("/") for k in keys if k}
    for c in rt.cfg.companies:
        token = (c.board_token or "").lower().strip("/")
        if token and (token in wanted or token.split("|")[0].split("/")[0] in wanted):
            return c.name
    return None


# --------------------------------------------------------------------------------------------- URL readers


def _company_for_workday(rt: Runtime, host: str, site: str) -> str | None:
    wanted = f"{host}/{site}".lower()
    return next((c.name for c in rt.cfg.companies if (c.board_token or "").lower().strip("/") == wanted), None)


def _workday(rt: Runtime, u, client: httpx.Client) -> dict | None:  # noqa: ANN001
    from jobhunt.sources import workday

    if not (u.hostname or "").endswith("myworkdayjobs.com"):
        return None
    parts = [p for p in u.path.split("/") if p]
    if "job" not in parts:
        return None
    i = parts.index("job")
    site_parts = [p for p in parts[:i] if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
    if not site_parts:
        return None
    host, tenant, site = u.hostname, u.hostname.split(".")[0], site_parts[-1]
    external = "/" + "/".join(parts[i:])
    detail = client.get(f"https://{host}/wday/cxs/{tenant}/{site}{external}", headers={"Accept": "application/json"})
    detail.raise_for_status()
    data = detail.json()
    company = (_company_for_workday(rt, host, site) or (data.get("hiringOrganization") or {}).get("name")
               or tenant.title())
    board = workday.WorkdayBoard(company, host, tenant, site)
    source_job_id = f"{tenant}/{external.rstrip('/').split('/')[-1]}"
    raw = workday.WorkdaySource.map_detail(board, {}, data, source_job_id, f"https://{host}/{site}{external}", None)
    return _from_raw(raw, "Workday job API")


def _greenhouse(rt: Runtime, u, client: httpx.Client) -> dict | None:  # noqa: ANN001
    from jobhunt.sources import greenhouse

    host = u.hostname or ""
    qs = parse_qs(u.query)
    parts = [p for p in u.path.split("/") if p]
    token = job_id = None
    if host.endswith("greenhouse.io") and len(parts) >= 3 and parts[1] == "jobs":
        token, job_id = parts[0], parts[2]
    elif host.endswith("greenhouse.io") and qs.get("for") and qs.get("token"):
        token, job_id = qs["for"][0], qs["token"][0]
    if not (token and job_id and job_id.isdigit()):
        return None
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}")
    r.raise_for_status()
    company = _company_for(rt, token)
    if not company:
        try:
            company = client.get(f"https://boards-api.greenhouse.io/v1/boards/{token}").json().get("name")
        except (httpx.HTTPError, ValueError):
            company = None
    return _from_raw(greenhouse.GreenhouseSource.map_job(company or token.title(), r.json()), "Greenhouse job API")


def _lever(rt: Runtime, u, client: httpx.Client) -> dict | None:  # noqa: ANN001
    from jobhunt.sources import lever

    parts = [p for p in u.path.split("/") if p]
    if not ((u.hostname or "").endswith("lever.co") and len(parts) >= 2):
        return None
    r = client.get(f"https://api.lever.co/v0/postings/{parts[0]}/{parts[1]}")
    r.raise_for_status()
    company = _company_for(rt, parts[0]) or parts[0].replace("-", " ").title()
    return _from_raw(lever.LeverSource.map_job(company, r.json()), "Lever job API")


def _ashby(rt: Runtime, u, client: httpx.Client) -> dict | None:  # noqa: ANN001
    from jobhunt.sources import ashby

    parts = [p for p in u.path.split("/") if p]
    if not ((u.hostname or "").endswith("ashbyhq.com") and len(parts) >= 2):
        return None
    r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{parts[0]}?includeCompensation=true")
    r.raise_for_status()
    job = next((j for j in r.json().get("jobs", []) if str(j.get("id")) == parts[1]), None)
    if job is None:
        raise ImportProblem("that Ashby job isn't on the company's public board any more")
    company = _company_for(rt, parts[0]) or parts[0].replace("-", " ").title()
    return _from_raw(ashby.AshbySource.map_job(company, job), "Ashby job board API")


def _eightfold(rt: Runtime, u, client: httpx.Client, page: str | None) -> dict | None:  # noqa: ANN001
    from jobhunt.sources import eightfold

    qs = parse_qs(u.query)
    m = re.search(r"/job/(\d+)", u.path)
    pid = (qs.get("pid") or [None])[0] or (m.group(1) if m else None)
    if not pid or not (u.hostname.endswith("eightfold.ai") or (page and "eightfold" in page.lower())):
        return None
    domain = (qs.get("domain") or [None])[0]
    host, domain = eightfold.parse_board_token(f"{u.hostname}|{domain}" if domain else u.hostname)
    r = client.get(f"https://{host}/api/apply/v2/jobs/{pid}", params={"domain": domain},
                   headers={"Accept": "application/json"})
    r.raise_for_status()
    company = _company_for(rt, host) or domain.split(".")[0].title()
    raw = eightfold.EightfoldSource.map_job(eightfold.EightfoldBoard(company, host, domain), {"id": pid}, r.json())
    return _from_raw(raw, "Eightfold job API")


def _phenom(rt: Runtime, u, client: httpx.Client, page: str | None) -> dict | None:  # noqa: ANN001
    from jobhunt.sources import phenom

    if not page or "phApp" not in page:
        return None
    parts = [p for p in u.path.split("/") if p]
    if "job" not in parts or parts.index("job") + 1 >= len(parts):
        return None
    seq = parts[parts.index("job") + 1]
    host, locale = phenom.parse_board_token(f"{u.hostname}{u.path}")
    src = phenom.PhenomSource([], client=client, rate_limit_per_sec=0)
    board = phenom.PhenomBoard(_company_for(rt, host) or "", host, locale)
    m = re.search(r"""(?:phApp\.pageId\s*=\s*|"pageId"\s*:\s*)["']([^"']+)["']""", page)
    job = src.detail(board, m.group(1) if m else "page1", seq)
    if not job:
        return None
    board.company_name = (board.company_name or job.get("companyName") or job.get("company")
                          or host.split(".")[1].title())
    url = f"{board.site}/job/{seq}"
    return _from_raw(phenom.PhenomSource.map_job(board, {"jobSeqNo": seq}, job, url), "Phenom job API")


def _successfactors(rt: Runtime, u, page: str | None) -> dict | None:  # noqa: ANN001
    from jobhunt.sources import successfactors

    m = re.search(r"/job/[^/]+/(\d+)/?", u.path)
    if not page or not m or not ("rmkcdn" in page or "successfactors" in page.lower()):
        return None
    detail = successfactors.parse_detail(page)
    if not detail.get("description_html"):
        return None
    company = _company_for(rt, u.hostname) or u.hostname.split(".")[-2].title()
    board = successfactors.SuccessFactorsBoard(company, u.hostname)
    raw = successfactors.SuccessFactorsSource.map_detail(board, {"id": m.group(1), "href": u.path, "title": ""}, detail)
    return _from_raw(raw, "SuccessFactors job page")


def _walk_json_ld(node) -> dict | None:  # noqa: ANN001
    if isinstance(node, list):
        for item in node:
            found = _walk_json_ld(item)
            if found:
                return found
    elif isinstance(node, dict):
        kind = node.get("@type")
        if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
            return node
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                found = _walk_json_ld(node[key])
                if found:
                    return found
    return None


def job_posting_json_ld(page: str) -> dict | None:
    for block in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", page, re.S | re.I):
        try:
            data = json.loads(html.unescape(block.strip()) if block.strip().startswith("&") else block.strip(),
                              strict=False)
        except ValueError:
            continue
        found = _walk_json_ld(data)
        if found:
            return found
    return None


def _ld_location(ld: dict) -> str:
    locs = ld.get("jobLocation") or []
    out = []
    for loc in locs if isinstance(locs, list) else [locs]:
        addr = (loc or {}).get("address") if isinstance(loc, dict) else None
        if isinstance(addr, dict):
            country = addr.get("addressCountry")
            country = country.get("name") if isinstance(country, dict) else country
            parts = (addr.get("addressLocality"), addr.get("addressRegion"), country)
            out.append(", ".join(str(x) for x in parts if x))
        elif isinstance(addr, str):
            out.append(addr)
    return "; ".join(x for x in dict.fromkeys(out) if x)


def draft_from_json_ld(ld: dict, url: str) -> dict:
    org = ld.get("hiringOrganization")
    company = org.get("name") if isinstance(org, dict) else (org if isinstance(org, str) else "")
    desc = ld.get("description") or ""
    if "&lt;" in desc and "<" not in desc:
        desc = html.unescape(desc)
    salary = (ld.get("baseSalary") or {}) if isinstance(ld.get("baseSalary"), dict) else {}
    value = salary.get("value") if isinstance(salary.get("value"), dict) else {}
    unit = str(value.get("unitText") or salary.get("unitText") or "").upper()
    lo, hi = value.get("minValue") or value.get("value"), value.get("maxValue") or value.get("value")
    comp_ok = unit in ("YEAR", "") and isinstance(lo, int | float) and lo > 1000
    emp = ld.get("employmentType")
    location = _ld_location(ld)
    remote = "remote" if str(ld.get("jobLocationType") or "").upper() == "TELECOMMUTE" else ""
    raw = RawJob(MANUAL_SOURCE, "", company or "", ld.get("title") or "", location or None, desc, url,
                 ld.get("url") or url, parse_iso(str(ld.get("datePosted") or "")) if ld.get("datePosted") else None,
                 int(lo) if comp_ok else None, int(hi) if comp_ok and hi else None,
                 {"workplace_type": remote or None,
                  "employment_hint": ", ".join(emp) if isinstance(emp, list) else emp})
    return _from_raw(raw, "the page's job data (schema.org JobPosting)")


def _visible_text(page: str) -> tuple[str, str, str]:
    """(title, site_name, main text) from an HTML page."""
    def meta(prop: str) -> str:
        m = re.search(rf'<meta[^>]+(?:property|name)=["\']{prop}["\'][^>]+content=["\']([^"\']*)', page, re.I)
        return html.unescape(m.group(1)).strip() if m else ""

    title = meta("og:title") or html.unescape((re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
                                               or [None, ""])[1]).strip()
    body = re.sub(r"<(script|style|noscript|svg|nav|header|footer|form|iframe)\b.*?</\1>", " ", page, flags=re.S | re.I)
    main = re.search(r"<(main|article)\b.*?</\1>", body, re.S | re.I)
    return title, meta("og:site_name"), strip_html(main.group(0) if main else body)


def read_url(rt: Runtime, url: str, client: httpx.Client | None = None) -> dict:
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if not host or "." not in host:
        raise ImportProblem("that doesn't look like a web address")
    if any(host == d or host.endswith("." + d) for d in BLOCKED_DOMAINS):
        raise ImportProblem(f"{host} isn't read automatically (its terms forbid it). Open the posting, copy the "
                            "text, and use “Paste the posting” instead; keep the link in the URL box.")
    client = client or _client()
    try:
        for reader in (_workday, _greenhouse, _lever, _ashby):
            draft = reader(rt, u, client)
            if draft:
                return draft
        r = client.get(url)
        r.raise_for_status()
        page = r.text
        final = urlparse(str(r.url))
        for reader in (_eightfold, _phenom):
            draft = reader(rt, final, client, page)
            if draft:
                return draft
        draft = _successfactors(rt, final, page)
        if draft:
            return draft
    except httpx.HTTPStatusError as exc:
        raise ImportProblem(f"the site answered {exc.response.status_code} for that link. If it needs a login or "
                            "blocks automated reads, paste the posting text instead.") from exc
    except httpx.HTTPError as exc:
        raise ImportProblem(f"couldn't reach {host}: {exc}. Check the link, or paste the posting text.") from exc
    ld = job_posting_json_ld(page)
    if ld and (ld.get("description") or ld.get("title")):
        draft = draft_from_json_ld(ld, str(r.url))
        if len(draft["description"]) >= MIN_TEXT_CHARS:
            return draft
    title, site, text = _visible_text(page)
    if len(text) < MIN_TEXT_CHARS:
        raise ImportProblem("that page has almost no readable text (it's probably built by JavaScript). Open it in "
                            "your browser, copy the posting, and paste it instead.")
    draft = _draft(title=title.split(" | ")[0].split(" - ")[0].strip(), company=site, url=str(r.url),
                   apply_url=str(r.url), description=text, read_with="the page text")
    draft["notes"].append("Read from the page text: check the description and trim anything that isn't the posting.")
    return draft


# --------------------------------------------------------------------------------------------- pasted text & model


def read_text(text: str, url: str | None = None) -> dict:
    text = (text or "").strip()
    if len(text) < 80:
        raise ImportProblem("paste the whole posting (title, responsibilities, requirements)")
    return _draft(description=text, url=(url or "").strip(), apply_url=(url or "").strip(), read_with="pasted text")


def fill_missing_with_model(rt: Runtime, draft: dict) -> dict:
    """Fill in title/company/location/pay from the description when missing. Never changes the description."""
    if draft["title"] and draft["company"]:
        return draft
    from jobhunt.llm.client import load_prompt
    from jobhunt.llm.schemas import PostingFields

    llm = rt.llm()
    try:
        out = llm.structured(PostingFields, system=load_prompt("extract_posting", "v1").safe_substitute(),
                             user=draft["description"][:12000], purpose="extract_posting", model=rt.cfg.scoring.model,
                             prompt_version="extract_posting.v1", max_tokens=600)
    except Exception as exc:  # the form still works; the user types the fields
        log.info("posting field extraction failed: %s", exc)
        draft["notes"].append(f"Couldn't fill in the fields automatically ({type(exc).__name__}); please enter them.")
        return draft
    label = llm.model_label("extract_posting") if hasattr(llm, "model_label") else "the model"
    filled = []
    for key, value in (("title", out.title), ("company", out.company), ("location", out.location)):
        if not draft[key] and value.strip():
            draft[key] = value.strip()
            filled.append(key)
    if not draft["remote_type"] and out.work_arrangement in ("remote", "hybrid", "onsite"):
        draft["remote_type"] = out.work_arrangement
        filled.append("work arrangement")
    if draft["comp_min"] is None and out.salary_min > 1000:
        draft["comp_min"], draft["comp_max"] = out.salary_min, out.salary_max if out.salary_max > 1000 else None
        filled.append("pay")
    if filled:
        draft["notes"].append(f"{', '.join(filled).capitalize()} filled in by {label}: check before saving.")
    return draft


def find_existing(rt: Runtime, draft: dict) -> dict:
    job_id = None
    if draft["source"] != MANUAL_SOURCE and draft["source_job_id"]:
        job_id = queries.job_id_by_source(rt.engine, draft["source"], draft["source_job_id"])
    if job_id is None:
        job_id = queries.job_id_by_url(rt.engine, draft["url"], draft["apply_url"])
    if job_id is not None:
        job = queries.get_job(rt.engine, job_id) or {}
        draft["existing_job_id"], draft["existing_status"] = job_id, job.get("status")
    return draft


# --------------------------------------------------------------------------------------------- save


def text_to_html(text: str) -> str:
    paragraphs = [p for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]
    return "\n".join("<p>" + "<br>".join(html.escape(line) for line in p.split("\n")) + "</p>" for p in paragraphs)


def _int(value: object) -> int | None:
    digits = re.sub(r"[^\d]", "", str(value or ""))
    return int(digits) if digits else None


def save(rt: Runtime, form: Mapping[str, str]) -> tuple[int, str]:
    """Create (or update) the job from the reviewed form. Returns (job_id, 'new' | 'updated' | 'existing')."""
    from jobhunt.discovery import service

    title, company = (form.get("title") or "").strip(), (form.get("company") or "").strip()
    description = (form.get("description") or "").strip()
    if not title or not company:
        raise ImportProblem("title and company are required")
    if len(description) < 80:
        raise ImportProblem("the job description is required (it's what the resume is tailored to)")
    url = (form.get("url") or "").strip()
    source = form.get("source") or MANUAL_SOURCE
    source_job_id = (form.get("source_job_id") or "").strip()
    if source == MANUAL_SOURCE or not source_job_id:
        source = MANUAL_SOURCE
        seed = url or f"{company}|{title}|{description[:500]}"
        source_job_id = "manual-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    remote = form.get("remote_type") if form.get("remote_type") in ("remote", "hybrid", "onsite") else None
    raw = RawJob(source, source_job_id, company, title, (form.get("location") or "").strip() or None,
                 text_to_html(description), url, (form.get("apply_url") or "").strip() or url or None,
                 parse_iso(form.get("posted_at")) if form.get("posted_at") else datetime.now(UTC),
                 _int(form.get("comp_min")), _int(form.get("comp_max")),
                 {"workplace_type": remote, "employment_hint": form.get("employment_hint") or None})
    rec = normalize_raw(raw)
    replace = _int(form.get("replace_job_id"))
    if replace:
        if queries.get_job(rt.engine, replace) is None:
            raise ImportProblem(f"job {replace} not found")
        queries.replace_job_content(rt.engine, replace, rec)
        job_id, outcome = replace, "updated"
    else:
        result, job_id = queries.upsert_job(rt.engine, rec)
        assert job_id is not None
        outcome = "new" if result == "new" else ("updated" if result == "changed" else "existing")
        if result == "new":
            queries.mark_manual(rt.engine, job_id)
    rt.submit(lambda: service.normalize(rt.engine, [job_id], rt.embedder()))  # embedding for search/dedupe
    return job_id, outcome


def next_step(rt: Runtime, job_id: int, form: Mapping[str, str]) -> tuple[str, str]:
    """Run the chosen follow-up. Returns (redirect url, message)."""
    from jobhunt.services import scoring_actions, triage

    step = form.get("next_step") if form.get("next_step") in NEXT_STEPS else "save"
    job = queries.get_job(rt.engine, job_id) or {}
    status = job.get("status")
    if step == "tailor":
        if status in ("shortlisted", "in_workflow", "ready"):
            return f"/jobs/{job_id}/workflow", "This job already has a tailoring workflow."
        cfg = rt.cfg
        if cfg.tailoring.provider == "anthropic" and not cfg.secrets.ANTHROPIC_API_KEY:
            raise ImportProblem("the job was saved, but tailoring uses Claude and ANTHROPIC_API_KEY isn't set. Add it "
                                "to .env and restart, or switch tailoring to the local model on Settings.")
        triage.shortlist(rt, job_id, reason="added manually", emphasis=form.get("emphasis") or None,
                         company_notes=form.get("company_notes") or None)
        return (f"/jobs/{job_id}/workflow", "Saved. Reading the requirements and drafting your resume: the draft "
                                            "appears here in a minute or two.")
    if step == "score":
        provider = form.get("score_provider") if form.get("score_provider") in ("local", "anthropic") else None
        if provider == "anthropic" and not rt.cfg.secrets.ANTHROPIC_API_KEY:
            raise ImportProblem("scoring with Claude needs ANTHROPIC_API_KEY in .env; choose Local instead")
        then = None
        message = "Saved. Scoring now; the score appears on this page in a few seconds, then choose whether to tailor."
        if form.get("auto_tailor") == "on":
            try:
                threshold = float(form.get("tailor_min") or rt.cfg.scoring.shortlist_threshold)
            except ValueError as exc:
                raise ImportProblem("the minimum score must be a number") from exc
            emphasis, notes = form.get("emphasis") or None, form.get("company_notes") or None

            def then(row: dict) -> None:
                notice = f"notice-{job_id}"
                if row["total"] < threshold:
                    rt.failures[notice] = (f"Scored {row['total']:.0f}, below your {threshold:.0f} minimum, so "
                                           "tailoring didn't start. You can still tailor it below.")
                    return
                cfg = rt.cfg
                if cfg.tailoring.provider == "anthropic" and not cfg.secrets.ANTHROPIC_API_KEY:
                    rt.failures[notice] = (f"Scored {row['total']:.0f}, but tailoring needs ANTHROPIC_API_KEY, so it "
                                           "didn't start.")
                    return
                triage.shortlist(rt, job_id, reason=f"added manually; scored {row['total']:.0f}", emphasis=emphasis,
                                 company_notes=notes)
                rt.failures[notice] = (f"Scored {row['total']:.0f} (at least {threshold:.0f}), so tailoring started: "
                                       "open the workflow to review the drafts.")

            message = (f"Saved. Scoring now; if it scores {threshold:.0f} or more, tailoring starts by itself. "
                       "This page updates when the score is in.")
        scoring_actions.score_background(rt, job_id, provider, then=then)
        return f"/jobs/{job_id}", message
    if step == "applied":
        when = form.get("applied_on") or utcnow_iso()[:10]
        try:
            # noon UTC, so the date shows as typed in any US time zone
            applied_at = datetime.fromisoformat(when[:10]).replace(hour=12, tzinfo=UTC)
        except ValueError as exc:
            raise ImportProblem("applied date must look like 2026-09-15") from exc
        queries.record_application(
            rt.engine, job_id=job_id, method=form.get("method") or "manual",
            confirmation_ref=(form.get("confirmation_ref") or "").strip() or None, document_ids=[],
            follow_up_at=(applied_at + timedelta(days=FOLLOW_UP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            notes=(form.get("application_notes") or "").strip() or None,
            submitted_at=applied_at.strftime("%Y-%m-%dT%H:%M:%SZ") if when[:10] != utcnow_iso()[:10] else None)
        return "/applications", f"Added “{job.get('title')}” to Applications (follow-up in {FOLLOW_UP_DAYS} days)."
    return f"/jobs/{job_id}", "Saved."
