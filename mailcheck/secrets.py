"""Credentials live in the OS keyring (Windows Credential Manager), never the DB."""

from __future__ import annotations

import re
import unicodedata

import keyring

SERVICE = "mail-check"
LLM_TOKEN_KEY = "llm-token"


class SecretError(RuntimeError):
    pass


_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)
#: Gmail-style app password: four groups of four alphanumerics.
_APP_PASSWORD = re.compile(r"(?:[A-Za-z0-9]{4}[\s]+){3}[A-Za-z0-9]{4}\Z")


def clean_password(raw: str) -> str:
    """Repair a pasted app password.

    Google renders app passwords as ``abcd efgh ijkl mnop``, and copying from
    that page yields non-breaking spaces. imaplib encodes commands as ASCII, so
    a U+00A0 crashes the login with a UnicodeEncodeError rather than a useful
    error. Normalise the separators, and collapse the grouped form to the 16
    characters the server actually expects.
    """
    text = raw.translate(_ZERO_WIDTH)
    # Any Unicode space separator (nbsp, narrow nbsp, figure space...) -> plain.
    text = "".join(" " if unicodedata.category(c) == "Zs" else c for c in text)
    text = text.strip()
    if _APP_PASSWORD.match(text):
        return re.sub(r"\s+", "", text)
    return text


def check_ascii(password: str) -> None:
    """IMAP LOGIN is ASCII-only; fail with an explanation, not a traceback."""
    try:
        password.encode("ascii")
    except UnicodeEncodeError as exc:
        bad = password[exc.start : exc.end]
        raise SecretError(
            f"The password contains a non-ASCII character ({bad!r}, U+{ord(bad[0]):04X}) "
            f"at position {exc.start}, which IMAP cannot send.\n"
            "This usually means it was pasted with invisible formatting. Retype it "
            "by hand, or paste it into a plain text editor first."
        ) from None


def _key_for_account(label: str) -> str:
    return f"account:{label}"


def set_account_password(label: str, password: str) -> None:
    """Store a password, repaired and validated so a bad paste fails here with
    a clear message rather than deep inside imaplib."""
    cleaned = clean_password(password)
    check_ascii(cleaned)
    keyring.set_password(SERVICE, _key_for_account(label), cleaned)


def get_account_password(label: str) -> str:
    value = keyring.get_password(SERVICE, _key_for_account(label))
    if not value:
        raise SecretError(
            f"No stored password for account '{label}'. "
            f"Re-add it with: mail-check account add"
        )
    return value


def delete_account_password(label: str) -> None:
    try:
        keyring.delete_password(SERVICE, _key_for_account(label))
    except keyring.errors.PasswordDeleteError:
        pass


def set_llm_token(token: str) -> None:
    keyring.set_password(SERVICE, LLM_TOKEN_KEY, token)


def get_llm_token() -> str:
    value = keyring.get_password(SERVICE, LLM_TOKEN_KEY)
    if not value:
        raise SecretError("No OmniRoute auth token stored. Run: mail-check init")
    return value


def has_llm_token() -> bool:
    return bool(keyring.get_password(SERVICE, LLM_TOKEN_KEY))
