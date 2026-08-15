"""Poll loop over the same pipeline. Prints the report to the terminal each cycle."""

from __future__ import annotations

import time
from datetime import datetime

from . import db, pipeline, report
from .config import Config
from .report import console


def watch(
    cfg: Config,
    *,
    interval_minutes: int | None = None,
    account_label: str | None = None,
    show_noise: bool = False,
) -> None:
    interval = (interval_minutes or cfg.watch.interval_minutes) * 60
    console.print(
        f"[bold]Watching[/bold] every {interval // 60} min. Press Ctrl+C to stop.\n"
    )

    while True:
        stamp = datetime.now().strftime("%H:%M:%S")
        console.rule(f"[dim]{stamp}[/dim]")
        try:
            with db.session() as conn:
                result = pipeline.check_once(
                    conn,
                    cfg,
                    account_label=account_label,
                    status=lambda m: console.print(f"[dim]{m}[/dim]"),
                )
                report.render(result, show_noise=show_noise)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive a bad cycle
            console.print(f"[red]Cycle failed:[/red] {exc}")

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            console.print("\n[dim]Stopped.[/dim]")
            return
