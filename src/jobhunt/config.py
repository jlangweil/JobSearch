"""Configuration: config.yaml (yours, git-ignored; created from the committed config.example.yaml on first run) +
.env (secrets) via pydantic-settings."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Locations(BaseModel):
    base: str = ""
    accept: list[str] = Field(default_factory=lambda: ["remote"])
    max_commute_miles: int | None = None


class Profile(BaseModel):
    headline: str = ""
    years_experience: int | None = None
    summary_fact_ids: list[str] = Field(default_factory=list)
    locations: Locations = Field(default_factory=Locations)
    seniority_band: list[str] = Field(default_factory=list)
    comp_floor: int | None = None
    employment_types: list[str] = Field(default_factory=lambda: ["fte"])
    target_roles: list[str] = Field(default_factory=list)
    role_preference: str = ""


class Filters(BaseModel):
    max_age_days: int = 30
    # keep: an old posting stays and is scored on its merits; filter: it is dropped as before. A posting date
    # is a poor guide to whether a job is live — boards renew listings without changing it.
    stale_action: str = "keep"
    title_blocklist: list[str] = Field(default_factory=list)
    title_allowlist: list[str] = Field(default_factory=list)
    employment_blocklist: list[str] = Field(default_factory=list)
    dealbreaker_patterns: list[str] = Field(default_factory=list)
    local_location_patterns: list[str] = Field(default_factory=list)
    # A listed office counts as local only if it names one of these states (or names no state at all), so
    # "Newark, DE" and "Madison, WI" don't pass as Newark and Madison, NJ. Empty = don't check the state.
    local_states: list[str] = Field(default_factory=list)
    excluded_location_patterns: list[str] = Field(default_factory=list)
    staffing_agency_slugs: list[str] = Field(default_factory=list)
    # False (default): once a posting is filtered out or marked duplicate, its description and embedding are
    # dropped (title, company, location, URL, reason and hash are kept). Keeps the database small.
    keep_filtered_descriptions: bool = False

    @field_validator("employment_blocklist", mode="before")
    @classmethod
    def _stringify(cls, v: Any) -> list[str]:
        return [str(x) for x in (v or [])]


PROVIDERS = ("anthropic", "local")


class LocalLLM(BaseModel):
    """A model served on this machine. `api: ollama` uses Ollama's native /api/chat (recommended); `api: openai` uses
    any OpenAI-compatible server (LM Studio, llama.cpp `llama-server`, vLLM)."""

    api: Literal["ollama", "openai"] = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:8b"
    num_ctx: int = 16384  # tokens of context to allocate; the scoring prompt + a long posting is ~6-10k
    temperature: float = 0.0
    keep_alive: str = "30m"  # how long Ollama keeps the model loaded after the last request
    timeout_s: float = 300
    concurrency: int = 2  # parallel scoring requests (Ollama queues beyond OLLAMA_NUM_PARALLEL)
    think: bool = False  # Qwen3 "thinking" is slower and not needed for structured scoring

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return v


class Focus(BaseModel):
    """Scoring focus, applied in code (discovery/focus.py) and shown to the scoring model."""

    boost: list[str] = Field(default_factory=list)  # phrases that should rank a job higher (title counts most)
    avoid: list[str] = Field(default_factory=list)  # title phrases for areas you don't want / lack experience in
    avoid_cap: int = 45  # a job whose title matches `avoid` gets a total of at most this

    @field_validator("avoid_cap")
    @classmethod
    def _cap_range(cls, v: int) -> int:
        if not 0 <= v <= 100:
            raise ValueError("avoid_cap must be between 0 and 100")
        return v


class IdealJob(BaseModel):
    role: str | None = None  # a role fact id, e.g. role-example-current; None turns ideal_fit off
    low: float = 0.60
    high: float = 0.80


class Scoring(BaseModel):
    provider: Literal["anthropic", "local"] = "anthropic"
    model: str = "claude-haiku-4-5-20251001"
    rubric_version: str = "v9"
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "skill_match": 0.20,
            "role_fit": 0.25,
            "focus_fit": 0.15,
            "seniority_fit": 0.10,
            "domain_fit": 0.10,
            "comp_fit": 0.10,
            "logistics_fit": 0.10,
        }
    )
    ghost_penalty: float = 0.25
    heatmap_min_score: float = 75  # Keyword heatmap: the jobs scoring at least this much
    # industry_fit: the employer's industry (from `companies:`) -> 0-100. Companies not in the list: "insurance" in
    # the name counts as insurance, anything else scores `unknown`. PROPOSED values: yours to tune.
    industry_scores: dict[str, int] = Field(default_factory=lambda: {
        "insurance": 100, "insurtech": 95, "financial": 75, "health": 60, "unknown": 50, "other": 40})
    # Employers that aren't boards under `companies:` (jobs from Adzuna, email alerts), or listed without an industry:
    # name -> industry. Matched like companies (the job's company name starts with it).
    industry_by_employer: dict[str, str] = Field(default_factory=dict)
    # ideal_fit: how close a posting is (by embedding) to your ideal job, a role in the fact bank together with its
    # accomplishments. Cosine similarity `low` or below scores 0, `high` or above 100, linear in between.
    ideal_job: IdealJob = Field(default_factory=lambda: IdealJob())
    shortlist_threshold: float = 70
    max_per_run: int = 150
    concurrency: int = 5
    # Scoring prompt background: this many most-recent roles are shown with their accomplishments (newest first);
    # older roles appear as one line each.
    # A posting shorter than this is a listing snippet (job alert digest, aggregator teaser), not a description:
    # it waits for you to paste the full posting instead of being scored on a couple of lines.
    min_description_chars: int = 400
    recent_roles_in_detail: int = 2
    max_achievements_per_role: int = 20
    # full: older roles are listed with their accomplishments too (so e.g. years of .NET at an earlier employer count
    # as real experience). titles: older roles are one line each (shorter prompt).
    earlier_roles: Literal["full", "titles"] = "full"


class AtsRules(BaseModel):
    """ATS Super Match: the extra rules a resume is drafted and checked against."""

    # most bullets per role, newest role first; roles beyond the list use the last number
    role_bullet_limits: list[int] = Field(default_factory=lambda: [12, 10, 6, 4, 3])
    max_words: int = 1500          # about three pages at 0.5" margins, Calibri 11 (~500 words a page)
    # openers ATS scanners and recruiters read as weak; a bullet must start with a stronger, accurate verb
    weak_openers: list[str] = Field(default_factory=lambda: [
        "helped", "assisted", "worked", "participated", "responsible", "involved", "tasked", "applied", "utilized",
        "handled", "duties"])


class ProjectsSection(BaseModel):
    """The resume's Projects section (kind: project facts), written by code into every tailored resume."""

    # relevant: projects with at least one tag (technology) the posting mentions; always: every project; never: none
    include: Literal["relevant", "always", "never"] = "relevant"
    max_projects: int = 3
    max_bullets_per_project: int = 2


class Tailoring(BaseModel):
    provider: Literal["anthropic", "local"] = "anthropic"
    # automated: draft straight away (the original flow). ats_super_match: first ask which posting keywords and which
    # headline may be added (checkboxes), then draft to ATS rules: three pages, best bullets per role, strong verbs,
    # acronyms spelled out, plain characters. Chosen per job when tailoring starts; this is the default.
    generation_mode: Literal["automated", "ats_super_match"] = "automated"
    ats: AtsRules = Field(default_factory=AtsRules)
    projects: ProjectsSection = Field(default_factory=ProjectsSection)
    model: str = "claude-sonnet-5"
    max_draft_attempts: int = 3
    # complete: every role and accomplishment from the fact bank, reordered and reworded for the posting.
    # focused: only the accomplishments that best match the posting's requirements (a shorter resume).
    resume_mode: Literal["complete", "focused"] = "complete"
    # How much Claude reasons before writing (Sonnet 5 / Opus 5 reason by default, and reasoning shares the output
    # budget). medium keeps complete resumes within the output limit; high reasons more; "" = the model's default.
    effort: Literal["low", "medium", "high", ""] = "medium"
    # How a role is written into the .docx. separate_lines: employer, job title and dates each on their own line,
    # which is what resume parsers (Workday, Taleo, iCIMS) read reliably. one_line: "Title | Employer | dates".
    role_layout: Literal["separate_lines", "one_line"] = "separate_lines"
    max_bullets_per_role: int = 0  # 0 = no limit
    max_bullet_chars: int = 320
    sections: list[str] = Field(default_factory=lambda: ["summary", "skills", "experience", "projects", "education"])
    facts_per_requirement: int = 5
    fact_match_threshold: float = 0.6


class EmailAlerts(BaseModel):
    """Job-alert emails (LinkedIn, Indeed, …). The mailbox is only ever read; credentials live in .env."""

    enabled: bool = False
    host: str = ""  # e.g. imap.gmail.com (falls back to IMAP_HOST in .env)
    port: int = 993
    folder: str = "INBOX"  # a Gmail label or mail folder keeps jobhunt away from the rest of your mail
    senders: list[str] = Field(default_factory=list)  # empty = the built-in LinkedIn/Indeed/… alert addresses
    subject_patterns: list[str] = Field(default_factory=list)
    max_age_days: int = 14
    max_messages: int = 200


class Embeddings(BaseModel):
    model: str = "BAAI/bge-small-en-v1.5"


class Schedule(BaseModel):
    # Schedules live in the database (Schedules page). `timezone` is the default for new ones; the rest is the old
    # single-schedule block, imported once into the Schedules page if it was enabled.
    discovery_cron: str = "0 7 * * 1-5"
    timezone: str = "America/New_York"
    enabled: bool = False
    # full: fetch, filter and score · fetch: fetch, filter and save only (score later from the Queue or Runs page)
    mode: Literal["full", "fetch"] = "full"
    # Company slugs or name fragments to include; empty = every active company.
    companies: list[str] = Field(default_factory=list)
    max_to_score: int | None = None  # full mode only; default scoring.max_per_run


class AutoApply(BaseModel):
    """Auto-apply (beta). Nothing is submitted unless `auto_submit` is explicitly turned on: the default run
    fills every step it can and stops on the Review page for you to read and press Submit yourself."""

    enabled: bool = False
    ats_types: list[str] = Field(default_factory=lambda: ["workday"])
    headless: bool = False          # watching the browser is the point during beta; you can take over mid-run
    auto_submit: bool = False
    stop_on_unknown: bool = True    # pause on a question with no stored answer rather than leaving it blank
    create_accounts: bool = True    # register on a Workday tenant you have not applied to before
    password_mode: str = "per_site"  # per_site: generate and store one password per tenant; shared: reuse yours
    store_passwords: str = "encrypted"   # encrypted (Windows DPAPI) or plain, shown in the UI as typed
    read_verification_email: bool = True   # read the "verify your email" code from the mailbox set up for alerts
    verification_wait_seconds: int = 180
    wait_for_human_seconds: int = 300   # a visible run pauses this long for a step only you can do
    keep_open_minutes: int = 30         # how long a visible window stays open after a run stops or fails
    upload_cover_letter: bool = True
    fill_work_experience: bool = True   # type the roles in; False attaches the resume and lets the site parse it
    apply_mode: str = "manual"          # manual | autofill: hand the resume to the site's parser first
    account_email: str = ""         # login used when creating an account on a new tenant; blank = profile email
    timeout_seconds: int = 45
    slow_mo_ms: int = 0
    keep_screenshots: bool = True
    max_work_experience: int = 6
    max_skills: int = 15            # how many skills to offer a form's skills picker


class Web(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    executor_workers: int = 4


class Paths(BaseModel):
    db: Path = Path("./data/jobhunt.db")
    checkpoints: Path = Path("./data/checkpoints.db")
    facts: Path = Path("./data/facts.yaml")
    output_root: Path = Path("./output")
    exports: Path = Path("./exports")
    resume_template: Path = Path("./data/resume_template.docx")
    apply_profile: Path = Path("./data/apply_profile.yaml")
    apply_artifacts: Path = Path("./output/_auto_apply")
    soffice: Path | None = None
    max_path_chars: int = 240
    lock_stale_minutes: int = 30

    def resolved(self, base: Path) -> Paths:
        def fix(p: Path | None) -> Path | None:
            if p is None:
                return None
            # Accept either separator in config; resolve to absolute.
            p = Path(str(p).replace("\\", "/"))
            return (p if p.is_absolute() else base / p).resolve()

        data = self.model_dump()
        for key in ("db", "checkpoints", "facts", "output_root", "exports", "resume_template",
                    "apply_profile", "apply_artifacts", "soffice"):
            data[key] = fix(getattr(self, key))
        return Paths(**data)


class SourceConfig(BaseModel):
    model_config = {"extra": "allow"}
    enabled: bool = False
    rate_limit_per_sec: float = 1.0


ATS_TYPES = ("greenhouse", "lever", "ashby", "workday", "successfactors", "phenom", "eightfold", "other", "unknown")


class CompanyConfig(BaseModel):
    name: str
    ats_type: str = "unknown"
    board_token: str | None = None
    industry: str | None = None
    # Optional per-company search terms for query-driven sources (workday, successfactors).
    queries: list[str] | None = None
    # Optional Workday override of sources.workday.category_patterns; [] disables category filtering.
    category_patterns: list[str] | None = None
    active: bool = True
    notes: str | None = None

    @field_validator("ats_type")
    @classmethod
    def _ats(cls, v: str) -> str:
        if v not in ATS_TYPES:
            raise ValueError(f"ats_type must be one of {ATS_TYPES}")
        return v


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file_encoding="utf-8", extra="ignore")
    ANTHROPIC_API_KEY: str | None = None
    ADZUNA_APP_ID: str | None = None
    ADZUNA_APP_KEY: str | None = None
    IMAP_HOST: str | None = None
    IMAP_USER: str | None = None
    IMAP_PASSWORD: str | None = None
    WEB_PASSWORD: str | None = None


class AppConfig(BaseModel):
    profile: Profile = Field(default_factory=Profile)
    must_haves: list[str] = Field(default_factory=list)
    nice_to_haves: list[str] = Field(default_factory=list)
    dealbreakers: list[str] = Field(default_factory=list)
    focus: Focus = Field(default_factory=Focus)
    filters: Filters = Field(default_factory=Filters)
    scoring: Scoring = Field(default_factory=Scoring)
    tailoring: Tailoring = Field(default_factory=Tailoring)
    local_llm: LocalLLM = Field(default_factory=LocalLLM)
    embeddings: Embeddings = Field(default_factory=Embeddings)
    email: EmailAlerts = Field(default_factory=EmailAlerts)
    schedule: Schedule = Field(default_factory=Schedule)
    auto_apply: AutoApply = Field(default_factory=AutoApply)
    web: Web = Field(default_factory=Web)
    paths: Paths = Field(default_factory=Paths)
    sources: dict[str, SourceConfig] = Field(default_factory=dict)
    companies: list[CompanyConfig] = Field(default_factory=list)

    # Not from YAML:
    config_path: Path | None = None
    secrets: Secrets = Field(default_factory=Secrets)

    @property
    def root(self) -> Path:
        return self.config_path.parent if self.config_path else PROJECT_ROOT


def default_config_path() -> Path:
    env = os.environ.get("JOBHUNT_CONFIG")
    if env:
        return Path(env).resolve()
    cwd = Path.cwd() / "config.yaml"
    return cwd.resolve() if cwd.exists() else (PROJECT_ROOT / "config.yaml")


EXAMPLE_CONFIG = PROJECT_ROOT / "config.example.yaml"


def load_config(path: Path | None = None) -> AppConfig:
    path = (path or default_config_path()).resolve()
    if not path.exists() and path.name == "config.yaml" and (path.parent / "config.example.yaml").exists():
        # first run of a fresh checkout: your own config starts as a copy of the template (config.yaml is git-ignored)
        shutil.copyfile(path.parent / "config.example.yaml", path)
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    cfg = AppConfig(**raw)
    cfg.config_path = path
    cfg.paths = cfg.paths.resolved(path.parent)
    env_file = path.parent / ".env"
    cfg.secrets = Secrets(_env_file=env_file if env_file.exists() else None)
    return cfg


_CONFIG: AppConfig | None = None


def get_config() -> AppConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG


def set_config(cfg: AppConfig | None) -> None:
    global _CONFIG
    _CONFIG = cfg


_WEIGHTS_LINE = re.compile(r"^(?P<indent>[ \t]*)weights:[ \t]*\{[^}\n]*\}[^\n]*$", re.MULTILINE)
_WEIGHTS_BLOCK = re.compile(r"^(?P<indent>[ \t]*)weights:[ \t]*\n(?:(?P=indent)[ \t]+[A-Za-z_]+:[^\n]*\n?)+",
                            re.MULTILINE)


def write_scoring_weights(cfg: AppConfig, weights: dict[str, float]) -> None:
    """Rewrite `scoring.weights` in config.yaml as one flow-style line, preserving the rest of the file."""
    assert cfg.config_path is not None
    with open(cfg.config_path, encoding="utf-8") as fh:
        text = fh.read()
    body = ", ".join(f"{k}: {round(float(v), 4)}" for k, v in weights.items())
    new_text, n = _WEIGHTS_LINE.subn(lambda m: f"{m.group('indent')}weights: {{{body}}}", text, count=1)
    if n != 1:
        new_text, n = _WEIGHTS_BLOCK.subn(lambda m: f"{m.group('indent')}weights: {{{body}}}\n", text, count=1)
    if n != 1:
        raise ValueError("could not find `weights:` under `scoring:` in config.yaml")
    with open(cfg.config_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(new_text)
    cfg.scoring.weights = dict(weights)


def write_env(env_path: Path, updates: dict[str, str | None]) -> None:
    """Set KEY=value lines in .env (created if missing), keeping every other line. None/"" clears a value."""
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    for key, value in updates.items():
        rendered = f"{key}={(value or '').strip()}"
        idx = next((i for i, ln in enumerate(lines) if ln.split("=", 1)[0].strip() == key), None)
        if idx is None:
            lines.append(rendered)
        else:
            lines[idx] = rendered
    with open(env_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
