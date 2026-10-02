"""Storage for ATS site passwords, either encrypted or in plain text.

Two modes, chosen by `auto_apply.store_passwords`:

* `encrypted` (default): Windows DPAPI (`CryptProtectData`), so the stored value is bound to this Windows user
  account and a copied jobhunt.db yields nothing.
* `plain`: the password is kept as typed and shown in the UI. The owner asked for this, and it has one real
  advantage — a password jobhunt generates for a new site can be read back and used by hand, which an encrypted
  one cannot be if it is ever overwritten. The cost: anything that can read `data/jobhunt.db` (or reach the web
  app on the network) can read the password.

Either form is readable back, so switching modes never strands an existing row.
"""

from __future__ import annotations

import base64
import ctypes
import os
import sys
from ctypes import wintypes

ENV_PASSWORD = "JOBHUNT_ATS_PASSWORD"
PREFIX = "dpapi:"
PLAIN_PREFIX = "plain:"
MODES = ("encrypted", "plain")


class CredentialError(RuntimeError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    @classmethod
    def of(cls, data: bytes) -> _Blob:
        buf = ctypes.create_string_buffer(data, len(data))
        return cls(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def take(self) -> bytes:
        out = ctypes.string_at(self.pbData, self.cbData)
        ctypes.windll.kernel32.LocalFree(self.pbData)  # type: ignore[attr-defined]
        return out


def available() -> bool:
    return sys.platform == "win32"


def _crypt(fn_name: str, data: bytes, description: str | None = None) -> bytes:
    if not available():
        raise CredentialError(
            f"saving passwords needs Windows DPAPI, which this platform does not have. Put the password in the "
            f"{ENV_PASSWORD} environment variable instead.")
    fn = getattr(ctypes.windll.crypt32, fn_name)  # type: ignore[attr-defined]
    blob_in, blob_out = _Blob.of(data), _Blob()
    desc = ctypes.c_wchar_p(description) if description else None
    args = [ctypes.byref(blob_in), desc if fn_name == "CryptProtectData" else None, None, None, None, 0,
            ctypes.byref(blob_out)]
    if not fn(*args):
        raise CredentialError(f"{fn_name} failed (Windows error {ctypes.GetLastError()})")
    return blob_out.take()


def protect(secret: str, *, description: str = "jobhunt ATS credential", mode: str = "encrypted") -> str:
    """Prepare a password for storage in the chosen mode. The prefix says which, so both can be read back."""
    if not secret:
        raise CredentialError("password is empty")
    if mode == "plain":
        return PLAIN_PREFIX + secret
    return PREFIX + base64.b64encode(_crypt("CryptProtectData", secret.encode("utf-8"), description)).decode("ascii")


def unprotect(stored: str) -> str:
    """The password back, whichever way it was stored."""
    if stored.startswith(PLAIN_PREFIX):
        return stored[len(PLAIN_PREFIX):]
    if not stored.startswith(PREFIX):
        raise CredentialError("stored credential is not in a form jobhunt wrote; re-enter the password")
    return _crypt("CryptUnprotectData", base64.b64decode(stored[len(PREFIX):])).decode("utf-8")


def reveal(stored: str | None) -> str:
    """For showing on the page. Never raises: an unreadable row simply shows nothing."""
    if not stored:
        return ""
    try:
        return unprotect(stored)
    except Exception:
        return ""


def is_plain(stored: str | None) -> bool:
    return bool(stored and stored.startswith(PLAIN_PREFIX))


def password_for(stored: str | None) -> str:
    """The password to use: the environment override wins, so a shared or CI machine never needs one at rest."""
    if env := os.environ.get(ENV_PASSWORD):
        return env
    if not stored:
        raise CredentialError("no password saved for this site and no " + ENV_PASSWORD + " set")
    return unprotect(stored)
