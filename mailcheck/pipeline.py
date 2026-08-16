"""The one pipeline. `check`, `watch` and the web UI are all thin wrappers on it."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from . import db, normalize, prefilter, secrets
from .config import Config
from .llm import LLMClient, PROMPT_VERSION, classify
from .models import NormalizedMessage, RunResult, TriagedMessage
from .sources import IMAPSource, OutlookGraphSource, SourceError
from .taxonomy import UNCLASSIFIED

StatusFn = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def build_source(account: db.Account, cfg: Config | None = None):
    """Select the provider adapter. Everything downstream is provider-neutral."""
    if account.provider == "outlook":
        cfg = cfg or Config()
        return OutlookGraphSource(
            label=account.label,
            client_id=cfg.outlook.client_id,
            email=account.email,
        )
    return IMAPSource(
        host=account.imap_host,
        port=account.imap_port,
        username=account.email,
        password=secrets.get_account_password(account.label),
        use_ssl=account.use_ssl,
        folder=account.folder,
    )


def fetch_account(
    account: db.Account, since: date, cfg: Config
) -> list[NormalizedMessage]:
    source = build_source(account, cfg)
    out: list[NormalizedMessage] = []
    for raw in source.fetch_unread(since):
        out.append(
            normalize.normalize(
                raw,
                account_id=account.id,
                account_label=account.label,
                max_body_chars=cfg.llm.max_body_chars,
            )
        )
    return out


def check_once(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    account_label: str | None = None,
    since_days: int | None = None,
    use_cache: bool = True,
    status: StatusFn = _noop,
    progress: Callable[[int, int], None] | None = None,
) -> RunResult:
    result = RunResult(started_at=datetime.now(timezone.utc))
    run_id = db.start_run(conn)

    accounts = db.list_accounts(conn, only_enabled=True)
    if account_label:
        accounts = [a for a in accounts if a.label == account_label]
        if not accounts:
            result.errors.append(f"No enabled account labelled '{account_label}'.")

    lookback = since_days if since_days is not None else cfg.check.lookback_days
    since = (datetime.now(timezone.utc) - timedelta(days=lookback)).date()

    # ---- retention -------------------------------------------------------
    # The local store is a rolling window, not an archive. Runs before the
    # fetch and on every path out of here, including the ones that fetch
    # nothing: how much mail is kept must not depend on whether this
    # particular check happened to find any.
    #
    # Never prunes inside the fetch window, even when the fetch window is the
    # wider of the two. A message deleted and then immediately re-downloaded
    # comes back as a new row with no handled_at — silently undoing a Done the
    # user had already given it. Keeping the two windows from overlapping is
    # what makes that impossible rather than merely unlikely.
    keep_days = max(cfg.check.retain_days, lookback)
    result.pruned = db.prune_messages(
        conn,
        before_iso=(datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat(),
    )

    # ---- fetch (one bad mailbox must not abort the run) -------------------
    fetched: list[NormalizedMessage] = []
    for account in accounts:
        status(f"Fetching {account.label} ({account.email})...")
        try:
            msgs = fetch_account(account, since, cfg)
        except (SourceError, secrets.SecretError) as exc:
            result.errors.append(f"[{account.label}] {exc}")
            continue
        fetched.extend(msgs)
        result.accounts_checked += 1
    result.fetched = len(fetched)

    if not fetched:
        result.finished_at = datetime.now(timezone.utc)
        _finish(conn, run_id, result)
        return result

    # ---- persist + resolve cache and prefilter ---------------------------
    pending: list[NormalizedMessage] = []
    pending_pks: list[int] = []

    for msg in fetched:
        pk = db.upsert_message(conn, msg)

        if use_cache:
            cached = db.get_cached(conn, pk, cfg.llm.model, PROMPT_VERSION)
            if cached:
                result.items.append(TriagedMessage(msg, cached, from_cache=True))
                result.from_cache += 1
                continue

        rule_hit = prefilter.apply(msg, cfg.prefilter_rules)
        if rule_hit:
            db.save_classification(
                conn, pk, rule_hit, cfg.llm.model, PROMPT_VERSION, commit=False
            )
            result.items.append(TriagedMessage(msg, rule_hit))
            result.prefiltered += 1
            continue

        pending.append(msg)
        pending_pks.append(pk)

    conn.commit()

    # ---- classify --------------------------------------------------------
    if pending:
        status(f"Classifying {len(pending)} email(s) with {cfg.llm.model}...")
        try:
            token = secrets.get_llm_token()
        except secrets.SecretError as exc:
            result.errors.append(str(exc))
            result.finished_at = datetime.now(timezone.utc)
            _finish(conn, run_id, result)
            return result

        with LLMClient(
            base_url=cfg.llm.base_url,
            token=token,
            model=cfg.llm.model,
            timeout=cfg.llm.timeout_seconds,
            max_retries=cfg.llm.max_retries,
            temperature=cfg.llm.temperature,
            use_json_mode=cfg.llm.use_json_mode,
        ) as client:
            results, errors = classify(
                client,
                pending,
                batch_size=cfg.llm.batch_size,
                concurrency=cfg.llm.concurrency,
                progress=progress,
            )
        result.errors.extend(errors)

        for msg, pk, cls in zip(pending, pending_pks, results):
            db.save_classification(
                conn, pk, cls, cfg.llm.model, PROMPT_VERSION, commit=False
            )
            result.items.append(TriagedMessage(msg, cls))
            result.classified += 1
        conn.commit()

    result.items.sort(key=lambda t: t.message.date_utc or datetime.min, reverse=True)
    result.finished_at = datetime.now(timezone.utc)
    _finish(conn, run_id, result)
    return result


def _row_to_normalized(row: sqlite3.Row) -> NormalizedMessage:
    """Rebuild the LLM's input from what was stored on the original run."""
    raw_date = row["date_utc"]
    date_utc = None
    if raw_date:
        try:
            date_utc = datetime.fromisoformat(raw_date)
        except ValueError:
            date_utc = None
    return NormalizedMessage(
        account_id=row["account_id"],
        account_label=row["account_label"],
        message_id=row["message_id"],
        uid=row["uid"] or "",
        folder=row["folder"] or "",
        from_addr=row["from_addr"] or "",
        from_name=row["from_name"] or "",
        subject=row["subject"] or "",
        date_utc=date_utc,
        body=row["body_text"] or "",
        provider_url=row["provider_url"],
    )


def reclassify(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    pks: list[int] | None = None,
    account_label: str | None = None,
    since_days: int | None = None,
    categories: list[str] | None = None,
    status: StatusFn = _noop,
    progress: Callable[[int, int], None] | None = None,
) -> RunResult:
    """Re-run classification over mail that came back ``unclassified``.

    A failed classification is sticky: the placeholder is written under the same
    (message, model, prompt version) key the cache is looked up by, so an
    ordinary check will hand back the failure forever and never try again. The
    only existing escape was ``--no-cache``, which re-bills every message in the
    window to rescue a handful. This retries exactly the ones that failed.

    Batches of one, deliberately: a batch reply that dropped or garbled an item
    is the usual cause, and re-sending the same batch shape tends to reproduce
    it. Single-message requests also take the stricter second-pass prompt in
    ``classify`` when the first attempt still will not parse.
    """
    result = RunResult(started_at=datetime.now(timezone.utc))
    run_id = db.start_run(conn)

    since_iso = None
    if since_days is not None:
        since_iso = (
            datetime.now(timezone.utc) - timedelta(days=since_days)
        ).isoformat()

    rows = db.messages_in_categories(
        conn,
        categories or [UNCLASSIFIED],
        pks=pks,
        account_label=account_label,
        since_iso=since_iso,
    )
    if not rows:
        result.finished_at = datetime.now(timezone.utc)
        _finish(conn, run_id, result)
        return result

    result.fetched = len(rows)
    result.accounts_checked = len({r["account_id"] for r in rows})

    # A rule the user added since the original run beats another paid call.
    pending: list[NormalizedMessage] = []
    pending_pks: list[int] = []
    for row in rows:
        msg = _row_to_normalized(row)
        rule_hit = prefilter.apply(msg, cfg.prefilter_rules)
        if rule_hit:
            db.save_classification(
                conn, row["pk"], rule_hit, cfg.llm.model, PROMPT_VERSION, commit=False
            )
            result.items.append(TriagedMessage(msg, rule_hit))
            result.prefiltered += 1
            continue
        pending.append(msg)
        pending_pks.append(row["pk"])
    conn.commit()

    if pending:
        status(f"Re-classifying {len(pending)} email(s) with {cfg.llm.model}...")
        try:
            token = secrets.get_llm_token()
        except secrets.SecretError as exc:
            result.errors.append(str(exc))
            result.finished_at = datetime.now(timezone.utc)
            _finish(conn, run_id, result)
            return result

        with LLMClient(
            base_url=cfg.llm.base_url,
            token=token,
            model=cfg.llm.model,
            timeout=cfg.llm.timeout_seconds,
            max_retries=cfg.llm.max_retries,
            temperature=cfg.llm.temperature,
            use_json_mode=cfg.llm.use_json_mode,
        ) as client:
            results, errors = classify(
                client,
                pending,
                batch_size=1,
                concurrency=cfg.llm.concurrency,
                progress=progress,
            )
        result.errors.extend(errors)

        for msg, pk, cls in zip(pending, pending_pks, results):
            db.save_classification(
                conn, pk, cls, cfg.llm.model, PROMPT_VERSION, commit=False
            )
            result.items.append(TriagedMessage(msg, cls))
            if cls.category != UNCLASSIFIED:
                result.classified += 1
        conn.commit()

    result.items.sort(key=lambda t: t.message.date_utc or datetime.min, reverse=True)
    result.finished_at = datetime.now(timezone.utc)
    _finish(conn, run_id, result)
    return result


def _finish(conn: sqlite3.Connection, run_id: int, result: RunResult) -> None:
    db.finish_run(
        conn,
        run_id,
        accounts_checked=result.accounts_checked,
        fetched=result.fetched,
        classified=result.classified,
        from_cache=result.from_cache,
        errors=result.errors,
    )


def pk_for(conn: sqlite3.Connection, item: TriagedMessage) -> int | None:
    row = conn.execute(
        "SELECT id FROM messages WHERE account_id = ? AND message_id = ?",
        (item.message.account_id, item.message.message_id),
    ).fetchone()
    return int(row["id"]) if row else None
