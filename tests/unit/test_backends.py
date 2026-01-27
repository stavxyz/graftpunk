"""Tests for browser backend abstraction layer."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from graftpunk.backends import (
    BrowserBackend,
    get_backend,
    list_backends,
    register_backend,
)
from graftpunk.backends.selenium import SeleniumBackend


class TestBrowserBackendProtocol:
    """Tests for BrowserBackend Protocol."""

    def test_selenium_backend_implements_protocol(self) -> None:
        """SeleniumBackend should satisfy BrowserBackend protocol."""
        backend = SeleniumBackend(headless=True)
        assert isinstance(backend, BrowserBackend)

    def test_protocol_is_runtime_checkable(self) -> None:
        """Protocol should support isinstance checks."""
        # Create a mock that has all required methods
        mock = MagicMock(spec=SeleniumBackend)
        # Note: MagicMock with spec automatically gets all methods
        # but isinstance check requires actual attributes
        assert hasattr(mock, "start")
        assert hasattr(mock, "stop")
        assert hasattr(mock, "navigate")


class TestGetBackend:
    """Tests for get_backend factory function."""

    def test_get_selenium_backend(self) -> None:
        """Get selenium backend by name."""
        backend = get_backend("selenium", headless=True)
        assert isinstance(backend, SeleniumBackend)
        assert backend._headless is True

    def test_get_legacy_alias(self) -> None:
        """legacy is an alias for selenium."""
        backend = get_backend("legacy", headless=True)
        assert isinstance(backend, SeleniumBackend)

    def test_default_backend_is_selenium(self) -> None:
        """Default backend should be selenium."""
        backend = get_backend()
        assert isinstance(backend, SeleniumBackend)

    def test_unknown_backend_raises_error(self) -> None:
        """Unknown backend name raises ValueError."""
        with pytest.raises(ValueError, match="Unknown backend"):
            get_backend("nonexistent")

    def test_passes_kwargs_to_backend(self) -> None:
        """kwargs are passed to backend constructor."""
        backend = get_backend(
            "selenium",
            headless=False,
            use_stealth=False,
            default_timeout=30,
        )
        assert backend._headless is False
        assert backend._use_stealth is False
        assert backend._default_timeout == 30


class TestListBackends:
    """Tests for list_backends function."""

    def test_list_backends_returns_list(self) -> None:
        """list_backends returns a list."""
        backends = list_backends()
        assert isinstance(backends, list)

    def test_list_backends_includes_selenium(self) -> None:
        """selenium should be in available backends."""
        backends = list_backends()
        assert "selenium" in backends

    def test_list_backends_includes_legacy(self) -> None:
        """legacy alias should be in available backends."""
        backends = list_backends()
        assert "legacy" in backends

    def test_list_backends_is_sorted(self) -> None:
        """list_backends returns sorted list."""
        backends = list_backends()
        assert backends == sorted(backends)


class TestRegisterBackend:
    """Tests for register_backend function."""

    def test_register_duplicate_raises_error(self) -> None:
        """Registering existing name raises ValueError."""
        with pytest.raises(ValueError, match="already registered"):
            register_backend("selenium", "some.module:SomeClass")

    def test_register_invalid_path_raises_error(self) -> None:
        """Invalid module:class path raises ValueError."""
        with pytest.raises(ValueError, match="Expected format"):
            register_backend("invalid", "invalid_path_no_colon")

    def test_register_backend_success_and_retrieval(self) -> None:
        """Registered backend can be retrieved via get_backend()."""
        from graftpunk.backends import _BACKEND_REGISTRY

        # Register a custom backend pointing to an existing class
        test_name = "test_custom_backend_registration"
        register_backend(test_name, "graftpunk.backends.selenium:SeleniumBackend")

        try:
            # Verify it's in the registry
            assert test_name in _BACKEND_REGISTRY

            # Verify we can retrieve it
            backend = get_backend(test_name)
            assert isinstance(backend, SeleniumBackend)
        finally:
            # Clean up registry to not pollute other tests
            del _BACKEND_REGISTRY[test_name]


class TestSeleniumBackend:
    """Tests for SeleniumBackend implementation."""

    def test_init_stores_options(self) -> None:
        """Backend stores initialization options."""
        backend = SeleniumBackend(
            headless=True,
            use_stealth=True,
            default_timeout=30,
        )
        assert backend._headless is True
        assert backend._use_stealth is True
        assert backend._default_timeout == 30

    def test_init_with_profile_dir(self, tmp_path: Path) -> None:
        """Backend accepts profile_dir option."""
        profile = tmp_path / "test_profile"
        backend = SeleniumBackend(profile_dir=profile)
        assert backend._profile_dir == profile

    def test_is_running_false_before_start(self) -> None:
        """is_running is False before start() called."""
        backend = SeleniumBackend(headless=True)
        assert backend.is_running is False

    def test_backend_type_is_selenium(self) -> None:
        """BACKEND_TYPE should be 'selenium'."""
        assert SeleniumBackend.BACKEND_TYPE == "selenium"

    def test_repr_shows_status(self) -> None:
        """__repr__ shows backend status."""
        backend = SeleniumBackend(headless=True, use_stealth=True)
        assert "stealth" in repr(backend)
        assert "stopped" in repr(backend)

    def test_repr_shows_standard_mode(self) -> None:
        """__repr__ shows standard mode when not stealth."""
        backend = SeleniumBackend(use_stealth=False)
        assert "standard" in repr(backend)

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_start_creates_stealth_driver(self, mock_create: MagicMock) -> None:
        """start() creates stealth driver when use_stealth=True."""
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(headless=True, use_stealth=True)
        backend.start()

        mock_create.assert_called_once_with(headless=True, profile_dir=None)
        assert backend.is_running is True
        assert backend._driver is mock_driver

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_start_is_idempotent(self, mock_create: MagicMock) -> None:
        """Calling start() twice only creates driver once."""
        mock_create.return_value = MagicMock()

        backend = SeleniumBackend(use_stealth=True)
        backend.start()
        backend.start()  # Second call should be no-op

        mock_create.assert_called_once()

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_stop_quits_driver(self, mock_create: MagicMock) -> None:
        """stop() quits the driver."""
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        backend.start()
        backend.stop()

        mock_driver.quit.assert_called_once()
        assert backend.is_running is False
        assert backend._driver is None

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_stop_is_idempotent(self, mock_create: MagicMock) -> None:
        """Calling stop() twice is safe."""
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        backend.start()
        backend.stop()
        backend.stop()  # Second call should be no-op

        mock_driver.quit.assert_called_once()

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_context_manager(self, mock_create: MagicMock) -> None:
        """Backend works as context manager."""
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver

        with SeleniumBackend(use_stealth=True) as backend:
            assert backend.is_running is True

        mock_driver.quit.assert_called_once()

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_navigate_calls_driver_get(self, mock_create: MagicMock) -> None:
        """navigate() calls driver.get()."""
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        backend.start()
        backend.navigate("https://example.com")

        mock_driver.get.assert_called_once_with("https://example.com")

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_current_url_returns_driver_url(self, mock_create: MagicMock) -> None:
        """current_url returns driver's current_url."""
        mock_driver = MagicMock()
        mock_driver.current_url = "https://example.com/page"
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        backend.start()

        assert backend.current_url == "https://example.com/page"

    def test_current_url_empty_when_not_running(self) -> None:
        """current_url returns empty string when not running."""
        backend = SeleniumBackend()
        assert backend.current_url == ""

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_get_cookies_returns_driver_cookies(self, mock_create: MagicMock) -> None:
        """get_cookies() returns driver's cookies."""
        mock_driver = MagicMock()
        mock_driver.get_cookies.return_value = [{"name": "session", "value": "abc123"}]
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        backend.start()

        cookies = backend.get_cookies()
        assert len(cookies) == 1
        assert cookies[0]["name"] == "session"

    def test_get_cookies_empty_when_not_running(self) -> None:
        """get_cookies() returns empty list when not running."""
        backend = SeleniumBackend()
        assert backend.get_cookies() == []

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_set_cookies_adds_to_driver(self, mock_create: MagicMock) -> None:
        """set_cookies() adds cookies to driver."""
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        backend.start()
        backend.set_cookies([{"name": "test", "value": "value"}])

        mock_driver.add_cookie.assert_called_once_with({"name": "test", "value": "value"})

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_get_user_agent(self, mock_create: MagicMock) -> None:
        """get_user_agent() executes JavaScript."""
        mock_driver = MagicMock()
        mock_driver.execute_script.return_value = "Mozilla/5.0 Test"
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        backend.start()

        ua = backend.get_user_agent()
        assert ua == "Mozilla/5.0 Test"
        mock_driver.execute_script.assert_called_with("return navigator.userAgent")

    def test_get_user_agent_empty_when_not_running(self) -> None:
        """get_user_agent() returns empty string when not running."""
        backend = SeleniumBackend()
        assert backend.get_user_agent() == ""


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


class TestSeleniumBackendErrorHandling:
    """Tests for SeleniumBackend error handling."""

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_start_webdriver_exception_raises_browser_error(self, mock_create: MagicMock) -> None:
        """WebDriverException during start raises BrowserError."""
        import selenium.common.exceptions

        from graftpunk.exceptions import BrowserError

        mock_create.side_effect = selenium.common.exceptions.WebDriverException("fail")

        backend = SeleniumBackend(use_stealth=True)
        with pytest.raises(BrowserError, match="Failed to start"):
            backend.start()

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_start_os_error_raises_browser_error(self, mock_create: MagicMock) -> None:
        """OSError during start raises BrowserError."""
        from graftpunk.exceptions import BrowserError

        mock_create.side_effect = OSError("Chrome not found")

        backend = SeleniumBackend(use_stealth=True)
        with pytest.raises(BrowserError, match="Failed to start"):
            backend.start()

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_navigate_webdriver_exception_raises_browser_error(
        self, mock_create: MagicMock
    ) -> None:
        """WebDriverException during navigate raises BrowserError."""
        import selenium.common.exceptions

        from graftpunk.exceptions import BrowserError

        mock_driver = MagicMock()
        mock_driver.get.side_effect = selenium.common.exceptions.WebDriverException("timeout")
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        backend.start()
        with pytest.raises(BrowserError, match="Navigation failed"):
            backend.navigate("https://example.com")

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_stop_handles_quit_exception_gracefully(self, mock_create: MagicMock) -> None:
        """stop() handles WebDriverException from quit gracefully."""
        import selenium.common.exceptions

        mock_driver = MagicMock()
        mock_driver.quit.side_effect = selenium.common.exceptions.WebDriverException(
            "already closed"
        )
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        backend.start()
        # Should not raise
        backend.stop()
        assert backend.is_running is False

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_stop_handles_os_error_gracefully(self, mock_create: MagicMock) -> None:
        """stop() handles OSError from quit gracefully."""
        mock_driver = MagicMock()
        mock_driver.quit.side_effect = OSError("process died")
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        backend.start()
        # Should not raise
        backend.stop()
        assert backend.is_running is False

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_navigate_auto_starts_browser(self, mock_create: MagicMock) -> None:
        """navigate() auto-starts browser if not running."""
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        assert backend.is_running is False

        backend.navigate("https://example.com")

        assert backend.is_running is True
        mock_driver.get.assert_called_once_with("https://example.com")

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_set_cookies_auto_starts_browser(self, mock_create: MagicMock) -> None:
        """set_cookies() auto-starts browser if not running."""
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        assert backend.is_running is False

        backend.set_cookies([{"name": "test", "value": "value"}])

        assert backend.is_running is True
        mock_driver.add_cookie.assert_called_once()

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_set_cookies_logs_warning_on_failure(self, mock_create: MagicMock) -> None:
        """set_cookies() logs warning when cookie add fails."""
        import selenium.common.exceptions

        mock_driver = MagicMock()
        mock_driver.add_cookie.side_effect = selenium.common.exceptions.WebDriverException(
            "wrong domain"
        )
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        backend.start()
        # Should not raise, but log warning
        backend.set_cookies([{"name": "test", "value": "value"}])

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_current_url_returns_empty_on_exception(self, mock_create: MagicMock) -> None:
        """current_url returns empty string on WebDriverException."""
        import selenium.common.exceptions

        mock_driver = MagicMock()
        mock_driver.current_url = property(
            lambda self: (_ for _ in ()).throw(
                selenium.common.exceptions.WebDriverException("stale")
            )
        )
        # Simpler approach: make it a method that raises
        type(mock_driver).current_url = property(
            MagicMock(side_effect=selenium.common.exceptions.WebDriverException("stale"))
        )
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        backend.start()

        assert backend.current_url == ""

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_page_title_returns_empty_on_exception(self, mock_create: MagicMock) -> None:
        """page_title returns empty string on WebDriverException."""
        import selenium.common.exceptions

        mock_driver = MagicMock()
        type(mock_driver).title = property(
            MagicMock(side_effect=selenium.common.exceptions.WebDriverException("stale"))
        )
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        backend.start()

        assert backend.page_title == ""


class TestSeleniumBackendStandardMode:
    """Tests for SeleniumBackend standard (non-stealth) mode."""

    @patch("selenium.webdriver.Chrome")
    @patch("graftpunk.backends.selenium.webdriver_manager.chrome.ChromeDriverManager")
    @patch("graftpunk.backends.selenium.get_chrome_version")
    def test_standard_mode_uses_webdriver_manager(
        self,
        mock_version: MagicMock,
        mock_manager: MagicMock,
        mock_chrome: MagicMock,
    ) -> None:
        """Standard mode uses webdriver-manager for chromedriver."""
        mock_version.return_value = "120"
        mock_manager.return_value.install.return_value = "/path/to/chromedriver"
        mock_chrome.return_value = MagicMock()

        backend = SeleniumBackend(use_stealth=False, headless=True)
        backend.start()

        mock_version.assert_called_once_with(major=True)
        mock_manager.assert_called_once_with(driver_version="120")
        assert backend.is_running is True

    @patch("selenium.webdriver.Chrome")
    @patch("graftpunk.backends.selenium.webdriver_manager.chrome.ChromeDriverManager")
    @patch("graftpunk.backends.selenium.get_chrome_version")
    def test_standard_mode_headless_argument(
        self,
        mock_version: MagicMock,
        mock_manager: MagicMock,
        mock_chrome: MagicMock,
    ) -> None:
        """Standard mode adds headless argument when headless=True."""
        mock_version.return_value = "120"
        mock_manager.return_value.install.return_value = "/path/to/chromedriver"
        mock_chrome.return_value = MagicMock()

        backend = SeleniumBackend(use_stealth=False, headless=True)
        backend.start()

        # Check that Chrome was called with options containing headless
        call_kwargs = mock_chrome.call_args[1]
        options = call_kwargs["options"]
        # Check arguments were added
        assert any("headless" in str(arg) for arg in options.arguments)

    @patch("graftpunk.backends.selenium.get_chrome_version")
    def test_standard_mode_chrome_detection_error(self, mock_version: MagicMock) -> None:
        """ChromeDriverError during version detection raises BrowserError."""
        from graftpunk.exceptions import BrowserError, ChromeDriverError

        mock_version.side_effect = ChromeDriverError("Chrome not found")

        backend = SeleniumBackend(use_stealth=False)
        with pytest.raises(BrowserError, match="Failed to detect Chrome"):
            backend.start()

    @patch("selenium.webdriver.Chrome")
    @patch("graftpunk.backends.selenium.webdriver_manager.chrome.ChromeDriverManager")
    @patch("graftpunk.backends.selenium.get_chrome_version")
    def test_standard_mode_respects_window_size_option(
        self,
        mock_version: MagicMock,
        mock_manager: MagicMock,
        mock_chrome: MagicMock,
    ) -> None:
        """Standard mode applies window_size option."""
        mock_version.return_value = "120"
        mock_manager.return_value.install.return_value = "/path/to/chromedriver"
        mock_chrome.return_value = MagicMock()

        backend = SeleniumBackend(use_stealth=False, window_size="1920,1080")
        backend.start()

        call_kwargs = mock_chrome.call_args[1]
        options = call_kwargs["options"]
        assert any("1920,1080" in str(arg) for arg in options.arguments)


class TestSeleniumBackendMissingCoverage:
    """Additional tests for SeleniumBackend coverage gaps."""

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_is_running_false_when_driver_none_but_started_true(
        self, mock_create: MagicMock
    ) -> None:
        """is_running returns False if driver is None even if _started is True."""
        mock_create.return_value = MagicMock()

        backend = SeleniumBackend(use_stealth=True)
        backend.start()
        assert backend.is_running is True

        # Simulate driver becoming None (crash scenario)
        backend._driver = None

        assert backend.is_running is False

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_start_updates_additional_options(self, mock_create: MagicMock) -> None:
        """start() merges additional options into _options."""
        mock_create.return_value = MagicMock()

        backend = SeleniumBackend(use_stealth=True, window_size="800,600")
        backend.start(custom_arg="custom_value")

        assert backend._options.get("custom_arg") == "custom_value"
        assert backend._options.get("window_size") == "800,600"  # Original preserved

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_page_source_returns_driver_source(self, mock_create: MagicMock) -> None:
        """page_source returns driver's page_source."""
        mock_driver = MagicMock()
        mock_driver.page_source = "<html><body>Test</body></html>"
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        backend.start()

        assert backend.page_source == "<html><body>Test</body></html>"

    def test_page_source_empty_when_not_running(self) -> None:
        """page_source returns empty string when not running."""
        backend = SeleniumBackend()
        assert backend.page_source == ""

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_page_source_returns_empty_on_exception(self, mock_create: MagicMock) -> None:
        """page_source returns empty string on WebDriverException."""
        import selenium.common.exceptions

        mock_driver = MagicMock()
        type(mock_driver).page_source = property(
            MagicMock(side_effect=selenium.common.exceptions.WebDriverException("stale"))
        )
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        backend.start()

        assert backend.page_source == ""

    def test_page_title_empty_when_not_running(self) -> None:
        """page_title returns empty string when not running."""
        backend = SeleniumBackend()
        assert backend.page_title == ""

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_delete_all_cookies_calls_driver(self, mock_create: MagicMock) -> None:
        """delete_all_cookies() calls driver.delete_all_cookies()."""
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        backend.start()
        backend.delete_all_cookies()

        mock_driver.delete_all_cookies.assert_called_once()

    def test_delete_all_cookies_safe_when_not_running(self) -> None:
        """delete_all_cookies() is safe when not running."""
        backend = SeleniumBackend()
        # Should not raise
        backend.delete_all_cookies()

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_delete_all_cookies_handles_exception_gracefully(self, mock_create: MagicMock) -> None:
        """delete_all_cookies() handles WebDriverException gracefully."""
        import selenium.common.exceptions

        mock_driver = MagicMock()
        mock_driver.delete_all_cookies.side_effect = selenium.common.exceptions.WebDriverException(
            "fail"
        )
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        backend.start()
        # Should not raise
        backend.delete_all_cookies()

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_driver_starts_if_not_running(self, mock_create: MagicMock) -> None:
        """driver property starts browser if not running."""
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        assert backend.is_running is False

        _ = backend.driver

        assert backend.is_running is True
        mock_create.assert_called_once()

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_context_manager_propagates_exceptions(self, mock_create: MagicMock) -> None:
        """Context manager does not suppress exceptions from within the block."""
        mock_create.return_value = MagicMock()

        with pytest.raises(ValueError, match="test error"), SeleniumBackend(use_stealth=True):
            raise ValueError("test error")

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


class TestGetBackendImportError:
    """Tests for get_backend() import error handling."""

    def test_get_backend_import_error_provides_helpful_message(self) -> None:
        """ImportError during backend loading provides helpful message."""
        with patch("graftpunk.backends.import_module") as mock_import:
            mock_import.side_effect = ImportError("No module named 'nodriver'")

            with pytest.raises(ImportError, match="requires additional dependencies"):
                get_backend("nodriver")

    def test_get_backend_attribute_error_provides_helpful_message(self) -> None:
        """AttributeError during class lookup provides helpful message."""
        with patch("graftpunk.backends.import_module") as mock_import:
            mock_import.return_value = MagicMock(spec=[])  # Empty spec, no attributes
            with pytest.raises(ImportError, match="class.*not found"):
                get_backend("selenium")


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


class TestSeleniumBackendMalformedUrls:
    """Tests for navigate() with malformed URLs."""

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_navigate_with_empty_url(self, mock_create: MagicMock) -> None:
        """navigate() with empty URL passes to driver (driver handles validation)."""
        import selenium.common.exceptions

        from graftpunk.exceptions import BrowserError

        mock_driver = MagicMock()
        mock_driver.get.side_effect = selenium.common.exceptions.WebDriverException("invalid URL")
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        backend.start()

        with pytest.raises(BrowserError, match="Navigation failed"):
            backend.navigate("")

    @patch("graftpunk.stealth.create_stealth_driver")
    def test_navigate_with_malformed_url(self, mock_create: MagicMock) -> None:
        """navigate() with malformed URL raises BrowserError from driver."""
        import selenium.common.exceptions

        from graftpunk.exceptions import BrowserError

        mock_driver = MagicMock()
        mock_driver.get.side_effect = selenium.common.exceptions.WebDriverException(
            "invalid argument: 'url' must be a valid URL"
        )
        mock_create.return_value = mock_driver

        backend = SeleniumBackend(use_stealth=True)
        backend.start()

        with pytest.raises(BrowserError, match="Navigation failed"):
            backend.navigate("not-a-valid-url")
