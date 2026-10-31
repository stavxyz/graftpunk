"""Observability data storage."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from slugify import slugify

from graftpunk.logging import get_logger

LOG = get_logger(__name__)

_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def session_dirname(session_name: str) -> str:
    """Map a session name to its observe run-dir name.

    The single source of truth for that mapping: a session name like
    ``myshop@alice`` is not a safe directory component (``@`` fails
    :data:`_SAFE_NAME_RE`), so it is slugified before being handed to
    :class:`ObserveStorage` (e.g. ``myshop@alice`` -> ``myshop-alice``).
    EVERY writer (the login capture, ``gp http --observe``, ``gp observe
    go/interactive``, :class:`~graftpunk.session.BrowserSession`) and every
    lookup site (``gp observe list/show/clean``) must apply this exact same
    transformation, or runs are written where no reader looks (#151).
    """
    return slugify(session_name) or "default"


def run_dir_of(session_name: str, run_id: str, *, base_dir: Path) -> Path:
    """Where the run *run_id* of the session *session_name* lives under *base_dir*.

    The one lookup of a named run: ``gp observe``'s run resolver and the fixtures
    check's local value check (``graftpunk.testing.plugin``) both go through it,
    so a sidecar's ``observe_run`` finds the directory ``gp observe show`` would.
    It builds the path and touches nothing on disk.
    """
    return base_dir / session_dirname(session_name) / run_id


#: A run holds the session's cookies and headers in plaintext, so only its
#: owner may read it.
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def make_private_dir(path: Path) -> None:
    """Create *path* (one level) readable only by its owner, if it is missing.

    ``mkdir(mode=)`` is narrowed by the umask but never widened past it, so
    the mode is set again after creation. A directory that already exists
    keeps its mode: an older run's files stay as they were written.
    """
    try:
        path.mkdir(mode=PRIVATE_DIR_MODE)
    except FileExistsError:
        return
    os.chmod(path, PRIVATE_DIR_MODE)


def write_private_file(path: Path, data: bytes | str, *, append: bool = False) -> int:
    """Write (or append) *data* to *path*, creating it readable only by its owner.

    ``str`` data is written as UTF-8. A file created here is 0600 whatever
    the umask; an existing one is set to 0600 too, since every file this is
    used for belongs to a run gp is writing now.

    Returns:
        The number of bytes written.
    """
    raw = data.encode("utf-8", errors="surrogatepass") if isinstance(data, str) else data
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    fd = os.open(path, flags, PRIVATE_FILE_MODE)
    try:
        os.fchmod(fd, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "ab" if append else "wb") as handle:
            fd = -1
            handle.write(raw)
    finally:
        if fd != -1:
            os.close(fd)
    return len(raw)


class ObserveStorage:
    """File-based storage for observability data.

    Organizes data into a run directory structure:
        base_dir/session_name/run_id/
            screenshots/
            bodies/
            events.jsonl
            network.har
            console.jsonl
            metadata.json

    The run directory is created here; ``screenshots/`` and ``bodies/`` only
    when something is written to them. A session directory this creates, the
    run directory and its subdirectories are 0700, and every file in the run
    0600 (:func:`make_private_dir`, :func:`write_private_file`).
    """

    def __init__(self, base_dir: Path, session_name: str, run_id: str) -> None:
        if not session_name or not _SAFE_NAME_RE.match(session_name):
            raise ValueError(f"Invalid session_name: {session_name!r}")
        if not run_id or not _SAFE_NAME_RE.match(run_id):
            raise ValueError(f"Invalid run_id: {run_id!r}")
        self._run_dir = base_dir / session_name / run_id
        base_dir.mkdir(parents=True, exist_ok=True)
        make_private_dir(base_dir / session_name)
        make_private_dir(self._run_dir)
        self._screenshots_dir = self._run_dir / "screenshots"
        self._events_path = self._run_dir / "events.jsonl"
        self._har_path = self._run_dir / "network.har"
        self._console_path = self._run_dir / "console.jsonl"
        self._metadata_path = self._run_dir / "metadata.json"

    @property
    def run_dir(self) -> Path:
        """Return the root directory for this observability run."""
        return self._run_dir

    def save_screenshot(self, index: int, label: str, png_data: bytes) -> Path:
        """Save a screenshot to the screenshots directory.

        Args:
            index: Sequential screenshot number.
            label: Descriptive label for the filename. Path separators and
                unsafe characters are stripped.
            png_data: Raw PNG image bytes.

        Returns:
            Path to the saved screenshot file.
        """
        # Sanitize label to prevent path traversal
        safe_label = re.sub(r"[^a-zA-Z0-9._-]", "-", label)
        filename = f"{index:03d}-{safe_label}.png"
        make_private_dir(self._screenshots_dir)
        path = self._screenshots_dir / filename
        write_private_file(path, png_data)
        return path

    def remove_if_empty(self) -> bool:
        """Remove the run directory when nothing was written to it.

        Returns:
            True when it was removed.
        """
        try:
            self._run_dir.rmdir()
        except OSError:
            return False
        return True

    def write_event(self, event: str, data: dict[str, Any]) -> None:
        """Append a structured event to the JSONL event log.

        Args:
            event: Event name/type.
            data: Key-value data for the event.
        """
        entry = {"event": event, "timestamp": time.time(), **data}
        write_private_file(self._events_path, json.dumps(entry) + "\n", append=True)

    def read_events(self) -> list[dict[str, Any]]:
        """Read all events from the JSONL event log.

        Returns:
            List of event dicts, or empty list if no events exist.
        """
        if not self._events_path.exists():
            return []
        events = []
        for line in self._events_path.read_text().strip().split("\n"):
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    LOG.warning("corrupt_event_line_skipped", error=str(exc))
        return events

    def write_console_logs(self, logs: list[dict[str, Any]]) -> None:
        """Append console log entries to the console JSONL file.

        ``timestamp`` on each entry is seconds since epoch: both CDP's
        millisecond ``Runtime.Timestamp`` and Selenium browser-log entries are
        normalised at capture (issue #158).

        Args:
            logs: List of console log entry dicts.
        """
        write_private_file(
            self._console_path, "".join(json.dumps(entry) + "\n" for entry in logs), append=True
        )

    def write_har(self, entries: Iterable[dict[str, Any]]) -> int:
        """Write HAR (HTTP Archive) data to the network.har file.

        Entries are serialized one at a time, so an iterator that reads them
        from disk is never held in memory whole. They go to a temporary file in
        the run directory, renamed onto ``network.har`` only once the HAR is
        complete: a failure part way (an entry that cannot be read or
        serialized, a full disk, an interrupt) leaves any earlier
        ``network.har`` as it was and no partial one.

        Args:
            entries: HAR entry dicts.

        Returns:
            The number of entries written.
        """
        count = 0
        fd, temp_name = tempfile.mkstemp(dir=self._run_dir, prefix=".network.har.")
        try:
            os.fchmod(fd, PRIVATE_FILE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(
                    '{\n  "log": {\n    "version": "1.2",\n'
                    '    "creator": {"name": "graftpunk", "version": "0.1.0"},\n'
                    '    "entries": ['
                )
                for entry in entries:
                    handle.write(",\n" if count else "\n")
                    handle.write(json.dumps(entry, indent=2))
                    count += 1
                handle.write("\n    ]\n  }\n}\n")
            os.replace(temp_name, self._har_path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return count

    def write_metadata(self, metadata: dict[str, Any]) -> None:
        """Write session metadata to metadata.json.

        Args:
            metadata: Arbitrary metadata dict.
        """
        write_private_file(self._metadata_path, json.dumps(metadata, indent=2))
