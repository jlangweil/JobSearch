"""Discovery business logic called by the (thin) discovery graph nodes."""

from __future__ import annotations

import logging
import traceback
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import httpx
from sqlalchemy import Engine

from jobhunt.config import AppConfig
from jobhunt.db import queries, vectors
from jobhunt.db.engine import iso_shift, parse_iso
from jobhunt.discovery import dedupe, ghost, keyword_heat, prefilter, scoring
from jobhunt.embeddings import Embedder
from jobhunt.facts import skills
from jobhunt.llm.client import LLMClient
from jobhunt.sources.base import JobSource
from jobhunt.sources.normalize import normalize_raw, slugify
from jobhunt.sources.registry import is_truncated

log = logging.getLogger(__name__)

DEDUPE_WINDOW_DAYS = 60
GHOST_SIMILAR_WINDOW_DAYS = 90

StopCheck = Callable[[], bool]


class RunCancelled(Exception):  # noqa: N818 - reads naturally in run error messages
    """Raised when the user asked a run to stop. The run is marked failed and can be resumed."""

    def __init__(self) -> None:
        super().__init__("stopped by user — resume to continue from the last checkpoint")


def _check(should_stop: StopCheck | None) -> None:
    if should_stop and should_stop():
        raise RunCancelled()


def _err(node: str, message: str, *, source: str | None = None, exc: BaseException | None = None) -> dict:
    head = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-800:] if exc else ""
    return {"source": source, "node": node, "message": message, "traceback_head": head}


# --------------------------------------------------------------------------------------------- ingest


def ingest(engine: Engine, sources: Sequence[JobSource], since: str | None,
           on_progress: Callable[[dict], None] | None = None,
           should_stop: StopCheck | None = None, *, posted_since: str | None = None
           ) -> tuple[list[int], dict, list[dict]]:
    """`posted_since` (this run only): postings dated before it are left out (not stored, not scored); a later run
    without the date can still pick them up. A posting with no date is kept."""
    since_dt: datetime | None = parse_iso(since)
    ids: list[int] = []
    counts: Counter = Counter()
    errors: list[dict] = []
    for src in sources:
        _check(should_stop)
        n = 0
        try:
            for raw in src.fetch(since_dt):
                n += 1
                if n % 10 == 0:
                    _check(should_stop)
                counts["fetched"] += 1
                rec = normalize_raw(raw)
                if posted_since and rec.get("posted_at") and rec["posted_at"] < posted_since:
                    counts["older_than_date"] += 1
                    continue
                outcome, job_id = queries.upsert_job(engine, rec, sighting_only=rec["sighting_only"],
                                                     listing_only=rec["listing_only"])
                counts[outcome] += 1
                if outcome in ("new", "changed") and job_id is not None:
                    ids.append(job_id)
                if on_progress and n % 25 == 0:
                    on_progress(dict(counts))
        except RunCancelled:
            raise
        except Exception as exc:  # an adapter failure must never stop the run (SPEC 1.7)
            errors.append(_err("ingest", f"{src.name}: {exc}", source=src.name, exc=exc))
        errors.extend(getattr(src, "errors", []) or [])
        log.info("source %s: %d postings", src.name, n)
    # Jobs still 'discovered' from an interrupted or failed earlier run are picked up again, so a posting
    # that was stored but never filtered/scored is not stranded when it comes back as a mere sighting.
    leftovers = set(queries.job_ids_with_status(engine, "discovered")) - set(ids) - queries.manual_job_ids(engine)
    if posted_since and leftovers:  # older waiting jobs stay waiting for a run without the date
        leftovers = {j["id"] for j in queries.get_jobs(engine, sorted(leftovers), columns="id, posted_at")
                     if not j["posted_at"] or j["posted_at"] >= posted_since}
    ids = ids + sorted(leftovers)
    summary = {"fetched": counts["fetched"], "new": counts["new"], "changed": counts["changed"],
               "seen": counts["seen"] + counts["unknown_sighting"]}
    if posted_since:
        summary["older_than_date"] = counts["older_than_date"]
    return ids, summary, errors


def restrict_to_companies(engine: Engine, cfg: AppConfig, job_ids: Sequence[int], wanted: Sequence[str]) -> list[int]:
    """Keep only jobs belonging to the selected companies (slugs or name substrings, as in run selection)."""
    from jobhunt.sources.registry import companies_from_config, match_companies

    names = {c["name"] for c in match_companies(companies_from_config(cfg), wanted)}
    return [j["id"] for j in queries.get_jobs(engine, job_ids, columns="id, company_name_raw")
            if j["company_name_raw"] in names]


# --------------------------------------------------------------------------------------------- normalize


def normalize(engine: Engine, job_ids: Sequence[int], embedder: Embedder, batch_size: int = 32,
              should_stop: StopCheck | None = None) -> int:
    """Company linking + embeddings (the parts of normalization that need the DB or a model)."""
    rows = queries.jobs_missing_embedding(engine, job_ids)
    company_ids: dict[str, int | None] = {}
    done = 0
    for i in range(0, len(rows), batch_size):
        _check(should_stop)
        batch = rows[i: i + batch_size]
        texts = [f"{r['title']}\n{r['company_name_raw']}\n{(r['description_text'] or '')[:2000]}" for r in batch]
        vecs = embedder.embed(texts)
        for r, v in zip(batch, vecs, strict=True):
            slug = slugify(r["company_name_raw"])
            if slug not in company_ids:
                company_ids[slug] = queries.company_id_by_slug(engine, slug)
            queries.update_job_normalized(engine, r["id"], company_id=company_ids[slug], embedding=vectors.to_blob(v))
            done += 1
    return done


# --------------------------------------------------------------------------------------------- dedupe / prefilter


def run_dedupe(engine: Engine, job_ids: Sequence[int], *, keep_content: bool = False) -> int:
    window = queries.window_jobs(engine, iso_shift(-DEDUPE_WINDOW_DAYS))
    marked: list[int] = []
    manual = queries.manual_job_ids(engine)
    for job_id, group, dup in dedupe.plan(job_ids, window):
        dup = dup and job_id not in manual  # a job you added yourself is never hidden as a duplicate
        queries.set_dedupe_group(engine, job_id, group, dup)
        if dup:
            marked.append(job_id)
    if marked and not keep_content:
        queries.strip_job_content(engine, marked)
    return len(marked)


def run_prefilter(engine: Engine, cfg: AppConfig, job_ids: Sequence[int]) -> tuple[list[int], int, Counter]:
    survivors, knocked, filtered, reasons = [], [], 0, Counter()
    manual = queries.manual_job_ids(engine, job_ids)
    for job in queries.get_jobs(engine, job_ids, status="discovered"):
        reason = None if job["id"] in manual else prefilter.knockout_reason(job, cfg)  # you chose manual jobs
        if reason:
            queries.set_job_status(engine, job["id"], "filtered_out", reason)
            filtered += 1
            reasons[reason.split(":")[0]] += 1
            knocked.append(job["id"])
        else:
            survivors.append(job["id"])
    if knocked and not cfg.filters.keep_filtered_descriptions:
        queries.strip_job_content(engine, knocked)
    return survivors, filtered, reasons


def run_ghost(engine: Engine, cfg: AppConfig, job_ids: Sequence[int]) -> dict[str, int]:
    jobs = queries.get_jobs(engine, job_ids, status="discovered")
    names = sorted({j["company_name_raw"] for j in jobs})
    similar = queries.company_titles_since(engine, names, iso_shift(-GHOST_SIMILAR_WINDOW_DAYS))
    by_company: dict[str, list[dict]] = {}
    for s in similar:
        by_company.setdefault(s["company_name_raw"], []).append(s)
    return {
        str(j["id"]): ghost.ghost_score(j, similar_titles=by_company.get(j["company_name_raw"], []),
                                        staffing_slugs=cfg.filters.staffing_agency_slugs)[0]
        for j in jobs
    }


# --------------------------------------------------------------------------------------------- score


def ideal_vector(engine: Engine, cfg: AppConfig):  # noqa: ANN201
    """The ideal job as one normalized vector: the configured role fact and its accomplishments, averaged."""
    import numpy as np

    from jobhunt.db import vectors

    role = cfg.scoring.ideal_job.role
    if not role:
        return None
    rows = [f for f in queries.list_facts(engine, with_embedding=True)
            if (f["id"] == role or f.get("parent_id") == role) and f.get("embedding") is not None]
    vecs = [v for f in rows if (v := vectors.from_blob(f["embedding"])) is not None]
    if not vecs:
        return None
    mean = np.mean(vecs, axis=0)
    norm = float(np.linalg.norm(mean))
    return mean / norm if norm else None


def candidate_background(engine: Engine, cfg: AppConfig) -> dict[str, str]:
    background = scoring.format_background(queries.list_facts(engine, file_order=True), cfg)
    labels = {r["skill"]: r["label"] for r in queries.list_skill_levels(engine)}
    background["skill_levels"] = skills.prompt_text(queries.skill_level_map(engine), labels)
    return background


def calibration(engine: Engine) -> list[dict]:
    if queries.count_decisions(engine) < 20:
        return []
    return queries.calibration_examples(engine, per_side=4)


def run_scoring(engine: Engine, cfg: AppConfig, llm: LLMClient, job_ids: Sequence[int], ghosts: dict[str, int],
                max_to_score: int, on_scored: Callable[[int], None] | None = None,
                should_stop: StopCheck | None = None) -> tuple[int, int, list[dict]]:
    """Scores up to max_to_score 'discovered' jobs with bounded concurrency. The LLM calls run in worker
    threads; each result is committed individually from this thread (never a batch transaction)."""
    # newest first: a run's own finds are scored before an older backlog (e.g. jobs re-opened when the filters
    # changed), so what a run brings in shows up scored when it ends
    waiting = sorted(queries.get_jobs(engine, job_ids, status="discovered"),
                     key=lambda j: (j.get("first_seen_at") or "", j["id"]), reverse=True)
    # Alert-email jobs carry a link and a one-line teaser, not a description; they wait for you to paste the posting
    # instead of being scored on a couple of lines.
    # fill in snippets and missing text first (up to what this run will score), then keep what has a posting
    fillable = [j for j in waiting if has_posting_text(j, cfg) or needs_full_text(j)][:max_to_score]
    filled, fetch_errors = complete_descriptions(engine, cfg, fillable, should_stop=should_stop)
    jobs = [j for j in filled if has_posting_text(j, cfg)]
    if jobs and hasattr(llm, "preflight"):
        llm.preflight("score")  # e.g. local server down: fail the run once, clearly, instead of per job
    background = candidate_background(engine, cfg)
    levels = queries.skill_level_map(engine)
    ideal = ideal_vector(engine, cfg)
    cal = calibration(engine)
    scored = above = 0
    errors: list[dict] = list(fetch_errors)

    def work(job: dict) -> dict:
        return scoring.score_job(job, cfg, llm, ghost=ghosts.get(str(job["id"]), 0), background=background,
                                 calibration=cal, truncated=is_truncated(job), levels=levels, ideal=ideal)

    workers = llm.concurrency("score") if hasattr(llm, "concurrency") else cfg.scoring.concurrency
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(work, j): j for j in jobs}
        for fut in as_completed(futures):
            if should_stop and should_stop():
                for pending in futures:
                    pending.cancel()  # not-yet-started calls are dropped; in-flight ones finish and are discarded
                raise RunCancelled()
            job = futures[fut]
            try:
                row = fut.result()
            except Exception as exc:  # one failed score leaves the job 'discovered' for the next run
                errors.append(_err("score", f"job {job['id']}: {exc}", source=job["source"], exc=exc))
                continue
            queries.record_score(engine, job["id"], row)
            keyword_heat.refresh(engine, cfg)
            scored += 1
            above += int(row["total"] >= cfg.scoring.shortlist_threshold)
            if on_scored:
                on_scored(scored)
    return scored, above, errors


def fetch_full_text_enabled(cfg: AppConfig) -> bool:
    scfg = cfg.sources.get("adzuna")
    return bool(((scfg.model_extra or {}) if scfg else {}).get("fetch_full_description", True))


def needs_full_text(job: Mapping) -> bool:
    """A snippet (Adzuna) or no text at all (a company-board job re-opened after its text was dropped) from a board
    whose posting can be read."""
    from jobhunt.sources import fulltext

    if job.get("source") not in fulltext.SUPPORTED:
        return False
    return is_truncated(job) or not (job.get("description_text") or "").strip()


def complete_descriptions(engine: Engine, cfg: AppConfig, jobs: list[dict], *, client: httpx.Client | None = None,
                          should_stop: StopCheck | None = None) -> tuple[list[dict], list[dict]]:
    """Before scoring: give each job that has only a snippet, or no text, its full posting from its board, and close
    a job the board no longer has. A failed fetch leaves the job as it was. Returns (jobs still open, errors)."""
    from jobhunt.sources import fulltext
    from jobhunt.sources.base import RateLimiter, default_client

    todo = [j for j in jobs if needs_full_text(j)
            and (j.get("source") != "adzuna" or fetch_full_text_enabled(cfg))]
    if not todo:
        return jobs, []
    tokens = queries.board_tokens(engine, [j.get("company_id") for j in todo])
    limiters: dict[str, RateLimiter] = {}
    cache: dict = {}
    own = client is None
    client = client or default_client(timeout=20.0)
    closed: set[int] = set()
    errors: list[dict] = []
    fetched = 0
    try:
        for job in todo:
            if should_stop and should_stop():
                raise RunCancelled()
            source = str(job.get("source"))
            if source not in limiters:
                scfg = cfg.sources.get(source)
                limiters[source] = RateLimiter(scfg.rate_limit_per_sec if scfg else 1.0)
            limiters[source].wait()
            result = fulltext.fetch({**job, "board_token": tokens.get(job.get("company_id"))}, client, cache)
            if result.gone and job.get("status") in ("discovered", "scored", "snoozed"):
                queries.set_job_status(engine, job["id"], "closed", f"posting closed: {result.detail}")
                closed.add(job["id"])
            elif result.text:
                queries.set_full_description(engine, job["id"], result.text)
                job.update(description_text=result.text, full_description=1)
                fetched += 1
            else:  # unclear, or gone while already being tailored or applied for: left to you
                errors.append(_err("full_text", f"job {job['id']}: {result.detail}", source=source))
    finally:
        if own:
            client.close()
    log.info("full postings: %d fetched, %d closed, %d unchanged", fetched, len(closed), len(errors))
    return [j for j in jobs if j["id"] not in closed], errors


def rescore_one(engine: Engine, cfg: AppConfig, llm: LLMClient, job_id: int) -> dict:
    job = queries.get_job(engine, job_id)
    if job is None:
        raise KeyError(f"job {job_id} not found")
    if needs_full_text(job):  # re-scoring a snippet or an empty job: get the full posting first
        complete_descriptions(engine, cfg, [job])
        job = queries.get_job(engine, job_id) or job
    g = run_ghost_for(engine, cfg, job)
    row = scoring.score_job(job, cfg, llm, ghost=g, background=candidate_background(engine, cfg),
                            calibration=calibration(engine), truncated=is_truncated(job),
                            levels=queries.skill_level_map(engine), ideal=ideal_vector(engine, cfg))
    queries.record_score(engine, job_id, row, set_scored=job["status"] == "discovered")
    keyword_heat.refresh(engine, cfg)
    return row


def run_ghost_for(engine: Engine, cfg: AppConfig, job: dict) -> int:
    similar = queries.company_titles_since(engine, [job["company_name_raw"]], iso_shift(-GHOST_SIMILAR_WINDOW_DAYS))
    return ghost.ghost_score(job, similar_titles=similar, staffing_slugs=cfg.filters.staffing_agency_slugs)[0]


def recompute_totals(engine: Engine, cfg: AppConfig) -> int:
    """Weights, focus or skill levels changed: recompute focus_fit, stack-based skill_match and totals for every
    stored score. No LLM calls."""
    updates = []
    derived: dict[int, dict[str, int]] = {}
    levels = queries.skill_level_map(engine)
    ideal = ideal_vector(engine, cfg)
    for r in queries.all_score_subscores(engine):
        analysis = queries.loads(r.get("analysis"), None) or {}
        changes: dict[str, int] = {}
        close = scoring.ideal_fit(r.get("embedding"), ideal, cfg)
        if close is not None and close != r.get("ideal_fit"):
            changes["ideal_fit"] = r["ideal_fit"] = close
        role_fit = scoring.role_fit_from(analysis.get("role"), r.get("title") or "", analysis.get("stack"))
        if role_fit is not None and role_fit != r["role_fit"]:
            changes["role_fit"] = r["role_fit"] = role_fit
        industry = scoring.industry_fit(r.get("company_name_raw"), cfg)
        if industry != r.get("industry_fit"):
            changes["industry_fit"] = r["industry_fit"] = industry
        if analysis.get("skill_from_stack") or analysis.get("competencies"):
            value = scoring.analysis_skill_match(analysis, levels, r.get("title") or "")
            if value is None:
                # the list no longer supports a score (e.g. only categories were listed) and the model's own
                # estimate wasn't kept: treat the old number like any unsupported one until the job is re-scored
                value = min(r["skill_match"], scoring.EVIDENCE_CAP)
            if value != r["skill_match"]:
                changes["skill_match"] = r["skill_match"] = value
        if changes:
            derived[r["id"]] = changes
        fit, hits, cap = scoring.focus_for(r, cfg)
        total = scoring.compute_total({**r, "focus_fit": fit}, cfg.scoring.weights, r.get("ghost_score") or 0,
                                      cfg.scoring.ghost_penalty, cap)
        updates.append((r["id"], total, fit, queries.dumps(hits)))
    queries.update_score_totals(engine, updates, derived)
    keyword_heat.refresh(engine, cfg)  # totals moved: jobs may have crossed the heatmap's line
    return len(updates)


# --------------------------------------------------------------------------------------------- dry run


def dry_run(cfg: AppConfig, sources: Sequence[JobSource], since: str | None,
            echo: Callable[[str], None] = print, should_stop: StopCheck | None = None) -> dict:
    """Fetch + normalize + prefilter in memory. Writes nothing, calls no LLM, needs no API key."""
    since_dt = parse_iso(since)
    summary: dict = {}
    for src in sources:
        _check(should_stop)
        counts: Counter = Counter()
        reasons: Counter = Counter()
        survivors: list[str] = []
        echo(f"[{src.name}] fetching…")
        try:
            for raw in src.fetch(since_dt):
                rec = normalize_raw(raw)
                counts["fetched"] += 1
                if counts["fetched"] % 10 == 0:
                    _check(should_stop)
                if counts["fetched"] % 100 == 0:
                    echo(f"[{src.name}] {counts['fetched']} postings so far…")
                if rec["sighting_only"]:
                    continue
                if rec["listing_only"]:
                    counts["listing_only"] += 1
                reason = prefilter.knockout_reason(rec, cfg)
                if reason:
                    counts["filtered"] += 1
                    reasons[reason.split(":")[0]] += 1
                else:
                    counts["survived"] += 1
                    survivors.append(f"{rec['company_name_raw']} — {rec['title']} ({rec['location_raw']}, "
                                     f"{rec['remote_type']})")
        except RunCancelled:
            raise
        except Exception as exc:  # report and continue with the next source
            counts["exceptions"] += 1
            echo(f"[{src.name}] adapter raised: {exc}")
        for board, notes in (getattr(src, "facet_notes", {}) or {}).items():
            echo(f"[{src.name}] {board}: " + "; ".join(notes))
        errs = getattr(src, "errors", [])
        pct = 100.0 * counts["filtered"] / counts["fetched"] if counts["fetched"] else 0.0
        echo(f"[{src.name}] fetched={counts['fetched']} filtered={counts['filtered']} ({pct:.0f}%) "
             f"survived={counts['survived']} errors={len(errs)}")
        for k, v in reasons.most_common():
            echo(f"    filtered by {k}: {v}")
        for e in errs[:10]:
            echo(f"    error: {e['message']}")
        for s in survivors[:25]:
            echo(f"    + {s}")
        summary[src.name] = {"counts": dict(counts), "reasons": dict(reasons), "errors": errs}
    return summary


def has_posting_text(job: Mapping, cfg: AppConfig) -> bool:
    """False for a job that is only a link (and perhaps a one-line teaser from an alert email): there is nothing to
    score until you paste the posting."""
    from jobhunt.sources.registry import LISTING_SOURCES

    text = (job.get("description_text") or "").strip()
    if job.get("source") in LISTING_SOURCES:
        return len(text) >= cfg.scoring.min_description_chars
    return bool(text)


def count_without_posting_text(engine: Engine, cfg: AppConfig, job_ids: Sequence[int]) -> int:
    rows = queries.get_jobs(engine, job_ids, status="discovered", columns="id, source, description_text")
    return sum(1 for r in rows if not has_posting_text(r, cfg))


REOPENED_REASON = "re-opened: the filters changed"


def recheck_filtered(engine: Engine, cfg: AppConfig, *, apply: bool = True) -> dict:
    """Re-run the knockout rules with the current config, both ways: filtered-out postings that now pass are
    re-opened (their descriptions were dropped, so they are fetched again before scoring), and open ones (waiting or
    scored, not yet acted on) that now fail are filtered out."""
    rows = queries.filtered_out_jobs(engine)
    # A description-based knockout (clearance required, ...) can't be re-judged once the description was dropped:
    # it stays out rather than "passing" for lack of text.
    passing = [r for r in rows if prefilter.knockout_reason(r, cfg) is None
               and not (str(r.get("status_reason") or "").startswith("dealbreaker")
                        and not (r.get("description_text") or "").strip())]
    reopened = queries.reopen_jobs(engine, [r["id"] for r in passing], REOPENED_REASON) if apply else 0
    failing = []
    for status in ("discovered", "scored"):
        for job in queries.get_jobs(engine, queries.job_ids_with_status(engine, status), status=status):
            if reason := prefilter.knockout_reason(job, cfg):
                failing.append((job, reason))
    if apply:
        for job, reason in failing:
            queries.set_job_status(engine, job["id"], "filtered_out", reason)
    return {"checked": len(rows), "now_pass": len(passing), "reopened": reopened, "now_fail": len(failing),
            "removed": len(failing) if apply else 0,
            "removed_sample": [f"{j['title']} — {j['company_name_raw']} ({reason})" for j, reason in failing[:8]],
            "sample": [f"{r['title']} — {r['company_name_raw']} ({r['location_raw'] or 'no location'})"
                       for r in passing[:8]]}
