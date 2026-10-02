"""Deterministic knockout rules (SPEC 6.4). No LLM. Returns a human-readable reason or None."""

from __future__ import annotations

import re
from functools import lru_cache

from jobhunt.config import AppConfig
from jobhunt.db.engine import age_days


@lru_cache(maxsize=512)
def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _any(patterns: list[str], text: str | None) -> str | None:
    if not text:
        return None
    for p in patterns:
        if _rx(p).search(text):
            return p
    return None


US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA",
    "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR",
    "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "PR",
}
STATE_NAMES = {
    "new jersey": "NJ", "new york": "NY", "pennsylvania": "PA", "connecticut": "CT", "delaware": "DE",
    "massachusetts": "MA", "maryland": "MD", "california": "CA", "texas": "TX", "florida": "FL", "illinois": "IL",
    "north carolina": "NC", "georgia": "GA", "ohio": "OH", "michigan": "MI", "minnesota": "MN", "arizona": "AZ",
    "washington": "WA", "colorado": "CO", "virginia": "VA", "wisconsin": "WI", "missouri": "MO", "tennessee": "TN",
    "indiana": "IN", "utah": "UT", "oregon": "OR", "nevada": "NV", "iowa": "IA", "kansas": "KS", "alabama": "AL",
    "kentucky": "KY", "oklahoma": "OK", "south carolina": "SC", "louisiana": "LA", "rhode island": "RI",
    "mississippi": "MS", "arkansas": "AR", "nebraska": "NE", "new mexico": "NM", "new hampshire": "NH", "maine": "ME",
    "vermont": "VT", "idaho": "ID", "montana": "MT", "wyoming": "WY", "north dakota": "ND", "south dakota": "SD",
    "west virginia": "WV", "alaska": "AK", "hawaii": "HI", "district of columbia": "DC",
}
_SPLIT = re.compile(r"\s*(?:;|\||•|\bor\b|/(?!\s*hr)|\bmultiple locations?:)\s*", re.IGNORECASE)
# Not "home office": USAA calls its San Antonio headquarters campus "Home Office I".
_REMOTE_OFFICE = re.compile(r"\b(remote|virtual|work(?:ing)? (?:from|at) home|wfh|anywhere|telecommut\w*)\b",
                            re.IGNORECASE)


def split_locations(raw: str | None) -> list[str]:
    """One posting can list several offices ("Newark, NJ; Austin, TX"); each is judged on its own."""
    return [part.strip(" ,-–—") for part in _SPLIT.split(raw or "") if part.strip(" ,-–—")]


def states_in(office: str) -> set[str]:
    """State codes an office names, from "NJ", "USA NJ - Franklin Lakes" or "Newark, New Jersey"."""
    found = {tok for tok in re.findall(r"(?<![A-Za-z])([A-Z]{2})(?![A-Za-z])", office or "") if tok in US_STATES}
    low = (office or "").lower()
    found |= {code for name, code in STATE_NAMES.items() if re.search(rf"\b{name}\b", low)}
    return found


def office_is_local(office: str, cfg: AppConfig) -> bool:
    """True when this one office matches a commute-area town and doesn't name a state you rule out."""
    f = cfg.filters
    if not f.local_location_patterns:
        return True
    if not _any(f.local_location_patterns, office):
        return False
    states = states_in(office)
    if not states or not f.local_states:
        return True
    return bool(states & {s.upper() for s in f.local_states})


def local_offices(raw: str | None, cfg: AppConfig) -> tuple[list[str], list[str], list[str]]:
    """(local offices, remote-style offices, offices in an excluded country) for one posting."""
    local, remote, excluded = [], [], []
    for office in split_locations(raw) or [""]:
        if _any(cfg.filters.excluded_location_patterns, office) and not office_is_local(office, cfg):
            excluded.append(office)  # "Remote - India" is remote *within India*: not somewhere you can work
        elif _REMOTE_OFFICE.search(office):
            # "Remote Flex - North Carolina", "Work At Home-Connecticut": remote *within that state*. With local
            # states set, one that names only other states is not a remote option for you.
            states = states_in(office)
            if states and cfg.filters.local_states and not states & {s.upper() for s in cfg.filters.local_states}:
                continue
            remote.append(office)
        elif office_is_local(office, cfg):
            local.append(office)
    return local, remote, excluded


def title_reason(title: str, cfg: AppConfig) -> str | None:
    """Only the blocklist knocks a title out. The allowlist no longer filters (inexact word matches dropped roles
    like "Managing Engineer"); its terms go to the scoring model as the titles you target, read by meaning."""
    f = cfg.filters
    if p := _any(f.title_blocklist, title):
        return f"title: matches blocklist {p!r}"
    return None


def knockout_reason(job: dict, cfg: AppConfig, *, now: str | None = None) -> str | None:
    f, profile = cfg.filters, cfg.profile

    if reason := title_reason(job.get("title") or "", cfg):
        return reason

    age = age_days(job.get("posted_at"), now=now)
    if age is not None and age > f.max_age_days and f.stale_action == "filter":
        return f"age: posted {age:.0f} days ago (max {f.max_age_days})"

    emp = job.get("employment_type") or "unknown"
    if emp in f.employment_blocklist:
        return f"employment: {emp} is blocklisted"
    if emp != "unknown" and profile.employment_types and emp not in profile.employment_types:
        return f"employment: {emp} not in {profile.employment_types}"

    if profile.comp_floor and job.get("comp_source") == "posted" and job.get("comp_max"):
        if int(job["comp_max"]) < profile.comp_floor:
            return f"comp: posted max ${int(job['comp_max']):,} below floor ${profile.comp_floor:,}"

    location = job.get("location_raw") or ""
    remote = job.get("remote_type") or "unknown"
    accept = set(profile.locations.accept)
    offices = split_locations(location)
    local, remote_offices, excluded = local_offices(location, cfg)
    if excluded and not local and not (remote_offices and "remote" in accept):
        return f"location: {location!r} is in an excluded country"
    if remote == "remote":
        if "remote" not in accept and not local:
            return "logistics: remote roles not accepted"
    elif remote in ("hybrid", "onsite"):
        if remote not in accept and not (remote_offices and "remote" in accept):
            return f"logistics: {remote} roles not accepted"
        if not local and not (remote_offices and "remote" in accept):
            where = f"{len(offices)} offices: {location}" if len(offices) > 1 else (location or "no location")
            return f"logistics: {remote}, none within commute area ({where})"
    elif not local and not remote_offices:
        return f"logistics: location {location or 'unknown'!r} not local and not marked remote"

    if p := _any(f.dealbreaker_patterns, job.get("description_text") or ""):
        return f"dealbreaker: description matches {p!r}"
    return None
