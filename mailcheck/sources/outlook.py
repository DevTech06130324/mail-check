"""Microsoft Graph mail source for personal Outlook.com accounts.

Every call is a GET. Graph is never asked to mark read, move, or modify
anything — the local Done state replaces all of that.

https://learn.microsoft.com/en-us/graph/api/user-list-messages
"""

from __future__ import annotations

import random
import time
from datetime import date, datetime, timezone
from typing import Iterator

import httpx

from ..models import RawMessage
from ..outlook_auth import OutlookAuthError, ReconnectRequired, acquire_token
from .base import SourceError

GRAPH = "https://graph.microsoft.com/v1.0"
MESSAGES_URL = f"{GRAPH}/me/mailFolders/inbox/messages"

#: Only the fields the pipeline consumes. Bodies dominate the payload, so an
#: explicit $select keeps responses small.
SELECT = (
    "id,internetMessageId,subject,from,receivedDateTime,isRead,webLink,bodyPreview,body"
)
PAGE_SIZE = 50
MAX_PAGES = 40  # a hard stop so a pathological mailbox cannot loop forever


class OutlookGraphSource:
    def __init__(
        self,
        *,
        label: str,
        client_id: str,
        email: str = "",
        timeout: int = 45,
        max_retries: int = 4,
    ) -> None:
        self.label = label
        self.client_id = client_id
        self.email = email
        self.timeout = timeout
        self.max_retries = max_retries

    # ------------------------------------------------------------------ http

    def _token(self) -> str:
        try:
            return acquire_token(self.label, self.client_id)
        except ReconnectRequired as exc:
            raise SourceError(str(exc)) from exc
        except OutlookAuthError as exc:
            raise SourceError(str(exc)) from exc

    def _get(self, client: httpx.Client, url: str, params: dict | None = None) -> dict:
        for attempt in range(self.max_retries):
            try:
                resp = client.get(url, params=params)
            except httpx.TransportError as exc:
                if attempt == self.max_retries - 1:
                    raise SourceError(f"Could not reach Microsoft Graph: {exc}") from exc
                time.sleep(_backoff(attempt))
                continue

            if resp.status_code in (429, 503, 504) or 500 <= resp.status_code < 600:
                if attempt == self.max_retries - 1:
                    raise SourceError(
                        f"Microsoft Graph is unavailable (HTTP {resp.status_code})."
                    )
                time.sleep(_retry_after(resp) or _backoff(attempt))
                continue

            if resp.status_code in (401, 403):
                raise SourceError(
                    f"Microsoft rejected the stored sign-in for '{self.label}' "
                    f"(HTTP {resp.status_code}). Reconnect the account."
                )
            if resp.status_code >= 400:
                raise SourceError(
                    f"Microsoft Graph error {resp.status_code}: {resp.text[:200]}"
                )
            try:
                return resp.json()
            except ValueError as exc:
                raise SourceError("Microsoft Graph returned a non-JSON response.") from exc
        raise SourceError("Microsoft Graph request failed after retries.")

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Accept": "application/json",
                # Ask Graph to convert HTML bodies to text server-side; saves us
                # the conversion and a lot of payload.
                "Prefer": 'outlook.body-content-type="text"',
            },
        )

    # ---------------------------------------------------------- MailSource API

    def test(self) -> None:
        with self._client() as client:
            data = self._get(client, f"{GRAPH}/me", {"$select": "mail,userPrincipalName"})
        if not (data.get("mail") or data.get("userPrincipalName")):
            raise SourceError("Signed in, but Microsoft returned no mailbox address.")

    def fetch_unread(self, since: date) -> Iterator[RawMessage]:
        since_iso = datetime(
            since.year, since.month, since.day, tzinfo=timezone.utc
        ).isoformat().replace("+00:00", "Z")

        params = {
            # Graph requires the $orderby property to appear first in $filter,
            # or the messages endpoint rejects the query with InefficientFilter.
            "$filter": f"receivedDateTime ge {since_iso} and isRead eq false",
            "$select": SELECT,
            "$top": str(PAGE_SIZE),
            "$orderby": "receivedDateTime desc",
        }
        with self._client() as client:
            url, pages = MESSAGES_URL, 0
            while url and pages < MAX_PAGES:
                data = self._get(client, url, params)
                for item in data.get("value") or []:
                    yield _to_raw(item)
                # nextLink already carries every query parameter.
                url, params, pages = data.get("@odata.nextLink"), None, pages + 1


def _to_raw(item: dict) -> RawMessage:
    sender = ((item.get("from") or {}).get("emailAddress")) or {}
    body = item.get("body") or {}
    text = body.get("content") or item.get("bodyPreview") or ""

    received = item.get("receivedDateTime")
    dt = None
    if received:
        try:
            dt = datetime.fromisoformat(received.replace("Z", "+00:00"))
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            dt = None

    # internetMessageId is the cross-provider identity; the Graph id is a stable
    # per-mailbox fallback when a sender omitted the header.
    message_id = (item.get("internetMessageId") or "").strip()
    if not message_id:
        message_id = f"<graph-{item.get('id', '')}>"

    return RawMessage(
        message_id=message_id,
        uid=str(item.get("id") or ""),
        folder="Inbox",
        from_addr=(sender.get("address") or "").lower(),
        from_name=sender.get("name") or "",
        subject=item.get("subject") or "(no subject)",
        date_utc=dt,
        text=text,
        provider_url=item.get("webLink"),
    )


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if not value:
        return None
    try:
        return min(float(value), 60.0)
    except ValueError:
        return None


def _backoff(attempt: int) -> float:
    return min(2**attempt, 30) + random.uniform(0, 1)
