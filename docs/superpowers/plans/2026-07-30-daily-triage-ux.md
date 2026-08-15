# Daily Triage UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the web console into a responsive action queue that a user can finish in under 30 seconds, with provider-read-only local Done and Undo.

**Architecture:** Add `messages.handled_at` as provider-neutral local state, expose focused database queries and two small mutation endpoints, then reshape the existing Jinja dashboard around Action, All mail, and Completed views. Keep the current FastAPI/Jinja/vanilla-JavaScript stack and preserve the CLI, classification pipeline, cache, and provider read-only guarantees.

**Tech Stack:** Python 3.11+, SQLite, FastAPI, Jinja2, vanilla JavaScript, CSS, existing smoke-test harness

## Global Constraints

- Never mutate read state, folders, labels, or content in Gmail, Outlook, or any other provider.
- The default view contains only unhandled `act` and `reply` tiers.
- Done persists locally in SQLite; immediate Undo and persistent restore from Completed are required.
- Re-fetching or reclassifying a message must preserve `handled_at`.
- The UI must not horizontally scroll at 320, 390, 768, or 1440 CSS pixels.
- Informational mail and noise must not visually compete with the actionable queue.
- Keep the current FastAPI + Jinja2 + vanilla JavaScript architecture; add no frontend framework.
- Preserve light mode, dark mode, reduced motion, keyboard operation, and text labels for every status color.

---

## File Map

- Modify `mailcheck/db.py`: schema migration, handled-state writes, and handled filters.
- Modify `mailcheck/web/app.py`: focused dashboard view model, Done/Undo endpoints, and correct future-date formatting.
- Modify `mailcheck/web/templates/base.html`: accessible navigation and live-region containers.
- Modify `mailcheck/web/templates/dashboard.html`: Action/All/Completed information architecture and primary card actions.
- Modify `mailcheck/web/templates/settings.html`: normal-versus-advanced settings hierarchy and consistent save behavior.
- Modify `mailcheck/web/static/app.js`: Done/Undo behavior, actionable toasts, dialog focus, and live announcements.
- Modify `mailcheck/web/static/app.css`: mobile containment, action-card hierarchy, view tabs, focus, and wrapping.
- Modify `tests/test_smoke.py`: migration, query, route, rendering, and date regression coverage.
- Modify `README.md`: describe the action queue and local-only Done semantics.

### Task 1: Persist local handled state

**Files:**
- Modify: `mailcheck/db.py`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Produces: `set_message_handled(conn: sqlite3.Connection, message_pk: int, handled: bool) -> bool`
- Produces: `query_triaged(..., handled: bool | None = None) -> list[sqlite3.Row]`
- Produces: `category_counts(..., handled: bool | None = None) -> dict[str, int]`
- Produces: `messages.handled_at: TEXT | NULL`

- [ ] **Step 1: Add a failing legacy-database migration test**

Add this test beside the existing database tests:

```python
def test_handled_state():
    print("\nlocal handled state")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "legacy.db"
        legacy = sqlite3.connect(path)
        legacy.executescript("""
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY, label TEXT UNIQUE, email TEXT,
                imap_host TEXT, imap_port INTEGER DEFAULT 993,
                use_ssl INTEGER DEFAULT 1, folder TEXT DEFAULT 'INBOX',
                enabled INTEGER DEFAULT 1, created_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, account_id INTEGER, message_id TEXT,
                uid TEXT, folder TEXT, from_addr TEXT, from_name TEXT,
                subject TEXT, date_utc TEXT, body_text TEXT, snippet TEXT,
                fetched_at TEXT, notified INTEGER DEFAULT 0,
                UNIQUE(account_id, message_id)
            );
            PRAGMA user_version = 1;
        """)
        legacy.close()

        conn = db.connect(path)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        check("v1 database gains handled_at", "handled_at" in columns)
        check("schema advances to v2",
              conn.execute("PRAGMA user_version").fetchone()[0] == 2)
        conn.close()
```

Call `test_handled_state()` from `main()` immediately before `test_pipeline_and_cache(msgs)`.

- [ ] **Step 2: Run the migration test and verify it fails**

Run: `python tests/test_smoke.py`

Expected: FAIL at `v1 database gains handled_at`.

- [ ] **Step 3: Add the v2 migration without resetting existing state**

In `mailcheck/db.py`, change the schema version and add the column to new databases:

```python
SCHEMA_VERSION = 2
```

```sql
handled_at   TEXT,
```

Place `handled_at` in the `messages` table after `notified`. Replace the unconditional
`PRAGMA user_version` assignment in `connect()` with:

```python
def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 2 and "handled_at" not in _column_names(conn, "messages"):
        conn.execute("ALTER TABLE messages ADD COLUMN handled_at TEXT")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
```

Call `_migrate(conn)` after `conn.executescript(SCHEMA)` and before `conn.commit()`.
`upsert_message()` must not mention `handled_at`, so an existing Done value survives conflict
updates and reclassification.

- [ ] **Step 4: Add failing handled-write and query-filter assertions**

Extend `test_handled_state()` by creating an account and classified message, then add:

```python
msg = NormalizedMessage(
    account_id=account_id, account_label="gmail", message_id="<done@test>",
    uid="1", folder="INBOX", from_addr="x@example.com", from_name="X",
    subject="Interview", date_utc=NOW, body="Choose a time."
)
pk = db.upsert_message(conn, msg)
db.save_classification(
    conn, pk,
    Classification(category="interview_invite", action_required=True),
    "m", "1",
)
check("message starts actionable",
      len(db.query_triaged(conn, handled=False)) == 1)
check("mark Done updates one row", db.set_message_handled(conn, pk, True))
check("Done leaves the active query", db.query_triaged(conn, handled=False) == [])
check("Done appears in completed query",
      len(db.query_triaged(conn, handled=True)) == 1)
check("missing message is refused", not db.set_message_handled(conn, 9999, True))
check("Undo restores the message", db.set_message_handled(conn, pk, False)
      and len(db.query_triaged(conn, handled=False)) == 1)
```

- [ ] **Step 5: Implement handled writes and filters**

Add:

```python
def set_message_handled(
    conn: sqlite3.Connection, message_pk: int, handled: bool
) -> bool:
    cur = conn.execute(
        "UPDATE messages SET handled_at = ? WHERE id = ?",
        (_now() if handled else None, message_pk),
    )
    conn.commit()
    return cur.rowcount > 0
```

Add `handled: bool | None = None` to `category_counts()` and `query_triaged()`. In each query,
append:

```python
if handled is True:
    sql += " AND m.handled_at IS NOT NULL"
elif handled is False:
    sql += " AND m.handled_at IS NULL"
```

Include `m.handled_at` in `query_triaged()`'s SELECT list.

- [ ] **Step 6: Run the full smoke test**

Run: `python tests/test_smoke.py`

Expected: all checks pass, including migration, Done, Completed, and Undo.

- [ ] **Step 7: Commit the persistence slice**

```bash
git add mailcheck/db.py tests/test_smoke.py
git commit -m "feat: persist local triage completion"
```

### Task 2: Add focused dashboard views and mutation endpoints

**Files:**
- Modify: `mailcheck/web/app.py`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Consumes: `db.set_message_handled(...)`, `db.query_triaged(..., handled=...)`
- Produces: `GET /?view=action|all|completed`
- Produces: `POST /api/messages/{message_pk}/done`
- Produces: `POST /api/messages/{message_pk}/undo`

- [ ] **Step 1: Add failing route and default-view checks**

In `test_web_console()`, after the dashboard is fetched, add:

```python
main_html = body(client.get("/"))
check("default dashboard is action view", 'data-view="action"' in main_html)
check("default view excludes information tier", 'class="tier info"' not in main_html)
check("all view includes information tier",
      'class="tier info"' in body(client.get("/?view=all")))

with db.session(tmp / "t.db") as c2:
    target = db.query_triaged(c2, handled=False)[0]["pk"]
done = client.post(f"/api/messages/{target}/done")
check("Done endpoint succeeds", done.status_code == 200 and done.json()["handled"] is True)
check("completed view contains Done item",
      f'data-message-pk="{target}"' in body(client.get("/?view=completed")))
undo = client.post(f"/api/messages/{target}/undo")
check("Undo endpoint succeeds", undo.status_code == 200 and undo.json()["handled"] is False)
check("unknown Done target is 404",
      client.post("/api/messages/999999/done").status_code == 404)
```

- [ ] **Step 2: Run the web test and verify it fails**

Run: `python tests/test_smoke.py`

Expected: FAIL because the view marker and mutation routes do not exist.

- [ ] **Step 3: Build an explicit view model in the dashboard route**

Add constants near the route declarations:

```python
_DASHBOARD_VIEWS = {"action", "all", "completed"}
_ACTION_TIERS = {"act", "reply"}
```

Add `view: str = "action"` to `dashboard()`, normalize unknown values to `"action"`, and query:

```python
view = view if view in _DASHBOARD_VIEWS else "action"
handled = view == "completed"
rows = db.query_triaged(
    conn,
    account_label=account,
    since_iso=since,
    handled=handled,
)
if view == "action":
    rows = [row for row in rows if tier_of(row["category"]) in _ACTION_TIERS]
```

Keep category filtering for `view=all` and `view=completed`; ignore a category parameter in
the action view unless it belongs to an action tier. Pass these template values:

```python
"view": view,
"action_count": sum(
    1 for row in rows if view == "action"
),
"all_unhandled_count": sum(db.category_counts(
    conn, since_iso=since, account_label=account, handled=False
).values()),
"completed_count": sum(db.category_counts(
    conn, since_iso=since, account_label=account, handled=True
).values()),
```

For `view=all`, retain the current `show_noise` behavior. For `view=completed`, show every
handled tier and do not hide noise.

- [ ] **Step 4: Implement Done and Undo endpoints**

Add:

```python
def _set_handled(message_pk: int, handled: bool):
    with db.session() as conn:
        if not db.set_message_handled(conn, message_pk, handled):
            return _err("No such message.", 404)
    return {
        "ok": True,
        "message_pk": message_pk,
        "handled": handled,
        "message": "Marked Done." if handled else "Restored to the action queue.",
    }


@app.post("/api/messages/{message_pk}/done")
def api_message_done(message_pk: int):
    return _set_handled(message_pk, True)


@app.post("/api/messages/{message_pk}/undo")
def api_message_undo(message_pk: int):
    return _set_handled(message_pk, False)
```

- [ ] **Step 5: Add and fix the future-date regression**

Add to `test_web_console()`:

```python
from mailcheck.web.app import _fmt_date
future = (datetime.now() + timedelta(days=1)).isoformat()
check("future date is never negative", "-1 days ago" not in _fmt_date(future))
```

Update `_fmt_date()` so negative deltas return a future label:

```python
delta = datetime.now() - dt.replace(tzinfo=None)
if delta.total_seconds() < 0:
    ahead = dt.replace(tzinfo=None) - datetime.now()
    if ahead.days == 0:
        return "Tomorrow"
    return f"In {ahead.days + 1} days"
```

- [ ] **Step 6: Run the full smoke test**

Run: `python tests/test_smoke.py`

Expected: all checks pass and no dashboard path returns a 500.

- [ ] **Step 7: Commit the route slice**

```bash
git add mailcheck/web/app.py tests/test_smoke.py
git commit -m "feat: add action and completed triage views"
```

### Task 3: Reshape the dashboard around primary actions

**Files:**
- Modify: `mailcheck/web/templates/dashboard.html`
- Modify: `mailcheck/web/templates/base.html`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Consumes: `view`, `action_count`, `all_unhandled_count`, `completed_count`, `i.pk`
- Produces: `.view-tabs`, `[data-done]`, `[data-undo]`, `[data-message-pk]`

- [ ] **Step 1: Add failing semantic rendering checks**

Add to `test_web_console()`:

```python
page = body(client.get("/"))
check("triage views are navigable",
      all(marker in page for marker in ('href="/?view=action"', 'view=all', 'view=completed')))
check("cards expose Done", "data-done" in page)
check("cards retain provider-open action", "Open in Gmail" in page)
completed = body(client.get("/?view=completed"))
check("completed cards expose Restore", "data-undo" in completed or "All caught up" in completed)
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `python tests/test_smoke.py`

Expected: FAIL because the new navigation and action attributes are absent.

- [ ] **Step 3: Replace statistic tiles with a compact queue header**

At the top of `dashboard.html`, render:

```html
<div class="page-head queue-head" data-view="{{ view }}">
  <div>
    <h1>
      {% if view == "action" %}Action queue
      {% elif view == "completed" %}Completed
      {% else %}All mail{% endif %}
    </h1>
    <p id="status-line" aria-live="polite">{{ last_run }}</p>
  </div>
  {% if view == "action" %}
  <div class="queue-count" aria-label="{{ action_count }} messages need attention">
    <strong>{{ action_count }}</strong><span>need attention</span>
  </div>
  {% endif %}
</div>

<nav class="view-tabs" aria-label="Triage views">
  <a href="/?view=action" aria-current="{{ 'page' if view == 'action' else 'false' }}">
    Action <span>{{ action_count }}</span>
  </a>
  <a href="/?view=all" aria-current="{{ 'page' if view == 'all' else 'false' }}">
    All mail <span>{{ all_unhandled_count }}</span>
  </a>
  <a href="/?view=completed" aria-current="{{ 'page' if view == 'completed' else 'false' }}">
    Completed <span>{{ completed_count }}</span>
  </a>
</nav>
```

Preserve `view` in filter URLs and hidden form inputs. Show category/noise filters only in
All mail; keep account and date controls in all three views.

- [ ] **Step 4: Reorder each mail card and expose its action**

Add `data-message-pk="{{ i.pk }}"` to `<article class="mail">`. Render the company/role and
summary before secondary subject/sender metadata. Add:

```html
<div class="mail-actions">
  {% if i.open_url %}
  <a class="btn primary sm" href="{{ i.open_url }}" target="_blank" rel="noopener">
    Open in {{ i.provider_label }}
  </a>
  {% endif %}
  {% if view == "completed" %}
  <button class="btn sm" type="button" data-undo="{{ i.pk }}">Restore</button>
  {% else %}
  <button class="btn sm" type="button" data-done="{{ i.pk }}">Done</button>
  {% endif %}
</div>
```

Until the Outlook plan supplies generic provider URLs, set `i.open_url` to the existing Gmail
URL and `i.provider_label` to `"Gmail"` in `web/app.py`.

- [ ] **Step 5: Add purposeful empty states**

Use:

```html
{% if view == "action" %}
  <h3>You're all caught up</h3>
  <p>No interview, assessment, offer, or reply is waiting for you.</p>
  <a class="btn" href="/?view=all">Review all mail</a>
{% elif view == "completed" %}
  <h3>Nothing completed yet</h3>
  <p>Messages you mark Done will appear here.</p>
  <a class="btn" href="/?view=action">Back to action queue</a>
{% endif %}
```

- [ ] **Step 6: Make shell navigation programmatic**

In `base.html`, add `aria-label="Primary"` to the main nav, add `aria-current="page"` to the
active segmented link, and make the brand a link to `/`. Change:

```html
<div id="toasts" aria-live="polite" aria-atomic="true"></div>
```

The urgent nav dot must have visually hidden text, not only a `title`.

- [ ] **Step 7: Run the smoke test**

Run: `python tests/test_smoke.py`

Expected: all rendering checks pass.

- [ ] **Step 8: Commit the dashboard markup**

```bash
git add mailcheck/web/templates/base.html mailcheck/web/templates/dashboard.html mailcheck/web/app.py tests/test_smoke.py
git commit -m "feat: focus dashboard on next actions"
```

### Task 4: Implement Done, Undo, and accessible interaction feedback

**Files:**
- Modify: `mailcheck/web/static/app.js`
- Modify: `mailcheck/web/templates/base.html`
- Modify: `mailcheck/web/templates/accounts.html`
- Modify: `mailcheck/web/templates/settings.html`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Consumes: `POST /api/messages/{pk}/done|undo`
- Produces: `toast(message, ok, ms, action)` with an optional action button
- Produces: keyboard-safe sheet open/close behavior

- [ ] **Step 1: Add an actionable toast API**

Replace `toast()` with:

```javascript
function toast(message, ok = true, ms = 4200, action = null) {
  const host = document.getElementById("toasts");
  const el = document.createElement("div");
  el.className = `toast ${ok ? "ok" : "bad"}`;
  el.setAttribute("role", ok ? "status" : "alert");
  el.innerHTML = `<span class="mark" aria-hidden="true">${ok ? "✓" : "!"}</span>
    <span class="msg"></span>`;
  el.querySelector(".msg").textContent = message;
  if (action) {
    const button = document.createElement("button");
    button.className = "toast-action";
    button.type = "button";
    button.textContent = action.label;
    button.addEventListener("click", async () => {
      await action.run();
      el.remove();
    });
    el.appendChild(button);
  }
  host.appendChild(el);
  const timer = setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 260);
  }, ok ? ms : Math.max(ms, 7000));
  el.addEventListener("focusin", () => clearTimeout(timer), { once: true });
}
```

- [ ] **Step 2: Wire Done with immediate local removal and Undo**

Add:

```javascript
async function setHandled(pk, handled) {
  return api(`/api/messages/${pk}/${handled ? "done" : "undo"}`);
}

document.addEventListener("click", async (event) => {
  const done = event.target.closest("[data-done]");
  const undo = event.target.closest("[data-undo]");
  const trigger = done || undo;
  if (!trigger) return;

  const pk = trigger.dataset.done || trigger.dataset.undo;
  const handled = !!done;
  const card = trigger.closest("[data-message-pk]");
  trigger.disabled = true;
  try {
    const result = await setHandled(pk, handled);
    if (card) card.remove();
    toast(result.message, true, 7000, handled ? {
      label: "Undo",
      run: async () => {
        await setHandled(pk, false);
        location.reload();
      },
    } : null);
  } catch (error) {
    trigger.disabled = false;
    toast(error.message, false);
  }
});
```

After removal, if no `.mail` remains in the action queue, reload once to show the server-rendered
all-caught-up state.

- [ ] **Step 3: Add sheet focus trapping and restoration**

Track the opener:

```javascript
let sheetOpener = null;

function openSheet(id, opener = document.activeElement) {
  const el = document.getElementById(id);
  sheetOpener = opener;
  el.hidden = false;
  document.body.classList.add("modal-open");
  const first = el.querySelector("input:not([type=hidden]), select, button");
  if (first) setTimeout(() => first.focus(), 60);
}

function closeSheet(id) {
  document.getElementById(id).hidden = true;
  document.body.classList.remove("modal-open");
  if (sheetOpener) sheetOpener.focus();
}
```

In the keydown listener, keep Tab focus inside the visible `.sheet`, and route Escape through
`closeSheet()`. Pass the clicked opener into `openSheet()`.

- [ ] **Step 4: Name every switch and connect hints to fields**

Add an `aria-label` to each account switch using the account label. Give the automatic-check
switch `aria-labelledby="auto-title"` and put `id="auto-title"` on its visible title. Add
`aria-describedby` to fields with hints, giving each hint a stable ID.

- [ ] **Step 5: Add a smoke-level accessibility assertion**

In `test_web_console()` add:

```python
shell = client.get("/").text
check("toast host is a live region", 'id="toasts" aria-live="polite"' in shell)
settings = client.get("/settings").text
check("automatic switch has an accessible name", 'aria-labelledby="auto-title"' in settings)
accounts_page = client.get("/accounts").text
check("account switches have labels", 'aria-label="Include gmail in checks"' in accounts_page)
```

- [ ] **Step 6: Run the smoke test**

Run: `python tests/test_smoke.py`

Expected: all checks pass.

- [ ] **Step 7: Commit interaction and accessibility behavior**

```bash
git add mailcheck/web/static/app.js mailcheck/web/templates/base.html mailcheck/web/templates/accounts.html mailcheck/web/templates/settings.html tests/test_smoke.py
git commit -m "feat: add accessible done and undo interactions"
```

### Task 5: Fix responsive containment and visual hierarchy

**Files:**
- Modify: `mailcheck/web/static/app.css`
- Modify: `mailcheck/web/templates/settings.html`

**Interfaces:**
- Produces: zero horizontal overflow at the four required viewport widths
- Produces: primary actions visible without expanding card details

- [ ] **Step 1: Add global containment and focus rules**

Add:

```css
html, body { max-width: 100%; overflow-x: clip; }
main, .nav, .card, .mail, .row, .row-main, .field, .mail-top { min-width: 0; }

:where(a, button, input, select, summary, .switch input):focus-visible {
  outline: 3px solid var(--accent-sunk);
  outline-offset: 2px;
}

.visually-hidden {
  position: absolute;
  width: 1px; height: 1px;
  padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0, 0, 0, 0);
  white-space: nowrap; border: 0;
}
```

- [ ] **Step 2: Style queue navigation and card actions**

Add:

```css
.queue-head { display: flex; align-items: end; justify-content: space-between; gap: 16px; }
.queue-count { display: flex; align-items: baseline; gap: 7px; color: var(--text-2); }
.queue-count strong { font-size: 28px; color: var(--act); font-variant-numeric: tabular-nums; }
.view-tabs { display: flex; gap: 4px; margin: 0 0 18px; overflow-x: auto; }
.view-tabs a { padding: 7px 11px; border-radius: var(--r-sm); color: var(--text-2); white-space: nowrap; }
.view-tabs a[aria-current="page"] { color: var(--text); background: var(--surface); box-shadow: var(--shadow-sm); }
.view-tabs span { color: var(--text-3); font-variant-numeric: tabular-nums; }
.mail-actions { display: flex; gap: 7px; margin-top: 12px; flex-wrap: wrap; }
.toast-action { border: 0; background: none; color: var(--accent); font: inherit; font-weight: 650; cursor: pointer; }
.modal-open { overflow: hidden; }
```

- [ ] **Step 3: Replace the mobile breakpoint with explicit stacking**

At `max-width: 640px`, include:

```css
.nav { height: auto; min-height: 52px; padding: 6px 10px; flex-wrap: wrap; }
.brand { flex: 0 0 auto; }
.segmented { order: 2; margin: 0; }
.next-check { margin-left: auto; max-width: 135px; overflow: hidden; text-overflow: ellipsis; }
.nav > [data-check] { width: 100%; order: 3; }
main { width: 100%; padding: 20px 14px 60px; }
.queue-head { align-items: flex-start; }
.toolbar select { flex: 1 1 145px; width: min(100%, 180px); }
.field-row { display: block; }
.row { align-items: flex-start; flex-wrap: wrap; }
.row-actions { width: 100%; justify-content: flex-end; }
.mail .who, .mail .subject, .mail .summary, .mail-foot { overflow-wrap: anywhere; }
.mail .when { margin-left: 0; }
.mail-actions .btn { flex: 1 1 120px; }
.sheet { max-width: 100%; max-height: calc(100dvh - 24px); border-radius: var(--r-lg); }
```

Remove the old mobile rules that hide only the brand text while leaving the navigation wider
than the viewport.

- [ ] **Step 4: Put technical settings behind Advanced**

Keep automatic checking and lookback visible. Wrap batch size, body characters, and interval
in:

```html
<details class="advanced-settings">
  <summary>Advanced checking settings</summary>
  <div class="card-body">
    <!-- existing batch, body-character, and interval fields -->
  </div>
</details>
```

Use one Save button for the entire Checking card. The auto switch may continue saving
immediately, but its subtitle must say “Saved immediately”; all numeric fields save only from
the single button.

- [ ] **Step 5: Manually verify the required viewport matrix**

Run:

```bash
python -m mailcheck web --no-open --port 8765
```

In browser responsive mode, check `/`, `/?view=all`, `/?view=completed`, `/accounts`, and
`/settings` at 320×568, 390×844, 768×1024, and 1440×900.

Expected at every size:

- `document.documentElement.scrollWidth === document.documentElement.clientWidth`
- Check now, Open, Done/Restore, and Save are visible.
- Long company/role, email, and endpoint values wrap or truncate inside their containers.
- Keyboard focus is visible and follows reading order.
- Dark mode retains readable status chips and action buttons.

- [ ] **Step 6: Commit responsive and settings polish**

```bash
git add mailcheck/web/static/app.css mailcheck/web/templates/settings.html
git commit -m "fix: make daily triage responsive and scannable"
```

### Task 6: Document and verify the complete daily-triage slice

**Files:**
- Modify: `README.md`
- Verify: `docs/DESIGN.md`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Consumes: every prior task
- Produces: a documented, independently releasable daily-triage experience

- [ ] **Step 1: Update the console documentation**

Replace the Triage bullet in `README.md` with copy that states:

```markdown
- **Triage** — opens on a focused action queue containing only unhandled interviews,
  assessments, offers, and messages that need a reply. Open the original in the provider,
  mark it Done locally, undo immediately, or restore it later from Completed. Done never
  marks, moves, or changes the provider message. All mail contains informational and noise
  tiers when you need them.
```

Add a sentence to Privacy: “Local Done/Undo state is stored only in SQLite.”

- [ ] **Step 2: Run the full automated verification**

Run: `python tests/test_smoke.py`

Expected: exit code 0 and zero failed checks.

- [ ] **Step 3: Run syntax and template smoke checks**

Run: `python -m compileall -q mailcheck`

Expected: exit code 0.

Run:

```bash
python -c "from fastapi.testclient import TestClient; from mailcheck.web.app import create_app; c=TestClient(create_app()); assert all(c.get(p).status_code == 200 for p in ['/', '/?view=all', '/?view=completed', '/accounts', '/settings'])"
```

Expected: exit code 0.

- [ ] **Step 4: Review the provider-read-only invariant**

Run:

```bash
rg -n "mark_seen=True|Mail.ReadWrite|/messages/.+PATCH|method=.?(PUT|PATCH|DELETE)" mailcheck
```

Expected: no provider-mail mutation path. Account deletion APIs are allowed; message mutation
against Gmail/Outlook is not.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md docs/DESIGN.md
git commit -m "docs: define the 30-second triage workflow"
```

