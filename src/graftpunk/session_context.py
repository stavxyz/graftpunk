"""Active session context management.

Resolution order: GRAFTPUNK_SESSION env var > .gp-session file in cwd > None.
"""

from __future__ import annotations

import os
from pathlib import Path

SESSION_FILE_NAME = ".gp-session"


def get_active_session(search_dir: Path | None = None) -> str | None:
    """Read the active session from env var or .gp-session file.

    Resolution order:
        1. GRAFTPUNK_SESSION environment variable (per-shell override)
        2. .gp-session file in search_dir (or cwd if not specified)

    Args:
        search_dir: Directory to look for .gp-session file. Defaults to cwd.

    Returns:
        Session name, or None if not set.
    """
    env = os.environ.get("GRAFTPUNK_SESSION")
    if env:
        return env.strip()

    directory = search_dir or Path.cwd()
    session_file = directory / SESSION_FILE_NAME
    if session_file.is_file():
        content = session_file.read_text().strip()
        return content or None
    return None


def set_active_session(name: str, directory: Path | None = None) -> Path:
    """Write the active session to .gp-session file.

    Args:
        name: Session name to set as active.
        directory: Directory to write .gp-session file in. Defaults to cwd.

    Returns:
        Path to the written file.
    """
    target = (directory or Path.cwd()) / SESSION_FILE_NAME
    target.write_text(name)
    return target


def clear_active_session(directory: Path | None = None) -> None:
    """Remove the .gp-session file.

    Args:
        directory: Directory containing the .gp-session file. Defaults to cwd.
    """
    target = (directory or Path.cwd()) / SESSION_FILE_NAME
    target.unlink(missing_ok=True)


def resolve_session(explicit: str | None, search_dir: Path | None = None) -> str | None:
    """Resolve session name: explicit flag > active session > None.

    Args:
        explicit: Explicit --session flag value, or None.
        search_dir: Directory for .gp-session lookup. Defaults to cwd.

    Returns:
        Resolved session name, or None.
    """
    if explicit:
        return explicit
    return get_active_session(search_dir=search_dir)
