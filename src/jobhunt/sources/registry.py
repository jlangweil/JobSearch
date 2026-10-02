"""Builds configured source adapters from config.yaml + the `companies` table."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

import httpx
from sqlalchemy import Engine

from jobhunt.config import AppConfig
from jobhunt.db import queries
from jobhunt.sources import adzuna, ashby, eightfold, email_alerts, greenhouse, lever, phenom, successfactors, workday
from jobhunt.sources.base import JobSource, KnownJob, TitleGate
from jobhunt.sources.normalize import slugify

ALL_SOURCES = ("workday", "successfactors", "phenom", "eightfold", "greenhouse", "lever", "ashby", "adzuna", "email")
ATS_SOURCES = {"workday", "successfactors", "phenom", "eightfold", "greenhouse", "lever", "ashby"}
AGGREGATOR_SOURCES = {"adzuna", "email"}
LISTING_SOURCES = {"email"}  # a link plus a teaser line; the posting itself has to be pasted in
TRUNCATING_SOURCES = {"adzuna"}


def is_truncated(job: Mapping) -> bool:
    """A snippet, not the whole posting: an aggregator's text until the full text is fetched or pasted."""
    return job.get("source") in TRUNCATING_SOURCES and not job.get("full_description")

DEFAULT_QUERIES = ["architect", "integration", "api platform", "technical lead"]
log = logging.getLogger(__name__)


def email_account(cfg: AppConfig) -> email_alerts.EmailAccount:
    e = cfg.email
    return email_alerts.EmailAccount(
        host=e.host or cfg.secrets.IMAP_HOST or "", user=cfg.secrets.IMAP_USER or "",
        password=cfg.secrets.IMAP_PASSWORD or "", port=e.port, folder=e.folder or "INBOX",
        senders=list(e.senders) or list(email_alerts.DEFAULT_SENDERS), subject_patterns=list(e.subject_patterns),
        max_messages=e.max_messages, max_age_days=e.max_age_days)


def adzuna_source(cfg: AppConfig, *, client: httpx.Client | None = None) -> adzuna.AdzunaSource:
    scfg = cfg.sources.get("adzuna")
    extra = (scfg.model_extra or {}) if scfg else {}
    return adzuna.AdzunaSource(
        app_id=cfg.secrets.ADZUNA_APP_ID or "", app_key=cfg.secrets.ADZUNA_APP_KEY or "",
        queries=extra.get("queries") or [], location=extra.get("location"),
        max_days_old=int(extra.get("max_days_old", 30)), pages=int(extra.get("pages", 2)),
        distance_miles=extra.get("distance_miles"), category=extra.get("category"),
        salary_min=extra.get("salary_min"), exclude_words=extra.get("exclude_words") or [],
        title_only=bool(extra.get("title_only", False)), client=client,
        rate_limit_per_sec=scfg.rate_limit_per_sec if scfg else 1.0)


def source_rank(source: str) -> int:
    """Dedupe canonical preference: company ATS > aggregator > email (SPEC 6.3)."""
    if source in ATS_SOURCES:
        return 0
    if source in AGGREGATOR_SOURCES:
        return 1
    return 2


def enabled_sources(cfg: AppConfig) -> list[str]:
    names = [name for name in ALL_SOURCES if cfg.sources.get(name) and cfg.sources[name].enabled]
    if cfg.email.enabled and "email" not in names:  # the mailbox is configured under `email:`, not `sources:`
        names.append("email")
    return names


def companies_from_config(cfg: AppConfig) -> list[dict]:
    """Company rows as `companies sync` writes them (also used by --dry-run, which never touches the DB)."""
    rows = []
    for c in cfg.companies:
        rows.append({"name": c.name, "slug": slugify(c.name), "ats_type": c.ats_type, "board_token": c.board_token,
                     "active": 1 if c.active else 0, "notes": c.notes})
    return rows


def match_companies(companies: Sequence[dict], wanted: Sequence[str]) -> list[dict]:
    """Each wanted term selects the company whose slug equals it; failing that, every company whose name contains
    it (case-insensitive). So "salesforce" is Salesforce only, while "lincoln financial" still matches by name."""
    slugs = {c["slug"] for c in companies}
    keep: list[dict] = []
    for term in (w.strip().lower() for w in wanted if w and w.strip()):
        hits = [c for c in companies if c["slug"] == term] if term in slugs else \
            [c for c in companies if term in c["name"].lower()]
        keep.extend(c for c in hits if c not in keep)
    return keep


def build_sources(
    cfg: AppConfig,
    names: Sequence[str] | None = None,
    *,
    engine: Engine | None = None,
    client: httpx.Client | None = None,
    known: KnownJob | None = None,
    title_gate: TitleGate | None = None,
    only_companies: Sequence[str] | None = None,
) -> list[JobSource]:
    names = list(names) if names else enabled_sources(cfg)
    if engine is not None:
        companies = queries.list_companies(engine)
    else:
        companies = [c for c in companies_from_config(cfg) if c["active"]]
    if only_companies:
        companies = match_companies(companies, only_companies)
        if not companies:
            raise ValueError(f"no configured company matches {', '.join(only_companies)}")
        names = [n for n in names if n not in AGGREGATOR_SOURCES]  # aggregators are not company boards
    per_company_queries = {slugify(c.name): c.queries for c in cfg.companies if c.queries}
    per_company_categories = {slugify(c.name): c.category_patterns for c in cfg.companies
                              if c.category_patterns is not None}

    def boards(ats: str) -> list[dict]:
        return [c for c in companies if c["ats_type"] == ats and c.get("board_token")]

    out: list[JobSource] = []
    for name in names:
        scfg = cfg.sources.get(name)
        rate = scfg.rate_limit_per_sec if scfg else 1.0
        extra = (scfg.model_extra or {}) if scfg else {}
        if name == "greenhouse":
            out.append(greenhouse.GreenhouseSource([(c["name"], c["board_token"]) for c in boards(name)],
                                                   client=client, rate_limit_per_sec=rate))
        elif name == "lever":
            out.append(lever.LeverSource([(c["name"], c["board_token"]) for c in boards(name)],
                                         client=client, rate_limit_per_sec=rate))
        elif name == "ashby":
            out.append(ashby.AshbySource([(c["name"], c["board_token"]) for c in boards(name)],
                                         client=client, rate_limit_per_sec=rate))
        elif name == "workday":
            wd_boards = []
            for c in boards(name):
                host, tenant, site = workday.parse_board_token(c["board_token"])
                wd_boards.append(workday.WorkdayBoard(c["name"], host, tenant, site,
                                                      list(per_company_queries.get(c["slug"]) or []),
                                                      per_company_categories.get(c["slug"])))
            out.append(workday.WorkdaySource(
                wd_boards, queries=extra.get("queries") or DEFAULT_QUERIES, client=client, rate_limit_per_sec=rate,
                category_patterns=extra.get("category_patterns", workday.DEFAULT_CATEGORY_PATTERNS),
                country_patterns=extra.get("country_patterns", workday.DEFAULT_COUNTRY_PATTERNS),
                max_pages_per_query=int(extra.get("max_pages_per_query", 3)), known=known, title_gate=title_gate))
        elif name == "successfactors":
            sf_boards = [
                successfactors.SuccessFactorsBoard(c["name"], successfactors.parse_board_token(c["board_token"]),
                                                   list(per_company_queries.get(c["slug"]) or []))
                for c in boards(name)
            ]
            out.append(successfactors.SuccessFactorsSource(
                sf_boards, queries=extra.get("queries") or DEFAULT_QUERIES, client=client, rate_limit_per_sec=rate,
                max_pages_per_query=int(extra.get("max_pages_per_query", 2)), known=known, title_gate=title_gate))
        elif name == "phenom":
            ph_boards = []
            for c in boards(name):
                host, locale = phenom.parse_board_token(c["board_token"])
                ph_boards.append(phenom.PhenomBoard(c["name"], host, locale,
                                                    list(per_company_queries.get(c["slug"]) or []),
                                                    per_company_categories.get(c["slug"])))
            out.append(phenom.PhenomSource(
                ph_boards, queries=extra.get("queries") or DEFAULT_QUERIES, client=client, rate_limit_per_sec=rate,
                category_patterns=extra.get("category_patterns"),
                country_patterns=extra.get("country_patterns", workday.DEFAULT_COUNTRY_PATTERNS),
                max_pages_per_query=int(extra.get("max_pages_per_query", 3)), known=known, title_gate=title_gate))
        elif name == "eightfold":
            ef_boards = []
            for c in boards(name):
                host, domain = eightfold.parse_board_token(c["board_token"])
                ef_boards.append(eightfold.EightfoldBoard(c["name"], host, domain,
                                                          list(per_company_queries.get(c["slug"]) or [])))
            out.append(eightfold.EightfoldSource(
                ef_boards, queries=extra.get("queries") or DEFAULT_QUERIES, client=client, rate_limit_per_sec=rate,
                max_pages_per_query=int(extra.get("max_pages_per_query", 5)), known=known, title_gate=title_gate))
        elif name == "email":
            account = email_account(cfg)
            if not (account.host and account.user and account.password):
                log.warning("job alert emails are enabled but the mailbox isn't set up; skipping them")
                continue
            out.append(email_alerts.EmailAlertSource(account, known=known))
        elif name == "adzuna":
            if not (cfg.secrets.ADZUNA_APP_ID and cfg.secrets.ADZUNA_APP_KEY):
                log.warning("adzuna is enabled but ADZUNA_APP_ID / ADZUNA_APP_KEY are not set; skipping it")
                continue
            out.append(adzuna_source(cfg, client=client))
        else:
            raise ValueError(f"unknown source {name!r}; known: {', '.join(ALL_SOURCES)}")
    return out
