"""Fact bank: data/facts.yaml is the source of truth; `facts sync` validates, embeds and loads it."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import Engine

from jobhunt.config import AppConfig
from jobhunt.db import queries, vectors
from jobhunt.embeddings import Embedder
from jobhunt.tailor.validate import extract_numeric_tokens

# project: a portfolio project (title = name, org = its link, tags = technologies); its accomplishments are
# achievements with the project as parent_id, and code writes them into the resume's Projects section.
KINDS = ("role", "achievement", "skill", "education", "cert", "summary", "project")
_DATE_RE = re.compile(r"^((19|20)\d{2})(-(0[1-9]|1[0-2]))?$|^present$", re.IGNORECASE)
_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class FactBankError(ValueError):
    pass


class Contact(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    website: str = ""

    def line(self) -> str:
        return " | ".join(x for x in (self.location, self.email, self.phone, self.linkedin, self.website) if x)


class Fact(BaseModel):
    id: str
    kind: str
    text: str
    parent_id: str | None = None
    variants: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] | list[Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    org: str | None = None
    title: str | None = None
    start_date: str | None = None
    end_date: str | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError("id may only contain letters, digits, '_', '.', '-'")
        return v

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        return v

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _date(cls, v: Any) -> str | None:
        if v is None or v == "":
            return None
        s = str(v).strip()
        if not _DATE_RE.match(s):
            raise ValueError("dates must be YYYY, YYYY-MM, or 'present'")
        return s.lower() if s.lower() == "present" else s


class Answer(BaseModel):
    question: str
    answer: str
    kind: str | None = None


class FactFile(BaseModel):
    contact: Contact = Field(default_factory=Contact)
    facts: list[Fact] = Field(default_factory=list)
    answers: list[Answer] = Field(default_factory=list)


def load_yaml(path: Path) -> FactFile:
    if not Path(path).exists():
        raise FactBankError(f"{path} does not exist — copy data/facts.example.yaml to data/facts.yaml")
    with open(path, encoding="utf-8") as fh:
        return parse_text(fh.read())


def parse_text(text: str) -> FactFile:
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise FactBankError(f"YAML syntax error: {exc}") from exc
    if not isinstance(raw, dict):
        raise FactBankError("facts.yaml must be a mapping with contact:, answers: and facts:")
    try:
        ff = FactFile(**raw)
    except ValidationError as exc:
        raise FactBankError(str(exc)) from exc
    problems: list[str] = []
    ids: dict[str, Fact] = {}
    for f in ff.facts:
        if f.id in ids:
            problems.append(f"duplicate fact id {f.id!r}")
        ids[f.id] = f
    for f in ff.facts:
        if f.kind == "achievement":
            if not f.parent_id:
                problems.append(f"{f.id}: achievements need parent_id (the role or project they belong to)")
            elif f.parent_id not in ids or ids[f.parent_id].kind not in ("role", "project"):
                problems.append(f"{f.id}: parent_id {f.parent_id!r} is not a role or project fact")
        if f.kind == "project" and not f.title:
            problems.append(f"{f.id}: project facts need a title (the project's name)")
        if f.kind == "role" and not (f.org and f.title and f.start_date):
            problems.append(f"{f.id}: roles need org, title and start_date")
        if f.kind == "education" and not (f.org and f.title):
            problems.append(f"{f.id}: education facts need org (institution) and title (degree)")
        if f.kind == "cert" and not f.title:
            problems.append(f"{f.id}: cert facts need a title (org/issuer is optional)")
    if problems:
        raise FactBankError("\n".join(problems))
    return ff


def _embed_text(f: Fact) -> str:
    return " ".join(x for x in (f.title, f.org, f.text, " ".join(f.tags)) if x)


def sync(engine: Engine, cfg: AppConfig, embedder: Embedder) -> tuple[int, int]:
    ff = load_yaml(cfg.paths.facts)
    vecs = embedder.embed([_embed_text(f) for f in ff.facts]) if ff.facts else []
    rows = [{**f.model_dump(), "embedding": vectors.to_blob(v)} for f, v in zip(ff.facts, vecs, strict=True)]
    queries.replace_facts(engine, rows)
    avecs = embedder.embed([a.question for a in ff.answers]) if ff.answers else []
    queries.replace_answers(engine, [{"question_text": a.question, "answer_text": a.answer, "kind": a.kind,
                                      "embedding": vectors.to_blob(v)} for a, v in zip(ff.answers, avecs, strict=True)])
    vectors.fact_cache.invalidate()
    return len(rows), len(ff.answers)


def read_text(cfg: AppConfig) -> str:
    path = cfg.paths.facts
    if not Path(path).exists():
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def save_text(engine: Engine, cfg: AppConfig, embedder: Embedder, text: str) -> tuple[int, int, str | None]:
    """Validate the whole file, back up the previous version, write it and sync. Nothing is written if invalid.
    Returns (facts, answers, backup path)."""
    from jobhunt.services.config_edit import backup_file

    text = text.replace("\r\n", "\n")
    parse_text(text)
    path = Path(cfg.paths.facts)
    backup = str(backup_file(path, cfg.paths.exports / "facts-backups", "facts")) if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    n_facts, n_answers = sync(engine, cfg, embedder)
    return n_facts, n_answers, backup


def contact(cfg: AppConfig) -> Contact:
    try:
        return load_yaml(cfg.paths.facts).contact
    except FactBankError:
        return Contact()


WEAK_OPENERS = {"helped", "assisted", "worked", "participated", "responsible", "involved", "tasked", "applied",
                "utilized", "handled", "duties"}


def lint(ff: FactFile) -> list[dict]:
    issues: list[dict] = []

    def issue(fid: str, severity: str, message: str) -> None:
        issues.append({"fact_id": fid, "severity": severity, "message": message})

    for f in ff.facts:
        if f.kind == "achievement":
            if extract_numeric_tokens(f.text) and not f.metrics:
                issue(f.id, "warn", "text has numbers but no metrics — record them so they can be verified")
            if isinstance(f.metrics, Mapping):
                for k, val in f.metrics.items():
                    toks = extract_numeric_tokens(str(val))
                    if toks and not all(any(abs(t.value - u.value) < 1e-9 for u in extract_numeric_tokens(f.text))
                                        for t in toks):
                        issue(f.id, "info", f"metric {k}={val!r} does not appear in text (fine if intentional)")
            for v in f.variants:
                extra = {t.value for t in extract_numeric_tokens(v)} - {t.value for t in extract_numeric_tokens(f.text)}
                if extra:
                    issue(f.id, "error", f"variant introduces numbers not in text: {sorted(extra)}")
        if f.kind == "achievement" and (first := (re.findall(r"[A-Za-z]+", f.text)[:1] or [""])[0].lower()) \
                in WEAK_OPENERS:
            issue(f.id, "warn", f"starts with the weak verb {first!r}: ATS scanners and recruiters read it as a minor "
                                "role; reword with an accurate stronger verb (Led, Delivered, Partnered with ...)")
        if f.kind in ("achievement", "skill", "project") and len(f.tags) < 2:
            issue(f.id, "warn", "fewer than 2 tags — retrieval will rely on text similarity alone")
        if len(f.text) > 300:
            issue(f.id, "warn", f"text is {len(f.text)} chars; keep facts atomic")
        if f.kind == "role" and not f.end_date:
            issue(f.id, "warn", "role has no end_date (use 'present' for a current role)")
    if not any(f.kind == "summary" for f in ff.facts):
        issues.append({"fact_id": "-", "severity": "warn", "message": "no summary fact"})
    if not ff.contact.name:
        issues.append({"fact_id": "contact", "severity": "warn", "message": "contact.name is empty"})
    return issues


def add_fact(engine: Engine, cfg: AppConfig, embedder: Embedder, fact: Mapping[str, Any]) -> Fact:
    """Append one fact to facts.yaml (preserving the rest of the file), validate the whole file, then sync."""
    new = Fact(**fact)
    path = cfg.paths.facts
    with open(path, encoding="utf-8") as fh:
        original = fh.read()
    block = yaml.safe_dump([new.model_dump(exclude_none=True, exclude_defaults=True) | {"id": new.id, "kind": new.kind,
                                                                                         "text": new.text}],
                           allow_unicode=True, sort_keys=False)
    indented = "".join(f"  {ln}\n" if ln else "\n" for ln in block.rstrip("\n").split("\n"))
    updated = original.rstrip("\n") + "\n" + indented
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(updated)
    try:
        load_yaml(path)
        if not any(f.id == new.id for f in load_yaml(path).facts):
            raise FactBankError("the new fact was not appended under `facts:` — keep `facts:` as the last section")
    except FactBankError:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(original)
        raise
    sync(engine, cfg, embedder)
    return new


# --------------------------------------------------------------------------------------------- retrieval


# Degree levels, for requirements like "Bachelor's degree or equivalent experience".
_DEGREE_LEVELS = (
    (3, re.compile(r"\b(ph\.?\s?d|doctora(te|l)|doctor of)\b", re.I)),
    (2, re.compile(r"\b(master'?s?|m\.?\s?s\.?|m\.?\s?sc|mba|m\.?\s?eng)\b", re.I)),
    (1, re.compile(r"\b(bachelor'?s?|b\.?\s?s\.?|b\.?\s?a\.?|b\.?\s?sc|b\.?\s?eng|undergraduate|four-year)"
                   r" ?(degree)?\b", re.I)),
)
# a degree requirement: a degree word, or an abbreviation with degree context ("BS in", "MS degree", "BS/MS"),
# so "MS Office" is not a master's requirement
_DEGREE_WORDS = re.compile(
    r"\b(degree|bachelor|master|ph\.?d|doctora|diploma|mba)\b"
    r"|\b(b\.?s|b\.?a|m\.?s|m\.?a)\.?(\s+(in|degree)\b|/(b|m)\.?(s|a)\b)", re.I)


def _degree_level(text: str) -> int | None:
    return next((level for level, pattern in _DEGREE_LEVELS if pattern.search(text or "")), None)


def education_covers(requirement: str, facts: Sequence[Mapping]) -> list[str] | None:
    """For a degree requirement: the education facts that meet it (a bachelor's requirement is met by a bachelor's
    or higher), [] when none does, None when the requirement isn't about a degree. "or equivalent experience" is met
    by any degree too. Pure; no LLM."""
    if not _DEGREE_WORDS.search(requirement or ""):
        return None
    # the lowest degree it names: "BS/MS", "Bachelor's required, Master's preferred" and "Master's or PhD" ask for
    # a bachelor's, a bachelor's and a master's
    needed = min((level for level, pattern in _DEGREE_LEVELS if pattern.search(requirement)), default=1)
    if re.search(r"equivalent|or (relevant|related|comparable) experience", requirement, re.I):
        needed = min(needed, 1)
    held = [(f["id"], _degree_level(f"{f.get('title') or ''} {f.get('text') or ''}"))
            for f in facts if f.get("kind") == "education"]
    return [fid for fid, level in held if level is not None and level >= needed]


def retrieve(engine: Engine, embedder: Embedder, requirements: Sequence[Mapping], *, top_k: int = 5,
             threshold: float = 0.6, include_all: bool = False) -> tuple[list[dict], list[dict]]:
    """Hybrid (cosine + tag overlap) retrieval of achievements per requirement. No LLM.

    Returns (candidate_facts, coverage). Candidates = retrieved achievements plus every role, skill,
    education, cert and summary fact (the resume skeleton). With `include_all` (resume_mode: complete) every
    achievement is a candidate; each achievement carries `relevance` (0-1) and `matches` (requirement texts it fits)
    so the drafter can order and reword by relevance."""
    facts = queries.list_facts(engine)
    by_id = {f["id"]: f for f in facts}
    ids, mat = vectors.fact_cache.get(lambda: queries.list_facts(engine, with_embedding=True))
    # a project's accomplishments go in the Projects section (written by code), never in Experience bullets
    project_ids = {f["id"] for f in facts if f["kind"] == "project"}
    in_projects = {f["id"] for f in facts if f["kind"] == "achievement" and f.get("parent_id") in project_ids}
    ach_rows = [i for i, fid in enumerate(ids)
                if by_id.get(fid, {}).get("kind") == "achievement" and fid not in in_projects]
    sub = mat[ach_rows] if ach_rows else np.zeros((0, vectors.DIM), dtype=np.float32)
    sub_ids = [ids[i] for i in ach_rows]
    # Coverage also looks at certifications, skills and education (a requirement for an AWS certification is met by
    # the cert fact, not by any accomplishment); only accomplishments are ranked for bullets.
    other_rows = [i for i, fid in enumerate(ids)
                  if by_id.get(fid, {}).get("kind") in ("cert", "skill", "education", "project") or fid in in_projects]
    other = mat[other_rows] if other_rows else np.zeros((0, vectors.DIM), dtype=np.float32)
    other_ids = [ids[i] for i in other_rows]

    ranked: dict[str, float] = {}
    matched: dict[str, list[str]] = {}
    coverage: list[dict] = []
    texts = [f"{r.get('text', '')} {' '.join(r.get('keywords') or [])}" for r in requirements]
    qvecs = embedder.embed(texts) if texts else []
    for req, q in zip(requirements, qvecs, strict=True):
        keywords = {k.lower() for kw in (req.get("keywords") or []) for k in re.findall(r"[a-z0-9+#.]+", kw.lower())}
        scored: list[tuple[str, float]] = []
        for idx, cos in vectors.top_k(sub, q, max(top_k * 3, top_k)):
            fid = sub_ids[idx]
            tags = {t.lower() for t in by_id[fid].get("tags") or []}
            overlap = len(keywords & tags) / len(keywords) if keywords else 0.0
            scored.append((fid, 0.75 * cos + 0.25 * overlap))
        scored.sort(key=lambda x: (-x[1], x[0]))
        top = scored[:top_k]
        best = top[0][1] if top else 0.0
        covered_by = [fid for fid, s in top if s >= threshold - 0.1]
        for idx, cos in vectors.top_k(other, q, 3):
            fid = other_ids[idx]
            tags = {t.lower() for t in by_id[fid].get("tags") or []}
            s = 0.75 * cos + 0.25 * (len(keywords & tags) / len(keywords) if keywords else 0.0)
            if s >= threshold - 0.1:
                covered_by.append(fid)
            best = max(best, s)
        status = "covered" if best >= threshold else ("partial" if best >= threshold - 0.1 else "gap")
        degrees = education_covers(str(req.get("text") or ""), facts)
        if degrees is not None:  # a degree requirement is decided by the education facts, not by similarity
            status, covered_by = ("covered", degrees) if degrees else ("gap", [])
        coverage.append({"requirement": req.get("text"), "kind": req.get("kind"),
                         "covered_by": list(dict.fromkeys(covered_by)),
                         "best_score": round(float(best), 3), "status": status})
        for fid, s in top:
            ranked[fid] = max(ranked.get(fid, 0.0), s)
            if s >= threshold - 0.1 and len(matched.setdefault(fid, [])) < 3:
                matched[fid].append(str(req.get("text") or "")[:120])

    def annotated(fid: str) -> dict:
        return {**by_id[fid], "relevance": round(float(ranked.get(fid, 0.0)), 2), "matches": matched.get(fid, [])}

    order = [fid for fid, _ in sorted(ranked.items(), key=lambda x: (-x[1], x[0]))]
    if include_all:
        order += [f["id"] for f in facts if f["kind"] == "achievement" and f["id"] not in ranked
                  and f["id"] not in in_projects]
    skeleton = [f for f in facts if f["kind"] != "achievement"]
    return skeleton + [annotated(fid) for fid in order], coverage
