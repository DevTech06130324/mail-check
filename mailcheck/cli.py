"""mail-check CLI."""

from __future__ import annotations

import json
import re
import shutil
import sys
import tomllib
import uuid

import typer
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import config as cfgmod
from . import db, report, secrets
from .llm import LLMClient, LLMError
from .pipeline import check_once
from .report import console
from .sources import SourceError
from .sources.presets import BY_KEY, PRESETS, guess_from_email
from .watch import watch as run_watch

app = typer.Typer(
    help="Triage unread job-application email.",
    no_args_is_help=True,
    add_completion=False,
)
account_app = typer.Typer(help="Manage mail accounts.", no_args_is_help=True)
app.add_typer(account_app, name="account")

PRIVACY_NOTICE = (
    "mail-check sends the sender, subject and the first part of each email body to "
    "the Ollama server you configure on your local network.\n\n"
    "Email content leaves this computer for that server. Mailbox connections still "
    "use your email provider.\n\n"
    "Your mail is never modified: mailboxes are opened read-only and nothing is ever "
    "marked as read."
)

_DURATION = re.compile(r"^(\d+)\s*([dwm]?)$", re.I)


def _parse_since(value: str | None) -> int | None:
    if not value:
        return None
    m = _DURATION.match(value.strip())
    if not m:
        raise typer.BadParameter("Use a form like 7, 7d, 2w or 1m.")
    n, unit = int(m.group(1)), m.group(2).lower()
    return {"": n, "d": n, "w": n * 7, "m": n * 30}[unit]


def _load_cfg() -> cfgmod.Config:
    try:
        return cfgmod.load()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Config at {cfgmod.config_path()} is invalid:[/red] {exc}")
        raise typer.Exit(1)


def _require_llm(cfg: cfgmod.Config) -> None:
    if not cfg.is_llm_ready():
        console.print("[red]LLM is not configured.[/red] Run: [bold]mail-check init[/bold]")
        raise typer.Exit(1)


# --------------------------------------------------------------------------- init


@app.command()
def init(
    base_url: str = typer.Option(None, help="Ollama server root, e.g. http://192.168.2.230:11440"),
    test: bool = typer.Option(True, help="Send a test request after saving."),
) -> None:
    """Configure the native Ollama endpoint.

    There is no model to choose: each check uses whichever model the server has loaded.
    """
    # Apply explicit migration arguments before validation: old /v1 configs
    # cannot otherwise be loaded to replace their endpoint.
    path = cfgmod.config_path()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        data = {}
    if base_url is not None:
        data.setdefault("llm", {})["base_url"] = base_url
    try:
        cfg = cfgmod.Config.model_validate(data)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    if not cfg.privacy_ack:
        console.print(Panel(PRIVACY_NOTICE, title="Before you start", border_style="yellow"))
        if not Confirm.ask("Continue?", default=True):
            raise typer.Exit(1)
        cfg.privacy_ack = True

    cfg.llm.base_url = base_url or Prompt.ask(
        "Ollama server root", default=cfg.llm.base_url or None
    )
    # Revalidate values assigned after loading so init cannot save a /v1 URL.
    try:
        cfg = cfgmod.Config.model_validate(cfg.model_dump())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    if path.exists():
        backup = path.with_name(f"config.toml.{uuid.uuid4().hex[:12]}.bak")
        shutil.copy2(path, backup)
        console.print(f"[dim]Previous configuration backed up to {backup}[/dim]")
    path = cfgmod.save(cfg)
    console.print(f"[green]Saved[/green] {path}")
    console.print(f"[dim]Database: {cfgmod.db_path()}[/dim]")

    if test:
        _ping(cfg)


def _ping(cfg: cfgmod.Config) -> None:
    console.print("Testing endpoint...")
    try:
        with db.session() as conn:
            fallback = db.last_llm_model(conn)
        with LLMClient.from_config(cfg.llm, fallback_model=fallback) as client:
            reply = client.ping()
        console.print(f"[green]OK[/green] - {client.model} replied: [dim]{reply.strip()[:120]}[/dim]")
    except LLMError as exc:
        console.print(f"[red]Endpoint test failed:[/red] {exc}")
        console.print("[dim]Fix with: mail-check init, or mail-check config set llm.base_url ...[/dim]")
        raise typer.Exit(1)


# ------------------------------------------------------------------------ account


@account_app.command("add")
def account_add(
    label: str = typer.Option(None, help="Short name for this account."),
    email: str = typer.Option(None, help="Email address."),
    host: str = typer.Option(None, help="IMAP host (skips the preset prompt)."),
    port: int = typer.Option(993, help="IMAP port."),
    no_ssl: bool = typer.Option(False, "--no-ssl", help="Disable SSL (not recommended)."),
    folder: str = typer.Option("INBOX", help="Folder to read."),
) -> None:
    """Add a mail account. The password is stored in the OS keyring."""
    email = email or Prompt.ask("Email address")
    if not label:
        console.print(
            "[dim]A label is just a short nickname for this mailbox. You use it in "
            "commands\n(mail-check check -a <label>) and it appears in the report's "
            "Acct column.[/dim]"
        )
        label = Prompt.ask("Label", default=_suggest_label(email))

    if not host:
        preset = guess_from_email(email)
        if preset:
            console.print(f"Detected [bold]{preset.name}[/bold]")
        else:
            table = Table("key", "provider", "host", show_header=True, header_style="dim")
            for p in PRESETS:
                mark = "" if p.supported else " [red](unsupported)[/red]"
                table.add_row(p.key, p.name + mark, p.host or "-")
            console.print(table)
            key = Prompt.ask("Provider", choices=[p.key for p in PRESETS], default="custom")
            preset = BY_KEY[key]

        if not preset.supported:
            console.print(Panel(preset.note, title=f"{preset.name}", border_style="red"))
            raise typer.Exit(1)
        if preset.note:
            console.print(Panel(preset.note, title="Password setup", border_style="yellow"))

        host = preset.host or Prompt.ask("IMAP host")
        port = preset.port
        no_ssl = not preset.use_ssl

    password = Prompt.ask("App password", password=True)

    with db.session() as conn:
        if db.get_account(conn, label):
            console.print(f"[red]An account labelled '{label}' already exists.[/red]")
            raise typer.Exit(1)
        try:
            secrets.set_account_password(label, password)
        except secrets.SecretError as exc:
            console.print(Panel(str(exc), title="Unusable password", border_style="red"))
            raise typer.Exit(1)
        try:
            source_ok = _test_account(host, port, not no_ssl, email, label, folder)
        except Exception:
            secrets.delete_account_password(label)
            raise
        if not source_ok:
            secrets.delete_account_password(label)
            raise typer.Exit(1)
        db.add_account(
            conn,
            label=label,
            email=email,
            imap_host=host,
            imap_port=port,
            use_ssl=not no_ssl,
            folder=folder,
        )
    console.print(f"[green]Added[/green] {label} ({email})")


def _suggest_label(email: str) -> str:
    """Prefer the provider name over the local part: shorter to type, and it is
    what you actually think of the mailbox as."""
    domain = email.split("@")[-1].lower()
    preset = guess_from_email(email)
    if preset and preset.key != "custom":
        return preset.key
    return domain.split(".")[0] or email.split("@")[0]


def _test_account(host, port, use_ssl, email, label, folder) -> bool:
    from .sources import IMAPSource

    console.print("Testing connection...")
    try:
        IMAPSource(
            host=host,
            port=port,
            username=email,
            password=secrets.get_account_password(label),
            use_ssl=use_ssl,
            folder=folder,
        ).test()
    except SourceError as exc:
        console.print(Panel(str(exc), title="Connection failed", border_style="red"))
        return False
    console.print("[green]Connection OK[/green]")
    return True


@account_app.command("list")
def account_list() -> None:
    """List configured accounts."""
    with db.session() as conn:
        accounts = db.list_accounts(conn)
    if not accounts:
        console.print("[dim]No accounts yet. Add one with: mail-check account add[/dim]")
        return
    table = Table("label", "email", "provider", "host", "folder", "enabled",
                  header_style="dim")
    for a in accounts:
        table.add_row(
            a.label,
            a.email,
            a.provider,
            f"{a.imap_host}:{a.imap_port}" if a.provider == "imap" else "-",
            a.folder if a.provider == "imap" else "-",
            "[green]yes[/green]" if a.enabled else "[red]no[/red]",
        )
    console.print(table)


@account_app.command("remove")
def account_remove(label: str) -> None:
    """Remove an account and its stored password."""
    with db.session() as conn:
        account = db.get_account(conn, label)
        if not account:
            console.print(f"[red]No account labelled '{label}'.[/red]")
            raise typer.Exit(1)
        if not Confirm.ask(
            f"Remove '{label}' ({account.email}) and its cached mail?", default=False
        ):
            raise typer.Exit()
        db.remove_account(conn, label)
    secrets.delete_account_password(label)
    from . import outlook_auth
    outlook_auth.delete_cache(label)  # a token must not outlive its account
    console.print(f"[green]Removed[/green] {label}")


@account_app.command("test")
def account_test(label: str) -> None:
    """Verify an account can log in."""
    with db.session() as conn:
        account = db.get_account(conn, label)
    if not account:
        console.print(f"[red]No account labelled '{label}'.[/red]")
        raise typer.Exit(1)
    if account.provider != "imap":
        from .pipeline import build_source
        try:
            build_source(account, _load_cfg()).test()
        except Exception as exc:  # noqa: BLE001 - message is what matters here
            console.print(Panel(str(exc), title="Connection failed", border_style="red"))
            raise typer.Exit(1)
        console.print("[green]Connection OK[/green]")
        raise typer.Exit(0)
    ok = _test_account(
        account.imap_host,
        account.imap_port,
        account.use_ssl,
        account.email,
        account.label,
        account.folder,
    )
    raise typer.Exit(0 if ok else 1)


@account_app.command("enable")
def account_enable(label: str, off: bool = typer.Option(False, "--off")) -> None:
    """Enable or disable an account without deleting it."""
    with db.session() as conn:
        if not db.set_account_enabled(conn, label, not off):
            console.print(f"[red]No account labelled '{label}'.[/red]")
            raise typer.Exit(1)
    console.print(f"[green]{'Disabled' if off else 'Enabled'}[/green] {label}")


# -------------------------------------------------------------------------- check


@app.command()
def check(
    account: str = typer.Option(None, "--account", "-a", help="Only this account."),
    since: str = typer.Option(None, "--since", help="Lookback window, e.g. 7d, 2w."),
    all_: bool = typer.Option(False, "--all", help="Include noise categories."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Re-classify everything."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Fetch unread mail, classify it, and print the triage report."""
    cfg = _load_cfg()
    _require_llm(cfg)

    status = (lambda m: None) if as_json else (lambda m: console.print(f"[dim]{m}[/dim]"))
    with db.session() as conn:
        result = check_once(
            conn,
            cfg,
            account_label=account,
            since_days=_parse_since(since),
            use_cache=not no_cache,
            status=status,
        )

    if as_json:
        print(json.dumps(_as_dict(result), indent=2, default=str))
    else:
        report.render(result, show_noise=all_)

    if result.errors and result.fetched == 0:
        raise typer.Exit(1)


def _as_dict(result) -> dict:
    return {
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "accounts_checked": result.accounts_checked,
        "fetched": result.fetched,
        "classified": result.classified,
        "from_cache": result.from_cache,
        "prefiltered": result.prefiltered,
        "errors": result.errors,
        "items": [
            {
                "account": i.message.account_label,
                "date": i.message.date_utc,
                "from": i.message.from_addr,
                "subject": i.message.subject,
                "category": i.classification.category,
                "confidence": i.classification.confidence,
                "company": i.classification.company,
                "role": i.classification.role,
                "deadline": i.classification.deadline,
                "action_required": i.classification.action_required,
                "summary": i.classification.summary,
            }
            for i in result.items
        ],
    }


@app.command()
def watch(
    interval: int = typer.Option(None, "--interval", "-i", help="Minutes between checks."),
    account: str = typer.Option(None, "--account", "-a"),
    all_: bool = typer.Option(False, "--all", help="Include noise categories."),
) -> None:
    """Re-run the check on an interval, notifying on urgent mail."""
    cfg = _load_cfg()
    _require_llm(cfg)
    try:
        run_watch(cfg, interval_minutes=interval, account_label=account, show_noise=all_)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")


@app.command()
def web(
    port: int = typer.Option(8765, help="Port to bind on localhost."),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
) -> None:
    """Serve the local dashboard on 127.0.0.1."""
    _load_cfg()
    try:
        import uvicorn
    except ImportError:
        console.print("[red]uvicorn is not installed.[/red] pip install uvicorn fastapi jinja2")
        raise typer.Exit(1)

    from .web.app import create_app

    url = f"http://127.0.0.1:{port}"
    console.print(f"Dashboard: [bold]{url}[/bold]  (Ctrl+C to stop)")
    if open_browser:
        import webbrowser
        import threading

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")


# ------------------------------------------------------------------------- config


config_app = typer.Typer(help="Inspect and edit configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show() -> None:
    """Print the current config (secrets are never shown)."""
    cfg = _load_cfg()
    console.print(f"[dim]{cfgmod.config_path()}[/dim]")
    console.print_json(cfg.model_dump_json(indent=2))



@config_app.command("set")
def config_set(key: str, value: str) -> None:
    """Set a dotted key, e.g. `config set llm.batch_size 5`."""
    cfg = _load_cfg()
    try:
        cfg = cfgmod.set_dotted(cfg, key, value)
    except KeyError:
        console.print(f"[red]Unknown key '{key}'.[/red] See: mail-check config show")
        raise typer.Exit(1)
    except Exception as exc:  # noqa: BLE001 - pydantic raises its own error type
        detail = getattr(exc, "errors", None)
        msg = detail()[0]["msg"] if callable(detail) and detail() else str(exc).split("\n")[0]
        console.print(f"[red]Invalid value for '{key}':[/red] {msg}")
        raise typer.Exit(1)
    cfgmod.save(cfg)
    console.print(f"[green]Set[/green] {key} = {value}")


@config_app.command("test")
def config_test() -> None:
    """Send a test request to the configured endpoint."""
    cfg = _load_cfg()
    _require_llm(cfg)
    _ping(cfg)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
