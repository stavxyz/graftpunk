"""Observability data storage."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from graftpunk.logging import get_logger

LOG = get_logger(__name__)

_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


class ObserveStorage:
    """File-based storage for observability data.

    Organizes data into a run directory structure:
        base_dir/session_name/run_id/
            screenshots/
            events.jsonl
            network.har
            console.jsonl
            metadata.json
    """

    def __init__(self, base_dir: Path, session_name: str, run_id: str) -> None:
        if not session_name or not _SAFE_NAME_RE.match(session_name):
            raise ValueError(f"Invalid session_name: {session_name!r}")
        if not run_id or not _SAFE_NAME_RE.match(run_id):
            raise ValueError(f"Invalid run_id: {run_id!r}")
        self._run_dir = base_dir / session_name / run_id
        self._screenshots_dir = self._run_dir / "screenshots"
        self._screenshots_dir.mkdir(parents=True, exist_ok=True)
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
        path = self._screenshots_dir / filename
        path.write_bytes(png_data)
        return path

    def write_event(self, event: str, data: dict[str, Any]) -> None:
        """Append a structured event to the JSONL event log.

        Args:
            event: Event name/type.
            data: Key-value data for the event.
        """
        entry = {"event": event, "timestamp": time.time(), **data}
        with self._events_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

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

        Args:
            logs: List of console log entry dicts.
        """
        with self._console_path.open("a") as f:
            for entry in logs:
                f.write(json.dumps(entry) + "\n")

    def write_har(self, entries: list[dict[str, Any]]) -> None:
        """Write HAR (HTTP Archive) data to the network.har file.

        Args:
            entries: List of HAR entry dicts.
        """
        har = {
            "log": {
                "version": "1.2",
                "creator": {"name": "graftpunk", "version": "0.1.0"},
                "entries": entries,
            }
        }
        self._har_path.write_text(json.dumps(har, indent=2))

    def write_metadata(self, metadata: dict[str, Any]) -> None:
        """Write session metadata to metadata.json.

        Args:
            metadata: Arbitrary metadata dict.
        """
        self._metadata_path.write_text(json.dumps(metadata, indent=2))
