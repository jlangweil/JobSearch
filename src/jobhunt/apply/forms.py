"""Generic form reading and filling on top of Playwright.

Deliberately not Workday-specific: fields are found as real form controls and labelled the way a screen reader
would label them (aria-labelledby, aria-label, <label for>, wrapping <label>, fieldset <legend>, and only then
Workday's `data-automation-id`). That keeps the logic testable against plain HTML fixtures and makes it survive
the tenant-by-tenant markup differences that break selector-only automation.

Nothing here decides *what* to answer — `profile.py` does that — and nothing here clicks a submit button.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field as dc_field

from jobhunt.apply.profile import choose_option

# Controls worth filling. Hidden inputs, buttons and search boxes are not fields we answer.
CONTROL_SELECTOR = (
    "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=image]):not([type=reset]), "
    "textarea, select, [role=combobox], button[aria-haspopup=listbox], div[data-automation-id=selectWidget]"
)
SKIP_LABELS = re.compile(
    r"^(search|keyword|filter|sign in with|show password|remember me)\b|"
    r"\b(utility menu|menu button|main menu|sign out|log out|careers home|job alerts|candidate home|"
    r"skip to main)\b", re.IGNORECASE)

# Bot traps. Forms hide a field that no person can see and reject the submission if anything fills it in — and
# they say nothing when they do, because telling a bot it was caught would defeat the point. First American's
# create-account form carries one labelled "Enter website. This input is for robots only, do not fill it out",
# which a matcher looking for "website" will happily complete, and then the form silently refuses forever.
HONEYPOT_LABELS = re.compile(
    r"(robots?\s*only|for\s*robots|do\s*not\s*fill|don'?t\s*fill|leave\s*(this\s*)?(field\s*)?(blank|empty)|"
    r"if\s*you\s*are\s*(a\s*)?human|human\s*verification\s*field|anti-?bot|honey\s*pot)", re.IGNORECASE)
HONEYPOT_NAMES = re.compile(r"(honey|hp_?field|bot_?trap|url_?trap|leave_?blank|nickname_?trap)", re.IGNORECASE)

# A field a person cannot see is not a field we may fill, however it was hidden. Playwright's is_visible() only
# rules out display:none and visibility:hidden, so opacity, clipping, tiny boxes and off-screen placement are
# checked here.
TRAP_STYLE_JS = """
(el) => {
  // A control that is hidden by CSS but carries a label a person can read is a *styled* control, not a trap:
  // Workday draws its own checkbox and leaves the real input at opacity 0 behind it. Skipping those would mean
  // never ticking a consent box. Only unlabelled hidden fields are treated as traps.
  const labelled = (() => {
    // aria-label counts: Workday's date boxes are labelled "Month" and "Year" that way and nothing else, and
    // treating them as unlabelled got every one of them thrown away as a bot trap.
    if ((el.getAttribute('aria-label') || '').trim()) return true;
    if (el.getAttribute('role') === 'spinbutton') return true;
    const nodes = [];
    const ids = el.getAttribute('aria-labelledby');
    if (ids) ids.split(/\\s+/).forEach(i => { const n = document.getElementById(i); if (n) nodes.push(n); });
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) nodes.push(l); }
    const wrap = el.closest('label');
    if (wrap) nodes.push(wrap);
    return nodes.some(n => { const r = n.getBoundingClientRect(); return r.width > 4 && r.height > 4; });
  })();
  const r = el.getBoundingClientRect();
  if (r.width === 0 && r.height === 0 && r.x === 0 && r.y === 0) return '';   // not laid out yet, not hidden
  if (el.getAttribute('aria-hidden') === 'true' && !labelled) return 'aria-hidden';
  if (r.x + r.width < -500 || r.y + r.height < -500) return 'positioned off screen';
  if (labelled) return '';
  if (r.width < 8 || r.height < 8) return 'too small to be real';
  let n = el;
  for (let i = 0; n && n !== document.body && i < 6; i++) {
    const s = getComputedStyle(n);
    if (s.opacity === '0') return 'transparent';
    if (s.visibility === 'hidden' || s.display === 'none') return 'hidden';
    if (s.clipPath === 'inset(100%)' || s.clip === 'rect(0px, 0px, 0px, 0px)') return 'clipped away';
    if (parseFloat(s.textIndent) < -999) return 'text-indented off screen';
    n = n.parentElement;
  }
  return '';
}
"""


def trap_reason(el, label: str, name: str = "") -> str:  # noqa: ANN001
    """Why this field looks like a bot trap, or "" when it looks like a real question."""
    if HONEYPOT_LABELS.search(label or ""):
        return "labelled as a trap"
    if HONEYPOT_NAMES.search(name or ""):
        return "named as a trap"
    try:
        return el.evaluate(TRAP_STYLE_JS) or ""
    except Exception:
        return ""


@dataclass
class Field:
    label: str
    kind: str                       # text | textarea | select | radio | checkbox | date | file | unknown
    locator: object = None          # Playwright Locator (or a stub in tests)
    options: list[str] = dc_field(default_factory=list)
    name: str = ""
    required: bool = False
    value: str = ""                 # what it already contains
    group: str = ""                 # the enclosing group's label: "From" for the Month/Year pair inside it

    @property
    def filled(self) -> bool:
        return bool((self.value or "").strip())


@dataclass
class FillResult:
    label: str
    kind: str
    status: str                     # filled | unchanged | unknown | no_option | error
    value: str = ""
    options: list[str] = dc_field(default_factory=list)
    detail: str = ""
    required: bool = False          # only a required field is worth stopping a run over


_WS = re.compile(r"\s+")
_HUMANIZE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def clean_label(text: str) -> str:
    """Trim the decoration Workday hangs off labels: required markers, 'select one', help-text tails."""
    s = _WS.sub(" ", (text or "").replace(" ", " ")).strip()
    s = re.sub(r"\s*\*\s*$", "", s)
    s = re.sub(r"^\s*\*\s*", "", s)
    s = re.sub(r"\s*\(required\)\s*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*required\s*$", "", s, flags=re.IGNORECASE)
    return s.strip(" :·|")


def humanize(automation_id: str) -> str:
    """`formField-legalNameSection_firstName` -> `first Name`, a usable label when nothing else is available."""
    tail = automation_id.split("-")[-1].split("_")[-1]
    return _HUMANIZE.sub(" ", tail).replace("--", " ").strip()


LABEL_JS = """
(el) => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const byIds = (ids) => ids.split(/\\s+/).map(i => document.getElementById(i))
      .filter(Boolean).map(n => clean(n.innerText || n.textContent)).join(' ');
  if (el.getAttribute('aria-labelledby')) {
    const t = byIds(el.getAttribute('aria-labelledby'));
    if (t) return t;
  }
  if (clean(el.getAttribute('aria-label'))) return clean(el.getAttribute('aria-label'));
  if (el.id) {
    const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (lab && clean(lab.innerText)) return clean(lab.innerText);
  }
  const wrap = el.closest('label');
  if (wrap && clean(wrap.innerText)) return clean(wrap.innerText);
  // Only a container holding this field alone (or nearly) can speak for it: a section-wide group would hand
  // every unlabelled input the first label it happens to contain — every date part came back as "Job Title".
  const group = el.closest('[data-automation-id^="formField-"], .field, [role=group]');
  if (group && group.querySelectorAll('input, select, textarea').length <= 3) {
    const lab = group.querySelector('label, legend, [id$="-label"]');
    if (lab && clean(lab.innerText)) return clean(lab.innerText);
  }
  if (clean(el.getAttribute('placeholder'))) return clean(el.getAttribute('placeholder'));
  const fs = el.closest('fieldset');
  if (fs) {
    const lg = fs.querySelector('legend');
    if (lg && clean(lg.innerText)) return clean(lg.innerText);
  }
  if (clean(el.getAttribute('placeholder'))) return clean(el.getAttribute('placeholder'));
  const auto = el.getAttribute('data-automation-id') || (group && group.getAttribute('data-automation-id')) || '';
  return auto ? '@' + auto : '';
}
"""


# The label of the block a field sits in. Workday writes a date as a "From" group containing a Month and a
# Year input, so the field's own label says nothing about which date it is part of.
GROUP_LABEL_JS = """
(el) => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const own = el.id ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null;
  let node = el.parentElement;
  for (let i = 0; node && i < 5; i++) {
    // a container holding several fields is a section, not this field's group: its labels belong to siblings
    if (node.querySelectorAll('input, select, textarea').length > 3) return '';
    if (node.matches('[role=group], fieldset, [data-automation-id^="formField-"], [data-automation-id=dateWidget]')) {
      const by = node.getAttribute('aria-labelledby');
      if (by) {
        const n = document.getElementById(by);
        const t = n ? clean(n.innerText) : '';
        if (t) return t;
      }
      for (const l of node.querySelectorAll('label, legend, [id$="-label"]')) {
        if (l === own || (own && own.contains(l))) continue;
        const t = clean(l.innerText);
        if (t) return t;
      }
    }
    node = node.parentElement;
  }
  return '';
}
"""


def _kind_of(tag: str, input_type: str, role: str) -> str:
    if tag == "select":
        return "select"
    if tag == "textarea":
        return "textarea"
    if tag == "button" or role in ("combobox", "listbox"):
        return "select"
    if tag == "div":
        return "select"
    return {
        "checkbox": "checkbox", "radio": "radio", "file": "file", "date": "date",
        "email": "text", "tel": "text", "password": "text", "number": "text", "url": "text", "": "text",
    }.get(input_type, "text")


def scan(scope) -> list[Field]:  # noqa: ANN001
    """Every answerable field in `scope` (a Page or Locator), in document order, radios grouped into one field.

    Bot traps are left out entirely: filling one makes the form refuse to submit, with no message to say why.
    """
    controls = scope.locator(CONTROL_SELECTOR)
    fields: list[Field] = []
    traps: list[Field] = []
    radios: dict[str, Field] = {}
    for i in range(controls.count()):
        el = controls.nth(i)
        try:
            if not el.is_visible():
                continue
            tag = el.evaluate("e => e.tagName.toLowerCase()")
            input_type = (el.get_attribute("type") or "").lower() if tag == "input" else ""
            role = (el.get_attribute("role") or "").lower()
            label = clean_label(el.evaluate(LABEL_JS))
        except Exception:                                   # a field that vanished mid-scan is not an error
            continue
        if label.startswith("@"):
            label = clean_label(humanize(label[1:]))
        if not label or SKIP_LABELS.match(label):
            continue
        reason = trap_reason(el, label, el.get_attribute("name") or "")
        if reason and reason not in ("labelled as a trap", "named as a trap"):
            # A panel that has just been added slides in, so for a moment its fields report a zero-size or
            # off-screen box and look exactly like a hidden trap. Look again once the layout has settled before
            # writing off a real field — this skipped every Job Title, Company and date on My Experience.
            with suppress(Exception):
                el.scroll_into_view_if_needed(timeout=1500)   # a field parked off screen may just be below
                el.page.wait_for_timeout(400)
                reason = trap_reason(el, label, el.get_attribute("name") or "")
        if reason:
            traps.append(Field(label, "trap", el, name=reason))
            continue
        kind = _kind_of(tag, input_type, role)
        if kind == "radio":
            group = el.get_attribute("name") or label
            option = clean_label(el.evaluate(LABEL_JS)) or label
            if group not in radios:
                legend = el.evaluate(
                    "(e) => { const f = e.closest('fieldset, [role=radiogroup], [data-automation-id^=\"formField-\"]');"
                    "const l = f && f.querySelector('legend, label, [id$=\"-label\"]');"
                    "return l ? l.innerText.replace(/\\s+/g, ' ').trim() : ''; }")
                radios[group] = Field(clean_label(legend) or group, "radio", el.locator("xpath=ancestor::*[1]"),
                                      name=group)
                fields.append(radios[group])
            radios[group].options.append(option)
            continue
        f = Field(label, kind, el, name=el.get_attribute("name") or "")
        with suppress(Exception):
            f.group = clean_label(el.evaluate(GROUP_LABEL_JS))
        f.required = (el.get_attribute("aria-required") == "true") or el.get_attribute("required") is not None
        if kind == "select" and tag == "select":
            f.options = [clean_label(o) for o in el.locator("option").all_inner_texts()]
        if kind in ("text", "textarea", "date"):
            try:
                f.value = el.input_value() or ""
            except Exception:
                f.value = ""
        elif kind == "select":
            current = clean_label(el.inner_text()) if tag != "select" else (el.input_value() or "")
            # Workday shows "Select One" in an empty dropdown; treating that as an answer left every one unset
            f.value = "" if current.strip().lower() in ("select one", "select", "choose one", "-", "—") else current
        elif kind == "checkbox":
            f.value = "checked" if el.is_checked() else ""
        fields.append(f)
    scan.last_traps = traps          # readable by the caller for the run log; never returned as fillable
    return fields


# ------------------------------------------------------------------------------------------------ filling


# What an opened dropdown's choices look like. Workday uses several: a listbox of options, a prompt menu, and
# a panel of radio buttons you have to select (Country and Country Phone Code are radios).
OPTION_ITEMS = ("[role=option]", "[data-automation-id=promptOption]", "ul[role=listbox] li",
                "[role=radio]", "[role=menuitemradio]", "[role=menuitem]")


def option_items(page):  # noqa: ANN001, ANN201
    return page.locator(", ".join(OPTION_ITEMS))


def click_through(loc, timeout: int = 2500) -> bool:  # noqa: ANN001
    """Click something, escalating past whatever is drawn over it. Each way is tried in turn; the caller checks
    whether it worked, because a forced click that lands on an overlay still reports success."""
    for attempt in ("normal", "forced", "dispatch", "js"):
        with suppress(Exception):
            if attempt == "normal":
                loc.click(timeout=timeout)
            elif attempt == "forced":
                loc.click(timeout=timeout, force=True)
            elif attempt == "dispatch":
                loc.dispatch_event("click")
            else:
                loc.evaluate("el => el.click()")
            return True
    return False


def visible_options(scope) -> list[str]:  # noqa: ANN001
    """Every choice in an open menu: listbox options, menu items, or a panel of radios.

    Read in one go rather than element by element — a Degree list runs to 106 entries, and a cap of 80 put
    "Master of Science (MS or M.Sc)" out of reach, so the field was left unset with nothing to show for it.
    """
    out: list[str] = []
    with suppress(Exception):
        # visible only, and in one go: all_inner_texts() alone also returns the options of a *closed* list,
        # which made every dropdown look like it had stayed open after being set
        out = [clean_label(t) for t in option_items(scope).filter(visible=True).all_inner_texts()]
    out = [o for o in out if o]
    if not out:
        with suppress(Exception):
            radios = scope.locator("input[type=radio]")
            for i in range(min(radios.count(), 120)):
                with suppress(Exception):
                    if radios.nth(i).is_visible():
                        out.append(clean_label(radios.nth(i).evaluate(LABEL_JS)))
    return [o for o in out if o]


def open_options(field: Field, wait_ms: int = 2500) -> list[str]:
    """Open a dropdown and read its choices.

    The button is usually under Workday's `click_filter` shield, so a plain click quietly does nothing: the menu
    never opens, no options are found, and the field is left alone — which looked like the run bouncing between
    Country and Country Phone Code without ever choosing anything. Each way of clicking is therefore followed by
    an actual check for choices on screen.
    """
    loc = field.locator
    page = loc.page
    for attempt in ("normal", "forced", "dispatch", "js"):
        with suppress(Exception):
            if attempt == "normal":
                loc.click(timeout=2000)
            elif attempt == "forced":
                loc.click(timeout=2000, force=True)
            elif attempt == "dispatch":
                loc.dispatch_event("click")
            else:
                loc.evaluate("el => el.click()")
        deadline = time.monotonic() + (wait_ms / 1000)
        while time.monotonic() < deadline:
            page.wait_for_timeout(200)
            if opts := visible_options(menu_for(field) or page):
                field.options = opts
                return opts
    return []


def menu_for(field: Field):  # noqa: ANN201
    """The menu this dropdown just opened, so its choices are read on their own.

    Reading the whole page instead picked up every other open list — a Degree dropdown came back offering
    "Generative AI" and "Team Leadership" from the skills picker further down the form.
    """
    page = field.locator.page
    with suppress(Exception):
        owned = field.locator.get_attribute("aria-controls") or field.locator.get_attribute("aria-owns")
        if owned:
            target = page.locator(f"#{owned}")
            if target.count() and target.first.is_visible():
                return target.first
    for selector in ('[data-automation-id="promptOptions"]', '[role=listbox]', '[role=menu]', '[role=dialog]'):
        with suppress(Exception):
            menus = page.locator(selector)
            for i in range(menus.count() - 1, -1, -1):          # the newest one is the one just opened
                if menus.nth(i).is_visible():
                    return menus.nth(i)
    return None


def search_options(field: Field, text: str) -> list[str]:
    """Type into an open prompt to narrow its list, and read what comes back.

    Workday's longer prompts (Degree is one) show only the first slice of their options and expect you to type.
    Looking only at what is on screen meant "Master of Science" was simply not there to be found.
    """
    page = field.locator.page
    box = None
    menu = menu_for(field)
    if menu is None:
        # Never fall back to the page: the first editable box on it belongs to another field, and typing a
        # degree name into it put "Master of Science (MS) Information Systems" in the School field.
        return []
    for scope in (menu,):
        for selector in ('input[type="text"]', 'input[type="search"]', "input:not([type=hidden])",
                         "[role=combobox]"):
            candidate = scope.locator(selector)
            with suppress(Exception):
                for i in range(min(candidate.count(), 4)):
                    if candidate.nth(i).is_visible() and candidate.nth(i).is_editable():
                        box = candidate.nth(i)
                        break
            if box is not None:
                break
        if box is not None:
            break
    if box is None:
        return []
    with suppress(Exception):
        box.fill("")
        box.press_sequentially(text[:40], delay=25)
        page.wait_for_timeout(700)
        return visible_options(menu_for(field) or page)
    return []


def pick_option(page, choice: str) -> bool:  # noqa: ANN001
    """Choose one opened option by its text, whether it is an option, a menu item or a radio button."""
    exact = re.compile(rf"^\s*{re.escape(choice)}\s*$")
    with suppress(Exception):
        item = option_items(page).filter(has_text=exact).first
        if item.count() and click_through(item):
            return True
    for role in ("radio", "option", "menuitemradio", "checkbox"):
        with suppress(Exception):
            by_role = page.get_by_role(role, name=choice, exact=False).first
            if by_role.count():
                if role in ("radio", "checkbox"):
                    by_role.check(force=True)
                else:
                    click_through(by_role)
                return True
    with suppress(Exception):
        label = page.get_by_text(exact).first
        if label.count() and click_through(label):
            return True
    return False


def close_options(field: Field) -> None:
    try:
        field.locator.page.keyboard.press("Escape")
    except Exception:
        pass


def fill(field: Field, value: str, *, overwrite: bool = False) -> FillResult:
    """Put `value` into `field`. Never guesses an option: an unmatched choice comes back as `no_option`."""
    r = FillResult(field.label, field.kind, "error", value)
    try:
        if field.kind in ("text", "textarea", "date"):
            if field.filled and not overwrite:
                return FillResult(field.label, field.kind, "unchanged", field.value)
            from jobhunt.apply.workday import type_into

            with suppress(Exception):
                field.locator.fill(value)
            if field.locator.input_value() != value:   # a controlled component that ignored the programmatic set
                type_into(field.locator, value)
            r.status = "filled"
        elif field.kind == "checkbox":
            want = str(value).strip().lower() not in ("", "no", "false", "0", "unchecked")
            if field.locator.is_checked() != want:
                field.locator.set_checked(want)
            r.status = "filled"
            r.value = "checked" if want else "unchecked"
        elif field.kind == "file":
            field.locator.set_input_files(value)
            r.status = "filled"
        elif field.kind == "radio":
            page = field.locator.page
            opts = field.options
            pick = choose_option(value, opts)
            if not pick:
                return FillResult(field.label, field.kind, "no_option", value, opts)
            page.get_by_role("radio", name=pick, exact=False).first.check()
            r.status, r.value = "filled", pick
        elif field.kind == "select":
            native = field.locator.evaluate("e => e.tagName.toLowerCase()") == "select"
            opts = field.options if native else (field.options or open_options(field))
            pick = choose_option(value, opts)
            if not pick and not native:
                # a long prompt shows only its first slice: type to narrow it, then look again
                if narrowed := search_options(field, value):
                    opts = narrowed
                    pick = choose_option(value, narrowed)
            if not pick:
                if not native:
                    close_options(field)
                return FillResult(field.label, field.kind, "no_option", value, opts)
            if native:
                field.locator.select_option(label=pick)
            else:
                page = field.locator.page
                # click inside *this* dropdown's menu: with two Degree pickers on the page, searching the whole
                # document can select the option in the other one
                if not pick_option(menu_for(field) or page, pick):
                    close_options(field)
                    return FillResult(field.label, field.kind, "no_option", value, opts,
                                      detail=f"{pick!r} could not be selected")
                page.wait_for_timeout(400)
                # Still showing *these* choices means nothing was chosen. Asking "is any menu open" misses a
                # plain div of options, and "is any option visible" matches the form's own Yes/No radios.
                if set(opts) & set(visible_options(page)):
                    close_options(field)
                    return FillResult(field.label, field.kind, "no_option", value, opts,
                                      detail=f"the list stayed open after choosing {pick!r}")
            r.status, r.value = "filled", pick
        else:
            r.status = "unknown"
    except Exception as exc:
        r.status, r.detail = "error", f"{type(exc).__name__}: {exc}"
    return r


def summarize(results: Sequence[FillResult]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in results:
        out[r.status] = out.get(r.status, 0) + 1
    return out
