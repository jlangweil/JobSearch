# Working notes for Claude Code

`SPEC.md` is the contract. Read it before writing code. If something in it seems wrong, say so
and stop — do not silently deviate.

**Target: Windows 11, native. SQLite, no database server, no Docker, no Node.** Read SPEC §2.1
(Windows) and §4 (database) before writing any code that touches files, paths, timestamps, or
the database.

## Ground rules

- Build phase by phase (SPEC §11). Do not start a phase until the previous one's acceptance
  criteria pass. Announce which phase you're on.
- No new dependencies beyond SPEC §2 without asking. Specifically excluded: Postgres, pgvector,
  Docker, Redis, Celery, React, Node, Streamlit, `uvloop`, FAISS, pandas.
- All SQL in `db/queries.py`. All LLM calls through `llm/client.py`. All timestamp handling
  through `utcnow_iso()` / `parse_iso()`. All embedding serialization through `db/vectors.py`.
  No exceptions to any of these.
- Graph nodes stay thin — read state, call a service function, write state. Business logic
  lives in `sources/`, `discovery/`, `facts/`, `tailor/`, `services/`.
- Web handlers do no business logic. They call services and render templates.
- Anything added to the CLI gets a UI equivalent (the owner wants full control from the browser).
- The UI is dark by default. Templates use plain Tailwind colour utilities; `static/app.css` re-maps them under
  `html.dark`. When a template uses a colour class that isn't in that map yet, add it there. Pages that are
  reachable belong in the sidebar (`web/shell.py`) and the Ctrl+K palette list.
- Schedules live in the database, not config.yaml. Build APScheduler triggers only through
  `services/schedules.build_trigger` (it fixes APScheduler 3's Monday = 0 weekday numbering).
- Model calls go through `RoutingLLM` (llm/client.py): `scoring.provider` / `tailoring.provider` pick Claude or the local
  model (`local_llm`, Ollama by default, plain `httpx`). Never call a model from anywhere else.
- Every source adapter ships a captured JSON fixture and an offline test. Never write an
  adapter test that hits the network.

## Non-negotiable invariants

1. `validate_resume` rejects any bullet without a valid fact reference, and any number,
   percentage, currency amount, or date range in a bullet that isn't present in the referenced
   fact. Applies equally to model output and user hand-edits. Write these tests first.
2. **Every resume is ATS-compliant (SPEC §1.8, §7.6).** Standard headings in configured order,
   plain single-column text, `### Title | Org | Mon YYYY – Present` role headings in
   reverse-chronological order, no tables/images/HTML/links/symbols/inline formatting; the rendered
   `.docx` passes `check_docx_ats`. Enforced in the validator and renderer, not the prompt.
3. Nothing auto-submits an application unless `auto_apply.auto_submit` is on (SPEC Amendment A3). It ships
   off, and off means: fill everything, stop on Review. A run never solves a captcha and never guesses an
   answer — an unmapped question is recorded for the owner, never filled in with a plausible value.
4. `jobhunt run --dry-run` works with no `ANTHROPIC_API_KEY` set.
5. A failing source records its error and the graph continues.
6. Every filterable view keeps its state in the query string. A filter living only in session
   state or a POST body is wrong.
7. Graph execution never blocks a web handler.
8. Export is one-way. Nothing reads data back from a spreadsheet.

## Sources

- **Workday and SuccessFactors are the priority sources** (SPEC §5.2), targeting insurance, health
  and tech employers. Workday uses the public CXS JSON API; configure only sites a tenant's
  robots.txt allows (`jobhunt companies probe-workday`). SuccessFactors uses robots-allowed
  `/search/` and `/job/` pages only — never `/services/` (RSS included).
- Adapters never filter or touch the DB; the registry injects `known` (sightings) and `title_gate`
  (listing-only rows) callables to keep large crawls cheap.

## SQLite rules

- PRAGMAs on every connection, in order: `journal_mode=WAL`, `busy_timeout=10000`,
  `foreign_keys=ON`, `synchronous=NORMAL`. Set them in a `connect` event listener, not
  ad hoc.
- **Write transactions are short and single-purpose.** Never hold one open across an LLM call,
  an HTTP request, or a loop over many jobs. The scoring node commits per job. Violating this
  is what produces `database is locked`, and raising the timeout is not the fix.
- Two database files: `data/jobhunt.db` for the app, `data/checkpoints.db` for `SqliteSaver`.
  Keep them separate — checkpoint writes during a run must not sit in front of queue reads.
- Type conventions from SPEC §4.3 are uniform and mandatory: timestamps are ISO-8601 UTC TEXT
  with a `Z`, booleans are INTEGER 0/1, arrays and structures are JSON TEXT, embeddings are
  float32 BLOBs normalized on write. Inconsistency here is the main failure mode of this port.
- Alembic needs `render_as_batch=True` in `env.py`; SQLite can't `ALTER COLUMN`.
- Vector search is brute-force numpy over a scoped subset. No index, no ANN library. Do not
  "optimize" this.
- Backup is `VACUUM INTO`. Never file-copy a live WAL database.
- Cross-process locking is the `locks` table (SPEC §4.16), not `threading.Lock` — a CLI run and
  the web app are separate processes.

## Windows rules

- `encoding="utf-8"` on every `open()` and every `subprocess` call. No exceptions. This is the
  most common source of bugs on this platform and they surface late, on one bad posting.
- `pathlib.Path` everywhere. Never build a path by string concatenation.
- Everything written to disk goes through the filename sanitizer in `tailor/render.py`:
  illegal characters, trailing dots and spaces, reserved device names, 240-char ceiling. Test
  it with `Sr. Architect: API/Integration (Remote) ` before trusting it.
- No `fork`-based multiprocessing, no `os.fork`, no Unix-only signal handlers. Background work
  is `ThreadPoolExecutor`. Never `os.kill(pid, 0)` to probe a pid — on Windows it terminates it.
- `tzdata` is a hard dependency — `ZoneInfo` raises without it on Windows.
- Ship `.gitattributes` with `* text=auto eol=lf`. CRLF in fixtures or prompt files breaks the
  substring check in evidence validation.
- Catch `PermissionError` around document rendering; Word holds exclusive locks.
- LibreOffice is not on PATH. Use `config.paths.soffice`; skip PDF rendering when it's null or
  missing rather than failing the workflow.

## LangGraph specifics

- `SqliteSaver` against `data/checkpoints.db`; call `.setup()` once at startup.
- Discovery threads `run-{run_id}`; application threads `job-{job_id}`, stored on
  `jobs.thread_id`.
- Graph state MUST stay JSON-serializable — no datetimes, no numpy arrays, no ORM objects.
- Human gates use `interrupt()` and resume with `Command(resume=payload)`. Payload shapes are
  fixed in SPEC §7.4 — don't invent new actions. Code before `interrupt()` must be side-effect
  free (the node re-runs from the top on resume).
- One in-flight execution per thread, enforced through the `locks` table.
- Every discovery node updates `runs.current_node` and `runs.counts` before returning.
- In Jinja, never name a context key `values`/`items`/`keys` — `d.values` resolves to the dict method.

## Order of work within a phase

Schema and migration → engine and queries → pure logic with tests → graph nodes → graph wiring
→ routes → templates. Tests before the code for anything in the validation path.

## Running things

```
$env:PYTHONUTF8 = "1"
uv run pytest                 # offline; no API key needed
uv run ruff check src tests
uv run jobhunt run --dry-run  # validates adapters against live boards, writes nothing
```

## Where to ask rather than guess

- The user's real career facts, comp expectations, and target companies. These belong in
  `config.yaml` and `data/facts.yaml` and are the user's to write. Ship clearly-labeled
  placeholders.
- Scoring rubric weights — propose, don't decide.
- Anything that would involve scraping a site whose terms forbid it.
