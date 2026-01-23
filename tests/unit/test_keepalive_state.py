"""Tests for keepalive state module."""

import pytest

from bsc.keepalive.state import (
    DaemonStatus,
    KeepaliveState,
    read_keepalive_state,
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
        monkeypatch.setenv("BSC_CONFIG_DIR", str(tmp_path))

        from bsc.config import reset_settings

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
        monkeypatch.setenv("BSC_CONFIG_DIR", str(tmp_path))

        from bsc.config import reset_settings

        reset_settings()

        result = read_keepalive_state()
        assert result is None

    def test_read_invalid_state_returns_none(self, tmp_path, monkeypatch):
        """Test reading invalid state file returns None."""
        monkeypatch.setenv("BSC_CONFIG_DIR", str(tmp_path))

        from bsc.config import reset_settings

        reset_settings()

        # Write invalid JSON
        state_path = tmp_path / "keepalive.state.json"
        state_path.write_text("not valid json")

        result = read_keepalive_state()
        assert result is None
