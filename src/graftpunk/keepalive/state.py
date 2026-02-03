"""Keepalive daemon state management.

This module provides types and functions for managing keepalive daemon state.
It is intentionally separated from CLI code to avoid circular imports.
"""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from graftpunk.config import get_settings


class DaemonStatus(StrEnum):
    """Status of the keepalive daemon.

    The daemon can be in one of two states:
    - WATCHING: No active session, periodically searching for new sessions
    - KEEPING_ALIVE: Actively maintaining a session
    """

    WATCHING = "watching"
    KEEPING_ALIVE = "keeping_alive"


@dataclass
class KeepaliveState:
    """State of a running keepalive daemon.

    This dataclass provides type safety for the state file written/read by
    the keepalive daemon. The state is persisted to disk so that --status
    can display daemon configuration.

    Attributes:
        watch: Whether the daemon is running in watch mode.
        no_switch: Whether session switching is disabled.
        max_switches: Maximum number of session switches allowed (0 = unlimited).
        switch_cooldown: Seconds to wait before switching sessions.
        watch_interval: Seconds between session discovery attempts in watch mode.
        interval: Minutes between touch operations (None if not yet determined).
        days: Number of days the daemon will run.
        current_session: Currently active session name (empty if searching).
        daemon_status: Current daemon status ("watching" or "keeping_alive").
    """

    watch: bool
    no_switch: bool
    max_switches: int
    switch_cooldown: int
    watch_interval: int
    interval: int | None
    days: int
    current_session: str = ""
    daemon_status: DaemonStatus = DaemonStatus.KEEPING_ALIVE

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KeepaliveState":
        """Create instance from dictionary.

        Args:
            data: Dictionary containing state fields.

        Returns:
            KeepaliveState instance with values from the dictionary.
            Unknown fields are ignored for forward compatibility.
        """
        # Only pass known fields to handle forward compatibility
        known_fields = {
            "watch",
            "no_switch",
            "max_switches",
            "switch_cooldown",
            "watch_interval",
            "interval",
            "days",
            "current_session",
            "daemon_status",
        }
        filtered = {k: v for k, v in data.items() if k in known_fields}
        # Convert daemon_status string to enum if present
        if "daemon_status" in filtered and isinstance(filtered["daemon_status"], str):
            filtered["daemon_status"] = DaemonStatus(filtered["daemon_status"])
        return cls(**filtered)

    def with_status(self, session: str, status: DaemonStatus) -> "KeepaliveState":
        """Return a new KeepaliveState with updated session and status.

        Uses dataclasses.replace() for immutable updates.

        Args:
            session: New current session name.
            status: New daemon status.

        Returns:
            New KeepaliveState instance with updated fields.
        """
        return replace(self, current_session=session, daemon_status=status)


def _get_keepalive_pid_path() -> Path:
    """Get path to keepalive PID file."""
    return get_settings().config_dir / "keepalive.pid"


def _get_keepalive_state_path() -> Path:
    """Get path to keepalive state file."""
    return get_settings().config_dir / "keepalive.state.json"


def _cleanup_stale_keepalive_files() -> None:
    """Clean up stale keepalive PID and state files.

    Called when the PID file exists but the process is dead. This is safe
    even if called concurrently from multiple processes because:
    - missing_ok=True handles race conditions on deletion
    - The files are only cleaned when the daemon process is confirmed dead
    """
    _get_keepalive_pid_path().unlink(missing_ok=True)
    _get_keepalive_state_path().unlink(missing_ok=True)


def read_keepalive_pid() -> int | None:
    """Read PID from keepalive PID file, return None if not found or invalid.

    Returns:
        PID if daemon is running, None otherwise.
    """
    pid_path = _get_keepalive_pid_path()
    if not pid_path.exists():
        return None
    try:
        pid = int(pid_path.read_text().strip())
        # Check if process is still running (sends no signal, just checks existence)
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        # PID file exists but process is dead - clean up
        _cleanup_stale_keepalive_files()
        return None


def write_keepalive_pid() -> None:
    """Write current PID to keepalive PID file."""
    pid_path = _get_keepalive_pid_path()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))


def write_keepalive_state(state: KeepaliveState | dict[str, Any]) -> None:
    """Write keepalive daemon state to state file atomically.

    Uses a temp file + rename pattern to prevent corruption if the process
    is interrupted during write.

    Args:
        state: KeepaliveState instance or dict containing daemon configuration.
    """
    state_path = _get_keepalive_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)

    # Convert dataclass to dict if needed
    state_dict = state.to_dict() if isinstance(state, KeepaliveState) else state

    # Atomic write: write to temp file, then rename
    # This prevents corruption if process is interrupted during write
    fd, temp_path = tempfile.mkstemp(
        dir=state_path.parent,
        prefix=".keepalive.state.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(state_dict))
        Path(temp_path).rename(state_path)
    except Exception:
        # Clean up temp file on failure
        Path(temp_path).unlink(missing_ok=True)
        raise


def read_keepalive_state() -> KeepaliveState | None:
    """Read keepalive daemon state from state file.

    Returns:
        KeepaliveState instance if file exists and is valid, None otherwise.
    """
    state_path = _get_keepalive_state_path()
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text())
        return KeepaliveState.from_dict(data)
    except (ValueError, json.JSONDecodeError, TypeError):
        return None


def remove_keepalive_pid() -> None:
    """Remove keepalive PID and state files."""
    _get_keepalive_pid_path().unlink(missing_ok=True)
    _get_keepalive_state_path().unlink(missing_ok=True)
