"""Local management console.

Binds 127.0.0.1 only — single local user, no auth. Because it now accepts
credential writes, non-GET requests carrying a foreign Origin are rejected, so a
web page you happen to have open cannot drive this API.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .. import config as cfgmod
from .. import db, outlook_auth, secrets
from ..pipeline import build_source, check_once, reclassify
from ..sources import IMAPSource, SourceError
from ..sources.presets import PRESETS, guess_from_email
from ..taxonomy import (
    CATEGORIES,
    TIER_ACT,
    TIER_META,
    TIER_ORDER,
    TIER_REPLY,
    TIER_UNKNOWN,
    UNCLASSIFIED,
    label_of,
    tier_of,
)

#: Tiers that make up the daily action queue. A failed classification
#: (``unclassified``) is exactly the kind of thing that must not go unseen, so
#: it belongs here alongside what needs a reply — never only in All mail.
ACTIONABLE_TIERS = (TIER_ACT, TIER_REPLY, TIER_UNKNOWN)
ACTIONABLE_CATEGORIES = [c.name for c in CATEGORIES if c.tier in ACTIONABLE_TIERS]

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

_job_lock = threading.Lock()
_job: dict = {"running": False, "message": "", "detail": "", "at": None, "ok": True}

#: In-flight Outlook device-code sign-in. Memory only — never written to disk.
#: ``active_id`` names the one flow whose eventual result should be honoured;
#: a background thread checks it before writing anything, so a cancelled or
#: superseded attempt can never save a token or create an account after the
#: fact — see the staleness checks in ``api_outlook_start``.
_outlook: dict = {"pending": None, "active_id": None, "state": "idle", "message": "", "email": ""}

#: Wall-clock epoch seconds of the next automatic check, or None when auto is off.
_sched: dict = {"next_due": None}
_scheduler_started = threading.Event()


def _reschedule(cfg=None) -> None:
    """Push the next automatic check one full interval into the future."""
    cfg = cfg or cfgmod.load()
    if cfg.watch.auto_check:
        _sched["next_due"] = time.time() + cfg.watch.interval_minutes * 60
    else:
        _sched["next_due"] = None


# ------------------------------------------------------------------ request bodies


class CheckBody(BaseModel):
    account: str | None = None
    since_days: int | None = None
    no_cache: bool = False


class ReclassifyBody(BaseModel):
    """Scope for a retry run. All fields optional: an empty body means
    "everything still unclassified", which is what the queue-wide button sends."""

    pks: list[int] | None = None
    account: str | None = None
    days: int | None = None


class AccountBody(BaseModel):
    label: str
    email: str
    host: str
    port: int = 993
    use_ssl: bool = True
    folder: str = "INBOX"
    password: str


class OutlookStartBody(BaseModel):
    label: str


class SettingsBody(BaseModel):
    auto_check: bool | None = None
    outlook_client_id: str | None = None
    base_url: str | None = None
    model: str | None = None
    token: str | None = None
    batch_size: int | None = None
    max_body_chars: int | None = None
    concurrency: int | None = None
    lookback_days: int | None = None
    interval_minutes: int | None = None
    privacy_ack: bool | None = None


class RuleBody(BaseModel):
    category: str
    sender: str | None = None
    sender_domain: str | None = None
    subject_contains: str | None = None


# ------------------------------------------------------------------------- helpers


def _err(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _state(conn=None, accounts=None) -> dict:
    """Everything the shell needs to render nav badges and setup prompts.

    Takes an open connection (and an already-read account list) when the caller
    has one: a page render would otherwise open a second database connection and
    repeat two queries it just ran.
    """
    cfg = cfgmod.load()
    if conn is None:
        with db.session() as own:
            accounts = db.list_accounts(own)
            summary = db.queue_counts(own)
    else:
        if accounts is None:
            accounts = db.list_accounts(conn)
        # Same definition as the queue itself (unhandled act/reply/unknown), so
        # the nav dot means exactly "there is something in your queue" — not a
        # lifetime count that includes mail already marked Done.
        summary = db.queue_counts(conn)
    due = _sched["next_due"]
    return {
        "llm_ready": cfg.is_llm_ready() and secrets.has_llm_token(),
        "has_accounts": any(a.enabled for a in accounts),
        "account_count": len(accounts),
        "urgent_count": summary["actionable"],
        "auto_check": cfg.watch.auto_check,
        # Seeds the countdown so it reads correctly before the first status poll.
        "next_in": max(0, int(due - time.time())) if due else None,
        "cfg": cfg,
    }


URGENT_CATEGORIES = [c.name for c in CATEGORIES if c.tier == TIER_ACT]


def _start_job(work, *, failed: str, on_finish=None) -> None:
    """Run ``work`` on a thread against the shared ``_job`` status block.

    The caller must already hold ``_job_lock``; this releases it. Both the
    check and the re-classify run report through the same block, so the
    progress bar, status line and toasts work for either with no extra client
    plumbing.
    """

    def runner():
        _job.update(running=True, message="Connecting…", detail="", ok=True)
        try:
            work()
        except Exception as exc:  # noqa: BLE001 - surface it in the UI, never 500
            _job.update(message=failed, detail=str(exc), ok=False)
        finally:
            _job.update(running=False, at=datetime.now().strftime("%H:%M"))
            if on_finish is not None:
                on_finish()
            _job_lock.release()

    threading.Thread(target=runner, daemon=True).start()


def _run_check(body: CheckBody) -> None:
    def work():
        cfg = cfgmod.load()
        with db.session() as conn:
            result = check_once(
                conn,
                cfg,
                account_label=body.account,
                since_days=body.since_days,
                use_cache=not body.no_cache,
                status=lambda m: _job.update(message=m),
                progress=lambda done, total: _job.update(
                    message=f"Classifying… {done} of {total}"
                ),
            )
        bits = [f"{result.fetched} unread", f"{result.classified} classified"]
        if result.from_cache:
            bits.append(f"{result.from_cache} cached")
        if result.prefiltered:
            bits.append(f"{result.prefiltered} filtered locally")
        _job.update(
            message=" · ".join(bits),
            detail="\n".join(result.errors[:5]),
            ok=not result.errors,
        )

    # A manual check counts as a check: restart the clock from now.
    _start_job(work, failed="Check failed", on_finish=_reschedule)


def _run_reclassify(body: ReclassifyBody) -> None:
    def work():
        cfg = cfgmod.load()
        with db.session() as conn:
            result = reclassify(
                conn,
                cfg,
                pks=body.pks or None,
                account_label=body.account,
                since_days=body.days,
                status=lambda m: _job.update(message=m),
                progress=lambda done, total: _job.update(
                    message=f"Re-classifying… {done} of {total}"
                ),
            )
        if not result.fetched:
            _job.update(message="Nothing left to re-classify.", detail="", ok=True)
            return
        still = result.fetched - result.classified - result.prefiltered
        bits = [f"{result.classified + result.prefiltered} of {result.fetched} resolved"]
        if still:
            bits.append(f"{still} still unclassified")
        _job.update(
            message=" · ".join(bits),
            detail="\n".join(result.errors[:5]),
            ok=not result.errors,
        )

    _start_job(work, failed="Re-classification failed")


def _scheduler() -> None:
    """Fire an automatic check when one is due.

    Runs in the web server process, so the countdown the page shows is backed by
    something real. `mail-check watch` remains the option for running unattended
    without a browser.
    """
    while True:
        time.sleep(2)
        try:
            cfg = cfgmod.load()
            if not cfg.watch.auto_check:
                _sched["next_due"] = None
                continue
            if _sched["next_due"] is None:
                _reschedule(cfg)
                continue
            if _job["running"] or time.time() < _sched["next_due"]:
                continue

            state = _state()
            if not (state["llm_ready"] and state["has_accounts"]):
                _reschedule(cfg)  # not set up yet; try again next interval
                continue
            if _job_lock.acquire(blocking=False):
                _reschedule(cfg)
                _run_check(CheckBody())
        except Exception:  # noqa: BLE001 - the scheduler must outlive a bad cycle
            _sched["next_due"] = None


# ----------------------------------------------------------------------------- app


def create_app() -> FastAPI:
    app = FastAPI(title="mail-check", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    if not _scheduler_started.is_set():
        _scheduler_started.set()
        threading.Thread(target=_scheduler, daemon=True).start()

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and (urlparse(origin).hostname or "") not in _LOCAL_HOSTS:
                return _err("Cross-origin requests are not allowed.", 403)
        return await call_next(request)

    # ------------------------------------------------------------------- pages

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        view: str = "queue",
        category: str | None = None,
        account: str | None = None,
        days: int = 30,
    ):
        """One template, three views.

        queue     — unhandled act + reply only. The default: the daily to-do list.
        all       — everything, grouped by tier.
        completed — locally handled items, most recent first.
        """
        if view not in ("queue", "all", "completed"):
            view = "queue"
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        # Matches each view's own filter, so the category dropdown's counts
        # describe what is actually on screen rather than every message ever
        # stored regardless of Done state.
        view_handled = {"queue": False, "completed": True, "all": None}[view]

        with db.session() as conn:
            if view == "queue":
                rows = db.query_triaged(
                    conn,
                    categories=[category] if category else ACTIONABLE_CATEGORIES,
                    account_label=account,
                    since_iso=since,
                    handled=False,
                )
            elif view == "completed":
                rows = db.query_triaged(
                    conn,
                    categories=[category] if category else None,
                    account_label=account,
                    since_iso=since,
                    handled=True,
                )
            else:
                rows = db.query_triaged(
                    conn,
                    categories=[category] if category else None,
                    account_label=account,
                    since_iso=since,
                )
            counts = db.category_counts(
                conn, since_iso=since, account_label=account, handled=view_handled
            )
            summary = db.queue_counts(conn, since_iso=since, account_label=account)
            accounts = db.list_accounts(conn)
            last = db.last_run(conn)
            state = _state(conn, accounts)

        groups: dict[str, list] = {t: [] for t in TIER_ORDER}
        for row in rows:
            item = dict(row)
            item["tier"] = tier_of(row["category"])
            item["category_label"] = label_of(row["category"])
            item["date_display"] = _fmt_date(row["date_utc"])
            item["deadline_display"] = _fmt_deadline(row["deadline"])
            item["open_url"], item["open_label"] = _open_link(row)
            groups[item["tier"]].append(item)

        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "page": "triage",
                "view": view,
                "groups": groups,
                "tier_order": TIER_ORDER,
                "tier_meta": TIER_META,
                "summary": summary,
                "categories": [c for c in CATEGORIES if counts.get(c.name)],
                "counts": counts,
                "accounts": accounts,
                "selected_category": category,
                "selected_account": account,
                "days": days,
                "total": len(rows),
                "last_run": _fmt_run(last),
                "unclassified_count": counts.get("unclassified", 0),
                **state,
            },
        )

    @app.get("/api/notifications")
    def api_notifications():
        """Urgent mail the browser has not announced yet.

        Does NOT mark anything as announced — the client does that via
        ``/api/notifications/ack`` only for the popups it actually managed to
        construct. Marking here instead, before the client had a chance to try,
        would permanently lose an alert to a denied permission, a constructor
        error, or the tab closing mid-flight.
        """
        with db.session() as conn:
            rows = db.pending_notifications(conn, URGENT_CATEGORIES)
            items = [
                {
                    "pk": r["pk"],
                    "category": r["category"],
                    "title": label_of(r["category"]),
                    "who": r["company"] or r["role"] or r["subject"],
                    "summary": r["summary"] or r["subject"],
                    "deadline": _fmt_deadline(r["deadline"]),
                    # No deep link exists for this provider (e.g. non-Gmail
                    # IMAP) — fall back to the queue rather than a null URL.
                    "url": _open_link(r)[0] or "/?view=queue",
                }
                for r in rows
            ]
        return {"ok": True, "items": items}

    @app.post("/api/notifications/ack")
    def api_notifications_ack(pks: list[int]):
        """Mark only the popups the browser actually managed to show."""
        if pks:
            with db.session() as conn:
                db.mark_announced(conn, pks)
        return {"ok": True}

    @app.post("/api/messages/{pk}/handled")
    def api_set_handled(pk: int, done: bool = True):
        """Local Done / Undo. Never touches the provider mailbox.

        Returns the recomputed headline counts so the page can update its
        badges in place. Reloading to refresh three numbers meant re-running
        every dashboard query and rebuilding the whole card list on each click.
        """
        with db.session() as conn:
            row = db.get_message(conn, pk)
            if not row:
                return _err("No such message.", 404)
            db.set_handled(conn, pk, done)
            summary = db.queue_counts(conn)
        return {
            "ok": True,
            "message": "Marked done." if done else "Restored to the queue.",
            "pk": pk,
            "done": done,
            "summary": summary,
        }

    @app.get("/api/messages/{pk}/body")
    def api_message_body(pk: int):
        """The stored body, fetched only when the user opens Details.

        Bodies are by far the largest column, and a list of 500 of them is most
        of the dashboard's weight for something the reader looks at one at a
        time — if at all.
        """
        with db.session() as conn:
            body = db.get_message_body(conn, pk)
        if body is None:
            return _err("No such message.", 404)
        return {"ok": True, "body": body}

    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(request: Request):
        with db.session() as conn:
            accounts = db.list_accounts(conn)
            state = _state(conn, accounts)
        return TEMPLATES.TemplateResponse(
            request,
            "accounts.html",
            {
                "page": "accounts",
                "accounts": accounts,
                "presets": [p for p in PRESETS],
                "outlook_ready": bool(state["cfg"].outlook.client_id),
                **state,
            },
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        state = _state()
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            {
                "page": "settings",
                "has_token": secrets.has_llm_token(),
                "categories": [c for c in CATEGORIES if c.name != "unclassified"],
                "config_path": str(cfgmod.config_path()),
                "db_path": str(cfgmod.db_path()),
                **state,
            },
        )

    # --------------------------------------------------------------------- api

    @app.post("/api/check")
    def api_check(body: CheckBody):
        state = _state()
        if not state["llm_ready"]:
            return _err("The model endpoint is not configured yet.")
        if not state["has_accounts"]:
            return _err("Add a mail account first.")
        if not _job_lock.acquire(blocking=False):
            return _err("A check is already running.", 409)
        _run_check(body)
        return {"ok": True}

    @app.post("/api/reclassify")
    def api_reclassify(body: ReclassifyBody):
        """Retry the mail that came back unclassified.

        Shares the check's lock and status block — the two both write
        classifications and drive the same progress UI, so they must not
        overlap.
        """
        if not _state()["llm_ready"]:
            return _err("The model endpoint is not configured yet.")
        with db.session() as conn:
            waiting = db.messages_in_categories(
                conn,
                [UNCLASSIFIED],
                pks=body.pks or None,
                account_label=body.account,
                since_iso=(
                    (datetime.now(timezone.utc) - timedelta(days=body.days)).isoformat()
                    if body.days
                    else None
                ),
            )
        if not waiting:
            return _err("Nothing is waiting to be re-classified.", 404)
        if not _job_lock.acquire(blocking=False):
            return _err("A check is already running.", 409)
        _run_reclassify(body)
        return {"ok": True, "count": len(waiting)}

    @app.get("/api/status")
    def api_status():
        cfg = cfgmod.load()
        due = _sched["next_due"]
        return {
            **_job,
            "auto": cfg.watch.auto_check,
            "interval_minutes": cfg.watch.interval_minutes,
            # Seconds remaining, so the browser never has to trust its own clock
            # agreeing with the server's.
            "next_in": max(0, int(due - time.time())) if due else None,
        }

    @app.post("/api/autocheck")
    def api_autocheck(enabled: bool):
        cfg = cfgmod.load()
        cfg.watch.auto_check = enabled
        cfgmod.save(cfg)
        _reschedule(cfg)
        if not enabled:
            return {"ok": True, "message": "Automatic checks turned off."}
        return {
            "ok": True,
            "message": f"Checking automatically every {cfg.watch.interval_minutes} min.",
        }

    @app.get("/api/preset")
    def api_preset(email: str):
        preset = guess_from_email(email)
        if not preset:
            return {"ok": True, "preset": None}
        return {
            "ok": True,
            "preset": {
                "key": preset.key,
                "name": preset.name,
                "host": preset.host,
                "port": preset.port,
                "use_ssl": preset.use_ssl,
                "note": preset.note,
                "supported": preset.supported,
            },
        }

    @app.post("/api/accounts")
    def api_add_account(body: AccountBody):
        label = body.label.strip()
        if not label or not body.email.strip() or not body.host.strip():
            return _err("Label, email and IMAP host are all required.")
        with db.session() as conn:
            if db.get_account(conn, label):
                return _err(f"An account labelled “{label}” already exists.")
        try:
            secrets.set_account_password(label, body.password)
        except secrets.SecretError as exc:
            return _err(str(exc))
        try:
            IMAPSource(
                host=body.host,
                port=body.port,
                username=body.email,
                password=secrets.get_account_password(label),
                use_ssl=body.use_ssl,
                folder=body.folder,
            ).test()
        except (SourceError, secrets.SecretError) as exc:
            secrets.delete_account_password(label)  # never keep a bad credential
            return _err(str(exc))

        with db.session() as conn:
            db.add_account(
                conn,
                label=label,
                email=body.email.strip(),
                imap_host=body.host.strip(),
                imap_port=body.port,
                use_ssl=body.use_ssl,
                folder=body.folder or "INBOX",
            )
        return {"ok": True, "message": f"Connected to {body.email}."}

    @app.post("/api/accounts/{label}/test")
    def api_test_account(label: str):
        with db.session() as conn:
            account = db.get_account(conn, label)
        if not account:
            return _err("No such account.", 404)
        try:
            build_source(account, cfgmod.load()).test()
        except (SourceError, secrets.SecretError, outlook_auth.OutlookAuthError) as exc:
            return _err(str(exc))
        return {"ok": True, "message": "Connection OK."}

    @app.post("/api/accounts/{label}/toggle")
    def api_toggle_account(label: str, enabled: bool):
        with db.session() as conn:
            if not db.set_account_enabled(conn, label, enabled):
                return _err("No such account.", 404)
        return {"ok": True, "message": ("Enabled" if enabled else "Paused") + f" {label}."}

    @app.post("/api/accounts/{label}/delete")
    def api_delete_account(label: str):
        with db.session() as conn:
            if not db.remove_account(conn, label):
                return _err("No such account.", 404)
        secrets.delete_account_password(label)
        outlook_auth.delete_cache(label)  # a token must not outlive its account
        return {"ok": True, "message": f"Removed {label}."}

    # ------------------------------------------------------- outlook sign-in

    @app.post("/api/outlook/start")
    def api_outlook_start(body: OutlookStartBody):
        cfg = cfgmod.load()
        if not cfg.outlook.client_id:
            return _err(
                "No Microsoft application client ID configured. Add one under "
                "Settings → Outlook before connecting an account."
            )
        label = body.label.strip()
        if not label:
            return _err("Give the account a label first.")
        with db.session() as conn:
            if db.get_account(conn, label):
                return _err(f"An account labelled “{label}” already exists.")

        # A new attempt supersedes whatever was in flight — for this label or
        # any other — so two browser tabs, or a restarted attempt, cannot leave
        # an orphaned thread polling Microsoft after the user has moved on.
        old = _outlook.get("pending")
        if old is not None:
            outlook_auth.abort(old)

        try:
            pending = outlook_auth.begin_device_flow(label, cfg.outlook.client_id)
        except outlook_auth.OutlookAuthError as exc:
            return _err(str(exc))

        _outlook.update(
            pending=pending, active_id=pending.id, state="waiting", message="", email=""
        )

        def wait():
            flow_id = pending.id
            try:
                email = outlook_auth.complete_device_flow(pending)
            except outlook_auth.OutlookAuthError as exc:
                outlook_auth.delete_cache(label)
                if _outlook.get("active_id") == flow_id:
                    _outlook.update(pending=None, state="failed", message=str(exc))
                return
            except Exception as exc:  # noqa: BLE001 - report, never crash the thread
                outlook_auth.delete_cache(label)
                if _outlook.get("active_id") == flow_id:
                    _outlook.update(
                        pending=None, state="failed",
                        message=f"{type(exc).__name__}: {exc}",
                    )
                return

            # Success — but if this flow was cancelled or superseded while we
            # were blocked, the cancellation is authoritative: the token we
            # just saved must not survive, and the account must not appear.
            if _outlook.get("active_id") != flow_id:
                outlook_auth.delete_cache(label)
                return
            with db.session() as conn:
                if not db.get_account(conn, label):
                    db.add_account(
                        conn, label=label, email=email or label, provider="outlook"
                    )
            _outlook.update(
                pending=None, state="connected", email=email, message=f"Connected {email}."
            )

        threading.Thread(target=wait, daemon=True).start()
        return {
            "ok": True,
            "user_code": pending.user_code,
            "verification_uri": pending.verification_uri,
        }

    @app.get("/api/outlook/status")
    def api_outlook_status():
        pending = _outlook.get("pending")
        if pending is not None and pending.expired:
            # Stop the background thread, not just the state the UI reads —
            # otherwise it keeps polling Microsoft for up to its own ~15 min
            # server-side expiry with nothing left to report the result to.
            outlook_auth.abort(pending)
            _outlook.update(
                pending=None, active_id=None, state="failed",
                message="The sign-in code expired. Start again.",
            )
            pending = None
        return {
            "state": _outlook["state"],
            "message": _outlook["message"],
            "email": _outlook["email"],
            "user_code": pending.user_code if pending else "",
            "verification_uri": pending.verification_uri if pending else "",
        }

    @app.post("/api/outlook/cancel")
    def api_outlook_cancel():
        pending = _outlook.get("pending")
        if pending is not None:
            outlook_auth.abort(pending)              # unblocks the background thread
            outlook_auth.delete_cache(pending.label)  # in case it already wrote one
        _outlook.update(pending=None, active_id=None, state="idle", message="", email="")
        return {"ok": True, "message": "Sign-in cancelled."}

    @app.post("/api/settings")
    def api_settings(body: SettingsBody):
        cfg = cfgmod.load()
        data = cfg.model_dump()
        for field in ("base_url", "model", "batch_size", "max_body_chars", "concurrency"):
            value = getattr(body, field)
            if value is not None:
                data["llm"][field] = value
        if body.lookback_days is not None:
            data["check"]["lookback_days"] = body.lookback_days
        if body.interval_minutes is not None:
            data["watch"]["interval_minutes"] = body.interval_minutes
        if body.auto_check is not None:
            data["watch"]["auto_check"] = body.auto_check
        if body.outlook_client_id is not None:
            data["outlook"]["client_id"] = body.outlook_client_id.strip()
        if body.privacy_ack is not None:
            data["privacy_ack"] = body.privacy_ack
        try:
            cfg = cfgmod.Config.model_validate(data)
        except Exception as exc:  # noqa: BLE001 - pydantic's own message is the useful bit
            detail = getattr(exc, "errors", None)
            msg = detail()[0]["msg"] if callable(detail) and detail() else str(exc)
            return _err(msg)
        cfgmod.save(cfg)
        if body.token:
            secrets.set_llm_token(body.token)
        _reschedule(cfg)  # a changed interval takes effect immediately
        return {"ok": True, "message": "Settings saved."}

    @app.post("/api/settings/test")
    def api_settings_test():
        from ..llm import LLMClient, LLMError

        cfg = cfgmod.load()
        if not cfg.is_llm_ready():
            return _err("Set a base URL and model first.")
        try:
            with LLMClient(
                base_url=cfg.llm.base_url,
                token=secrets.get_llm_token(),
                model=cfg.llm.model,
                timeout=cfg.llm.timeout_seconds,
                max_retries=2,
            ) as client:
                reply = client.ping()
        except (LLMError, secrets.SecretError) as exc:
            return _err(str(exc))
        return {"ok": True, "message": f"Model replied: {reply.strip()[:120]}"}

    @app.post("/api/rules")
    def api_add_rule(body: RuleBody):
        if not (body.sender or body.sender_domain or body.subject_contains):
            return _err("A rule needs at least one condition.")
        cfg = cfgmod.load()
        cfg.prefilter_rules.append(cfgmod.PrefilterRule(**body.model_dump()))
        cfgmod.save(cfg)
        return {"ok": True, "message": "Rule added."}

    @app.post("/api/rules/{index}/delete")
    def api_delete_rule(index: int):
        cfg = cfgmod.load()
        if not 0 <= index < len(cfg.prefilter_rules):
            return _err("No such rule.", 404)
        cfg.prefilter_rules.pop(index)
        cfgmod.save(cfg)
        return {"ok": True, "message": "Rule removed."}

    return app


def _short_date(dt: datetime) -> str:
    # %-d is not portable to Windows; strip the zero by hand.
    return dt.strftime("%b %d").replace(" 0", " ")


def _fmt_date(value: str | None) -> str:
    """Relative date that stays sensible for future timestamps.

    A mail dated slightly ahead of the local clock (skewed sender, timezone edge)
    must not render as "-1 days ago".
    """
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value[:16]
    dt = dt.replace(tzinfo=None)
    now = datetime.now()
    days = (dt.date() - now.date()).days

    if days > 0:
        if days == 1:
            return "Tomorrow"
        return f"In {days} days" if days < 7 else _short_date(dt)
    if days == 0:
        return dt.strftime("%H:%M")
    if days == -1:
        return "Yesterday"
    if days > -7:
        return f"{-days} days ago"
    return _short_date(dt)


def _fmt_deadline(value: str | None) -> str:
    """Deadlines are what the queue is really about, so they say how long is left."""
    if not value:
        return ""
    try:
        due = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return value
    days = (due - datetime.now().date()).days
    if days < 0:
        return f"Overdue — was {_short_date(datetime.combine(due, datetime.min.time()))}"
    if days == 0:
        return "Due today"
    if days == 1:
        return "Due tomorrow"
    if days < 7:
        return f"Due in {days} days"
    return f"Due {_short_date(datetime.combine(due, datetime.min.time()))}"


def _open_link(row) -> tuple[str | None, str]:
    """Prefer the provider's own deep link.

    IMAP has no equivalent, but Gmail's web UI supports searching by
    Message-ID, so that link only gets built when the account's own IMAP host
    is actually Gmail/Google Workspace — never assumed for Yahoo, iCloud,
    Fastmail or any other IMAP provider, which have no equivalent URL scheme.
    Those get no link at all: a card with no working deep link is confusing,
    but a wrong one that opens someone else's inbox is worse.
    """
    keys = row.keys()
    url = row["provider_url"] if "provider_url" in keys else None
    provider = (row["provider"] if "provider" in keys else "imap") or "imap"
    if url:
        return url, "Open in Outlook" if provider == "outlook" else "Open original"

    host = ((row["imap_host"] if "imap_host" in keys else "") or "").lower()
    if "gmail" in host or "google" in host:
        return (
            "https://mail.google.com/mail/u/0/#search/rfc822msgid:"
            f"{quote(row['message_id'])}",
            "Open in Gmail",
        )
    return None, ""


def _fmt_run(row) -> str:
    if not row:
        return "Never checked"
    try:
        when = datetime.fromisoformat(row["finished_at"]).astimezone().strftime("%H:%M")
    except (ValueError, TypeError):
        return "Never checked"
    return f"Last checked {when} · {row['fetched']} unread"
