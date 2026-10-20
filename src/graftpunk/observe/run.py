"""Observe run lifecycle helpers shared by the CLI, BrowserSession and the login engine."""

from __future__ import annotations

import base64
import datetime
import json
import os
import urllib.parse
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from graftpunk.logging import get_logger
from graftpunk.observe.storage import ObserveStorage

LOG = get_logger(__name__)

REDACTED = "***REDACTED***"
# Secrets shorter than this are not redacted: replacing every "ab" in a HAR
# would mangle it beyond use, and such values are not meaningfully secret.
_MIN_REDACT_LEN = 3


def make_run_id() -> str:
    """Return a timestamp+pid run id (unique per process per second)."""
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"


def _secret_forms(secret: str) -> set[str]:
    """Every encoding of ``secret`` that can appear on the wire in a login flow."""
    return {
        secret,
        urllib.parse.quote_plus(secret),
        urllib.parse.quote(secret, safe=""),
        json.dumps(secret)[1:-1],
        base64.b64encode(secret.encode()).decode(),
    }


def redact_har_entries(
    entries: list[dict[str, Any]], secrets: Iterable[str]
) -> list[dict[str, Any]]:
    """Return ``entries`` with every occurrence of ``secrets`` replaced.

    A login run's HAR carries the credentials verbatim: in the form POST body,
    possibly in a query string, in ``Authorization`` headers, and echoed by
    some sites into response bodies. Replace each secret -- raw, URL-encoded,
    JSON-escaped and base64 -- in every string of every entry, longest forms
    first so a partial encoding does not leave a fragment behind.

    Args:
        entries: HAR 1.2 entry dicts (not mutated).
        secrets: Values to remove; empty and very short values are skipped.
    """
    ordered = _redaction_forms(secrets)
    if not ordered:
        return entries

    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            return _scrub_text(value, ordered)
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value

    return scrub(entries)


def _redaction_forms(secrets: Iterable[str]) -> list[str]:
    """Every wire form of each secret long enough to redact, longest first."""
    forms: set[str] = set()
    for secret in secrets:
        if secret and len(secret) >= _MIN_REDACT_LEN:
            forms |= _secret_forms(secret)
    return sorted(forms, key=len, reverse=True)


def _scrub_text(value: str, ordered: list[str]) -> str:
    for form in ordered:
        if form in value:
            value = value.replace(form, REDACTED)
    return value


def redact_request_body_files(
    entries: list[dict[str, Any]], secrets: Iterable[str], run_dir: Path
) -> None:
    """Scrub *secrets* from the request bodies the recorder wrote to ``bodies/``.

    A request body over the HAR's cap, or one that is not text, is written to
    the run's ``bodies/`` and named by ``request._postDataFile`` instead of
    sitting in ``postData``, where :func:`redact_har_entries` would scrub it. A
    login POST large enough to land there carries the password just the same.
    A file that decodes as UTF-8 is scrubbed with the same forms and rewritten,
    and the entry's ``_postDataSize`` updated (*entries* is mutated). A binary
    file is left as recorded and logged, since a secret's text forms cannot be
    replaced reliably inside arbitrary bytes. Response bodies in ``bodies/`` are
    not scanned here.
    """
    ordered = _redaction_forms(secrets)
    if not ordered:
        return
    root = run_dir.resolve()
    for entry in entries:
        request = entry.get("request") if isinstance(entry, dict) else None
        if not isinstance(request, dict):
            continue
        ref = request.get("_postDataFile")
        if not isinstance(ref, str):
            continue
        path = (root / ref).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            continue
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            LOG.warning("request_body_file_not_scrubbed", file=ref, reason="binary")
            continue
        scrubbed = _scrub_text(text, ordered)
        if scrubbed != text:
            data = scrubbed.encode("utf-8")
            path.write_bytes(data)
            request["_postDataSize"] = len(data)


async def save_observe_run(
    storage: ObserveStorage,
    backend: Any,
    screenshot_label: str,
    *,
    console: Any | None = None,
    redact: Iterable[str] = (),
) -> None:
    """Persist a nodriver observe run: screenshot, page source, HAR, console logs.

    ``backend.stop_capture_async()`` runs after the screenshot/page-source
    grab so the late body fetch happens while the browser is still alive.
    Each write is guarded on its own so one failure (a full disk, an
    unserialisable body) cannot take the others down or mask the exception
    the caller may be unwinding. Progress lines go to ``console`` (a rich
    Console) when given; the log always carries them.

    Args:
        storage: Run directory owner.
        backend: An async-capable capture backend (nodriver).
        screenshot_label: Label baked into the screenshot filename.
        console: Optional rich console for user-facing progress.
        redact: Secret values (e.g. the submitted credentials) to scrub from
            the HAR before it is written, and from the text request bodies
            written to ``bodies/`` (:func:`redact_request_body_files`). Binary
            request bodies and response bodies on disk are not scanned.
    """

    def say(msg: str) -> None:
        if console is not None:
            console.print(msg)

    def step(name: str, fn: Any) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — each write is best-effort
            LOG.error(
                "observe_run_write_failed", step=name, error=str(exc), exc_type=type(exc).__name__
            )
            say(f"[red]{name} failed:[/red] {exc}")
            return None

    screenshot_data = await backend.take_screenshot()
    page_source = await backend.get_page_source()

    if screenshot_data is None and page_source is None:
        say("[yellow]Browser disconnected — screenshot and page source unavailable[/yellow]")
    else:
        if screenshot_data:
            path = step(
                "screenshot", lambda: storage.save_screenshot(1, screenshot_label, screenshot_data)
            )
            if path is not None:
                say(f"[green]Screenshot saved:[/green] {path}")
        else:
            say("[yellow]Screenshot capture failed[/yellow]")

        if page_source:
            source_path = storage.run_dir / "page-source.html"
            if (
                step("page source", lambda: source_path.write_text(page_source, encoding="utf-8"))
                is not None
            ):
                say(f"[green]Page source saved:[/green] {source_path}")
        else:
            say("[yellow]Page source capture failed[/yellow]")

    try:
        await backend.stop_capture_async()
    except Exception as exc:  # noqa: BLE001 — still write what was captured
        LOG.error("observe_stop_capture_failed", error=str(exc), exc_type=type(exc).__name__)

    redact = tuple(redact)
    har_entries = redact_har_entries(backend.get_har_entries(), redact)
    step(
        "request body redaction",
        lambda: redact_request_body_files(har_entries, redact, storage.run_dir),
    )
    if har_entries and step("HAR", lambda: storage.write_har(har_entries)) is not None:
        say(f"[green]HAR data saved:[/green] {len(har_entries)} entries")

    console_logs = backend.get_console_logs()
    if (
        console_logs
        and step("console logs", lambda: storage.write_console_logs(console_logs)) is not None
    ):
        say(f"[green]Console logs saved:[/green] {len(console_logs)} entries")

    LOG.info(
        "observe_run_saved",
        run_dir=str(storage.run_dir),
        har_entries=len(har_entries),
        console_logs=len(console_logs),
    )
    say(f"\n[bold]Observe run:[/bold] {storage.run_dir}")
