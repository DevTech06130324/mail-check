"""Terminal rendering: grouped by urgency tier, newest first."""

from __future__ import annotations

from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .models import RunResult, TriagedMessage
from .taxonomy import TIER_META, TIER_ORDER, label_of, tier_of

console = Console()


def _when(dt: datetime | None) -> str:
    return dt.strftime("%b %d") if dt else "-"


def _what(item: TriagedMessage) -> Text:
    c = item.classification
    if c.company and c.role:
        head = f"{c.company} - {c.role}"
    elif c.company:
        head = c.company
    elif c.role:
        head = c.role
    else:
        head = item.message.subject
    text = Text(head[:52], style="bold")
    if c.deadline:
        text.append(f"\ndue {c.deadline}", style="bold red")
    return text


def _summary(item: TriagedMessage) -> Text:
    c = item.classification
    text = Text(c.summary or item.message.snippet)
    if c.confidence and c.confidence < 0.5:
        text.append(f"  (low confidence {c.confidence:.0%})", style="dim italic")
    return text


def render(result: RunResult, *, show_noise: bool = False) -> None:
    if not result.items:
        console.print("[dim]No unread mail in the selected window.[/dim]")
        _render_footer(result)
        return

    groups: dict[str, list[TriagedMessage]] = {}
    for item in result.items:
        groups.setdefault(tier_of(item.classification.category), []).append(item)

    printed = False
    for tier in TIER_ORDER:
        items = groups.get(tier)
        if not items:
            continue
        if tier == "noise" and not show_noise:
            console.print(
                f"[dim]{len(items)} noise email(s) hidden - use --all to show them.[/dim]\n"
            )
            continue

        title, style = TIER_META[tier]
        table = Table(
            title=f"{title}  ({len(items)})",
            title_style=style,
            title_justify="left",
            header_style="dim",
            expand=True,
            show_lines=False,
            padding=(0, 1),
        )
        table.add_column("Date", width=6, no_wrap=True)
        table.add_column("Type", width=20, no_wrap=True)
        table.add_column("Company / Role", width=28)
        table.add_column("What they want", ratio=1, min_width=24)
        table.add_column("Acct", width=10, no_wrap=True)

        for item in items:
            table.add_row(
                _when(item.message.date_utc),
                Text(label_of(item.classification.category), style=style),
                _what(item),
                _summary(item),
                Text(item.message.account_label, style="dim"),
            )
        console.print(table)
        console.print()
        printed = True

    if not printed:
        console.print("[dim]Nothing needing attention.[/dim]\n")
    _render_footer(result)


def _render_footer(result: RunResult) -> None:
    took = ""
    if result.finished_at:
        secs = (result.finished_at - result.started_at).total_seconds()
        took = f" in {secs:.1f}s"

    bits = [
        f"[bold]{result.fetched}[/bold] unread",
        f"[bold]{result.classified}[/bold] classified",
        f"[bold]{result.from_cache}[/bold] cached",
    ]
    if result.prefiltered:
        bits.append(f"[bold]{result.prefiltered}[/bold] prefiltered")
    console.print(f"[dim]{' | '.join(bits)}  across {result.accounts_checked} account(s){took}[/dim]")

    if result.errors:
        body = "\n".join(f"- {e}" for e in result.errors[:10])
        if len(result.errors) > 10:
            body += f"\n- ...and {len(result.errors) - 10} more"
        console.print(
            Panel(body, title=f"{len(result.errors)} problem(s)", border_style="red")
        )


def render_urgent_line(result: RunResult) -> str:
    urgent = result.urgent
    if not urgent:
        return "No urgent mail."
    parts = []
    for item in urgent[:3]:
        c = item.classification
        who = c.company or item.message.from_name or item.message.from_addr
        parts.append(f"{label_of(c.category)}: {who}")
    extra = f" (+{len(urgent) - 3} more)" if len(urgent) > 3 else ""
    return "; ".join(parts) + extra
