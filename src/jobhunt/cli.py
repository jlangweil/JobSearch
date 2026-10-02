"""jobhunt command line (SPEC 10)."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import typer

from jobhunt.config import get_config, load_config, set_config

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Personal job-search system.")
db_app = typer.Typer(no_args_is_help=True, help="Database management.")
companies_app = typer.Typer(no_args_is_help=True, help="Target companies.")
facts_app = typer.Typer(no_args_is_help=True, help="Fact bank (data/facts.yaml).")
app.add_typer(db_app, name="db")
app.add_typer(companies_app, name="companies")
app.add_typer(facts_app, name="facts")
llm_app = typer.Typer(no_args_is_help=True, help="Language models.")
app.add_typer(llm_app, name="llm")


@app.callback()
def _main(
    config: Path = typer.Option(None, "--config", help="Path to config.yaml (default: ./config.yaml)."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "transformers", "urllib3", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if config:
        set_config(load_config(config))


def _engine():  # noqa: ANN202
    from jobhunt.db.engine import get_engine

    return get_engine(get_config().paths.db)


# --------------------------------------------------------------------------------------------- db


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Apply migrations and set up the LangGraph checkpoint database."""
    from jobhunt.db import migrate, queries

    cfg = get_config()
    migrate.upgrade(cfg)
    typer.echo(f"database ready: {cfg.paths.db}")
    typer.echo(f"checkpoints ready: {cfg.paths.checkpoints}")
    typer.echo(f"pragmas: {queries.pragma_values(_engine())}")


@db_app.command("compact")
def db_compact() -> None:
    """Reclaim disk space (VACUUM). Run while no discovery run is in progress."""
    from jobhunt.db import queries

    cfg = get_config()
    before = cfg.paths.db.stat().st_size
    queries.compact(_engine())
    after = cfg.paths.db.stat().st_size
    typer.echo(f"{cfg.paths.db}: {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB")


# --------------------------------------------------------------------------------------------- companies


@companies_app.command("sync")
def companies_sync() -> None:
    """Upsert companies from config.yaml; companies removed from config are deactivated."""
    from jobhunt.services.companies import sync_config_companies

    result = sync_config_companies(_engine(), get_config())
    typer.echo(f"synced {result['synced']} companies {result['by_ats']}; deactivated {result['deactivated']}")


@companies_app.command("probe-workday")
def companies_probe_workday(
    url: str = typer.Argument(..., help="e.g. https://pru.wd5.myworkdayjobs.com/Careers"),
) -> None:
    """List a Workday tenant's public career sites (from robots.txt) as config-ready board tokens."""
    from jobhunt.sources import workday

    extra = get_config().sources.get("workday")
    extra = (extra.model_extra or {}) if extra else {}
    for s in workday.discover_sites(url, category_patterns=extra.get("category_patterns",
                                                                     workday.DEFAULT_CATEGORY_PATTERNS),
                                    country_patterns=extra.get("country_patterns", workday.DEFAULT_COUNTRY_PATTERNS)):
        typer.echo(f"  board_token: {s['board_token']:<60} postings: {s['total']}")
        for note in s["facets"]:
            typer.echo(f"      facet {note}")


@companies_app.command("probe-successfactors")
def companies_probe_sf(domain: str = typer.Argument(..., help="e.g. careers.swissre.com")) -> None:
    """Check that a domain is a SuccessFactors career site the adapter can read."""
    import httpx

    from jobhunt.sources.base import default_client
    from jobhunt.sources.successfactors import parse_board_token, parse_listing

    host = parse_board_token(domain)
    try:
        page = default_client().get(f"https://{host}/search/?q=&sortColumn=referencedate&sortDirection=desc").text
    except httpx.HTTPError as exc:
        typer.echo(f"request failed: {exc}")
        raise typer.Exit(1) from exc
    rows, total = parse_listing(page)
    ok = bool(rows)
    typer.echo(f"{host}: {'OK' if ok else 'NOT a readable SuccessFactors site'} — {len(rows)} postings on page 1, "
               f"total {total}")
    raise typer.Exit(0 if ok else 1)


# --------------------------------------------------------------------------------------------- run / score


@app.command("run")
def run(
    sources: str = typer.Option(None, "--sources", help="Comma-separated subset, e.g. workday,successfactors"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fetch + normalize + prefilter only. No writes, no LLM."),
    limit: int = typer.Option(None, "--limit", help="Max jobs to score this run (default scoring.max_per_run)."),
    resume: int = typer.Option(None, "--resume", help="Resume a failed run id from its last checkpoint."),
    companies: str = typer.Option(None, "--companies",
                                  help="Only these companies (comma-separated, name substring), for quick tests."),
    no_score: bool = typer.Option(False, "--no-score", help="Fetch, filter and save, but don't score (free)."),
    stored: bool = typer.Option(False, "--stored", help="Don't fetch; score jobs already saved and waiting."),
) -> None:
    """Run discovery: ingest -> normalize -> dedupe -> prefilter -> ghost -> score."""
    cfg = get_config()
    names = [s.strip() for s in sources.split(",")] if sources else None
    only = [s.strip() for s in companies.split(",") if s.strip()] if companies else None
    if dry_run:
        from jobhunt.db.engine import iso_shift
        from jobhunt.discovery.prefilter import title_reason
        from jobhunt.discovery.service import dry_run as do_dry_run
        from jobhunt.sources.registry import build_sources

        try:
            srcs = build_sources(cfg, names, engine=None, title_gate=lambda t: title_reason(t, cfg) is None,
                                 only_companies=only)
        except ValueError as exc:
            typer.echo(str(exc))
            raise typer.Exit(2) from exc
        do_dry_run(cfg, srcs, iso_shift(-cfg.filters.max_age_days), echo=typer.echo)
        return

    from jobhunt.db.locks import LockHeld
    from jobhunt.graphs.discovery import graph as dg
    from jobhunt.graphs.runtime import init_runtime

    if cfg.scoring.provider == "anthropic" and not cfg.secrets.ANTHROPIC_API_KEY and not no_score:
        typer.echo("warning: ANTHROPIC_API_KEY is not set — the run will stop at the score step. Add it to .env and "
                   "resume with `jobhunt run --resume <id>` (nothing already fetched is fetched again).")
    rt = init_runtime(cfg)
    from jobhunt.services.companies import sync_config_companies

    sync_config_companies(rt.engine, cfg)
    try:
        if resume:
            dg.prepare_resume(rt, resume)
            run_id, initial = resume, None
            typer.echo(f"resuming run {run_id} from its last checkpoint")
        else:
            run_id, initial = dg.start_run(rt, trigger="cli", sources=names, max_to_score=limit, companies=only,
                                           skip_fetch=stored, skip_score=no_score)
            typer.echo(f"started run {run_id}")
    except LockHeld as exc:
        typer.echo(f"refused: a discovery run is already in progress ({exc})")
        raise typer.Exit(2) from exc
    except dg.RunNotResumable as exc:
        typer.echo(str(exc))
        raise typer.Exit(2) from exc
    try:
        result = dg.execute(rt, run_id, initial)
    except Exception as exc:
        typer.echo(f"run {run_id} failed: {exc}\nresume with: jobhunt run --resume {run_id}")
        raise typer.Exit(1) from exc
    typer.echo(f"run {run_id} complete: {json.dumps(result.get('counts', {}))}")
    for e in result.get("errors", [])[:20]:
        typer.echo(f"  error [{e.get('source')}/{e.get('node')}]: {e.get('message')}")


@app.command("score")
def score(
    job_id: int = typer.Option(..., "--job-id"),
    explain: bool = typer.Option(False, "--explain", help="Print the exact prompt sent to the model."),
    dry_run: bool = typer.Option(False, "--dry-run", help="With --explain: print the prompt only, call no LLM."),
    provider: str = typer.Option(None, "--provider", help="anthropic | local — override scoring.provider once."),
) -> None:
    """Re-score one job (adds a new job_scores row)."""
    from jobhunt.db import queries
    from jobhunt.discovery import scoring, service
    from jobhunt.graphs.runtime import init_runtime
    from jobhunt.sources.registry import is_truncated

    rt = init_runtime(get_config())
    job = queries.get_job(rt.engine, job_id)
    if job is None:
        typer.echo(f"job {job_id} not found")
        raise typer.Exit(1)
    if explain:
        typer.echo(scoring.explain(job, rt.cfg, ghost=service.run_ghost_for(rt.engine, rt.cfg, job),
                                   background=service.candidate_background(rt.engine, rt.cfg),
                                   calibration=service.calibration(rt.engine),
                                   truncated=is_truncated(job)))
    if dry_run:
        return
    row = service.rescore_one(rt.engine, rt.cfg, rt.llm(provider), job_id)
    typer.echo(json.dumps(row, ensure_ascii=False, indent=2))


@llm_app.command("test")
def llm_test() -> None:
    """Check the local model server and make one small structured call."""
    from jobhunt.db.engine import get_engine
    from jobhunt.llm.client import LocalLLM, check_local_model

    cfg = get_config()
    typer.echo(f"testing {cfg.local_llm.model} via {cfg.local_llm.api} at {cfg.local_llm.base_url} …")
    result = check_local_model(LocalLLM(get_engine(cfg.paths.db), lambda: cfg.local_llm))
    typer.echo(("OK: " if result["ok"] else "FAILED: ") + result["message"])
    raise typer.Exit(0 if result["ok"] else 1)


# --------------------------------------------------------------------------------------------- serve


@app.command("serve")
def serve() -> None:
    """Start the web app (and the scheduler, if schedule.enabled)."""
    import uvicorn

    from jobhunt.web.app import create_app

    cfg = get_config()
    if cfg.web.host not in ("127.0.0.1", "localhost") and not cfg.secrets.WEB_PASSWORD:
        typer.echo("refusing to bind a non-localhost host without WEB_PASSWORD set in .env")
        raise typer.Exit(2)
    uvicorn.run(create_app(cfg), host=cfg.web.host, port=cfg.web.port, log_level="info")


# --------------------------------------------------------------------------------------------- facts


@facts_app.command("sync")
def facts_sync() -> None:
    """Validate data/facts.yaml, embed, and load into the database (also loads the answers bank)."""
    from jobhunt.embeddings import get_embedder
    from jobhunt.facts import bank

    cfg = get_config()
    try:
        n_facts, n_answers = bank.sync(_engine(), cfg, get_embedder(cfg.embeddings.model))
    except bank.FactBankError as exc:
        typer.echo(f"facts.yaml is invalid:\n{exc}")
        raise typer.Exit(1) from exc
    typer.echo(f"synced {n_facts} facts and {n_answers} answers from {cfg.paths.facts}")


@facts_app.command("lint")
def facts_lint() -> None:
    """Flag unverifiable metrics, thin tags, and structural problems in facts.yaml."""
    from jobhunt.facts import bank

    cfg = get_config()
    try:
        issues = bank.lint(bank.load_yaml(cfg.paths.facts))
    except bank.FactBankError as exc:
        typer.echo(f"facts.yaml is invalid:\n{exc}")
        raise typer.Exit(1) from exc
    for i in issues:
        typer.echo(f"  [{i['severity']}] {i['fact_id']}: {i['message']}")
    typer.echo(f"{len(issues)} issue(s)")


# --------------------------------------------------------------------------------------------- export / backup / stats


@app.command("export")
def export(
    status: str = typer.Option("scored", "--status", help="Job status, or 'any'."),
    min_score: float = typer.Option(None, "--min-score"),
    out: Path = typer.Option(None, "--out"),
) -> None:
    """Export the queue (filtered) to .xlsx."""
    from jobhunt.db import queries
    from jobhunt.db.engine import utcnow_iso
    from jobhunt.db.export import write_xlsx

    cfg = get_config()
    rows, _ = queries.list_queue(_engine(), queries.QueueFilters(status=status, min_score=min_score), paginate=False)
    out = out or cfg.paths.exports / f"jobhunt-export-{utcnow_iso()[:10].replace('-', '')}.xlsx"
    path = write_xlsx(rows, out)
    typer.echo(f"wrote {len(rows)} rows to {path}")


@app.command("backup")
def backup(out: Path = typer.Option(None, "--out")) -> None:
    """Online, consistent backup via VACUUM INTO."""
    from jobhunt.db import queries
    from jobhunt.db.engine import utcnow_iso

    cfg = get_config()
    out = out or cfg.paths.exports / f"jobhunt-backup-{utcnow_iso()[:10].replace('-', '')}.db"
    try:
        queries.vacuum_into(_engine(), out)
    except FileExistsError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"backup written to {Path(out).resolve()}")


@app.command("stats")
def stats() -> None:
    """Counts by status and source, recent runs, LLM spend."""
    from jobhunt.db import queries

    engine = _engine()
    typer.echo("jobs by status:")
    for k, v in sorted(queries.status_counts(engine).items()):
        typer.echo(f"  {k:<14} {v}")
    typer.echo("jobs by source:")
    for k, v in sorted(queries.source_counts(engine).items()):
        typer.echo(f"  {k:<14} {v}")
    typer.echo("recent runs:")
    for r in queries.list_runs(engine, limit=5):
        typer.echo(f"  #{r['id']} {r['status']:<9} {r['started_at']} {json.dumps(r['counts'])}")
    typer.echo(f"LLM spend to date: ${queries.total_spend(engine):.4f}")


if __name__ == "__main__":
    app()
