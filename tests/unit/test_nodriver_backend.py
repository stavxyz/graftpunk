"""Tests for NoDriver browser backend."""

import asyncio
import os
import pickle
import threading
import time
import warnings
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from graftpunk.backends import get_backend, list_backends
from graftpunk.backends.base import Cookie
from graftpunk.backends.nodriver import NoDriverBackend
from graftpunk.chrome_orphans import OWNER_SWITCH, base_browser_args
from graftpunk.exceptions import BrowserError

from .conftest import FakeCdpTab, FakeChrome, with_private_loop

pytestmark = pytest.mark.usefixtures("release_nodriver_loops")


@pytest.fixture()
def temp_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A sandbox that ``tempfile.gettempdir()`` points at.

    ``_removable_temp_profile`` only deletes a directory sitting directly under
    the temp directory, which is where nodriver puts its profiles. A test
    profile has to be somewhere that predicate accepts.
    """
    import tempfile

    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root))
    return root


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


def _stoppable_browser(**attrs: Any) -> MagicMock:
    """A browser the real stop path can run against: no subprocess, no temp profile."""
    browser = MagicMock()
    browser._process = None
    browser.config.uses_custom_data_dir = True
    for name, value in attrs.items():
        setattr(browser, name, value)
    return browser


def _dead_loop_backend() -> NoDriverBackend:
    """A "running" backend whose event loop has stopped, so every sync call fails.

    A browser started with ``BrowserSession.start_async()`` looks like this
    once the caller's loop is gone: ``_run_async`` raises RuntimeError, which
    each sync method turns into its documented fallback.
    """
    loop = asyncio.new_event_loop()
    loop.close()
    backend = NoDriverBackend()
    backend._started = True
    backend._browser = _stoppable_browser()
    backend._page = MagicMock()
    backend._loop = loop
    return backend


class TestNoDriverBackendStart:
    """Tests for NoDriverBackend start/stop lifecycle."""

    def test_start_launches_a_browser_and_opens_a_blank_tab(self, fake_chrome: FakeChrome) -> None:
        backend = NoDriverBackend()

        backend.start()

        assert backend.is_running is True
        (browser,) = fake_chrome.browsers
        assert backend._browser is browser
        assert backend._page is browser.tabs[0]
        assert browser.tabs[0].url == "about:blank"

    def test_start_is_idempotent(self, fake_chrome: FakeChrome) -> None:
        """Calling start() twice only starts one browser."""
        backend = NoDriverBackend()
        backend.start()
        backend.start()

        assert len(fake_chrome.browsers) == 1

    def test_stop_clears_state(self, fake_chrome: FakeChrome) -> None:
        """stop() stops the browser and clears browser and page state."""
        backend = NoDriverBackend()
        backend.start()

        backend.stop()

        assert fake_chrome.browsers[0].stopped_on is not None
        assert backend._browser is None
        assert backend._page is None
        assert backend._started is False

    def test_stop_is_idempotent(self, fake_chrome: FakeChrome) -> None:
        """Calling stop() twice is safe."""
        backend = NoDriverBackend()
        backend.start()
        backend.stop()
        backend.stop()

        assert backend.is_running is False

    def test_context_manager(self, fake_chrome: FakeChrome) -> None:
        """Backend works as context manager."""
        with NoDriverBackend() as backend:
            assert backend.is_running is True

        assert backend._started is False
        assert fake_chrome.browsers[0].stopped_on is not None


class TestNoDriverBackendNavigation:
    """Tests for NoDriverBackend navigation."""

    def test_navigate_makes_the_new_tab_current(self, fake_chrome: FakeChrome) -> None:
        backend = NoDriverBackend()
        backend.start()

        backend.navigate("https://example.com/")

        assert backend._page is fake_chrome.browsers[0].tabs[-1]
        assert backend.current_url == "https://example.com/"


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

    def test_driver_starts_if_not_running(self, fake_chrome: FakeChrome) -> None:
        """driver property starts browser if not running."""
        backend = NoDriverBackend()

        assert backend.driver is fake_chrome.browsers[0]
        assert backend._started is True


class TestNoDriverBackendErrorHandling:
    """Tests for NoDriverBackend error handling."""

    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("Chrome not found"),
            ConnectionError("CDP connection failed"),
            TimeoutError("Browser startup timed out"),
            OSError("Chrome binary not found"),
        ],
        ids=["RuntimeError", "ConnectionError", "TimeoutError", "OSError"],
    )
    def test_a_start_error_raises_browser_error(
        self, fake_chrome: FakeChrome, exc: Exception
    ) -> None:
        fake_chrome.fail_with = exc
        backend = NoDriverBackend()

        with pytest.raises(BrowserError, match="Failed to start NoDriver"):
            backend.start()

        assert backend.is_running is False

    def test_start_import_error_raises_browser_error(self) -> None:
        """A missing nodriver package raises BrowserError with the install hint."""
        backend = NoDriverBackend()

        with (
            patch.dict("sys.modules", {"nodriver": None}),
            pytest.raises(BrowserError, match="nodriver package not installed"),
        ):
            backend.start()

    def test_navigate_error_raises_browser_error(self) -> None:
        """Errors during navigation raise BrowserError."""
        backend = with_private_loop(NoDriverBackend())
        backend._started = True
        backend._browser = _stoppable_browser(
            get=AsyncMock(side_effect=RuntimeError("navigation failed"))
        )

        with pytest.raises(BrowserError, match="Navigation failed"):
            backend.navigate("https://example.com")

    @pytest.mark.parametrize(
        "exc",
        [RuntimeError("already stopped"), OSError("process died")],
        ids=["RuntimeError", "OSError"],
    )
    def test_stop_handles_browser_stop_errors(self, exc: Exception) -> None:
        backend = with_private_loop(NoDriverBackend())
        backend._started = True
        backend._browser = _stoppable_browser(stop=MagicMock(side_effect=exc))
        backend._page = MagicMock()

        backend.stop()

        assert backend.is_running is False

    def test_stop_logs_debug_for_expected_errors(self, captured_logs: list[dict[str, Any]]) -> None:
        """stop() logs DEBUG for expected cleanup errors like 'browser is already closed'."""
        backend = with_private_loop(NoDriverBackend())
        backend._started = True
        backend._browser = _stoppable_browser(
            stop=MagicMock(side_effect=RuntimeError("browser is already closed"))
        )

        backend.stop()

        assert [(e["event"], e["log_level"]) for e in captured_logs if "stop_" in e["event"]] == [
            ("nodriver_backend_stop_expected", "debug")
        ]

    def test_stop_logs_warning_for_unexpected_errors(
        self, captured_logs: list[dict[str, Any]]
    ) -> None:
        """stop() logs WARNING for unexpected errors."""
        backend = with_private_loop(NoDriverBackend())
        backend._started = True
        backend._browser = _stoppable_browser(
            stop=MagicMock(side_effect=RuntimeError("unexpected error not in patterns"))
        )

        backend.stop()

        assert [(e["event"], e["log_level"]) for e in captured_logs if "stop_" in e["event"]] == [
            ("nodriver_backend_stop_unexpected", "warning")
        ]

    def test_navigate_auto_starts_browser(self, fake_chrome: FakeChrome) -> None:
        """navigate() auto-starts browser if not running."""
        backend = NoDriverBackend()

        backend.navigate("https://example.com/")

        assert backend.is_running is True
        assert backend.current_url == "https://example.com/"

    def test_set_cookies_logs_warning_on_failure(self) -> None:
        """set_cookies() returns 0 and logs a warning when the call cannot run."""
        backend = _dead_loop_backend()

        with capture_logs() as logs:
            result = backend.set_cookies([{"name": "test", "value": "value"}])

        assert result == 0
        assert [e["event"] for e in logs if e["log_level"] == "warning"] == [
            "nodriver_backend_cookie_set_failed"
        ]

    def test_delete_cookies_logs_warning_on_failure(self) -> None:
        """delete_all_cookies() returns False and logs a warning when the call cannot run."""
        backend = _dead_loop_backend()

        with capture_logs() as logs:
            result = backend.delete_all_cookies()

        assert result is False
        assert [e["event"] for e in logs if e["log_level"] == "warning"] == [
            "nodriver_backend_delete_cookies_failed"
        ]

    def test_current_url_returns_empty_on_exception(self) -> None:
        assert _dead_loop_backend().current_url == ""

    def test_page_title_returns_empty_on_exception(self) -> None:
        assert _dead_loop_backend().page_title == ""

    def test_page_source_returns_empty_on_exception(self) -> None:
        assert _dead_loop_backend().page_source == ""

    def test_get_user_agent_returns_empty_on_exception(self) -> None:
        assert _dead_loop_backend().get_user_agent() == ""


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

    def test_is_running_false_when_browser_none_but_started_true(self) -> None:
        """is_running returns False if browser is None even if _started is True."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()

        assert backend.is_running is True

        # Simulate browser becoming None (crash scenario)
        backend._browser = None

        assert backend.is_running is False

    def test_start_updates_additional_options(self, fake_chrome: FakeChrome) -> None:
        """start() merges additional options into _options."""
        backend = NoDriverBackend(browser_args=["--disable-gpu"])
        backend.start(custom_arg="custom_value")

        assert backend._options.get("custom_arg") == "custom_value"
        assert backend._options.get("browser_args") == ["--disable-gpu"]  # Original preserved

    def test_delete_all_cookies_empties_a_started_browsers_jar(
        self, fake_chrome: FakeChrome
    ) -> None:
        backend = NoDriverBackend()
        backend.start()
        backend.set_cookies([{"name": "a", "value": "1", "domain": "example.com"}])

        assert backend.delete_all_cookies() is True
        assert backend.get_cookies() == []

    def test_delete_all_cookies_safe_when_not_running(self) -> None:
        """delete_all_cookies() is safe when not running."""
        backend = NoDriverBackend()
        # Should not raise
        backend.delete_all_cookies()

    def test_context_manager_propagates_exceptions(self, fake_chrome: FakeChrome) -> None:
        """Context manager does not suppress exceptions from within the block."""
        with pytest.raises(ValueError, match="test error"), NoDriverBackend():
            raise ValueError("test error")

    def test_start_overrides_headless(self, fake_chrome: FakeChrome) -> None:
        """start() can override headless setting."""
        backend = NoDriverBackend(headless=True)
        backend.start(headless=False)

        assert backend._headless is False
        assert fake_chrome.browsers[0].start_kwargs["headless"] is False

    def test_start_overrides_profile_dir(self, fake_chrome: FakeChrome, tmp_path: Path) -> None:
        """start() can override profile_dir setting."""
        profile = tmp_path / "override_profile"

        backend = NoDriverBackend()
        backend.start(profile_dir=profile)

        assert backend._profile_dir == profile
        assert fake_chrome.browsers[0].start_kwargs["user_data_dir"] == str(profile)

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

    def test_get_cookies_returns_empty_on_exception(self) -> None:
        """get_cookies() returns empty list when the call cannot run."""
        assert _dead_loop_backend().get_cookies() == []

    def test_properties_return_empty_when_page_none_but_running(self) -> None:
        """Properties return empty values if _page is None despite running state."""
        backend = with_private_loop(NoDriverBackend())
        backend._started = True
        backend._browser = MagicMock()
        backend._page = None  # Page is None but browser is running

        # The async methods check `if self._page is None: return ""`
        assert backend.current_url == ""
        assert backend.page_title == ""
        assert backend.page_source == ""
        assert backend.get_user_agent() == ""

    def test_delete_all_cookies_safe_when_page_none(
        self, captured_logs: list[dict[str, Any]]
    ) -> None:
        """delete_all_cookies() is safe when _page is None."""
        backend = with_private_loop(NoDriverBackend())
        backend._started = True
        backend._browser = MagicMock()
        backend._page = None

        assert backend.delete_all_cookies() is True

        assert [e["event"] for e in captured_logs] == ["nodriver_delete_cookies_no_page"]


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

    @pytest.mark.parametrize(
        ("url", "error"),
        [("", "Invalid URL"), ("not-a-valid-url", "invalid argument: 'url' must be a valid URL")],
        ids=["empty", "malformed"],
    )
    def test_a_driver_rejection_raises_browser_error(self, url: str, error: str) -> None:
        """The driver validates URLs; its rejection surfaces as BrowserError."""
        backend = with_private_loop(NoDriverBackend())
        backend._started = True
        backend._browser = _stoppable_browser(get=AsyncMock(side_effect=RuntimeError(error)))

        with pytest.raises(BrowserError, match="Navigation failed"):
            backend.navigate(url)


class TestNoDriverBackendAsyncContextLimitation:
    """The one place a sync call cannot work: the browser's own event loop."""

    def test_the_docstring_documents_the_event_loop_model(self) -> None:
        doc = NoDriverBackend.__doc__ or ""
        assert "graftpunk-nodriver-loop" in doc
        assert "deadlock" in doc

    def test_run_async_runs_the_coroutine_on_the_private_loop_thread(self) -> None:
        async def thread_name() -> str:
            return threading.current_thread().name

        backend = with_private_loop(NoDriverBackend())

        assert backend._run_async(thread_name()) == "graftpunk-nodriver-loop"

    async def test_run_async_raises_error_on_the_browsers_own_loop(
        self, fake_chrome: FakeChrome
    ) -> None:
        """Waiting on the loop from inside it would deadlock, so it raises instead."""
        backend = NoDriverBackend()
        await backend._start_async()
        backend._started = True

        async def dummy() -> str:
            return "result"

        try:
            with pytest.raises(RuntimeError, match="cannot be used from within an async context"):
                backend._run_async(dummy())
        finally:
            await backend.stop_async()


class TestNoDriverNavigateErrorTypes:
    """Tests for different error types during navigate."""

    @pytest.mark.parametrize(
        "exc",
        [ConnectionError("connection refused"), TimeoutError("operation timed out")],
        ids=["ConnectionError", "TimeoutError"],
    )
    def test_a_transport_error_raises_browser_error(self, exc: Exception) -> None:
        backend = with_private_loop(NoDriverBackend())
        backend._started = True
        backend._browser = _stoppable_browser(get=AsyncMock(side_effect=exc))

        with pytest.raises(BrowserError, match="Navigation failed"):
            backend.navigate("https://example.com")


class TestNoDriverExpectedStopPatterns:
    """Parameterized tests for all _EXPECTED_STOP_PATTERNS."""

    @pytest.mark.parametrize(
        "pattern",
        [
            "cannot schedule new futures after shutdown",
            "browser is already closed",
            "no such process found",
            "event loop is closed",
            "target closed unexpectedly",
            "NoDriverBackend call was cancelled (browser stopping)",
        ],
    )
    def test_expected_stop_pattern_detection(self, pattern: str) -> None:
        """All expected stop patterns are correctly detected."""
        backend = NoDriverBackend()
        assert backend._is_expected_stop_error(pattern) is True

    @pytest.mark.parametrize(
        "pattern",
        [
            "Cannot schedule",  # Different case
            "BROWSER IS ALREADY CLOSED",  # Uppercase
            "Event Loop Is Closed",  # Mixed case
        ],
    )
    def test_expected_stop_pattern_case_insensitive(self, pattern: str) -> None:
        """Pattern matching is case-insensitive."""
        backend = NoDriverBackend()
        assert backend._is_expected_stop_error(pattern) is True

    @pytest.mark.parametrize(
        "pattern",
        [
            "random error message",
            "disk full",
            "permission denied",
            "something went wrong",
        ],
    )
    def test_unexpected_stop_pattern_detection(self, pattern: str) -> None:
        """Unexpected patterns are correctly identified."""
        backend = NoDriverBackend()
        assert backend._is_expected_stop_error(pattern) is False


class TestNoDriverBackendStopReap:
    """Tests that NoDriverBackend.stop() reaps the Chrome subprocess.

    nodriver's Browser.stop() sends SIGTERM but never waits for the
    subprocess, leaving Chrome as a zombie under the live python parent. The
    backend's _stop_async() compensates by waiting, after browser.stop(), for
    the loop's child watcher to collect the exit status
    (_reap_browser_process).
    """

    class _Proc:
        """An asyncio subprocess stand-in whose watcher collects the exit status
        (sets ``returncode``) once SIGTERM or SIGKILL has made it exit."""

        def __init__(
            self, *, exits_on_term: bool, exits_on_kill: bool, kill_raises: OSError | None
        ) -> None:
            self.pid = 12345
            self._exits_on_term = exits_on_term
            self._exits_on_kill = exits_on_kill
            self._kill_raises = kill_raises
            self.kill_called = False

        @property
        def returncode(self) -> int | None:
            if self._exits_on_term:
                return 0
            if self.kill_called and self._exits_on_kill:
                return -9
            return None

        def kill(self) -> None:
            self.kill_called = True
            if self._kill_raises is not None:
                raise self._kill_raises

    @classmethod
    def _make_proc(
        cls,
        exits_on_term: bool = True,
        exits_on_kill: bool = True,
        kill_raises: OSError | None = None,
    ) -> "TestNoDriverBackendStopReap._Proc":
        """Build a fake subprocess.

        kill_raises: if not None, .kill() raises this OSError. Typed as
            OSError because the helper only handles ProcessLookupError and
            other OSError subclasses on the kill path.
        """
        return cls._Proc(
            exits_on_term=exits_on_term, exits_on_kill=exits_on_kill, kill_raises=kill_raises
        )

    @staticmethod
    def _make_browser(proc: Any) -> MagicMock:
        """Build a MagicMock Browser whose .stop() nulls _process, as
        nodriver's stop() does when terminate() and kill() both fail."""
        browser = MagicMock()
        browser._process = proc

        def fake_stop():
            browser._process = None

        browser.stop = fake_stop
        return browser

    @staticmethod
    def _tight_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("graftpunk.backends.nodriver._REAP_TERM_TIMEOUT_S", 0.05)
        monkeypatch.setattr("graftpunk.backends.nodriver._REAP_KILL_TIMEOUT_S", 0.05)
        monkeypatch.setattr("graftpunk.backends.nodriver._EXIT_POLL_S", 0.01)

    async def test_stop_async_reaps_process(self) -> None:
        """A Chrome that exits on SIGTERM is collected without a SIGKILL."""
        proc = self._make_proc()
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._make_browser(proc)

        with capture_logs() as cap:
            await backend._stop_async()

        assert proc.returncode == 0
        assert proc.kill_called is False, "kill() should not be needed on success"
        assert not any(e["event"] == "chrome_sigterm_timeout_escalating_to_sigkill" for e in cap)

    async def test_stop_async_no_op_when_no_browser(self) -> None:
        """_stop_async returns immediately when _browser is None — helper
        is not invoked at all. Regression test for the early-return guard."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = None

        # Should not raise (no proc to read, helper not called).
        await backend._stop_async()

    async def test_stop_async_handles_browser_with_no_process(self) -> None:
        """If start() raised between Browser construction and subprocess
        spawn, _browser._process is None. Helper must no-op cleanly."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._make_browser(proc=None)

        # Should not raise.
        await backend._stop_async()

    async def test_stop_async_captures_proc_before_browser_stop_nulls_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_stop_async must capture _browser._process BEFORE calling stop().

        nodriver's Browser.stop() resets self._process = None inside its
        cleanup loop when terminate() and kill() both fail. If our backend
        read _process AFTER stop(), it would then get None and skip the reap,
        so a Chrome that ignores SIGTERM would never get the SIGKILL.
        """
        self._tight_timeouts(monkeypatch)
        proc = self._make_proc(exits_on_term=False)
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._make_browser(proc)

        await backend._stop_async()

        assert proc.kill_called is True
        assert proc.returncode == -9

    async def test_stop_async_escalates_to_sigkill_on_term_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When SIGTERM times out, helper escalates to SIGKILL and re-waits."""
        self._tight_timeouts(monkeypatch)
        proc = self._make_proc(exits_on_term=False)
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._make_browser(proc)

        with capture_logs() as cap:
            await backend._stop_async()

        assert proc.kill_called is True, "kill() should have been called after SIGTERM timeout"
        assert proc.returncode == -9
        assert not any(e["event"] == "chrome_failed_to_exit_after_sigkill" for e in cap)

    async def test_stop_async_logs_when_sigkill_also_times_out(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When even SIGKILL is not collected, log a warning and return."""
        self._tight_timeouts(monkeypatch)
        # Never collected: simulates a kernel-level wedge.
        proc = self._make_proc(exits_on_term=False, exits_on_kill=False)
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._make_browser(proc)

        with capture_logs() as cap:
            await backend._stop_async()

        assert proc.kill_called is True
        # capture_logs() returns event_dicts where 'log_level' is the level
        # key (structlog's testing processor normalises method name → log_level)
        # and 'event' is the event string.
        assert any(
            e.get("event") == "chrome_failed_to_exit_after_sigkill"
            and e.get("pid") == proc.pid
            and e.get("log_level") == "warning"
            for e in cap
        ), f"expected warning event in captured logs: {cap}"

    async def test_stop_async_handles_process_lookup_race(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ProcessLookupError on kill() is a successful reap (race-tight).

        The SIGTERM wait timed out; Chrome exited in the window between the
        timeout and our kill() call, so kill() raises ProcessLookupError
        because the PID is gone. That is success: no second wait and no
        warning.
        """
        self._tight_timeouts(monkeypatch)
        proc = self._make_proc(
            exits_on_term=False,
            exits_on_kill=False,
            kill_raises=ProcessLookupError(3, "No such process"),
        )
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._make_browser(proc)

        with capture_logs() as cap:
            await backend._stop_async()

        assert proc.kill_called is True
        assert [e for e in cap if e["log_level"] == "warning"] == []

    def test_sync_stop_also_reaps_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The sync stop() path inherits the reap via _run_async->_stop_async.

        Regression guard against a future refactor that splits the sync and
        async stop paths and forgets to wire the reap into both.
        """
        self._tight_timeouts(monkeypatch)
        proc = self._make_proc(exits_on_term=False)
        backend = with_private_loop(NoDriverBackend())
        backend._started = True
        backend._browser = self._make_browser(proc)

        backend.stop()  # sync path

        assert proc.kill_called is True, "sync stop() should also reap via _stop_async"

    async def test_stop_async_public_api_reaps_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The public async stop_async() inherits the reap via _stop_async.

        Symmetric regression guard to test_sync_stop_also_reaps_process —
        BrowserSession.__aexit__ calls stop_async() (not _stop_async()),
        so the public path is production-critical.
        """
        self._tight_timeouts(monkeypatch)
        proc = self._make_proc(exits_on_term=False)
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._make_browser(proc)

        await backend.stop_async()

        assert proc.kill_called is True, "stop_async() should reap via _stop_async"

    async def test_stop_async_still_reaps_when_browser_stop_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If browser.stop() raises a handled error, the reap still runs.

        Locks the documented edge case from _stop_async's inline comment:
        even when browser.stop() raises (and is swallowed by
        _handle_stop_error), the try/finally must still drive
        _reap_browser_process so the subprocess is collected. Without the
        finally, a stop()-side exception would leak the very subprocess
        the reap exists to collect.
        """
        self._tight_timeouts(monkeypatch)
        # No SIGTERM was sent because stop() raised, so only the SIGKILL
        # escalation ends it.
        proc = self._make_proc(exits_on_term=False)
        backend = NoDriverBackend()
        backend._started = True
        browser = self._make_browser(proc)

        def raising_stop() -> None:
            raise RuntimeError("browser is already closed")

        browser.stop = raising_stop
        backend._browser = browser

        # Should not raise (RuntimeError caught by _handle_stop_error).
        await backend._stop_async()

        assert proc.kill_called is True, (
            "SIGKILL escalation should reap the process when browser.stop() raised"
        )
        assert proc.returncode == -9

    @pytest.mark.skipif(os.name != "posix", reason="SIGTERM handler in the stand-in")
    async def test_the_reap_does_not_wait_for_helpers_holding_chromes_pipes(self) -> None:
        """A Chrome stand-in exits on SIGTERM at once while a helper it spawned
        keeps its pipes open for 6 s: the reap returns once the exit status is
        collected, with no SIGKILL escalation."""
        import sys
        import time

        from graftpunk.backends.nodriver import _reap_browser_process

        child = (
            "import signal, subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(6)'])\n"
            "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
            "print('ready', flush=True)\n"
            "time.sleep(30)\n"
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            child,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert proc.stdout is not None
            await proc.stdout.readline()
            proc.terminate()
            began = time.monotonic()
            with capture_logs() as cap:
                await _reap_browser_process(proc)
            took = time.monotonic() - began
        finally:
            if proc.returncode is None:
                proc.kill()
            proc._transport.close()  # type: ignore[attr-defined]

        assert proc.returncode == 0
        assert took < 1.5
        assert [e["event"] for e in cap] == []


class TestNoDriverBackendOrphanCleanup:
    """The start path marks its own Chrome and clears the ones earlier runs left (#96)."""

    @staticmethod
    def _fake_nodriver(record: list) -> MagicMock:
        """A stand-in nodriver module whose start() records what it was given."""
        mock_uc = MagicMock()

        async def fake_start(**kwargs: object) -> MagicMock:
            record.append(kwargs)
            browser = MagicMock()
            browser.get = AsyncMock(return_value=MagicMock())
            # No subprocess to reap: an auto-created MagicMock here is not
            # awaitable and the stop path awaits proc.wait().
            browser._process = None
            return browser

        mock_uc.start = fake_start
        return mock_uc

    async def test_the_owner_marker_rides_in_browser_args(self) -> None:
        started: list = []
        backend = NoDriverBackend()

        with patch.dict("sys.modules", {"nodriver": self._fake_nodriver(started)}):
            await backend._start_async()

        (kwargs,) = started
        assert kwargs["browser_args"][:2] == ["--test-type", f"{OWNER_SWITCH}={os.getpid()}"]

    async def test_configured_browser_args_follow_the_shared_ones(self) -> None:
        started: list = []
        backend = NoDriverBackend(browser_args=["--lang=en-US"])

        with patch.dict("sys.modules", {"nodriver": self._fake_nodriver(started)}):
            await backend._start_async()

        assert started[0]["browser_args"][:-1] == [*base_browser_args(), "--lang=en-US"]

    async def test_each_start_marks_its_launch_with_a_switch_of_its_own(self) -> None:
        started: list = []

        with patch.dict("sys.modules", {"nodriver": self._fake_nodriver(started)}):
            await NoDriverBackend()._start_async()
            await NoDriverBackend()._start_async()

        marks = [kwargs["browser_args"][-1] for kwargs in started]
        assert all(m.startswith("--graftpunk-launch-id=") for m in marks)
        assert marks[0] != marks[1]

    async def test_the_sweep_runs_before_the_browser_starts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reaping after the launch would leave the new Chrome competing with the old."""
        from graftpunk.browser_launch import CleanupReport

        order: list[str] = []

        def fake_prepare() -> CleanupReport:
            order.append("sweep")
            return CleanupReport()

        monkeypatch.setattr("graftpunk.backends.nodriver.prepare_browser_launch", fake_prepare)
        mock_uc = MagicMock()

        async def fake_start(**kwargs: object) -> MagicMock:
            order.append("start")
            browser = MagicMock()
            browser.get = AsyncMock(return_value=MagicMock())
            return browser

        mock_uc.start = fake_start
        backend = NoDriverBackend()

        with patch.dict("sys.modules", {"nodriver": mock_uc}):
            await backend._start_async()

        assert order == ["sweep", "start"]

    async def test_a_started_backend_is_in_the_signal_registry(self) -> None:
        from graftpunk.signals import live_browsers

        backend = NoDriverBackend()
        try:
            with patch.dict("sys.modules", {"nodriver": self._fake_nodriver([])}):
                await backend._start_async()

            assert backend in live_browsers()
        finally:
            backend._reset_state()

    async def test_the_sweep_runs_once_across_a_connect_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sweep sits outside the retry loop: one pass, however many attempts."""
        from graftpunk.browser_launch import CleanupReport

        sweeps: list[str] = []

        def fake_prepare() -> CleanupReport:
            sweeps.append("sweep")
            return CleanupReport()

        monkeypatch.setattr("graftpunk.backends.nodriver.prepare_browser_launch", fake_prepare)

        async def no_wait(_delay: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", no_wait)
        attempts: list[int] = []
        mock_uc = MagicMock()

        async def fake_start(**kwargs: object) -> MagicMock:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("Failed to connect to browser")
            browser = MagicMock()
            browser.get = AsyncMock(return_value=MagicMock())
            browser._process = None
            return browser

        mock_uc.start = fake_start
        backend = NoDriverBackend()

        with patch.dict("sys.modules", {"nodriver": mock_uc}):
            await backend._start_async()

        assert len(attempts) == 2
        assert sweeps == ["sweep"]

    async def test_the_start_path_arms_the_termination_handlers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Arming has to happen on this thread, not inside the worker sweep.

        ``signal.signal`` raises off the main thread and the installer logs
        that at debug, so arming from the sweep would leave the process with
        no handlers and no error.
        """
        import signal as signal_module

        from graftpunk import chrome_orphans, signals
        from graftpunk.browser_launch import CleanupReport

        monkeypatch.setattr(chrome_orphans, "_ARMED", True)
        # The sweep itself is replaced at its call site, so an armed pass in
        # this test can never reach the real process table.
        monkeypatch.setattr(
            "graftpunk.backends.nodriver.prepare_browser_launch", lambda: CleanupReport()
        )
        monkeypatch.setattr(signals, "auto_install", True)
        signal_module.signal(signal_module.SIGTERM, signal_module.SIG_DFL)
        backend = NoDriverBackend()

        try:
            with patch.dict("sys.modules", {"nodriver": self._fake_nodriver([])}):
                await backend._start_async()

            assert signal_module.getsignal(signal_module.SIGTERM) is signals._handle_termination
        finally:
            backend._reset_state()

    async def test_stopping_leaves_the_signal_registry(self) -> None:
        from graftpunk.signals import live_browsers

        backend = NoDriverBackend()
        with patch.dict("sys.modules", {"nodriver": self._fake_nodriver([])}):
            await backend._start_async()
        backend._started = True

        await backend.stop_async()

        assert backend not in live_browsers()


class TestNoDriverBackendFailedStartAttempts:
    """A start attempt that half succeeded used to leave its Chrome running (#192)."""

    @staticmethod
    def _browser(profile: Path, pid: int = 4242) -> MagicMock:
        """A browser that starts fine and then fails its first CDP call."""
        browser = MagicMock()
        browser._process = MagicMock()
        browser._process.pid = pid
        browser.config.uses_custom_data_dir = False
        browser.config.user_data_dir = str(profile)
        browser.get = AsyncMock(side_effect=RuntimeError("Failed to connect to browser"))
        return browser

    async def test_the_browser_a_failed_attempt_left_is_ended_and_its_profile_removed(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """uc.start() returns a live browser; the failure comes from the first get()."""
        signalled: list = []
        monkeypatch.setattr(
            "graftpunk.backends.nodriver.terminate_nodriver_browser", signalled.append
        )

        async def no_wait(_delay: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", no_wait)

        first_profile = temp_profiles / "uc_first"
        (first_profile / "Default").mkdir(parents=True)
        first = self._browser(first_profile)
        browsers: list[MagicMock] = []

        async def fake_start(**kwargs: object) -> MagicMock:
            if not browsers:
                browsers.append(first)
                return first
            good = MagicMock()
            good._process = None
            good.get = AsyncMock(return_value=MagicMock())
            good.config.uses_custom_data_dir = False
            good.config.user_data_dir = None
            browsers.append(good)
            return good

        mock_uc = MagicMock()
        mock_uc.start = fake_start
        backend = NoDriverBackend()

        try:
            with patch.dict("sys.modules", {"nodriver": mock_uc}):
                await backend._start_async()
        finally:
            backend._reset_state()

        assert signalled == [first]
        assert not first_profile.exists()

    async def test_the_last_failed_attempt_is_released_before_the_error_is_raised(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Three failures used to leave three Chromes and three directories behind."""
        signalled: list = []
        monkeypatch.setattr(
            "graftpunk.backends.nodriver.terminate_nodriver_browser", signalled.append
        )

        async def no_wait(_delay: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", no_wait)

        profiles = []
        for index in range(3):
            profile = temp_profiles / f"uc_attempt{index}"
            (profile / "Default").mkdir(parents=True)
            profiles.append(profile)
        started = iter(self._browser(profile) for profile in profiles)

        mock_uc = MagicMock()
        mock_uc.start = AsyncMock(side_effect=lambda **kwargs: next(started))
        backend = NoDriverBackend()

        with (
            patch.dict("sys.modules", {"nodriver": mock_uc}),
            pytest.raises(BrowserError, match="Failed to connect to browser after 3 attempts"),
        ):
            await backend._start_async()

        assert len(signalled) == 3
        assert [profile.exists() for profile in profiles] == [False, False, False]
        assert backend._browser is None

    async def test_a_failed_attempt_leaves_the_signal_registry(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The handle is registered between the start and the first get(), so it must go."""
        from graftpunk.signals import live_browsers

        monkeypatch.setattr(
            "graftpunk.backends.nodriver.terminate_nodriver_browser", lambda _b: None
        )

        async def no_wait(_delay: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", no_wait)

        profile = temp_profiles / "uc_registered"
        profile.mkdir()
        mock_uc = MagicMock()
        mock_uc.start = AsyncMock(side_effect=lambda **kwargs: self._browser(profile))
        backend = NoDriverBackend()

        with (
            patch.dict("sys.modules", {"nodriver": mock_uc}),
            pytest.raises(BrowserError),
        ):
            await backend._start_async()

        assert backend not in live_browsers()


class TestNoDriverBackendTempProfileCleanup:
    """Every stop used to leak the temp profile nodriver made for it (#96)."""

    @staticmethod
    def _browser(profile: Path | None, *, custom: bool) -> MagicMock:
        browser = MagicMock()
        browser._process = None
        browser.config.uses_custom_data_dir = custom
        browser.config.user_data_dir = str(profile) if profile is not None else None
        browser.stop = MagicMock()
        return browser

    async def test_a_temp_profile_is_removed_on_stop(self, temp_profiles: Path) -> None:
        profile = temp_profiles / "uc_abc"
        (profile / "Default").mkdir(parents=True)
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._browser(profile, custom=False)

        await backend._stop_async()

        assert not profile.exists()

    async def test_a_custom_profile_dir_survives_stop(self, temp_profiles: Path) -> None:
        """A plugin's persistent profile is the user's data, never ours to delete."""
        profile = temp_profiles / "uc_persistent"
        profile.mkdir()
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._browser(profile, custom=True)

        await backend._stop_async()

        assert profile.exists()

    async def test_a_browser_with_no_profile_recorded_is_harmless(self) -> None:
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._browser(None, custom=False)

        with capture_logs() as logs:
            await backend._stop_async()

        assert [entry for entry in logs if entry["event"] == "chrome_temp_profile_removed"] == []

    async def test_a_stop_that_raises_still_removes_the_temp_profile(
        self, temp_profiles: Path
    ) -> None:
        """The removal sits in the same finally as the reap, for the same reason."""
        profile = temp_profiles / "uc_raised"
        (profile / "Default").mkdir(parents=True)
        backend = NoDriverBackend()
        backend._started = True
        browser = self._browser(profile, custom=False)
        browser.stop = MagicMock(side_effect=ValueError("nodriver blew up"))
        backend._browser = browser

        with pytest.raises(ValueError, match="nodriver blew up"):
            await backend._stop_async()

        assert not profile.exists()

    async def test_a_reap_that_raises_still_removes_the_temp_profile(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The removal is nested behind the reap, so a broken reap does not cost it."""
        profile = temp_profiles / "uc_reap_raised"
        (profile / "Default").mkdir(parents=True)

        async def boom(_proc: object) -> None:
            raise RuntimeError("the reap blew up")

        monkeypatch.setattr("graftpunk.backends.nodriver._reap_browser_process", boom)
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._browser(profile, custom=False)

        with pytest.raises(RuntimeError, match="the reap blew up"):
            await backend._stop_async()

        assert not profile.exists()


class TestNoDriverBackendTerminateForSignal:
    """The loop-free path the signal handler takes (#96)."""

    @staticmethod
    def _browser(profile: Path, pid: int = 4242) -> MagicMock:
        browser = MagicMock()
        browser._process = MagicMock()
        browser._process.pid = pid
        browser.config.uses_custom_data_dir = False
        browser.config.user_data_dir = str(profile)
        return browser

    def test_it_delegates_the_signal_to_the_shared_helper(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One implementation of the sequence, shared with the observe handle."""
        seen: list = []
        monkeypatch.setattr("graftpunk.backends.nodriver.terminate_nodriver_browser", seen.append)
        profile = temp_profiles / "uc_live"
        profile.mkdir()
        backend = NoDriverBackend()
        backend._browser = self._browser(profile)
        backend._started = True
        browser = backend._browser

        backend._terminate_for_signal()

        assert seen == [browser]

    def test_it_leaves_the_temp_profile_for_the_stale_sweep(self, temp_profiles: Path) -> None:
        """A handler does bounded work: no rmtree between the signal and the exit."""
        profile = temp_profiles / "uc_live"
        profile.mkdir()
        backend = NoDriverBackend()
        backend._browser = self._browser(profile)
        backend._started = True

        backend._terminate_for_signal()

        assert profile.exists()

    def test_it_leaves_the_backend_coherent(self, temp_profiles: Path) -> None:
        """is_running must not claim a browser this method just ended."""
        from graftpunk.signals import live_browsers, register_live_browser

        profile = temp_profiles / "uc_live"
        profile.mkdir()
        backend = NoDriverBackend()
        backend._browser = self._browser(profile)
        backend._started = True
        register_live_browser(backend)

        backend._terminate_for_signal()

        assert backend.is_running is False
        assert backend._browser is None
        assert backend not in live_browsers()

    def test_no_browser_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list = []
        monkeypatch.setattr("graftpunk.backends.nodriver.terminate_nodriver_browser", seen.append)
        backend = NoDriverBackend()

        backend._terminate_for_signal()

        assert seen == []
        assert backend.is_running is False

    def test_the_backend_satisfies_the_protocol(self) -> None:
        from graftpunk.signals import TerminatableBrowser

        assert isinstance(NoDriverBackend(), TerminatableBrowser)


class TestKeepOrphanedChromeSetting:
    """The opt-out, for users who deliberately keep detached browsers (#96)."""

    def test_the_default_is_to_clean_up(self) -> None:
        from graftpunk.config import get_settings

        assert get_settings().keep_orphaned_chrome is False

    def test_the_environment_variable_turns_it_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from graftpunk.config import get_settings, reset_settings

        monkeypatch.setenv("GRAFTPUNK_KEEP_ORPHANED_CHROME", "1")
        reset_settings()

        assert get_settings().keep_orphaned_chrome is True


def _backend_with_tab(tab: FakeCdpTab | None) -> NoDriverBackend:
    backend = with_private_loop(NoDriverBackend())
    backend._started = True
    backend._browser = MagicMock()
    backend._page = tab
    return backend


def _warnings(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e["log_level"] == "warning"]


class TestNoDriverCookiesThroughCdp:
    """Cookie read and write go through CDP Storage commands (#193).

    nodriver.Tab has no get_cookies() or set_cookies(), so the backend's old
    calls to them always failed; these tests run the real coroutines through
    the backend's ``_run_async`` against a fake tab.
    """

    SECRET = "s3cr3t-value-do-not-log"  # noqa: S105 - a marker the log must never contain

    def test_set_then_get_round_trips_every_field(self) -> None:
        backend = _backend_with_tab(FakeCdpTab())
        persistent: Cookie = {
            "name": "sid",
            "value": self.SECRET,
            "domain": "myshop.example",
            "path": "/account",
            "secure": True,
            "httponly": True,
            "sameSite": "Lax",
            "expires": 1893456000.0,
        }
        session_cookie: Cookie = {
            "name": "pref",
            "value": "dark",
            "domain": ".example.com",
            "secure": False,
            "httponly": False,
        }

        assert backend.set_cookies([persistent, session_cookie]) == 2

        by_name = {c["name"]: c for c in backend.get_cookies()}
        assert by_name["sid"] == {
            "name": "sid",
            "value": self.SECRET,
            "domain": ".myshop.example",
            "path": "/account",
            "secure": True,
            "httponly": True,
            "sameSite": "Lax",
            "expires": 1893456000.0,
        }
        # A session cookie has no expires, and path defaults to "/".
        assert by_name["pref"] == {
            "name": "pref",
            "value": "dark",
            "domain": ".example.com",
            "path": "/",
            "secure": False,
            "httponly": False,
        }

    def test_same_site_is_accepted_in_any_case(self) -> None:
        backend = _backend_with_tab(FakeCdpTab())

        count = backend.set_cookies(
            [{"name": "a", "value": "1", "domain": "example.com", "sameSite": "strict"}]
        )

        assert count == 1
        assert backend.get_cookies()[0]["sameSite"] == "Strict"

    def test_set_counts_only_cookies_present_and_warns_with_missing_names(
        self, captured_logs: list[dict[str, Any]]
    ) -> None:
        tab = FakeCdpTab()
        tab.reject = {"blocked"}
        backend = _backend_with_tab(tab)

        count = backend.set_cookies(
            [
                {"name": "kept", "value": "1", "domain": "example.com"},
                {"name": "blocked", "value": self.SECRET, "domain": "example.com"},
            ]
        )

        assert count == 1
        assert [c["name"] for c in backend.get_cookies()] == ["kept"]
        [warning] = _warnings(captured_logs)
        assert warning["event"] == "nodriver_set_cookies_incomplete"
        assert warning["missing_names"] == ["blocked"]
        assert warning["requested"] == 2
        assert warning["present"] == 1

    def test_the_incomplete_warning_counts_only_the_cookies_sent(
        self, captured_logs: list[dict[str, Any]]
    ) -> None:
        tab = FakeCdpTab()
        tab.reject = {"blocked"}
        backend = _backend_with_tab(tab)

        count = backend.set_cookies(
            [
                {"name": "kept", "value": "1", "domain": "example.com"},
                {"name": "blocked", "value": "2", "domain": "example.com"},
                {"name": "nodomain", "value": "3"},
            ]
        )

        assert count == 1
        skipped, incomplete = _warnings(captured_logs)
        assert skipped["event"] == "nodriver_set_cookies_skipped"
        assert incomplete["event"] == "nodriver_set_cookies_incomplete"
        assert incomplete["requested"] == 2
        assert incomplete["requested"] - incomplete["present"] == len(incomplete["missing_names"])

    def test_an_empty_path_is_set_and_counted_at_the_root(
        self, captured_logs: list[dict[str, Any]]
    ) -> None:
        tab = FakeCdpTab()
        backend = _backend_with_tab(tab)

        count = backend.set_cookies(
            [{"name": "a", "value": "1", "domain": "example.com", "path": ""}]
        )

        assert count == 1
        assert [c["path"] for c in tab.jar] == ["/"]
        assert _warnings(captured_logs) == []

    def test_a_cookie_without_a_domain_is_skipped_with_a_warning(
        self, captured_logs: list[dict[str, Any]]
    ) -> None:
        tab = FakeCdpTab()
        backend = _backend_with_tab(tab)

        count = backend.set_cookies(
            [
                {"name": "nodomain", "value": self.SECRET},
                {"name": "ok", "value": "1", "domain": "example.com"},
            ]
        )

        assert count == 1
        assert [c["name"] for c in tab.jar] == ["ok"]
        [warning] = _warnings(captured_logs)
        assert warning["event"] == "nodriver_set_cookies_skipped"
        assert warning["cookie_name"] == "nodomain"
        assert warning["reason"] == "no domain"

    def test_an_unknown_same_site_is_skipped_with_a_warning(
        self, captured_logs: list[dict[str, Any]]
    ) -> None:
        backend = _backend_with_tab(FakeCdpTab())

        count = backend.set_cookies(
            [{"name": "odd", "value": "1", "domain": "example.com", "sameSite": "sometimes"}]
        )

        assert count == 0
        [warning] = _warnings(captured_logs)
        assert warning["event"] == "nodriver_set_cookies_skipped"
        assert warning["cookie_name"] == "odd"

    def test_an_overwrite_the_browser_refused_is_not_counted(
        self, captured_logs: list[dict[str, Any]]
    ) -> None:
        """Chrome can keep the old cookie without an error; the count must say so."""
        tab = FakeCdpTab()
        backend = _backend_with_tab(tab)
        assert backend.set_cookies([{"name": "k", "value": "old", "domain": "example.com"}]) == 1
        tab.reject = {"k"}

        count = backend.set_cookies(
            [{"name": "k", "value": self.SECRET, "domain": "example.com", "sameSite": "None"}]
        )

        assert count == 0
        assert [(c["name"], c["value"]) for c in backend.get_cookies()] == [("k", "old")]
        [warning] = _warnings(captured_logs)
        assert warning["event"] == "nodriver_set_cookies_incomplete"
        assert warning["missing_names"] == ["k"]
        assert self.SECRET not in repr(captured_logs)

    @pytest.mark.parametrize(
        ("cookie", "reason"),
        [
            ({"value": "1", "domain": "example.com"}, "no name"),
            ({"name": "a", "domain": "example.com"}, "no value"),
            (
                {"name": "a", "value": "1", "domain": "example.com", "expires": "soon"},
                "expires is not a number",
            ),
            (
                {"name": "a", "value": "1", "domain": "example.com", "expires": float("nan")},
                "expires is not a finite number",
            ),
            (
                {"name": "a", "value": "1", "domain": "example.com", "expires": "inf"},
                "expires is not a finite number",
            ),
        ],
        ids=["no-name", "no-value", "expires-not-a-number", "expires-nan", "expires-inf"],
    )
    def test_a_malformed_cookie_is_skipped_with_a_warning(
        self, cookie: Cookie, reason: str, captured_logs: list[dict[str, Any]]
    ) -> None:
        tab = FakeCdpTab()
        backend = _backend_with_tab(tab)

        count = backend.set_cookies([cookie, {"name": "ok", "value": "1", "domain": "example.com"}])

        assert count == 1
        assert [c["name"] for c in tab.jar] == ["ok"]
        [warning] = _warnings(captured_logs)
        assert warning["event"] == "nodriver_set_cookies_skipped"
        assert warning["reason"] == reason

    def test_a_numeric_string_expires_is_accepted(self) -> None:
        backend = _backend_with_tab(FakeCdpTab())

        count = backend.set_cookies(
            [{"name": "a", "value": "1", "domain": "example.com", "expires": "1893456000"}]
        )

        assert count == 1
        assert backend.get_cookies()[0]["expires"] == 1893456000.0

    def test_no_page_get_returns_empty(self) -> None:
        assert _backend_with_tab(None).get_cookies() == []

    def test_no_page_set_returns_zero_with_the_no_page_warning(
        self, captured_logs: list[dict[str, Any]]
    ) -> None:
        backend = _backend_with_tab(None)

        assert backend.set_cookies([{"name": "a", "value": "1", "domain": "example.com"}]) == 0
        assert [e["event"] for e in _warnings(captured_logs)] == [
            "nodriver_backend_cookie_set_no_page"
        ]

    @pytest.mark.parametrize("kind", ["ProtocolException", "Exception"])
    def test_a_cdp_failure_warns_and_returns_nothing(
        self, kind: str, captured_logs: list[dict[str, Any]]
    ) -> None:
        from nodriver.core.connection import ProtocolException

        tab = FakeCdpTab()
        tab.fail_with = (
            ProtocolException({"code": -32602, "message": "Invalid cookie fields"})
            if kind == "ProtocolException"
            else Exception("socket gone")
        )
        backend = _backend_with_tab(tab)

        assert backend.set_cookies([{"name": "a", "value": "1", "domain": "example.com"}]) == 0
        assert backend.get_cookies() == []
        warnings = _warnings(captured_logs)
        assert [e["event"] for e in warnings] == [
            "nodriver_set_cookies_failed",
            "nodriver_get_cookies_failed",
        ]
        assert warnings[0]["cookie_names"] == ["a"]
        assert warnings[0]["exc_type"] == kind

    def test_delete_all_cookies_empties_the_jar(self) -> None:
        backend = _backend_with_tab(FakeCdpTab())
        backend.set_cookies([{"name": "a", "value": "1", "domain": "example.com"}])

        assert backend.delete_all_cookies() is True
        assert backend.get_cookies() == []

    def test_cookie_values_never_reach_the_log(self, captured_logs: list[dict[str, Any]]) -> None:
        tab = FakeCdpTab()
        tab.reject = {"rejected"}
        backend = _backend_with_tab(tab)
        backend.set_cookies(
            [
                {"name": "kept", "value": self.SECRET, "domain": "example.com"},
                {"name": "rejected", "value": self.SECRET, "domain": "example.com"},
                {"name": "nodomain", "value": self.SECRET},
            ]
        )
        backend.get_cookies()
        tab.fail_with = RuntimeError("cdp down")
        backend.set_cookies([{"name": "x", "value": self.SECRET, "domain": "example.com"}])
        backend._page = None
        backend.set_cookies([{"name": "y", "value": self.SECRET, "domain": "example.com"}])

        assert {e["event"] for e in captured_logs} >= {
            "nodriver_set_cookies_incomplete",
            "nodriver_set_cookies_skipped",
            "nodriver_set_cookies_failed",
            "nodriver_backend_cookie_set_no_page",
        }
        assert self.SECRET not in repr(captured_logs)


@contextmanager
def _loop_in_thread() -> Iterator[asyncio.AbstractEventLoop]:
    """A caller's event loop running in its own thread, like an async app's."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="caller-loop", daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


def _loop_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name == "graftpunk-nodriver-loop"}


class TestNoDriverBackendOneEventLoop:
    """Every sync call runs on the loop the browser was started on (#193).

    Each call used to run on a fresh ``asyncio.run()`` loop, so after a sync
    start() the websocket belonged to a closed loop and the next call hung.
    The fakes here fail loudly when used from a loop they were not made on.
    """

    def test_a_sync_start_and_every_later_sync_call_share_one_loop(
        self, fake_chrome: FakeChrome
    ) -> None:
        backend = NoDriverBackend()

        backend.start()
        backend.navigate("https://example.com/")
        assert backend.current_url == "https://example.com/"
        assert backend.page_title == "Example Domain"
        assert backend.page_source == "<html><body>Example Domain</body></html>"
        assert backend.get_user_agent() == "FakeChrome/1.0"
        assert backend.set_cookies([{"name": "a", "value": "1", "domain": "example.com"}]) == 1
        assert [c["name"] for c in backend.get_cookies()] == ["a"]
        assert backend.delete_all_cookies() is True
        assert backend.get_cookies() == []
        loop = backend._loop
        backend.stop()

        assert fake_chrome.misuse == []
        (browser,) = fake_chrome.browsers
        assert browser.loop is loop
        assert {tab.loop for tab in browser.tabs} == {loop}
        assert browser.stopped_on is loop

    def test_stop_ends_the_private_loop_thread(self, fake_chrome: FakeChrome) -> None:
        backend = NoDriverBackend()
        backend.start()
        assert backend._private_loop is not None
        thread = backend._private_loop.thread
        assert thread.name == "graftpunk-nodriver-loop"
        assert thread.daemon is True

        backend.stop()

        assert not thread.is_alive()
        assert backend._loop is None

    def test_a_failed_start_ends_the_private_loop_thread(self, fake_chrome: FakeChrome) -> None:
        fake_chrome.fail_with = RuntimeError("Chrome not found")
        backend = NoDriverBackend()
        before = _loop_threads()

        with pytest.raises(BrowserError):
            backend.start()

        assert _loop_threads() - before == set()
        assert backend._loop is None

    def test_a_backend_starts_again_on_a_fresh_loop_after_stop(
        self, fake_chrome: FakeChrome
    ) -> None:
        backend = NoDriverBackend()
        backend.start()
        backend.stop()

        backend.start()
        backend.navigate("https://example.com/")

        assert backend.current_url == "https://example.com/"
        assert fake_chrome.misuse == []
        first, second = fake_chrome.browsers
        assert first.loop is not second.loop
        backend.stop()

    def test_a_sync_call_from_inside_the_browsers_loop_raises(
        self, fake_chrome: FakeChrome
    ) -> None:
        """Waiting on the loop from inside it would deadlock, so it raises instead."""
        backend = NoDriverBackend()
        backend.start()
        assert backend._loop is not None

        async def navigate_from_inside() -> None:
            backend.navigate("https://example.com/")

        future = asyncio.run_coroutine_threadsafe(navigate_from_inside(), backend._loop)
        with pytest.raises(BrowserError, match="browser's own event loop"):
            future.result(5)

    def test_a_sync_call_from_a_different_running_loop_works(self, fake_chrome: FakeChrome) -> None:
        """A sync method called in an async app's handler runs on the browser's loop."""
        backend = NoDriverBackend()
        backend.start()
        backend.navigate("https://example.com/")

        async def handler() -> str:
            return backend.current_url

        assert asyncio.run(handler()) == "https://example.com/"
        assert fake_chrome.misuse == []

    def test_a_backend_started_async_runs_sync_calls_on_the_callers_loop(
        self, fake_chrome: FakeChrome
    ) -> None:
        """BrowserSession.start_async() starts the browser on the caller's loop."""
        backend = NoDriverBackend()
        with _loop_in_thread() as caller_loop:
            asyncio.run_coroutine_threadsafe(backend._start_async(), caller_loop).result(5)
            backend._started = True

            backend.navigate("https://example.com/")
            url = backend.current_url
            backend.stop()

            # The caller's loop is the caller's: stop() forgets it, never stops it.
            assert caller_loop.is_running()

        assert url == "https://example.com/"
        assert fake_chrome.misuse == []
        (browser,) = fake_chrome.browsers
        assert browser.loop is caller_loop
        assert browser.stopped_on is caller_loop
        assert backend._private_loop is None

    def test_a_sync_call_after_the_callers_loop_stopped_returns_the_fallback(
        self, fake_chrome: FakeChrome, captured_logs: list[dict[str, Any]]
    ) -> None:
        backend = NoDriverBackend()
        with _loop_in_thread() as caller_loop:
            asyncio.run_coroutine_threadsafe(backend._start_async(), caller_loop).result(5)
            backend._started = True

        assert backend.current_url == ""
        [warning] = [e for e in captured_logs if e["log_level"] == "warning"]
        assert warning["event"] == "nodriver_current_url_failed"
        assert "no longer running" in warning["error"]

    def test_stop_async_from_another_loop_stops_the_browser_on_its_own_loop(
        self, fake_chrome: FakeChrome
    ) -> None:
        backend = NoDriverBackend()
        backend.start()
        loop = backend._loop
        assert backend._private_loop is not None
        thread = backend._private_loop.thread

        asyncio.run(backend.stop_async())

        assert fake_chrome.browsers[0].stopped_on is loop
        assert not thread.is_alive()
        assert backend.is_running is False

    def test_a_started_backend_pickles_as_a_stopped_one(self) -> None:
        """The browser and tab belong to the loop, which does not survive pickling."""
        backend = with_private_loop(NoDriverBackend(headless=True))
        backend._started = True
        backend._browser = "a browser"  # stand-ins: MagicMock does not pickle
        backend._page = "a tab"

        clone = pickle.loads(pickle.dumps(backend))  # noqa: S301 - round-trips its own bytes

        assert clone._loop is None
        assert clone._private_loop is None
        assert clone._started is False
        assert clone._browser is None
        assert clone._page is None
        assert clone._headless is True
        assert clone.current_url == ""
        assert backend._started is True

    def test_an_unpickled_backend_starts_on_first_use(self, fake_chrome: FakeChrome) -> None:
        clone = pickle.loads(pickle.dumps(NoDriverBackend(headless=True)))  # noqa: S301 - round-trips its own bytes

        clone.navigate("https://example.com/")

        assert clone.is_running is True
        assert fake_chrome.browsers[0].tabs[-1].url == "https://example.com/"
        clone.stop()

    def test_a_backend_pickled_before_the_loop_attributes_existed_starts(
        self, fake_chrome: FakeChrome
    ) -> None:
        state = NoDriverBackend(headless=True).__getstate__()
        del state["_loop"]
        del state["_private_loop"]
        old = NoDriverBackend.__new__(NoDriverBackend)

        old.__setstate__(state)
        old.start()

        assert old.is_running is True
        assert fake_chrome.misuse == []
        old.stop()
        assert fake_chrome.browsers[0].stopped_on is not None

    def test_a_sync_call_without_a_loop_returns_the_fallback_and_makes_no_loop(
        self, captured_logs: list[dict[str, Any]]
    ) -> None:
        """Only start() creates the loop; a call before it must not run anywhere else."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()
        before = _loop_threads()

        assert backend.page_title == ""

        assert _loop_threads() - before == set()
        assert backend._loop is None
        [warning] = [e for e in captured_logs if e["log_level"] == "warning"]
        assert warning["event"] == "nodriver_page_title_failed"
        assert "no event loop" in warning["error"]

    def test_threads_starting_at_once_make_one_loop(self, fake_chrome: FakeChrome) -> None:
        backend = NoDriverBackend()
        barrier = threading.Barrier(8)

        def start() -> None:
            barrier.wait()
            backend.start()

        before = _loop_threads()
        threads = [threading.Thread(target=start) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        assert len(_loop_threads() - before) == 1
        assert len(fake_chrome.browsers) == 1
        backend.stop()

    def test_a_sync_start_arms_the_termination_handlers_on_the_calling_thread(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_start_async runs on the loop thread, where signal.signal cannot (#96)."""
        import signal as signal_module

        from graftpunk import chrome_orphans, signals
        from graftpunk.browser_launch import CleanupReport

        monkeypatch.setattr(chrome_orphans, "_ARMED", True)
        monkeypatch.setattr(
            "graftpunk.backends.nodriver.prepare_browser_launch", lambda: CleanupReport()
        )
        monkeypatch.setattr(signals, "auto_install", True)
        signal_module.signal(signal_module.SIGTERM, signal_module.SIG_DFL)
        backend = NoDriverBackend()

        backend.start()
        try:
            assert signal_module.getsignal(signal_module.SIGTERM) is signals._handle_termination
        finally:
            backend.stop()


class TestNoDriverBackendLifecycleEdges:
    """Stops and starts that cannot go through the browser's loop the usual way."""

    @staticmethod
    def _record_terminations(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        ended: list[Any] = []
        monkeypatch.setattr("graftpunk.backends.nodriver.terminate_nodriver_browser", ended.append)
        return ended

    @staticmethod
    async def _start_on_current_loop(backend: NoDriverBackend) -> None:
        """What BrowserSession.start_async() does."""
        await backend._start_async()
        backend._started = True

    def test_a_sync_stop_after_the_browsers_loop_closed_still_ends_chrome(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.signals import live_browsers

        ended = self._record_terminations(monkeypatch)
        backend = NoDriverBackend()
        asyncio.run(self._start_on_current_loop(backend))

        backend.stop()

        assert ended == fake_chrome.browsers
        assert backend.is_running is False
        assert backend._loop is None
        assert backend not in live_browsers()

    def test_stop_async_after_the_browsers_loop_closed_still_ends_chrome(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ended = self._record_terminations(monkeypatch)
        backend = NoDriverBackend()
        asyncio.run(self._start_on_current_loop(backend))

        asyncio.run(backend.stop_async())

        assert ended == fake_chrome.browsers
        assert backend.is_running is False
        assert backend._loop is None

    def test_a_sync_stop_from_inside_the_browsers_loop_ends_chrome_and_warns(
        self,
        fake_chrome: FakeChrome,
        monkeypatch: pytest.MonkeyPatch,
        captured_logs: list[dict[str, Any]],
    ) -> None:
        """session.quit() inside 'async with BrowserSession(...)' takes this path."""
        ended = self._record_terminations(monkeypatch)
        backend = NoDriverBackend()

        async def start_then_stop_inside() -> None:
            await self._start_on_current_loop(backend)
            backend.stop()

        asyncio.run(start_then_stop_inside())

        assert ended == fake_chrome.browsers
        assert backend.is_running is False
        [warning] = [e for e in captured_logs if e["log_level"] == "warning"]
        assert warning["event"] == "nodriver_backend_stop_in_own_loop"
        assert "stop_async" in warning["hint"]

    def test_a_failed_start_async_does_not_leave_the_callers_loop_behind(
        self, fake_chrome: FakeChrome
    ) -> None:
        fake_chrome.fail_with = RuntimeError("Chrome not found")
        backend = NoDriverBackend()

        with pytest.raises(RuntimeError, match="Chrome not found"):
            asyncio.run(backend._start_async())

        assert backend._loop is None
        fake_chrome.fail_with = None
        backend.start()
        assert backend.is_running is True
        backend.stop()
        assert fake_chrome.browsers[0].stopped_on is not None

    def test_ctrl_c_during_a_sync_start_leaves_no_browser_and_no_loop(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The interrupt lands while the first tab is loading."""
        from graftpunk.signals import live_browsers

        ended = self._record_terminations(monkeypatch)
        fake_chrome.hang_get = True
        real_submit = asyncio.run_coroutine_threadsafe
        interrupts = [KeyboardInterrupt()]

        def submit_then_interrupt(coro: Any, loop: asyncio.AbstractEventLoop) -> Any:
            future = real_submit(coro, loop)
            if not interrupts:
                return future

            class Interrupted:
                def result(self) -> Any:
                    assert fake_chrome.get_entered.wait(5)
                    raise interrupts.pop()

                def cancel(self) -> bool:
                    return future.cancel()

            return Interrupted()

        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", submit_then_interrupt)
        backend = NoDriverBackend()
        before = _loop_threads()

        with pytest.raises(KeyboardInterrupt):
            backend.start()

        # Ended before start() re-raised, not later by the loop's teardown.
        assert ended == fake_chrome.browsers
        assert backend._browser is None
        assert backend not in live_browsers()
        assert backend._loop is None
        assert _loop_threads() - before == set()

        fake_chrome.hang_get = False
        backend.start()
        backend.stop()
        assert len(fake_chrome.browsers) == 2
        assert fake_chrome.browsers[1].stopped_on is not None

    def test_stop_async_joins_the_private_loop_off_the_callers_loop(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time

        from graftpunk.backends.nodriver import _PrivateLoop

        backend = NoDriverBackend()
        backend.start()
        real_close = _PrivateLoop.close

        def slow_close(self: _PrivateLoop) -> None:
            time.sleep(0.3)
            real_close(self)

        monkeypatch.setattr(_PrivateLoop, "close", slow_close)

        async def ticks_during_stop() -> int:
            ticks = 0

            async def tick() -> None:
                nonlocal ticks
                while True:
                    ticks += 1
                    await asyncio.sleep(0.01)

            ticker = asyncio.create_task(tick())
            await backend.stop_async()
            ticker.cancel()
            return ticks

        assert asyncio.run(ticks_during_stop()) >= 10
        assert backend._loop is None

    @staticmethod
    def _printing_stop(browser: Any) -> None:
        """Make the fake browser's stop print, as nodriver's cleanup does."""
        real_stop = browser.stop

        def stop() -> None:
            print("successfully removed temp profile /tmp/uc_example")
            real_stop()

        browser.stop = stop

    def test_a_stop_keeps_nodrivers_output_but_not_another_threads(
        self,
        fake_chrome: FakeChrome,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from graftpunk.backends.nodriver import _PrivateLoop

        backend = NoDriverBackend()
        backend.start()
        self._printing_stop(fake_chrome.browsers[0])
        real_close = _PrivateLoop.close

        def close_while_another_thread_prints(self: _PrivateLoop) -> None:
            printer = threading.Thread(target=print, args=("another thread's line",))
            printer.start()
            printer.join(5)
            real_close(self)

        monkeypatch.setattr(_PrivateLoop, "close", close_while_another_thread_prints)

        backend.stop()

        out = capsys.readouterr().out
        assert "another thread's line" in out.splitlines()
        assert "successfully removed temp profile /tmp/uc_example" not in out.splitlines()
        assert fake_chrome.browsers[0].stopped_on is not None

    def test_a_stop_async_keeps_nodrivers_output_but_not_the_callers_loops(
        self,
        fake_chrome: FakeChrome,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from graftpunk.backends.nodriver import _PrivateLoop

        backend = NoDriverBackend()
        backend.start()
        self._printing_stop(fake_chrome.browsers[0])
        real_close = _PrivateLoop.close
        callers: list[asyncio.AbstractEventLoop] = []

        def close_while_the_caller_prints(self: _PrivateLoop) -> None:
            # stop_async joins the private loop on a worker thread, so the
            # caller's loop runs other work meanwhile.
            printed = threading.Event()

            def print_on_the_callers_loop() -> None:
                print("the caller's loop's line")
                printed.set()

            callers[0].call_soon_threadsafe(print_on_the_callers_loop)
            assert printed.wait(5)
            real_close(self)

        monkeypatch.setattr(_PrivateLoop, "close", close_while_the_caller_prints)

        async def stop() -> None:
            callers.append(asyncio.get_running_loop())
            await backend.stop_async()

        asyncio.run(stop())

        out = capsys.readouterr().out
        assert "the caller's loop's line" in out.splitlines()
        assert "successfully removed temp profile /tmp/uc_example" not in out.splitlines()
        assert fake_chrome.browsers[0].stopped_on is not None

    def test_a_call_cancelled_by_a_stop_on_another_thread_returns_the_fallback(
        self, captured_logs: list[dict[str, Any]]
    ) -> None:
        backend = with_private_loop(NoDriverBackend())
        backend._started = True
        backend._browser = MagicMock()
        entered = threading.Event()

        async def never_answers(_expression: str) -> str:
            entered.set()
            await asyncio.Event().wait()
            return "unreachable"

        backend._page = MagicMock()
        backend._page.evaluate = never_answers
        results: list[str] = []
        caller = threading.Thread(target=lambda: results.append(backend.current_url))
        caller.start()
        assert entered.wait(5)

        backend._release_loop()
        caller.join(5)

        assert results == [""]
        [warning] = [e for e in captured_logs if e["log_level"] == "warning"]
        assert warning["event"] == "nodriver_current_url_failed"
        assert "cancelled" in warning["error"]

    def test_a_loop_thread_that_does_not_start_leaves_no_unawaited_coroutine(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import gc
        import warnings

        from graftpunk.backends.nodriver import _PrivateLoop

        def no_thread(self: _PrivateLoop) -> None:
            raise RuntimeError("NoDriverBackend's event loop thread did not start")

        monkeypatch.setattr(_PrivateLoop, "__init__", no_thread)
        backend = NoDriverBackend()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(BrowserError, match="did not start"):
                backend.start()
            gc.collect()

        assert [str(w.message) for w in caught if "never awaited" in str(w.message)] == []
        assert backend._loop is None

    def test_ctrl_c_after_the_loop_dropped_the_call_leaves_no_unawaited_coroutine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A loop that stops between the submit and its next iteration drops
        the submitted call, so neither coroutine ever runs."""
        import concurrent.futures
        import gc
        import inspect
        import warnings

        backend = with_private_loop(NoDriverBackend())
        private = backend._private_loop
        assert private is not None

        def submit_to_a_stopping_loop(
            coro: Any, loop: asyncio.AbstractEventLoop
        ) -> concurrent.futures.Future[Any]:
            private.close()

            class Interrupted(concurrent.futures.Future[Any]):
                def result(self, timeout: float | None = None) -> Any:
                    raise KeyboardInterrupt

            return Interrupted()

        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", submit_to_a_stopping_loop)

        async def call() -> str:
            return "unreachable"

        coro = call()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(KeyboardInterrupt):
                backend._run_async(coro)
            gc.collect()

        assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
        assert [str(w.message) for w in caught if "never awaited" in str(w.message)] == []

    def test_a_loop_thread_that_starts_after_the_timeout_exits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.backends.nodriver import _PrivateLoop

        monkeypatch.setattr("graftpunk.backends.nodriver._LOOP_JOIN_TIMEOUT_S", 0.1)
        gate = threading.Event()
        threads: list[threading.Thread] = []
        real_main = _PrivateLoop._main

        def late_main(self: _PrivateLoop) -> None:
            threads.append(threading.current_thread())
            gate.wait(5)
            real_main(self)

        monkeypatch.setattr(_PrivateLoop, "_main", late_main)

        try:
            with pytest.raises(RuntimeError, match="did not start"):
                _PrivateLoop()
        finally:
            gate.set()
        [thread] = threads
        thread.join(5)

        assert thread.is_alive() is False


_CHILD_UNTIL_TERM = """
import os, signal, sys, time
profile = sys.argv[1]
delay = float(sys.argv[2])

def on_term(signum, frame):
    # Like Chrome, write the profile's last files on the way out.
    time.sleep(delay)
    os.makedirs(profile, exist_ok=True)
    with open(os.path.join(profile, "Local State"), "w") as f:
        f.write("{}")
    sys.exit(0)

signal.signal(signal.SIGTERM, on_term)
print("ready", flush=True)
while True:
    time.sleep(0.05)
"""

_CHILD_IGNORING_TERM = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("ready", flush=True)
while True:
    time.sleep(0.05)
"""


@contextmanager
def _chrome_like_child(
    profile: Path, exit_delay: float = 0.3, *, reaped: bool = True, script: str = _CHILD_UNTIL_TERM
) -> Iterator[Any]:
    """A real child process standing in for Chrome.

    By default it rewrites ``Local State`` in *profile* *exit_delay* seconds
    after SIGTERM, then exits. With *reaped*, a thread waits on it the way
    asyncio's threaded child watcher does (macOS), so a pid that exited stops
    answering ``kill(pid, 0)``. Without it, nothing reaps the child, which is
    what Linux's pidfd watcher amounts to while the browser's loop is blocked
    or closed: an exited Chrome stays a zombie.
    """
    import subprocess
    import sys

    child = subprocess.Popen(  # noqa: S603 - a fixed script run by this interpreter
        [sys.executable, "-c", script, str(profile), str(exit_delay)],
        stdout=subprocess.PIPE,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == b"ready"
    if reaped:
        reaper = threading.Thread(target=child.wait, daemon=True)
        reaper.start()
    try:
        yield child
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(5)
        child.stdout.close()


class _Browser:
    """A nodriver Browser stand-in, hashable as nodriver's registry needs."""

    def __init__(self, process: Any, config: SimpleNamespace) -> None:
        self._process = process
        self.config = config


def _temp_browser(profile: Path, process: Any = None) -> _Browser:
    """A browser with a temp profile nodriver made."""
    return _Browser(
        process, SimpleNamespace(uses_custom_data_dir=False, user_data_dir=str(profile))
    )


class TestNoDriverBackendEndingChromeWithoutItsLoop:
    """The loop-free stop waits for Chrome before removing its profile, on any OS."""

    def test_the_profile_is_removed_after_chrome_has_written_its_last_files(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("graftpunk.chrome_orphans._ARMED", True)
        profile = temp_profiles / "uc_live"
        profile.mkdir()
        backend = NoDriverBackend()

        with _chrome_like_child(profile) as child:
            backend._browser = _temp_browser(profile, child)
            backend._release_failed_attempt()

            assert child.poll() is not None
            assert not profile.exists()
        assert backend._browser is None

    def test_without_posix_signals_chrome_is_ended_through_its_process_handle(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """terminate_nodriver_browser does nothing off POSIX, and deregistering
        took away nodriver's atexit stop, so the handle is what ends Chrome."""
        monkeypatch.setattr("graftpunk.backends.nodriver._signals_available", lambda: False)
        profile = temp_profiles / "uc_windows"
        profile.mkdir()
        backend = NoDriverBackend()

        with _chrome_like_child(profile, exit_delay=0.0) as child:
            # asyncio's Process keeps the Popen on its transport.
            process = SimpleNamespace(
                pid=child.pid,
                terminate=child.terminate,
                _transport=SimpleNamespace(_proc=child, close=lambda: None),
            )
            backend._browser = _temp_browser(profile, process)
            backend._release_failed_attempt()

            assert child.wait(5) == 0
        assert not profile.exists()

    def test_the_subprocess_transport_is_closed_so_exit_prints_no_noise(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unclosed transport collected after its loop closed prints
        "Exception ignored in BaseSubprocessTransport.__del__"."""
        import gc
        import sys
        import time

        monkeypatch.setattr("graftpunk.chrome_orphans._ARMED", True)
        unraisable: list[Any] = []
        monkeypatch.setattr(sys, "unraisablehook", unraisable.append)
        profile = temp_profiles / "uc_transport"
        profile.mkdir()

        async def spawn() -> asyncio.subprocess.Process:
            return await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        process = asyncio.run(spawn())
        pid = process.pid
        backend = NoDriverBackend()
        backend._browser = _temp_browser(profile, process)

        gc.collect()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            backend._release_failed_attempt()
            # asyncio's child watcher holds the transport until it has reaped
            # the child, so wait for that before collecting it.
            for _ in range(100):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            del process
            gc.collect()

        assert [str(u.exc_value) for u in unraisable] == []
        assert _resource_warnings(caught) == []
        assert not profile.exists()

    def test_a_stop_through_the_loop_closes_the_transport_so_exit_prints_no_noise(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The loop has reaped Chrome by then, so closing its transport cannot steal the status.

        Chrome's helper processes hold its stdout and stderr open after it
        exits, so its transport is not finished when the stop reaps it, and
        an open transport collected after its loop closed prints "Exception
        ignored in BaseSubprocessTransport.__del__". The stand-in has exited
        and a grandchild holds its pipes.
        """
        import gc
        import sys

        unraisable: list[Any] = []
        monkeypatch.setattr(sys, "unraisablehook", unraisable.append)
        profile = temp_profiles / "uc_loop_stop"
        profile.mkdir()

        class _StoppableBrowser(_Browser):
            def stop(self) -> None:
                """Chrome has already exited."""

        helper = "import time; time.sleep(1)"
        chrome = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {helper!r}])"

        async def spawn_then_stop() -> asyncio.subprocess.Process:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                chrome,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            while process.returncode is None:
                await asyncio.sleep(0.01)
            backend = NoDriverBackend()
            backend._browser = _StoppableBrowser(
                process, SimpleNamespace(uses_custom_data_dir=False, user_data_dir=str(profile))
            )
            await backend._stop_async()
            return process

        process = asyncio.run(spawn_then_stop())

        # Something still holds an open one until interpreter exit, which is
        # when it would print; closed, its collection has nothing to do.
        assert process._transport.is_closing() is True  # type: ignore[attr-defined]
        del process
        gc.collect()
        assert [str(u.exc_value) for u in unraisable] == []
        assert not profile.exists()

    @pytest.mark.skipif(
        not hasattr(os, "waitid"),
        reason="CPython has no os.waitid on macOS, where asyncio reaps from a thread",
    )
    def test_an_exited_chrome_nothing_has_reaped_ends_promptly_and_stays_unreaped(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Linux's pidfd watcher reaps on the browser's loop, which is blocked
        or closed here, so the exited Chrome is a zombie that answers kill(pid, 0)."""
        import time

        monkeypatch.setattr("graftpunk.chrome_orphans._ARMED", True)
        profile = temp_profiles / "uc_zombie"
        profile.mkdir()
        backend = NoDriverBackend()

        with _chrome_like_child(profile, exit_delay=0.0, reaped=False) as child:
            backend._browser = _temp_browser(profile, child)
            began = time.monotonic()
            backend._release_failed_attempt()
            elapsed = time.monotonic() - began

            # The exit status is still there for the loop's watcher to collect.
            assert os.waitpid(child.pid, os.WNOHANG) == (child.pid, 0)
        # _REAP_TERM_TIMEOUT_S is 3 s; a wait that misread the zombie runs it out.
        assert elapsed < 1.5
        assert not profile.exists()

    def test_a_chrome_that_ignores_sigterm_is_killed_before_its_profile_goes(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import signal

        monkeypatch.setattr("graftpunk.chrome_orphans._ARMED", True)
        monkeypatch.setattr("graftpunk.backends.nodriver._REAP_TERM_TIMEOUT_S", 0.3)
        profile = temp_profiles / "uc_stubborn"
        profile.mkdir()
        backend = NoDriverBackend()

        with _chrome_like_child(profile, script=_CHILD_IGNORING_TERM) as child:
            backend._browser = _temp_browser(profile, child)
            backend._release_failed_attempt()

            assert child.wait(5) == -signal.SIGKILL
        assert not profile.exists()

    def test_a_chrome_still_running_after_both_signals_keeps_its_profile(
        self,
        temp_profiles: Path,
        monkeypatch: pytest.MonkeyPatch,
        captured_logs: list[dict[str, Any]],
    ) -> None:
        monkeypatch.setattr("graftpunk.chrome_orphans._ARMED", True)
        monkeypatch.setattr("graftpunk.backends.nodriver._REAP_TERM_TIMEOUT_S", 0.2)
        monkeypatch.setattr("graftpunk.backends.nodriver._REAP_KILL_TIMEOUT_S", 0.2)
        # The SIGKILL is refused, as it is for a process of another user (EPERM).
        monkeypatch.setattr("graftpunk.backends.nodriver._kill", lambda _proc: False)
        profile = temp_profiles / "uc_wedged"
        profile.mkdir()
        backend = NoDriverBackend()

        with _chrome_like_child(profile, script=_CHILD_IGNORING_TERM) as child:
            backend._browser = _temp_browser(profile, child)
            backend._release_failed_attempt()

            assert child.poll() is None
            assert profile.exists()
        [warning] = [e for e in captured_logs if e["log_level"] == "warning"]
        assert warning["event"] == "chrome_still_running_profile_kept"
        assert warning["pid"] == child.pid

    def test_a_chrome_already_reaped_is_not_signalled_again(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once reaped its pid is free, and the process holding it now is not Chrome."""
        import subprocess

        monkeypatch.setattr("graftpunk.chrome_orphans._ARMED", True)
        profile = temp_profiles / "uc_reaped"
        profile.mkdir()
        backend = NoDriverBackend()

        with _chrome_like_child(temp_profiles / "other", exit_delay=0.0) as other:
            reaped = SimpleNamespace(pid=other.pid, returncode=0)
            backend._browser = _temp_browser(profile, reaped)
            backend._release_failed_attempt()

            with pytest.raises(subprocess.TimeoutExpired):
                other.wait(0.5)
        assert not profile.exists()
        assert not (temp_profiles / "other").exists()

    def test_a_running_loop_keeps_the_exit_status_for_its_child_watcher(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The end runs on the loop, blocking it, as a sync stop() inside it
        does. A transport close there polls the Popen, which reaps Chrome
        before the watcher can; on Linux the watcher then logs "exit status
        already read" and reports 255."""
        import signal
        import sys

        monkeypatch.setattr("graftpunk.chrome_orphans._ARMED", True)
        profile = temp_profiles / "uc_running_loop"
        profile.mkdir()

        async def spawn() -> asyncio.subprocess.Process:
            return await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        backend = NoDriverBackend()

        async def end_on_the_loop_then_wait() -> int:
            process = await spawn()
            backend._browser = _temp_browser(profile, process)
            backend._release_failed_attempt()
            return await process.wait()

        with _loop_in_thread() as loop:
            returncode = asyncio.run_coroutine_threadsafe(end_on_the_loop_then_wait(), loop).result(
                10
            )

        assert returncode == -signal.SIGTERM
        assert not profile.exists()


# Chrome stand-in: exits on SIGTERM and leaves a helper holding its pipes, as
# Chrome's own helper processes do, so its transport does not finish by itself.
_CHROME_WITH_HELPER = """
import signal, subprocess, sys, time
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
print("ready", flush=True)
time.sleep(30)
"""


class _TransportWatch:
    """Spawns the stand-in on the running loop and records every close of its transport."""

    def __init__(self) -> None:
        self.process: Any = None
        self.loop_thread: int | None = None
        # (thread ident, repr of what the close raised, or None)
        self.closes: list[tuple[int, str | None]] = []

    async def spawn(self) -> asyncio.subprocess.Process:
        import sys

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _CHROME_WITH_HELPER,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        await process.stdout.readline()
        self.loop_thread = threading.get_ident()
        transport = process._transport  # type: ignore[attr-defined]
        real_close = transport.close

        def close() -> None:
            try:
                real_close()
            except BaseException as exc:
                self.closes.append((threading.get_ident(), repr(exc)))
                raise
            self.closes.append((threading.get_ident(), None))

        transport.close = close
        self.process = process
        return process

    def is_closing(self) -> bool:
        return bool(self.process._transport.is_closing())

    async def until_closing(self) -> None:
        for _ in range(300):
            if self.is_closing():
                return
            await asyncio.sleep(0.01)

    def assert_closed_on_its_loop(self, captured_logs: list[dict[str, Any]]) -> None:
        assert self.is_closing() is True
        assert self.closes
        assert all(close == (self.loop_thread, None) for close in self.closes)
        assert [e for e in captured_logs if e["event"] == "chrome_transport_close_failed"] == []


class TestNoDriverBackendTransportClosedOnItsLoop:
    """Chrome's transport is closed on its loop's thread, after the loop collected its exit.

    asyncio transports are not thread-safe: in debug mode a close from another
    thread raises "Non-thread-safe operation", and outside it mutates the loop
    from the wrong thread. Left open, the transport is collected after its
    loop closed, and exit prints "Exception ignored in
    BaseSubprocessTransport.__del__ ... Event loop is closed".
    """

    @pytest.fixture(autouse=True)
    def _armed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("graftpunk.chrome_orphans._ARMED", True)

    @staticmethod
    def _spawning_start(
        fake_chrome: FakeChrome,
        watch: _TransportWatch,
        monkeypatch: pytest.MonkeyPatch,
        get: Callable[[str], Coroutine[Any, Any, Any]] | None = None,
    ) -> None:
        """Make each start's browser own a real stand-in process, and optionally its own get()."""
        import nodriver

        async def start(**kwargs: Any) -> Any:
            browser = await fake_chrome.start(**kwargs)
            browser._process = await watch.spawn()
            if get is not None:
                browser.get = get
            return browser

        monkeypatch.setattr(nodriver, "start", start)

    def test_a_sync_stop_inside_the_loop_closes_the_transport_before_the_loop_closes(
        self, monkeypatch: pytest.MonkeyPatch, captured_logs: list[dict[str, Any]]
    ) -> None:
        import gc
        import sys

        unraisable: list[Any] = []
        monkeypatch.setattr(sys, "unraisablehook", unraisable.append)
        watch = _TransportWatch()

        async def stop_inside_the_loop() -> None:
            process = await watch.spawn()
            backend = NoDriverBackend()
            backend._browser = _Browser(
                process, SimpleNamespace(uses_custom_data_dir=True, user_data_dir=None)
            )
            backend._loop = asyncio.get_running_loop()
            backend._started = True
            backend.stop()
            # Returns at once, as an 'async with' body ending here would.

        asyncio.run(stop_inside_the_loop())

        watch.assert_closed_on_its_loop(captured_logs)
        watch.process = None
        gc.collect()
        assert [str(u.exc_value) for u in unraisable] == []

    def test_a_failed_start_attempt_closes_the_transport_on_its_loop(
        self,
        fake_chrome: FakeChrome,
        monkeypatch: pytest.MonkeyPatch,
        captured_logs: list[dict[str, Any]],
    ) -> None:
        watch = _TransportWatch()

        async def first_tab_fails(url: str) -> Any:
            raise RuntimeError("the first tab did not open")

        self._spawning_start(fake_chrome, watch, monkeypatch, get=first_tab_fails)
        backend = NoDriverBackend()

        async def start() -> None:
            with pytest.raises(RuntimeError, match="first tab"):
                await backend._start_async()
            await watch.until_closing()

        asyncio.run(start(), debug=True)

        watch.assert_closed_on_its_loop(captured_logs)

    def test_a_cancelled_start_attempt_closes_the_transport_on_its_loop(
        self,
        fake_chrome: FakeChrome,
        monkeypatch: pytest.MonkeyPatch,
        captured_logs: list[dict[str, Any]],
    ) -> None:
        watch = _TransportWatch()
        self._spawning_start(fake_chrome, watch, monkeypatch)
        fake_chrome.hang_get = True
        backend = NoDriverBackend()

        async def start_then_cancel() -> None:
            task = asyncio.ensure_future(backend._start_async())
            assert await asyncio.to_thread(fake_chrome.get_entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await watch.until_closing()

        asyncio.run(start_then_cancel(), debug=True)

        watch.assert_closed_on_its_loop(captured_logs)

    def test_a_cancelled_stop_async_closes_the_transport_on_its_loop(
        self,
        fake_chrome: FakeChrome,
        monkeypatch: pytest.MonkeyPatch,
        captured_logs: list[dict[str, Any]],
    ) -> None:
        watch = _TransportWatch()
        self._spawning_start(fake_chrome, watch, monkeypatch)
        backend = NoDriverBackend()

        async def stop_that_never_finishes() -> None:
            await asyncio.Event().wait()

        async def start_then_cancel_the_stop() -> None:
            await backend._start_async()
            backend._started = True
            monkeypatch.setattr(backend, "_stop_async", stop_that_never_finishes)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(backend.stop_async(), 0.05)
            await watch.until_closing()

        asyncio.run(start_then_cancel_the_stop(), debug=True)

        watch.assert_closed_on_its_loop(captured_logs)
        assert backend.is_running is False

    def test_an_interrupted_sync_stop_closes_the_transport_on_the_private_loop(
        self,
        fake_chrome: FakeChrome,
        monkeypatch: pytest.MonkeyPatch,
        captured_logs: list[dict[str, Any]],
    ) -> None:
        watch = _TransportWatch()
        self._spawning_start(fake_chrome, watch, monkeypatch)
        backend = with_private_loop(NoDriverBackend())
        loop = backend._loop
        assert loop is not None
        loop.call_soon_threadsafe(loop.set_debug, True)
        backend.start()

        def interrupted(coro: Any) -> Any:
            coro.close()
            raise KeyboardInterrupt

        monkeypatch.setattr(backend, "_run_async", interrupted)

        with pytest.raises(KeyboardInterrupt):
            backend.stop()

        # The stop joined the loop's thread, so the loop has closed by now.
        assert loop.is_closed()
        watch.assert_closed_on_its_loop(captured_logs)

    class _RecordingTransport:
        """A subprocess transport stand-in that records when its close ran."""

        def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
            self._loop = loop
            self.closed_at: float | None = None

        def close(self) -> None:
            import time

            if self.closed_at is None:
                self.closed_at = time.monotonic()

    def test_the_give_up_clock_starts_when_the_loop_first_runs_the_close(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The loop is blocked for longer than the whole give-up wait right
        after the close is scheduled: the watcher still gets that wait once
        the loop runs, and the close then gives up and closes."""
        import time

        from graftpunk.backends.nodriver import _close_transport

        monkeypatch.setattr("graftpunk.backends.nodriver._REAP_TERM_TIMEOUT_S", 0.2)
        monkeypatch.setattr("graftpunk.backends.nodriver._REAP_KILL_TIMEOUT_S", 0.1)
        monkeypatch.setattr("graftpunk.backends.nodriver._EXIT_POLL_S", 0.01)

        async def schedule_then_block() -> tuple[float, float | None]:
            transport = self._RecordingTransport(asyncio.get_running_loop())
            # Never collected: the watcher is wedged.
            proc = SimpleNamespace(returncode=None, _transport=transport)
            _close_transport(proc)
            time.sleep(0.5)
            unblocked = time.monotonic()
            deadline = unblocked + 3
            while transport.closed_at is None and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            return unblocked, transport.closed_at

        unblocked, closed_at = asyncio.run(schedule_then_block())

        assert closed_at is not None
        assert closed_at - unblocked >= 0.25

    def test_a_stopped_loop_closes_the_transport_when_it_runs_again(self) -> None:
        """A hand-managed loop between run_until_complete() calls: closing now
        would reap Chrome ahead of the watcher that runs when the loop does."""
        from graftpunk.backends.nodriver import _close_transport

        loop = asyncio.new_event_loop()
        try:
            transport = self._RecordingTransport(loop)
            proc = SimpleNamespace(returncode=None, _transport=transport)

            _close_transport(proc)
            closed_while_stopped = transport.closed_at is not None
            # The watcher collects the exit once the loop runs.
            proc.returncode = 0
            loop.run_until_complete(asyncio.sleep(0.05))
        finally:
            loop.close()

        assert closed_while_stopped is False
        assert transport.closed_at is not None

    @pytest.mark.parametrize("stop", ["stop", "stop_async"])
    def test_a_stopped_loop_closed_without_running_again_prints_no_noise(
        self, stop: str, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hand-managed caller loop that started Chrome has stopped; the stop
        ends Chrome without it and schedules the transport's close there, and
        the caller then closes the loop without running it again (#243).

        The dropped close closes the transport without the loop, pipes
        included, and reads Chrome's exit status: nothing is left for a
        ResourceWarning (``unclosed transport``, ``still running``)."""
        import gc
        import sys

        unraisable: list[Any] = []
        monkeypatch.setattr(sys, "unraisablehook", unraisable.append)
        profile = temp_profiles / f"uc_closed_after_{stop}"
        profile.mkdir()

        async def spawn() -> asyncio.subprocess.Process:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _CHILD_UNTIL_TERM,
                str(profile),
                "0",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert process.stdout is not None
            assert (await process.stdout.readline()).strip() == b"ready"
            return process

        loop = asyncio.new_event_loop()
        # Earlier tests' garbage is collected outside the recording.
        gc.collect()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                process = loop.run_until_complete(spawn())
                transport = process._transport  # type: ignore[attr-defined]
                popen = transport._proc
                pipe_transports = [p.pipe for p in transport._pipes.values() if p is not None]
                backend = NoDriverBackend()
                backend._browser = _temp_browser(profile, process)
                backend._started = True
                backend._loop = loop
                del process
                if stop == "stop":
                    _within(10, backend.stop)
                else:
                    _within(10, lambda: asyncio.run(backend.stop_async()))
                closing_before_the_loop_closed = transport.is_closing()
                del transport
            finally:
                loop.close()
            gc.collect()
            pipes_left_open = [p for p in pipe_transports if p._pipe is not None]
            exit_status = popen.returncode
            del pipe_transports, popen
            gc.collect()

        assert closing_before_the_loop_closed is False
        assert [repr(u.exc_value) for u in unraisable] == []
        assert pipes_left_open == []
        assert exit_status is not None
        assert _resource_warnings(caught) == []
        assert backend.is_running is False
        assert not profile.exists()

    def test_a_scheduled_close_dropped_while_its_loop_is_open_leaves_the_transport_alone(
        self,
    ) -> None:
        """An open loop runs a pending close itself; closing from the finalizer
        would race it on another thread."""
        import gc

        from graftpunk.backends.nodriver import _ScheduledClose

        class Transport:
            closed = 0

            def close(self) -> None:
                self.closed += 1

        transport = Transport()
        loop = asyncio.new_event_loop()
        try:
            _ScheduledClose(SimpleNamespace(returncode=None), transport, loop)
            gc.collect()
            closed_while_open = transport.closed
        finally:
            loop.close()
        _ScheduledClose(SimpleNamespace(returncode=None), transport, loop)
        gc.collect()

        assert closed_while_open == 0
        assert transport.closed == 1


class TestNoDriverBackendStartThatNeverReturned:
    """A start that fails or is interrupted inside uc.start() never returns its browser."""

    @staticmethod
    def _registry(monkeypatch: pytest.MonkeyPatch) -> set[Any]:
        registry: set[Any] = set()
        monkeypatch.setattr("graftpunk.backends.nodriver._nodriver_registry", lambda: registry)
        return registry

    @staticmethod
    def _spawned(profile: Path, browser_args: Any) -> _Browser:
        """What nodriver's Browser.start() has registered before it connects."""
        profile.mkdir()
        return _Browser(
            None,
            SimpleNamespace(
                uses_custom_data_dir=False,
                user_data_dir=str(profile),
                _browser_args=browser_args,
            ),
        )

    async def test_an_interrupt_while_connecting_ends_the_launched_chrome(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ended: list[Any] = []
        monkeypatch.setattr("graftpunk.backends.nodriver.terminate_nodriver_browser", ended.append)
        registry = self._registry(monkeypatch)
        spawned: list[Any] = []
        connecting = asyncio.Event()

        async def start_then_hang(**kwargs: Any) -> None:
            browser = self._spawned(temp_profiles / "uc_connecting", kwargs["browser_args"])
            registry.add(browser)
            spawned.append(browser)
            connecting.set()
            await asyncio.Event().wait()

        mock_uc = MagicMock()
        mock_uc.start = start_then_hang
        backend = NoDriverBackend()

        with patch.dict("sys.modules", {"nodriver": mock_uc}):
            task = asyncio.create_task(backend._start_async())
            await connecting.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert ended == spawned
        assert registry == set()
        assert not (temp_profiles / "uc_connecting").exists()

    async def test_each_failed_connect_ends_its_chrome_before_the_retry(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """nodriver raises "Failed to connect to browser" from inside Browser.start()."""
        ended: list[Any] = []
        monkeypatch.setattr("graftpunk.backends.nodriver.terminate_nodriver_browser", ended.append)
        registry = self._registry(monkeypatch)

        async def no_wait(_delay: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", no_wait)
        spawned: list[Any] = []

        async def start_then_fail(**kwargs: Any) -> None:
            browser = self._spawned(
                temp_profiles / f"uc_attempt{len(spawned)}", kwargs["browser_args"]
            )
            registry.add(browser)
            spawned.append(browser)
            raise Exception("\nFailed to connect to browser\n")  # noqa: TRY002 - nodriver's own type

        mock_uc = MagicMock()
        mock_uc.start = start_then_fail
        backend = NoDriverBackend()

        with (
            patch.dict("sys.modules", {"nodriver": mock_uc}),
            pytest.raises(BrowserError, match="after 3 attempts"),
        ):
            await backend._start_async()

        assert len(spawned) == 3
        assert ended == spawned
        assert registry == set()
        assert [p.name for p in temp_profiles.iterdir()] == []

    @staticmethod
    def _slow_end(monkeypatch: pytest.MonkeyPatch, ended: list[Any]) -> None:
        """Make ending a Chrome take 0.3 s, the way waiting for its exit does."""
        import time

        def slow_end(browser: Any) -> None:
            time.sleep(0.3)
            ended.append(browser)

        monkeypatch.setattr("graftpunk.backends.nodriver._end_browser_without_loop", slow_end)

    @staticmethod
    async def _ticks_while(awaitable: Any) -> tuple[int, BaseException | None]:
        """Count 10 ms ticks on this loop while *awaitable* runs, and what it raised."""
        ticks = 0

        async def tick() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        ticker = asyncio.create_task(tick())
        raised: BaseException | None = None
        try:
            await awaitable
        except BaseException as exc:  # noqa: BLE001 - returned to the test
            raised = exc
        ticker.cancel()
        return ticks, raised

    async def test_a_failed_connect_ends_its_chrome_off_the_callers_loop(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = self._registry(monkeypatch)
        ended: list[Any] = []
        self._slow_end(monkeypatch, ended)
        spawned: list[Any] = []

        async def start_then_fail(**kwargs: Any) -> None:
            browser = self._spawned(temp_profiles / "uc_failed", kwargs["browser_args"])
            registry.add(browser)
            spawned.append(browser)
            raise Exception("\nFailed to connect to browser\n")  # noqa: TRY002 - nodriver's own type

        mock_uc = MagicMock()
        mock_uc.start = start_then_fail

        with patch.dict("sys.modules", {"nodriver": mock_uc}):
            ticks, raised = await self._ticks_while(NoDriverBackend()._start_async(_max_attempts=1))

        assert isinstance(raised, BrowserError)
        assert ended == spawned
        assert ticks >= 10

    async def test_a_cancelled_start_async_ends_its_chrome_off_the_loop_through_a_second_cancel(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = self._registry(monkeypatch)
        ended: list[Any] = []
        self._slow_end(monkeypatch, ended)
        spawned: list[Any] = []
        connecting = asyncio.Event()

        async def start_then_hang(**kwargs: Any) -> None:
            browser = self._spawned(temp_profiles / "uc_cancelled", kwargs["browser_args"])
            registry.add(browser)
            spawned.append(browser)
            connecting.set()
            await asyncio.Event().wait()

        mock_uc = MagicMock()
        mock_uc.start = start_then_hang

        async def cancel_twice(task: asyncio.Task[None]) -> None:
            await connecting.wait()
            task.cancel()
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        with patch.dict("sys.modules", {"nodriver": mock_uc}):
            task = asyncio.create_task(NoDriverBackend()._start_async())
            ticks, raised = await self._ticks_while(cancel_twice(task))

        assert raised is None
        assert ended == spawned
        assert ticks >= 10

    async def test_a_browser_launched_with_a_copy_of_the_arguments_is_still_found(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Matching is by the launch switch's value, not by which list object nodriver kept."""
        ended: list[Any] = []
        monkeypatch.setattr("graftpunk.backends.nodriver.terminate_nodriver_browser", ended.append)
        registry = self._registry(monkeypatch)
        spawned: list[Any] = []

        async def start_then_fail(**kwargs: Any) -> None:
            browser = self._spawned(temp_profiles / "uc_ours", list(kwargs["browser_args"]))
            registry.add(browser)
            spawned.append(browser)
            raise RuntimeError("Chrome crashed")

        mock_uc = MagicMock()
        mock_uc.start = start_then_fail

        with (
            patch.dict("sys.modules", {"nodriver": mock_uc}),
            pytest.raises(RuntimeError, match="Chrome crashed"),
        ):
            await NoDriverBackend()._start_async()

        assert ended == spawned
        assert registry == set()

    async def test_a_browser_whose_arguments_cannot_be_read_is_left_alone(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ended: list[Any] = []
        monkeypatch.setattr("graftpunk.backends.nodriver.terminate_nodriver_browser", ended.append)
        registry = self._registry(monkeypatch)

        async def start_then_fail(**kwargs: Any) -> None:
            registry.add(self._spawned(temp_profiles / "uc_unknown", None))
            raise RuntimeError("Chrome crashed")

        mock_uc = MagicMock()
        mock_uc.start = start_then_fail

        with (
            patch.dict("sys.modules", {"nodriver": mock_uc}),
            pytest.raises(RuntimeError, match="Chrome crashed"),
        ):
            await NoDriverBackend()._start_async()

        assert ended == []
        assert len(registry) == 1
        assert (temp_profiles / "uc_unknown").exists()

    async def test_a_browser_another_start_launched_meanwhile_is_left_alone(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ended: list[Any] = []
        monkeypatch.setattr("graftpunk.backends.nodriver.terminate_nodriver_browser", ended.append)
        registry = self._registry(monkeypatch)
        spawned: list[Any] = []

        async def start_then_fail(**kwargs: Any) -> None:
            # Another backend's start, in another thread, registered first.
            registry.add(self._spawned(temp_profiles / "uc_other", ["--graftpunk-launch-id=other"]))
            browser = self._spawned(temp_profiles / "uc_ours", kwargs["browser_args"])
            registry.add(browser)
            spawned.append(browser)
            raise RuntimeError("Chrome crashed")

        mock_uc = MagicMock()
        mock_uc.start = start_then_fail
        backend = NoDriverBackend()

        with (
            patch.dict("sys.modules", {"nodriver": mock_uc}),
            pytest.raises(RuntimeError, match="Chrome crashed"),
        ):
            await backend._start_async()

        assert ended == spawned
        assert [b.config.user_data_dir for b in registry] == [str(temp_profiles / "uc_other")]
        assert (temp_profiles / "uc_other").exists()


class _ContendedLock:
    """The backend's lock, which says when a thread had to wait for it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.contended = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._lock.acquire(blocking=False):
            return True
        self.contended.set()
        return self._lock.acquire(blocking, timeout) if blocking else False

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


class TestNoDriverBackendInterruptedLifecycle:
    """Interrupts that land between the steps of a start or a stop."""

    @staticmethod
    def _record_terminations(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        ended: list[Any] = []
        monkeypatch.setattr("graftpunk.backends.nodriver.terminate_nodriver_browser", ended.append)
        return ended

    def test_ctrl_c_after_the_browser_started_but_before_start_returned(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.signals import live_browsers

        ended = self._record_terminations(monkeypatch)
        backend = NoDriverBackend()
        real_run_async = backend._run_async

        def run_then_interrupt(coro: Any) -> Any:
            real_run_async(coro)
            raise KeyboardInterrupt

        monkeypatch.setattr(backend, "_run_async", run_then_interrupt)

        with pytest.raises(KeyboardInterrupt):
            backend.start()

        assert len(fake_chrome.browsers) == 1
        assert ended == fake_chrome.browsers
        assert backend._browser is None
        assert backend not in live_browsers()
        assert backend._loop is None

    def test_ctrl_c_before_a_sync_stop_reached_the_loop_still_ends_chrome(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ended = self._record_terminations(monkeypatch)
        backend = NoDriverBackend()
        backend.start()

        def interrupted(coro: Any) -> Any:
            coro.close()
            raise KeyboardInterrupt

        monkeypatch.setattr(backend, "_run_async", interrupted)

        with pytest.raises(KeyboardInterrupt):
            backend.stop()

        assert ended == fake_chrome.browsers
        assert fake_chrome.browsers[0].stopped_on is None
        assert backend.is_running is False
        assert backend._loop is None

    def test_a_stop_async_cancelled_before_the_stop_ran_still_ends_chrome(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ended: list[Any] = []
        gate = threading.Event()

        def end_then_free_the_loop(browser: Any) -> bool:
            # The cancellation has been handled by the time Chrome is ended,
            # so the loop can run again: the stop's join of its thread needs it.
            ended.append(browser)
            gate.set()
            return False

        monkeypatch.setattr(
            "graftpunk.backends.nodriver.terminate_nodriver_browser", end_then_free_the_loop
        )
        backend = NoDriverBackend()
        backend.start()
        loop = backend._loop
        assert loop is not None
        # Keep the browser's loop busy, so the submitted stop has not begun
        # when wait_for cancels it.
        loop.call_soon_threadsafe(gate.wait, 10)

        async def stop_with_timeout() -> None:
            await asyncio.wait_for(backend.stop_async(), 0.1)

        try:
            with pytest.raises(TimeoutError):
                asyncio.run(stop_with_timeout())
        finally:
            gate.set()

        assert ended == fake_chrome.browsers
        assert fake_chrome.browsers[0].stopped_on is None
        assert backend.is_running is False

    def test_a_cancelled_stop_async_ends_chrome_off_the_callers_loop(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time

        ended: list[Any] = []

        def slow_end(browser: Any) -> None:
            time.sleep(0.3)
            ended.append(browser)

        monkeypatch.setattr("graftpunk.backends.nodriver._end_browser_without_loop", slow_end)
        backend = NoDriverBackend()

        async def stop_that_never_finishes() -> None:
            await asyncio.Event().wait()

        async def ticks_during_a_cancelled_stop() -> int:
            await backend._start_async()
            backend._started = True
            monkeypatch.setattr(backend, "_stop_async", stop_that_never_finishes)
            ticks = 0

            async def tick() -> None:
                nonlocal ticks
                while True:
                    ticks += 1
                    await asyncio.sleep(0.01)

            ticker = asyncio.create_task(tick())
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(backend.stop_async(), 0.05)
            ticker.cancel()
            return ticks

        assert asyncio.run(ticks_during_a_cancelled_stop()) >= 10
        assert ended == fake_chrome.browsers
        assert backend.is_running is False

    def test_two_threads_starting_one_backend_launch_one_browser(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import nodriver

        async def slow_start(**kwargs: Any) -> Any:
            await asyncio.sleep(0.2)
            return await fake_chrome.start(**kwargs)

        monkeypatch.setattr(nodriver, "start", slow_start)
        backend = NoDriverBackend()
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def start() -> None:
            barrier.wait()
            try:
                backend.start()
            except BaseException as exc:  # noqa: BLE001 - reported by the assertion
                errors.append(exc)

        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        assert errors == []
        assert len(fake_chrome.browsers) == 1
        backend.stop()

    @staticmethod
    def _start_in_a_thread_until_a_stop_waits(
        backend: NoDriverBackend, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[threading.Thread, threading.Event]:
        """Start *backend* in a thread whose launch finishes only once a stop waits on the lock."""
        import nodriver

        lock = _ContendedLock()
        backend._lock = lock  # type: ignore[assignment]
        launching = threading.Event()

        async def start_once_a_stop_waits(**kwargs: Any) -> Any:
            launching.set()
            await asyncio.to_thread(lock.contended.wait, 5)
            return await fake_chrome.start(**kwargs)

        monkeypatch.setattr(nodriver, "start", start_once_a_stop_waits)
        starter = threading.Thread(target=backend.start)
        starter.start()
        return starter, launching

    def test_a_stop_during_another_threads_start_stops_the_browser_it_launched(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = NoDriverBackend()
        starter, launching = self._start_in_a_thread_until_a_stop_waits(
            backend, fake_chrome, monkeypatch
        )
        assert launching.wait(5)

        backend.stop()
        starter.join(5)

        [browser] = fake_chrome.browsers
        assert browser.stopped_on is not None
        assert backend.is_running is False
        assert backend._loop is None

    def test_a_stop_async_during_another_threads_start_stops_the_browser_it_launched(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = NoDriverBackend()
        starter, launching = self._start_in_a_thread_until_a_stop_waits(
            backend, fake_chrome, monkeypatch
        )

        async def stop_during_start() -> None:
            assert await asyncio.to_thread(launching.wait, 5)
            await backend.stop_async()

        asyncio.run(stop_during_start())
        starter.join(5)

        [browser] = fake_chrome.browsers
        assert browser.stopped_on is not None
        assert backend.is_running is False

    def test_start_async_after_a_sync_start_keeps_the_running_browser(
        self, fake_chrome: FakeChrome
    ) -> None:
        from graftpunk.session import BrowserSession

        backend = NoDriverBackend()
        backend.start()
        private = backend._private_loop
        session = SimpleNamespace(_backend_type="nodriver", _backend_instance=backend)

        asyncio.run(BrowserSession.start_async(session))  # type: ignore[arg-type]

        assert len(fake_chrome.browsers) == 1
        assert backend._private_loop is private
        backend.navigate("https://example.com/")
        assert backend.current_url == "https://example.com/"
        assert fake_chrome.misuse == []
        backend.stop()
        assert fake_chrome.browsers[0].stopped_on is not None

    def test_start_async_twice_launches_one_browser(self, fake_chrome: FakeChrome) -> None:
        """await session.start_async() and then 'async with session:' start twice."""
        from graftpunk.session import BrowserSession

        backend = NoDriverBackend()
        session = SimpleNamespace(_backend_type="nodriver", _backend_instance=backend)

        async def start_twice_then_stop() -> None:
            await BrowserSession.start_async(session)  # type: ignore[arg-type]
            await BrowserSession.start_async(session)  # type: ignore[arg-type]
            await backend.stop_async()

        asyncio.run(start_twice_then_stop())

        assert len(fake_chrome.browsers) == 1
        assert fake_chrome.browsers[0].stopped_on is not None

    def test_a_start_async_releases_an_old_private_loop_off_the_callers_loop(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time

        from graftpunk.backends.nodriver import _PrivateLoop

        backend = with_private_loop(NoDriverBackend())
        real_close = _PrivateLoop.close

        def slow_close(self: _PrivateLoop) -> None:
            time.sleep(0.3)
            real_close(self)

        monkeypatch.setattr(_PrivateLoop, "close", slow_close)

        async def ticks_during_start() -> int:
            ticks = 0

            async def tick() -> None:
                nonlocal ticks
                while True:
                    ticks += 1
                    await asyncio.sleep(0.01)

            ticker = asyncio.create_task(tick())
            await backend._start_async()
            backend._started = True
            await backend.stop_async()
            ticker.cancel()
            return ticks

        assert asyncio.run(ticks_during_start()) >= 10
        assert backend._private_loop is None

    def test_start_async_after_its_loop_finished_ends_the_old_browser_and_starts_anew(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A first asyncio.run(session.start_async()) that never stopped leaves
        a browser whose websocket belongs to a closed loop."""
        from graftpunk.session import BrowserSession

        ended: list[Any] = []
        monkeypatch.setattr("graftpunk.backends.nodriver._end_browser_without_loop", ended.append)
        backend = NoDriverBackend()
        session = SimpleNamespace(_backend_type="nodriver", _backend_instance=backend)

        asyncio.run(BrowserSession.start_async(session))  # type: ignore[arg-type]
        first_loop = backend._loop
        assert first_loop is not None and first_loop.is_closed()

        async def start_again_then_use() -> str:
            await BrowserSession.start_async(session)  # type: ignore[arg-type]
            page = backend._page
            assert page is not None
            url = await page.evaluate("window.location.href")
            await backend.stop_async()
            return str(url)

        assert asyncio.run(start_again_then_use()) == "about:blank"
        first, second = fake_chrome.browsers
        assert ended == [first]
        assert second.stopped_on is not None
        assert fake_chrome.misuse == []

    def test_a_start_async_cancelled_while_it_ends_a_finished_loops_browser_leaves_it_stopped(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time

        from graftpunk.session import BrowserSession

        ended: list[Any] = []

        def slow_end(browser: Any) -> None:
            time.sleep(0.5)
            ended.append(browser)

        monkeypatch.setattr("graftpunk.backends.nodriver._end_browser_without_loop", slow_end)
        backend = NoDriverBackend()
        session = SimpleNamespace(_backend_type="nodriver", _backend_instance=backend)
        asyncio.run(BrowserSession.start_async(session))  # type: ignore[arg-type]

        async def cancelled_start() -> None:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(BrowserSession.start_async(session), 0.1)  # type: ignore[arg-type]

        asyncio.run(cancelled_start())

        assert ended == fake_chrome.browsers
        assert backend._started is False
        assert backend.is_running is False
        backend.start()
        backend.navigate("https://example.com/")
        assert backend.current_url == "https://example.com/"
        assert len(fake_chrome.browsers) == 2
        backend.stop()
        assert fake_chrome.browsers[1].stopped_on is not None
        assert fake_chrome.misuse == []

    def test_a_sync_start_inside_the_browsers_loop_raises_rather_than_wait_for_a_stop(
        self, fake_chrome: FakeChrome
    ) -> None:
        """Another thread's stop() holds the lock and waits on the browser's
        loop; a start() on that loop waiting for the lock would deadlock both."""
        import time

        backend = NoDriverBackend()
        errors: list[str] = []

        async def start_while_another_thread_stops() -> None:
            await backend._start_async()
            backend._started = True
            stopper = threading.Thread(target=backend.stop, daemon=True)
            stopper.start()
            # Until the other thread holds the lock.
            deadline = time.monotonic() + 5
            while backend._lock.acquire(blocking=False) and time.monotonic() < deadline:
                backend._lock.release()
                time.sleep(0.001)
            try:
                backend.start()
            except RuntimeError as exc:
                errors.append(str(exc))
            # Free the loop, so the other thread's stop can run on it.
            await asyncio.to_thread(stopper.join, 5)

        # The watchdog: a deadlock leaves this thread alive.
        runner = threading.Thread(
            target=asyncio.run, args=(start_while_another_thread_stops(),), daemon=True
        )
        runner.start()
        runner.join(10)

        assert not runner.is_alive()
        assert len(errors) == 1
        assert "browser's own event loop" in errors[0]
        assert fake_chrome.browsers[0].stopped_on is not None
        assert backend.is_running is False

    def test_sync_calls_during_this_threads_stop_async_stop_chrome_and_refuse_to_start(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Another coroutine on the loop running stop_async() calls stop(),
        start(), and start_async() while that stop is paused at an await.
        The browser's loop is the private one, which still runs, so the sync
        stop stops Chrome there rather than without it."""
        from graftpunk.session import BrowserSession

        ended: list[Any] = []
        monkeypatch.setattr("graftpunk.backends.nodriver._end_browser_without_loop", ended.append)
        backend = NoDriverBackend()
        backend.start()
        loop = backend._loop
        real_stop = backend._stop_async
        stopping = threading.Event()

        async def slow_stop() -> None:
            stopping.set()
            await asyncio.sleep(0.3)
            await real_stop()

        monkeypatch.setattr(backend, "_stop_async", slow_stop)
        session = SimpleNamespace(_backend_type="nodriver", _backend_instance=backend)
        start_errors: list[str] = []
        running_after_sync_stop: list[bool] = []

        async def stop_with_sync_calls_meanwhile() -> None:
            stop = asyncio.ensure_future(backend.stop_async())
            assert await asyncio.to_thread(stopping.wait, 5)
            backend.stop()
            running_after_sync_stop.append(backend.is_running)
            try:
                backend.start()
            except RuntimeError as exc:
                start_errors.append(str(exc))
            try:
                await BrowserSession.start_async(session)  # type: ignore[arg-type]
            except RuntimeError as exc:
                start_errors.append(str(exc))
            await stop

        asyncio.run(stop_with_sync_calls_meanwhile())

        assert running_after_sync_stop == [False]
        assert fake_chrome.browsers[0].stopped_on is loop
        assert ended == []
        assert len(start_errors) == 2
        assert all("still stopping" in error for error in start_errors)
        assert len(fake_chrome.browsers) == 1
        assert backend.is_running is False
        assert backend._loop is None
        assert backend._lock.locked() is False

    def test_a_sync_stop_ends_chrome_while_this_threads_stop_async_is_stranded(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stop_async() suspended on a hand-managed loop that stopped (a
        KeyboardInterrupt out of run_until_complete, say) keeps the lock and
        will not resume: a stop on its thread must end Chrome itself, and a
        later stop_async() must not wait for that lock."""
        ended: list[Any] = []
        monkeypatch.setattr("graftpunk.backends.nodriver._end_browser_without_loop", ended.append)
        backend = NoDriverBackend()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(backend._start_async())
            backend._started = True
            real_stop = backend._stop_async
            resume = asyncio.Event()

            async def stop_paused_at_an_await() -> None:
                await resume.wait()
                await real_stop()

            monkeypatch.setattr(backend, "_stop_async", stop_paused_at_an_await)
            stranded = loop.create_task(backend.stop_async())
            loop.run_until_complete(asyncio.sleep(0.05))
            assert backend._held_by_this_thread()

            backend.stop()

            assert ended == fake_chrome.browsers
            assert backend.is_running is False
            with pytest.raises(RuntimeError, match="still stopping"):
                backend.start()

            async def stop_again() -> None:
                await asyncio.wait_for(backend.stop_async(), 2)

            asyncio.run(stop_again())
            assert ended == fake_chrome.browsers

            # The stranded stop, resumed after all, finds the backend stopped.
            resume.set()
            stranded_result = loop.run_until_complete(stranded)
        finally:
            loop.close()

        assert stranded_result is None
        assert backend._lock.locked() is False
        assert len(fake_chrome.browsers) == 1

    def test_a_sync_stop_stops_chrome_on_the_private_loop_while_a_stop_async_is_stranded(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stranded stop_async() was on a hand-managed caller loop that
        stopped, while the browser's private loop still runs: a stop on its
        thread stops Chrome through that private loop, and must not hang."""
        ended: list[Any] = []
        monkeypatch.setattr("graftpunk.backends.nodriver._end_browser_without_loop", ended.append)
        backend = NoDriverBackend()
        backend.start()
        private_loop = backend._loop
        assert private_loop is not None
        real_stop = backend._stop_async
        calls = 0

        async def first_stop_hangs() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.Event().wait()
            await real_stop()

        monkeypatch.setattr(backend, "_stop_async", first_stop_hangs)
        steps: list[str] = []
        failures: list[BaseException] = []

        def on_one_thread() -> None:
            # The holder, the sync stop, and the later stops share this
            # thread, which is what makes the stop skip the lock.
            caller = asyncio.new_event_loop()
            try:
                stranded = caller.create_task(backend.stop_async())
                caller.run_until_complete(asyncio.sleep(0.05))
                assert backend._held_by_this_thread()
                backend.stop()
                steps.append("sync stop returned")
                assert backend.is_running is False
                asyncio.run(asyncio.wait_for(backend.stop_async(), 2))
                steps.append("stop_async returned")
                assert caller.run_until_complete(asyncio.wait_for(stranded, 2)) is None
                steps.append("stranded stop resumed")
            except BaseException as exc:
                failures.append(exc)
            finally:
                caller.close()

        worker = threading.Thread(target=on_one_thread, daemon=True)
        worker.start()
        worker.join(10)

        assert not worker.is_alive(), f"hung after {steps}"
        assert failures == []
        assert steps == ["sync stop returned", "stop_async returned", "stranded stop resumed"]
        assert fake_chrome.browsers[0].stopped_on is private_loop
        assert ended == []
        assert private_loop.is_closed()
        assert backend.is_running is False
        assert backend._lock.locked() is False

    def test_a_second_stop_async_on_the_thread_of_a_paused_one_stops_chrome_on_its_loop(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first never finishes; the second does not wait for it."""
        ended: list[Any] = []
        monkeypatch.setattr("graftpunk.backends.nodriver._end_browser_without_loop", ended.append)
        backend = NoDriverBackend()
        backend.start()
        loop = backend._loop
        real_stop = backend._stop_async
        calls = 0

        async def first_stop_hangs() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.Event().wait()
            await real_stop()

        monkeypatch.setattr(backend, "_stop_async", first_stop_hangs)

        async def stop_twice() -> None:
            first = asyncio.ensure_future(backend.stop_async())
            await asyncio.sleep(0.05)
            await asyncio.wait_for(backend.stop_async(), 2)
            # Its call on the private loop was cancelled when the loop closed.
            return await first

        assert asyncio.run(stop_twice()) is None
        assert fake_chrome.browsers[0].stopped_on is loop
        assert ended == []
        assert backend.is_running is False
        assert backend._lock.locked() is False

    def test_two_stop_async_calls_at_once_collect_chromes_exit_status(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On Linux the private loop's child watcher reaps Chrome, so a loop
        closed while either stop is still ending Chrome leaves it a zombie,
        with its pipes open. The stand-in exits 0.3 s after SIGTERM, as
        Chrome takes a moment to."""
        import sys

        nodriver = pytest.importorskip("nodriver")
        monkeypatch.setattr("graftpunk.chrome_orphans._ARMED", True)
        monkeypatch.setattr("graftpunk.backends.nodriver.prepare_browser_launch", lambda: None)
        profile = temp_profiles / "uc_two_stops"
        profile.mkdir()
        processes: list[asyncio.subprocess.Process] = []

        class _SignalledBrowser(_Browser):
            async def get(self, _url: str) -> SimpleNamespace:
                return SimpleNamespace()

            def stop(self) -> None:
                self._process.terminate()

        async def start(**_kwargs: Any) -> _SignalledBrowser:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _CHILD_UNTIL_TERM,
                str(profile),
                "0.3",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert process.stdout is not None
            assert (await process.stdout.readline()).strip() == b"ready"
            processes.append(process)
            return _SignalledBrowser(
                process, SimpleNamespace(uses_custom_data_dir=False, user_data_dir=str(profile))
            )

        monkeypatch.setattr(nodriver, "start", start)
        backend = NoDriverBackend()
        backend.start()

        async def stop_twice() -> None:
            await asyncio.gather(backend.stop_async(), backend.stop_async())

        asyncio.run(stop_twice())

        [process] = processes
        assert process.returncode == 0
        assert process._transport.is_closing() is True  # type: ignore[attr-defined]
        assert not profile.exists()
        assert backend.is_running is False
        assert backend._lock.locked() is False

    def test_a_cancelled_stop_async_on_the_private_loop_lets_that_loop_stop_promptly(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stop_async() awaited on the browser's own private loop and
        cancelled ends Chrome on a worker thread. That worker must not join
        the loop's thread, whose teardown waits for the stop, which waits for
        the worker: the join would stall for its whole timeout."""
        import sys
        import time

        from graftpunk.backends import nodriver as nodriver_backend

        nodriver = pytest.importorskip("nodriver")
        monkeypatch.setattr("graftpunk.chrome_orphans._ARMED", True)
        monkeypatch.setattr("graftpunk.backends.nodriver.prepare_browser_launch", lambda: None)
        profile = temp_profiles / "uc_cancelled_on_private"
        profile.mkdir()
        processes: list[asyncio.subprocess.Process] = []

        class _SignalledBrowser(_Browser):
            async def get(self, _url: str) -> SimpleNamespace:
                return SimpleNamespace()

            def stop(self) -> None:
                self._process.terminate()

        async def start(**_kwargs: Any) -> _SignalledBrowser:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _CHILD_UNTIL_TERM,
                str(profile),
                "0.5",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert process.stdout is not None
            assert (await process.stdout.readline()).strip() == b"ready"
            processes.append(process)
            return _SignalledBrowser(
                process, SimpleNamespace(uses_custom_data_dir=False, user_data_dir=str(profile))
            )

        monkeypatch.setattr(nodriver, "start", start)
        backend = NoDriverBackend()
        backend.start()
        private = backend._private_loop
        assert private is not None
        caller_saw: list[type[BaseException]] = []

        async def stop_on_the_private_loop() -> None:
            try:
                await asyncio.wait_for(backend.stop_async(), 0.05)
            except TimeoutError as exc:
                caller_saw.append(type(exc))

        with capture_logs() as logs:
            started = time.monotonic()
            future = asyncio.run_coroutine_threadsafe(stop_on_the_private_loop(), private.loop)
            future.result(10)
            private.thread.join(10)
            elapsed = time.monotonic() - started

        assert caller_saw == [TimeoutError]
        assert not private.thread.is_alive()
        assert elapsed < nodriver_backend._LOOP_JOIN_TIMEOUT_S / 2
        assert "nodriver_loop_thread_still_running" not in [log["event"] for log in logs]
        [process] = processes
        assert process.returncode == 0
        assert not profile.exists()
        assert backend.is_running is False
        assert backend._lock.locked() is False

    def test_a_stop_on_the_thread_of_a_cancelled_one_keeps_the_loop_until_chrome_has_ended(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cancelled stop ends Chrome without the loop, on a worker thread.
        The loop's child watcher collects Chrome's exit status on Linux, so the
        loop must still be open when that end finishes."""
        import time

        backend = NoDriverBackend()
        backend.start()
        loop = backend._loop
        assert loop is not None
        ending = threading.Event()
        loop_closed_when_ended: list[bool] = []

        def slow_end(_browser: Any) -> None:
            ending.set()
            time.sleep(0.3)
            loop_closed_when_ended.append(loop.is_closed())

        monkeypatch.setattr("graftpunk.backends.nodriver._end_browser_without_loop", slow_end)
        real_stop = backend._stop_async

        async def stop_that_hangs_on_a_live_browser() -> None:
            if backend._browser is not None:
                await asyncio.Event().wait()
            await real_stop()

        monkeypatch.setattr(backend, "_stop_async", stop_that_hangs_on_a_live_browser)

        async def cancel_one_then_stop_again() -> None:
            first = asyncio.ensure_future(asyncio.wait_for(backend.stop_async(), 0.05))
            assert await asyncio.to_thread(ending.wait, 5)
            await asyncio.wait_for(backend.stop_async(), 2)
            with pytest.raises(TimeoutError):
                await first

        asyncio.run(cancel_one_then_stop_again())

        assert loop_closed_when_ended == [False]
        assert loop.is_closed()
        assert backend.is_running is False
        assert backend._lock.locked() is False

    def test_a_stop_on_the_thread_of_one_closing_the_loop_does_not_stop_chrome_again(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first has stopped Chrome and waits for the private loop to
        close; the browser must not be stopped again on the caller's loop,
        which it does not belong to."""
        import time

        ended: list[Any] = []
        monkeypatch.setattr("graftpunk.backends.nodriver._end_browser_without_loop", ended.append)
        backend = NoDriverBackend()
        backend.start()
        loop = backend._loop
        private = backend._private_loop
        assert private is not None
        browser = fake_chrome.browsers[0]
        stopped_on: list[asyncio.AbstractEventLoop] = []
        monkeypatch.setattr(browser, "stop", lambda: stopped_on.append(asyncio.get_running_loop()))
        real_close = private.close
        closing = threading.Event()

        def slow_close() -> None:
            closing.set()
            time.sleep(0.3)
            real_close()

        monkeypatch.setattr(private, "close", slow_close)

        async def stop_again_while_the_first_closes_the_loop() -> None:
            first = asyncio.ensure_future(backend.stop_async())
            assert await asyncio.to_thread(closing.wait, 5)
            await asyncio.wait_for(backend.stop_async(), 2)
            await first

        asyncio.run(stop_again_while_the_first_closes_the_loop())

        assert stopped_on == [loop]
        assert ended == []
        assert backend.is_running is False
        assert backend._lock.locked() is False

    def test_a_stop_its_browsers_loop_cancelled_returns_normally_and_ends_chrome(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nobody cancelled the stop_async() call, so it must not raise CancelledError."""
        ended: list[Any] = []
        monkeypatch.setattr("graftpunk.backends.nodriver._end_browser_without_loop", ended.append)
        backend = NoDriverBackend()
        backend.start()
        loop = backend._loop
        assert loop is not None
        running: list[asyncio.Task[Any]] = []
        entered = threading.Event()

        async def stop_that_never_finishes() -> None:
            task = asyncio.current_task()
            assert task is not None
            running.append(task)
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(backend, "_stop_async", stop_that_never_finishes)

        async def stop_while_the_browsers_loop_cancels_it() -> None:
            stop = asyncio.ensure_future(backend.stop_async())
            assert await asyncio.to_thread(entered.wait, 5)
            loop.call_soon_threadsafe(running[0].cancel)
            await stop

        asyncio.run(stop_while_the_browsers_loop_cancels_it())

        assert ended == fake_chrome.browsers
        assert backend.is_running is False


class TestNoDriverBackendCallTimeout:
    """A synchronous call gives up on a CDP call that never answers (#240).

    The fakes' tab can be told to hang, like a Chrome that stopped answering
    without closing its websocket. Before the timeout, such a call blocked its
    caller forever.
    """

    TIMEOUT = 0.2
    # Generous: the call should return a hair after TIMEOUT, but a loaded CI
    # machine can be slow to schedule the threads involved.
    SLACK = 3.0

    @staticmethod
    def _started(backend: NoDriverBackend) -> Any:
        backend.start()
        backend.navigate("https://example.com/")
        tab = backend._page
        assert tab is not None
        return tab

    @pytest.mark.parametrize(
        ("method", "call", "fallback", "event"),
        [
            ("current_url", lambda b: b.current_url, "", "nodriver_current_url_failed"),
            ("page_title", lambda b: b.page_title, "", "nodriver_page_title_failed"),
            ("page_source", lambda b: b.page_source, "", "nodriver_page_source_failed"),
            ("get_user_agent()", lambda b: b.get_user_agent(), "", "nodriver_user_agent_failed"),
            ("get_cookies()", lambda b: b.get_cookies(), [], "nodriver_cookies_failed"),
            (
                "set_cookies()",
                lambda b: b.set_cookies([{"name": "a", "value": "1", "domain": "example.com"}]),
                0,
                "nodriver_backend_cookie_set_failed",
            ),
            (
                "delete_all_cookies()",
                lambda b: b.delete_all_cookies(),
                False,
                "nodriver_backend_delete_cookies_failed",
            ),
        ],
    )
    def test_a_hung_call_returns_its_fallback_and_the_next_call_works(
        self,
        fake_chrome: FakeChrome,
        captured_logs: list[dict[str, Any]],
        method: str,
        call: Callable[[NoDriverBackend], Any],
        fallback: Any,
        event: str,
    ) -> None:
        backend = NoDriverBackend(call_timeout=self.TIMEOUT)
        tab = self._started(backend)
        assert backend._private_loop is not None
        loop_thread = backend._private_loop.thread
        tab.hang = True

        began = time.monotonic()
        result = call(backend)
        elapsed = time.monotonic() - began

        assert result == fallback
        assert self.TIMEOUT <= elapsed < self.TIMEOUT + self.SLACK
        assert tab.hung_calls == 1
        assert tab.cancelled_calls == 1
        [warning] = _warnings(captured_logs)
        assert warning["event"] == event
        assert warning["error"] == f"NoDriverBackend.{method} did not finish within 0.2 s"

        tab.hang = False
        assert backend.page_title == "Example Domain"
        assert backend.set_cookies([{"name": "a", "value": "1", "domain": "example.com"}]) == 1
        assert backend._private_loop is not None
        assert backend._private_loop.thread is loop_thread
        assert loop_thread.is_alive()
        assert fake_chrome.misuse == []
        backend.stop()

    def test_a_hung_navigate_raises_browser_error_and_the_next_one_works(
        self, fake_chrome: FakeChrome
    ) -> None:
        backend = NoDriverBackend(navigate_timeout=self.TIMEOUT)
        backend.start()
        fake_chrome.hang_get = True

        began = time.monotonic()
        with pytest.raises(BrowserError, match=r"NoDriverBackend\.navigate\(\) did not finish"):
            backend.navigate("https://example.com/slow")
        elapsed = time.monotonic() - began

        assert self.TIMEOUT <= elapsed < self.TIMEOUT + self.SLACK
        fake_chrome.hang_get = False
        backend.navigate("https://example.com/")
        assert backend.current_url == "https://example.com/"
        backend.stop()

    def test_a_timed_out_navigate_stops_the_load_so_the_tab_answers_at_once(
        self, fake_chrome: FakeChrome
    ) -> None:
        """Chrome keeps loading after the wait is cancelled; Page.stopLoading ends it.

        Without it, the tab answers no evaluate until the load ends, so the
        getters wait out call_timeout and return "".
        """
        backend = NoDriverBackend(navigate_timeout=self.TIMEOUT, call_timeout=30)
        tab = self._started(backend)
        fake_chrome.hang_get = True

        with pytest.raises(BrowserError, match=r"NoDriverBackend\.navigate\(\) did not finish"):
            backend.navigate("https://example.com/slow")
        began = time.monotonic()
        url = backend.current_url
        elapsed = time.monotonic() - began

        assert url == "https://example.com/"
        assert elapsed < self.SLACK
        assert tab.stop_loading_calls == 1
        fake_chrome.hang_get = False
        backend.navigate("https://example.com/next")
        assert backend.current_url == "https://example.com/next"
        backend.stop()

    def test_a_navigate_with_no_tab_recorded_stops_the_load_in_the_first_page_target(
        self, fake_chrome: FakeChrome
    ) -> None:
        """Browser.get navigates the first page target; with no _page, stop that one."""
        backend = NoDriverBackend(navigate_timeout=self.TIMEOUT)
        backend.start()
        first_tab = fake_chrome.browsers[0].tabs[0]
        backend._page = None
        fake_chrome.hang_get = True

        with pytest.raises(BrowserError):
            backend.navigate("https://example.com/slow")

        assert backend._page is None
        assert first_tab.stop_loading_calls == 1
        assert first_tab.idle.is_set()
        fake_chrome.hang_get = False
        backend.navigate("https://example.com/")
        assert backend.current_url == "https://example.com/"
        backend.stop()

    def test_a_navigate_stops_the_first_page_target_even_when_another_tab_is_recorded(
        self, fake_chrome: FakeChrome
    ) -> None:
        """Browser.get navigates the first page target, not a recorded tab since closed."""
        backend = NoDriverBackend(navigate_timeout=self.TIMEOUT)
        backend.start()
        first_tab = fake_chrome.browsers[0].tabs[0]
        sent_to_recorded: list[Any] = []

        async def record(command: Any) -> None:
            sent_to_recorded.append(command)

        backend._page = SimpleNamespace(send=record)
        fake_chrome.hang_get = True

        with pytest.raises(BrowserError):
            backend.navigate("https://example.com/slow")

        assert first_tab.stop_loading_calls == 1
        assert sent_to_recorded == []
        fake_chrome.hang_get = False
        backend.stop()

    @pytest.mark.parametrize("failure", ["raises", "hangs"])
    def test_a_stop_loading_that_fails_is_logged_and_the_navigate_error_stands(
        self,
        fake_chrome: FakeChrome,
        captured_logs: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        failure: str,
    ) -> None:
        monkeypatch.setattr("graftpunk.backends.nodriver._STOP_LOADING_TIMEOUT_S", self.TIMEOUT)
        backend = NoDriverBackend(navigate_timeout=self.TIMEOUT)
        tab = self._started(backend)
        if failure == "raises":
            tab.fail_with = RuntimeError("target closed")
        else:
            tab.hang = True
        fake_chrome.hang_get = True

        began = time.monotonic()
        with pytest.raises(BrowserError, match=r"NoDriverBackend\.navigate\(\) did not finish"):
            backend.navigate("https://example.com/slow")
        elapsed = time.monotonic() - began

        assert elapsed < 2 * self.TIMEOUT + self.SLACK
        [warning] = _warnings(captured_logs)
        assert warning["event"] == "nodriver_backend_stop_loading_failed"
        expected = (
            "target closed"
            if failure == "raises"
            else "NoDriverBackend.navigate() stopping the load did not finish within 0.2 s"
        )
        assert warning["error"] == expected
        backend.stop()

    def test_start_and_stop_are_not_limited_by_call_timeout(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A start or stop slower than call_timeout still finishes."""
        import graftpunk.backends.nodriver as nodriver_module

        start = fake_chrome.start

        async def slow_start(**kwargs: Any) -> Any:
            await asyncio.sleep(0.3)
            return await start(**kwargs)

        monkeypatch.setattr("nodriver.start", slow_start)
        reap = nodriver_module._reap_browser_process
        reaped: list[bool] = []

        async def slow_reap(proc: Any) -> None:
            await asyncio.sleep(0.3)
            await reap(proc)
            reaped.append(True)

        monkeypatch.setattr(nodriver_module, "_reap_browser_process", slow_reap)
        backend = NoDriverBackend(call_timeout=0.05)

        backend.start()
        assert backend.is_running
        browser = fake_chrome.browsers[0]
        backend.stop()

        assert not backend.is_running
        assert browser.stopped_on is not None
        assert reaped == [True]

    def test_navigate_has_its_own_limit(self, fake_chrome: FakeChrome) -> None:
        """A page load slower than call_timeout but within navigate_timeout succeeds."""
        backend = NoDriverBackend(call_timeout=0.05, navigate_timeout=5)
        backend.start()
        browser = fake_chrome.browsers[0]
        get = browser.get

        async def slow_get(url: str) -> Any:
            await asyncio.sleep(0.3)
            return await get(url)

        browser.get = slow_get  # type: ignore[method-assign]

        backend.navigate("https://example.com/")

        assert backend.current_url == "https://example.com/"
        backend.stop()

    @pytest.mark.parametrize(("call_timeout", "expected"), [(0.1, ""), (None, "Example Domain")])
    def test_none_waits_for_a_slow_call(
        self, fake_chrome: FakeChrome, call_timeout: float | None, expected: str
    ) -> None:
        backend = NoDriverBackend(call_timeout=call_timeout)
        tab = self._started(backend)
        evaluate = tab.evaluate

        async def slow_evaluate(expression: str) -> str:
            await asyncio.sleep(0.4)
            return await evaluate(expression)

        tab.evaluate = slow_evaluate

        assert backend.page_title == expected
        backend.stop()

    def test_the_defaults(self) -> None:
        backend = NoDriverBackend()

        assert backend._call_timeout == 60.0
        assert backend._navigate_timeout == 120.0

    @pytest.mark.parametrize(
        "value",
        [0, -1, float("nan"), float("inf"), True, "5", threading.TIMEOUT_MAX * 2, 1e300],
    )
    @pytest.mark.parametrize("name", ["call_timeout", "navigate_timeout"])
    def test_an_invalid_timeout_is_refused(self, name: str, value: Any) -> None:
        with pytest.raises(ValueError, match=f"{name} must be None or a positive number"):
            NoDriverBackend(**{name: value})

    def test_the_largest_timeout_the_wait_accepts_is_allowed(self) -> None:
        backend = NoDriverBackend(call_timeout=threading.TIMEOUT_MAX)

        assert backend._call_timeout == threading.TIMEOUT_MAX

    def test_a_call_that_finishes_as_the_wait_expires_returns_its_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """wait() reports not done, but the call finished before the recheck."""
        import concurrent.futures

        backend = with_private_loop(NoDriverBackend())
        release = threading.Event()

        async def work() -> str:
            await asyncio.to_thread(release.wait)
            return "done"

        real_wait = concurrent.futures.wait

        def wait_that_expires_as_the_call_finishes(fs: Any, timeout: Any = None) -> Any:
            release.set()
            real_wait(fs)
            return SimpleNamespace(done=set(), not_done=set(fs))

        monkeypatch.setattr(concurrent.futures, "wait", wait_that_expires_as_the_call_finishes)

        assert backend._run_async(work(), timeout=1, method="x") == "done"

    @pytest.mark.parametrize(
        ("ending", "expected"), [("return", "done"), ("raise", ValueError("own error"))]
    )
    def test_a_call_that_finishes_before_the_cancel_reaches_it_keeps_its_outcome(
        self, monkeypatch: pytest.MonkeyPatch, ending: str, expected: Any
    ) -> None:
        """The cancel loses the race: the coroutine runs to the end after cancel().

        The coroutine blocks the loop thread, so the cancel cannot reach its
        task, and it is released by the cancel itself (the future's done
        callback runs in the cancelling thread).
        """
        import concurrent.futures

        backend = with_private_loop(NoDriverBackend())
        entered = threading.Event()
        release = threading.Event()

        async def work() -> str:
            entered.set()
            release.wait(5)  # blocks the loop, so the cancel cannot land
            if ending == "raise":
                raise ValueError("own error")
            return "done"

        def wait_that_expires(fs: Any, timeout: Any = None) -> Any:
            assert entered.wait(5)
            for future in fs:
                future.add_done_callback(lambda _f: release.set())
            return SimpleNamespace(done=set(), not_done=set(fs))

        monkeypatch.setattr(concurrent.futures, "wait", wait_that_expires)

        if isinstance(expected, Exception):
            with pytest.raises(ValueError, match="own error"):
                backend._run_async(work(), timeout=1, method="x")
        else:
            assert backend._run_async(work(), timeout=1, method="x") == expected
        assert release.is_set()

    def test_a_call_still_running_after_the_cancel_times_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import concurrent.futures

        backend = with_private_loop(NoDriverBackend())
        entered = threading.Event()
        cancelled = threading.Event()

        async def work() -> str:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "unreachable"

        def wait_that_expires(fs: Any, timeout: Any = None) -> Any:
            # Expire only once the call is running, so the cancel reaches a
            # started coroutine rather than one the loop never began.
            entered.wait(timeout=5)
            return SimpleNamespace(done=set(), not_done=set(fs))

        monkeypatch.setattr(concurrent.futures, "wait", wait_that_expires)

        with pytest.raises(TimeoutError, match=r"NoDriverBackend\.x did not finish within 1 s"):
            backend._run_async(work(), timeout=1, method="x")
        # The patched wait also stands in for the wait on the cancellation, so
        # the cancel may land on the loop thread after the raise; it must land.
        assert cancelled.wait(timeout=5)

    def test_the_timeouts_survive_get_state_and_stay_out_of_the_start_options(self) -> None:
        original = NoDriverBackend(call_timeout=7.5, navigate_timeout=None, lang="en-US")

        recreated = NoDriverBackend.from_state(original.get_state())

        assert recreated._call_timeout == 7.5
        assert recreated._navigate_timeout is None
        assert recreated._options == {"lang": "en-US"}

    def test_the_timeouts_survive_a_pickle(self) -> None:
        original = NoDriverBackend(call_timeout=3, navigate_timeout=None)

        copy = pickle.loads(pickle.dumps(original))  # noqa: S301 - round-trips its own bytes

        assert copy._call_timeout == 3
        assert copy._navigate_timeout is None

    def test_a_backend_pickled_before_the_timeouts_existed_gets_the_defaults(self) -> None:
        state = NoDriverBackend().__getstate__()
        del state["_call_timeout"]
        del state["_navigate_timeout"]
        restored = NoDriverBackend.__new__(NoDriverBackend)

        restored.__setstate__(state)

        assert restored._call_timeout == 60.0
        assert restored._navigate_timeout == 120.0


def _resource_warnings(caught: list[warnings.WarningMessage]) -> list[str]:
    """The warnings a subprocess transport or its child left open gives when collected."""
    return [
        str(w.message)
        for w in caught
        if issubclass(w.category, ResourceWarning)
        and ("transport" in str(w.message) or "subprocess" in str(w.message))
    ]


def _within(seconds: float, call: Callable[[], Any]) -> float:
    """Run *call* on a watchdog thread, fail if it is still running after *seconds*; its duration.

    A lock wait that regresses into waiting forever then fails the test
    instead of hanging the suite. What *call* raises is raised here.
    """
    errors: list[BaseException] = []
    started = time.monotonic()

    def run() -> None:
        try:
            call()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            errors.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), f"still running after {seconds} s"
    if errors:
        raise errors[0]
    return time.monotonic() - started


class _Stranded:
    """A start_async() or stop_async() left holding the lock on a loop that stopped.

    A thread of its own runs a hand-managed loop until the call holds the
    backend's lock and *until* is true, then stops running it, the way a ``run_until_complete()``
    interrupted by Ctrl-C does. ``finish()`` resumes the call, cancels it,
    and closes the loop, so the test leaves nothing running.
    """

    def __init__(
        self,
        backend: NoDriverBackend,
        holder: Callable[[], Coroutine[Any, Any, Any]],
        holder_name: str,
        until: Callable[[], bool] = lambda: True,
    ) -> None:
        self.errors: list[BaseException] = []
        self._ready = threading.Event()
        self._resume = threading.Event()

        def main() -> None:
            loop = asyncio.new_event_loop()
            try:
                task = loop.create_task(holder())

                async def until_held() -> None:
                    for _ in range(500):
                        if backend._lock_holder == holder_name and until():
                            return
                        await asyncio.sleep(0.01)
                    raise AssertionError(f"{holder_name}() never got there")

                loop.run_until_complete(until_held())
                self._ready.set()
                self._resume.wait(30)
                task.cancel()
                loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
            except BaseException as exc:  # noqa: BLE001 - reported by __init__ or finish()
                self.errors.append(exc)
            finally:
                self._ready.set()
                loop.close()

        self._thread = threading.Thread(target=main, daemon=True)
        self._thread.start()
        assert self._ready.wait(10)
        assert self.errors == []

    def finish(self) -> None:
        self._resume.set()
        self._thread.join(10)
        assert not self._thread.is_alive()
        assert self.errors == []


async def _never_finishes() -> None:
    await asyncio.Event().wait()


async def _ticks_during(call: Coroutine[Any, Any, Any]) -> int:
    """Await *call* while counting the turns a 10 ms ticker gets on this loop."""
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(tick())
    try:
        await call
    finally:
        ticker.cancel()
    return ticks


class TestNoDriverBackendStartAsyncTakesTheLock:
    """start_async() is serialized with start() and the stops across threads (#243)."""

    def test_a_start_async_and_a_start_on_other_threads_launch_one_browser(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import nodriver

        async def slow_start(**kwargs: Any) -> Any:
            await asyncio.sleep(0.2)
            return await fake_chrome.start(**kwargs)

        monkeypatch.setattr(nodriver, "start", slow_start)
        backend = NoDriverBackend()
        barrier = threading.Barrier(2)
        done = threading.Event()
        errors: list[BaseException] = []

        def start_async_and_keep_its_loop() -> None:
            async def main() -> None:
                barrier.wait(5)
                await backend.start_async()
                # The browser may live on this loop: keep it running until
                # the test is done with it.
                await asyncio.to_thread(done.wait, 10)
                await backend.stop_async()

            try:
                asyncio.run(main())
            except BaseException as exc:  # noqa: BLE001 - reported by the assertion
                errors.append(exc)

        def start() -> None:
            barrier.wait(5)
            backend.start()

        async_starter = threading.Thread(target=start_async_and_keep_its_loop, daemon=True)
        async_starter.start()
        try:
            _within(10, start)
            # Whichever started second waited for the first and found it started.
            launched = len(fake_chrome.browsers)
            running = backend.is_running
        finally:
            done.set()
            async_starter.join(10)
            backend.stop()

        assert launched == 1
        assert running is True
        assert not async_starter.is_alive()
        assert errors == []
        assert fake_chrome.browsers[0].stopped_on is not None
        assert fake_chrome.misuse == []
        assert backend._lock.locked() is False

    def test_start_async_waits_for_another_threads_start_without_blocking_its_loop(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import nodriver

        launching = threading.Event()

        async def slow_start(**kwargs: Any) -> Any:
            launching.set()
            await asyncio.sleep(0.4)
            return await fake_chrome.start(**kwargs)

        monkeypatch.setattr(nodriver, "start", slow_start)
        backend = NoDriverBackend()
        starter = threading.Thread(target=backend.start, daemon=True)
        starter.start()
        assert launching.wait(5)

        async def start_async() -> int:
            return await _ticks_during(asyncio.wait_for(backend.start_async(), 5))

        try:
            ticks = asyncio.run(start_async())
            starter.join(5)
            launched = len(fake_chrome.browsers)
            private = backend._private_loop
        finally:
            backend.stop()

        assert ticks >= 10
        # The sync start launched it; start_async() then found it started on
        # a loop that still runs.
        assert launched == 1
        assert private is not None
        assert fake_chrome.browsers[0].stopped_on is not None

    def test_a_stop_on_another_thread_waits_for_start_async_and_stops_its_browser(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import nodriver

        backend = NoDriverBackend()
        lock = _ContendedLock()
        backend._lock = lock  # type: ignore[assignment]
        launching = threading.Event()

        async def start_once_a_stop_waits(**kwargs: Any) -> Any:
            launching.set()
            await asyncio.to_thread(lock.contended.wait, 5)
            return await fake_chrome.start(**kwargs)

        monkeypatch.setattr(nodriver, "start", start_once_a_stop_waits)
        loops: list[asyncio.AbstractEventLoop] = []
        stopped = threading.Event()

        def start_async_and_keep_its_loop() -> None:
            async def main() -> None:
                loops.append(asyncio.get_running_loop())
                await backend.start_async()
                await asyncio.to_thread(stopped.wait, 10)

            asyncio.run(main())

        async_starter = threading.Thread(target=start_async_and_keep_its_loop, daemon=True)
        async_starter.start()
        try:
            assert launching.wait(5)
            _within(10, backend.stop)
        finally:
            stopped.set()
            async_starter.join(10)

        [browser] = fake_chrome.browsers
        assert browser.stopped_on is loops[0]
        assert backend.is_running is False
        assert backend._lock.locked() is False

    def test_a_start_async_cancelled_while_waiting_for_the_lock_holds_nothing(
        self, fake_chrome: FakeChrome
    ) -> None:
        backend = NoDriverBackend()
        held = threading.Event()
        release = threading.Event()

        def hold_the_lock() -> None:
            assert backend._acquire_lock("start") is None
            held.set()
            release.wait(10)
            backend._release_lock()

        holder = threading.Thread(target=hold_the_lock, daemon=True)
        holder.start()
        assert held.wait(5)

        async def give_up_waiting() -> None:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(backend.start_async(), 0.2)

        try:
            asyncio.run(give_up_waiting())
        finally:
            release.set()
            holder.join(5)

        assert backend._lock.locked() is False
        assert fake_chrome.browsers == []
        backend.start()
        assert len(fake_chrome.browsers) == 1
        backend.stop()

    def test_a_second_start_async_on_the_thread_of_one_in_progress_raises(
        self, fake_chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No call waits for a lock its own thread holds."""
        import nodriver

        async def slow_start(**kwargs: Any) -> Any:
            await asyncio.sleep(0.2)
            return await fake_chrome.start(**kwargs)

        monkeypatch.setattr(nodriver, "start", slow_start)
        backend = NoDriverBackend()

        async def start_twice_at_once() -> list[Any]:
            results = await asyncio.wait_for(
                asyncio.gather(
                    backend.start_async(), backend.start_async(), return_exceptions=True
                ),
                5,
            )
            await backend.stop_async()
            return results

        first, second = asyncio.run(start_twice_at_once())

        assert first is None
        assert isinstance(second, RuntimeError)
        assert "start_async() on this thread is still starting" in str(second)
        assert len(fake_chrome.browsers) == 1
        assert backend._lock.locked() is False


class TestNoDriverBackendAbandonedLockHolder:
    """A start_async() or stop_async() that never resumes cannot block other threads (#243)."""

    BOUND = 0.5

    @pytest.fixture(autouse=True)
    def _short_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("graftpunk.backends.nodriver._ABANDONED_HOLD_S", self.BOUND)
        monkeypatch.setattr("graftpunk.chrome_orphans._ARMED", True)

    @staticmethod
    def _give_chrome(browser: Any, child: Any, profile: Path) -> None:
        """Make a fake browser stand for a real child process with a temp profile."""
        browser._process = child
        browser.config = SimpleNamespace(uses_custom_data_dir=False, user_data_dir=str(profile))

    def _start_then_strand_a_stop_async(
        self, backend: NoDriverBackend, child: Any, profile: Path, monkeypatch: pytest.MonkeyPatch
    ) -> _Stranded:
        """Start on the private loop, then leave a stop_async() holding the lock on a dead loop."""
        backend.start()
        assert backend._browser is not None
        self._give_chrome(backend._browser, child, profile)
        monkeypatch.setattr(backend, "_stop_async", _never_finishes)
        return _Stranded(backend, backend.stop_async, "stop_async")

    @staticmethod
    def _given_up(logs: list[dict[str, Any]]) -> list[tuple[str, str]]:
        return [
            (e["method"], e["holder"])
            for e in logs
            if e["event"] == "nodriver_backend_lock_holder_abandoned"
        ]

    def test_a_stop_gives_up_on_an_abandoned_stop_async_and_ends_chrome(
        self,
        fake_chrome: FakeChrome,
        temp_profiles: Path,
        monkeypatch: pytest.MonkeyPatch,
        captured_logs: list[dict[str, Any]],
    ) -> None:
        profile = temp_profiles / "uc_abandoned_stop"
        profile.mkdir()
        backend = NoDriverBackend()
        with _chrome_like_child(profile) as child:
            stranded = self._start_then_strand_a_stop_async(backend, child, profile, monkeypatch)
            try:
                elapsed = _within(10, backend.stop)
                exited = child.poll() is not None
            finally:
                stranded.finish()

        assert self.BOUND <= elapsed < self.BOUND + 5
        assert exited
        assert not profile.exists()
        assert backend.is_running is False
        assert backend._loop is None
        assert self._given_up(captured_logs) == [("stop", "stop_async")]
        assert backend._lock.locked() is False

    def test_a_stop_async_on_another_thread_gives_up_and_ends_chrome_off_its_loop(
        self, fake_chrome: FakeChrome, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = temp_profiles / "uc_abandoned_stop_async"
        profile.mkdir()
        backend = NoDriverBackend()
        ticks: list[int] = []

        def stop_async() -> None:
            ticks.append(asyncio.run(_ticks_during(backend.stop_async())))

        # Chrome takes 0.5 s to exit after SIGTERM, which a blocked loop
        # would not tick through.
        with _chrome_like_child(profile, exit_delay=0.5) as child:
            stranded = self._start_then_strand_a_stop_async(backend, child, profile, monkeypatch)
            try:
                elapsed = _within(10, stop_async)
                exited = child.poll() is not None
            finally:
                stranded.finish()

        assert self.BOUND <= elapsed < self.BOUND + 5
        assert exited
        assert ticks[0] >= 50
        assert not profile.exists()
        assert backend.is_running is False

    def test_a_start_raises_naming_the_abandoned_stop_async_and_later_ones_raise_at_once(
        self, fake_chrome: FakeChrome, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = temp_profiles / "uc_abandoned_then_start"
        profile.mkdir()
        backend = NoDriverBackend()
        errors: list[str] = []

        def start() -> None:
            with pytest.raises(RuntimeError) as raised:
                backend.start()
            errors.append(str(raised.value))

        def start_async() -> None:
            with pytest.raises(RuntimeError) as raised:
                asyncio.run(backend.start_async())
            errors.append(str(raised.value))

        with _chrome_like_child(profile) as child:
            stranded = self._start_then_strand_a_stop_async(backend, child, profile, monkeypatch)
            try:
                first = _within(10, start)
                again = _within(10, start)
                from_async = _within(10, start_async)
                running = backend.is_running
            finally:
                stranded.finish()
                backend.stop()

        assert self.BOUND <= first < self.BOUND + 2
        assert again < self.BOUND
        assert from_async < self.BOUND
        assert errors[0].startswith("NoDriverBackend.start() gave up after waiting 0.5 s")
        assert errors[2].startswith("NoDriverBackend.start_async() gave up after waiting 0.5 s")
        assert all("for a stop_async() on another thread" in error for error in errors)
        # Refusing to start changes nothing: the browser is still there to stop.
        assert running is True
        assert len(fake_chrome.browsers) == 1

    def test_a_stop_gives_up_on_an_abandoned_start_async_and_ends_its_chrome(
        self,
        fake_chrome: FakeChrome,
        temp_profiles: Path,
        monkeypatch: pytest.MonkeyPatch,
        captured_logs: list[dict[str, Any]],
    ) -> None:
        """The start launched Chrome and was left waiting for its first tab."""
        import nodriver

        profile = temp_profiles / "uc_abandoned_start"
        profile.mkdir()
        fake_chrome.hang_get = True
        backend = NoDriverBackend()
        errors: list[str] = []

        def start() -> None:
            with pytest.raises(RuntimeError) as raised:
                backend.start()
            errors.append(str(raised.value))

        with _chrome_like_child(profile) as child:

            async def start_with_a_real_chrome(**kwargs: Any) -> Any:
                browser = await fake_chrome.start(**kwargs)
                self._give_chrome(browser, child, profile)
                return browser

            monkeypatch.setattr(nodriver, "start", start_with_a_real_chrome)
            stranded = _Stranded(
                backend, backend.start_async, "start_async", fake_chrome.get_entered.is_set
            )
            try:
                elapsed = _within(10, backend.stop)
                exited = child.poll() is not None
                _within(10, start)
            finally:
                stranded.finish()

        assert self.BOUND <= elapsed < self.BOUND + 5
        assert exited
        assert not profile.exists()
        assert backend.is_running is False
        assert self._given_up(captured_logs) == [("stop", "start_async"), ("start", "start_async")]
        assert len(errors) == 1
        assert "for a start_async() on another thread" in errors[0]
        assert backend._lock.locked() is False

    def test_a_sync_start_slower_than_the_bound_is_waited_for(
        self,
        fake_chrome: FakeChrome,
        monkeypatch: pytest.MonkeyPatch,
        captured_logs: list[dict[str, Any]],
    ) -> None:
        """Its thread is running the start, so a stop waits it out and stops its browser."""
        import nodriver

        backend = NoDriverBackend()
        lock = _ContendedLock()
        backend._lock = lock  # type: ignore[assignment]
        launching = threading.Event()

        async def start_well_after_a_stop_waits(**kwargs: Any) -> Any:
            launching.set()
            await asyncio.to_thread(lock.contended.wait, 5)
            await asyncio.sleep(self.BOUND * 3)
            return await fake_chrome.start(**kwargs)

        monkeypatch.setattr(nodriver, "start", start_well_after_a_stop_waits)
        starter = threading.Thread(target=backend.start, daemon=True)
        starter.start()
        assert launching.wait(5)

        elapsed = _within(10, backend.stop)
        starter.join(5)

        assert elapsed >= self.BOUND * 3
        [browser] = fake_chrome.browsers
        assert browser.stopped_on is not None
        assert backend.is_running is False
        assert self._given_up(captured_logs) == []

    def test_a_stop_async_that_finishes_within_the_bound_is_waited_for(
        self,
        fake_chrome: FakeChrome,
        monkeypatch: pytest.MonkeyPatch,
        captured_logs: list[dict[str, Any]],
    ) -> None:
        """A working stop_async() on another thread is not given up on."""
        ended: list[Any] = []
        monkeypatch.setattr("graftpunk.backends.nodriver._end_browser_without_loop", ended.append)
        backend = NoDriverBackend()
        backend.start()
        loop = backend._loop
        real_stop = backend._stop_async
        stopping = threading.Event()

        async def slow_stop() -> None:
            stopping.set()
            await asyncio.sleep(self.BOUND / 2)
            await real_stop()

        monkeypatch.setattr(backend, "_stop_async", slow_stop)
        stopper = threading.Thread(target=lambda: asyncio.run(backend.stop_async()), daemon=True)
        stopper.start()
        assert stopping.wait(5)

        _within(10, backend.stop)
        stopper.join(5)

        assert fake_chrome.browsers[0].stopped_on is loop
        assert ended == []
        assert backend.is_running is False
        assert self._given_up(captured_logs) == []
