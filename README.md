# jobhunt

A personal job-search agent. It finds roles on company career sites (Workday and SAP SuccessFactors
first, plus Greenhouse, Lever and Ashby), filters and scores them against your profile with Claude,
and gives you a web app to triage them. For jobs you shortlist, it drafts an ATS-compliant resume and
cover letter using only facts you've recorded, gives you the application link and a checklist, and
tracks what you applied to and what happened.

It never submits an application for you.

- Contract: [SPEC.md](SPEC.md) (with the 2026-09-14 amendments: ATS-compliant resumes, Workday/SuccessFactors priority)
- Notes for AI coding assistants: [CLAUDE.md](CLAUDE.md)

---

## What you need to do manually

Work through these in order. Items 1–6 are required before the first real run.

### 1. Tooling (one time)

- [ ] **Python 3.12** is already installed (`C:\Users\jlang\AppData\Local\Programs\Python\Python312`).
- [ ] **uv** was installed for you with `pip install --user uv`. Add its folder to your PATH so `uv` works
      in any terminal:
      `C:\Users\jlang\AppData\Roaming\Python\Python312\Scripts`
      (Settings → System → About → Advanced system settings → Environment Variables → Path → New).
- [ ] **Set `PYTHONUTF8=1` permanently** (job postings are full of characters Windows' default encoding
      mangles). In PowerShell: `setx PYTHONUTF8 1`, then open a new terminal.
- [ ] Your `d:\coding\.venv` virtualenv may auto-activate in VS Code. This project has its own
      `.venv`; either run `deactivate` first or always prefix commands with `uv run` (as below).
- [ ] Dependencies are already installed in `d:\coding\jobsearch\.venv`. If you ever need to reinstall:
      `uv sync`.

### 2. Anthropic API key

- [ ] Create a key at <https://console.anthropic.com/> (Settings → API keys) and add billing.
- [ ] `copy .env.example .env` and set `ANTHROPIC_API_KEY=...`. `.env` is git-ignored.
- Models (from `config.yaml`): scoring uses **Claude Haiku 4.5** ($1 / $5 per million input/output
  tokens); tailoring uses **Claude Sonnet 5** ($2 / $10). Rough estimate, not a measurement: scoring
  one posting is ~5k input + ~1k output tokens ≈ **$0.01**, so a 150-job run ≈ **$1–2**; one full tailoring
  pass (requirements + resume + cover letter, before revisions) ≈ **$0.10–0.25**. Actual spend by day is on
  `/settings`.

### 3. Tell it who you are — `config.yaml`

`config.yaml` is yours and git-ignored. The first time jobhunt runs, it copies the committed template
`config.example.yaml` to `config.yaml` (or copy it yourself: `copy config.example.yaml config.yaml`). Edit
`config.yaml`, directly or on the Settings page, never the template. Everything marked `PLACEHOLDER` is an example.
In particular:

- [ ] `profile`: headline, years of experience, comp floor, `employment_types` (currently `[fte]` only —
      add `c2h` if you'd consider contract-to-hire).
- [ ] `must_haves`, `nice_to_haves`, `dealbreakers` — these go straight into the scoring prompt.
- [ ] `filters.title_allowlist` / `title_blocklist` — the biggest lever on noise and on crawl time (titles
      that fail these are never downloaded in full).
- [ ] `filters.local_location_patterns` — nothing is geocoded, so this list of towns stands in for your
      20-mile commute radius. Hybrid/on-site roles survive only if their location matches. Add
      `Jersey City`, `New York`, etc. if you'd commute there.
- [ ] `focus` — **what should score high and what should be pushed down.** `boost` phrases (MuleSoft, API,
      integration, migration, tech/delivery lead…) raise the code-computed **focus fit** sub-score; a job whose
      *title* matches an `avoid` phrase (cybersecurity, information security, GRC…) has its total capped at
      `avoid_cap` (45). Edit on **Settings → Scoring focus**, which previews which jobs move before you save.
      Saving recomputes every stored score instantly, with no model calls.
- [ ] `scoring.weights` — proposed defaults (focus fit 0.15); tune them later on `/settings` (no re-scoring cost).
- [ ] `sources.workday.queries` / `sources.successfactors.queries` — the search terms sent to each
      career site.
- [ ] `companies` — ~237 verified boards are pre-loaded (135 Workday, 51 Greenhouse, 31 Ashby, 11 SuccessFactors,
      6 Lever, 2 Phenom, 1 Eightfold), weighted to insurance/health/pharma/finance/tech. Every token was checked live;
      Workday sites are only ones the tenant's robots.txt marks Allow. A full crawl of all of them takes well over an
      hour, so prefer **Industries** or a company selection on the Runs page (or a schedule) for day-to-day fetches.
      Delete the ones you don't want, deactivate them on **Companies**, and add your own:
      - Workday: find the career site URL (e.g. `https://pru.wd5.myworkdayjobs.com/Careers`), then
        `uv run jobhunt companies probe-workday https://pru.wd5.myworkdayjobs.com` to list its public sites and
        paste the `board_token`.
      - SuccessFactors: `uv run jobhunt companies probe-successfactors careers.example.com`.
      - Phenom (pages under `/us/en/…` with "phApp" in the source, e.g. careers.marsh.com): token
        `careers.marsh.com/global/en`. Eightfold (e.g. New York Life): token `careers.newyorklife.com|newyorklife.com`.
        Check either on **Companies → Check a career site**. Many Phenom sites front a Workday tenant (the Apply
        button goes to myworkdayjobs.com); if that Workday board is already configured, don't add the Phenom site.
      - Set `active: false` to pause a company without deleting it.

### 4. Your fact bank — `data/facts.yaml` (the most important step)

Resumes and cover letters can **only** contain what's in this file. Start from the template:
`copy data\facts.example.yaml data\facts.yaml` (put your current resume in `data\` to work from; it is
git-ignored, see [data/README.md](data/README.md)). Replace every entry with your real history:

- [ ] `contact:` — name, email, phone, location, LinkedIn (printed at the top of every document).
- [ ] One `role` fact per job (org, title, `start_date`, `end_date` as `YYYY-MM` or `present`).
- [ ] Many small `achievement` facts per role, each with `parent_id` = the role. Put every number in
      `metrics` exactly as you want it to appear (`"120"`, `"60%"`, `"$2.5M"`).
- [ ] `skill`, `education`, `cert` and one `summary` fact (`id: summary-primary`, referenced by config).
- [ ] `project` facts for portfolio projects: `title` is the name, `org` the link (`github.com/you/repo`), and `tags`
      the technologies. Their accomplishments are `achievement` facts with the project as `parent_id`. Tailored
      resumes get a **Projects** section, written from these facts, for every project whose technologies the posting
      mentions, with a `Technologies:` line that lists the posting's keywords first (`tailoring.projects` in config).
- [ ] `answers:` — reusable answers (work authorization, sponsorship, salary expectations…) shown at the
      apply step.
- [ ] Check it: `uv run jobhunt facts lint`, then load it: `uv run jobhunt facts sync`.
      (The first sync downloads the local embedding model, ~130 MB, once.)

Why it's strict: the validator rejects any bullet whose numbers or dates aren't in the fact it cites, any
role/degree/cert/skill that isn't in your facts, and anything that breaks ATS formatting. This applies to your
own hand edits too. `data/facts.yaml` is git-ignored because it's personal.

**What stays out of git** (`.gitignore`):
- `.env` and `config.yaml`;
- everything in `data\` except the templates (`facts.example.yaml`, `apply_profile.example.yaml`,
  `resume_template.docx`, `README.md`);
- `output\` and `exports\`;
- every `.docx`/`.pdf` other than the style template;
- databases and `scratch_*` files.

Before a commit, `git status` should list only code, docs and those templates.

### 5. Resume styling (optional) and PDF (optional)

- [ ] `data/resume_template.docx` controls the **look** of generated resumes and cover letters, not their content.
      **It is supposed to look blank.** When a document is generated, everything in the template's body is deleted
      and the tailored text is written using five Word styles: **Title** (your name), **Normal** (contact line,
      summary, skills, letter paragraphs), **Heading 1** (SUMMARY / EXPERIENCE …), **Heading 2** (role lines) and
      **List Bullet** (bullets), plus the page margins.
      - **Don't use a copy of your finished resume as the template.** Its text gets deleted, and most resumes are
        formatted line by line rather than with those styles, so the look doesn't carry over. If it lacks a style
        (e.g. List Bullet), rendering stops with an error rather than producing bullets without markers.
      - The shipped template matches a compact version of your current resume: Calibri, 11pt body, 14pt bold name,
        bold centered all-caps section headings, bold role lines, black text, 0.5" margins.
      - To change the look, open the template in Word → **Home → Styles**, right-click one of those five styles →
        **Modify** (font, size, spacing), and set margins in **Layout → Margins**. Save it under the same name. Don't
        type content into it.
      - Keep it ATS-safe: **no tables, text boxes, columns, images, or text in the page header/footer**, and a
        standard font. Rendering refuses a template that breaks these rules.
- [ ] PDFs need **LibreOffice**, which isn't installed. Install it from <https://www.libreoffice.org/> (the
      default path matches `paths.soffice` in config), or set `soffice: null`. `.docx` is generated either way
      and is the better format for ATS uploads.

### 6. First run

```powershell
cd d:\coding\jobsearch
uv run jobhunt db upgrade          # creates data\jobhunt.db and data\checkpoints.db
uv run jobhunt companies sync      # loads companies from config.yaml
uv run jobhunt facts sync          # after step 4
uv run jobhunt run --dry-run       # fetch + filter only: no writes, no API calls. Check the filter rates.
uv run jobhunt run                 # the real thing (use --sources workday or --limit 25 to start small)
uv run jobhunt serve               # then open http://localhost:8000
```

**Expect the first full run to take about an hour.** Measured on 2026-09-14 with the shipped config: crawling
all 113 companies took ~41 minutes (11,520 postings: Workday 6,827 · Greenhouse 2,303 · SuccessFactors 949 ·
Lever 927 · Ashby 514), local embeddings ~9 minutes, dedupe and prefilter a few seconds. The deterministic
stages removed **97%** of postings (3,270 duplicates, 7,929 filtered), leaving **321 jobs to score**. Scoring
is capped at `scoring.max_per_run` (150); the rest stay queued and are scored on the next run. Later runs are
much faster, because postings already in the database are recorded as sightings without being downloaded
again. To speed things up, prune `companies`, tighten `title_allowlist`, or reduce `queries` /
`max_pages_per_query`.

Some survivors are still off-target (e.g. "Assistant Director, Reinsurance Accounting" matches `director`;
remote roles headquartered elsewhere). Scoring ranks those low, but tightening `title_allowlist` and
`title_blocklist` is cheaper than paying to score them.

If a run is interrupted (closed terminal, reboot), resume it from its last checkpoint:
`uv run jobhunt run --resume <run id>` (or the Resume button on `/runs/<id>`).

### 7. Local model for scoring (optional, free)

Scoring can run on this PC instead of Claude. **`config.yaml` now defaults scoring to the local model** (Qwen3-8B in
Ollama); switch back any time on Settings → Language models. Resume and cover-letter tailoring should stay on Claude: an 8B model fails the strict fact
and ATS checks far more often.

- [ ] **Install Ollama** from <https://ollama.com/download> (Windows installer; it runs in the system tray and
      starts with Windows). It serves models at `http://127.0.0.1:11434`. It is a signed program, so Smart App
      Control doesn't interfere. *(Ollama 0.34.0 is already installed on this machine.)*
- [ ] **Download the model** in PowerShell. Pick one:
      - `ollama pull qwen3:8b` — 5.2 GB, 4-bit (the config default; fastest)
      - `ollama pull qwen3:8b-q8_0` — 8.9 GB, 8-bit, closer to full quality
      - `ollama pull qwen3:8b-fp16` — 16 GB, unquantized

      If you use one of the larger ones, set it as the model on the Settings page.
- [ ] **Settings → Language models**: choose **Local model** for Scoring, check the server URL and model name,
      **Save**, then **Test local model**. The first test loads the model onto the GPU, which takes a while;
      later calls are fast. (Command line: `uv run jobhunt llm test`.)
- [ ] **Compare before switching everything.** On a job's page, **Score with Claude** / **Score with local** adds
      a score from the other model; the **Score history** table shows both side by side, including how many
      evidence quotes survived. Small models paraphrase more, and paraphrased quotes are discarded, which caps
      sub-scores at 69, so local totals may run lower.

Measured on this machine (2026-09-15, Ollama 0.34.0, `qwen3:8b`, RTX 5090): the first call loads the model onto the GPU
(~40 s); after that, scoring 20 real Prudential/Cigna/Travelers postings took **67 s in total** (≈ 6 s per job, 2 in
parallel, ~2,900 prompt tokens each). Every reply matched the schema. The ranking was sensible (director and architect
roles on top, early-career programs and hands-on engineer roles at the bottom), but rationales ran long, and many
sub-scores hit the evidence cap of 69. Judge it against Claude on a few jobs before relying on it.

Notes:
- Local calls are logged on Settings at $0 with the real token counts.
- Settings: **Context tokens** (default 16384) must fit the prompt. The app refuses to use a truncated prompt and
  tells you to raise it. **Parallel requests** (default 2) is how many jobs are scored at once. **Keep loaded** keeps
  the model in GPU memory between jobs.
- If Ollama isn't running when a run reaches scoring, the run stops once with a clear message; start Ollama and
  click **Resume**.
- LM Studio or llama.cpp's `llama-server` also work: set **API** to *OpenAI-compatible* and the server URL (LM
  Studio's is usually `http://127.0.0.1:1234`).
- Python for your own experiments (same request the app sends):

  ```python
  import httpx, json
  from jobhunt.llm.schemas import JobScore
  body = {"model": "qwen3:8b", "stream": False, "think": False, "format": JobScore.model_json_schema(),
          "options": {"num_ctx": 16384, "temperature": 0},
          "messages": [{"role": "system", "content": "Score this posting for the candidate…"},
                       {"role": "user", "content": "Title: Delivery Lead …"}]}
  reply = httpx.post("http://127.0.0.1:11434/api/chat", json=body, timeout=300).json()
  print(JobScore.model_validate_json(reply["message"]["content"]))
  ```

### 8. Ongoing (optional)

- [ ] **Schedules** (sidebar → **Schedules**): create as many as you like, each with its own on/off switch.
      Each has a mode (**Fetch & save only**: free, jobs wait for you; **Full run**; **Score waiting jobs**: no
      fetching, e.g. nightly with the local model), when (days & times, every N hours in a window, or a cron),
      companies/industries/sources, max to score, what to do if another run is busy (wait and retry every 10 min
      for 2 h, or skip), and **catch up** (run once when the server comes back if the PC was off or asleep at the
      time, within 12 h). **Pause all** stops everything without losing each schedule's on/off setting; **Run
      now** starts one immediately. Schedules fire only while `jobhunt serve` is running; to run without the
      server, use Windows Task Scheduler with `uv run jobhunt run --no-score` in `d:\coding\jobsearch`.
      Score the waiting jobs later from **Waiting to score** (sidebar), a run's **Score them now**, or a schedule.
- [ ] **Adzuna** (optional, free): sign up at <https://developer.adzuna.com/signup>, then **Settings → Adzuna**:
      paste the App ID and App key (saved to `.env`), set search terms, area, radius, age and category, switch on
      **Include Adzuna in runs**, **Save**, then **Test with saved settings**. Adzuna finds jobs at companies that
      aren't in your list, but only gives a short snippet of each posting: before tailoring one, open it and click
      **Paste the full posting**.
- [ ] **Job alert emails (LinkedIn, Indeed, Glassdoor, ZipRecruiter):** those sites have no public job-search API and
      their terms forbid crawling, but the alert emails they send you are yours. Create job alerts on each site, then
      **Settings → Job alert emails**: mail server (e.g. `imap.gmail.com`), your address, an app password, and the
      folder or label to read. **Test (read-only)** shows what it finds. For Gmail you need 2-step verification and an
      app password; a filter that labels alerts (e.g. `JobAlerts`) keeps jobhunt away from the rest of your mail, and a
      separate address with your alerts forwarded to it is tighter still. The mailbox is only ever read: nothing is
      deleted, moved, replied to or marked as read. Alerts carry a title, company, location and link but no
      description, so those jobs wait in **Waiting to score** with **Paste the posting** (their links often lead to the
      employer's own careers page, which **Add a job** can read in full). Digest emails ("29 new jobs…") are split into
      one job each, with company, location, pay and the teaser line kept, matched by the site's own job id so the next
      digest doesn't duplicate them.
- [ ] **Open it from another device on your network** (optional): **Settings → Network access**. Set a password,
      tick *Allow other devices on my network*, save, then restart `jobhunt serve`; the page shows the address to
      open on the laptop (e.g. `http://192.168.1.50:8000`) and the one-line Windows firewall command if Windows
      doesn't prompt. Traffic is plain HTTP, so use it only on a network you trust, and never forward the port on
      your router. Off by default (the app listens on 127.0.0.1, which nothing else can reach).
- [ ] **Backups:** `uv run jobhunt backup` or the button on `/settings` (writes `exports\jobhunt-backup-*.db`).
- [ ] **Git:** a repository was initialized but nothing has been committed. Review, then commit when ready.
- [ ] **Be a good citizen:** these are public career-site endpoints used at low rates for personal use. The
      Workday list uses only sites each tenant's robots.txt allows, and the SuccessFactors adapter only reads
      robots-allowed pages. Keep rate limits where they are.

---

## Quick tests from the command line

Everything below can also be done in the web app (see the next section). All commands run from `d:\coding\jobsearch` in PowerShell with `$env:PYTHONUTF8 = "1"` (or `setx` it once).
Use `--companies "Prudential,Cigna"` to keep any run small (name substring, comma-separated).

**Without the LLM (free)**

```powershell
uv run pytest                                   # 187 offline tests
uv run jobhunt facts lint                       # check data\facts.yaml
uv run jobhunt db upgrade                       # once
uv run jobhunt companies sync                   # after editing companies
uv run jobhunt facts sync                       # after editing facts.yaml (first time downloads the embedding model)
uv run jobhunt run --dry-run --sources workday,successfactors --companies "Prudential,Cigna,Lincoln Financial"
                                                # fetch + filter, prints counts/reasons/survivors; writes nothing (~1 min)
uv run jobhunt run --sources workday --companies "Prudential,Cigna"
                                                # without a key: stores and filters real jobs, then stops at "score"
uv run jobhunt stats                            # counts by status; note the run id
uv run jobhunt score --job-id <id> --explain --dry-run
                                                # prints the exact scoring prompt (your recent roles, certs, rubric); no LLM
uv run jobhunt serve                            # http://localhost:8000 → /queue?status=discovered , /runs , /jobs/<id>
```

**With the LLM** (add `ANTHROPIC_API_KEY` to `.env` first)

```powershell
uv run jobhunt run --resume <run id>            # finishes the run above: scores its survivors (≈ $0.01 each)
uv run jobhunt run --sources workday --companies "Prudential,Cigna" --limit 10   # or a fresh small run
uv run jobhunt score --job-id <id> --explain    # prompt + the model's scored answer for one job
uv run jobhunt export --min-score 0             # xlsx of all scores incl. the role_fit column
uv run jobhunt serve                            # /queue → open a job (evidence, gaps, role fit) → Shortlist one
                                                # → workflow drafts a resume + cover letter from your facts (≈ $0.10–0.25)
```

Check spend any time on `/settings`. Delete `data\jobhunt.db` and `data\checkpoints.db` to start over.

## How jobs are found and narrowed

Nothing is pulled "all at once". Each source is narrowed as early as it allows, and whatever gets through is
narrowed again locally before anything is sent to Claude:

| Stage | Workday | SuccessFactors / Phenom / Eightfold | Greenhouse / Lever / Ashby |
|---|---|---|---|
| **At the source** | Searched with `sources.workday.queries`, **restricted to the company's own technology-type job categories and to the United States** using its Workday facets (e.g. Prudential "Technology", Cigna "Information Technology", AIG "IT - Information Technology"), up to `max_pages_per_query` pages each | Searched with each source's `queries`, up to `max_pages_per_query` pages. Phenom is also restricted to the United States with the site's country facet | These APIs return **every open posting** at the company in one request; there is no server-side search |
| **Before downloading a posting** | Postings already in the database → just marked "seen again"; titles failing `title_allowlist`/`title_blocklist` → stored as a one-line listing, full description never downloaded; older than `max_age_days` → skipped | Same | Not applicable (descriptions arrive in the bulk response) |
| **Locally (prefilter, free)** | Duplicates merged; then knocked out by title, age, employment type, posted comp below your floor, **location** (below), dealbreaker phrases | Same | Same |
| **Scoring** (Claude or local model) | Only the survivors are scored, at most `scoring.max_per_run` per run. The run page's **Funnel** shows fetched → duplicates → filtered → **left to score** → scored → **remaining** | Same | Same |

To see what a Workday company's categories are and which ones get kept:
`uv run jobhunt companies probe-workday https://cigna.wd5.myworkdayjobs.com`.
Widen or narrow with `sources.workday.category_patterns`, or per company with `category_patterns: [...]`
(`[]` turns category filtering off for that company). Measured on 10 of the configured Workday companies, the
category + country facets cut the postings listed by 41% (961 → 566) before anything else runs.

**Location.** Jobs outside your area that aren't remote **are excluded** by the prefilter:

- a posting that lists several offices is judged **office by office**: one office in your commute area keeps it;
- an office only counts as local when it matches `filters.local_location_patterns` **and** names one of
  `filters.local_states` (or no state at all) — so "Newark, DE" and "Madison, WI" don't pass as the NJ towns;
- an office that says "Remote", "Work at Home" or "Virtual" counts as remote, so a hybrid posting with a
  work-from-home option is kept;
- **remote** roles are kept (you accept remote), unless every office is in an excluded country ("Remote - India");
- **hybrid** roles are kept only if one office is local;
- **on-site** roles are excluded (add `onsite` to `profile.locations.accept` to keep local ones);
- roles whose remote/hybrid/on-site status can't be determined are kept only if an office is local or remote.

After changing any of this, **Settings → Re-check filtered jobs** re-runs the rules over everything knocked out
before and re-opens what now passes (their postings are downloaded again on the next run).

Excluded postings still get a row — title, company, location, link and the reason, e.g.
`logistics: hybrid outside commute area (Dallas, TX)` — so they aren't downloaded again on the next run and you
can see why something was dropped (`/queue?status=filtered_out`). Their description and embedding are **not**
kept unless you set `filters.keep_filtered_descriptions: true`.

## Where your data lives

- The real database is `d:\coding\jobsearch\data\jobhunt.db` (plus `data\checkpoints.db`), created by
  `jobhunt db upgrade`. It doesn't exist yet. It's in `.gitignore`, so it's never committed; the same goes for
  `.env`, `data\facts.yaml`, `output\` and `exports\`.
- To keep it elsewhere, set `paths.db` and `paths.checkpoints` in `config.yaml` (e.g. `D:/jobhunt-data/jobhunt.db`).
  Avoid OneDrive/Dropbox folders: live SQLite files in synced folders get corrupted. Use `jobhunt backup` and copy
  the backup file instead.
- Size: the first test crawl of all 113 companies, before descriptions of excluded postings were dropped, made a
  121 MB database for 11,520 postings. Roughly 70% of that was descriptions and embeddings of postings that were
  filtered out or duplicates, which are no longer stored. After changing settings, `uv run jobhunt db compact`
  reclaims space.

## Using the web app (everything is available here)

Start it with `uv run jobhunt serve` and open http://localhost:8000. On startup it creates or upgrades the database
and syncs companies from `config.yaml`, so no other command is needed first. Put `ANTHROPIC_API_KEY` in `.env`
before starting (restart the server after changing `.env`).

**Finding your way around.** The app opens on **Home**: counts, next steps, best new matches, recent runs and the
next scheduled runs. The left sidebar groups every page (Jobs, Apply, Discover, Setup) with live counts, e.g. how
many jobs are waiting to be scored. Press **Ctrl+K** anywhere to jump to any page or action by typing a few
letters, or type `#123` to open job 123; press **/** to search jobs. The top bar shows a running run, and has the
**Scoring: Local | Claude** switch (Claude is greyed out until the API key is set) and the theme button (dark
by default; click to cycle dark → light → follow Windows).

**Rescoring.** Every job page has **Re-score** plus **Score with Claude / Score with local** to compare models.
To rescore many jobs at once, filter the queue (e.g. *Top matches*, one company, or min score 60), open
**Rescore these N jobs…** above the table, pick Local or Claude and how many, and **Start**. A progress page shows
how many went up or down and has a **Stop** button. Each attempt is kept in the job's score history; the latest
counts. Changing only scoring focus or weights never needs a rescore: those recompute instantly.

### First session, step by step

1. **Settings** (`/settings`): confirm the spend box doesn't say "ANTHROPIC_API_KEY not set". **Edit config.yaml**
   opens the whole config in an editor: profile, target roles, filters, search terms, weights, schedule. Saving
   validates everything (YAML, every regex, board tokens, weights, cron) and writes nothing if there's a problem.
   A copy of the previous version goes to `exports\config-backups`, and most changes apply immediately.
2. **Facts** (`/facts`): **Sync from facts.yaml** loads your fact bank. **Edit facts.yaml** edits it in the browser
   (validated, backed up to `exports\facts-backups`, synced on save). Lint notes show what's worth checking.
3. **Companies** (`/companies`): see every company with jobs stored/scored, pause or activate one with a click,
   **Check a career site** (paste a Workday/SuccessFactors URL or a Greenhouse/Lever/Ashby slug) and **Add** it.
   Each row has **dry run** and **run** links that open the Runs page with that company ticked.
4. **Runs** (`/runs`): pick
   - **What to do:** **Full run** (fetch → filter → store → score with Claude), **Fetch & save** (free: fetch,
     filter and store without scoring, so you can review jobs and score some or all of them later), **Dry run** (free: fetch and
     filter only, shows counts, filter reasons and the jobs that would survive; saves nothing), or **Score stored
     jobs only** (no fetching; scores jobs left waiting by earlier runs);
   - **Sources**, **Industries** and/or individual **Companies** (nothing ticked = everything active);
   - **Max jobs to score** (caps spend).

   A good first test: tick Prudential and Cigna → **Dry run** → check the survivors → **Do this as a full run**
   with max 10. The run page updates every 2 seconds; **Stop run** halts it within seconds and **Resume from last
   checkpoint** continues later without re-downloading.
5. **Queue** (`/queue`): scored jobs, best first. Filters live in the URL (bookmarkable); **Export .xlsx** saves
   exactly what you see. Hover the score bars for the six sub-scores. Jobs still waiting to be scored are at
   `/queue?status=discovered`, each with **Score now**.
6. **A job** (`/jobs/<id>`): total and sub-scores (including role fit), rationale, quoted evidence, gaps, red
   flags. **Show scoring prompt** shows exactly what the model is told (free); **Re-score** scores it again
   (≈ $0.01) after you change weights-independent things like target roles, facts or must-haves.
7. **Triage** from the queue or the job page: **Reject** (reason required; after 20 decisions they calibrate the
   scorer), **Snooze** until a date, or **Shortlist** (optional emphasis and company notes) to start tailoring.
   **To get a resume and cover letter for a job:** Top matches (or the job's page) → **Shortlist** →
   **Shortlist & start tailoring**. Jobs not scored yet have **Tailor without scoring** instead. The job then moves
   to **Tailoring** (sidebar), then **Ready to apply** once you've approved both documents; every tailored
   resume and cover letter, with downloads, is under **Resumes & letters**. Tailoring uses Claude by default and
   needs `ANTHROPIC_API_KEY`.
   **Resume style** (Settings → Resume tailoring): **Complete** (default) keeps every role and accomplishment from
   your fact bank. The most relevant bullets go first in each role and are reworded with the posting's keywords,
   and the posting's keywords that your facts support lead the Skills section. The validator rejects a draft that
   leaves an accomplishment out or adds a posting keyword your facts don't back. **Focused** keeps only the best
   matches. A complete resume takes about 2–3 minutes and roughly $0.25 per draft with Claude Sonnet.
   **Regenerate:** on the workflow page, **Regenerate draft** (while reviewing) or **Regenerate** (when ready to
   apply) makes new versions from your current facts and settings, with optional instructions.
8. **Workflow** (`/jobs/<id>/workflow`): requirements with coverage, ATS keyword match, each bullet's source fact.
   Resume draft: **Accept**, edit and **Save my edit** (validated like model drafts), **Revise** with
   instructions, or **Abandon**. Then the cover letter. Then **Approve & apply**: open the application, follow
   the checklist, upload the `.docx` from `output\`, submit it yourself, **Confirm applied**. If a step stops
   (an error, or the server restarted mid-step), the page and **Home → Next steps** say so; **Retry from last
   checkpoint** continues without redoing finished steps.
10. **Add a job** (sidebar): for a posting you found yourself. Paste its **link** (Workday, Greenhouse, Lever,
   Ashby, Phenom, Eightfold and SuccessFactors are read from the company's job system; other sites from the page's
   job data or text) or **paste the posting text** (title, company and location are filled in for you to check).
   LinkedIn, Indeed, Glassdoor and ZipRecruiter links aren't read: paste their text, and keep the link. Review the
   details, then choose **Score it first** (the default: pick Local or Claude; the job page then shows the score
   with a **Tailor my resume & cover letter** button, or tick "tailor automatically if it scores at least N"),
   **Tailor now, without scoring**, **Just save it**, or **I already applied** (straight to Applications).
   Jobs you add are never filtered out or hidden as duplicates by later runs.
9. **Applications** (`/applications`): everything you applied to, follow-up dates, outcomes. Tick
   **Needs follow-up** to see what's due.

**Settings** (section links at the top) has Language models, **Scoring focus**, scoring weights (recomputed
instantly, no LLM cost), spend by day, **Back up now**, **Compact** (reclaims disk space; refused while something
is running), **Sync companies**, **Sync facts**, jobs by status and source, and the configuration summary.

---

## What was verified, and what wasn't

Verified on this machine (2026-09-14):

- 187 automated tests pass offline (`uv run pytest`), and `ruff` is clean. They cover the validator (fabricated
  numbers, dates, employers, degrees, skills, and every ATS rule, for model output and hand edits), recorded
  fixtures for all six adapters, Unicode round-trips, the Windows filename sanitizer (`Sr. Architect:
  API/Integration (Remote) `), locks, crash-resume without re-fetching, a web-started run with live progress
  while the queue stays responsive, URL filters surviving a restart, xlsx export, the full tailoring workflow
  including a restart mid-review, and `.docx` ATS checks.
- Live: the Workday and SuccessFactors adapters against real boards (Prudential, Swiss Re, Lincoln Financial…);
  a real discovery run over all 113 configured companies (11,520 postings, 0 corrupted characters) that was
  killed mid-crawl and resumed (it reclaimed the dead process's lock and skipped already-stored postings), then
  stopped cleanly at the score step for lack of an API key; `jobhunt serve` answering every page in ~50 ms while
  that separate CLI run was writing to the same database; a second run refused by the lock.

**Not verified — needs your API key:**

- [ ] Real scoring and drafting calls. All model calls were exercised with test doubles. Do a small real run
      first (`uv run jobhunt run --limit 10`), then `uv run jobhunt score --job-id <id> --explain` to see the
      exact prompt and response, and read a few scores before trusting the queue.
- [ ] Draft quality with your real facts. Expect a few validator loops at first. If a draft keeps failing on
      the same rule, the fact (or its `metrics`) is usually what needs fixing.

## Not built yet

- Auto-apply beyond Workday. Greenhouse, Lever, Ashby and SuccessFactors still use the deep-link checklist.
- Legacy `career*.successfactors.com` portals (JavaScript-only); only SuccessFactors Career Site Builder sites
  (`/search/` + `/job/` pages) are supported.
- There's no "mark applied" shortcut outside the workflow: the status lifecycle only allows `ready → applied`,
  so recording an application goes through the tailoring workflow's final step.

## Auto-apply (beta)

Fills a Workday application in a real browser: signs in with the account you save, uploads the tailored resume,
types your work history in from the fact bank, answers the standard questions, and stops on the Review page.

One-time setup:

```
uv run playwright install chromium        # ~150 MB, once
```

Then, in the UI:

1. **Settings → Auto-apply** — switch it on. Leave *Submit automatically* off to start with.
2. **Auto-apply → Workday sign-ins** — add one entry with the site `*` to reuse a single candidate account on
   every tenant (jobhunt creates the account on a site you have not applied to before), or one entry per site.
   Passwords are encrypted with Windows DPAPI for your login: they are never in `config.yaml`, never in a backup
   you can read, and never shown again. A run also accepts `JOBHUNT_ATS_PASSWORD` from the environment instead.
3. **Auto-apply → Your answers** — check `data/apply_profile.yaml`: address, phone, LinkedIn, work authorization,
   EEO answers (every one may stay "I do not wish to answer"), and free-form answers matched on the field label.
4. Tailor a resume for a Workday job as usual, then open the job and press **Auto-apply**.

### A Workday site you have never applied to

Every tenant keeps its own candidate account, so a new employer means signing up. A run does that by itself:

1. **Signs in first** with the email on your `*` row. (It never registers just because a "Create Account" link is
   on the page — on a site you already use that fails with "an account already exists".)
2. If there is no account, **creates one**: your email, a password generated for that tenant (18 characters,
   mixed case, digit, symbol — satisfies every policy seen so far), terms accepted.
3. **Reads the verification code** the site emails, from the same read-only mailbox as job alerts, and types it
   in. Only messages that arrive *after* the run asks are considered, so a stale code is never reused.
4. **Saves the new account** on the Auto-apply page, encrypted. The next application on that tenant just signs in.

Settings → Auto-apply controls all of this: turn account creation off, reuse one password everywhere instead of
generating one per site, or turn off reading the mailbox. Without a mailbox connected the run stops at the
verification step and tells you to type the code into the open window — it does not guess or skip it.

What it will not do: solve a captcha (it stops and leaves the window open for you), guess an answer it does not
have (the question goes to **Auto-apply → Open questions**; answer it once and every later run reuses it), or
submit anything unless you turn on *Submit automatically*. Every run records each field it filled, each question
it could not answer, and a screenshot of every step.

The tailored resume is always attached. The work history is *also* typed in field by field rather than left to
Workday's resume parser, which splits roles, drops the employer, and mangles dashes in job titles — the same
parse failure you see when you upload the .docx by hand. Turn off *Type the work history into their fields* in
Settings → Auto-apply if you would rather let a particular site parse the attachment.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Windows Security: "Part of this app has been blocked", or a run error "DLL load failed … An Application Control policy has blocked this file" | Windows 11 **Smart App Control** blocks unsigned compiled Python modules it doesn't trust. jobhunt avoids the ones seen so far: SQLAlchemy's optional speed-up (disabled) and SciPy / scikit-learn (no longer installed; embeddings use `transformers` + `torch` directly). Restart `jobhunt serve` after updating, then **Resume** the failed run. If a new file is blocked, note its name from the error. Turning Smart App Control off fixes it for good, but Windows cannot turn it back on without a reset. |
| `UnicodeDecodeError` / garbled dashes | `setx PYTHONUTF8 1` and open a new terminal |
| "a discovery run is already in progress" | Another run holds the lock (CLI or server). Locks from crashed processes are reclaimed automatically. |
| A run died | `uv run jobhunt run --resume <id>` or the Resume button on `/runs/<id>` |
| "…is open in another program (probably Word)" | Close the `.docx` in Word, then click **Retry** on the workflow page |
| `ANTHROPIC_API_KEY is not set` | Add it to `.env` in this folder |
| The queue has too much noise | Tighten `title_allowlist`, `local_location_patterns` and `dealbreaker_patterns`; the prefilter should remove most rows |
| A Workday company returns 422/404 | Its site name changed; re-run `jobhunt companies probe-workday <url>` |

## Layout

```
config.example.yaml    template, committed; copied to config.yaml on first run
config.yaml            your profile, filters, sources, companies (git-ignored)
.env                   secrets (create from .env.example; git-ignored)
data\facts.yaml        your fact bank (create from facts.example.yaml; git-ignored)
data\apply_profile.yaml your answers for application forms (created on first use; git-ignored)
data\resume_template.docx
data\jobhunt.db        application database (SQLite, WAL)
data\checkpoints.db    LangGraph checkpoints
output\                rendered resumes and cover letters
exports\               xlsx exports and backups
src\jobhunt\           sources\ discovery\ graphs\ tailor\ facts\ services\ web\ db\ llm\
tests\                 offline tests + recorded fixtures
```
