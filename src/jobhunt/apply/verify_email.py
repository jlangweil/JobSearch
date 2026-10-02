"""Read the account-verification code Workday emails when you create a candidate account on a new tenant.

Uses the same read-only IMAP access as job-alert ingestion (`BODY.PEEK`, `readonly=True`): nothing is marked
read, moved or deleted. Only messages that arrived *after* the run asked for the code are considered, so an old
code from a previous tenant can never be typed into a new form.
"""

from __future__ import annotations

import email
import imaplib
import re
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from jobhunt.sources.email_alerts import EmailAccount, header, message_html

# "Your verification code is 123456", "Use code 123456 to verify", "Code: 123456"
CODE_NEAR_WORD = re.compile(r"(?:code|pin|passcode)\D{0,40}?(\d{4,8})\b", re.IGNORECASE)
CODE_ALONE = re.compile(r"(?<![\d.])(\d{6})(?![\d.])")
SUBJECT_HINTS = re.compile(r"verif|confirm|activate|one[- ]time|security code|account", re.IGNORECASE)
SENDER_HINTS = ("workday", "myworkday", "noreply", "no-reply", "donotreply")
_TAGS = re.compile(r"<[^>]+>")


LINK = re.compile(r"https?://[^\s<>\"'\])]+")


class VerificationError(RuntimeError):
    pass


def _plain(msg: email.message.Message) -> str:
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() == "text/plain":
            try:
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:  # noqa: BLE001 - a malformed part is not worth failing the lookup
                continue
    return _TAGS.sub(" ", message_html(msg))


def extract_code(text: str) -> str | None:
    """The code, preferring a number that is introduced as one over any six digits in the body.

    Links are stripped first: an activation URL carries the job requisition number and a long random token, and
    "R058770" reads exactly like a six-digit code.
    """
    text = LINK.sub(" ", text or "")
    if m := CODE_NEAR_WORD.search(text):
        return m.group(1)
    if m := CODE_ALONE.search(text):
        return m.group(1)
    return None


def extract_link(text: str, host_hint: str = "") -> str | None:
    """The activation URL out of an email body. Workday's "verify your candidate account" message carries a
    one-time /activate/ link instead of a code, and following it is the only way to finish the sign-up."""
    urls = [u.rstrip(".,)") for u in LINK.findall(text or "")]
    host = (host_hint or "").lower()
    for url in urls:
        low = url.lower()
        if "/activate/" in low or "/verify" in low or "confirm" in low:
            if not host or host in low:
                return url
    if host:
        for url in urls:
            if host in url.lower() and "unsubscribe" not in url.lower():
                return url
    return None


def _connect(account: EmailAccount):  # noqa: ANN202
    client = imaplib.IMAP4_SSL(account.host, account.port)
    client.login(account.user, account.password)
    return client


def recent_messages(account: EmailAccount, since: datetime, *, connect: Callable | None = None
                    ) -> Iterator[tuple[datetime | None, str, str, str]]:
    """(sent, from, subject, body) for messages since that moment, newest first."""
    client = (connect or _connect)(account)
    try:
        # the code rarely lands in a job-alert folder, so look in INBOX unless the account says otherwise
        status, _ = client.select(f'"{account.folder or "INBOX"}"', readonly=True)
        if status != "OK":
            raise VerificationError(f"mailbox folder {account.folder!r} not found")
        day = (since - timedelta(days=1)).strftime("%d-%b-%Y")   # IMAP SINCE has day granularity
        status, data = client.search(None, "SINCE", day)
        if status != "OK":
            raise VerificationError("the mail server refused the search")
        for uid in reversed((data[0] or b"").split()[-60:]):
            status, payload = client.fetch(uid, "(BODY.PEEK[])")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            msg = email.message_from_bytes(payload[0][1])
            try:
                sent = parsedate_to_datetime(msg.get("Date", ""))
                sent = sent.astimezone(UTC) if sent and sent.tzinfo else sent
            except (TypeError, ValueError):
                sent = None
            yield sent, header(msg, "From"), header(msg, "Subject"), _plain(msg)
    finally:
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def looks_like_verification(from_addr: str, subject: str, host_hint: str = "") -> bool:
    addr = (from_addr or "").lower()
    if host_hint and host_hint.split(".")[0] in addr:
        return True
    return any(h in addr for h in SENDER_HINTS) and bool(SUBJECT_HINTS.search(subject or ""))


def find_code(account: EmailAccount, since: datetime, *, host_hint: str = "", connect: Callable | None = None
              ) -> str | None:
    for sent, from_addr, subject, body in recent_messages(account, since, connect=connect):
        if sent and sent < since - timedelta(minutes=2):      # an older code is the wrong code
            continue
        if not looks_like_verification(from_addr, subject, host_hint):
            continue
        if code := extract_code(f"{subject}\n{body}"):
            return code
    return None


def find_link(account: EmailAccount, since: datetime, *, host_hint: str = "", connect: Callable | None = None
              ) -> str | None:
    for sent, from_addr, subject, body in recent_messages(account, since, connect=connect):
        if sent and sent < since - timedelta(minutes=2):
            continue
        if not looks_like_verification(from_addr, subject, host_hint):
            continue
        if link := extract_link(f"{subject}\n{body}", host_hint):
            return link
    return None


def wait_for(account: EmailAccount, since: datetime, finder: Callable, *, host_hint: str = "", timeout_s: int = 180,
             poll_s: int = 10, connect: Callable | None = None, sleep: Callable[[float], None] = time.sleep):
    """Poll the mailbox with `finder` until it returns something or the time runs out."""
    deadline = time.monotonic() + max(timeout_s, 0)
    while True:
        try:
            if found := finder(account, since, host_hint=host_hint, connect=connect):
                return found
        except (VerificationError, imaplib.IMAP4.error, OSError) as exc:
            raise VerificationError(f"could not read the mailbox for the verification email: {exc}") from exc
        if time.monotonic() >= deadline:
            return None
        sleep(min(poll_s, max(1, deadline - time.monotonic())))


def wait_for_link(account: EmailAccount, since: datetime, **kw) -> str | None:
    return wait_for(account, since, find_link, **kw)


def wait_for_code(account: EmailAccount, since: datetime, *, host_hint: str = "", timeout_s: int = 180,
                  poll_s: int = 10, connect: Callable | None = None, sleep: Callable[[float], None] = time.sleep
                  ) -> str | None:
    """Poll the mailbox until the code arrives or `timeout_s` passes (mail servers take a few seconds)."""
    deadline = time.monotonic() + max(timeout_s, 0)
    while True:
        try:
            if code := find_code(account, since, host_hint=host_hint, connect=connect):
                return code
        except (VerificationError, imaplib.IMAP4.error, OSError) as exc:
            raise VerificationError(f"could not read the mailbox for the verification code: {exc}") from exc
        if time.monotonic() >= deadline:
            return None
        sleep(min(poll_s, max(1, deadline - time.monotonic())))
