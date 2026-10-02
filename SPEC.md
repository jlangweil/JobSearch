# jobhunt — build specification

A personal job-search system. It discovers roles from job boards, scores them against a
profile, and walks shortlisted roles through a tailoring workflow with a human in the loop at
every meaningful decision.

> **Amendments (2026-09-14).** Sections marked **[Amendment]** were added after the original
> contract. The two owner-requested changes are **A1: every resume must be ATS-compliant** (§1.8,
> §7.6) and **A2: Workday and SAP SuccessFactors are the most important sources**, targeting
> insurance, health and tech employers (§5.2). The remaining amendments record clarifications the
> build needed and are called out where they occur.

**Runtime shape:**

- **SQLite** is the database. Two files, no server, no service, nothing to install.
- **LangGraph** owns all workflow. Discovery is a graph. The per-job application workflow is a
  graph with real human-in-the-loop interrupts, checkpointed to disk.
- **FastAPI + Jinja + HTMX** is the interface — a server-rendered web app at
  `http://localhost:8000` with real, bookmarkable URLs.
- **Windows 11**, running natively. No Docker, no WSL, no Node.

This document is the contract. Build to it. Where it says MUST, it is not a suggestion.
Where it says LATER, do not build it yet.

---

## 1. Design decisions (do not relitigate)

**1.1 Two graphs, different jobs.**

- `discovery_graph` — batch, unattended, no interrupts. Ingest → normalize → dedupe →
  prefilter → ghost → score. LangGraph earns its place through checkpointing: a run that dies
  partway resumes from the last completed node instead of re-fetching and re-scoring.
- `application_graph` — one thread per job, long-lived, full of interrupts. Requirements →
  facts → resume draft → **review** → cover letter → **review** → render → **approve** →
  apply → track. A thread can sit interrupted for days.

Triage (shortlist / reject / snooze) is **not** a graph. It is a database state transition
driven by a form POST. Do not model it as one.

**1.2 All model calls are server-side.** Scoring uses a Haiku-class model in batch. Drafting
uses a Sonnet-class model. Both go through `langchain-anthropic` so LangGraph gets native
structured output.

**[Amendment] Local models.** Scoring (and, experimentally, tailoring) may instead use a model served on the same
machine: `scoring.provider` / `tailoring.provider` = `anthropic` | `local`, with `local_llm` describing the server
(`api: ollama` → `/api/chat` with a JSON-schema `format`, `think: false`, explicit `num_ctx`; `api: openai` →
`/v1/chat/completions` with `response_format: json_schema`). Default: Qwen3-8B in Ollama. Calls go through
`llm/client.py` (`RoutingLLM` → `AnthropicLLM` | `LocalLLM`) over `httpx` (no extra dependency) and are logged to
`llm_calls` at $0 with model `local:<name>`. Responses are validated against the same pydantic schema and pass through
the same code-level enforcement (evidence substring check, 69 cap, Python-computed total). A prompt that fills the
context window is rejected rather than scored truncated. A run fails once, before scoring, if the configured model is
unreachable. `job_scores.model` records the provider, so scores from both can be compared per job.

**1.3 Nothing reaches a document that isn't traceable to a fact.** Resume bullets are
assembled from a curated fact bank. Every bullet MUST carry the ID of the fact it came from,
and a validation node MUST reject any number or percentage in a bullet that does not appear in
the referenced fact. On rejection the graph loops back to drafting with the errors in state,
up to `max_draft_attempts` (default 3), then interrupts and asks the user. This is enforced in
code, not by prompting.

**1.4 Every meaningful view has a URL.** Filters, sort, and pagination live in the query
string. `/queue?min_score=75&remote=remote&sort=posted` is a bookmark. Actions are POST →
redirect → GET. No client-side routing, no SPA, no build step.

**1.5 SQLite is the store; spreadsheets are an output.** The database is the source of truth.
Any view can be exported to `.xlsx` for browsing or pasting elsewhere. Nothing reads back from
a spreadsheet.

**1.6 Deep links before auto-apply.** Phases 1–3 produce documents and an application URL with
a checklist. Browser automation is Phase 4, optional, and never submits.

**1.7 Source adapters are isolated.** One adapter per source behind a common interface. An
adapter that throws records the failure in the run record and the graph continues.

**1.8 [Amendment A1] Every resume is ATS-compliant.** Applicant tracking systems (Workday,
SuccessFactors, Greenhouse, Taleo, iCIMS…) parse resumes before a human sees them. Drafts MUST use
standard section headings, a single column of plain text, consistent role headings and dates, no
tables/images/text boxes/columns/header-footer content/special symbols, and MUST be rendered to a
`.docx` that passes a structural ATS check. Like 1.3, this is enforced by the validator and the
renderer, not by prompting. Full rules in §7.6.

---

## 2. Stack

| Concern | Choice |
|---|---|
| Language | Python 3.11+ |
| Package/venv | `uv` |
| Database | SQLite (stdlib `sqlite3`), WAL mode |
| DB access | SQLAlchemy 2.x Core (not the ORM) + Alembic with `render_as_batch=True` |
| Workflow | `langgraph` + `langgraph-checkpoint-sqlite` (`SqliteSaver`) |
| LLM | `langchain-anthropic` (`ChatAnthropic`), structured output via `.with_structured_output()` |
| Web | `fastapi`, `jinja2`, `uvicorn`, `htmx` 2.x vendored into `static/` |
| CSS | Tailwind Play CDN (localhost only). Swap to a built stylesheet if ever exposed. |
| HTTP client | `httpx` |
| Embeddings | `BAAI/bge-small-en-v1.5` (local, free, 384-dim). **[Amendment]** computed with `transformers` + `torch` (CLS pooling + L2 normalize, identical to the model's sentence-transformers config) instead of the `sentence-transformers` package, which pulls in SciPy/scikit-learn whose unsigned compiled modules Windows Smart App Control blocks |
| Vector search | `numpy` brute-force cosine. No index. See §4.3. |
| Scheduling | `apscheduler`, in-process |
| Spreadsheet export | `openpyxl` |
| Doc rendering | Markdown → `.docx` via `python-docx`; `.pdf` via LibreOffice, path from config |
| CLI | `typer` |
| Config | `config.yaml` + `.env` via `pydantic-settings` |
| Timezones | `tzdata` (required — Windows ships no zoneinfo database) |
| Testing | `pytest`, recorded JSON fixtures per adapter |
| Lint | `ruff` |
| **[Amendment]** implied by the above | `rapidfuzz` (§6.3 title similarity), `pyyaml` (config/facts YAML), `python-multipart` (FastAPI form POSTs) |

Explicitly **not** used: Postgres, pgvector, Docker, Redis, Celery, React, Node, Streamlit, SciPy, scikit-learn,
`uvloop` (no Windows wheels), FAISS (numpy is sufficient at this scale), pandas, BeautifulSoup
(HTML is parsed with the stdlib `html.parser`).

Structured output uses Claude's native JSON-schema mode (`method="json_schema"`), which is compatible
with the adaptive thinking that current Sonnet-class models run by default.

### 2.1 Windows 11 — platform requirements

The app runs natively on Windows. These are not refinements; getting them wrong produces bugs
that only appear on real data, usually a week in.

**Encoding.** Windows defaults to cp1252, and job descriptions are full of en dashes, curly
quotes, and non-Latin characters that will raise `UnicodeDecodeError` or silently mangle.

- Every `open()` MUST pass `encoding="utf-8"` explicitly. No exceptions — YAML, prompt
  templates, fixtures, log files, exports.
- Set `PYTHONUTF8=1` in the environment that launches the app, and document it.
- `subprocess` calls MUST pass `encoding="utf-8"` and `errors="replace"`.

**Paths.**

- `pathlib.Path` everywhere. No string concatenation with separators, ever.
- Config values naming a path accept either separator and resolve to absolute at load.
- Database files default to `./data/`, resolved absolute at startup.

**Output filenames.** `output/<slug>-<title>-<jobid>/` needs a sanitizer in
`tailor/render.py`:

- Strip `< > : " / \ | ? *` and control characters.
- Strip trailing dots and spaces (Windows silently rejects these).
- Rename reserved device names: `CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`,
  case-insensitive, with or without extension.
- Truncate each component to 64 chars; full path MUST stay under 240 characters. Assume long-
  path support is off.
- Unit-test with a job titled `Sr. Architect: API/Integration (Remote) ` — a real-shaped title
  that breaks all three rules at once.

**Process model.**

- Default asyncio loop. No `uvloop`.
- APScheduler uses `ZoneInfo` from `tzdata`. A bare lookup raises on Windows.
- Background work is `ThreadPoolExecutor`. No `fork`-based multiprocessing, no `os.fork`, no
  Unix-only signal handlers. **[Amendment]** Lock-holder liveness checks use `OpenProcess` via
  `ctypes`, never `os.kill(pid, 0)` (which terminates the process on Windows).

**Git.** Ship `.gitattributes` with `* text=auto eol=lf`. CRLF in fixtures or prompt templates
breaks the substring check in evidence validation.

**File locks.** Word holds an exclusive lock on open `.docx` files. Catch `PermissionError`
around rendering and return a clear message, not a traceback.

---

## 3. Layout

```
jobhunt\
  pyproject.toml
  config.yaml
  .env.example
  .gitattributes
  alembic\
  data\
    jobhunt.db                 # application database
    checkpoints.db             # LangGraph checkpoints (separate file, see 4.2)
    facts.yaml                 # yours; copied from facts.example.yaml
    facts.example.yaml
    resume_template.docx
  output\
    <company>-<title>-<jobid>\
  exports\
  src\jobhunt\
    config.py
    embeddings.py              # [Amendment] embedder protocol + transformers/torch impl
    db\
      engine.py                # engine factory, PRAGMAs, connection lifecycle, timestamp helpers
      models.py                # SQLAlchemy Table objects
      queries.py               # ALL SQL lives here
      vectors.py               # BLOB <-> numpy, cosine search
      locks.py                 # cross-process advisory locks
      export.py                # xlsx writer
      migrate.py               # [Amendment] programmatic alembic upgrade
    sources\
      base.py                  # JobSource protocol + RawJob
      normalize.py             # [Amendment] pure RawJob -> job columns
      workday.py               # [Amendment A2] Phase 1, priority
      successfactors.py        # [Amendment A2] Phase 1, priority
      greenhouse.py
      lever.py
      ashby.py                 # Phase 2
      adzuna.py                # Phase 2
      email_alerts.py          # Phase 4
      registry.py
    discovery\                 # [Amendment] business logic for discovery nodes
      prefilter.py  dedupe.py  ghost.py  scoring.py  service.py
    graphs\
      discovery\  state.py nodes.py graph.py
      application\  state.py nodes.py graph.py
      runtime.py               # checkpointer, thread helpers, background executor
    llm\
      client.py                # ChatAnthropic factories, cost logging
      schemas.py               # pydantic models for structured outputs
      prompts\                 # versioned .md templates
    facts\
      bank.py
    tailor\
      validate.py              # facts + ATS validation
      drafting.py              # [Amendment] LLM drafting steps
      render.py
    apply\
      instructions.py
      answers.py
      browser.py               # Phase 4
    services\                  # [Amendment] triage, workflow, runs, search, settings, scheduler
    web\
      app.py  common.py
      routes\  queue.py jobs.py workflow.py applications.py facts.py runs.py settings.py
      templates\  base.html _nav.html queue.html search.html job_detail.html workflow.html document.html
                  applications.html facts.html runs.html run_detail.html settings.html
        fragments\  queue_rows.html job_actions.html run_progress.html draft_panel.html validation_errors.html
      static\  htmx.min.js app.css
    cli.py
  tests\
    fixtures\
```

---

## 4. Database

### 4.1 Engine configuration

`db/engine.py` creates the engine once at startup:

```python
engine = create_engine(
    f"sqlite+pysqlite:///{db_path}",
    connect_args={"check_same_thread": False, "timeout": 30},
    future=True,
)
```

On every connection, via a `connect` event listener, in this order:

```sql
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 10000;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;
```

WAL is what makes concurrent reads during a discovery run work. `busy_timeout` is what stops
`database is locked` from surfacing as an error during the brief moments two writers collide.
Both are mandatory.

**Write discipline.** SQLite allows one writer at a time. Keep write transactions short and
single-purpose. The scoring node commits per job, never in one batch transaction across 150
jobs. Never hold a transaction open across an LLM call or an HTTP request.

### 4.2 Two database files

- `data/jobhunt.db` — everything in §4.4 onward.
- `data/checkpoints.db` — LangGraph's `SqliteSaver`, created by its own `.setup()`.

Separate files, deliberately. A discovery run writes checkpoints continuously; keeping that
traffic out of the application database means the queue page never waits behind it. There is
no cross-file transactional consistency and none is needed.

### 4.3 Type conventions

| Concept | Storage | Rules |
|---|---|---|
| Timestamps | `TEXT`, ISO-8601 UTC with `Z` (`2026-09-14T13:45:02Z`) | Always UTC, always the same format so lexicographic comparison equals chronological. Convert to local only in templates. Helpers `utcnow_iso()` and `parse_iso()` in `db/engine.py`; nothing else parses timestamps. |
| Booleans | `INTEGER` 0/1 | Never `TRUE`/`'true'`. |
| Arrays (tags, ids) | `TEXT` holding a JSON array | Query with `json_each()`. |
| Structured blobs | `TEXT` holding JSON | `evidence`, `gaps`, `counts`, `coverage`, `fact_map`. |
| Embeddings | `BLOB` | float32, L2-normalized on write, `np.asarray(v, dtype=np.float32).tobytes()`. 384 dims = 1536 bytes. |
| Money | `INTEGER` USD | Whole dollars, annualized. |
| Scores | `REAL` for `total`, `INTEGER` for subscores | |

**Vector search** (`db/vectors.py`): load the relevant embeddings into one `numpy` matrix,
compute `matrix @ query` (cosine, since everything is normalized), `argpartition` for top-k.
Scope the load — the 60-day window for dedupe, the fact bank for retrieval — never the whole
table. At 20,000 jobs the full matrix is 30MB and the search is single-digit milliseconds, so
no index is needed and none should be added.

Cache the fact-bank matrix in memory with invalidation on `facts sync` or `/facts` POST. Do
not cache the jobs matrix; it changes every run.

### 4.4 `companies`
`id INTEGER PK AUTOINCREMENT, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE, ats_type TEXT, board_token TEXT, active INTEGER DEFAULT 1, notes TEXT`

`ats_type` one of `greenhouse|lever|ashby|workday|successfactors|other|unknown`
(**[Amendment A2]** adds `successfactors`). Seeded from `config.yaml` via `jobhunt companies sync`.
Board tokens: Greenhouse/Lever/Ashby slug; Workday `<tenant>.<wdN>.myworkdayjobs.com/<site>`;
SuccessFactors the career-site domain.

### 4.5 `jobs`
| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| source | TEXT NOT NULL | adapter name |
| source_job_id | TEXT NOT NULL | |
| company_id | INTEGER FK | nullable for aggregator hits |
| company_name_raw | TEXT NOT NULL | |
| title | TEXT NOT NULL | |
| location_raw | TEXT | |
| remote_type | TEXT | `remote`\|`hybrid`\|`onsite`\|`unknown` |
| employment_type | TEXT | `fte`\|`contract`\|`c2h`\|`unknown` |
| description_text | TEXT NOT NULL | HTML stripped, ≤20k chars; empty for listing-only rows (§5.2) and, unless `filters.keep_filtered_descriptions`, for `filtered_out`/`duplicate` rows (§6.4) |
| url | TEXT NOT NULL | |
| apply_url | TEXT | |
| comp_min, comp_max | INTEGER | |
| comp_source | TEXT | `posted`\|`inferred`\|`none` |
| posted_at | TEXT | ISO-8601 UTC |
| first_seen_at | TEXT NOT NULL | |
| last_seen_at | TEXT NOT NULL | |
| seen_count | INTEGER DEFAULT 1 | ghost signal |
| content_hash | TEXT NOT NULL | sha256 of normalized title+company+description |
| embedding | BLOB | title + first 2000 chars |
| dedupe_group | INTEGER | canonical job id (itself if canonical) |
| status | TEXT NOT NULL DEFAULT 'discovered' | see 4.6 |
| status_reason | TEXT | |
| snooze_until | TEXT | |
| thread_id | TEXT | LangGraph thread for this job |
| updated_at | TEXT NOT NULL | |

`UNIQUE(source, source_job_id)`. Indexes on `status`, `dedupe_group`, `posted_at`, `thread_id`.

### 4.6 Status lifecycle

```
discovered → filtered_out      (prefilter knockout, terminal)
discovered → duplicate         (non-canonical, terminal)
discovered → scored
scored     → shortlisted | rejected | snoozed
snoozed    → scored            (when snooze_until passes)
shortlisted → in_workflow      (application_graph thread started)
in_workflow → ready | abandoned
ready      → applied → responded | closed
ready      → in_workflow       [Amendment] required by approve_and_apply `back_to_resume` (§7.2)
discovered → shortlisted       [Amendment] tailor a job without scoring it (manual jobs, "Tailor without scoring")
discovered | scored → applied  [Amendment] "I already applied" for a job applied to outside the workflow
responded  → closed            [Amendment]
```

All transitions go through validating functions in `db/queries.py`. Invalid transitions raise.

### 4.7 `jobs_fts`
FTS5 virtual table over `title`, `company_name_raw`, `description_text`, using
`content='jobs'` and `content_rowid='id'`. Kept in sync with AFTER INSERT / UPDATE / DELETE
triggers created in the migration. This backs keyword search on `/search` and `/queue?q=`;
semantic search uses the embedding path. Hybrid: union the two result sets, rank by
`0.5 * normalized_bm25 + 0.5 * cosine`.

### 4.8 `job_scores`
One row per attempt, never re-scored in place.

`id, job_id, scored_at, model, rubric_version, total REAL, skill_match, role_fit [Amendment], domain_fit, seniority_fit, comp_fit, logistics_fit (INTEGER 0-100), red_flags TEXT(json), gaps TEXT(json), evidence TEXT(json), rationale TEXT, ghost_score INTEGER, low_confidence INTEGER`

`total` is computed **in Python** from subscores using weights in `config.yaml`, not by the
model, so weights can change without re-scoring. **[Amendment]** `total` is a derived column: a
weights change rewrites `total` on existing rows (subscores are never modified); a re-score is a new row.

### 4.9 `decisions`
`id, job_id, action, reason, total_at_decision REAL, created_at` — every triage action.
Calibration signal.

### 4.10 `facts`
Loaded from `data/facts.yaml` by `jobhunt facts sync`. YAML is the source of truth.

`id TEXT PK (human-readable), kind TEXT, parent_id TEXT, text TEXT, variants TEXT(json), metrics TEXT(json), tags TEXT(json), org TEXT, title TEXT, start_date TEXT, end_date TEXT, embedding BLOB`

`kind` one of `role|achievement|skill|education|cert|summary`.
**[Amendment]** `facts.yaml` also holds `contact:` (name, email, phone, location, linkedin — rendered
into the document body) and `answers:` (loaded into §4.12).

### 4.11 `documents`
`id, job_id, kind (resume|cover_letter), version INTEGER, markdown TEXT, fact_map TEXT(json), fact_ids TEXT(json), coverage TEXT(json), approved INTEGER DEFAULT 0, file_path TEXT, created_at TEXT`

`fact_map` is `[{bullet_index, fact_id}]` (cover letters: `[{paragraph_index, fact_id}]`).
`file_path` holds a JSON array of rendered files (`.docx`, and `.pdf` when available).

### 4.12 `answers`
`id, question_text TEXT, embedding BLOB, answer_text TEXT, kind TEXT, last_used_at TEXT, use_count INTEGER`

### 4.13 `applications`
`id, job_id, method (deep_link|manual|automated), submitted_at TEXT, confirmation_ref TEXT, document_ids TEXT(json), follow_up_at TEXT, outcome (none|rejected|screen|interview|offer), notes TEXT`

### 4.14 `runs`
`id, thread_id TEXT, started_at TEXT, finished_at TEXT, trigger (web|cli|schedule), status (running|complete|failed), sources TEXT(json), counts TEXT(json), errors TEXT(json), current_node TEXT`

`counts`: `{fetched, new, duplicates, filtered, scored, above_threshold}`. `current_node` is
updated by each discovery node so the UI can poll progress. **[Amendment]** The node timeline on
`/runs/{id}` is derived from checkpoint history (each checkpoint marks a node boundary), so no extra table.

### 4.15 `llm_calls`
`id, job_id, purpose, model, prompt_version, input_tokens, output_tokens, cost_usd REAL, latency_ms, created_at`

Cost visibility is a feature. `/settings` shows spend by day.

### 4.16 `locks`
`name TEXT PK, acquired_at TEXT NOT NULL, pid INTEGER NOT NULL, note TEXT`

Cross-process advisory locking (`db/locks.py`). Acquire with an `INSERT` — the primary key
makes it fail if held. Release with `DELETE`. A lock whose `acquired_at` is older than
`lock_stale_minutes` (default 30) may be reclaimed, and reclaiming MUST be logged.
**[Amendment]** A lock whose holder pid no longer exists may also be reclaimed (logged), and
long-running holders refresh `acquired_at` as a heartbeat so they are never treated as stale.

Used for: `discovery_run` (one at a time, whatever started it) and `thread:{thread_id}` (one
in-flight graph execution per job). This works across processes, so running `jobhunt run` from
a terminal while `jobhunt serve` is up is safe — the second one is told a run is already in
progress rather than corrupting state.

### 4.17 Backup and export

Backup is `VACUUM INTO 'path'` — online, consistent, one statement, no file copy race. Expose
it as `jobhunt backup [--out PATH]` and a button on `/settings`. Default target
`exports/jobhunt-backup-YYYYMMDD.db`.

Export is `db/export.py`, `openpyxl`, one sheet per export. `jobhunt export` and
`/queue/export?<same query params>` both produce an `.xlsx` of exactly the current filtered
view. Descriptions are truncated to 32,000 characters on export with a `…` marker, because
Excel's cell limit is 32,767 — the truncation is an export artifact only and never touches
stored data.

---

## 5. Source adapters

### 5.1 Interface

```python
@dataclass(frozen=True)
class RawJob:
    source: str
    source_job_id: str
    company_name: str
    title: str
    location: str | None
    description_html: str
    url: str
    apply_url: str | None
    posted_at: datetime | None
    comp_min: int | None
    comp_max: int | None
    extra: dict

class JobSource(Protocol):
    name: str
    def fetch(self, since: datetime | None) -> Iterator[RawJob]: ...
```

Pure fetch-and-map. No filtering, no DB, no LLM. Rate limit (default 1 req/sec, per-source
configurable). Every adapter ships a recorded fixture in `tests/fixtures/<source>/` and an
offline mapping test. Never write an adapter test that hits the network.

**[Amendment]** Adapters record per-board failures in an `errors` list and continue with the next
board. Query-driven adapters may be *injected* with two callables by the registry (they still never
touch the DB or apply rules themselves):
- `known(source, source_job_id) -> bool` — when true, yield a sighting (`extra.sighting_only`) without
  downloading the posting again; ingest only bumps `last_seen_at`/`seen_count`.
- `title_gate(title) -> bool` — built from the prefilter's title rules; when false, yield a listing-only
  row (`extra.listing_only`, empty description) without downloading the posting. The prefilter still
  records and knocks the row out. This keeps large Workday crawls affordable.

### 5.2 Phase 1

**[Amendment A2] Workday — priority source.** Every `*.myworkdayjobs.com` career site is backed by
a public JSON API:
- list: `POST https://{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` with
  `{"appliedFacets": {}, "limit": 20, "offset": n, "searchText": q}` (limit max 20);
- detail: `GET https://{host}/wday/cxs/{tenant}/{site}{externalPath}`.

Large employers list thousands of postings, so Workday is query-driven: `sources.workday.queries`
(or a company's own `queries`), `max_pages_per_query` pages each, de-duplicated per board. Postings
whose "Posted N Days Ago" is outside the run's `since` window are skipped. Only sites the tenant's
`robots.txt` marks `Allow:` are configured; `jobhunt companies probe-workday <url>` lists them.
`source_job_id` = `<tenant>/<last path segment>`.

Source-side narrowing: one facet request per board reads the tenant's own facets. The adapter applies the
job-category/family facet values matching `sources.workday.category_patterns` (Technology, Information
Technology, Data, Engineering, …) and the country values matching `country_patterns` (United States) to
every search on that board. A tenant without such a facet, or with no matching value, is left unfiltered on
that dimension, so a naming mismatch never silently empties a board. A company may override
`category_patterns` (`[]` disables it). State/city facets are deliberately not used: US remote roles are
listed under arbitrary states.

**[Amendment A2] SAP SuccessFactors — priority source.** Recruiting Marketing / Career Site Builder
sites (e.g. `careers.swissre.com`) have no public JSON API. The adapter reads only robots-allowed paths:
`/search/?q=…&sortColumn=referencedate&sortDirection=desc&startrow=n` for listings (both table and tile
layouts; parse `a.jobTitle-link`) and `/job/<slug>/<id>/` for details (schema.org microdata:
`datePosted`, address; `span.jobdescription`). `/services/` (including RSS) is robots-disallowed and
MUST NOT be used. Results are newest-first, so paging stops at the first posting older than `since`.
`source_job_id` = `<domain>/<id>`. Legacy `career{N}.successfactors.com` portals are out of scope.

**Target employers.** The shipped company list emphasises insurance, health and tech employers
(many headquartered in or near New Jersey), each verified reachable.

**Greenhouse** — `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`.
Public, unauthenticated. Iterate companies where `ats_type='greenhouse'`.

**Lever** — `https://api.lever.co/v0/postings/{token}?mode=json`. Public.
`descriptionPlain` is already usable text.

All are per-company; the company list in `config.yaml` is the input. Ship a starter list of
verified companies as a worked example.

**[Amendment] Phenom and Eightfold (2026-09-15).** `sources/phenom.py`: GET `/{country}/{lang}/search-results` once
per board for `phApp.pageId`, then POST `/widgets` with `ddoKey: refineSearch` (keywords, from, size 50,
`selected_fields` from the site's country/category facets) and `ddoKey: jobDetail` for descriptions. Board token
`host/country/lang`. `sources/phenom.py` never calls `*/apply` or `*/px-widgets` (robots-disallowed).
`sources/eightfold.py`: GET `/api/apply/v2/jobs?domain=&query=&start=&num=10` and `/api/apply/v2/jobs/{id}?domain=`
(robots `Allow: /api/apply`). Board token `host|domain`. Both are query-driven with `known` and `title_gate` like
Workday, with offline fixture tests.

### 5.3 Phase 2

**Ashby** — `https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true`

**Adzuna** — keyed aggregator, query-driven from `config.yaml`. Descriptions are truncated;
set `description_truncated` in `extra` so scoring discounts confidence.

### 5.4 Phase 4

**Email alerts** — IMAP against a dedicated mailbox receiving LinkedIn/Indeed/Dice alerts.
Extract links; follow only ATS-domain links. Do not scrape LinkedIn directly. Source name
`email:<provider>`.

---

## 6. `discovery_graph`

### 6.1 State

```python
class DiscoveryState(TypedDict):
    run_id: int
    sources: list[str]
    since: str | None                # ISO-8601 UTC
    fetched_job_ids: list[int]
    candidate_job_ids: list[int]
    counts: dict[str, int]
    errors: list[dict]               # {source, node, message, traceback_head}
    max_to_score: int
    ghost_scores: dict[str, int]     # [Amendment] ghost node -> score node, keyed by str(job_id)
```

State MUST stay JSON-serializable — no datetimes, no numpy arrays, no ORM objects. The
checkpointer serializes it on every node boundary.

### 6.2 Nodes

`ingest` → `normalize` → `dedupe` → `prefilter` → `ghost` → `score` → END

- **ingest** — for each enabled source: fetch, upsert on `(source, source_job_id)`. On
  conflict bump `last_seen_at`/`seen_count`; update `description_text` only if `content_hash`
  changed. Per-source exceptions append to `errors` and the node continues.
  **[Amendment]** The pure parts of normalization (HTML stripping, hash, remote/employment
  derivation, comp parsing) run at ingest, because the raw adapter payload is not persisted and the
  upsert needs `description_text` and `content_hash`.
- **normalize** — slug the company name, link to `companies`, compute and store the embedding.
- **dedupe** — see 6.3.
- **prefilter** — see 6.4. Deterministic, no LLM.
- **ghost** — see 6.5. Deterministic, no LLM.
- **score** — see 6.6. The only LLM node.

Each node MUST update `runs.current_node` and `runs.counts` before returning so the progress
fragment has something to poll.

Compile with `SqliteSaver` against `data/checkpoints.db`. Thread id `run-{run_id}`. A failed
run resumes with `graph.invoke(None, config)`, exposed as a Resume button on `/runs/{id}` and
**[Amendment]** `jobhunt run --resume RUN_ID`. A run left `running` by a dead process is marked
failed (and so becomes resumable) on the next start, resume, or server boot.

### 6.3 Dedupe
Load embeddings for jobs in a rolling 60-day window into one matrix, then:

1. Exact `content_hash` match → same group.
2. Else cosine ≥ 0.93 **and** equal company slug **and**
   `rapidfuzz.token_set_ratio(title) ≥ 85` → same group.
3. Canonical preference: company ATS source > aggregator > email; tie-break earliest
   `posted_at`. A job already past `discovered` is never demoted to `duplicate`.

Non-canonical members get `status='duplicate'` and `dedupe_group` set.

### 6.4 Prefilter
Knock out with a `status_reason` when any config rule fails: incompatible location/remote,
`posted_at` older than `max_age_days`, title matches `title_blocklist`, `employment_type` in
`employment_blocklist`, posted comp max below `comp_floor`, description matches
`dealbreaker_patterns`.

**[Amendment]** Additional config rules: `title_allowlist` (a title must match one, when non-empty);
`employment_type` not in `profile.employment_types`; `local_location_patterns` (a stand-in for commute
radius — hybrid/onsite/unknown-type jobs survive only when their location matches, since nothing is
geocoded; remote jobs survive when `remote` is accepted); `excluded_location_patterns` (e.g. remote-India).

This MUST eliminate the clear majority of ingested rows. If it isn't, the config is wrong, not
the code.

**[Amendment]** Storage: when a job is knocked out or marked duplicate, its `description_text` and `embedding`
are cleared (title, company, location, URLs, status, reason, `content_hash` and `seen_count` stay, so sightings,
dedupe and ghost signals still work). `filters.keep_filtered_descriptions: true` keeps everything.
`jobhunt db compact` reclaims the space. Jobs still `discovered` from an interrupted or failed earlier run are
re-processed by the next run.

**[Amendment] Multi-office locations (2026-09-17).** `prefilter.split_locations` breaks `location_raw` on `;`, `|`,
`•`, `/` and "or", and each office is judged separately: `office_is_local` requires a `local_location_patterns` match
*and*, when the office names a state, membership of `filters.local_states` (so "Newark, DE" and "Madison, WI" fail).
An office matching remote/work-at-home/virtual counts as remote unless it is in an excluded country ("Remote - India").
A posting is knocked out on location only when no office is local and none is an acceptable remote option; the reason
names how many offices were considered. `service.recheck_filtered` re-runs the rules over `filtered_out` rows and
re-opens the ones that now pass (transition `filtered_out -> discovered`); `queries.job_exists` reports a re-opened,
content-less job as unknown so the adapter downloads its posting again.

### 6.5 Ghost score (0–100, higher = more likely stale or fake)
High `seen_count` relative to age; `posted_at` older than 45 days and still listed; same
company + near-identical title in a different dedupe group within 90 days; company slug in the
staffing-agency list; no comp, no team, no named hiring manager.

### 6.6 Scoring node
Only `status='discovered'` survivors, capped at `max_to_score`. Concurrency 5, one job per
request, `ChatAnthropic.with_structured_output(JobScore)`.

System prompt: profile summary, must-haves, nice-to-haves, dealbreakers, rubric definition —
from `config.yaml` and the `summary` facts. User message: the posting.

**[Amendment, rubric v2]** The system prompt also carries the candidate's background from the fact bank,
most recent first: the `scoring.recent_roles_in_detail` (default 2) most recent roles with up to
`max_achievements_per_role` accomplishments each, in `facts.yaml` order; older roles as one line each; then
certifications, skills and education. The rubric weighs the most recent role most heavily, treats a held
certification the posting asks for as strong evidence (and a missing required one as a gap), and adds a sixth
subscore, **`role_fit`** (0–100): how well the job's primary responsibilities match `profile.target_roles` and
`profile.role_preference` — leadership/delivery ownership with limited hands-on coding scores high, primarily
individual-contributor development scores low. `job_scores.role_fit` is added by migration 0002; totals for
rows scored before v2 renormalize over the subscores present.

```python
class Evidence(BaseModel):
    claim: str
    quote: str          # must be a literal substring of the posting
    dimension: str      # [Amendment] which subscore it supports, so the cap below is enforceable

class JobScore(BaseModel):
    skill_match: int; domain_fit: int; seniority_fit: int
    comp_fit: int; logistics_fit: int
    red_flags: list[RedFlag]      # RedFlag = {flag, severity: low|medium|high, quote}
    gaps: list[str]
    evidence: list[Evidence]
    rationale: str      # <= 3 sentences
```

Post-response enforcement in code:
- Evidence quotes MUST be substrings of the posting (whitespace-normalized). Drop any that
  aren't and log it. If more than half are fabricated, set `low_confidence=1`.
- Any subscore ≥ 70 without supporting evidence (for that dimension) is capped at 69.
- `total` computed from configured weights, then penalized by `ghost_score` per
  `config.scoring.ghost_penalty`.

Commit per job. Set `jobs.status='scored'`. A job whose score call fails stays `discovered` and
is retried next run.

**[Amendment] Scoring focus and rubric v3 (2026-09-15).** Config `focus: {boost: [...], avoid: [...], avoid_cap}`.
`focus_fit` (0-100) is computed in code (`discovery/focus.py`): 20, +40 if a boost phrase is in the title, +10 per
further distinct boost phrase in the description (max +40); a title matching an avoid phrase gives 0 and caps the
total at `avoid_cap`. It is stored with `focus_hits` on `job_scores` (migration 0004), weighted like the other
subscores (`WEIGHT_KEYS`), and recomputed for every stored score whenever focus or weights change (and at startup for
rows without it). Rubric v3 adds the focus lists to the prompt, forbids crediting the candidate with anything not
in their background, and adds `Evidence.candidate_quote`: skill_match/domain_fit evidence counts toward the ≥70
threshold only if that quote is found (exact or ≥90 partial fuzzy match) in the candidate background.

**[Amendment] Earlier roles and rubric v4 (2026-09-15).** With rubric v3 the scoring prompt listed roles beyond
`scoring.recent_roles_in_detail` as title lines "weigh far less", so skills used only in earlier roles (e.g. years of
.NET at two earlier employers) were invisible and reported as gaps. `scoring.earlier_roles: full` (default) lists earlier
roles with their accomplishments, and a "Technologies and topics by role" block (top 40 accomplishment tags per
role) is added. Rubric v4 treats earlier-role experience as experience the candidate has ("not recent", never a gap).
`titles` restores the one-line form. Prompt size for the owner's bank: ~6k tokens.

### 6.7 Calibration
Once `decisions` has ≥ 20 rows, the scoring prompt includes up to 8 few-shot examples: the
highest-scored rejects and lowest-scored shortlists, with the user's stated reason. Selection
is deterministic so scores stay reproducible within a `rubric_version`.

---

## 7. `application_graph`

One thread per job, `thread_id = f"job-{job_id}"`, stored on `jobs.thread_id`. Started when a
job is shortlisted. Checkpointed to `data/checkpoints.db`; a thread may sit interrupted
indefinitely.

### 7.1 State

```python
class ApplicationState(TypedDict):
    job_id: int
    emphasis: str | None
    requirements: list[dict]         # {text, kind: must|nice, keywords}
    candidate_facts: list[dict]
    resume_markdown: str | None
    resume_fact_map: list[dict]
    resume_document_id: int | None
    cover_markdown: str | None
    cover_fact_map: list[dict]
    cover_document_id: int | None
    coverage: list[dict]             # {requirement, covered_by, status}
    validation_errors: list[dict]
    draft_attempts: int
    company_notes: str | None
    rendered_paths: list[str]
    stage: str
    # [Amendment] draft_origin (model|user), revision_instructions, gate_message,
    # keyword_coverage, ats_warnings, application_method, confirmation_ref, application_notes
```

### 7.2 Nodes and edges

```
START
  → parse_requirements
  → retrieve_facts
  → draft_resume
  → validate_resume ──fail──> draft_resume      (max draft_attempts, then interrupt)
        │ pass
        ▼
   [INTERRUPT: review_resume]
        ├─ revise   → draft_resume
        ├─ edit     → validate_resume  (a failing hand edit returns to review, never to drafting)
        ├─ abandon  → END (status='abandoned')
        └─ accept   ▼  (refused while validation errors stand)
  → draft_cover_letter
  → validate_cover ──fail──> draft_cover_letter
        │ pass
        ▼
   [INTERRUPT: review_cover]
        ├─ revise → draft_cover_letter
        ├─ edit   → validate_cover
        └─ accept ▼
  → render_documents
  → [INTERRUPT: approve_and_apply]
        ├─ back_to_resume → draft_resume
        └─ confirm ▼
  → record_application → END
```

- **parse_requirements** — one structured LLM call: posting → ordered requirement list.
- **retrieve_facts** — for each requirement, hybrid search over `facts` (cosine over the
  cached fact matrix + tag overlap), top-k, de-duplicated into a ranked candidate set.
  Requirements with no matching fact become coverage gaps. **No LLM call.**
  **[Amendment]** Retrieval ranks achievements; every role, skill, education, cert and summary fact
  is added to the candidate set as the resume skeleton.
- **draft_resume** — Sonnet-class, structured output. Input: requirements, candidate facts
  (text + variants + metrics + tags), format rules from config, `emphasis`, and any
  `validation_errors` from a previous attempt. Output: markdown plus
  `[{bullet_index, fact_id}]`. The prompt MUST state that only supplied facts may be used and
  that every experience bullet needs a fact id.
- **validate_resume** — see 7.3 and 7.6. On failure increments `draft_attempts` and routes back; at
  `max_draft_attempts` it interrupts with the errors so the user decides.
- **draft_cover_letter** — same pattern, looser rules: no bullet requirement, but any claim
  about the user's experience needs a fact id, and company-specific claims must derive from
  `company_notes`.
- **render_documents** — markdown → `.docx` using `data/resume_template.docx` for styles →
  `output/<sanitized>/`. PDF too when `paths.soffice` is set and the binary exists.
  **[Amendment A1]** The rendered `.docx` MUST pass the structural ATS check (§7.6); a template that
  fails it stops rendering with a clear message.
- **record_application** — write `applications`, set `jobs.status='applied'`,
  `follow_up_at = now + 10 days`.

### 7.3 Validation (`tailor/validate.py`)

For a resume draft, MUST:
1. Parse markdown, extract experience bullets in order.
2. Assert every bullet index appears in the fact map.
3. Assert every `fact_id` exists and was in this thread's candidate set.
4. Assert every number, percentage, currency amount, and date range in a bullet appears in the
   referenced fact's `metrics` or `text`. **This is the single most important check in the
   system. Write its tests first.**
5. Return structured errors: `[{bullet_index, bullet_text, problem, detail}]`.

**[Amendment]** Also: numbers in the Summary must appear in some candidate fact; role headings must
match a candidate `role` fact's org and dates; education headings and certifications must match
candidate `education`/`cert` facts; each listed skill must appear in some candidate fact's text or tags.
Years in a bullet may also be supported by the cited fact's own `start_date`/`end_date`.

Never save a partially valid draft. Applies identically to model output and to user hand-edits.
Hand edits carry their fact map inline as `{fact:id}` at the end of each bullet (or paragraph).

### 7.4 Interrupts and resumption

Use `langgraph.types.interrupt()` in the review nodes, resume with `Command(resume={...})`.
Payload shapes, fixed:

- `review_resume`: `{"action": "accept"}` | `{"action": "revise", "instructions": str}` |
  `{"action": "edit", "markdown": str, "fact_map": list}` | `{"action": "abandon"}`
- `review_cover`: same minus `abandon`
- `approve_and_apply`: `{"action": "confirm", "method": str, "confirmation_ref": str|None, "notes": str|None}` | `{"action": "back_to_resume"}`
- `review_ats` **[Amendment A4, 2026-10-01, owner request: "ATS Super Match"]**, only when the workflow was started
  in `ats_super_match` mode, between `retrieve_facts` and `draft_resume`:
  `{"action": "apply", "keywords": list[str], "use_headline": bool, "headline": str}` | `{"action": "skip"}` |
  `{"action": "abandon"}`. The gate offers the posting keywords the fact bank does not support and a headline built
  from the posting title; only what the owner ticks is used. Ticked keywords are kept in the workflow state and
  join that job's candidate facts as one `ats-confirmed-keywords` skill source; they are never written to facts.yaml
  or the facts table. Invariant 1 still holds: nothing reaches a resume unless a fact, or the owner's explicit
  confirmation for that posting, supports it.
  The draft is then held to ATS rules (three pages, bullets per role by recency, strong openers, plain
  characters), keeping major projects with their tech stacks and the biggest measurable wins.
  `automated` mode skips the gate and is unchanged.

### 7.5 Execution model

Graph invocations run in a background `ThreadPoolExecutor` owned by the FastAPI app
(`graphs/runtime.py`). Web handlers never block on a graph step: the handler acquires the
thread lock, submits the work, returns a redirect, and the workflow page polls a fragment
showing `stage` until the next interrupt. A step that raises (e.g. Word holds the file) is shown on
the workflow page with a Retry button that resumes from the last checkpoint.

Concurrency rules:
- One in-flight execution per thread, via `locks` (§4.16).
- Graph worker threads and web request threads share the engine. All writes stay short. No
  transaction spans an LLM call.
- The executor is bounded (default 4 workers) so scoring concurrency can't starve the UI.

### 7.6 [Amendment A1] ATS compliance

Enforced for every resume draft (model or hand edit) by `validate_resume`, and for every rendered
file by `render.check_docx_ats`. Violations are validation errors with the same shape as §7.3.

**Markdown / content rules**
- Section headings only from `tailoring.sections`, spelled exactly `## Summary`, `## Skills`,
  `## Experience`, `## Education`, `## Certifications`, in the configured order, each at most once.
  (`nonstandard_section_heading`, `section_order`, `duplicate_section`)
- No `# Name` header in the draft — name and contact details come from `facts.yaml` and are rendered
  into the document body, never a page header/footer. (`contains_name_header`)
- An Experience section with at least one bullet is required; it contains only `### ` role headings
  and `- ` bullets. (`missing_experience`, `stray_text`, `bullet_outside_role`)
- Role headings are `### Title | Organization | Mon YYYY – Mon YYYY` (or `– Present`), listed in
  reverse-chronological order. (`role_heading_format`, `not_reverse_chronological`)
- Education headings are `### Degree | Institution | YYYY`. (`education_heading_format`)
- Plain text only: no tables, images, HTML, markdown links, code blocks, bold/italic inside bullets,
  emoji, icons or decorative bullets (★ ✓ ➤ • …). (`table`, `image`, `html`, `link`, `code_block`,
  `inline_formatting`, `special_character`)
- At most `max_bullets_per_role` bullets per role and `max_bullet_chars` per bullet.
  (`too_many_bullets`, `bullet_too_long`)

**Rendered `.docx` rules** (`check_docx_ats`)
- Single column; no tables, images/shapes, or text boxes; no text in page headers or footers.
- Built-in `Title`, `Heading 1`, `Heading 2`, `List Bullet`, `Normal` styles, so parsers see structure.
- Standard fonts only (Calibri, Arial, Helvetica, Georgia, Garamond, Cambria, Times New Roman, Verdana,
  Tahoma, Aptos, Book Antiqua, Trebuchet MS); other fonts are reported as warnings.
- File names are `First-Last-Resume.docx` / `First-Last-Cover-Letter.docx`; `.docx` is the primary
  upload format, PDF (with a text layer, via LibreOffice) is optional.

**Keyword coverage** — the workflow shows which requirement keywords the draft contains and which
must-have keywords are missing. This is advisory: a missing keyword is added only by adding a true fact.

**[Amendment A5, 2026-10-02, owner request: "appropriate projects and their keywords ARE included"] Projects.**

Facts:
- `kind: project` is a portfolio project: `title` is its name, `org` its link, `tags` its technologies.
- Its accomplishments are `achievement` facts with the project as `parent_id`.

Writing the section:
- `## Projects` is a standard section (placed by `tailoring.sections`, default after Experience).
- Code writes it into every draft (`tailor/projects.py`); the model is told not to write it.
- A project is included when a tag of the project or of its accomplishments appears in the posting
  (`tailoring.projects.include: relevant | always | never`). Projects are ordered by how many tags match, up to
  `max_projects`.
- Each project gets its best-matching accomplishments, up to `max_bullets_per_project`, in the facts' own wording.
- Each project ends with a `Technologies:` line: its tags, with the posting's keywords first.

Separation from Experience:
- Project accomplishments are never Experience candidates or required facts.
- Project facts do count as experience for the Skills lines and the Summary.

Validation:
- `validate_resume` checks the section against the project facts: `unknown_project`,
  `unsupported_project_bullet`, `unsupported_skill`, `stray_text`. This covers hand edits too.

---

## 8. Web UI

FastAPI + Jinja2 + HTMX, server-rendered, bound to `127.0.0.1`.

### 8.1 Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | redirect to `/queue` |
| GET | `/queue` | scored jobs. Params: `q`, `min_score`, `max_ghost`, `status`, `remote`, `employment`, `company`, `source`, `sort`, `page`. **All filters in the query string.** |
| GET | `/queue/export` | same params, returns `.xlsx` |
| GET | `/jobs/{id}` | detail: description, score breakdown, evidence, gaps, red flags, dedupe siblings, decision history |
| POST | `/jobs/{id}/shortlist` | starts `application_graph`, redirects to workflow (optional `emphasis`, `company_notes`) |
| POST | `/jobs/{id}/reject` | reason required |
| POST | `/jobs/{id}/snooze` | `until` date |
| GET | `/jobs/{id}/workflow` | tailoring workspace |
| POST | `/jobs/{id}/workflow/resume` | posts the `Command(resume=...)` payload for the current gate |
| GET | `/jobs/{id}/workflow/status` | HTMX fragment, polled every 2s while running |
| GET | `/documents/{id}` | rendered markdown + download links |
| GET | `/documents/{id}/files/{n}` | **[Amendment]** download a rendered file (only under `output_root`) |
| GET | `/applications` | tracker. Params: `outcome`, `needs_follow_up` |
| POST | `/applications/{id}/outcome` | record outcome |
| GET | `/search` | `?q=` hybrid FTS5 + cosine across stored jobs |
| GET | `/facts` | browse/search fact bank; `?q=` |
| POST | `/facts` | add a fact — writes `facts.yaml` **and** syncs to DB |
| GET | `/runs` | run history with counts and errors |
| POST | `/runs` | trigger discovery; params `sources`, `max_to_score` |
| GET | `/runs/{id}` | run detail, node timeline, per-source counts, errors |
| GET | `/runs/{id}/progress` | **[Amendment]** HTMX progress fragment, polled every 2s while running |
| POST | `/runs/{id}/resume` | resume a failed run from its last checkpoint |
| GET | `/settings` | config view, scoring weights, LLM spend by day, backup button |
| POST | `/settings/weights` | update weights (recomputes `total` in place, no re-scoring) |
| POST | `/settings/backup` | `VACUUM INTO` an `exports/` file |
| POST | `/runs` | **[Amendment]** also `mode` (`full`\|`fetch`\|`dry`\|`stored`), `industries`, `companies` (slugs or name substrings). `fetch` stops before scoring (`DiscoveryState.skip_score`); `stored` scores waiting jobs of the selected companies only |
| POST | `/runs/{id}/stop` | **[Amendment]** ask a running run to stop at its next check; it becomes failed/resumable |
| GET | `/dryruns/{id}` · `/dryruns/{id}/progress` | **[Amendment]** in-memory dry run (fetch + filter, nothing saved, no LLM) with polled output |
| POST | `/dryruns/{id}/stop` | **[Amendment]** stop a dry run |
| GET | `/companies` | **[Amendment]** companies with job counts; `q`, `industry`, `ats`, `active`; `probe_ats` + `probe_target` checks a career site |
| POST | `/companies` · `/companies/active` · `/companies/sync` | **[Amendment]** add a company / pause-activate / sync — edits `config.yaml` |
| GET/POST | `/settings/config` | **[Amendment]** edit `config.yaml`: full validation, timestamped backup, live reload |
| POST | `/settings/compact` | **[Amendment]** `VACUUM`, refused while any lock is held |
| POST | `/facts/sync` · GET/POST `/facts/edit` | **[Amendment]** sync / edit `facts.yaml` (validated, backed up, synced) |
| POST | `/jobs/{id}/score` · GET `/jobs/{id}/prompt` | **[Amendment]** score one job in the background / show the exact scoring prompt |

Every POST redirects (303) to a canonical GET. **[Amendment]** Exception: a failed validation on the config and facts
editors re-renders the editor (HTTP 422) with the submitted text, so nothing typed is lost; the file is unchanged.

**[Amendment] Operational control from the UI.** Everything the CLI can do is available in the web app. `jobhunt serve`
runs migrations and syncs companies on startup. Discovery runs accept a company/industry selection and a
"score stored jobs only" mode (`DiscoveryState.skip_fetch`). A stop request is a `stop:run-{id}` row in `locks`
(so it works for CLI runs too); ingest, normalize and score check it and raise `RunCancelled`, which marks the run
failed and resumable. Editing companies, config or facts always validates the whole file first, keeps a timestamped
backup under `exports/`, then reloads the running config, re-syncs companies and re-applies the schedule. Changes to
`paths`, `embeddings` or `web` need a restart.

**[Amendment] Schedules (2026-09-15).** Schedules are rows in a `schedules` table (migration 0003), managed on
`/schedules` (list with HTMX on/off switch, new/edit form with live preview, duplicate, delete, Run now, Pause all
stored in `app_state`). Fields: name, enabled, mode (`fetch` = skip_score, `full`, `stored` = skip_fetch), when
(`days` + `times`, `interval` = every N hours between two hours on chosen days, or `cron`), timezone, sources,
industries (resolved to companies when fired), companies, max_to_score, `if_busy` (`wait`: retry every 10 min up to
12 times; `skip`), `catch_up` (misfire grace and a one-off run at startup for a fire missed within 12 h), notes, plus
last_fired_at/last_run_id/last_result. `runs.schedule_id` links runs to their schedule. Triggers are built with
APScheduler day *names*: APScheduler 3 numbers weekdays from Monday = 0, so numeric cron weekdays are converted from
cron meaning (0/7 = Sunday) first. An enabled legacy `schedule:` block in config.yaml is imported once;
`schedule.timezone` remains the default for new schedules.

**[Amendment] App shell (2026-09-15).** Every full page has a grouped sidebar with live counts (`web/shell.py`), a top
bar (running run, search, Local/Claude scoring switch `POST /settings/scoring-provider`, Ctrl+K command palette,
theme), and a `/home` dashboard (`/` redirects there). Dark theme is the default: `static/app.css` re-maps the Tailwind
colour utilities the templates use under `html.dark`, so templates stay theme-agnostic; the choice (dark, light,
system) is kept in localStorage.

**[Amendment] Add a job (2026-09-15).** `/jobs/new` (`services/manual_import.py`): read a posting from a link or
pasted text, review it in a form, save, then a next step: tailor (shortlist → workflow), score, save, or record an
application. Links to known ATSs use the adapters' own JSON APIs/parsers and the adapter's `source`/`source_job_id`
(so later runs see the same job); other links use schema.org `JobPosting` JSON-LD, then page text (refused below
300 characters). LinkedIn, Indeed, Glassdoor and ZipRecruiter URLs are never fetched. Pasted text is stored
verbatim; an `extract_posting` model call (scoring provider) fills only missing title/company/location/pay.
Manually added jobs get `source=manual` unless an ATS was recognised, and while `discovered` carry
`status_reason = 'added manually'`, which excludes them from run leftovers, the prefilter and duplicate marking.
`/jobs/new?replace=<id>` replaces a stored job's content (e.g. an Adzuna snippet) with the full posting.

**[Amendment] Job alert emails (2026-09-16).** `sources/email_alerts.py` reads a mailbox over IMAP (stdlib `imaplib`),
selected `readonly=True` and fetched with `BODY.PEEK[]`, so nothing is marked, moved or deleted. Messages are filtered
by `email.senders` (default: LinkedIn/Indeed/Glassdoor/ZipRecruiter alert addresses) and/or `email.subject_patterns`,
within `email.folder` and `email.max_age_days`. Each posting link in the HTML body becomes a listing-only RawJob with
`source = "email"` and a provider-stable id (`linkedin/<job id>`, `indeed/<jk>`), so repeats are sightings. LinkedIn
and Indeed pages are never fetched. A digest email yields one job per posting, with company (rating stripped), location,
pay (`parse_pay`, hourly kept as an interval) and the teaser line. Alert jobs have only that teaser:
`service.has_posting_text` treats a `LISTING_SOURCES` job shorter than `scoring.min_description_chars` (400) as
unscorable, `run_scoring` skips it (counted as `need_posting_text` on the run) and the queue offers
**Paste the posting**
(`/jobs/new?replace=<id>`). Credentials are `IMAP_HOST`/`IMAP_USER`/`IMAP_PASSWORD` in `.env`, written from
Settings → Job alert emails, which also has a read-only **Test**. `email` counts as an aggregator for dedupe ranking
and is enabled by `email.enabled`, not a `sources:` entry.

**[Amendment] Adzuna from the UI (2026-09-15).** Settings → Adzuna writes `ADZUNA_APP_ID`/`ADZUNA_APP_KEY` to `.env`
and `sources.adzuna` (block style: queries, location, distance_miles, max_days_old, pages, category, salary_min,
exclude_words, title_only) via nested config edits; **Test** makes one 5-result search. The adapter sends
`what`|`title_only`, `where`, `distance` (km), `category`, `salary_min`, `what_exclude`, `sort_by=date`. An enabled
Adzuna without keys is skipped with a warning rather than failing the run's other sources.

**[Amendment] Skills section (2026-09-18).** `parse_requirements.v2` demands keywords that read as skills (complete
noun phrases naming a technology, platform, method, practice or domain), and `drafting._is_skill_phrase` drops bare
filler a parser still emits ("validate", "cutover", "testing", "blockers") unless the posting itself writes the word
as a name or acronym. `draft_resume.v3` removes the "Key skills for this role:" line: the candidate's own skill
groups are kept, ordered so the posting's areas come first, with supported keywords leading each group in the
posting's spelling. The validator adds `skills_line_without_label` — once the section has more than one line, every
line needs its own "Label: …" prefix (a single plain line is still fine).

**[Amendment] Role headings and re-rendering (2026-09-18).** Resume parsers (Workday's included) segment work history
by line, so `render.markdown_to_docx(role_layout="separate_lines")` (the default, `tailoring.role_layout`) writes each
role as employer (Heading 2), job title, then the date range with an ASCII hyphen; `one_line` keeps the old
"Title | Employer | dates" heading. The markdown contract and validator are unchanged. `workflow.rerender_document`
rewrites a stored document's files with the current template and layout without a model call or a new version;
an older version renders to a `-v<N>` filename so the current file is never overwritten.
`POST /documents/{id}/rerender`, from the document page and Resumes & letters.

**[Amendment] Network access (2026-09-18).** `web.host` stays 127.0.0.1 unless Settings → Network access turns it on,
which requires `WEB_PASSWORD` (>= 8 chars, written to .env) and sets `web.host` to 0.0.0.0; `jobhunt serve` already
refuses a non-local host without a password. The page shows the LAN URL and the `netsh advfirewall` command; changes
need a restart.

**[Amendment] Complete, keyword-tailored resumes (2026-09-15).** `tailoring.resume_mode: complete` (default) makes
every achievement a candidate (`bank.retrieve(include_all=True)`, each annotated with `relevance` and `matches`) and
uses prompt `draft_resume.v2`: all roles and achievements, reordered by relevance and reworded in the posting's
terms; a "Key skills for this role" line of posting keywords the facts support (`validate.keyword_support`, spelled as
in the posting, years-of-experience phrases dropped), then every skill group. The validator adds `omitted_fact` /
`repeated_fact` (complete mode, model drafts without revision instructions), and `unsupported_keyword`: a posting
keyword that no fact supports may not appear in the summary, or in a bullet whose own fact lacks it. Skills are
supported when the phrase, or all its (≤4) meaningful words, appear in one fact. A retry after validation errors
sends the rejected draft back to be repaired rather than redrafted. `max_bullets_per_role: 0` means no limit;
`max_bullet_chars: 320`. `focused` keeps the previous behaviour. Drafting facts are re-retrieved on every draft, so
regenerating uses the current fact bank. `tailoring.effort` (low|medium|high, default medium) sets Claude's
`output_config.effort` for tailoring calls; calls with max_tokens above 16k stream. Resume drafts allow 48k tokens.
UI: Settings → Resume tailoring; `/documents` (Resumes & letters); sidebar Tailoring / Ready to apply; Regenerate on
the approve-and-apply gate (`back_to_resume` with instructions).

**[Amendment] Tailoring output budgets (2026-09-15).** Reasoning shares Claude's output budget, so drafting calls
use max_tokens 8000 (requirements), 20000 (resume), 12000 (cover) with a timeout of max(180 s, max_tokens/25). A
reply cut off at the limit raises a clear `LLMOutputError` naming the limit.

**[Amendment] Bulk rescoring (2026-09-15).** `POST /scoring/batches` takes the queue's filters, a provider and a
limit, and scores those jobs in the background (in-memory batch with progress page, Stop, one batch at a time via
the `score:batch` lock). Jobs keep their status; each attempt is a new `job_scores` row. The discovery run page shows
a funnel from `runs.counts` (`processing`, `duplicates`, `filtered`, `left_to_score`, `scored`, remaining). Company names in `--companies` and the run form match an exact slug
first, then fall back to a name substring. HTMX handles in-page swaps — row actions,
progress polling, draft panel — never navigation. When `HX-Request` is present, row actions return
the updated row fragment instead of a redirect.

### 8.2 Pages that matter

**`/queue`** — dense table: score, company, title, location, comp, employment type, age, ghost
badge, one-line rationale. Subscores as a compact bar. Row actions shortlist / reject / snooze
inline via HTMX without losing scroll position. Filters are a plain `method=get` form so
submitting updates the URL. Default: `?status=scored&min_score=70&sort=total`. Export button
carries the current query string.

**`/jobs/{id}/workflow`** — left: requirements with coverage status (and ATS keyword coverage). Right:
current draft in an editable textarea, fact map shown per bullet, click a bullet to see its source fact.
Below: a revise box and the gate buttons for the current interrupt. Validation failures render an error
panel naming the offending bullets and the specific problem.

**`/runs/{id}`** — node timeline with timings, per-source counts, full error list, Resume
button when `status='failed'`.

### 8.3 Conventions

- One `base.html`; everything extends it.
- Fragments in `templates/fragments/`, returned directly when `HX-Request` is present.
- `htmx.min.js` vendored into `static/` — the app works offline.
- No JavaScript beyond HTMX attributes. If something seems to need custom JS, it needs a
  server round-trip instead.
- Real `<label>`s, real buttons, sane tab order.

### 8.4 Auth

None by default; localhost only. If `WEB_PASSWORD` is set in `.env`, enable HTTP Basic on all
routes. Do not expose this publicly without it (`jobhunt serve` refuses a non-localhost host
without it).

---

## 9. Configuration

`config.yaml` — committed, no secrets. See the shipped file for the full, commented example. Shape:

```yaml
profile:
  headline: "..."
  years_experience: 25
  summary_fact_ids: [summary-primary]
  locations: {base: "Springfield, IL", accept: [remote, hybrid], max_commute_miles: 20}
  seniority_band: [lead, principal, staff, architect, manager]
  comp_floor: 150000
  employment_types: [fte]
must_haves: [...]
nice_to_haves: [...]
dealbreakers: [...]
filters:
  max_age_days: 30
  title_blocklist: [...]
  title_allowlist: [...]                 # [Amendment]
  employment_blocklist: [contract, "1099"]
  dealbreaker_patterns: [...]            # regexes
  local_location_patterns: [...]         # [Amendment] commute-area stand-in
  excluded_location_patterns: [...]      # [Amendment]
  staffing_agency_slugs: []
scoring: {model: claude-haiku-4-5-20251001, rubric_version: v1, weights: {...}, ghost_penalty: 0.25,
          shortlist_threshold: 70, max_per_run: 150, concurrency: 5}
tailoring: {model: claude-sonnet-5, max_draft_attempts: 3, max_bullets_per_role: 5, max_bullet_chars: 220,
            sections: [summary, skills, experience, education, certifications],
            facts_per_requirement: 5, fact_match_threshold: 0.6}
embeddings: {model: "BAAI/bge-small-en-v1.5"}
schedule: {discovery_cron: "0 7 * * 1-5", timezone: "America/New_York", enabled: false,
           mode: full|fetch, companies: [], max_to_score: null}   # [Amendment] fetch = skip_score
web: {host: "127.0.0.1", port: 8000, executor_workers: 4}
paths: {db, checkpoints, facts, output_root, exports, resume_template, soffice, max_path_chars: 240,
        lock_stale_minutes: 30}
sources:
  workday: {enabled: true, rate_limit_per_sec: 2, max_pages_per_query: 3, queries: [...]}     # [Amendment A2]
  successfactors: {enabled: true, rate_limit_per_sec: 1, max_pages_per_query: 2, queries: [...]}  # [Amendment A2]
  greenhouse: {enabled: true, rate_limit_per_sec: 1}
  lever: {enabled: true}
  ashby: {enabled: true}
  adzuna: {enabled: false, queries: [...], location: "New Jersey"}
companies:
  - {name: "Prudential", ats_type: workday, board_token: pru.wd5.myworkdayjobs.com/Careers, industry: insurance}
  - {name: "Swiss Re", ats_type: successfactors, board_token: careers.swissre.com, industry: insurance}
  - {name: "Oscar Health", ats_type: greenhouse, board_token: oscar, industry: health}
  # optional per company: queries: [...], active: false
```

`.env`: `ANTHROPIC_API_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `IMAP_HOST`, `IMAP_USER`,
`IMAP_PASSWORD`, `WEB_PASSWORD` (optional). No `DATABASE_URL` — the path is in `config.yaml`.

---

## 10. CLI

```
jobhunt db upgrade                 # migrations + SqliteSaver.setup()
jobhunt db compact                 # [Amendment] VACUUM to reclaim space
jobhunt companies sync
jobhunt companies probe-workday URL          # [Amendment] list a tenant's robots-allowed sites
jobhunt companies probe-successfactors DOMAIN  # [Amendment] check a SuccessFactors career site
jobhunt facts sync                 # yaml -> db, validate, embed
jobhunt facts lint                 # flag unverifiable metrics, thin tags
jobhunt run [--sources a,b] [--companies "A,B"] [--dry-run] [--no-score] [--stored] [--limit N] [--resume RUN_ID]
jobhunt llm test                   # [Amendment] check the local model server and make one structured call
jobhunt score --job-id N --explain [--dry-run] [--provider anthropic|local]  # re-score one job, print prompt + response ([--dry-run]: prompt only, no LLM)
jobhunt serve                      # uvicorn + scheduler
jobhunt export [--status s] [--min-score N] [--out PATH]
jobhunt backup [--out PATH]
jobhunt stats
```

`--dry-run` fetches and normalizes but writes nothing and calls no LLM. It MUST work with no
API key configured — this is how adapters get validated. `--limit` caps how many jobs are scored.

---

## 11. Build phases and acceptance

Build in order. Do not start a phase before the previous one's acceptance passes.

**Phase 1 — discovery to scored queue (headless).**
Config, schema, migrations, engine with PRAGMAs, vectors, locks, **Workday + SuccessFactors**
[Amendment A2] + Greenhouse + Lever adapters, `discovery_graph`, `jobhunt run`, `jobhunt stats`,
`jobhunt backup`.
*Accept when:* a run against the configured boards ingests, dedupes, filters out >70% on
deterministic rules, scores the rest, and a killed mid-run process resumes from its last
checkpoint without re-fetching. All adapter tests pass offline from fixtures. A posting
containing em dashes, curly quotes, and a non-Latin character round-trips intact. A second
`jobhunt run` launched while one is in progress is refused by the lock rather than corrupting
state.

**Phase 2 — web UI for triage.**
FastAPI app, `/queue`, `/jobs/{id}`, `/search`, `/runs`, `/settings`, triage POSTs, run trigger
with progress polling, FTS5 sync triggers, xlsx export. Ashby + Adzuna adapters.
*Accept when:* a run can be started from the browser with live progress **while the queue page
stays responsive and no `database is locked` error occurs**; the queue is filterable entirely
through the URL; `/queue?min_score=80&remote=remote` survives a bookmark and a restart; export
produces a readable `.xlsx`; triage decisions land in `decisions`.

**Phase 3 — application_graph and tailoring.**
Fact bank loader, all application nodes, interrupts, validation, rendering,
`/jobs/{id}/workflow`, documents, applications tracker, answers bank.
*Accept when:* shortlisting starts a thread; the draft appears with a coverage report; a
deliberately fabricated metric is caught by the validator in a test **and** when hand-typed
into the editor; revise and re-draft work; an approved document renders to a clean `.docx`
**that passes the ATS check** [Amendment A1]; an ATS violation (nonstandard heading, table, symbol,
misformatted role heading) is rejected like a fabricated metric; a title containing `:` `/` and a
trailing space produces a valid folder under 240 characters; the thread survives a full app restart
mid-interrupt and resumes correctly.

**Phase 4 — optional automation.**
Email alert ingestion, scheduled runs enabled, Playwright form fill (non-headless by default, stops on captcha
or unmapped field). Per Amendment A3 the fill may be followed by a real submit, gated on `auto_apply.auto_submit`.
*Accept when:* a Workday wizard is driven end to end against the offline tenant fixture — sign-in, My Information,
My Experience (work history typed from the fact bank, not parsed from the resume), Application Questions — and
stops on Review with nothing submitted; an unmapped question stops the run and is recorded for the owner to
answer once; passwords are stored encrypted and never appear in the UI or config.

---

## 12. Non-goals

- No scraping of LinkedIn, Indeed, Glassdoor, or ZipRecruiter. Alerts-to-email only.
- No reading robots-disallowed paths on employer career sites (e.g. SuccessFactors `/services/`).
- ~~No automatic submission of applications, in any phase.~~ **[Amendment A3, 2026-09-18, owner request]**
  Auto-apply (beta, SPEC §11 Phase 4) may submit a Workday application, but only with `auto_apply.auto_submit`
  explicitly turned on; it ships off, and with it off every run fills the form and stops on the Review page.
  A run never solves a captcha, never invents an answer to a question it has no stored answer for, and records
  every field it touched. Everything else in this list stands.
- No multi-user support, no SPA, no Node build step, no deployment story.
- No reading data back from spreadsheets. Export is one-way.
- No resume "optimization" that alters facts, inflates dates, or invents metrics. The
  validator exists to make this structurally impossible, not merely discouraged. ATS compliance
  (§7.6) is about format and honest keyword alignment, never keyword stuffing.

---

## 13. Conventions

- All SQL in `db/queries.py` (Alembic migrations excepted). No SQL strings elsewhere, no ORM
  sessions leaking into graph nodes.
- All timestamp handling through `utcnow_iso()` / `parse_iso()` (and their siblings in
  `db/engine.py`). Nothing else formats or parses one.
- All embedding serialization through `db/vectors.py`. Nothing else touches a BLOB.
- All LLM calls through `llm/client.py`, which logs to `llm_calls` on every call.
- Prompts are versioned files in `llm/prompts/`, loaded by name + version.
- Graph nodes are thin: read state, call one service function, write state. Business logic
  lives in `sources/`, `discovery/`, `facts/`, `tailor/`, `services/`.
- Graph state stays JSON-serializable. No datetimes, no numpy arrays.
- Web handlers do no business logic — call a service, render a template.
- Write transactions are short and single-purpose. Never open one across an LLM or HTTP call.
- `encoding="utf-8"` on every file open and subprocess call.
- `pathlib.Path` for every path; everything written to disk goes through the sanitizer.
- Type hints everywhere, `ruff` clean, no bare `except`.
- Functions that touch the network take an injectable client so tests can pass a fake.
