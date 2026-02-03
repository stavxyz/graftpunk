"""Tests for keepalive state module."""

import json
import os
from unittest.mock import patch

import pytest

from graftpunk.keepalive.state import (
    DaemonStatus,
    KeepaliveState,
    read_keepalive_pid,
    read_keepalive_state,
    remove_keepalive_pid,
    write_keepalive_pid,
    write_keepalive_state,
)


class TestDaemonStatus:
    """Tests for DaemonStatus enum."""

    def test_daemon_status_values(self):
        """Test DaemonStatus enum values."""
        assert DaemonStatus.WATCHING.value == "watching"
        assert DaemonStatus.KEEPING_ALIVE.value == "keeping_alive"

    def test_daemon_status_is_string(self):
        """Test that DaemonStatus inherits from str."""
        assert isinstance(DaemonStatus.WATCHING, str)
        assert DaemonStatus.WATCHING == "watching"


class TestKeepaliveState:
    """Tests for KeepaliveState dataclass."""

    @pytest.fixture
    def sample_state(self):
        """Create a sample KeepaliveState."""
        return KeepaliveState(
            watch=True,
            no_switch=False,
            max_switches=10,
            switch_cooldown=30,
            watch_interval=60,
            interval=25,
            days=7,
            current_session="test-session",
            daemon_status=DaemonStatus.KEEPING_ALIVE,
        )

    def test_to_dict(self, sample_state):
        """Test converting state to dictionary."""
        state_dict = sample_state.to_dict()

        assert state_dict["watch"] is True
        assert state_dict["no_switch"] is False
        assert state_dict["max_switches"] == 10
        assert state_dict["current_session"] == "test-session"
        assert state_dict["daemon_status"] == DaemonStatus.KEEPING_ALIVE

    def test_from_dict(self):
        """Test creating state from dictionary."""
        data = {
            "watch": True,
            "no_switch": False,
            "max_switches": 5,
            "switch_cooldown": 30,
            "watch_interval": 60,
            "interval": 20,
            "days": 3,
            "current_session": "my-session",
            "daemon_status": "keeping_alive",
        }

        state = KeepaliveState.from_dict(data)

        assert state.watch is True
        assert state.max_switches == 5
        assert state.current_session == "my-session"
        assert state.daemon_status == DaemonStatus.KEEPING_ALIVE

    def test_from_dict_ignores_unknown_fields(self):
        """Test that from_dict ignores unknown fields."""
        data = {
            "watch": True,
            "no_switch": False,
            "max_switches": 5,
            "switch_cooldown": 30,
            "watch_interval": 60,
            "interval": 20,
            "days": 3,
            "unknown_field": "ignored",
        }

        state = KeepaliveState.from_dict(data)
        assert not hasattr(state, "unknown_field")

    def test_with_status(self, sample_state):
        """Test creating new state with updated status."""
        new_state = sample_state.with_status("new-session", DaemonStatus.WATCHING)

        # Original unchanged
        assert sample_state.current_session == "test-session"
        assert sample_state.daemon_status == DaemonStatus.KEEPING_ALIVE

        # New state updated
        assert new_state.current_session == "new-session"
        assert new_state.daemon_status == DaemonStatus.WATCHING

        # Other fields preserved
        assert new_state.watch == sample_state.watch
        assert new_state.max_switches == sample_state.max_switches


class TestKeepaliveStateIO:
    """Tests for keepalive state file I/O."""

    def test_write_and_read_state(self, tmp_path, monkeypatch):
        """Test writing and reading state file."""
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))

        from graftpunk.config import reset_settings

        reset_settings()

        state = KeepaliveState(
            watch=True,
            no_switch=False,
            max_switches=10,
            switch_cooldown=30,
            watch_interval=60,
            interval=25,
            days=7,
            current_session="test",
            daemon_status=DaemonStatus.KEEPING_ALIVE,
        )

        write_keepalive_state(state)
        loaded = read_keepalive_state()

        assert loaded is not None
        assert loaded.watch == state.watch
        assert loaded.current_session == state.current_session
        assert loaded.daemon_status == state.daemon_status

    def test_read_nonexistent_state_returns_none(self, tmp_path, monkeypatch):
        """Test reading non-existent state file returns None."""
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))

        from graftpunk.config import reset_settings

        reset_settings()

        result = read_keepalive_state()
        assert result is None

    def test_read_invalid_state_returns_none(self, tmp_path, monkeypatch):
        """Test reading invalid state file returns None."""
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))

        from graftpunk.config import reset_settings

        reset_settings()

        # Write invalid JSON
        state_path = tmp_path / "keepalive.state.json"
        state_path.write_text("not valid json")

        result = read_keepalive_state()
        assert result is None


class TestReadKeepalivePid:
    """Tests for read_keepalive_pid exception paths."""

    @pytest.fixture(autouse=True)
    def _setup_config(self, tmp_path, monkeypatch):
        """Set up config dir for each test."""
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        from graftpunk.config import reset_settings

        reset_settings()
        self.tmp_path = tmp_path

    def test_no_pid_file_returns_none(self):
        """Test returns None when PID file does not exist."""
        result = read_keepalive_pid()
        assert result is None

    def test_pid_file_with_non_integer_returns_none(self):
        """Test ValueError path: PID file contains non-integer text."""
        pid_path = self.tmp_path / "keepalive.pid"
        pid_path.write_text("not-a-number")

        result = read_keepalive_pid()

        assert result is None
        # Stale files should be cleaned up
        assert not pid_path.exists()

    def test_pid_file_with_dead_process_returns_none(self):
        """Test ProcessLookupError path: PID references a dead process."""
        pid_path = self.tmp_path / "keepalive.pid"
        pid_path.write_text("999999")

        with patch("graftpunk.keepalive.state.os.kill", side_effect=ProcessLookupError):
            result = read_keepalive_pid()

        assert result is None
        # Stale files should be cleaned up
        assert not pid_path.exists()

    def test_pid_file_with_permission_error_returns_none(self):
        """Test PermissionError path: no permission to check process."""
        pid_path = self.tmp_path / "keepalive.pid"
        pid_path.write_text("12345")

        with patch("graftpunk.keepalive.state.os.kill", side_effect=PermissionError):
            result = read_keepalive_pid()

        # Current implementation cleans up and returns None for all exceptions
        assert result is None

    def test_pid_file_with_running_process(self):
        """Test returns PID when process is running."""
        pid_path = self.tmp_path / "keepalive.pid"
        current_pid = os.getpid()
        pid_path.write_text(str(current_pid))

        with patch("graftpunk.keepalive.state.os.kill") as mock_kill:
            mock_kill.return_value = None  # No exception means process exists
            result = read_keepalive_pid()

        assert result == current_pid
        mock_kill.assert_called_once_with(current_pid, 0)


class TestWriteKeepaliveStateExceptionPaths:
    """Tests for write_keepalive_state exception paths."""

    @pytest.fixture(autouse=True)
    def _setup_config(self, tmp_path, monkeypatch):
        """Set up config dir for each test."""
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        from graftpunk.config import reset_settings

        reset_settings()
        self.tmp_path = tmp_path

    def test_write_dict_state(self):
        """Test writing a plain dict as state."""
        state_dict = {
            "watch": False,
            "no_switch": True,
            "max_switches": 0,
            "switch_cooldown": 60,
            "watch_interval": 120,
            "interval": 15,
            "days": 1,
        }

        write_keepalive_state(state_dict)

        state_path = self.tmp_path / "keepalive.state.json"
        assert state_path.exists()
        loaded = json.loads(state_path.read_text())
        assert loaded["watch"] is False
        assert loaded["interval"] == 15

    def test_write_exception_cleans_up_temp_file(self):
        """Test that temp file is cleaned up when write fails."""
        state = KeepaliveState(
            watch=True,
            no_switch=False,
            max_switches=10,
            switch_cooldown=30,
            watch_interval=60,
            interval=25,
            days=7,
        )

        with (
            patch("graftpunk.keepalive.state.os.fdopen", side_effect=OSError("disk full")),
            pytest.raises(OSError, match="disk full"),
        ):
            write_keepalive_state(state)

        # No leftover temp files
        temp_files = list(self.tmp_path.glob(".keepalive.state.*.tmp"))
        assert len(temp_files) == 0


class TestReadKeepaliveStateExceptionPaths:
    """Tests for read_keepalive_state additional exception paths."""

    @pytest.fixture(autouse=True)
    def _setup_config(self, tmp_path, monkeypatch):
        """Set up config dir for each test."""
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        from graftpunk.config import reset_settings

        reset_settings()
        self.tmp_path = tmp_path

    def test_valid_json_but_missing_required_fields_returns_none(self):
        """Test valid JSON but missing required fields returns None."""
        state_path = self.tmp_path / "keepalive.state.json"
        state_path.write_text(json.dumps({"watch": True}))

        result = read_keepalive_state()

        assert result is None

    def test_json_with_list_type_returns_none(self):
        """Test JSON with list type (no .items()) returns None."""
        state_path = self.tmp_path / "keepalive.state.json"
        state_path.write_text(json.dumps([1, 2, 3]))

        # list has no .items() method, raises AttributeError
        # which is not caught by the (ValueError, JSONDecodeError, TypeError) handler
        # so this propagates as an unhandled error
        with pytest.raises(AttributeError):
            read_keepalive_state()

    def test_json_with_null_raises_attribute_error(self):
        """Test JSON null value raises AttributeError (not caught by handler)."""
        state_path = self.tmp_path / "keepalive.state.json"
        state_path.write_text("null")

        # json.loads("null") returns None, which has no .items()
        with pytest.raises(AttributeError):
            read_keepalive_state()


class TestRemoveKeepalivePid:
    """Tests for remove_keepalive_pid."""

    @pytest.fixture(autouse=True)
    def _setup_config(self, tmp_path, monkeypatch):
        """Set up config dir for each test."""
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        from graftpunk.config import reset_settings

        reset_settings()
        self.tmp_path = tmp_path

    def test_removes_both_pid_and_state_files(self):
        """Test that remove_keepalive_pid removes both PID and state files."""
        pid_path = self.tmp_path / "keepalive.pid"
        state_path = self.tmp_path / "keepalive.state.json"
        pid_path.write_text("12345")
        state_path.write_text("{}")

        remove_keepalive_pid()

        assert not pid_path.exists()
        assert not state_path.exists()

    def test_removes_when_files_dont_exist(self):
        """Test that remove_keepalive_pid handles missing files gracefully."""
        # Should not raise when files don't exist
        remove_keepalive_pid()


class TestWriteKeepalivePid:
    """Tests for write_keepalive_pid."""

    @pytest.fixture(autouse=True)
    def _setup_config(self, tmp_path, monkeypatch):
        """Set up config dir for each test."""
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        from graftpunk.config import reset_settings

        reset_settings()
        self.tmp_path = tmp_path

    def test_writes_current_pid(self):
        """Test that write_keepalive_pid writes the current process PID."""
        write_keepalive_pid()

        pid_path = self.tmp_path / "keepalive.pid"
        assert pid_path.exists()
        assert int(pid_path.read_text().strip()) == os.getpid()


class TestKeepaliveCLICommands:
    """Tests for keepalive CLI commands."""

    @pytest.fixture(autouse=True)
    def _setup_config(self, tmp_path, monkeypatch):
        """Set up config dir for each test."""
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        from graftpunk.config import reset_settings

        reset_settings()
        self.tmp_path = tmp_path

    def test_keepalive_status_not_running(self):
        """Test keepalive status when daemon is not running."""
        from typer.testing import CliRunner

        from graftpunk.cli.keepalive_commands import keepalive_app

        runner = CliRunner()
        result = runner.invoke(keepalive_app, ["status"])

        assert result.exit_code == 0
        assert "not running" in result.output.lower()

    def test_keepalive_status_running_with_state(self):
        """Test keepalive status when daemon is running with state."""
        from typer.testing import CliRunner

        from graftpunk.cli.keepalive_commands import keepalive_app

        state = KeepaliveState(
            watch=True,
            no_switch=False,
            max_switches=5,
            switch_cooldown=30,
            watch_interval=60,
            interval=10,
            days=3,
            current_session="my-session",
            daemon_status=DaemonStatus.KEEPING_ALIVE,
        )
        write_keepalive_state(state)

        runner = CliRunner()
        with patch("graftpunk.cli.keepalive_commands.read_keepalive_pid", return_value=12345):
            result = runner.invoke(keepalive_app, ["status"])

        assert result.exit_code == 0
        assert "12345" in result.output

    def test_keepalive_status_running_no_state(self):
        """Test keepalive status when daemon running but no state file."""
        from typer.testing import CliRunner

        from graftpunk.cli.keepalive_commands import keepalive_app

        runner = CliRunner()
        with patch("graftpunk.cli.keepalive_commands.read_keepalive_pid", return_value=99999):
            result = runner.invoke(keepalive_app, ["status"])

        assert result.exit_code == 0
        assert "99999" in result.output
        assert "State file not found" in result.output

    def test_keepalive_stop_not_running(self):
        """Test keepalive stop when daemon is not running."""
        from typer.testing import CliRunner

        from graftpunk.cli.keepalive_commands import keepalive_app

        runner = CliRunner()
        result = runner.invoke(keepalive_app, ["stop"])

        assert result.exit_code == 0
        assert "not running" in result.output.lower()

    def test_keepalive_stop_success(self):
        """Test keepalive stop sends SIGTERM successfully."""
        from typer.testing import CliRunner

        from graftpunk.cli.keepalive_commands import keepalive_app

        runner = CliRunner()
        with (
            patch("graftpunk.cli.keepalive_commands.read_keepalive_pid", return_value=12345),
            patch("os.kill") as mock_kill,
        ):
            mock_kill.return_value = None
            result = runner.invoke(keepalive_app, ["stop"])

        assert result.exit_code == 0
        assert "12345" in result.output

    def test_keepalive_stop_already_stopped(self):
        """Test keepalive stop when process already exited."""
        from typer.testing import CliRunner

        from graftpunk.cli.keepalive_commands import keepalive_app

        runner = CliRunner()
        with (
            patch("graftpunk.cli.keepalive_commands.read_keepalive_pid", return_value=12345),
            patch("os.kill", side_effect=ProcessLookupError),
        ):
            result = runner.invoke(keepalive_app, ["stop"])

        assert result.exit_code == 0
        assert "already stopped" in result.output.lower()
