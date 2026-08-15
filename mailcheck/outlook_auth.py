"""MSAL public-client auth for personal Microsoft accounts.

Device authorization only — no client secret is shipped or stored, and no
redirect URI or hosted callback is needed. The token cache is serialized into
the OS keyring per account; SQLite and the TOML config never hold a token.

https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import keyring

from .secrets import SERVICE

#: Personal Microsoft accounts only. Work/school tenants are out of scope.
AUTHORITY = "https://login.microsoftonline.com/consumers"
#: The minimum delegated permission that includes message bodies.
SCOPES = ["Mail.Read"]

#: How long a pending device-code flow may sit in memory before it is dropped.
FLOW_TTL_SECONDS = 15 * 60


class OutlookAuthError(RuntimeError):
    pass


class ReconnectRequired(OutlookAuthError):
    """Consent was revoked, the account was removed, or the refresh token died.

    Callers treat this as a per-account failure so other accounts still check.
    """


#: Windows Credential Manager caps a credential blob at 2560 bytes — 1280 chars
#: as UTF-16. A serialized MSAL cache (access + refresh + id token) runs to
#: several thousand, so it is stored split across numbered entries. Measured, not
#: guessed: CredWrite fails with "The stub received bad data" above 1280.
CHUNK_CHARS = 1100
MAX_CHUNKS = 64


def _cache_key(label: str) -> str:
    return f"outlook-cache:{label}"


def _chunk_key(label: str, index: int) -> str:
    return f"outlook-cache:{label}:{index}"


def _read_blob(label: str) -> str:
    header = keyring.get_password(SERVICE, _cache_key(label))
    if not header:
        return ""
    if not header.startswith("chunks:"):
        return header  # written before chunking; still readable
    try:
        count = int(header.split(":", 1)[1])
    except ValueError:
        return ""
    parts = []
    for i in range(min(count, MAX_CHUNKS)):
        piece = keyring.get_password(SERVICE, _chunk_key(label, i))
        if piece is None:
            return ""  # torn write — treat as no cache and re-authenticate
        parts.append(piece)
    return "".join(parts)


def _write_blob(label: str, blob: str) -> None:
    chunks = [blob[i : i + CHUNK_CHARS] for i in range(0, len(blob), CHUNK_CHARS)] or [""]
    if len(chunks) > MAX_CHUNKS:
        raise OutlookAuthError(
            f"Token cache is unexpectedly large ({len(blob)} chars); refusing to store it."
        )
    for i, piece in enumerate(chunks):
        keyring.set_password(SERVICE, _chunk_key(label, i), piece)
    keyring.set_password(SERVICE, _cache_key(label), f"chunks:{len(chunks)}")
    # Drop chunks left over from a previously longer cache.
    for i in range(len(chunks), MAX_CHUNKS):
        if keyring.get_password(SERVICE, _chunk_key(label, i)) is None:
            break
        _delete_quietly(_chunk_key(label, i))


def _delete_quietly(key: str) -> None:
    try:
        keyring.delete_password(SERVICE, key)
    except keyring.errors.PasswordDeleteError:
        pass


def load_cache(label: str) -> Any:
    """Deserialize this account's MSAL cache out of the keyring."""
    import msal

    cache = msal.SerializableTokenCache()
    blob = _read_blob(label)
    if blob:
        try:
            cache.deserialize(blob)
        except Exception:  # noqa: BLE001 - a corrupt cache just means re-authenticate
            return msal.SerializableTokenCache()
    return cache


def save_cache(label: str, cache) -> None:
    if cache.has_state_changed:
        _write_blob(label, cache.serialize())


def delete_cache(label: str) -> None:
    """Called on account removal so no token survives the account."""
    _delete_quietly(_cache_key(label))
    for i in range(MAX_CHUNKS):
        _delete_quietly(_chunk_key(label, i))


def has_cache(label: str) -> bool:
    return bool(keyring.get_password(SERVICE, _cache_key(label)))


def _app(client_id: str, cache):
    try:
        import msal
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise OutlookAuthError(
            "The 'msal' package is required for Outlook accounts. "
            "Install it with: pip install msal"
        ) from exc
    if not client_id:
        raise OutlookAuthError(
            "No Microsoft application client ID configured. Set outlook.client_id "
            "in Settings — register a public client application in the Microsoft "
            "Entra admin center to obtain one. It is a public identifier, not a secret."
        )
    return msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache)


# ------------------------------------------------------------------ device flow


@dataclass
class PendingFlow:
    """A device-code flow awaiting the user. Memory only, never written to disk.

    ``id`` distinguishes this attempt from any other — including a retry for the
    same label — so a stale background thread can recognise it has been
    superseded and must not act.
    """

    label: str
    client_id: str
    flow: dict
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: float = field(default_factory=time.time)

    @property
    def user_code(self) -> str:
        return self.flow.get("user_code", "")

    @property
    def verification_uri(self) -> str:
        return self.flow.get("verification_uri") or self.flow.get("verification_url", "")

    @property
    def expired(self) -> bool:
        return time.time() - self.started_at > FLOW_TTL_SECONDS


def begin_device_flow(label: str, client_id: str) -> PendingFlow:
    cache = load_cache(label)
    app = _app(client_id, cache)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise OutlookAuthError(
            f"Could not start Microsoft sign-in: {flow.get('error_description') or flow}"
        )
    return PendingFlow(label=label, client_id=client_id, flow=flow)


def abort(pending: PendingFlow) -> None:
    """Unblock a background ``complete_device_flow`` call for this flow.

    MSAL documents this exact mechanism: "You can abort the polling loop at any
    time, by changing the value of the flow's 'expires_at' key to 0" — but only
    if the caller holds the *same* dict object the blocking call is polling, not
    a copy. ``complete_device_flow`` below deliberately never copies ``pending.flow``
    so this works.
    """
    pending.flow["expires_at"] = 0


def complete_device_flow(pending: PendingFlow) -> str:
    """Block until the user finishes signing in, or ``abort()`` is called.

    Runs on a background thread; the web UI polls its state rather than waiting.
    Mutates ``pending.flow`` directly (no defensive copy) so a cancellation
    from another thread can reach the same dict MSAL is polling — see ``abort()``.
    """
    cache = load_cache(pending.label)
    app = _app(pending.client_id, cache)
    result = app.acquire_token_by_device_flow(pending.flow)
    if "access_token" not in result:
        error = result.get("error", "")
        description = result.get("error_description") or str(result)
        if error in ("authorization_declined", "expired_token", "bad_verification_code"):
            raise OutlookAuthError(f"Sign-in was not completed: {description}")
        raise OutlookAuthError(f"Microsoft sign-in failed: {description}")

    save_cache(pending.label, cache)
    claims = result.get("id_token_claims") or {}
    return (
        claims.get("preferred_username")
        or claims.get("email")
        or result.get("account", {}).get("username", "")
    )


def acquire_token(label: str, client_id: str) -> str:
    """Return a valid access token, refreshing silently from the cache.

    Raises ReconnectRequired when the cache can no longer produce one — the
    caller surfaces that for this account only.
    """
    cache = load_cache(label)
    app = _app(client_id, cache)
    accounts = app.get_accounts()
    if not accounts:
        raise ReconnectRequired(
            f"No stored Microsoft sign-in for '{label}'. Reconnect the account."
        )
    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    save_cache(label, cache)
    if not result or "access_token" not in result:
        raise ReconnectRequired(
            f"Microsoft sign-in for '{label}' has expired or been revoked. "
            f"Reconnect the account."
        )
    return result["access_token"]
