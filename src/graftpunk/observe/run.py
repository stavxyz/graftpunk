"""Observe run lifecycle helpers shared by the CLI and the login engine."""

from __future__ import annotations

import datetime
import os
from typing import Any

from graftpunk.logging import get_logger
from graftpunk.observe.storage import ObserveStorage

LOG = get_logger(__name__)


def make_run_id() -> str:
    """Return a timestamp+pid run id (unique per process per second)."""
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"


async def save_observe_run(
    storage: ObserveStorage,
    backend: Any,
    screenshot_label: str,
    *,
    console: Any | None = None,
) -> None:
    """Persist a nodriver observe run: screenshot, page source, HAR, console logs.

    ``backend.stop_capture_async()`` runs after the screenshot/page-source
    grab so the late body fetch happens while the browser is still alive.
    Progress lines go to ``console`` (a rich Console) when given; otherwise
    only the log carries them.

    Args:
        storage: Run directory owner.
        backend: An async-capable capture backend (nodriver).
        screenshot_label: Label baked into the screenshot filename.
        console: Optional rich console for user-facing progress.
    """

    def say(msg: str) -> None:
        if console is not None:
            console.print(msg)

    screenshot_data = await backend.take_screenshot()
    page_source = await backend.get_page_source()

    if screenshot_data is None and page_source is None:
        say("[yellow]Browser disconnected — screenshot and page source unavailable[/yellow]")
    else:
        if screenshot_data:
            path = storage.save_screenshot(1, screenshot_label, screenshot_data)
            say(f"[green]Screenshot saved:[/green] {path}")
        else:
            say("[yellow]Screenshot capture failed[/yellow]")

        if page_source:
            source_path = storage.run_dir / "page-source.html"
            source_path.write_text(page_source, encoding="utf-8")
            say(f"[green]Page source saved:[/green] {source_path}")
        else:
            say("[yellow]Page source capture failed[/yellow]")

    await backend.stop_capture_async()

    har_entries = backend.get_har_entries()
    if har_entries:
        storage.write_har(har_entries)
        say(f"[green]HAR data saved:[/green] {len(har_entries)} entries")

    console_logs = backend.get_console_logs()
    if console_logs:
        storage.write_console_logs(console_logs)
        say(f"[green]Console logs saved:[/green] {len(console_logs)} entries")

    LOG.info(
        "observe_run_saved",
        run_dir=str(storage.run_dir),
        har_entries=len(har_entries),
        console_logs=len(console_logs),
    )
    say(f"\n[bold]Observe run:[/bold] {storage.run_dir}")
