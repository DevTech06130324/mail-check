# mail-check — Design

Triage unread job-application email: fetch, classify with an LLM, report what needs action.

## 1. Problem

Job hunting generates high-volume, low-signal inbox traffic. Application acknowledgements,
job-board digests, and rejections bury the two things that actually matter: **interview
invites** and **timed assessments**. Manually opening every unread mail to find them is the
cost this tool removes.

Success criterion: after a run, the user knows — without opening the inbox — what needs a
reply today, and can ignore the rest.

## 2. Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | Best MIME/IMAP ecosystem; fastest path to working |
| Interface | CLI (primary) + local web UI | CLI is cron-friendly; web UI is better for *reading* results |
| Mail access | IMAP + app password; Microsoft Graph OAuth for personal Outlook | Provider-specific auth behind one source protocol |
| Trigger | `check` (one-shot) + `watch` (poll loop) | Same pipeline, two entry points; cron just calls `check` |
| LLM output | Label **+ extracted fields** | Turns the report into a to-do list, not a folder listing |
| Storage | SQLite + OS keyring | Local-only; credentials never touch the DB |
| Provider mail mutation | **None** — every source is read-only | Unread state is the user's own triage signal |

### Non-goals (v1)

- Sending or replying to mail
- Marking read / moving / labelling on the mail provider. The local **Done**
  state described below never changes Gmail or Outlook.
- Native mobile app, hosted, or multi-user operation
- Real-time IMAP IDLE (see §9)

## 3. Architecture

The pipeline is built once; the three trigger modes are thin wrappers over it.

```
                    ┌──────────── triggers ────────────┐
                    │  check     watch     web "Refresh" │
                    └────────────────┬──────────────────┘
                                     ▼
  accounts ──▶ fetch ──▶ normalize ──▶ prefilter ──▶ cache? ──▶ LLM ──▶ store ──▶ report
   (SQLite) (IMAP/Graph RO) (→text)      (rules)      (SQLite)  (batched)  (SQLite)  (rich/web)
```

### Module layout

```
mailcheck/
  cli.py              # typer entry points
  config.py           # TOML config + pydantic validation
  db.py               # schema, migrations, queries
  secrets.py          # keyring wrapper (Windows Credential Manager)
  sources/
    base.py           # MailSource protocol — the pluggable seam
    imap.py           # IMAPSource (v1)
    outlook.py        # OutlookGraphSource for personal Microsoft accounts
  outlook_auth.py     # MSAL public-client auth + keyring-backed token cache
  normalize.py        # MIME → clean plain text
  prefilter.py        # rule-based noise labelling, pre-LLM
  llm/
    client.py         # OmniRoute (OpenAI-compatible) + retry/backoff
    prompt.py         # taxonomy prompt, versioned
    schema.py         # pydantic Classification model
    classify.py       # batching, JSON repair, fallback
  pipeline.py         # check_once() — the whole loop
  watch.py            # poll loop, prints the report each cycle
  report.py           # rich terminal rendering
  web/app.py          # FastAPI, binds 127.0.0.1 only
```

The `MailSource` protocol is the most important boundary in the design:

```python
class MailSource(Protocol):
    def test(self) -> None: ...
    def fetch_unread(self, since: date) -> Iterator[RawMessage]: ...
```

Everything downstream consumes `RawMessage`. IMAP and Microsoft Graph are provider adapters;
adding another provider is a new file in `sources/`, not a pipeline refactor.

## 4. Taxonomy

Categories are ordered by urgency tier, which is how the report groups them.

| Tier | Category | Definition |
|---|---|---|
| 🔴 Act now | `interview_invite` | Invitation to interview / phone screen / scheduling request |
| 🔴 | `assessment` | Coding test, take-home, OA — **usually deadline-bound** |
| 🔴 | `offer` | Offer extended, or offer terms/negotiation |
| 🟡 Reply | `info_request` | Wants references, salary expectations, availability, documents |
| 🟡 | `recruiter_outreach` | Inbound cold contact, not tied to an application you sent |
| ⚪ Info | `rejection` | Application declined / position closed / moving on with others |
| ⚪ | `application_ack` | Automated "we received your application" |
| ⚫ Noise | `job_alert` | LinkedIn/Indeed/ZipRecruiter digests, saved-search alerts |
| ⚫ | `other` | Not job-related |
| ❓ | `unclassified` | LLM failed after retries — surfaced for manual review, never silently dropped |

`assessment` is deliberately split from `interview_invite`: it carries a hard deadline and is
the single most expensive thing to miss.

### Extracted fields

```json
{
  "id": "3",
  "category": "interview_invite",
  "confidence": 0.92,
  "company": "Acme Inc",
  "role": "Backend Engineer",
  "deadline": "2026-08-05",
  "action_required": true,
  "summary": "Offers a 30-min phone screen; asks for three time slots next week."
}
```

`company` / `role` / `deadline` are nullable. `deadline` is ISO-8601 date or null — the model
is told to emit null rather than guess.

## 5. Data model

```sql
accounts(id, label UNIQUE, email, provider DEFAULT 'imap',
         imap_host, imap_port, use_ssl, folder DEFAULT 'INBOX',
         enabled, created_at)

messages(id, account_id, message_id, uid, folder,
         from_addr, from_name, subject, date_utc, body_text, snippet,
         provider_url, handled_at, fetched_at,
         UNIQUE(account_id, message_id))

classifications(message_pk, category, confidence, company, role, deadline,
                action_required, summary,
                model, prompt_version, source,   -- 'llm' | 'prefilter'
                created_at,
                UNIQUE(message_pk, model, prompt_version))

runs(id, started_at, finished_at, accounts_checked,
     fetched, classified, from_cache, errors)
```

**Cache key is `(message_id, model, prompt_version)`.** Re-running costs nothing; changing the
model or bumping the prompt version transparently forces re-classification.

IMAP passwords, the LLM token, and serialized Outlook MSAL token caches live in the OS
keyring under distinct `mail-check` entries. The SQLite and TOML files contain no secrets;
the Microsoft application client ID is a public identifier, not a credential.

## 6. Normalization

Body text is the LLM's input, so this stage directly controls cost and accuracy.

1. Prefer `text/plain`; fall back to `html2text` over `text/html`.
2. Cut quoted history — `On … wrote:`, `-----Original Message-----`, leading `>` blocks.
3. Cut boilerplate footers — unsubscribe blocks, legal disclaimers.
4. Collapse whitespace; truncate long URL query strings (tracking params are pure token burn).
5. Truncate to `max_body_chars` (default 1200), **keeping the head** — job mail puts its
   intent in the first paragraph.

## 7. Prefilter

Rule-based labelling *before* the LLM, for traffic that is unambiguous:

- Known digest senders (`jobalerts-noreply@linkedin.com`, `alert@indeed.com`,
  `noreply@ziprecruiter.com`, …) → `job_alert`
- User-defined sender/subject rules from config

Deliberately conservative. A false negative just costs a few tokens; a false positive hides an
interview invite. Rules only fire on exact sender matches, never on heuristics like the
presence of a `List-Unsubscribe` header — ATS platforms set that too.

## 8. LLM integration (OmniRoute)

OpenAI-compatible `POST {base_url}/chat/completions`. User supplies `base_url`, `auth_token`,
`model`.

**Batching.** ~8 emails per request as a JSON array in, JSON array out. Cuts request count ~8×,
which matters most against free-tier rate limits.

**Assume the free model is bad at JSON.** This is the most likely failure mode in production,
so it gets an explicit ladder:

1. Request `response_format: {"type": "json_object"}`; wrap results as `{"results": [...]}`
   because many endpoints reject a bare array root.
2. Strip markdown fences; brace-match to extract the first balanced JSON object if the model
   wraps it in prose.
3. Validate each item with pydantic. Items that fail — or ids missing from the response — are
   retried **individually** with a stricter prompt.
4. Still failing after 2 attempts → `unclassified`, stored and shown in the report.

Unknown category strings are fuzzy-matched to the nearest known label, else `other`. A batch
never fails as a unit; one bad item can't take down seven good ones.

**Rate limits.** 429 → exponential backoff with jitter, honouring `Retry-After`. Concurrency
defaults to 1 (free tiers are strict), configurable.

## 9. Trigger modes

- **`check`** — run once, print report, exit. Cron / Task Scheduler calls this.
- **`watch`** — the same pipeline on an interval (default 10 min), printing the report to the
  terminal each cycle. No desktop notification: the web console's browser-notification channel
  (§15) is the supported way to be alerted about urgent mail; `watch` is for headless/unattended
  runs where the report itself is the record.
- **Web "Refresh"** — POSTs to the same `check_once()`.

**Why not IMAP IDLE.** It needs a persistent connection per account with reconnect/keepalive
handling, and for job hunting a 10-minute delay on an interview invite changes nothing. The
maintenance cost is real; the benefit is not. Deferred, not refused — if it's ever wanted, it
slots in as a new trigger over the unchanged pipeline.

## 10. Interfaces

### CLI

```
mail-check init                     # wizard: OmniRoute base_url / token / model
mail-check account add              # wizard: label, email, provider preset, app password
mail-check account list | remove | test
mail-check check [--account L] [--since 7d] [--no-cache] [--json]
mail-check watch [--interval 10]
mail-check web [--port 8765]
```

Provider presets for `account add`: gmail, yahoo, icloud, fastmail, custom.

Terminal report groups by urgency tier, newest first, with a footer of run stats
(fetched / classified / cached / errors).

### Web UI

FastAPI + Jinja2, **bound to 127.0.0.1 only**, no auth (single local user). Reads the same
SQLite DB. The default view is an action queue containing only unhandled **Act now** and
**Needs a reply** messages. Each card exposes its primary actions directly: open the
provider's original message or mark the item Done locally. Informational mail, noise, and
completed items are secondary views. Filters remain available without competing with the
primary daily-triage path.

## 11. Privacy

**Email bodies are sent to a third-party LLM router.** Job-application mail contains real
names, phone numbers, and salary discussion. Free tiers on aggregators commonly log — and
sometimes train on — request data.

The tool must state this at `init` time, not bury it. Mitigations available: `max_body_chars`
limits exposure, the prefilter keeps obvious noise local, and per-account `enabled` lets the
user exclude a sensitive mailbox entirely. A `--redact` pass for phone numbers and postal
addresses is a candidate for later.

## 12. Known constraints

- Personal Outlook.com / Hotmail / Live accounts use Microsoft Graph with delegated
  `Mail.Read` permission. They do not use IMAP passwords. Microsoft 365 work and school
  accounts remain outside the approved scope.
- **Gmail app passwords require 2FA** enabled on the account.
- Large inboxes need `--since` bounding; default lookback is 30 days.

## 13. Milestones

| # | Deliverable | Proves |
|---|---|---|
| M1 | config, `account add`, IMAP fetch, normalize, raw dump | Mail comes in clean |
| M2 | LLM classify + cache + terminal report | **Core value — usable here** |
| M3 | `watch` poll loop | Unattended operation |
| M4 | Web UI | Comfortable reading |
| M5 | Prefilter rules, presets, CSV export | Polish |
| M6 | 30-second action queue + local Done/Undo | Daily triage has a finish line |
| M7 | Personal Outlook via Microsoft Graph | Gmail and Outlook share one read-only pipeline |

M2 is the first genuinely useful build; M1 is a checkpoint, not a release.

## 14. Open questions

1. **OmniRoute API shape** — assumed OpenAI-compatible `/chat/completions`. Needs confirming
   against the actual docs before `llm/client.py`, particularly whether `response_format` is
   honoured on free models.
2. **Model choice** — the taxonomy prompt needs testing against whichever free model is used;
   weaker models may need the batch size dropped or the taxonomy simplified.
3. **Threading** — a rejection replying to your own application thread is currently classified
   in isolation. Grouping by `In-Reply-To`/`References` would improve accuracy but adds scope.

## 15. Approved product evolution (2026-07-30)

### 15.1 Product goal

The web console's north star is: **finish daily job-mail triage in under 30 seconds**.
The interface should answer three questions in order:

1. What needs my attention?
2. What should I do next?
3. When is the queue finished?

The number of informational messages is useful context, but it must not visually compete
with actionable mail.

### 15.2 Daily action queue

The default Triage view contains only unhandled `act` and `reply` items. Its header shows one
combined actionable count and the last-check state. Large per-tier statistic tiles are
replaced by a compact summary so an information-heavy inbox cannot dominate the first screen.

Each mail card presents information in this order:

1. urgency/category and deadline;
2. company and role;
3. one-line summary;
4. primary actions: **Open in Gmail/Outlook** and **Done**;
5. subject, sender, account, confidence, source, and cleaned body as secondary detail.

`For information` and `Noise` move to the All mail view. `Completed` is a separate view for
locally handled messages. Filters are progressive disclosure, not the main navigation.

### 15.3 Local Done and Undo

Done is stored as `messages.handled_at` in SQLite. It removes the item from the default queue
without changing unread state, labels, folders, or any other provider data. The success toast
offers Undo; Completed provides a persistent restore action after the toast disappears.
Reclassification and subsequent checks preserve `handled_at`.

The queue is complete when it has no unhandled `act` or `reply` items. The empty state should
say so explicitly and offer All mail as a secondary path.

### 15.4 Responsive and accessible behavior

The UI must work without horizontal scrolling at 320, 390, 768, and 1440 CSS pixels. Cards,
navigation, tiles, form rows, and long company/role text must be allowed to shrink and wrap.
Primary actions remain visible at every width.

Navigation uses `aria-current`; switches have programmatic names; status updates and toasts
use polite or assertive live regions as appropriate; dialogs trap and restore focus; all
interactive controls have visible keyboard focus. Motion respects `prefers-reduced-motion`.
Future timestamps render as `Tomorrow`, `In N days`, or a time instead of negative relative
dates.

### 15.5 Personal Outlook support

Personal Outlook support is a second, independently testable delivery after the action-queue
redesign. It uses Microsoft Graph, not basic-auth IMAP:

- Account types: Outlook.com, Hotmail, and Live personal Microsoft accounts only.
- Authentication: MSAL Python public client against the `/consumers` authority using device
  authorization. No client secret is shipped or stored.
- Deployment prerequisite: a Microsoft app registration supplies the public application
  client ID in `outlook.client_id`. The ID is not a secret; when it is absent, Accounts
  explains how to configure it instead of starting a broken sign-in.
- Permission: delegated `Mail.Read`, the minimum permission that includes message bodies.
- Tokens: MSAL's serialized cache is stored per account in the OS keyring. SQLite stores no
  access token, refresh token, device code, or client secret.
- Fetch: `GET /me/mailFolders/inbox/messages`, filtered to unread messages within the
  configured lookback, with an explicit `$select`, `Prefer:
  outlook.body-content-type="text"`, and complete `@odata.nextLink` traversal.
- Mapping: Graph messages become the existing provider-neutral `RawMessage`;
  `internetMessageId` is the cache identity and Graph `id` is the provider UID fallback.
- Deep link: Graph `webLink` is stored as `messages.provider_url` and drives **Open in
  Outlook**. Gmail continues to use its Message-ID search link.
- Mutation: Graph calls are GET-only. Done/Undo remains local.

The Accounts screen begins with provider choices. IMAP providers keep the current email,
app-password, and advanced-server flow. Outlook shows **Connect Outlook**, a device-code
instruction state, success, expiry, cancellation, and reconnect states. Account removal also
deletes its token cache from the keyring.

### 15.6 Boundaries and failure behavior

`MailSource` remains the only pipeline-facing provider interface. `pipeline.build_source()`
selects `IMAPSource` or `OutlookGraphSource` from `Account.provider`; normalization,
prefiltering, classification, caching, reporting, scheduling, and local Done state remain
provider-neutral.

An expired access token is refreshed silently from the MSAL cache. Missing consent,
`invalid_grant`, or a removed Microsoft account surfaces a reconnect-needed error for that
account and does not abort checks for other accounts. Graph 429 and transient 5xx responses
honor `Retry-After` and retry with bounded exponential backoff. Authentication flows expire
and are removed from server memory; they are never written to disk.

### 15.7 Explicit non-goals

- Sending, replying, archiving, deleting, marking read, or moving provider mail
- Microsoft 365 work/school or shared mailboxes
- Outlook calendar or contacts
- Hosted OAuth callback infrastructure
- Thread grouping, snoozing, assignments, or a general job-application CRM
- Graph webhooks or delta sync in the first Outlook delivery

### 15.8 Acceptance criteria

- At 390 px, the default action queue, settings, account connection flow, and every primary
  action fit without horizontal scrolling or clipping.
- A user can mark an actionable message Done, undo immediately, restore it later from
  Completed, and observe no provider-side mailbox mutation.
- The default queue excludes informational, noise, and completed messages and reaches a clear
  all-done state.
- A personal Outlook account can be connected without entering a mailbox password, survives
  application restart through the keyring-backed cache, and contributes unread messages to
  the same classification pipeline as Gmail.
- One failing or disconnected Outlook account does not prevent other accounts from checking.
- Stored Outlook messages open through their Graph `webLink`; Gmail messages retain their
  Gmail search link.

### 15.9 Microsoft references

- [Device authorization grant](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code)
- [Acquire tokens with MSAL Python](https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens)
- [Microsoft Graph `Mail.Read` permission](https://learn.microsoft.com/en-us/graph/permissions-reference#mailread)
- [List messages](https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0)
- [Message resource and `webLink`](https://learn.microsoft.com/en-us/graph/api/resources/message?view=graph-rest-1.0)
- [Microsoft Graph paging](https://learn.microsoft.com/en-us/graph/sdks/paging)

### 15.10 Implementation plans

- [Daily triage UX](superpowers/plans/2026-07-30-daily-triage-ux.md)
- [Personal Outlook mail](superpowers/plans/2026-07-30-personal-outlook.md)
