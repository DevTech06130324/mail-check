"""SQLite storage: accounts, fetched messages, classification cache, run history."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import config
from .models import Classification, NormalizedMessage

SCHEMA_VERSION = 7

#: ``INSERT ... RETURNING`` needs SQLite 3.35 (2021). Python 3.11 bundles far
#: newer than that everywhere we run, but the two-statement fallback costs
#: nothing to keep and means an old system SQLite degrades instead of crashing.
_HAS_RETURNING = sqlite3.sqlite_version_info >= (3, 35, 0)

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY,
    label       TEXT NOT NULL UNIQUE,
    email       TEXT NOT NULL,
    provider    TEXT NOT NULL DEFAULT 'imap',
    imap_host   TEXT NOT NULL DEFAULT '',
    imap_port   INTEGER NOT NULL DEFAULT 993,
    use_ssl     INTEGER NOT NULL DEFAULT 1,
    folder      TEXT NOT NULL DEFAULT 'INBOX',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    message_id  TEXT NOT NULL,
    uid         TEXT,
    folder      TEXT,
    from_addr   TEXT,
    from_name   TEXT,
    subject     TEXT,
    date_utc    TEXT,
    body_text   TEXT,
    snippet     TEXT,
    provider_url TEXT,
    handled_at  TEXT,
    fetched_at  TEXT NOT NULL,
    notified    INTEGER NOT NULL DEFAULT 0,
    web_notified INTEGER NOT NULL DEFAULT 0,
    UNIQUE(account_id, message_id)
);


CREATE TABLE IF NOT EXISTS classifications (
    id              INTEGER PRIMARY KEY,
    message_pk      INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    category        TEXT NOT NULL,
    confidence      REAL DEFAULT 0,
    company         TEXT,
    role            TEXT,
    deadline        TEXT,
    action_required INTEGER NOT NULL DEFAULT 0,
    summary         TEXT,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'llm',
    retryable       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    UNIQUE(message_pk, model, prompt_version)
);

CREATE TABLE IF NOT EXISTS runs (
    id               INTEGER PRIMARY KEY,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    accounts_checked INTEGER DEFAULT 0,
    fetched          INTEGER DEFAULT 0,
    classified       INTEGER DEFAULT 0,
    from_cache       INTEGER DEFAULT 0,
    fetch_seconds    REAL DEFAULT 0,
    classification_seconds REAL DEFAULT 0,
    llm_requests     INTEGER DEFAULT 0,
    llm_timeouts     INTEGER DEFAULT 0,
    llm_busy_responses INTEGER DEFAULT 0,
    llm_retries      INTEGER DEFAULT 0,
    errors           TEXT
);
"""


@dataclass
class Account:
    id: int
    label: str
    email: str
    imap_host: str
    imap_port: int
    use_ssl: bool
    folder: str
    enabled: bool
    provider: str = "imap"


#: Applied only after the column migrations — on a v1 database the tables already
#: exist, so CREATE TABLE IF NOT EXISTS is a no-op and an index over a new column
#: would reference something that is not there yet.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date_utc DESC);
CREATE INDEX IF NOT EXISTS idx_messages_handled ON messages(handled_at);
CREATE INDEX IF NOT EXISTS idx_messages_acct_date ON messages(account_id, date_utc DESC);
CREATE INDEX IF NOT EXISTS idx_messages_handled_date ON messages(handled_at, date_utc DESC);
CREATE INDEX IF NOT EXISTS idx_messages_received
    ON messages(julianday(COALESCE(date_utc, fetched_at)), id);
CREATE INDEX IF NOT EXISTS idx_class_latest
    ON classifications(message_pk, created_at DESC, id DESC);
"""

#: Columns added after v1. SQLite cannot add them inside CREATE TABLE IF NOT
#: EXISTS on a database that already exists, so they are applied separately.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("accounts", "provider", "TEXT NOT NULL DEFAULT 'imap'"),
    ("messages", "provider_url", "TEXT"),
    ("messages", "handled_at", "TEXT"),
    ("messages", "web_notified", "INTEGER NOT NULL DEFAULT 0"),
    ("classifications", "retryable", "INTEGER NOT NULL DEFAULT 0"),
    ("runs", "fetch_seconds", "REAL DEFAULT 0"),
    ("runs", "classification_seconds", "REAL DEFAULT 0"),
    ("runs", "llm_requests", "INTEGER DEFAULT 0"),
    ("runs", "llm_timeouts", "INTEGER DEFAULT 0"),
    ("runs", "llm_busy_responses", "INTEGER DEFAULT 0"),
    ("runs", "llm_retries", "INTEGER DEFAULT 0"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in _MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    # imap_tools reports an unparseable Date header as 1900-01-01 rather than
    # None. Such rows were being pruned as ancient and hidden from every date
    # window; an unknown date is NULL, which falls back to fetched_at.
    conn.execute("UPDATE messages SET date_utc = NULL WHERE date_utc < '1971'")
    conn.commit()


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the database, bringing the schema up to date only when it is not.

    The console opens a connection per request, so the setup work here runs
    constantly. Everything below the ``user_version`` check — four CREATE TABLE
    statements, a PRAGMA table_info per migration, and the index script — is a
    no-op on an already-current database, so it is skipped entirely rather than
    re-issued on every open.
    """
    path = path or config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the console read while a check is writing, instead of the two
    # blocking each other; NORMAL trades an fsync per commit for one per
    # checkpoint, which is the right call for a cache that can always be
    # rebuilt by re-running a check.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")

    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.executescript(INDEXES)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- accounts


def add_account(
    conn: sqlite3.Connection,
    *,
    label: str,
    email: str,
    imap_host: str = "",
    imap_port: int = 993,
    use_ssl: bool = True,
    folder: str = "INBOX",
    provider: str = "imap",
) -> int:
    cur = conn.execute(
        "INSERT INTO accounts (label, email, provider, imap_host, imap_port, use_ssl,"
        " folder, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (label, email, provider, imap_host, imap_port, int(use_ssl), folder, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def _row_to_account(row: sqlite3.Row) -> Account:
    keys = row.keys()
    return Account(
        id=row["id"],
        label=row["label"],
        email=row["email"],
        imap_host=row["imap_host"],
        imap_port=row["imap_port"],
        use_ssl=bool(row["use_ssl"]),
        folder=row["folder"],
        enabled=bool(row["enabled"]),
        provider=(row["provider"] if "provider" in keys else "imap") or "imap",
    )


def list_accounts(conn: sqlite3.Connection, only_enabled: bool = False) -> list[Account]:
    sql = "SELECT * FROM accounts"
    if only_enabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY label"
    return [_row_to_account(r) for r in conn.execute(sql)]


def get_account(conn: sqlite3.Connection, label: str) -> Account | None:
    row = conn.execute("SELECT * FROM accounts WHERE label = ?", (label,)).fetchone()
    return _row_to_account(row) if row else None


def remove_account(conn: sqlite3.Connection, label: str) -> bool:
    cur = conn.execute("DELETE FROM accounts WHERE label = ?", (label,))
    conn.commit()
    return cur.rowcount > 0


def set_account_enabled(conn: sqlite3.Connection, label: str, enabled: bool) -> bool:
    cur = conn.execute(
        "UPDATE accounts SET enabled = ? WHERE label = ?", (int(enabled), label)
    )
    conn.commit()
    return cur.rowcount > 0


# --------------------------------------------------------------------------- messages


_UPSERT_MESSAGE = """
        INSERT INTO messages
            (account_id, message_id, uid, folder, from_addr, from_name,
             subject, date_utc, body_text, snippet, provider_url, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_id, message_id) DO UPDATE SET
            uid = excluded.uid,
            body_text = excluded.body_text,
            snippet = excluded.snippet,
            provider_url = COALESCE(excluded.provider_url, messages.provider_url)
"""


def upsert_message(conn: sqlite3.Connection, msg: NormalizedMessage) -> int:
    """Insert if new; return the primary key either way.

    ``handled_at`` is deliberately absent from the UPDATE clause: re-fetching or
    re-classifying a message must never resurrect something the user marked Done.
    """
    params = (
        msg.account_id,
        msg.message_id,
        msg.uid,
        msg.folder,
        msg.from_addr,
        msg.from_name,
        msg.subject,
        msg.date_utc.isoformat() if msg.date_utc else None,
        msg.body,
        msg.snippet,
        msg.provider_url,
        _now(),
    )
    if _HAS_RETURNING:
        # DO UPDATE always fires on conflict, so a row comes back either way.
        row = conn.execute(_UPSERT_MESSAGE + " RETURNING id", params).fetchone()
        return int(row["id"])

    conn.execute(_UPSERT_MESSAGE, params)
    row = conn.execute(
        "SELECT id FROM messages WHERE account_id = ? AND message_id = ?",
        (msg.account_id, msg.message_id),
    ).fetchone()
    return int(row["id"])


def mark_notified(conn: sqlite3.Connection, pks: list[int]) -> None:
    if not pks:
        return
    conn.executemany(
        "UPDATE messages SET notified = 1 WHERE id = ?", [(pk,) for pk in pks]
    )
    conn.commit()


def pending_notifications(
    conn: sqlite3.Connection,
    categories: list[str],
    *,
    limit: int = 20,
    since_iso: str | None = None,
) -> list[sqlite3.Row]:
    """Urgent, unhandled mail the browser has not announced yet.

    ``since_iso`` keeps an alert from pointing at mail the queue's own date
    window would not show.
    """
    if not categories:
        return []
    sql = (
        """
        SELECT m.id AS pk, m.subject, m.message_id, m.provider_url,
               a.provider, a.imap_host, a.email AS account_email, c.category, c.company, c.role, c.summary, c.deadline
        FROM messages m
        JOIN accounts a ON a.id = m.account_id
        """
        + LATEST_CLASSIFICATION
        + f"""
        WHERE m.handled_at IS NULL
          AND m.web_notified = 0
          AND c.category IN ({','.join('?' * len(categories))})
        {"AND COALESCE(m.date_utc, m.fetched_at) >= ?" if since_iso else ""}
        ORDER BY COALESCE(m.date_utc, m.fetched_at) DESC
        LIMIT ?
    """
    )
    params = [*categories, *([since_iso] if since_iso else []), limit]
    return list(conn.execute(sql, params))


def mark_announced(conn: sqlite3.Connection, pks: list[int]) -> None:
    if not pks:
        return
    conn.executemany(
        "UPDATE messages SET web_notified = 1 WHERE id = ?", [(pk,) for pk in pks]
    )
    conn.commit()


def set_handled(conn: sqlite3.Connection, pk: int, handled: bool) -> bool:
    """Mark Done / restore. Local only — no provider mailbox is touched."""
    cur = conn.execute(
        "UPDATE messages SET handled_at = ? WHERE id = ?",
        (_now() if handled else None, pk),
    )
    conn.commit()
    return cur.rowcount > 0


def get_message(conn: sqlite3.Connection, pk: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM messages WHERE id = ?", (pk,)).fetchone()


def get_message_body(conn: sqlite3.Connection, pk: int) -> str | None:
    """The stored body on its own. ``None`` means no such message."""
    row = conn.execute("SELECT body_text FROM messages WHERE id = ?", (pk,)).fetchone()
    return None if row is None else (row["body_text"] or "")


def prune_messages(conn: sqlite3.Connection, *, before_iso: str) -> int:
    """Delete stored mail older than ``before_iso``. Returns how many went.

    Local only, like every other write here: this empties rows out of
    mail-check's own database and never touches a provider mailbox, so a pruned
    message is still sitting in Gmail or Outlook, still unread.

    Classifications go with it — the foreign key declares ON DELETE CASCADE and
    ``connect()`` turns foreign keys on, which SQLite does not do by itself.

    Mail with no Date header ages out on when it was fetched instead, so a
    missing header cannot buy a message an indefinite stay. Both columns hold
    UTC ISO strings (``date_utc`` naive, ``fetched_at`` with an offset), which
    order correctly as text at the precision a cutoff needs — the same
    comparison every query in this module already makes against a cutoff.
    """
    cur = conn.execute(
        "DELETE FROM messages WHERE COALESCE(date_utc, fetched_at) < ?", (before_iso,)
    )
    conn.commit()
    return cur.rowcount


# -------------------------------------------------------------------- classifications


def get_cached(
    conn: sqlite3.Connection, message_pk: int, model: str, prompt_version: str
) -> Classification | None:
    row = conn.execute(
        "SELECT * FROM classifications WHERE message_pk = ? AND model = ? AND prompt_version = ?",
        (message_pk, model, prompt_version),
    ).fetchone()
    if not row:
        return None
    if row["retryable"]:
        return None
    return Classification(
        category=row["category"],
        confidence=row["confidence"] or 0.0,
        company=row["company"],
        role=row["role"],
        deadline=row["deadline"],
        action_required=bool(row["action_required"]),
        summary=row["summary"] or "",
        source=row["source"],
        retryable=bool(row["retryable"]),
    )


def save_classification(
    conn: sqlite3.Connection,
    message_pk: int,
    result: Classification,
    model: str,
    prompt_version: str,
    *,
    commit: bool = True,
) -> None:
    """Store a result, overwriting any previous one for the same model+version.

    ``commit=False`` is for the pipeline's write loop, which saves one row per
    classified message and would otherwise force a separate transaction — and,
    under the old ``synchronous=FULL``, a separate fsync — for every single
    email. It commits once when the loop is done instead.
    """
    conn.execute(
        """
        INSERT INTO classifications
            (message_pk, category, confidence, company, role, deadline,
             action_required, summary, model, prompt_version, source, retryable, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_pk, model, prompt_version) DO UPDATE SET
            category = excluded.category,
            confidence = excluded.confidence,
            company = excluded.company,
            role = excluded.role,
            deadline = excluded.deadline,
            action_required = excluded.action_required,
            summary = excluded.summary,
            source = excluded.source,
            retryable = excluded.retryable,
            created_at = excluded.created_at
        """,
        (
            message_pk,
            result.category,
            result.confidence,
            result.company,
            result.role,
            result.deadline,
            int(result.action_required),
            result.summary,
            model,
            prompt_version,
            result.source,
            int(result.retryable),
            _now(),
        ),
    )
    if commit:
        conn.commit()


# ------------------------------------------------------------------------------ runs


def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (_now(),))
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    accounts_checked: int,
    fetched: int,
    classified: int,
    from_cache: int,
    errors: list[str],
    fetch_seconds: float = 0.0,
    classification_seconds: float = 0.0,
    llm_requests: int = 0,
    llm_timeouts: int = 0,
    llm_busy_responses: int = 0,
    llm_retries: int = 0,
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, accounts_checked = ?, fetched = ?,"
        " classified = ?, from_cache = ?, errors = ?, fetch_seconds = ?,"
        " classification_seconds = ?, llm_requests = ?, llm_timeouts = ?,"
        " llm_busy_responses = ?, llm_retries = ? WHERE id = ?",
        (
            _now(),
            accounts_checked,
            fetched,
            classified,
            from_cache,
            "\n".join(errors) or None,
            fetch_seconds,
            classification_seconds,
            llm_requests,
            llm_timeouts,
            llm_busy_responses,
            llm_retries,
            run_id,
        ),
    )
    conn.commit()


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()


# ----------------------------------------------------------------------- reporting

#: Joins each message to its most recent classification, and nothing else.
#:
#: The join predicate is on ``c.id`` — the primary key — rather than on
#: ``c.message_pk`` with the newest-row test moved into the WHERE clause. Both
#: forms return the same rows, but the latter makes SQLite produce every
#: classification a message ever had and then throw away all but one of them,
#: which is O(revisions) rows discarded per message. Matching the primary key
#: directly means the subquery (covered by ``idx_class_latest``) resolves to a
#: single id and the join is one row lookup.
#: When a message counts as having arrived: its Date header, or the time it was
#: fetched when the header is missing or unparseable. Every date window uses
#: this, so undated mail is neither hidden nor immortal.
RECEIVED = "COALESCE(m.date_utc, m.fetched_at)"

LATEST_CLASSIFICATION = """
        JOIN classifications c ON c.id = (
            SELECT id FROM classifications
            WHERE message_pk = m.id ORDER BY created_at DESC, id DESC LIMIT 1
        )
"""


def category_counts(
    conn: sqlite3.Connection,
    *,
    since_iso: str | None = None,
    account_label: str | None = None,
    handled: bool | None = None,
) -> dict[str, int]:
    """Counts per category over the latest classification of each message."""
    sql = (
        """
        SELECT c.category AS category, COUNT(*) AS n
        FROM messages m
        JOIN accounts a ON a.id = m.account_id
        """
        + LATEST_CLASSIFICATION
        + " WHERE 1 = 1"
    )
    params: list = []
    if since_iso:
        sql += f" AND {RECEIVED} >= ?"
        params.append(since_iso)
    if account_label:
        sql += " AND a.label = ?"
        params.append(account_label)
    if handled is True:
        sql += " AND m.handled_at IS NOT NULL"
    elif handled is False:
        sql += " AND m.handled_at IS NULL"
    sql += " GROUP BY c.category"
    return {r["category"]: r["n"] for r in conn.execute(sql, params)}


def query_triaged(
    conn: sqlite3.Connection,
    *,
    categories: list[str] | None = None,
    account_label: str | None = None,
    since_iso: str | None = None,
    handled: bool | None = None,
    limit: int = 500,
) -> list[sqlite3.Row]:
    """Latest classification per message, joined with the message and account.

    ``handled`` filters on the local Done state: False = still in the queue,
    True = completed, None = both.
    """
    # ``body_text`` is deliberately not selected: full email bodies for every row
    # dominate both this result set and the HTML built from it, and the console
    # only ever shows one at a time, on demand — see get_triaged() and the
    # /api/messages/{pk}/reader fragment it feeds.
    sql = (
        """
        SELECT m.id AS pk, m.message_id, m.subject, m.from_addr, m.from_name,
               m.date_utc, m.snippet, m.provider_url, m.handled_at,
               a.label AS account_label, a.email AS account_email, a.provider, a.imap_host,
               c.category, c.confidence, c.company, c.role, c.deadline,
               c.action_required, c.summary, c.source, c.retryable, c.created_at
        FROM messages m
        JOIN accounts a ON a.id = m.account_id
        """
        + LATEST_CLASSIFICATION
        + " WHERE 1 = 1"
    )
    params: list = []
    if categories:
        sql += f" AND c.category IN ({','.join('?' * len(categories))})"
        params += categories
    if account_label:
        sql += " AND a.label = ?"
        params.append(account_label)
    if since_iso:
        sql += f" AND {RECEIVED} >= ?"
        params.append(since_iso)
    if handled is True:
        sql += " AND m.handled_at IS NOT NULL"
    elif handled is False:
        sql += " AND m.handled_at IS NULL"
    # Completed reads best most-recently-finished first; everything else by date.
    sql += f" ORDER BY m.handled_at DESC, {RECEIVED} DESC LIMIT ?" if handled else \
           f" ORDER BY {RECEIVED} DESC LIMIT ?"
    params.append(limit)
    return list(conn.execute(sql, params))


def last_handled(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The message marked Done most recently, if any.

    Backs the console's Ctrl+Z once the page has no history of its own: a
    finished check reloads the page, and undo should not quietly stop meaning
    anything because the tab was rebuilt underneath the reader.
    """
    return conn.execute(
        """
        SELECT id AS pk, subject, handled_at
        FROM messages
        WHERE handled_at IS NOT NULL
        ORDER BY handled_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()


def get_triaged(conn: sqlite3.Connection, pk: int) -> sqlite3.Row | None:
    """One message with its latest classification, body included.

    The list query deliberately leaves ``body_text`` out, because 500 full
    bodies are most of a page's weight. This is the other half of that trade:
    the reader asks for one message at a time and gets everything it needs to
    render in a single round trip.
    """
    return conn.execute(
        """
        SELECT m.id AS pk, m.message_id, m.subject, m.from_addr, m.from_name,
               m.date_utc, m.fetched_at, m.snippet, m.body_text, m.provider_url, m.handled_at,
               a.label AS account_label, a.email AS account_email, a.provider, a.imap_host,
               c.category, c.confidence, c.company, c.role, c.deadline,
               c.action_required, c.summary, c.source, c.retryable, c.created_at
        FROM messages m
        JOIN accounts a ON a.id = m.account_id
        """
        + LATEST_CLASSIFICATION
        + " WHERE m.id = ?",
        (pk,),
    ).fetchone()


def queue_counts(
    conn: sqlite3.Connection,
    *,
    since_iso: str | None = None,
    account_label: str | None = None,
) -> dict[str, int]:
    """Headline numbers for the action queue: actionable, informational, done.

    "Actionable" matches exactly what the queue view itself shows — including
    ``unclassified`` mail, which needs a manual look precisely because
    automatic classification failed on it and it must never go unseen.
    """
    from .taxonomy import TIER_ACT, TIER_REPLY, TIER_UNKNOWN, tier_of

    # One pass grouped by (category, done) rather than two full passes over the
    # same join — every page render needs all three numbers at once.
    sql = (
        """
        SELECT c.category AS category,
               m.handled_at IS NOT NULL AS done,
               COUNT(*) AS n
        FROM messages m
        JOIN accounts a ON a.id = m.account_id
        """
        + LATEST_CLASSIFICATION
        + " WHERE 1 = 1"
    )
    params: list = []
    if since_iso:
        sql += f" AND {RECEIVED} >= ?"
        params.append(since_iso)
    if account_label:
        sql += " AND a.label = ?"
        params.append(account_label)
    sql += " GROUP BY c.category, done"

    actionable = informational = done = 0
    for row in conn.execute(sql, params):
        if row["done"]:
            done += row["n"]
        elif tier_of(row["category"]) in (TIER_ACT, TIER_REPLY, TIER_UNKNOWN):
            actionable += row["n"]
        else:
            informational += row["n"]
    return {"actionable": actionable, "informational": informational, "done": done}


def messages_in_categories(
    conn: sqlite3.Connection,
    categories: list[str],
    *,
    pks: list[int] | None = None,
    account_label: str | None = None,
    since_iso: str | None = None,
    limit: int = 200,
) -> list[sqlite3.Row]:
    """Full message rows (bodies included) whose *latest* classification is one
    of ``categories`` — what a re-classification run needs as its input.

    Nothing is re-fetched from the mail provider: the cleaned body captured on
    the original run is still in the database, so a retry costs one LLM call and
    no mailbox traffic at all.
    """
    if not categories:
        return []
    sql = (
        """
        SELECT m.id AS pk, m.account_id, m.message_id, m.uid, m.folder,
               m.from_addr, m.from_name, m.subject, m.date_utc, m.body_text,
               m.provider_url, a.label AS account_label
        FROM messages m
        JOIN accounts a ON a.id = m.account_id
        """
        + LATEST_CLASSIFICATION
        + f" WHERE c.category IN ({','.join('?' * len(categories))})"
    )
    params: list = [*categories]
    if pks:
        sql += f" AND m.id IN ({','.join('?' * len(pks))})"
        params += pks
    if account_label:
        sql += " AND a.label = ?"
        params.append(account_label)
    if since_iso:
        sql += f" AND {RECEIVED} >= ?"
        params.append(since_iso)
    sql += f" ORDER BY {RECEIVED} DESC LIMIT ?"
    params.append(limit)
    return list(conn.execute(sql, params))
