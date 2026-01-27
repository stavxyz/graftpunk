"""Tests for NoDriver browser backend."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from graftpunk.backends import get_backend, list_backends
from graftpunk.backends.nodriver import NoDriverBackend


class TestNoDriverBackendProtocol:
    """Tests for NoDriverBackend Protocol compliance."""

    def test_nodriver_backend_implements_protocol(self) -> None:
        """NoDriverBackend should satisfy BrowserBackend protocol.

        Note: We check the class for protocol methods/properties instead of
        using isinstance() or hasattr() on instance, because:
        1. isinstance(backend, BrowserBackend) can trigger nodriver imports
        2. hasattr(backend, "driver") accesses the property, which calls start()
        3. The nodriver library may have compatibility issues on some Python versions

        Checking the class directly avoids all these issues while still verifying
        that the required interface is implemented.
        """
        # Verify class has all required protocol methods/properties
        assert hasattr(NoDriverBackend, "start")
        assert hasattr(NoDriverBackend, "stop")
        assert hasattr(NoDriverBackend, "navigate")
        assert hasattr(NoDriverBackend, "is_running")
        assert hasattr(NoDriverBackend, "current_url")
        assert hasattr(NoDriverBackend, "page_title")
        assert hasattr(NoDriverBackend, "page_source")
        assert hasattr(NoDriverBackend, "driver")
        assert hasattr(NoDriverBackend, "get_cookies")
        assert hasattr(NoDriverBackend, "set_cookies")
        assert hasattr(NoDriverBackend, "delete_all_cookies")
        assert hasattr(NoDriverBackend, "get_user_agent")
        assert hasattr(NoDriverBackend, "get_state")
        assert hasattr(NoDriverBackend, "from_state")
        assert hasattr(NoDriverBackend, "BACKEND_TYPE")

        # Verify class attribute value
        assert NoDriverBackend.BACKEND_TYPE == "nodriver"

        # Verify instance can be created (without starting browser)
        backend = NoDriverBackend(headless=False)
        assert backend._headless is False
        assert backend.is_running is False

    def test_backend_type_is_nodriver(self) -> None:
        """BACKEND_TYPE should be 'nodriver'."""
        assert NoDriverBackend.BACKEND_TYPE == "nodriver"


class TestNoDriverBackendRegistry:
    """Tests for NoDriverBackend registration."""

    def test_nodriver_in_list_backends(self) -> None:
        """nodriver should be in available backends."""
        backends = list_backends()
        assert "nodriver" in backends

    def test_get_nodriver_backend(self) -> None:
        """Get nodriver backend by name."""
        backend = get_backend("nodriver", headless=False)
        assert isinstance(backend, NoDriverBackend)
        assert backend._headless is False

    def test_get_nodriver_passes_kwargs(self) -> None:
        """kwargs are passed to backend constructor."""
        backend = get_backend(
            "nodriver",
            headless=True,
            default_timeout=30,
        )
        assert backend._headless is True
        assert backend._default_timeout == 30


class TestNoDriverBackendInit:
    """Tests for NoDriverBackend initialization."""

    def test_init_stores_options(self) -> None:
        """Backend stores initialization options."""
        backend = NoDriverBackend(
            headless=True,
            default_timeout=30,
        )
        assert backend._headless is True
        assert backend._default_timeout == 30

    def test_init_with_profile_dir(self, tmp_path: Path) -> None:
        """Backend accepts profile_dir option."""
        profile = tmp_path / "test_profile"
        backend = NoDriverBackend(profile_dir=profile)
        assert backend._profile_dir == profile

    def test_init_default_headless_false(self) -> None:
        """Default headless is False for better stealth."""
        backend = NoDriverBackend()
        assert backend._headless is False

    def test_is_running_false_before_start(self) -> None:
        """is_running is False before start() called."""
        backend = NoDriverBackend()
        assert backend.is_running is False


class TestNoDriverBackendRepr:
    """Tests for NoDriverBackend string representation."""

    def test_repr_shows_headed_stopped(self) -> None:
        """__repr__ shows headed mode when not headless."""
        backend = NoDriverBackend(headless=False)
        assert "headed" in repr(backend)
        assert "stopped" in repr(backend)

    def test_repr_shows_headless(self) -> None:
        """__repr__ shows headless mode when headless=True."""
        backend = NoDriverBackend(headless=True)
        assert "headless" in repr(backend)


class TestNoDriverBackendStart:
    """Tests for NoDriverBackend start/stop lifecycle."""

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_start_calls_asyncio_run(self, mock_run: MagicMock) -> None:
        """start() uses asyncio.run to execute async code."""
        backend = NoDriverBackend()
        backend.start()

        mock_run.assert_called_once()
        assert backend._started is True

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_start_is_idempotent(self, mock_run: MagicMock) -> None:
        """Calling start() twice only starts once."""
        backend = NoDriverBackend()
        backend.start()
        backend.start()  # Second call should be no-op

        mock_run.assert_called_once()

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_stop_clears_state(self, mock_run: MagicMock) -> None:
        """stop() clears browser and page state."""
        backend = NoDriverBackend()
        backend.start()
        backend._browser = MagicMock()
        backend._page = MagicMock()

        backend.stop()

        assert backend._browser is None
        assert backend._page is None
        assert backend._started is False

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_stop_is_idempotent(self, mock_run: MagicMock) -> None:
        """Calling stop() twice is safe."""
        backend = NoDriverBackend()
        backend.start()
        backend.stop()
        backend.stop()  # Second call should be no-op

        # Two calls: one for start, one for stop
        assert mock_run.call_count == 2

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_context_manager(self, mock_run: MagicMock) -> None:
        """Backend works as context manager."""
        with NoDriverBackend() as backend:
            assert backend._started is True

        # After exit, stop should have been called
        assert backend._started is False


class TestNoDriverBackendNavigation:
    """Tests for NoDriverBackend navigation."""

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_navigate_calls_asyncio_run(self, mock_run: MagicMock) -> None:
        """navigate() uses asyncio.run."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()

        backend.navigate("https://example.com")

        # Called for navigation
        mock_run.assert_called()


class TestNoDriverBackendSerialization:
    """Tests for NoDriverBackend serialization."""

    def test_get_state_returns_backend_type(self) -> None:
        """get_state includes backend_type."""
        backend = NoDriverBackend()
        state = backend.get_state()

        assert state["backend_type"] == "nodriver"

    def test_get_state_includes_options(self) -> None:
        """get_state includes initialization options."""
        backend = NoDriverBackend(
            headless=True,
            default_timeout=30,
        )
        state = backend.get_state()

        assert state["headless"] is True
        assert state["default_timeout"] == 30

    def test_get_state_includes_profile_dir(self, tmp_path: Path) -> None:
        """get_state includes profile_dir as string."""
        profile = tmp_path / "test"
        backend = NoDriverBackend(profile_dir=profile)
        state = backend.get_state()

        assert state["profile_dir"] == str(profile)

    def test_from_state_recreates_backend(self, tmp_path: Path) -> None:
        """from_state recreates backend with same options."""
        profile = tmp_path / "test"
        original = NoDriverBackend(
            headless=True,
            default_timeout=30,
            profile_dir=profile,
        )
        state = original.get_state()

        recreated = NoDriverBackend.from_state(state)

        assert recreated._headless is True
        assert recreated._default_timeout == 30
        assert recreated._profile_dir == profile

    def test_from_state_with_defaults(self) -> None:
        """from_state uses defaults for missing keys."""
        state = {"backend_type": "nodriver"}
        backend = NoDriverBackend.from_state(state)

        assert backend._headless is False  # nodriver default
        assert backend._default_timeout == 15

    def test_roundtrip_serialization(self) -> None:
        """get_state/from_state roundtrip preserves options."""
        original = NoDriverBackend(
            headless=True,
            default_timeout=45,
        )

        state = original.get_state()
        recreated = NoDriverBackend.from_state(state)

        assert recreated._headless == original._headless
        assert recreated._default_timeout == original._default_timeout


class TestNoDriverBackendDriver:
    """Tests for NoDriverBackend driver property."""

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_driver_starts_if_not_running(self, mock_run: MagicMock) -> None:
        """driver property starts browser if not running."""
        backend = NoDriverBackend()
        _ = backend.driver

        # Should have started
        mock_run.assert_called()
        assert backend._started is True


class TestNoDriverBackendErrorHandling:
    """Tests for NoDriverBackend error handling."""

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_start_runtime_error_raises_browser_error(self, mock_run: MagicMock) -> None:
        """RuntimeError during start raises BrowserError."""
        from graftpunk.exceptions import BrowserError

        mock_run.side_effect = RuntimeError("Chrome not found")

        backend = NoDriverBackend()
        with pytest.raises(BrowserError, match="Failed to start NoDriver"):
            backend.start()

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_start_connection_error_raises_browser_error(self, mock_run: MagicMock) -> None:
        """ConnectionError during start raises BrowserError."""
        from graftpunk.exceptions import BrowserError

        mock_run.side_effect = ConnectionError("CDP connection failed")

        backend = NoDriverBackend()
        with pytest.raises(BrowserError, match="Failed to start NoDriver"):
            backend.start()

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_start_timeout_error_raises_browser_error(self, mock_run: MagicMock) -> None:
        """TimeoutError during start raises BrowserError."""
        from graftpunk.exceptions import BrowserError

        mock_run.side_effect = TimeoutError("Browser startup timed out")

        backend = NoDriverBackend()
        with pytest.raises(BrowserError, match="Failed to start NoDriver"):
            backend.start()

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_start_os_error_raises_browser_error(self, mock_run: MagicMock) -> None:
        """OSError during start raises BrowserError."""
        from graftpunk.exceptions import BrowserError

        mock_run.side_effect = OSError("Chrome binary not found")

        backend = NoDriverBackend()
        with pytest.raises(BrowserError, match="Failed to start NoDriver"):
            backend.start()

    def test_start_import_error_raises_browser_error(self) -> None:
        """ImportError for nodriver package raises BrowserError with install hint."""
        from graftpunk.exceptions import BrowserError

        # Simulate nodriver not being installed by making asyncio.run raise
        # the BrowserError that would come from the import failure
        with patch("graftpunk.backends.nodriver.asyncio.run") as mock_run:
            mock_run.side_effect = BrowserError(
                "nodriver package not installed. Install with: pip install graftpunk[nodriver]"
            )

            backend = NoDriverBackend()
            with pytest.raises(BrowserError, match="nodriver package not installed"):
                backend.start()

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_navigate_error_raises_browser_error(self, mock_run: MagicMock) -> None:
        """Errors during navigation raise BrowserError."""
        from graftpunk.exceptions import BrowserError

        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()

        # First call is for navigate, make it raise
        mock_run.side_effect = RuntimeError("navigation failed")

        with pytest.raises(BrowserError, match="Navigation failed"):
            backend.navigate("https://example.com")

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_stop_handles_runtime_error_gracefully(self, mock_run: MagicMock) -> None:
        """stop() handles RuntimeError gracefully."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()

        # Make stop raise RuntimeError
        mock_run.side_effect = RuntimeError("already stopped")

        # Should not raise
        backend.stop()
        assert backend.is_running is False

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_stop_handles_os_error_gracefully(self, mock_run: MagicMock) -> None:
        """stop() handles OSError gracefully."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()

        # Make stop raise OSError
        mock_run.side_effect = OSError("process died")

        # Should not raise
        backend.stop()
        assert backend.is_running is False

    @patch("graftpunk.backends.nodriver.LOG")
    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_stop_logs_debug_for_expected_errors(
        self, mock_run: MagicMock, mock_log: MagicMock
    ) -> None:
        """stop() logs DEBUG for expected cleanup errors like 'browser is already closed'."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()

        # Use an expected error pattern
        mock_run.side_effect = RuntimeError("browser is already closed")
        backend.stop()

        # Should log at debug level for expected error
        mock_log.debug.assert_called()

    @patch("graftpunk.backends.nodriver.LOG")
    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_stop_logs_warning_for_unexpected_errors(
        self, mock_run: MagicMock, mock_log: MagicMock
    ) -> None:
        """stop() logs WARNING for unexpected errors."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()

        # Use an unexpected error pattern
        mock_run.side_effect = RuntimeError("unexpected error not in patterns")
        backend.stop()

        # Should log at warning level for unexpected error
        mock_log.warning.assert_called()

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_navigate_auto_starts_browser(self, mock_run: MagicMock) -> None:
        """navigate() auto-starts browser if not running."""
        backend = NoDriverBackend()
        assert backend.is_running is False

        backend.navigate("https://example.com")

        # Should have called start (via asyncio.run)
        assert mock_run.call_count >= 1
        assert backend._started is True

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_set_cookies_logs_warning_on_failure(self, mock_run: MagicMock) -> None:
        """set_cookies() logs warning when operation fails."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()

        # Make set_cookies fail
        mock_run.side_effect = RuntimeError("cookies failed")

        # Should not raise, but log warning
        backend.set_cookies([{"name": "test", "value": "value"}])

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_delete_cookies_logs_warning_on_failure(self, mock_run: MagicMock) -> None:
        """delete_all_cookies() logs warning when operation fails."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()

        # Make delete fail
        mock_run.side_effect = RuntimeError("delete failed")

        # Should not raise, but log warning
        backend.delete_all_cookies()

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_current_url_returns_empty_on_exception(self, mock_run: MagicMock) -> None:
        """current_url returns empty string on exception."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()

        mock_run.side_effect = RuntimeError("page gone")

        assert backend.current_url == ""

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_page_title_returns_empty_on_exception(self, mock_run: MagicMock) -> None:
        """page_title returns empty string on exception."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()

        mock_run.side_effect = RuntimeError("page gone")

        assert backend.page_title == ""

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_page_source_returns_empty_on_exception(self, mock_run: MagicMock) -> None:
        """page_source returns empty string on exception."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()

        mock_run.side_effect = RuntimeError("page gone")

        assert backend.page_source == ""

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_get_user_agent_returns_empty_on_exception(self, mock_run: MagicMock) -> None:
        """get_user_agent() returns empty string on exception."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()

        mock_run.side_effect = RuntimeError("page gone")

        assert backend.get_user_agent() == ""


class TestNoDriverBackendOptions:
    """Tests for NoDriverBackend option passthrough."""

    def test_browser_args_stored_in_options(self) -> None:
        """browser_args option is stored."""
        backend = NoDriverBackend(browser_args=["--disable-gpu"])
        assert backend._options.get("browser_args") == ["--disable-gpu"]

    def test_lang_stored_in_options(self) -> None:
        """lang option is stored."""
        backend = NoDriverBackend(lang="en-US")
        assert backend._options.get("lang") == "en-US"

    def test_browser_executable_path_stored_in_options(self) -> None:
        """browser_executable_path option is stored."""
        backend = NoDriverBackend(browser_executable_path="/usr/bin/chrome")
        assert backend._options.get("browser_executable_path") == "/usr/bin/chrome"

    def test_options_included_in_state(self) -> None:
        """Additional options are included in get_state()."""
        backend = NoDriverBackend(
            browser_args=["--disable-gpu"],
            lang="en-US",
        )
        state = backend.get_state()

        assert state.get("browser_args") == ["--disable-gpu"]
        assert state.get("lang") == "en-US"

    def test_options_restored_from_state(self) -> None:
        """Additional options are restored via from_state()."""
        original = NoDriverBackend(
            browser_args=["--no-sandbox"],
            lang="de-DE",
        )
        state = original.get_state()

        recreated = NoDriverBackend.from_state(state)

        assert recreated._options.get("browser_args") == ["--no-sandbox"]
        assert recreated._options.get("lang") == "de-DE"


class TestNoDriverBackendMissingCoverage:
    """Additional tests for NoDriverBackend coverage gaps."""

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_is_running_false_when_browser_none_but_started_true(self, mock_run: MagicMock) -> None:
        """is_running returns False if browser is None even if _started is True."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()

        assert backend.is_running is True

        # Simulate browser becoming None (crash scenario)
        backend._browser = None

        assert backend.is_running is False

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_start_updates_additional_options(self, mock_run: MagicMock) -> None:
        """start() merges additional options into _options."""
        backend = NoDriverBackend(browser_args=["--disable-gpu"])
        backend.start(custom_arg="custom_value")

        assert backend._options.get("custom_arg") == "custom_value"
        assert backend._options.get("browser_args") == ["--disable-gpu"]  # Original preserved

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_delete_all_cookies_calls_cdp_command(self, mock_run: MagicMock) -> None:
        """delete_all_cookies() invokes CDP command when running."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()

        backend.delete_all_cookies()

        # Verify asyncio.run was called (to execute _delete_all_cookies_async)
        mock_run.assert_called_once()

    def test_delete_all_cookies_safe_when_not_running(self) -> None:
        """delete_all_cookies() is safe when not running."""
        backend = NoDriverBackend()
        # Should not raise
        backend.delete_all_cookies()

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_context_manager_propagates_exceptions(self, mock_run: MagicMock) -> None:
        """Context manager does not suppress exceptions from within the block."""
        with pytest.raises(ValueError, match="test error"), NoDriverBackend():
            raise ValueError("test error")

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_start_overrides_headless(self, mock_run: MagicMock) -> None:
        """start() can override headless setting."""
        backend = NoDriverBackend(headless=True)
        backend.start(headless=False)

        assert backend._headless is False

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_start_overrides_profile_dir(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """start() can override profile_dir setting."""
        profile = tmp_path / "override_profile"

        backend = NoDriverBackend()
        backend.start(profile_dir=profile)

        assert backend._profile_dir == profile

    def test_from_state_preserves_extra_options(self) -> None:
        """from_state preserves unknown keys as options."""
        state = {
            "backend_type": "nodriver",
            "headless": False,
            "custom_option": "value",
            "another_option": 123,
        }
        backend = NoDriverBackend.from_state(state)

        assert backend._options.get("custom_option") == "value"
        assert backend._options.get("another_option") == 123

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_get_cookies_returns_cookies_when_running(self, mock_run: MagicMock) -> None:
        """get_cookies() returns cookies when running."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()

        mock_run.return_value = [{"name": "session", "value": "abc123"}]

        cookies = backend.get_cookies()
        assert len(cookies) == 1
        assert cookies[0]["name"] == "session"

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_get_cookies_returns_empty_on_exception(self, mock_run: MagicMock) -> None:
        """get_cookies() returns empty list on exception."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()

        mock_run.side_effect = RuntimeError("failed")

        assert backend.get_cookies() == []

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_properties_return_empty_when_page_none_but_running(self, mock_run: MagicMock) -> None:
        """Properties return empty values if _page is None despite running state."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = None  # Page is None but browser is running

        # These should return empty values via the async methods
        # which check `if self._page is None: return ""`
        mock_run.return_value = ""

        assert backend.current_url == ""
        assert backend.page_title == ""
        assert backend.page_source == ""
        assert backend.get_user_agent() == ""

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_get_cookies_returns_empty_when_page_none(self, mock_run: MagicMock) -> None:
        """get_cookies() returns empty list if _page is None."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = None

        mock_run.return_value = []

        assert backend.get_cookies() == []

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_set_cookies_safe_when_page_none(self, mock_run: MagicMock) -> None:
        """set_cookies() is safe when _page is None."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = None

        # Should not raise - _set_cookies_async checks for None page
        backend.set_cookies([{"name": "test", "value": "value"}])

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_delete_all_cookies_safe_when_page_none(self, mock_run: MagicMock) -> None:
        """delete_all_cookies() is safe when _page is None."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = None

        # Should not raise - _delete_all_cookies_async checks for None page
        backend.delete_all_cookies()


class TestNoDriverBackendFromStateMismatch:
    """Tests for from_state() with mismatched backend_type."""

    def test_from_state_ignores_backend_type_mismatch(self) -> None:
        """from_state ignores backend_type key - creates class it's called on."""
        state = {"backend_type": "selenium", "headless": True}
        backend = NoDriverBackend.from_state(state)

        # Should create NoDriverBackend regardless of backend_type in state
        assert isinstance(backend, NoDriverBackend)
        assert backend._headless is True

    def test_from_state_warns_on_type_mismatch(self) -> None:
        """from_state logs warning when backend_type doesn't match."""
        from unittest.mock import patch

        with patch("graftpunk.backends.nodriver.LOG") as mock_log:
            state = {"backend_type": "selenium", "headless": True}
            NoDriverBackend.from_state(state)

        # Should have logged a warning about the mismatch
        mock_log.warning.assert_called_once()
        call_args = mock_log.warning.call_args
        assert call_args[0][0] == "backend_type_mismatch"

    def test_from_state_logs_when_using_defaults(self) -> None:
        """from_state gracefully handles missing keys with defaults."""
        state = {}  # Empty state
        backend = NoDriverBackend.from_state(state)

        assert backend._headless is False  # NoDriver default
        assert backend._default_timeout == 15  # Default


class TestNoDriverBackendMalformedUrls:
    """Tests for navigate() with malformed URLs."""

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_navigate_with_empty_url(self, mock_run: MagicMock) -> None:
        """navigate() with empty URL passes to driver (driver handles validation)."""
        from graftpunk.exceptions import BrowserError

        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()

        mock_run.side_effect = RuntimeError("Invalid URL")

        with pytest.raises(BrowserError, match="Navigation failed"):
            backend.navigate("")

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_navigate_with_malformed_url(self, mock_run: MagicMock) -> None:
        """navigate() with malformed URL raises BrowserError from driver."""
        from graftpunk.exceptions import BrowserError

        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()

        mock_run.side_effect = RuntimeError("invalid argument: 'url' must be a valid URL")

        with pytest.raises(BrowserError, match="Navigation failed"):
            backend.navigate("not-a-valid-url")


class TestNoDriverBackendAsyncContextLimitation:
    """Tests documenting NoDriver's async context limitation."""

    def test_nodriver_documents_async_limitation(self) -> None:
        """NoDriverBackend docstring documents async context limitation."""
        # This test verifies the documented limitation is present
        assert "asyncio.run()" in NoDriverBackend.__doc__
        assert "async" in NoDriverBackend.__doc__.lower()

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_run_async_uses_asyncio_run(self, mock_run: MagicMock) -> None:
        """_run_async uses asyncio.run() which fails in existing event loop."""

        async def sample_coro():
            return "result"

        mock_run.return_value = "result"

        backend = NoDriverBackend()
        backend._run_async(sample_coro())

        # Verify asyncio.run was called
        mock_run.assert_called_once()

    def test_run_async_raises_error_in_async_context(self) -> None:
        """_run_async raises RuntimeError when called from async context."""
        import asyncio

        backend = NoDriverBackend()

        async def test_in_async_context():
            async def dummy_coro():
                return "result"

            # This should raise because we're inside an event loop
            backend._run_async(dummy_coro())

        # Run the test inside an event loop to trigger the error
        with pytest.raises(RuntimeError, match="cannot be used from within an async context"):
            asyncio.run(test_in_async_context())
