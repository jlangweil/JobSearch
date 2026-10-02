"""Edit config.yaml from the web UI: validate everything, keep a timestamped backup, write, reload the running
app's config, re-sync companies and re-apply the schedule. Nothing is written unless the new file is valid."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import ValidationError

from jobhunt.config import AppConfig, load_config, set_config
from jobhunt.db.engine import utcnow_iso
from jobhunt.graphs.runtime import Runtime

RESTART_FIELDS = {
    "paths": "file locations",
    "embeddings": "the embedding model",
    "web": "the web host/port/workers",
}


class ConfigError(ValueError):
    pass


def read_text(rt: Runtime) -> str:
    assert rt.cfg.config_path is not None
    with open(rt.cfg.config_path, encoding="utf-8") as fh:
        return fh.read()


def _regexes(cfg: AppConfig) -> list[tuple[str, str]]:
    f = cfg.filters
    out = [(f"filters.{name}", p) for name in ("title_blocklist", "title_allowlist", "dealbreaker_patterns",
                                               "local_location_patterns", "excluded_location_patterns")
           for p in getattr(f, name)]
    wd = cfg.sources.get("workday")
    extra = (wd.model_extra or {}) if wd else {}
    for key in ("category_patterns", "country_patterns"):
        out += [(f"sources.workday.{key}", p) for p in extra.get(key) or []]
    for c in cfg.companies:
        out += [(f"companies[{c.name}].category_patterns", p) for p in c.category_patterns or []]
    return out


def validate_text(text: str, secrets=None) -> AppConfig:  # noqa: ANN001  (Secrets: the running app's keys)

    from jobhunt.db.queries import WEIGHT_KEYS
    from jobhunt.services.companies import CompanyError, validate_token
    from jobhunt.sources.normalize import slugify

    problems: list[str] = []
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML syntax error: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must be a mapping of sections (profile:, filters:, …)")
    try:
        cfg = AppConfig(**raw)
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
    for where, pattern in _regexes(cfg):
        try:
            re.compile(pattern)
        except re.error as exc:
            problems.append(f"{where}: invalid regex {pattern!r}: {exc}")
    seen: dict[str, str] = {}
    for c in cfg.companies:
        slug = slugify(c.name)
        if slug in seen:
            problems.append(f"companies: {c.name!r} and {seen[slug]!r} have the same slug {slug!r}")
        seen[slug] = c.name
        try:
            validate_token(c.ats_type, c.board_token)
        except CompanyError as exc:
            problems.append(f"companies[{c.name}]: {exc}")
    unknown = set(cfg.scoring.weights) - set(WEIGHT_KEYS)
    if unknown:
        problems.append(f"scoring.weights: unknown subscores {sorted(unknown)} (known: {', '.join(WEIGHT_KEYS)})")
    if sum(max(0.0, v) for v in cfg.scoring.weights.values()) <= 0:
        problems.append("scoring.weights: at least one weight must be positive")
    try:
        from jobhunt.services.schedules import crontab_trigger

        tz = ZoneInfo(cfg.schedule.timezone)
        if cfg.schedule.enabled:  # legacy block, imported once into the Schedules page
            crontab_trigger(cfg.schedule.discovery_cron, tz)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a config problem to report
        problems.append(f"schedule: {exc}")
    adz = cfg.sources.get("adzuna")
    keys = secrets or cfg.secrets
    if adz and adz.enabled and not (keys.ADZUNA_APP_ID and keys.ADZUNA_APP_KEY):
        problems.append("sources.adzuna: enabled, but ADZUNA_APP_ID / ADZUNA_APP_KEY aren't set (Settings → Adzuna)")
    if cfg.schedule.companies:
        from jobhunt.sources.registry import companies_from_config, match_companies

        known = companies_from_config(cfg)
        for term in cfg.schedule.companies:
            if not match_companies(known, [term]):
                problems.append(f"schedule.companies: {term!r} does not match any configured company")
    try:
        from jobhunt.llm.client import load_prompt

        load_prompt("score_job", cfg.scoring.rubric_version)
    except FileNotFoundError:
        problems.append(f"scoring.rubric_version: no prompt file score_job.{cfg.scoring.rubric_version}.md")
    if problems:
        raise ConfigError("\n".join(problems))
    return cfg


def backup_file(path: Path, backups_dir: Path, prefix: str) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow_iso().replace(":", "").replace("-", "")
    out = backups_dir / f"{prefix}-{stamp}{path.suffix}"
    shutil.copy2(path, out)
    return out


def save_text(rt: Runtime, text: str) -> list[str]:
    """Validate, back up, write and apply. Returns notes (e.g. settings that need a restart)."""
    from jobhunt.services import companies, scheduler

    text = text.replace("\r\n", "\n")
    validate_text(text, rt.cfg.secrets)
    path = rt.cfg.config_path
    assert path is not None
    backup = backup_file(path, rt.cfg.paths.exports / "config-backups", "config")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    reloaded = load_config(path)
    notes = [f"previous version saved to {backup}"]
    old = rt.cfg
    for key, label in RESTART_FIELDS.items():
        if getattr(old, key).model_dump() != getattr(reloaded, key).model_dump():
            notes.append(f"restart `jobhunt serve` to apply changes to {label}")
    if old.paths != reloaded.paths:
        reloaded.paths = old.paths  # the running app keeps its open database until restarted
    rt.cfg = reloaded
    set_config(reloaded)
    companies.sync_config_companies(rt.engine, reloaded)
    scheduler.apply_schedules(rt)
    if old.focus != reloaded.focus or old.scoring.weights != reloaded.scoring.weights:
        from jobhunt.discovery.service import recompute_totals

        notes.append(f"recomputed focus and totals for {recompute_totals(rt.engine, reloaded)} scores (no LLM calls)")
    return notes


def _yaml_scalar(value: object) -> str:
    import json

    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list | tuple):
        return json.dumps([str(v) for v in value], ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def _set_line(text: str, section: str, key: str, rendered: str) -> str:
    lines = text.split("\n")
    header = next((i for i, ln in enumerate(lines) if re.match(rf"^{re.escape(section)}:\s*(#.*)?$", ln)), None)
    if header is None:
        return text.rstrip("\n") + f"\n\n{section}:\n  {key}: {rendered}\n"
    end = next((i for i in range(header + 1, len(lines)) if lines[i] and not lines[i][0].isspace()), len(lines))
    pattern = re.compile(rf"^(?P<indent>[ \t]+){re.escape(key)}:[ \t]*(?P<val>[^#\n]*?)(?P<comment>[ \t]+#.*)?$")
    for i in range(header + 1, end):
        m = pattern.match(lines[i])
        if m:
            lines[i] = f"{m.group('indent')}{key}: {rendered}{m.group('comment') or ''}"
            return "\n".join(lines)
    lines.insert(header + 1, f"  {key}: {rendered}")
    return "\n".join(lines)


def _block(lines: list[str], start: int, end: int, parent_indent: int) -> tuple[int, int | None]:
    """End of the block that starts at `start` (lines indented deeper than the parent) and its child indent."""
    child = None
    i = start
    while i < end:
        ln = lines[i]
        if ln.strip() and not ln.lstrip().startswith("#"):
            ind = len(ln) - len(ln.lstrip())
            if ind <= parent_indent:
                break
            child = ind if child is None else child
        i += 1
    return i, child


def _set_path(text: str, path: list[str], rendered: str) -> str:
    """Set a nested mapping key (e.g. sources → adzuna → enabled), keeping comments. A flow-style parent mapping
    (`adzuna: {enabled: false, ...}`) is rewritten as a block first."""
    lines = text.split("\n")
    start, end, indent = 0, len(lines), -1
    for depth, key in enumerate(path):
        leaf = depth == len(path) - 1
        _, child = _block(lines, start, end, indent)
        child = child if child is not None else indent + 2
        pat = re.compile(rf"^(?P<ind>[ \t]{{{child}}}){re.escape(key)}:(?P<rest>[^\n]*)$")
        hit = next((i for i in range(start, end) if pat.match(lines[i])), None)
        if hit is None:
            lines.insert(start, f"{' ' * child}{key}:" + (f" {rendered}" if leaf else ""))
            end += 1
            if leaf:
                return "\n".join(lines)
            hit = start
        m = pat.match(lines[hit])
        rest = m.group("rest")
        if leaf:
            comment = re.search(r"(\s+#.*)$", rest) if not rest.strip().startswith(("'", '"')) else None
            lines[hit] = f"{' ' * child}{key}: {rendered}{comment.group(1) if comment else ''}"
            return "\n".join(lines)
        value = rest.split(" #")[0].strip()
        if value.startswith("{"):
            mapping = yaml.safe_load(value) or {}
            lines[hit] = f"{' ' * child}{key}:"
            lines[hit + 1:hit + 1] = [f"{' ' * (child + 2)}{k}: {_yaml_scalar(v)}" for k, v in mapping.items()]
            end += len(mapping)
        block_end, _ = _block(lines, hit + 1, len(lines), child)
        start, end, indent = hit + 1, block_end, child
    return "\n".join(lines)


def set_values(rt: Runtime, updates: dict[str, object]) -> list[str]:
    """Set config.yaml values by dotted path (`section.key` or deeper), keeping comments and layout, then
    validate/back up/apply."""
    text = read_text(rt)
    for dotted, value in updates.items():
        parts = dotted.split(".")
        if len(parts) == 2:
            text = _set_line(text, parts[0], parts[1], _yaml_scalar(value))
        else:
            text = _set_path(text, parts, _yaml_scalar(value))
    return save_text(rt, text)
