"""Tests for browser backend serialization and state management."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from graftpunk.backends.selenium import SeleniumBackend


class TestSeleniumBackendSerialization:
    """Tests for SeleniumBackend serialization."""

    def test_get_state_returns_backend_type(self) -> None:
        """get_state includes backend_type."""
        backend = SeleniumBackend(headless=True)
        state = backend.get_state()

        assert state["backend_type"] == "selenium"

    def test_get_state_includes_options(self) -> None:
        """get_state includes initialization options."""
        backend = SeleniumBackend(
            headless=False,
            use_stealth=False,
            default_timeout=30,
        )
        state = backend.get_state()

        assert state["headless"] is False
        assert state["use_stealth"] is False
        assert state["default_timeout"] == 30

    def test_get_state_includes_profile_dir(self, tmp_path: Path) -> None:
        """get_state includes profile_dir as string."""
        profile = tmp_path / "test"
        backend = SeleniumBackend(profile_dir=profile)
        state = backend.get_state()

        assert state["profile_dir"] == str(profile)

    def test_from_state_recreates_backend(self, tmp_path: Path) -> None:
        """from_state recreates backend with same options."""
        profile = tmp_path / "test"
        original = SeleniumBackend(
            headless=False,
            use_stealth=False,
            default_timeout=30,
            profile_dir=profile,
        )
        state = original.get_state()

        recreated = SeleniumBackend.from_state(state)

        assert recreated._headless is False
        assert recreated._use_stealth is False
        assert recreated._default_timeout == 30
        assert recreated._profile_dir == profile

    def test_from_state_with_defaults(self) -> None:
        """from_state uses defaults for missing keys."""
        state = {"backend_type": "selenium"}
        backend = SeleniumBackend.from_state(state)

        assert backend._headless is True
        assert backend._use_stealth is True
        assert backend._default_timeout == 15

    def test_roundtrip_serialization(self) -> None:
        """get_state/from_state roundtrip preserves options."""
        original = SeleniumBackend(
            headless=False,
            use_stealth=True,
            default_timeout=45,
        )

        state = original.get_state()
        recreated = SeleniumBackend.from_state(state)

        assert recreated._headless == original._headless
        assert recreated._use_stealth == original._use_stealth
        assert recreated._default_timeout == original._default_timeout


class TestSeleniumBackendFromStateMismatch:
    """Tests for from_state() with mismatched backend_type."""

    def test_from_state_ignores_backend_type_mismatch(self) -> None:
        """from_state ignores backend_type key - creates class it's called on."""
        state = {"backend_type": "nodriver", "headless": False, "use_stealth": False}
        backend = SeleniumBackend.from_state(state)

        # Should create SeleniumBackend regardless of backend_type in state
        assert isinstance(backend, SeleniumBackend)
        assert backend._headless is False

    def test_from_state_logs_when_using_defaults(self) -> None:
        """from_state gracefully handles missing keys with defaults."""
        state = {}  # Empty state
        backend = SeleniumBackend.from_state(state)

        assert backend._headless is True  # Default
        assert backend._use_stealth is True  # Default
        assert backend._default_timeout == 15  # Default

    def test_from_state_preserves_extra_options(self) -> None:
        """from_state preserves unknown keys as options."""
        state = {
            "backend_type": "selenium",
            "headless": True,
            "window_size": "1920,1080",
            "custom_option": "value",
        }
        backend = SeleniumBackend.from_state(state)

        assert backend._options.get("window_size") == "1920,1080"
        assert backend._options.get("custom_option") == "value"


class TestSeleniumBackendContextManager:
    """Tests for SeleniumBackend context manager behavior."""

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_context_manager(self, mock_create: MagicMock) -> None:
        """Backend works as context manager."""
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver

        with SeleniumBackend(use_stealth=True) as backend:
            assert backend.is_running is True

        mock_driver.quit.assert_called_once()

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_context_manager_propagates_exceptions(self, mock_create: MagicMock) -> None:
        """Context manager does not suppress exceptions from within the block."""
        mock_create.return_value = MagicMock()

        with pytest.raises(ValueError, match="test error"), SeleniumBackend(use_stealth=True):
            raise ValueError("test error")


class TestSeleniumBackendStartOverrides:
    """Tests for start() parameter overrides."""

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_start_updates_additional_options(self, mock_create: MagicMock) -> None:
        """start() merges additional options into _options."""
        mock_create.return_value = MagicMock()

        backend = SeleniumBackend(use_stealth=True, window_size="800,600")
        backend.start(custom_arg="custom_value")

        assert backend._options.get("custom_arg") == "custom_value"
        assert backend._options.get("window_size") == "800,600"  # Original preserved

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_start_overrides_headless(self, mock_create: MagicMock) -> None:
        """start() can override headless setting."""
        mock_create.return_value = MagicMock()

        backend = SeleniumBackend(headless=True, use_stealth=True)
        backend.start(headless=False)

        mock_create.assert_called_once_with(headless=False, profile_dir=None)

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_start_overrides_profile_dir(self, mock_create: MagicMock, tmp_path: Path) -> None:
        """start() can override profile_dir setting."""
        mock_create.return_value = MagicMock()
        profile = tmp_path / "override_profile"

        backend = SeleniumBackend(use_stealth=True)
        backend.start(profile_dir=profile)

        mock_create.assert_called_once_with(headless=True, profile_dir=profile)
