"""IMAP mail source. Opens the mailbox READ-ONLY and never sets \\Seen.

The user's unread state is their own triage signal; a classifier that destroys
it is worse than useless. Two independent guards enforce this: the folder is
selected read-only, and every fetch passes ``mark_seen=False``.
"""

from __future__ import annotations

import socket
from datetime import date, timezone
from typing import Iterator

from imap_tools import AND, MailBox, MailBoxUnencrypted
from imap_tools.errors import ImapToolsError

from ..models import RawMessage
from ..secrets import clean_password
from .base import SourceError


class IMAPSource:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        use_ssl: bool = True,
        folder: str = "INBOX",
        timeout: int = 45,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        # Defensive: also repairs credentials stored before this was added.
        self.password = clean_password(password)
        self.use_ssl = use_ssl
        self.folder = folder
        self.timeout = timeout

    def _open(self) -> MailBox:
        cls = MailBox if self.use_ssl else MailBoxUnencrypted
        try:
            box = cls(self.host, port=self.port, timeout=self.timeout)
            box.login(self.username, self.password, initial_folder=None)
            box.folder.set(self.folder, readonly=True)
            return box
        except UnicodeEncodeError as exc:
            bad = self.password[exc.start : exc.end]
            raise SourceError(
                f"The stored password contains a non-ASCII character "
                f"({bad!r}, U+{ord(bad[0]):04X}), which IMAP cannot send. "
                f"Re-add the account and retype the password by hand."
            ) from exc
        except (ImapToolsError, OSError, socket.timeout) as exc:
            raise SourceError(_explain(exc, self.host, self.username)) from exc

    def test(self) -> None:
        box = self._open()
        try:
            box.folder.list()
        except (ImapToolsError, OSError) as exc:
            raise SourceError(str(exc)) from exc
        finally:
            _quiet_logout(box)

    def fetch_unread(self, since: date) -> Iterator[RawMessage]:
        box = self._open()
        try:
            criteria = AND(seen=False, date_gte=since)
            for msg in box.fetch(criteria, mark_seen=False, bulk=True):
                yield _to_raw(msg, self.folder)
        except (ImapToolsError, OSError) as exc:
            raise SourceError(f"Fetch failed for {self.username}: {exc}") from exc
        finally:
            _quiet_logout(box)


def _quiet_logout(box: MailBox) -> None:
    try:
        box.logout()
    except Exception:  # noqa: BLE001 - teardown must never mask the real error
        pass


def _to_raw(msg, folder: str) -> RawMessage:
    headers = getattr(msg, "headers", {}) or {}
    message_id = ""
    raw_mid = headers.get("message-id") or headers.get("Message-ID")
    if raw_mid:
        message_id = raw_mid[0].strip()
    if not message_id:
        # Rare, but some senders omit it. UID is stable per folder, which is
        # good enough to key the cache on.
        message_id = f"<no-id-{folder}-{msg.uid}>"

    sender = msg.from_values
    from_addr = (sender.email if sender else msg.from_) or ""
    from_name = (sender.name if sender else "") or ""

    dt = msg.date
    # imap_tools returns 1900-01-01 for a Date header it cannot parse. Treat that
    # as "no date": stored as-is it would look ancient, get pruned and re-fetched
    # on every check, and never appear in a date window.
    if dt is not None and dt.year < 1971:
        dt = None
    if dt is not None and dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

    return RawMessage(
        message_id=message_id,
        uid=str(msg.uid or ""),
        folder=folder,
        from_addr=from_addr.lower(),
        from_name=from_name,
        subject=msg.subject or "(no subject)",
        date_utc=dt,
        text=msg.text or msg.html or "",
    )


def _explain(exc: Exception, host: str, username: str) -> str:
    """Turn opaque IMAP failures into something the user can act on."""
    text = str(exc)
    low = text.lower()
    if "authenticationfailed" in low.replace(" ", "") or "invalid credentials" in low:
        hint = (
            "Authentication failed. If this is Gmail, you need an App Password "
            "(2-Step Verification must be on) — your normal password will not work."
        )
        if "outlook" in host or "office365" in host or "hotmail" in host:
            hint = (
                "Authentication failed. Microsoft has disabled basic-auth IMAP for "
                "Outlook.com/Microsoft 365, so app passwords no longer work there. "
                "This account needs OAuth2, which mail-check does not support yet."
            )
        return f"{hint}\n  ({username} @ {host}: {text})"
    if isinstance(exc, socket.gaierror) or "name or service not known" in low:
        return f"Cannot resolve IMAP host '{host}'. Check the hostname."
    if "timed out" in low:
        return f"Connection to {host} timed out. Check the host, port, and your network."
    return f"{host}: {text}"
