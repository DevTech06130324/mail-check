"""Local management console.

Binds 127.0.0.1 only — single local user, no auth. Because it now accepts
credential writes, non-GET requests carrying a foreign Origin are rejected, so a
web page you happen to have open cannot drive this API.
"""

from __future__ import annotations

import threading
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError

from .. import config as cfgmod
from .. import db, outlook_auth, secrets
from ..analytics import DashboardFilters, dashboard_data
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
from . import bodyhtml

#: Tiers that make up the daily action queue. A failed classification
#: (``unclassified``) is exactly the kind of thing that must not go unseen, so
#: it belongs here alongside what needs a reply — never only in All mail.
ACTIONABLE_TIERS = (TIER_ACT, TIER_REPLY, TIER_UNKNOWN)
ACTIONABLE_CATEGORIES = [c.name for c in CATEGORIES if c.tier in ACTIONABLE_TIERS]

HERE = Path(__file__).parent
FRONTEND_DIST = HERE / "frontend_dist"
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))


def _asset_version() -> str:
    """Newest mtime across the static files, as a cache-busting stamp.

    The stylesheet and the script are served without a Cache-Control header, so
    a browser is free to reuse whatever it already has — which during any UI
    work means an old layout rendered over new markup, and no way to tell that
    from a bug. Stamping the URL makes an edited file a different URL, and the
    question stops coming up.
    """
    newest = max(
        (f.stat().st_mtime for f in (HERE / "static").glob("*") if f.is_file()),
        default=0.0,
    )
    return str(int(newest))


#: The function, not its result: a stylesheet edited while the server is up
#: is exactly when a stale copy is most confusing, and a few stat() calls
#: per render cost nothing on a single-user local app.
TEMPLATES.env.globals["asset_v"] = _asset_version

#: ``testserver`` is Starlette's TestClient host; it is not resolvable on a real
#: network, so allowing it costs nothing.
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "testserver"}

_job_lock = threading.Lock()
_job: dict = {"running": False, "message": "", "detail": "", "at": None,
              "ok": True, "stage_started": None, "completion_id": 0}

#: In-flight Outlook device-code sign-in. Memory only — never written to disk.
#: ``active_id`` names the one flow whose eventual result should be honoured;
#: a background thread checks it before writing anything, so a cancelled or
#: superseded attempt can never save a token or create an account after the
#: fact — see the staleness checks in ``api_outlook_start``.
_outlook: dict = {"pending": None, "active_id": None, "state": "idle", "message": "", "email": ""}
#: Makes "is this flow still the active one?" and "save the account" one step.
#: Without it a cancel landing between the two leaves an account behind that
#: the user just cancelled.
_outlook_lock = threading.Lock()

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
    since_days: int | None = Field(default=None, ge=1, le=3650)
    no_cache: bool = False


class ReclassifyBody(BaseModel):
    """Scope for a retry run. All fields optional: an empty body means
    "everything still unclassified", which is what the queue-wide button sends."""

    pks: list[int] | None = None
    account: str | None = None
    days: int | None = Field(default=None, ge=1, le=3650)


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
    num_ctx: int | None = None
    think: bool | None = None
    keep_alive: str | None = None
    timeout_seconds: int | None = None
    classification_deadline_seconds: int | None = None
    batch_size: int | None = None
    max_body_chars: int | None = None
    concurrency: int | None = None
    lookback_days: int | None = None
    retain_days: int | None = None
    interval_minutes: int | None = None
    privacy_ack: bool | None = None


class RuleBody(BaseModel):
    category: str
    sender: str | None = None
    sender_domain: str | None = None
    subject_contains: str | None = None


class MessageSummary(BaseModel):
    pk: int
    message_id: str
    subject: str
    from_addr: str
    from_name: str
    date_utc: str | None = None
    snippet: str = ""
    provider_url: str | None = None
    handled_at: str | None = None
    account_label: str
    provider: str = "imap"
    category: str
    confidence: float = 0
    company: str | None = None
    role: str | None = None
    deadline: str | None = None
    action_required: bool = False
    summary: str = ""
    source: str = "llm"
    retryable: bool = False
    tier: str
    category_label: str
    date_estimated: bool
    date_display: str
    deadline_display: str
    open_url: str | None = None
    open_label: str = ""


class TriageGroup(BaseModel):
    tier: str
    label: str
    items: list[MessageSummary]


class TriageResponse(BaseModel):
    view: str
    groups: list[TriageGroup]
    summary: dict[str, int]
    counts: dict[str, int]
    accounts: list[dict[str, Any]]
    selected_account: str | None
    selected_category: str | None
    days: int
    total: int


class PublicAccount(BaseModel):
    id: int
    label: str
    email: str
    provider: str
    imap_host: str
    imap_port: int
    use_ssl: bool
    folder: str
    enabled: bool


class AccountsResponse(BaseModel):
    accounts: list[PublicAccount]
    outlook_ready: bool


class ReaderResponse(BaseModel):
    message: MessageSummary
    body_html: str


class BootstrapReadiness(BaseModel):
    model: bool
    accounts: bool
    privacy_ack: bool
    outlook: bool


class BootstrapResponse(BaseModel):
    readiness: BootstrapReadiness
    counts: dict[str, int]
    taxonomy: list[dict[str, str]]
    presets: list[dict[str, Any]]
    last_run: str
    account_count: int


class SettingsResponse(BaseModel):
    settings: dict[str, Any]
    rules: list[dict[str, Any]]
    categories: list[dict[str, str]]
    config_path: str
    db_path: str


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
            summary = db.queue_counts(own, since_iso=_default_since())
    else:
        if accounts is None:
            accounts = db.list_accounts(conn)
        # Same definition as the queue itself (unhandled act/reply/unknown), so
        # the nav dot means exactly "there is something in your queue" — not a
        # lifetime count that includes mail already marked Done.
        summary = db.queue_counts(conn, since_iso=_default_since())
    due = _sched["next_due"]
    return {
        "llm_ready": cfg.is_llm_ready(),
        "has_accounts": any(a.enabled for a in accounts),
        "account_count": len(accounts),
        "urgent_count": summary["actionable"],
        "auto_check": cfg.watch.auto_check,
        # Seeds the countdown so it reads correctly before the first status poll.
        "next_in": max(0, int(due - time.time())) if due else None,
        "cfg": cfg,
    }


URGENT_CATEGORIES = [c.name for c in CATEGORIES if c.tier == TIER_ACT]

#: The queue's default window (``days=30``). The nav badge and bootstrap counts
#: use the same one, so the number on the badge is the number the queue shows —
#: mail older than that can outlive it locally when ``retain_days`` is larger.
DEFAULT_DAYS = 30


def _default_since() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=DEFAULT_DAYS)).isoformat()


def _start_job(work, *, failed: str, on_finish=None) -> None:
    """Run ``work`` on a thread against the shared ``_job`` status block.

    The caller must already hold ``_job_lock``; this releases it. Both the
    check and the re-classify run report through the same block, so the
    progress bar, status line and toasts work for either with no extra client
    plumbing.
    """

    def runner():
        _job.update(running=True, message="Connecting…", detail="", ok=True,
                    stage_started=time.time())
        try:
            work()
        except Exception as exc:  # noqa: BLE001 - surface it in the UI, never 500
            _job.update(message=failed, detail=str(exc), ok=False)
        finally:
            _job.update(running=False, at=datetime.now().strftime("%H:%M"),
                        stage_started=None, completion_id=_job["completion_id"] + 1)
            if on_finish is not None:
                on_finish()
            _job_lock.release()

    threading.Thread(target=runner, daemon=True).start()


def _run_check(body: CheckBody) -> None:
    def work():
        cfg = cfgmod.load()

        def event(values: dict) -> None:
            if values.get("type") == "llm_request_started":
                _job.update(
                    message=(f"Classifying batch {values['batch_index']} of "
                             f"{values['total_batches']} · {values['completed']} of "
                             f"{values.get('total', '?')} complete"),
                    stage_started=time.time(),
                )
            elif values.get("type") == "model_loading":
                _job.update(message="Loading the model into memory — this can take a couple of minutes…",
                            stage_started=time.time())
            elif values.get("type") == "llm_request" and values.get("outcome") in {
                "timeout", "busy", "deadline"
            }:
                _job.update(message="Ollama busy; remaining mail will retry next check")

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
                event=event,
            )
        bits = [f"{result.fetched} unread", f"{result.classified} classified"]
        if result.from_cache:
            bits.append(f"{result.from_cache} cached")
        if result.prefiltered:
            bits.append(f"{result.prefiltered} filtered locally")
        if result.pruned:
            bits.append(f"{result.pruned} aged out")
        if result.retryable:
            bits.append(f"{result.retryable} deferred; Ollama busy — retry next check")
        _job.update(
            message=" · ".join(bits),
            detail="\n".join(result.errors[:5]),
            ok=not result.errors or result.retryable > 0,
        )

    # A manual check counts as a check: restart the clock from now.
    _start_job(work, failed="Check failed", on_finish=_reschedule)


def _run_reclassify(body: ReclassifyBody) -> None:
    def work():
        cfg = cfgmod.load()

        def event(values: dict) -> None:
            if values.get("type") == "llm_request_started":
                _job.update(
                    message=(f"Re-classifying batch {values['batch_index']} of "
                             f"{values['total_batches']} · {values['completed']} of "
                             f"{values.get('total', '?')} complete"),
                    stage_started=time.time(),
                )
            elif values.get("type") == "model_loading":
                _job.update(message="Loading the model into memory — this can take a couple of minutes…",
                            stage_started=time.time())
            elif values.get("type") == "llm_request" and values.get("outcome") in {
                "timeout", "busy", "deadline"
            }:
                _job.update(message="Ollama busy; press Retry again to continue")

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
                event=event,
            )
        if not result.fetched:
            _job.update(message="Nothing left to re-classify.", detail="", ok=True)
            return
        still = result.fetched - result.classified - result.prefiltered
        bits = [f"{result.classified + result.prefiltered} of {result.fetched} resolved"]
        if still:
            bits.append(f"{still} still unclassified")
        if result.retryable:
            bits.append("Ollama busy — press Retry again to continue")
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
    if (FRONTEND_DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")

    def frontend_page():
        index = FRONTEND_DIST / "index.html"
        if index.is_file():
            # The shell points at content-hashed assets. Always revalidate the
            # shell so a browser cannot keep references from an older build.
            return FileResponse(index, headers={"Cache-Control": "no-cache, must-revalidate"})
        return None

    @app.exception_handler(tomllib.TOMLDecodeError)
    async def unreadable_config(request: Request, exc: tomllib.TOMLDecodeError):
        # The parser's message can quote the offending line, so it is not echoed.
        return PlainTextResponse(
            f"The saved configuration at {cfgmod.config_path()} is not valid TOML.\n\n"
            "Fix or delete the file, then reload this page.",
            status_code=503,
        )

    @app.exception_handler(ValidationError)
    async def invalid_saved_config(request: Request, exc: ValidationError):
        # Validation errors may contain old endpoint credentials or other values.
        # Show recovery instructions without echoing the invalid input.
        return PlainTextResponse(
            f"The saved configuration at {cfgmod.config_path()} needs updating.\n\n"
            "For an old model endpoint, run:\n"
            "mail-check init --base-url http://192.168.2.230:11440 "
            "--model qwen3.5:35b-a3b\n\n"
            "This backs up the existing configuration and preserves other settings. "
            "If another setting is invalid, correct it in the configuration file, "
            "then reload this page. Automatic checks cannot run until it is valid.",
            status_code=503,
        )

    if not _scheduler_started.is_set():
        _scheduler_started.set()
        threading.Thread(target=_scheduler, daemon=True).start()

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        # DNS rebinding: a page on an attacker's domain that resolves to
        # 127.0.0.1 makes same-origin GETs to this server, and the Origin check
        # below never sees them. Its requests still carry the attacker's Host.
        host = urlparse("//" + request.headers.get("host", "")).hostname or ""
        if host not in _LOCAL_HOSTS:
            return _err("Unrecognised Host header.", 403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and (urlparse(origin).hostname or "") not in _LOCAL_HOSTS:
                return _err("Cross-origin requests are not allowed.", 403)
        return await call_next(request)

    # ------------------------------------------------------------------- pages

    @app.get("/dashboard", response_class=HTMLResponse)
    def analytics_dashboard(request: Request):
        page = frontend_page()
        if page:
            return page
        with db.session() as conn:
            accounts = db.list_accounts(conn)
            state = _state(conn, accounts)
        return TEMPLATES.TemplateResponse(request, "analytics.html", {
            "page": "analytics", "main_class": "wide", "accounts": accounts,
            "categories": CATEGORIES, **state,
        })

    @app.get("/api/dashboard")
    def api_dashboard(request: Request):
        values = dict(request.query_params)
        values.pop("category", None)
        values["categories"] = request.query_params.getlist("category")
        try:
            filters = DashboardFilters.model_validate(values)
        except ValidationError as exc:
            return _err(exc.errors()[0]["msg"])
        cfg = cfgmod.load()
        with db.session() as conn:
            return dashboard_data(conn, filters, retention_days=cfg.check.retain_days)

    @app.get("/api/bootstrap", response_model=BootstrapResponse)
    def api_bootstrap():
        """Small, non-sensitive shell data shared by every React page."""
        with db.session() as conn:
            accounts = db.list_accounts(conn)
            state = _state(conn, accounts)
            run = db.last_run(conn)
            summary = db.queue_counts(conn, since_iso=_default_since())
        return {
            "readiness": {
                "model": state["llm_ready"],
                "accounts": state["has_accounts"],
                "privacy_ack": state["cfg"].privacy_ack,
                "outlook": bool(state["cfg"].outlook.client_id),
            },
            "counts": summary,
            "taxonomy": [{"name": c.name, "label": c.label, "tier": c.tier}
                         for c in CATEGORIES],
            "presets": [{"key": p.key, "name": p.name, "host": p.host,
                         "port": p.port, "use_ssl": p.use_ssl, "note": p.note,
                         "supported": p.supported} for p in PRESETS],
            "last_run": _fmt_run(run),
            "account_count": state["account_count"],
        }

    @app.get("/api/triage", response_model=TriageResponse)
    def api_triage(view: str = "queue", category: str | None = None,
                   account: str | None = None, days: int = 30):
        """Read the same bounded, body-free rows as the original console."""
        if view not in ("queue", "all", "completed"):
            return _err("Unknown mail view.")
        if days not in (2, 3, 7, 14, 30, 90):
            return _err("Choose a supported time range.")
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        handled = {"queue": False, "completed": True, "all": None}[view]
        with db.session() as conn:
            rows = db.query_triaged(
                conn,
                categories=([category] if category else
                            ACTIONABLE_CATEGORIES if view == "queue" else None),
                account_label=account,
                since_iso=since,
                handled=handled,
                limit=500,
            )
            counts = db.category_counts(conn, since_iso=since, account_label=account,
                                        handled=handled)
            summary = db.queue_counts(conn, since_iso=since, account_label=account)
            accounts = db.list_accounts(conn)
        grouped: dict[str, list] = {tier: [] for tier in TIER_ORDER}
        for row in rows:
            item = _decorate(row)
            grouped[item["tier"]].append(item)
        return {
            "view": view,
            "groups": [{"tier": tier, "label": TIER_META[tier][0],
                        "items": grouped[tier]} for tier in TIER_ORDER if grouped[tier]],
            "summary": summary,
            "counts": counts,
            "accounts": [{"label": a.label, "enabled": a.enabled} for a in accounts],
            "selected_account": account,
            "selected_category": category,
            "days": days,
            "total": len(rows),
        }

    @app.get("/api/messages/{pk}", response_model=ReaderResponse)
    def api_message(pk: int):
        with db.session() as conn:
            row = db.get_triaged(conn, pk)
            if not row:
                return _err("No such message.", 404)
            body_html = bodyhtml.render_body(row["body_text"])
        return {"message": _decorate(row), "body_html": str(body_html)}

    @app.get("/api/accounts", response_model=AccountsResponse)
    def api_accounts():
        with db.session() as conn:
            accounts = db.list_accounts(conn)
        return {
            "accounts": [{
                "id": a.id, "label": a.label, "email": a.email,
                "provider": a.provider, "imap_host": a.imap_host,
                "imap_port": a.imap_port, "use_ssl": a.use_ssl,
                "folder": a.folder, "enabled": a.enabled,
            } for a in accounts],
            "outlook_ready": bool(cfgmod.load().outlook.client_id),
        }

    @app.get("/api/settings", response_model=SettingsResponse)
    def api_settings():
        cfg = cfgmod.load()
        return {
            "settings": {
                "llm": cfg.llm.model_dump(),
                "check": cfg.check.model_dump(),
                "watch": cfg.watch.model_dump(),
                "outlook": cfg.outlook.model_dump(),
                "privacy_ack": cfg.privacy_ack,
            },
            "rules": [rule.model_dump() for rule in cfg.prefilter_rules],
            "categories": [{"name": c.name, "label": c.label}
                           for c in CATEGORIES if c.name != UNCLASSIFIED],
            "config_path": str(cfgmod.config_path()),
            "db_path": str(cfgmod.db_path()),
        }

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        view: str = "queue",
        category: str | None = None,
        account: str | None = None,
        days: int = Query(30, ge=1, le=3650),
    ):
        """One template, three views.

        queue     — unhandled act + reply only. The default: the daily to-do list.
        all       — everything, grouped by tier.
        completed — locally handled items, most recent first.
        """
        page = frontend_page()
        if page:
            return page
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
            item = _decorate(row)
            groups[item["tier"]].append(item)

        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "page": "triage",
                # Triage is the one page laid out in two panes; the rest are
                # single-column reading width and must stay that way.
                "main_class": "wide",
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
            rows = db.pending_notifications(conn, URGENT_CATEGORIES, since_iso=_default_since())
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
            summary = db.queue_counts(conn, since_iso=_default_since())
        return {
            "ok": True,
            "message": "Marked done." if done else "Restored to the queue.",
            "pk": pk,
            "done": done,
            "summary": summary,
        }

    @app.post("/api/messages/undo-last")
    def api_undo_last():
        """Restore the most recently completed message.

        The console undoes from its own in-page history, which is exact and
        needs no round trip. This is the fallback for when that history is
        gone — a finished check reloads the page — so that Ctrl+Z keeps
        working rather than silently doing nothing.
        """
        with db.session() as conn:
            row = db.last_handled(conn)
            if not row:
                return {"ok": True, "pk": None, "message": "Nothing to undo."}
            db.set_handled(conn, row["pk"], False)
            summary = db.queue_counts(conn, since_iso=_default_since())
        subject = (row["subject"] or "").strip()
        return {
            "ok": True,
            "pk": row["pk"],
            "message": f"Restored “{subject[:60]}”." if subject else "Restored to the queue.",
            "summary": summary,
        }

    @app.get("/api/messages/{pk}/reader", response_class=HTMLResponse)
    def api_message_reader(request: Request, pk: int):
        """The right-hand pane for one message, rendered server-side.

        Sending this with the page instead — a hidden block per row — put half
        the document's bytes into detail for mail the reader looks at one at a
        time, which is the same trade already rejected for bodies. Fetching it
        on selection also keeps every formatted value coming from the same
        template as the list, rather than being rebuilt in JavaScript.
        """
        with db.session() as conn:
            row = db.get_triaged(conn, pk)
            if not row:
                return HTMLResponse("<p class=\"reader-placeholder\">No such message.</p>", 404)
            state = _state(conn)
        return TEMPLATES.TemplateResponse(
            request,
            "reader.html",
            {
                "i": _decorate(row),
                "llm_ready": state["llm_ready"],
                # Rendered here rather than in the template: it is escaping
                # work on hostile input, which belongs in tested code and not
                # in a filter chain.
                "body_html": bodyhtml.render_body(row["body_text"]),
            },
        )

    @app.get("/api/messages/{pk}/body")
    def api_message_body(pk: int):
        """The stored body on its own, as JSON.

        The console no longer calls this — the reader fragment above arrives
        with the body already in it, in one round trip instead of two. Kept
        because it is the one way to read a stored body without asking for a
        page of markup around it.
        """
        with db.session() as conn:
            body = db.get_message_body(conn, pk)
        if body is None:
            return _err("No such message.", 404)
        return {"ok": True, "body": body}

    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(request: Request):
        page = frontend_page()
        if page:
            return page
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
        page = frontend_page()
        if page:
            return page
        state = _state()
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            {
                "page": "settings",
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
            "completion_id": _job["completion_id"],
            "auto": cfg.watch.auto_check,
            "interval_minutes": cfg.watch.interval_minutes,
            # Seconds remaining, so the browser never has to trust its own clock
            # agreeing with the server's.
            "next_in": max(0, int(due - time.time())) if due else None,
            "stage_elapsed": (
                max(0, int(time.time() - _job["stage_started"]))
                if _job["running"] and _job.get("stage_started") else None
            ),
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

        with _outlook_lock:
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
            with _outlook_lock:
                if _outlook.get("active_id") != flow_id:
                    outlook_auth.delete_cache(label)
                    return
                with db.session() as conn:
                    if not db.get_account(conn, label):
                        db.add_account(
                            conn, label=label, email=email or label, provider="outlook"
                        )
                _outlook.update(
                    pending=None, state="connected", email=email,
                    message=f"Connected {email}.",
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
        with _outlook_lock:
            if _outlook["state"] == "connected":
                # The sign-in finished first; the account exists and is kept.
                return {"ok": True, "message": "Already connected."}
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
        for field in (
            "base_url", "model", "batch_size", "max_body_chars", "concurrency",
            "num_ctx", "think", "keep_alive", "timeout_seconds",
            "classification_deadline_seconds",
        ):
            value = getattr(body, field)
            if value is not None:
                data["llm"][field] = value
        if body.lookback_days is not None:
            data["check"]["lookback_days"] = body.lookback_days
        if body.retain_days is not None:
            data["check"]["retain_days"] = body.retain_days
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
        _reschedule(cfg)  # a changed interval takes effect immediately
        return {"ok": True, "message": "Settings saved."}

    @app.post("/api/settings/test")
    def api_settings_test():
        from ..llm import LLMClient, LLMError

        cfg = cfgmod.load()
        if not cfg.is_llm_ready():
            return _err("Set a base URL and model first.")
        try:
            with LLMClient.from_config(cfg.llm) as client:
                reply = client.ping()
        except LLMError as exc:
            return _err(str(exc))
        return {"ok": True, "message": f"Model replied: {reply.strip()[:120]}"}

    @app.post("/api/rules")
    def api_add_rule(body: RuleBody):
        if body.category not in {c.name for c in CATEGORIES} or body.category == UNCLASSIFIED:
            return _err("Choose one of the listed categories.")
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

    @app.get("/{path:path}", include_in_schema=False)
    def frontend_route(path: str):
        if path == "api" or path.startswith("api/"):
            return _err("Not found.", 404)
        # Let the SPA own deep links only after it has been built. The legacy
        # template UI remains available in editable Python installs until then.
        if FRONTEND_DIST.is_dir() and (FRONTEND_DIST / "index.html").is_file():
            index = (FRONTEND_DIST / "index.html").resolve()
            requested = (FRONTEND_DIST / path).resolve()
            if requested.is_relative_to(FRONTEND_DIST.resolve()) and requested.is_file():
                return FileResponse(requested)
            return FileResponse(index, headers={"Cache-Control": "no-cache, must-revalidate"})
        return JSONResponse({"ok": False, "error": "Not found."}, status_code=404)

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
    # Stored dates are UTC — ``date_utc`` as a naive value, ``fetched_at`` with an
    # offset. Convert to local time before comparing against the local clock,
    # or "today" and "Yesterday" flip at the wrong hour and times read as UTC.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone().replace(tzinfo=None)
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


def _decorate(row) -> dict:
    """Add the display-only fields the templates read.

    Shared by the list and the reader so a date, a deadline or an open link is
    formatted once, by one piece of code, however it reaches the page.
    """
    item = dict(row)
    item["tier"] = tier_of(row["category"])
    item["category_label"] = label_of(row["category"])
    item["date_estimated"] = not bool(row["date_utc"])
    item["date_display"] = _fmt_date(row["date_utc"] or item.get("fetched_at"))
    item["deadline_display"] = _fmt_deadline(row["deadline"])
    item["open_url"], item["open_label"] = _open_link(row)
    return item


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
        # /u/0/ is whichever Google account is signed in first, which is not
        # necessarily this mailbox; authuser names the account explicitly.
        email = (row["account_email"] if "account_email" in keys else "") or ""
        account = f"?authuser={quote(email, safe='')}" if email else ""
        return (
            f"https://mail.google.com/mail/{account}#search/rfc822msgid:"
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
