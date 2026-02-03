"""Tests for session_context module."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from graftpunk.session_context import (
    SESSION_FILE_NAME,
    clear_active_session,
    get_active_session,
    resolve_session,
    set_active_session,
)


class TestGetActiveSession:
    """Tests for get_active_session."""

    def test_returns_none_when_no_env_no_file(self, tmp_path: Path) -> None:
        """Return None when neither env var nor file is set."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_active_session(search_dir=tmp_path)
        assert result is None

    def test_env_var_takes_priority(self, tmp_path: Path) -> None:
        """GRAFTPUNK_SESSION env var wins over .gp-session file."""
        (tmp_path / SESSION_FILE_NAME).write_text("from-file")
        with patch.dict(os.environ, {"GRAFTPUNK_SESSION": "from-env"}, clear=True):
            result = get_active_session(search_dir=tmp_path)
        assert result == "from-env"

    def test_reads_file_when_no_env(self, tmp_path: Path) -> None:
        """Read .gp-session file when env var is not set."""
        (tmp_path / SESSION_FILE_NAME).write_text("my-session")
        with patch.dict(os.environ, {}, clear=True):
            result = get_active_session(search_dir=tmp_path)
        assert result == "my-session"

    def test_strips_whitespace(self, tmp_path: Path) -> None:
        """Strip whitespace from both env var and file content."""
        with patch.dict(os.environ, {"GRAFTPUNK_SESSION": "  padded  "}, clear=True):
            result = get_active_session(search_dir=tmp_path)
        assert result == "padded"

    def test_strips_whitespace_from_file(self, tmp_path: Path) -> None:
        """Strip whitespace from file content."""
        (tmp_path / SESSION_FILE_NAME).write_text("  padded-file  \n")
        with patch.dict(os.environ, {}, clear=True):
            result = get_active_session(search_dir=tmp_path)
        assert result == "padded-file"

    def test_returns_none_for_empty_file(self, tmp_path: Path) -> None:
        """Return None when .gp-session file exists but is empty."""
        (tmp_path / SESSION_FILE_NAME).write_text("")
        with patch.dict(os.environ, {}, clear=True):
            result = get_active_session(search_dir=tmp_path)
        assert result is None

    def test_returns_none_for_whitespace_only_file(self, tmp_path: Path) -> None:
        """Return None when .gp-session file contains only whitespace."""
        (tmp_path / SESSION_FILE_NAME).write_text("   \n  ")
        with patch.dict(os.environ, {}, clear=True):
            result = get_active_session(search_dir=tmp_path)
        assert result is None


class TestSetActiveSession:
    """Tests for set_active_session."""

    def test_writes_file(self, tmp_path: Path) -> None:
        """Write session name to .gp-session file."""
        result = set_active_session("my-session", directory=tmp_path)
        assert result == tmp_path / SESSION_FILE_NAME
        assert result.read_text() == "my-session"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        """Overwrite existing .gp-session file."""
        (tmp_path / SESSION_FILE_NAME).write_text("old-session")
        set_active_session("new-session", directory=tmp_path)
        assert (tmp_path / SESSION_FILE_NAME).read_text() == "new-session"


class TestClearActiveSession:
    """Tests for clear_active_session."""

    def test_removes_file(self, tmp_path: Path) -> None:
        """Remove .gp-session file."""
        session_file = tmp_path / SESSION_FILE_NAME
        session_file.write_text("my-session")
        clear_active_session(directory=tmp_path)
        assert not session_file.exists()

    def test_noop_when_no_file(self, tmp_path: Path) -> None:
        """No error when .gp-session file does not exist."""
        clear_active_session(directory=tmp_path)  # Should not raise


class TestResolveSession:
    """Tests for resolve_session."""

    def test_explicit_wins(self, tmp_path: Path) -> None:
        """Explicit value takes priority over active session."""
        (tmp_path / SESSION_FILE_NAME).write_text("from-file")
        with patch.dict(os.environ, {"GRAFTPUNK_SESSION": "from-env"}, clear=True):
            result = resolve_session("explicit-value", search_dir=tmp_path)
        assert result == "explicit-value"

    def test_falls_back_to_active(self, tmp_path: Path) -> None:
        """Fall back to active session when no explicit value."""
        (tmp_path / SESSION_FILE_NAME).write_text("from-file")
        with patch.dict(os.environ, {}, clear=True):
            result = resolve_session(None, search_dir=tmp_path)
        assert result == "from-file"

    def test_returns_none_when_nothing(self, tmp_path: Path) -> None:
        """Return None when no explicit value and no active session."""
        with patch.dict(os.environ, {}, clear=True):
            result = resolve_session(None, search_dir=tmp_path)
        assert result is None
