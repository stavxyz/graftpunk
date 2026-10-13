"""Tests for NoDriver browser backend."""

import asyncio
import os
import pickle
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from graftpunk.backends import get_backend, list_backends
from graftpunk.backends.base import Cookie
from graftpunk.backends.nodriver import NoDriverBackend
from graftpunk.chrome_orphans import OWNER_SWITCH, base_browser_args
from graftpunk.exceptions import BrowserError

from .conftest import FakeCdpTab, FakeChrome

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
        backend = NoDriverBackend()
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
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = _stoppable_browser(stop=MagicMock(side_effect=exc))
        backend._page = MagicMock()

        backend.stop()

        assert backend.is_running is False

    def test_stop_logs_debug_for_expected_errors(self, captured_logs: list[dict[str, Any]]) -> None:
        """stop() logs DEBUG for expected cleanup errors like 'browser is already closed'."""
        backend = NoDriverBackend()
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
        backend = NoDriverBackend()
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
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = None  # Page is None but browser is running

        # The async methods check `if self._page is None: return ""`
        assert backend.current_url == ""
        assert backend.page_title == ""
        assert backend.page_source == ""
        assert backend.get_user_agent() == ""

    def test_delete_all_cookies_safe_when_page_none(self) -> None:
        """delete_all_cookies() is safe when _page is None."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = None

        assert backend.delete_all_cookies() is True


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
        backend = NoDriverBackend()
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

        backend = NoDriverBackend()

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
        backend = NoDriverBackend()
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

    nodriver's Browser.stop() sends SIGTERM but never awaits proc.wait(),
    leaving Chrome as a zombie under the live python parent. The backend's
    _stop_async() compensates by awaiting the subprocess after browser.stop()
    via the _reap_browser_process helper.
    """

    @staticmethod
    def _make_proc(
        wait_behavior: list,
        kill_raises: OSError | None = None,
    ) -> MagicMock:
        """Build a MagicMock asyncio subprocess.

        wait_behavior: list consumed in order on each .wait() call. Each entry
            is either an int (returned exit code), an Exception (raised), or
            the string "block" (wait blocks until cancelled by wait_for).
        kill_raises: if not None, .kill() raises this OSError. Typed as
            OSError because the helper only handles ProcessLookupError and
            other OSError subclasses on the kill path.
        """
        proc = MagicMock()
        proc.pid = 12345
        proc.returncode = None
        proc.wait_call_count = 0
        proc.kill_called = False
        results_iter = iter(wait_behavior)

        async def fake_wait():
            proc.wait_call_count += 1
            r = next(results_iter)
            if r == "block":
                await asyncio.Event().wait()
            if isinstance(r, BaseException):
                raise r
            return r

        def fake_kill():
            proc.kill_called = True
            if kill_raises is not None:
                raise kill_raises

        proc.wait = fake_wait
        proc.kill = fake_kill
        return proc

    @staticmethod
    def _make_browser(proc: MagicMock | None) -> MagicMock:
        """Build a MagicMock Browser whose .stop() nulls _process (matches
        nodriver's real behavior in core/browser.py:718)."""
        browser = MagicMock()
        browser._process = proc

        def fake_stop():
            browser._process = None

        browser.stop = fake_stop
        return browser

    async def test_stop_async_reaps_process(self) -> None:
        """_stop_async awaits proc.wait() so the kernel reaps Chrome."""
        proc = self._make_proc(wait_behavior=[0])
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._make_browser(proc)

        await backend._stop_async()

        assert proc.wait_call_count == 1, "proc.wait() should have been awaited exactly once"
        assert proc.kill_called is False, "kill() should not be needed on success"

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

    async def test_stop_async_captures_proc_before_browser_stop_nulls_it(self) -> None:
        """_stop_async must capture _browser._process BEFORE calling stop().

        nodriver's Browser.stop() resets self._process = None inside its
        cleanup loop. If our backend reads _process AFTER stop(), it gets
        None and the helper no-ops, leaving the subprocess as a zombie.
        """
        proc = self._make_proc(wait_behavior=[0])
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._make_browser(proc)

        await backend._stop_async()

        assert proc.wait_call_count == 1, (
            "wait() not awaited — capture-before-stop ordering may be broken"
        )
        # Sanity: fake_stop nulled _process. If our backend read _process
        # after stop(), it would have received None and skipped wait() —
        # which is exactly the bug this test guards against.
        assert backend._browser._process is None

    async def test_stop_async_escalates_to_sigkill_on_term_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When SIGTERM times out, helper escalates to SIGKILL and re-waits."""
        # Tighten timeouts so the test runs in milliseconds.
        monkeypatch.setattr("graftpunk.backends.nodriver._REAP_TERM_TIMEOUT_S", 0.05)
        monkeypatch.setattr("graftpunk.backends.nodriver._REAP_KILL_TIMEOUT_S", 0.05)

        # First wait blocks (simulates Chrome ignoring SIGTERM); second wait
        # returns immediately (simulates exit after SIGKILL).
        proc = self._make_proc(wait_behavior=["block", 0])
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._make_browser(proc)

        await backend._stop_async()

        assert proc.wait_call_count == 2, "Expected two wait() calls (SIGTERM + SIGKILL)"
        assert proc.kill_called is True, "kill() should have been called after SIGTERM timeout"

    async def test_stop_async_logs_when_sigkill_also_times_out(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When even SIGKILL doesn't unblock wait(), log a warning and return."""
        monkeypatch.setattr("graftpunk.backends.nodriver._REAP_TERM_TIMEOUT_S", 0.05)
        monkeypatch.setattr("graftpunk.backends.nodriver._REAP_KILL_TIMEOUT_S", 0.05)

        # Both wait() calls block — simulates a kernel-level wedge.
        proc = self._make_proc(wait_behavior=["block", "block"])
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._make_browser(proc)

        with capture_logs() as cap:
            await backend._stop_async()

        assert proc.wait_call_count == 2
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

        SIGTERM wait timed out; Chrome exited in the window between the
        wait_for timeout and our kill() call. kill() raises
        ProcessLookupError because the PID is gone. This is success — the
        process IS reaped, the wait_for just missed the exit by microseconds.
        """
        monkeypatch.setattr("graftpunk.backends.nodriver._REAP_TERM_TIMEOUT_S", 0.05)

        # First wait blocks; kill raises ProcessLookupError; helper returns
        # without making a second wait call.
        proc = self._make_proc(
            wait_behavior=["block"], kill_raises=ProcessLookupError(3, "No such process")
        )
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._make_browser(proc)

        # Should not raise.
        await backend._stop_async()

        assert proc.wait_call_count == 1, (
            "Helper should not call wait() a second time after ProcessLookupError"
        )
        assert proc.kill_called is True

    def test_sync_stop_also_reaps_process(self) -> None:
        """The sync stop() path inherits the reap via _run_async->_stop_async.

        Regression guard against a future refactor that splits the sync and
        async stop paths and forgets to wire the reap into both.
        """
        proc = self._make_proc(wait_behavior=[0])
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._make_browser(proc)

        backend.stop()  # sync path

        assert proc.wait_call_count == 1, "sync stop() should also reap via _stop_async"

    async def test_stop_async_public_api_reaps_process(self) -> None:
        """The public async stop_async() inherits the reap via _stop_async.

        Symmetric regression guard to test_sync_stop_also_reaps_process —
        BrowserSession.__aexit__ calls stop_async() (not _stop_async()),
        so the public path is production-critical.
        """
        proc = self._make_proc(wait_behavior=[0])
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._make_browser(proc)

        await backend.stop_async()

        assert proc.wait_call_count == 1, "stop_async() should reap via _stop_async"

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
        # Tighten timeouts so SIGTERM-then-SIGKILL completes in milliseconds.
        monkeypatch.setattr("graftpunk.backends.nodriver._REAP_TERM_TIMEOUT_S", 0.05)
        monkeypatch.setattr("graftpunk.backends.nodriver._REAP_KILL_TIMEOUT_S", 0.05)

        # First wait blocks (no SIGTERM was sent because stop() raised);
        # second wait returns 0 after our SIGKILL escalation.
        proc = self._make_proc(wait_behavior=["block", 0])
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
        assert proc.wait_call_count == 2, "Both wait() calls (SIGTERM + SIGKILL) should have run"


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
        assert kwargs["browser_args"] == ["--test-type", f"{OWNER_SWITCH}={os.getpid()}"]

    async def test_configured_browser_args_follow_the_shared_ones(self) -> None:
        started: list = []
        backend = NoDriverBackend(browser_args=["--lang=en-US"])

        with patch.dict("sys.modules", {"nodriver": self._fake_nodriver(started)}):
            await backend._start_async()

        assert started[0]["browser_args"] == [*base_browser_args(), "--lang=en-US"]

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
    backend = NoDriverBackend()
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

    def test_a_backend_with_a_live_loop_pickles_without_it(self) -> None:
        backend = NoDriverBackend(headless=True)

        async def noop() -> None:
            return None

        backend._run_async(noop())
        assert backend._loop is not None

        clone = pickle.loads(pickle.dumps(backend))  # noqa: S301 - round-trips its own bytes

        assert clone._loop is None
        assert clone._private_loop is None
        assert clone._headless is True

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
