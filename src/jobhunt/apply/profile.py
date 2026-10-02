"""The apply profile: everything an ATS form asks for that is not on the resume, plus the matcher that decides
which stored answer belongs in a given field.

Workday asks the same few dozen questions on every tenant, but each tenant words them slightly differently
("Are you legally authorized to work in the United States?" / "Do you have the legal right to work in the US?"),
so fields are matched by pattern against a normalized label rather than by exact text. Anything that does not
match is *not* guessed: it comes back as an open question for you to answer in the UI, and the run pauses on it
if `stop_on_unknown` is set. Guessing on an EEO or sponsorship question would put a wrong answer on a legal
form under your name.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

DECLINE = "I do not wish to answer"


class Identity(BaseModel):
    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""
    preferred_name: str = ""
    email: str = ""
    phone: str = ""
    phone_extension: str = ""
    phone_type: str = "Mobile"
    country_phone_code: str = "United States of America (+1)"
    address_line_1: str = ""
    address_line_2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    county: str = ""
    country: str = "United States of America"


class Links(BaseModel):
    linkedin: str = ""
    website: str = ""
    github: str = ""
    twitter: str = ""


class Eligibility(BaseModel):
    """Answers to the standard screening questions. Values are written the way a form expects them ("Yes"/"No")."""

    authorized_to_work: str = "Yes"
    requires_sponsorship: str = "No"
    at_least_18: str = "Yes"
    former_employee: str = "No"
    related_to_employee: str = "No"
    non_compete: str = "No"
    willing_to_relocate: str = "No"
    willing_to_travel: str = "Yes"
    background_check_consent: str = "Yes"
    drug_test_consent: str = "Yes"
    government_employee: str = "No"
    start_date: str = "Two weeks from offer"
    desired_salary: str = ""
    how_did_you_hear: str = "Company website"
    referred_by: str = ""


class Eeo(BaseModel):
    """Voluntary disclosures. Every one of these may be left as "I do not wish to answer"."""

    gender: str = DECLINE
    hispanic_or_latino: str = DECLINE
    ethnicity: str = DECLINE
    veteran_status: str = DECLINE
    disability: str = DECLINE
    disability_name: str = ""          # the Self-Identify page asks you to type your name and today's date
    military_discharge: str = ""


class Signature(BaseModel):
    full_name: str = ""
    initials: str = ""
    accept_terms: bool = True


class CustomAnswer(BaseModel):
    """`match` is a substring of the field label, or /a regex/ between slashes."""

    match: str
    answer: str = ""
    note: str = ""


class ApplyProfile(BaseModel):
    identity: Identity = Field(default_factory=Identity)
    links: Links = Field(default_factory=Links)
    eligibility: Eligibility = Field(default_factory=Eligibility)
    eeo: Eeo = Field(default_factory=Eeo)
    signature: Signature = Field(default_factory=Signature)
    questions: list[CustomAnswer] = Field(default_factory=list)

    def value(self, dotted: str) -> str:
        node: Any = self
        for part in dotted.split("."):
            node = getattr(node, part, "")
        return "" if node is None else (node if isinstance(node, str) else str(node))


# Label pattern -> profile field. Order matters: the first match wins, so put the specific patterns first
# ("legally authorized" before a bare "authorized"). Patterns run against a normalized label (see `normalize`).
RULES: list[tuple[str, str]] = [
    # --- name and contact
    (r"\b(first|given)\s*name\b", "identity.first_name"),
    (r"\bmiddle\s*(name|initial)\b", "identity.middle_name"),
    (r"\b(last|family|sur)\s*name\b", "identity.last_name"),
    (r"\bpreferred\s*name\b", "identity.preferred_name"),
    (r"\b(e-?mail)\b", "identity.email"),
    (r"\bphone\s*(device\s*)?type\b", "identity.phone_type"),
    # before the phone rule: "Phone Extension" is not somewhere to put a phone number
    (r"\bextension\b|\bext\.?$", "identity.phone_extension"),
    (r"\bcountry\s*phone\s*code\b", "identity.country_phone_code"),
    (r"\b(phone|mobile|telephone)\b", "identity.phone"),
    (r"\baddress\s*line\s*1\b|^address$|\bstreet\s*address\b", "identity.address_line_1"),
    (r"\baddress\s*line\s*2\b|\b(apt|suite|unit)\b", "identity.address_line_2"),
    (r"\bcity\b|\bmunicipality\b", "identity.city"),
    (r"\b(state|province|region)\b", "identity.state"),
    (r"\b(postal\s*code|zip)\b", "identity.postal_code"),
    (r"\bcounty\b", "identity.county"),
    (r"\bcountry\b", "identity.country"),
    # --- links
    (r"\blinked\s*in\b", "links.linkedin"),
    (r"\bgit\s*hub\b", "links.github"),
    (r"\b(website|web\s*site|portfolio|personal\s*url|social\s*network)\b", "links.website"),
    # --- screening
    (r"legally\s*(authorized|entitled)|right\s*to\s*work|authorized\s*to\s*work", "eligibility.authorized_to_work"),
    (r"sponsorship|sponsor\b|visa\s*status", "eligibility.requires_sponsorship"),
    (r"\b(18|eighteen)\s*(years)?\s*(of\s*age|or\s*older)", "eligibility.at_least_18"),
    (r"(previously|ever)\s*(been\s*)?(employed|worked)|former\s*employee|current\s*or\s*former\s*employee",
     "eligibility.former_employee"),
    (r"(relative|family\s*member|friend)s?\s*(who\s*)?(work|employed)|related\s*to\s*.*employee",
     "eligibility.related_to_employee"),
    (r"non-?compete|restrictive\s*covenant", "eligibility.non_compete"),
    (r"\brelocat", "eligibility.willing_to_relocate"),
    (r"\btravel\b", "eligibility.willing_to_travel"),
    (r"background\s*(check|investigation)", "eligibility.background_check_consent"),
    (r"drug\s*(test|screen)", "eligibility.drug_test_consent"),
    (r"government\s*(employee|official)|public\s*official", "eligibility.government_employee"),
    (r"(available|start)\s*(to\s*start|date)|when\s*can\s*you\s*start|notice\s*period", "eligibility.start_date"),
    (r"salary|compensation|pay\s*(expectation|rate)|desired\s*(pay|rate)", "eligibility.desired_salary"),
    (r"how\s*did\s*you\s*hear|source\b|referral\s*source", "eligibility.how_did_you_hear"),
    (r"referred\s*by|referrer|employee\s*referral", "eligibility.referred_by"),
    # --- voluntary disclosures
    (r"hispanic|latino", "eeo.hispanic_or_latino"),
    (r"\b(gender|sex)\b", "eeo.gender"),
    (r"ethnic|race\b|racial", "eeo.ethnicity"),
    (r"veteran|military\s*service|protected\s*veteran", "eeo.veteran_status"),
    (r"discharge", "eeo.military_discharge"),
    (r"disab|\bada\b", "eeo.disability"),
    # --- signature blocks
    (r"(your\s*name|full\s*name|please\s*type\s*your\s*name|name\s*of\s*applicant)", "signature.full_name"),
    (r"initials?", "signature.initials"),
    (r"(terms\s*and\s*conditions|i\s*(have\s*)?(read|agree|consent|acknowledge)|privacy\s*(policy|notice)"
     r"|certif(y|ication)|electronic\s*signature)", "signature.accept_terms"),
]
_COMPILED = [(re.compile(p), key) for p, key in RULES]

_NOISE = re.compile(r"\s*\((required|optional|select one)\)\s*|\*|\s*\(.*?\)\s*$", re.IGNORECASE)


def normalize(label: str) -> str:
    """Lower-case, punctuation-light form of a field label, so tenant wording differences stop mattering."""
    s = (label or "").replace("’", "'").strip()
    s = _NOISE.sub(" ", s)
    s = re.sub(r"[^a-z0-9+/&' -]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip(" -")


def field_key(label: str) -> str | None:
    """Which profile field a form label maps to, or None when we have no rule for it."""
    norm = normalize(label)
    if not norm:
        return None
    for pattern, key in _COMPILED:
        if pattern.search(norm):
            return key
    return None


def custom_answer(profile: ApplyProfile, label: str) -> str | None:
    norm = normalize(label)
    for entry in profile.questions:
        pat = entry.match.strip()
        if not pat:
            continue
        if pat.startswith("/") and pat.endswith("/") and len(pat) > 2:
            if re.search(pat[1:-1], norm, re.IGNORECASE):
                return entry.answer
        elif normalize(pat) in norm:
            return entry.answer
    return None


def answer_for(profile: ApplyProfile, label: str) -> tuple[str | None, str]:
    """(answer, source). Your own `questions:` entries win over the built-in rules, so a tenant that words a
    question unusually can always be handled without a code change."""
    if (custom := custom_answer(profile, label)) is not None:
        return (custom or None), "custom"
    if key := field_key(label):
        val = profile.value(key)
        return (val or None), key
    return None, ""


# --------------------------------------------------------------------------------------- choosing from a list

_YES = re.compile(r"^(yes|y|true)\b", re.IGNORECASE)
_NO = re.compile(r"^(no|n|false)\b", re.IGNORECASE)
_DECLINE = re.compile(r"(wish\s*not|not\s*wish|prefer\s*not|decline|don'?t\s*want\s*to\s*(answer|identify)"
                      r"|choose\s*not\s*to)", re.IGNORECASE)


def choose_option(answer: str, options: Sequence[str], *, threshold: int = 82) -> str | None:
    """Pick the dropdown/radio option that means `answer`, or None when nothing is close enough.

    Workday spells the same choice a dozen ways ("I am not a protected veteran", "No, I am not a veteran",
    "I don't wish to answer" / "I prefer not to answer"), so yes/no and decline forms are matched by shape and
    everything else by fuzzy text. Returning None is a feature: a wrong pick on a disclosure page is worse than
    an open question.
    """
    if not answer or not options:
        return None
    clean = [o for o in options if (o or "").strip() and normalize(o) not in ("select one", "select")]
    for opt in clean:                                                     # exact first
        if normalize(opt) == normalize(answer):
            return opt
    # Then an option that starts with the answer. A real Degree list holds both "Master of Science (MS or M.Sc)"
    # and "Master of Science - IT (Msc IT)", so the shortest one wins: it is the plain degree, and the longer
    # ones are that degree plus a qualifier nobody asked for.
    wanted = normalize(answer)
    if wanted:
        starting = [o for o in clean if normalize(o).startswith(wanted)]
        if starting:
            return min(starting, key=lambda o: (len(o), o))
    if _DECLINE.search(answer):
        for opt in clean:
            if _DECLINE.search(opt):
                return opt
    if _YES.match(answer.strip()) or _NO.match(answer.strip()):
        want = _YES if _YES.match(answer.strip()) else _NO
        other = _NO if want is _YES else _YES
        hits = [o for o in clean if want.match(o.strip()) and not _DECLINE.search(o)]
        if len(hits) == 1:
            return hits[0]
        if hits and not any(other.match(o.strip()) for o in hits):
            return hits[0]
    # Two different jobs here. Normally an option has to contain every meaningful word of the answer, because
    # token_set_ratio scores "Science (FAI Only)" a perfect 100 against "Master of Science" — it ignores the
    # words the option adds. When nothing covers the answer, only a near-identical option will do, judged by a
    # ratio that does penalise the difference: that is what lets "I do not wish to answer" find "I don't wish
    # to answer" without letting a Degree list match on one shared word.
    covering = [o for o in clean if _covers(answer, o)]
    if covering:
        scored = sorted(((fuzz.token_set_ratio(normalize(answer), normalize(o)), o) for o in covering),
                        reverse=True)
        if scored[0][0] >= threshold and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 5):
            return scored[0][1]
        return covering[0] if len(covering) == 1 else None
    close = sorted(((fuzz.ratio(normalize(answer), normalize(o)), o) for o in clean), reverse=True)
    return close[0][1] if close and close[0][0] >= 88 else None


STOPWORDS = {"of", "the", "in", "a", "an", "and", "for", "with"}


def _covers(answer: str, option: str) -> bool:
    """Does this option actually contain what was asked for? "Master of Science" is not "Science (FAI Only)"."""
    words = [w for w in normalize(answer).split() if w not in STOPWORDS]
    text = normalize(option)
    return bool(words) and all(w in text for w in words)


# --------------------------------------------------------------------------------------- file handling


def load_profile(path: Path) -> ApplyProfile:
    if not Path(path).exists():
        return ApplyProfile()
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return ApplyProfile.model_validate(raw)


def save_profile(path: Path, profile: ApplyProfile) -> Path:
    for field_name in ("linkedin", "website", "github", "twitter"):   # stored ready to be typed into a form
        setattr(profile.links, field_name, canonical_url(getattr(profile.links, field_name)))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(HEADER)
        yaml.safe_dump(profile.model_dump(), fh, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return path


HEADER = (
    "# Answers jobhunt types into application forms on your behalf. Nothing here leaves your machine except into\n"
    "# the form you are applying on. Passwords are NOT stored here (Settings -> Auto-apply keeps them encrypted).\n"
    "#\n"
    "# 'questions:' entries win over the built-in matching, so add one whenever a site words something oddly:\n"
    "#   - match: \"how did you hear\"          # substring of the label, or /a regex/\n"
    "#     answer: \"Company website\"\n"
    "# Leave an EEO answer as \"I do not wish to answer\" if you would rather not say.\n\n"
)


def canonical_url(value: str) -> str:
    """A URL in the form these forms accept. Workday validates the LinkedIn field and rejects
    "linkedin.com/in/you" and "https://linkedin.com/in/you" alike: it wants the www host."""
    v = (value or "").strip()
    if not v:
        return v
    if not v.startswith(("http://", "https://")):
        v = "https://" + v
    v = re.sub(r"^https?://(?:www\.)?linkedin\.com", "https://www.linkedin.com", v, flags=re.IGNORECASE)
    return v


def state_name(value: str) -> str:
    """"NJ" -> "New Jersey": a form's state dropdown lists full names, and a two-letter code matches nothing."""
    from jobhunt.discovery.prefilter import STATE_NAMES

    code = (value or "").strip()
    if len(code) != 2:
        return code
    for name, abbr in STATE_NAMES.items():
        if abbr == code.upper():
            return name.title()
    return code


def seed_from_contact(contact: Any, profile: ApplyProfile | None = None) -> ApplyProfile:
    """Pre-fill from facts.yaml `contact:` so the file starts out mostly correct instead of empty."""
    p = profile or ApplyProfile()
    name = (getattr(contact, "name", "") or "").split()
    if name and not p.identity.first_name:
        p.identity.first_name, p.identity.last_name = name[0], name[-1] if len(name) > 1 else ""
    for src, dst in (("email", "email"), ("phone", "phone")):
        if (v := getattr(contact, src, "")) and not getattr(p.identity, dst):
            setattr(p.identity, dst, v)
    loc = getattr(contact, "location", "") or ""
    if "," in loc and not p.identity.city:
        p.identity.city, state = (x.strip() for x in loc.split(",", 1))
        p.identity.state = state_name(state)
    for src, dst in (("linkedin", "linkedin"), ("website", "website")):
        if (v := getattr(contact, src, "")) and not getattr(p.links, dst):
            setattr(p.links, dst, canonical_url(v))
    if not p.signature.full_name:
        p.signature.full_name = getattr(contact, "name", "") or ""
    if not p.signature.initials and len(name) >= 2:
        p.signature.initials = f"{name[0][0]}{name[-1][0]}".upper()
    return p
