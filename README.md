# mail-check

Triage unread job-application email. Fetches your unread mail, classifies it with an LLM,
and tells you what actually needs a reply today.

```
Act now  (2)
 Date   Type               Company / Role            What they want              Acct
 Jul 29 Interview invite   Acme - Backend Engineer   Offers a 30-min phone       gmail
                                                     screen; asks for 3 slots.
 Jul 29 Assessment         Hooli - Senior SRE        HackerRank test, ~90 min.   gmail
                           due 2026-08-05

For information  (2)
 Jul 28 Application recei… Globex - Data Engineer    Application received.       gmail
 Jul 27 Rejection          Initech - Platform Eng.   Declined after review.      gmail

3 noise email(s) hidden - use --all to show them.

12 unread | 4 classified | 5 cached | 3 prefiltered  across 1 account(s) in 6.2s
```

**Your mail is never modified.** Mailboxes are opened read-only and nothing is ever
marked as read — your unread state is your own triage signal.

## Install

```bash
pip install -e .
```

## Setup

Everything can be done from the web console — connecting a model, adding mailboxes,
running checks, and reading results:

```bash
python -m mailcheck web
```

It opens `127.0.0.1:8765` and walks you through setup if nothing is configured yet.
Prefer the terminal? The same steps are below.

**1. Point it at your model.** Any OpenAI-compatible endpoint (OmniRoute, OpenRouter, …):

```bash
mail-check init
# OmniRoute base_url: https://your-endpoint/v1
# Model name:         some/free-model
# Auth token:         ****
```

The token goes into Windows Credential Manager, never into a config file. `init` sends a
test request so you find out immediately if the endpoint is wrong.

**2. Add a mailbox:**

```bash
mail-check account add
```

It detects the provider from your address and tells you how to get an app password.
Gmail needs one from [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
(2-Step Verification must be on) — your normal password will not work.

**3. Run it:**

```bash
mail-check check
```

## Usage

```bash
mail-check check                  # triage unread mail, print the report
mail-check check --since 7d       # only the last week
mail-check check --all            # include job-board noise
mail-check check --no-cache       # re-classify everything
mail-check check --json           # machine-readable output

mail-check watch                  # re-check every 10 min, notify on urgent mail
mail-check watch -i 30

mail-check web                    # management console on 127.0.0.1:8765

mail-check account list | test <label> | remove <label> | enable <label> --off
mail-check config show | set llm.batch_size 4 | token | test
```

## The console

Three pages, all of it local:

- **Queue** (default) — only what needs you: unhandled interview invites, assessments,
  offers, info requests and recruiter outreach. Each card leads with its category and
  deadline, then company and role, then the mailbox it arrived in, then a one-line
  summary, then two actions: **Open in Gmail/Outlook** and **Done**. Everything else is
  behind Details.
- **All mail** — the same cards, every category, grouped by urgency.
- **Completed** — what you've marked Done, with Restore.
- **Accounts** — connect an IMAP mailbox with an app password, or sign in to a personal
  Outlook account with Microsoft. Test, pause, or remove any of them.
- **Settings** — endpoint, model and token; automatic checks; batch size, body length,
  lookback and interval; and local sender rules that label mail before it ever reaches
  the model.

Turn on **Check automatically** in Settings and the console runs the check itself on your
interval. A live countdown — *Next check in 7:24* — sits in the toolbar on every page;
click it to switch automatic checks back off. It is off by default, because opening a page
should never start spending tokens on its own. The schedule belongs to the running console,
so closing it stops the checks; use `mail-check watch` for unattended background runs.

It binds `127.0.0.1` only and has no login, since it is a single local user. Because it
accepts credential writes, non-GET requests carrying a foreign `Origin` are rejected, so a
web page you happen to have open cannot drive the API.

Colours follow the light or dark setting of your OS. Status hues are contrast-validated for
text in both modes, and no tier is identified by colour alone — every one is also named.

For a scheduled run, point Task Scheduler or cron at `mail-check check` — that is all
`watch` does under the hood.

## Categories

| Tier | Category | Meaning |
|---|---|---|
| 🔴 Act now | `interview_invite` | Interview or phone screen offered |
| 🔴 | `assessment` | Coding test / take-home — **usually has a deadline** |
| 🔴 | `offer` | Offer or terms |
| 🟡 Reply | `info_request` | Wants references, salary expectations, availability |
| 🟡 | `recruiter_outreach` | Cold inbound, not from an application you sent |
| ⚪ Info | `rejection` | Declined |
| ⚪ | `application_ack` | Automated "we received your application" |
| ⚫ Noise | `job_alert` | LinkedIn/Indeed digests |
| ⚫ | `other` | Not job-related |
| ❓ | `unclassified` | The model failed — surfaced for a manual look, never dropped |

Each email also gets **company, role, deadline, action_required and a one-line summary**
extracted, which is what turns the report into a to-do list.

## Cost and reliability

Classification is cached in SQLite keyed by `(message_id, model, prompt_version)`, so
re-running costs nothing. Changing the model or bumping `PROMPT_VERSION` transparently
forces a re-classify.

Emails go to the model **8 per request** with bodies stripped of HTML, quoted replies and
footer boilerplate, then truncated to 1200 characters. Known job-board senders are labelled
locally and never sent at all.

Free models are unreliable at JSON, so the parser expects that: it strips code fences,
pulls JSON out of surrounding prose, validates each item independently, retries missing
items one at a time with a stricter prompt, and falls back to `unclassified` rather than
crashing. One malformed entry cannot cost you the other seven.

If you hit rate limits, lower the batch size:

```bash
mail-check config set llm.batch_size 4
```

## Privacy

**Email bodies are sent to whatever endpoint you configure.** Job-application mail contains
real names, phone numbers and salary discussion, and free tiers on model routers commonly
log — and sometimes train on — request data. Decide if that is acceptable before pointing
this at a mailbox.

You can exclude a sensitive mailbox without deleting it:

```bash
mail-check account enable work --off
```

## Notifications

Turn on **Notify me about urgent mail** in Settings and the browser announces interview
invites, assessments and offers as they arrive — the three things worth interrupting you for.
Each message is announced once, and clicking the notification opens it in Gmail or Outlook.

Permission is granted per browser, so the switch is per browser too. Alerts fire after any
check, including automatic ones, as long as a console tab is open. Use the **Test** button to
confirm popups actually reach you: a granted permission does not by itself guarantee a
visible notification, since Windows can suppress your browser's notifications independently
(Settings → System → Notifications, and Do not disturb).

## Done is local

**Done** marks an item finished in mail-check's own database. It does not mark the message
read, move it, label it, or change anything in Gmail or Outlook — provider mail is only ever
read. Re-running a check never resurrects something you finished, and Completed can restore
it whenever you want.

## Outlook

Personal **Outlook.com, Hotmail and Live** accounts sign in with Microsoft instead of a
mailbox password — Microsoft disabled basic-auth IMAP, so app passwords cannot work there.

One-time setup: register a **public client** application in the Microsoft Entra admin
center with delegated `Mail.Read` permission and device-code flow enabled, then paste its
Application (client) ID into Settings → Outlook. That ID is a public identifier, not a
secret; no client secret is used or stored. Accounts → *Outlook.com, Hotmail, Live* then
shows a code to enter at Microsoft's sign-in page. Tokens live in the OS keyring and are
deleted with the account.

Microsoft 365 work and school accounts are out of scope.

## Known limitations

- Each email is classified in isolation; replies within a thread are not grouped.
- The Outlook sign-in flow is covered by tests against a stand-in Graph server, but has not
  been run against a real Microsoft tenant.

## Development

```bash
python tests/test_smoke.py
```

Runs the whole pipeline against a fake mailbox and a fake model — including the malformed-JSON
paths — with no network and no credentials. See [docs/DESIGN.md](docs/DESIGN.md) for the
architecture and the reasoning behind each decision.
