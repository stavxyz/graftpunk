"""Tests for NoDriver browser backend."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from graftpunk.backends import BrowserBackend, get_backend, list_backends
from graftpunk.backends.nodriver import NoDriverBackend


class TestNoDriverBackendProtocol:
    """Tests for NoDriverBackend Protocol compliance."""

    def test_nodriver_backend_implements_protocol(self) -> None:
        """NoDriverBackend should satisfy BrowserBackend protocol."""
        backend = NoDriverBackend(headless=False)
        assert isinstance(backend, BrowserBackend)

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


class TestNoDriverBackendProperties:
    """Tests for NoDriverBackend property accessors."""

    def test_current_url_empty_when_not_running(self) -> None:
        """current_url returns empty string when not running."""
        backend = NoDriverBackend()
        assert backend.current_url == ""

    def test_page_title_empty_when_not_running(self) -> None:
        """page_title returns empty string when not running."""
        backend = NoDriverBackend()
        assert backend.page_title == ""

    def test_page_source_empty_when_not_running(self) -> None:
        """page_source returns empty string when not running."""
        backend = NoDriverBackend()
        assert backend.page_source == ""

    def test_get_user_agent_empty_when_not_running(self) -> None:
        """get_user_agent() returns empty string when not running."""
        backend = NoDriverBackend()
        assert backend.get_user_agent() == ""


class TestNoDriverBackendCookies:
    """Tests for NoDriverBackend cookie operations."""

    def test_get_cookies_empty_when_not_running(self) -> None:
        """get_cookies() returns empty list when not running."""
        backend = NoDriverBackend()
        assert backend.get_cookies() == []

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_set_cookies_starts_if_not_running(self, mock_run: MagicMock) -> None:
        """set_cookies() starts browser if not running."""
        backend = NoDriverBackend()
        backend.set_cookies([{"name": "test", "value": "value"}])

        # Should have called start and then set_cookies
        assert mock_run.call_count >= 1


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
        import pytest

        from graftpunk.exceptions import BrowserError

        mock_run.side_effect = RuntimeError("Chrome not found")

        backend = NoDriverBackend()
        with pytest.raises(BrowserError, match="Failed to start NoDriver"):
            backend.start()

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_start_connection_error_raises_browser_error(self, mock_run: MagicMock) -> None:
        """ConnectionError during start raises BrowserError."""
        import pytest

        from graftpunk.exceptions import BrowserError

        mock_run.side_effect = ConnectionError("CDP connection failed")

        backend = NoDriverBackend()
        with pytest.raises(BrowserError, match="Failed to start NoDriver"):
            backend.start()

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_start_timeout_error_raises_browser_error(self, mock_run: MagicMock) -> None:
        """TimeoutError during start raises BrowserError."""
        import pytest

        from graftpunk.exceptions import BrowserError

        mock_run.side_effect = TimeoutError("Browser startup timed out")

        backend = NoDriverBackend()
        with pytest.raises(BrowserError, match="Failed to start NoDriver"):
            backend.start()

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_start_os_error_raises_browser_error(self, mock_run: MagicMock) -> None:
        """OSError during start raises BrowserError."""
        import pytest

        from graftpunk.exceptions import BrowserError

        mock_run.side_effect = OSError("Chrome binary not found")

        backend = NoDriverBackend()
        with pytest.raises(BrowserError, match="Failed to start NoDriver"):
            backend.start()

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_navigate_error_raises_browser_error(self, mock_run: MagicMock) -> None:
        """Errors during navigation raise BrowserError."""
        import pytest

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

    def test_delete_all_cookies_safe_when_not_running(self) -> None:
        """delete_all_cookies() is safe when not running."""
        backend = NoDriverBackend()
        # Should not raise
        backend.delete_all_cookies()

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_context_manager_propagates_exceptions(self, mock_run: MagicMock) -> None:
        """Context manager does not suppress exceptions from within the block."""
        import pytest

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
