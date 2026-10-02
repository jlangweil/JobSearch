"""Shared web helpers: templates + filters, HTMX detection, safe redirects."""

from __future__ import annotations

import html
import re
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from jobhunt.db.engine import age_days, local_display
from jobhunt.graphs.runtime import Runtime, get_runtime

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def rt() -> Runtime:
    return get_runtime()


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def redirect(url: str, **params: object) -> RedirectResponse:
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    if clean:
        url += ("&" if "?" in url else "?") + urlencode(clean)
    return RedirectResponse(url, status_code=303)


def safe_next(value: str | None, default: str) -> str:
    """Only same-site relative paths are allowed as redirect targets."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return default


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200, headers=None):  # noqa: ANN001, ANN201
    ctx = {"request": request, "message": request.query_params.get("message"),
           "error": request.query_params.get("error")}
    ctx.update(context or {})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code, headers=headers)


# --------------------------------------------------------------------------------------------- filters


def _money(job: dict) -> str:
    lo, hi = job.get("comp_min"), job.get("comp_max")
    if not lo and not hi:
        return ""
    fmt = lambda v: f"${round(v / 1000):,}k" if v else "?"  # noqa: E731
    text = fmt(lo) if lo == hi else f"{fmt(lo)}–{fmt(hi)}"
    return text + ("*" if job.get("comp_source") == "inferred" else "")


def _age(value: str | None) -> str:
    d = age_days(value)
    if d is None:
        return ""
    return "today" if d < 1 else f"{int(d)}d"


def _score_class(value: float | None) -> str:
    if value is None:
        return "bg-gray-100 text-gray-500"
    if value >= 80:
        return "bg-green-100 text-green-800"
    if value >= 70:
        return "bg-lime-100 text-lime-800"
    if value >= 55:
        return "bg-amber-100 text-amber-800"
    return "bg-red-100 text-red-800"


def _md(text: str | None) -> Markup:
    """Tiny, escape-first markdown renderer for our constrained document format."""
    out: list[str] = []
    in_list = False
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = html.escape(raw.rstrip())
        bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        if bullet:
            if not in_list:
                out.append('<ul class="list-disc ml-6">')
                in_list = True
            out.append(f"<li>{bullet.group(1)}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.startswith("### "):
            out.append(f'<h4 class="font-semibold mt-3">{line[4:]}</h4>')
        elif line.startswith("## "):
            out.append(f'<h3 class="text-lg font-bold mt-4 border-b">{line[3:]}</h3>')
        elif line.startswith("# "):
            out.append(f'<h2 class="text-xl font-bold">{line[2:]}</h2>')
        elif line.strip():
            out.append(f'<p class="my-2">{line}</p>')
    if in_list:
        out.append("</ul>")
    return Markup("\n".join(out))


def _qs(params: dict, **overrides: object) -> str:
    merged = {k: v for k, v in params.items() if v not in (None, "")}
    for k, v in overrides.items():
        if v in (None, ""):
            merged.pop(k, None)
        else:
            merged[k] = v
    return urlencode(merged)


templates.env.filters.update(local=local_display, money=_money, age=_age, score_class=_score_class, md=_md)


def _scoring_cost() -> str:
    cfg = get_runtime().cfg
    return "free, local model" if cfg.scoring.provider == "local" else "≈ $0.01 each"


def _scoring_label() -> str:
    cfg = get_runtime().cfg
    if cfg.scoring.provider == "local":
        return f"local {cfg.local_llm.model}"
    return f"Claude {cfg.scoring.model}"


def _scoring_ready() -> bool:
    cfg = get_runtime().cfg
    return cfg.scoring.provider == "local" or bool(cfg.secrets.ANTHROPIC_API_KEY)


templates.env.globals.update(qs=_qs, scoring_cost=_scoring_cost, scoring_label=_scoring_label,
                             scoring_ready=_scoring_ready)


def _when(value) -> str:  # noqa: ANN001
    from jobhunt.services.schedules import fmt_when

    return fmt_when(value)


def _asset(name: str) -> str:
    """Static URL with a version from the file's modification time, so browsers never keep a stale copy."""
    path = TEMPLATES_DIR.parent / "static" / name
    try:
        version = path.stat().st_mtime_ns
    except OSError:
        version = 0
    return f"/static/{name}?v={version}"


def _scorer(model: str | None, size: str = "sm") -> Markup:
    """Small tag saying which model produced a score: Claude, or Local (the model on this PC)."""
    if not model:
        return Markup("")
    local = str(model).startswith("local:")
    label, name = ("Local", str(model)[6:]) if local else ("Claude", str(model))
    cls = "scorer scorer-local" if local else "scorer scorer-claude"
    if size == "lg":
        cls += " scorer-lg"
    return Markup(f'<span class="{cls}" title="scored by {html.escape(name)}">{label}</span>')


def _install_shell() -> None:
    from jobhunt.web.shell import PALETTE, shell

    templates.env.filters.update(when=_when, scorer=_scorer)
    templates.env.globals.update(shell=shell, palette=PALETTE, asset=_asset)


_install_shell()
