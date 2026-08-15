# Personal Outlook Mail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add read-only unread-mail ingestion for personal Outlook.com, Hotmail, and Live accounts through Microsoft Graph while preserving the existing provider-neutral pipeline.

**Architecture:** Extend account and message persistence with a provider discriminator and provider deep link, implement MSAL public-client device authorization with a per-account keyring-backed token cache, and add an `OutlookGraphSource` that maps Graph messages into `RawMessage`. The Accounts page owns the asynchronous connection flow; the existing pipeline, classifier, cache, reports, scheduler, and local Done state remain provider-neutral.

**Tech Stack:** Python 3.11+, `msal>=1.37,<2`, `httpx>=0.27`, Microsoft Graph v1.0, SQLite, OS keyring, FastAPI, Jinja2, vanilla JavaScript

## Global Constraints

- This plan follows `2026-07-30-daily-triage-ux.md` and starts from schema version 2.
- Support only personal Outlook.com, Hotmail, and Live Microsoft accounts.
- Use the Microsoft identity `/consumers` authority and delegated `Mail.Read`.
- Treat mail-check as a public client: never create, request, ship, or store a client secret.
- `outlook.client_id` is a required non-secret deployment setting supplied by a Microsoft app registration configured for personal accounts and public-client device flow.
- Store serialized MSAL token caches per mail-check account in the OS keyring; store no OAuth token or device code in SQLite or TOML.
- Perform GET-only Graph mail requests. Never send, delete, move, mark read, categorize, or otherwise mutate provider mail.
- Reapply authorization and `Prefer: outlook.body-content-type="text"` headers to every `@odata.nextLink` request.
- One broken Outlook account must not abort checks for other accounts.
- Honor `Retry-After` for 429 responses and retry only 429 and transient 5xx responses with a bounded delay.
- Keep Microsoft 365 work/school accounts, shared mailboxes, webhooks, delta sync, calendar, and contacts out of scope.

---

## File Map

- Modify `pyproject.toml`: add the stable MSAL dependency.
- Modify `mailcheck/config.py`: non-secret Outlook client ID configuration.
- Modify `mailcheck/db.py`: provider and provider URL schema migration and account mapping.
- Modify `mailcheck/models.py`: provider deep link on raw and normalized messages.
- Modify `mailcheck/normalize.py`: preserve provider deep links during normalization.
- Modify `mailcheck/secrets.py`: keyring-backed serialized MSAL cache operations.
- Create `mailcheck/outlook_auth.py`: MSAL cache, silent token, and device-flow operations.
- Create `mailcheck/sources/outlook.py`: read-only Microsoft Graph adapter.
- Modify `mailcheck/sources/__init__.py`: export the Graph source.
- Modify `mailcheck/pipeline.py`: provider-aware source construction.
- Modify `mailcheck/cli.py`: provider-aware list, test, and removal behavior.
- Modify `mailcheck/web/app.py`: connection job APIs, provider-aware account APIs, and deep links.
- Modify `mailcheck/web/templates/accounts.html`: provider choice and Outlook device-code states.
- Modify `mailcheck/web/static/app.js`: generic sheet behavior already supplied by the UX plan.
- Modify `mailcheck/web/static/app.css`: provider picker and connection-state styling.
- Modify `tests/test_smoke.py`: migration, auth-cache, Graph paging/retry/mapping, pipeline, and web-flow coverage.
- Modify `README.md`: setup, permissions, privacy, and limitations.

### Task 1: Add provider-neutral persistence fields

**Files:**
- Modify: `mailcheck/db.py`
- Modify: `mailcheck/models.py`
- Modify: `mailcheck/normalize.py`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Produces: `Account.provider: str`
- Produces: `RawMessage.open_url: str | None`
- Produces: `NormalizedMessage.open_url: str | None`
- Produces: `accounts.provider: TEXT NOT NULL DEFAULT 'imap'`
- Produces: `messages.provider_url: TEXT | NULL`

- [ ] **Step 1: Add a failing v2-to-v3 migration test**

Extend the daily-triage migration test:

```python
conn.execute("PRAGMA user_version = 2")
conn.commit()
conn.close()

conn = db.connect(path)
account_columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
message_columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
check("v2 database gains provider", "provider" in account_columns)
check("v2 database gains provider_url", "provider_url" in message_columns)
check("schema advances to v3",
      conn.execute("PRAGMA user_version").fetchone()[0] == 3)
```

For the legacy account copied by the test, assert:

```python
check("existing account migrates as IMAP",
      db.list_accounts(conn)[0].provider == "imap")
```

- [ ] **Step 2: Run the smoke test and verify it fails**

Run: `python tests/test_smoke.py`

Expected: FAIL because provider fields do not exist.

- [ ] **Step 3: Implement the v3 schema migration**

Set `SCHEMA_VERSION = 3`. Add to the new-database schema:

```sql
provider      TEXT NOT NULL DEFAULT 'imap',
```

after `accounts.email`, and:

```sql
provider_url  TEXT,
```

after `messages.snippet`.

Extend `_migrate()`:

```python
if version < 3:
    if "provider" not in _column_names(conn, "accounts"):
        conn.execute(
            "ALTER TABLE accounts ADD COLUMN provider TEXT NOT NULL DEFAULT 'imap'"
        )
    if "provider_url" not in _column_names(conn, "messages"):
        conn.execute("ALTER TABLE messages ADD COLUMN provider_url TEXT")
```

Add `provider: str` to `Account`, read it in `_row_to_account()`, and add
`provider: str = "imap"` to `add_account()`. Include `provider` in the INSERT.

- [ ] **Step 4: Carry provider URLs through normalization**

Append this defaulted field after `RawMessage.text`:

```python
open_url: str | None = None
```

Append this defaulted field after `NormalizedMessage.body`:

```python
open_url: str | None = None
```

In `normalize.normalize()`, pass `open_url=raw.open_url`. In `db.upsert_message()`, insert
`provider_url` and update it on conflict:

```sql
provider_url = excluded.provider_url
```

Pass `msg.open_url` in the values tuple. Include `m.provider_url`, `a.provider`, and `a.email
AS account_email` in `query_triaged()`.

- [ ] **Step 5: Add a persistence assertion**

After upserting a normalized message with
`open_url="https://outlook.live.com/mail/0/inbox/id/example"`, assert:

```python
row = db.query_triaged(conn)[0]
check("provider URL persists", row["provider_url"].startswith("https://outlook.live.com/"))
```

- [ ] **Step 6: Run the smoke test**

Run: `python tests/test_smoke.py`

Expected: all checks pass.

- [ ] **Step 7: Commit the provider-neutral schema**

```bash
git add mailcheck/db.py mailcheck/models.py mailcheck/normalize.py tests/test_smoke.py
git commit -m "feat: add provider-aware account persistence"
```

### Task 2: Add keyring-backed MSAL authentication

**Files:**
- Modify: `pyproject.toml`
- Modify: `mailcheck/config.py`
- Modify: `mailcheck/secrets.py`
- Create: `mailcheck/outlook_auth.py`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Produces: `OutlookConfig(client_id: str = "")`
- Produces: `begin_device_flow(label: str, client_id: str) -> dict`
- Produces: `complete_device_flow(label: str, client_id: str, flow: dict) -> dict`
- Produces: `get_access_token(label: str, client_id: str) -> str`
- Produces: `delete_outlook_token_cache(label: str) -> None`

- [ ] **Step 1: Add MSAL and configuration**

Add to dependencies:

```toml
"msal>=1.37,<2",
```

Add:

```python
class OutlookConfig(BaseModel):
    client_id: str = ""
    """Public Microsoft application ID; it is an identifier, not a secret."""
```

and to `Config`:

```python
outlook: OutlookConfig = Field(default_factory=OutlookConfig)
```

Extend `set_dotted()` coverage in the smoke test:

```python
cfg = cfgmod.set_dotted(cfgmod.Config(), "outlook.client_id", "client-id")
check("Outlook client ID is configurable", cfg.outlook.client_id == "client-id")
```

- [ ] **Step 2: Add failing token-cache tests**

In a new `test_outlook_auth()` function, monkeypatch the keyring calls in `secrets` with an
in-memory dictionary and assert:

```python
secrets.set_outlook_token_cache("outlook", "serialized-cache")
check("Outlook cache round-trips",
      secrets.get_outlook_token_cache("outlook") == "serialized-cache")
secrets.delete_outlook_token_cache("outlook")
check("Outlook cache deletes", secrets.get_outlook_token_cache("outlook") is None)
```

- [ ] **Step 3: Implement token-cache keyring functions**

Add:

```python
def _outlook_cache_key(label: str) -> str:
    return f"outlook-token-cache:{label}"


def set_outlook_token_cache(label: str, serialized: str) -> None:
    keyring.set_password(SERVICE, _outlook_cache_key(label), serialized)


def get_outlook_token_cache(label: str) -> str | None:
    return keyring.get_password(SERVICE, _outlook_cache_key(label))


def delete_outlook_token_cache(label: str) -> None:
    try:
        keyring.delete_password(SERVICE, _outlook_cache_key(label))
    except keyring.errors.PasswordDeleteError:
        pass
```

- [ ] **Step 4: Implement the MSAL public-client wrapper**

Create `mailcheck/outlook_auth.py`:

```python
from __future__ import annotations

import msal

from . import secrets

AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Mail.Read"]


class OutlookAuthError(RuntimeError):
    pass


def _application(label: str, client_id: str):
    if not client_id.strip():
        raise OutlookAuthError(
            "Microsoft application client ID is not configured."
        )
    cache = msal.SerializableTokenCache()
    serialized = secrets.get_outlook_token_cache(label)
    if serialized:
        cache.deserialize(serialized)
    app = msal.PublicClientApplication(
        client_id.strip(), authority=AUTHORITY, token_cache=cache
    )
    return app, cache


def _save_changed(label: str, cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        secrets.set_outlook_token_cache(label, cache.serialize())


def begin_device_flow(label: str, client_id: str) -> dict:
    app, cache = _application(label, client_id)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise OutlookAuthError(
            flow.get("error_description") or "Microsoft did not start sign-in."
        )
    _save_changed(label, cache)
    return flow


def complete_device_flow(label: str, client_id: str, flow: dict) -> dict:
    app, cache = _application(label, client_id)
    result = app.acquire_token_by_device_flow(flow)
    _save_changed(label, cache)
    if "access_token" not in result:
        raise OutlookAuthError(
            result.get("error_description")
            or result.get("error")
            or "Microsoft sign-in failed."
        )
    return result


def get_access_token(label: str, client_id: str) -> str:
    app, cache = _application(label, client_id)
    accounts = app.get_accounts()
    if not accounts:
        raise OutlookAuthError("Reconnect this Outlook account.")
    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    _save_changed(label, cache)
    if not result or "access_token" not in result:
        raise OutlookAuthError("Reconnect this Outlook account.")
    return result["access_token"]
```

Do not log or return the serialized cache, device code, access token, or refresh token.

- [ ] **Step 5: Test MSAL behavior with a fake module client**

Replace `outlook_auth.msal.PublicClientApplication` in the test with a fake that records
`authority`, scopes, and cache writes. Assert:

```python
check("Outlook uses consumers authority", fake.authority.endswith("/consumers"))
check("Outlook requests only Mail.Read", fake.scopes == ["Mail.Read"])
check("silent token succeeds", outlook_auth.get_access_token("outlook", "cid") == "token")
```

Also make `acquire_token_silent()` return `None` and assert `OutlookAuthError` contains
“Reconnect”.

- [ ] **Step 6: Run the smoke test**

Run: `python tests/test_smoke.py`

Expected: all auth-cache and existing checks pass.

- [ ] **Step 7: Commit authentication**

```bash
git add pyproject.toml mailcheck/config.py mailcheck/secrets.py mailcheck/outlook_auth.py tests/test_smoke.py
git commit -m "feat: add personal Outlook authentication"
```

### Task 3: Implement the read-only Microsoft Graph source

**Files:**
- Create: `mailcheck/sources/outlook.py`
- Modify: `mailcheck/sources/__init__.py`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Produces: `OutlookGraphSource(token_provider: Callable[[], str], client: httpx.Client | None = None)`
- Implements: `test() -> None`
- Implements: `fetch_unread(since: date) -> Iterator[RawMessage]`

- [ ] **Step 1: Add a paged Graph fixture and failing mapping test**

Use `httpx.MockTransport`:

```python
def test_outlook_source():
    print("\nOutlook Graph source")
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/mailFolders/inbox"):
            return httpx.Response(200, json={"id": "inbox", "displayName": "Inbox"})
        if "page=2" in str(request.url):
            return httpx.Response(200, json={"value": [{
                "id": "graph-2", "internetMessageId": None,
                "subject": "Assessment", "receivedDateTime": "2026-07-30T14:00:00Z",
                "from": {"emailAddress": {"address": "jobs@example.com", "name": "Jobs"}},
                "body": {"contentType": "text", "content": "Complete the test."},
                "webLink": "https://outlook.live.com/mail/0/inbox/id/graph-2"
            }]})
        return httpx.Response(200, json={
            "value": [{
                "id": "graph-1", "internetMessageId": "<graph-1@example.com>",
                "subject": "Interview", "receivedDateTime": "2026-07-30T13:00:00Z",
                "from": {"emailAddress": {"address": "HR@Example.com", "name": "Recruiting"}},
                "body": {"contentType": "text", "content": "Choose a time."},
                "webLink": "https://outlook.live.com/mail/0/inbox/id/graph-1"
            }],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?page=2"
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = OutlookGraphSource(token_provider=lambda: "token", client=client)
    source.test()
    rows = list(source.fetch_unread(date(2026, 7, 1)))
    check("Graph paging yields every message", len(rows) == 2)
    check("Graph message ID is preserved", rows[0].message_id == "<graph-1@example.com>")
    check("missing internet ID has stable fallback",
          rows[1].message_id == "<outlook-graph-2>")
    check("sender is normalized", rows[0].from_addr == "hr@example.com")
    check("Outlook webLink is preserved", rows[0].open_url.endswith("/graph-1"))
    check("plain-text preference is sent on every page",
          all(r.headers.get("Prefer") == 'outlook.body-content-type="text"'
              for r in requests))
```

- [ ] **Step 2: Run the smoke test and verify it fails**

Run: `python tests/test_smoke.py`

Expected: import failure for `OutlookGraphSource`.

- [ ] **Step 3: Implement Graph requests, mapping, and paging**

Create `mailcheck/sources/outlook.py` with:

```python
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
_SELECT = "id,internetMessageId,subject,receivedDateTime,from,body,webLink"

class OutlookGraphSource:
    def __init__(self, *, token_provider, client=None, timeout=45):
        self.token_provider = token_provider
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout)

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.token_provider()}",
            "Prefer": 'outlook.body-content-type="text"',
        }

    def test(self) -> None:
        try:
            self._request(
                f"{GRAPH_ROOT}/me/mailFolders/inbox",
                params={"$select": "id,displayName"},
            )
        finally:
            if self._owns_client:
                self.client.close()

    def fetch_unread(self, since: date):
        url = f"{GRAPH_ROOT}/me/mailFolders/inbox/messages"
        params = {
            "$select": _SELECT,
            "$filter": (
                f"receivedDateTime ge {since.isoformat()}T00:00:00Z "
                "and isRead eq false"
            ),
            "$orderby": "receivedDateTime desc",
            "$top": "100",
        }
        try:
            while url:
                payload = self._request(url, params=params)
                params = None
                for item in payload.get("value", []):
                    yield _to_raw(item)
                url = payload.get("@odata.nextLink")
        finally:
            if self._owns_client:
                self.client.close()
```

Implement `_to_raw()` with:

```python
sender = (item.get("from") or {}).get("emailAddress") or {}
graph_id = item.get("id") or ""
internet_id = (item.get("internetMessageId") or "").strip()
dt = datetime.fromisoformat(item["receivedDateTime"].replace("Z", "+00:00"))
return RawMessage(
    message_id=internet_id or f"<outlook-{graph_id}>",
    uid=graph_id,
    folder="INBOX",
    from_addr=(sender.get("address") or "").lower(),
    from_name=sender.get("name") or "",
    subject=item.get("subject") or "(no subject)",
    date_utc=dt.astimezone(timezone.utc).replace(tzinfo=None),
    text=(item.get("body") or {}).get("content") or "",
    open_url=item.get("webLink"),
)
```

- [ ] **Step 4: Add bounded retry behavior**

In `_request()`, retry at most four attempts for 429, 500, 502, 503, and 504. Parse
`Retry-After` as seconds when present; otherwise delay `min(2 ** attempt, 8)` seconds. Inject
`sleep: Callable[[float], None] = time.sleep` into the constructor so tests record delays
without waiting. Raise `SourceError` with:

- 401/403: “Reconnect this Outlook account.”
- 429 after retries: “Microsoft Graph rate limit did not clear.”
- other HTTP/network failures: status and Graph `error.message`, without tokens or headers.

- [ ] **Step 5: Add a 429 retry test**

Return 429 with `Retry-After: 2` on the first request and 200 on the second. Assert:

```python
check("Graph retries 429", request_count == 2)
check("Graph honors Retry-After", delays == [2.0])
```

Return 401 and assert `SourceError` contains “Reconnect”.

- [ ] **Step 6: Export and run tests**

Export `OutlookGraphSource` from `mailcheck/sources/__init__.py`.

Run: `python tests/test_smoke.py`

Expected: paging, mapping, header, retry, and reconnect checks pass.

- [ ] **Step 7: Commit the Graph adapter**

```bash
git add mailcheck/sources/outlook.py mailcheck/sources/__init__.py tests/test_smoke.py
git commit -m "feat: fetch personal Outlook mail through Graph"
```

### Task 4: Route Outlook accounts through the existing pipeline and CLI

**Files:**
- Modify: `mailcheck/pipeline.py`
- Modify: `mailcheck/cli.py`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Consumes: `Account.provider`, `OutlookGraphSource`, `outlook_auth.get_access_token`
- Produces: `build_source(account: db.Account, cfg: Config) -> MailSource`

- [ ] **Step 1: Add a failing source-selection test**

Add:

```python
outlook_account = db.Account(
    id=9, label="outlook", email="me@outlook.com", provider="outlook_graph",
    imap_host="", imap_port=0, use_ssl=True, folder="INBOX", enabled=True,
)
cfg = Config.model_validate({"outlook": {"client_id": "cid"}})
source = pipeline.build_source(outlook_account, cfg)
check("Outlook account builds Graph source",
      isinstance(source, OutlookGraphSource))
```

Monkeypatch `pipeline.outlook_auth.get_access_token` before calling `source.test()`; no live
Microsoft request is allowed in tests.

- [ ] **Step 2: Run the smoke test and verify it fails**

Run: `python tests/test_smoke.py`

Expected: FAIL because `build_source()` takes only an account and always returns IMAP.

- [ ] **Step 3: Make source construction provider-aware**

Change:

```python
def build_source(account: db.Account, cfg: Config) -> MailSource:
    if account.provider == "outlook_graph":
        def outlook_token() -> str:
            try:
                return outlook_auth.get_access_token(
                    account.label, cfg.outlook.client_id
                )
            except outlook_auth.OutlookAuthError as exc:
                raise SourceError(str(exc)) from exc

        return OutlookGraphSource(
            token_provider=outlook_token
        )
    if account.provider != "imap":
        raise SourceError(f"Unsupported account provider: {account.provider}")
    return IMAPSource(...)
```

Pass `cfg` from `fetch_account()` and every direct caller. Wrap `OutlookAuthError` as
`SourceError` so the existing per-account isolation remains intact.

- [ ] **Step 4: Make CLI list, test, and removal provider-aware**

In `account_list()`, change Host to Provider and render `Outlook` for `outlook_graph` and the
IMAP host for `imap`.

In `account_test()`, call `pipeline.build_source(account, cfgmod.load()).test()` instead of
`_test_account()` for every provider.

In `account_remove()`, delete both possible credential types:

```python
if account.provider == "outlook_graph":
    secrets.delete_outlook_token_cache(label)
else:
    secrets.delete_account_password(label)
```

Keep `account add` as the IMAP command for this delivery; direct users to the web console for
Outlook device authorization.

- [ ] **Step 5: Prove account isolation**

Add a pipeline test with one fake Outlook source raising `SourceError("Reconnect...")` and one
working fake IMAP source. Assert the result contains one account error and still includes the
working account's messages.

- [ ] **Step 6: Run the smoke test**

Run: `python tests/test_smoke.py`

Expected: all checks pass.

- [ ] **Step 7: Commit pipeline integration**

```bash
git add mailcheck/pipeline.py mailcheck/cli.py tests/test_smoke.py
git commit -m "feat: route Outlook through the mail pipeline"
```

### Task 5: Add asynchronous Outlook connection APIs

**Files:**
- Modify: `mailcheck/web/app.py`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Produces: `POST /api/outlook/connect/start`
- Produces: `GET /api/outlook/connect/{flow_id}`
- Produces: in-memory, expiring flow state that never exposes `device_code`

- [ ] **Step 1: Define request and server-only flow state**

Add:

```python
class OutlookConnectBody(BaseModel):
    label: str
    email: str
    client_id: str | None = None


_outlook_flows: dict[str, dict] = {}
_outlook_flows_lock = threading.Lock()
```

Each stored record has `label`, `email`, `flow`, `expires_at`, `status`, `message`, and
`error`. The API response may include only `flow_id`, `status`, `verification_uri`,
`user_code`, `expires_in`, `message`, and `error`; never return the MSAL `flow` dictionary.

- [ ] **Step 2: Add failing start/status tests with fake auth**

Patch `outlook_auth.begin_device_flow()` to return:

```python
{
    "user_code": "ABCD-EFGH",
    "verification_uri": "https://microsoft.com/devicelogin",
    "expires_in": 900,
    "device_code": "server-only",
}
```

Patch completion to return `{"access_token": "secret"}`. Assert:

```python
start = client.post("/api/outlook/connect/start", json={
    "label": "outlook", "email": "me@outlook.com", "client_id": "cid"
})
payload = start.json()
check("Outlook flow starts", start.status_code == 200 and payload["user_code"] == "ABCD-EFGH")
check("device code never reaches browser", "device_code" not in payload)
check("access token never reaches browser", "access_token" not in payload)
```

Poll the returned flow ID until terminal and assert an `outlook_graph` account was stored.

- [ ] **Step 3: Implement the start route**

Validate:

- label, email, and effective client ID are non-empty;
- email domain is exactly `outlook.com`, `hotmail.com`, or `live.com`;
- label does not already exist.

If `body.client_id` is supplied, persist it to `cfg.outlook.client_id` because a client ID is
not secret. Start the device flow, create a UUID4 flow ID, store the full flow only in memory,
and launch a daemon thread that calls `complete_device_flow()`.

On success, add:

```python
db.add_account(
    conn,
    label=label,
    email=email,
    provider="outlook_graph",
    imap_host="",
    imap_port=0,
    use_ssl=True,
    folder="INBOX",
)
```

On failure, keep a user-actionable error and delete any partial token cache. Always clear the
MSAL flow/device code from the record when the thread reaches a terminal state.

- [ ] **Step 4: Implement safe status and cleanup**

`GET /api/outlook/connect/{flow_id}` returns 404 for unknown IDs. Remove terminal flows after
five minutes and pending flows at `expires_at`. On expiry, return:

```json
{"status": "expired", "error": "The Microsoft sign-in code expired. Start again."}
```

Do not persist flow state across application restarts.

- [ ] **Step 5: Make account test and removal generic**

In `/api/accounts/{label}/test`, use `pipeline.build_source(account, cfgmod.load()).test()`.
In deletion, remove the provider's keyring entry. Change the success copy from “Connection OK”
to “Outlook connection OK” for Graph and “IMAP connection OK” otherwise.

- [ ] **Step 6: Run the smoke test**

Run: `python tests/test_smoke.py`

Expected: connection start/status, no-secret response, account creation, expiry, duplicate
label, generic test, and removal checks pass.

- [ ] **Step 7: Commit the connection API**

```bash
git add mailcheck/web/app.py tests/test_smoke.py
git commit -m "feat: add Outlook device sign-in API"
```

### Task 6: Build the Accounts connection experience and generic deep links

**Files:**
- Modify: `mailcheck/web/templates/accounts.html`
- Modify: `mailcheck/web/templates/dashboard.html`
- Modify: `mailcheck/web/static/app.css`
- Modify: `mailcheck/web/app.py`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Consumes: Outlook start/status APIs and `provider_url`
- Produces: provider picker, device-code screen, reconnect state, and provider-labeled Open action

- [ ] **Step 1: Replace the unsupported Outlook note with provider choice**

At the first step of the add sheet, render two choices:

```html
<button class="provider-choice" type="button" data-provider="imap">
  <strong>Gmail or other IMAP</strong>
  <span>Connect with an app password</span>
</button>
<button class="provider-choice" type="button" data-provider="outlook">
  <strong>Personal Outlook</strong>
  <span>Outlook.com, Hotmail, or Live — connect with Microsoft</span>
</button>
```

Remove the page-level statement that Outlook is unsupported. Keep a scoped note in the
Outlook panel: “Personal accounts only; Microsoft 365 work and school accounts are not yet
supported.”

- [ ] **Step 2: Add the device-code state**

The Outlook panel collects email and label. If `cfg.outlook.client_id` is empty, show a
collapsible “Microsoft application ID” field with a link to the Microsoft app-registration
instructions. Explain that the client ID is public and the app never asks for a client secret.

After start, show:

```html
<div class="device-flow" hidden>
  <p>Open <a id="outlook-verification" target="_blank" rel="noopener"></a></p>
  <div class="device-code" id="outlook-code" aria-label="Microsoft sign-in code"></div>
  <button class="btn" type="button" id="copy-outlook-code">Copy code</button>
  <p id="outlook-status" role="status" aria-live="polite">Waiting for Microsoft sign-in…</p>
  <button class="btn" type="button" data-close="add-sheet">Cancel</button>
</div>
```

Do not put tokens or the server-side flow object in the DOM, query string, or browser storage.

- [ ] **Step 3: Implement browser polling**

Add page-local functions:

```javascript
async function startOutlook(btn) {
  await withBusy(btn, async () => {
    const result = await api("/api/outlook/connect/start", {
      label: document.getElementById("o-label").value.trim(),
      email: document.getElementById("o-email").value.trim(),
      client_id: document.getElementById("o-client-id")?.value.trim() || null,
    });
    showOutlookCode(result);
    pollOutlook(result.flow_id);
  });
}

async function pollOutlook(flowId) {
  while (true) {
    const response = await fetch(`/api/outlook/connect/${encodeURIComponent(flowId)}`);
    const state = await response.json();
    if (state.status === "connected") {
      toast("Outlook connected.");
      location.reload();
      return;
    }
    if (["failed", "expired"].includes(state.status)) {
      toast(state.error, false);
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 1200));
  }
}
```

Cancel stops browser polling but does not claim to revoke an already-approved Microsoft grant.

- [ ] **Step 4: Generate generic Open links**

In the dashboard route:

```python
if row["provider"] == "outlook_graph":
    item["open_url"] = row["provider_url"]
    item["provider_label"] = "Outlook"
elif row["account_email"].lower().endswith(("@gmail.com", "@googlemail.com")):
    item["open_url"] = gmail_message_id_url
    item["provider_label"] = "Gmail"
else:
    item["open_url"] = None
    item["provider_label"] = "mailbox"
```

The template shows the Open button only when `open_url` exists. Never label a Yahoo, iCloud,
or Fastmail message “Open in Gmail”.

- [ ] **Step 5: Add provider and deep-link rendering tests**

Assert:

```python
accounts_html = body(client.get("/accounts"))
check("Accounts offers personal Outlook", "Personal Outlook" in accounts_html)
check("Accounts does not claim Outlook is unsupported",
      "Outlook.com and Microsoft 365 aren't supported" not in accounts_html)
dashboard_html = body(client.get("/"))
check("Outlook messages use Outlook links", "Open in Outlook" in dashboard_html)
check("provider URL is escaped into href", "outlook.live.com" in dashboard_html)
```

- [ ] **Step 6: Add responsive styles**

Add a two-column provider picker above 640 px and one column below. Make `.device-code`
large, monospaced, selectable, and wrapping-safe. Verify the dialog at 320 and 390 px with
200% zoom; every button and code must remain visible without horizontal scrolling.

- [ ] **Step 7: Run the smoke test and manual connection UI check**

Run: `python tests/test_smoke.py`

Expected: all checks pass.

Run the web app with fake auth responses in a development test fixture and verify provider
choice → code → waiting → connected, expired, and failed states with keyboard only.

- [ ] **Step 8: Commit the Outlook UI**

```bash
git add mailcheck/web/templates/accounts.html mailcheck/web/templates/dashboard.html mailcheck/web/static/app.css mailcheck/web/app.py tests/test_smoke.py
git commit -m "feat: add personal Outlook connection experience"
```

### Task 7: Document, privacy-review, and verify Outlook end to end

**Files:**
- Modify: `README.md`
- Verify: `docs/DESIGN.md`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Consumes: all Outlook tasks
- Produces: an independently releasable personal-Outlook integration

- [ ] **Step 1: Update installation and setup documentation**

Document:

- `msal>=1.37,<2` is installed with the project.
- The Microsoft app registration supports personal Microsoft accounts and public-client device
  flow.
- The public application client ID goes in `outlook.client_id`; no client secret is used.
- The user grants delegated `Mail.Read`, and mail-check performs GET-only Graph requests.
- Token caches live in the OS keyring and are deleted when the account is removed.
- Personal Outlook is supported; Microsoft 365 work/school and shared mailboxes are not.

Include links to the six Microsoft references already recorded in `docs/DESIGN.md`.

- [ ] **Step 2: Run the full automated suite**

Run: `python tests/test_smoke.py`

Expected: exit code 0 and zero failed checks.

- [ ] **Step 3: Run syntax verification**

Run: `python -m compileall -q mailcheck`

Expected: exit code 0.

- [ ] **Step 4: Audit stored and returned secrets**

Run:

```bash
rg -n "access_token|refresh_token|device_code|client_secret" mailcheck
```

Expected:

- `access_token` appears only inside `outlook_auth.py` and authorization header construction.
- `device_code` appears only in server-side auth/flow handling and explicit response-redaction
  tests.
- `refresh_token` and `client_secret` are not persisted, logged, rendered, or returned.

- [ ] **Step 5: Audit Graph mutation safety**

Run:

```bash
rg -n "graph.microsoft.com|Mail.ReadWrite|client\\.(post|put|patch|delete)" mailcheck
```

Expected: Graph mail operations use GET only; no `Mail.ReadWrite` scope exists.

- [ ] **Step 6: Perform one live personal-account acceptance run**

With a dedicated test Outlook.com account and a valid public client ID:

1. Connect from `/accounts` and verify no mailbox password or client secret is requested.
2. Restart mail-check and test the account; silent token acquisition must succeed.
3. Send the account a known unread job email and run Check now.
4. Verify classification, account label, message body, and Open in Outlook.
5. Mark Done and Undo; verify the Outlook message stays unread and unmoved.
6. Revoke the app grant in the Microsoft account, run Check now, and verify that Outlook asks
   to reconnect while another configured account still completes.
7. Remove the Outlook account and verify its token-cache key is absent from the keyring.

- [ ] **Step 7: Commit documentation**

```bash
git add README.md docs/DESIGN.md
git commit -m "docs: describe personal Outlook support"
```
