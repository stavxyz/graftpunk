"""Shared test helpers for unit tests."""

import asyncio
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

# Rich builds a Console at import time and takes its width from the ambient
# COLUMNS, so on an 80-column terminal table cells truncate and messages wrap
# mid-word — and assertions on rendered output fail for reasons that have
# nothing to do with the code under test. CliRunner's env={"COLUMNS": ...}
# comes too late: the console already exists. Pin every module-level console
# wide instead, once, for the whole unit suite.
_CONSOLE_LOCATIONS = (
    ("graftpunk.console", "out_console"),
    ("graftpunk.console", "err_console"),
    ("graftpunk.cli.main", "console"),
    ("graftpunk.cli.session_commands", "console"),
    ("graftpunk.cli.config_commands", "console"),
    ("graftpunk.cli.keepalive_commands", "console"),
    ("graftpunk.cli.plugin_runtime", "_format_console"),
    ("graftpunk.cli.observe_commands", "console"),
    ("graftpunk.cli.observe_browser", "console"),
    ("graftpunk.cli.scaffold_commands", "console"),
)


@pytest.fixture(autouse=True)
def _wide_consoles(monkeypatch):  # noqa: ANN001, ANN201
    """Render as if on a wide terminal, whatever the developer's terminal is."""
    import importlib

    for module_name, attr in _CONSOLE_LOCATIONS:
        console = getattr(importlib.import_module(module_name), attr)
        monkeypatch.setattr(console, "width", 220)


@pytest.fixture(autouse=True)
def _disarm_chrome_orphan_cleanup(monkeypatch):  # noqa: ANN001, ANN201
    """No unit test may signal a process or delete a directory it does not own.

    The reaper reads the real process table and sends real signals, and the
    browser launch sites call it for real in tests that drive a start. Disarm
    it at the origin, once, for the whole unit suite: reap_orphans and
    remove_stale_temp_profiles then do nothing, whoever calls them, so a new
    caller is covered the day it is written. The reaper's own tests re-arm it
    and inject a fake process table.
    """
    monkeypatch.setattr("graftpunk.chrome_orphans._ARMED", False)


@pytest.fixture(autouse=True)
def _restore_termination_signals():  # noqa: ANN201
    """Give each test the SIGTERM and SIGHUP slots back, and the arming flag.

    A CLI invocation sets ``signals.auto_install`` and a browser launch then
    installs the handlers, both process-wide. Left in place, one test's
    leftovers are ambient state for the rest of the run, and the signal tests
    cannot tell an install apart from an inheritance. One fixture owns all
    three pieces of that state, because they are set by the same flow.

    The flag is cleared first, dispositions are restored next, and the live
    browser registry is put back last, in a ``finally``, so a fixture-cached
    handle from some other task's tests cannot leak across tests even when
    restoring a disposition raises.
    """
    import signal

    from graftpunk import signals

    saved_signals = {}
    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is not None:
            saved_signals[signum] = signal.getsignal(signum)
    saved_browsers = list(signals._LIVE_BROWSERS)
    yield
    signals.auto_install = False
    try:
        for signum, handler in saved_signals.items():
            if handler is not None:
                signal.signal(signum, handler)
            else:
                # getsignal returns None when the disposition was set outside
                # Python, which cannot be restored as it was. Leaving
                # graftpunk's handler in the slot would make one test's
                # install ambient state for the rest of the run, so SIG_DFL is
                # the closest honest restore.
                signal.signal(signum, signal.SIG_DFL)
    finally:
        signals._LIVE_BROWSERS.clear()
        for handle in saved_browsers:
            signals._LIVE_BROWSERS.add(handle)


@pytest.fixture()
def fresh_backend(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    """A private tmp cache dir + a reset of cache.py's backend singleton.

    ``isolated_config`` (autouse) isolates GRAFTPUNK_CONFIG_DIR, but the
    module-global storage-backend singleton survives across tests; reset it
    on both sides so each test builds its backend from this test's env.
    """
    from graftpunk import cache as cache_mod

    monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
    cache_mod._reset_session_storage_backend()
    yield
    cache_mod._reset_session_storage_backend()


@pytest.fixture()
def _fast_login_timings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch login engine timing constants so tests don't wait real time.

    _ELEMENT_WAIT_TIMEOUT (30s) causes deadline-based loops to spin for 30
    real seconds even when asyncio.sleep is mocked.  _LOGIN_POLL_INTERVAL
    (0.5s) is a real sleep between post-submit signal checks.
    _ELEMENT_RETRY_INTERVAL (1s) adds real delay between retry attempts in
    _select_with_retry's deadline loop.

    _NO_SIGNAL_GRACE (3s) is how long a login with no success signal watches
    for its failure signal, in real seconds.

    The post-submit poll's own budget is LoginConfig.timeout, which is per
    plugin rather than a module constant: a test whose login never shows its
    success signal sets a small timeout on its own LoginConfig.
    """
    monkeypatch.setattr("graftpunk.plugins.login_settle._LOGIN_POLL_INTERVAL", 0.001)
    monkeypatch.setattr("graftpunk.plugins.login_settle._NO_SIGNAL_GRACE", 0.01)
    monkeypatch.setattr("graftpunk.plugins.login_engine._ELEMENT_WAIT_TIMEOUT", 0.05)
    monkeypatch.setattr("graftpunk.plugins.login_engine._ELEMENT_RETRY_INTERVAL", 0.001)
    monkeypatch.setattr("graftpunk.plugins.login_engine._LOGIN_NAV_TIMEOUT", 0.05)
    monkeypatch.setattr("graftpunk.plugins.login_engine._FIELD_SETTLE_DELAY", 0.001)


class FakeCdpTab:
    """A tab whose ``send`` drives nodriver's real CDP generator protocol.

    nodriver's ``Tab.send(cmd)`` calls ``next(cmd)`` to get the request dict,
    then sends the response JSON back into the generator, whose return value
    is the parsed result. This fake does the same against an in-memory jar
    speaking CDP's JSON shapes, so the backend's CDP objects are built and
    parsed by nodriver itself.
    """

    def __init__(self) -> None:
        self.jar: list[dict[str, Any]] = []
        self.reject: set[str] = set()
        self.fail_with: Exception | None = None

    async def send(self, cmd: Any) -> Any:
        request = next(cmd)
        if self.fail_with is not None:
            cmd.close()
            raise self.fail_with
        handler = {
            "Storage.setCookies": self._set_cookies,
            "Storage.getCookies": self._get_cookies,
            "Network.clearBrowserCookies": self._clear,
        }[request["method"]]
        try:
            cmd.send(handler(request.get("params", {})))
        except StopIteration as stop:
            return stop.value
        raise AssertionError(f"{request['method']} did not finish after one response")

    def _set_cookies(self, params: dict[str, Any]) -> dict[str, Any]:
        for param in params["cookies"]:
            if param["name"] in self.reject:
                continue
            expires = param.get("expires")
            stored = {
                "name": param["name"],
                "value": param["value"],
                # The jar marks a domain cookie with a leading dot.
                "domain": "." + param["domain"].lstrip("."),
                "path": param.get("path", "/"),
                "size": len(param["name"]) + len(param["value"]),
                "httpOnly": param.get("httpOnly", False),
                "secure": param.get("secure", False),
                "session": expires is None,
                "expires": -1 if expires is None else expires,
                "priority": "Medium",
                "sourceScheme": "Secure",
                "sourcePort": 443,
            }
            if "sameSite" in param:
                stored["sameSite"] = param["sameSite"]
            self.jar = [
                c
                for c in self.jar
                if (c["name"], c["domain"], c["path"])
                != (stored["name"], stored["domain"], stored["path"])
            ]
            self.jar.append(stored)
        return {}

    def _get_cookies(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"cookies": [dict(c) for c in self.jar]}

    def _clear(self, params: dict[str, Any]) -> dict[str, Any]:
        self.jar = []
        return {}


class _LoopBound:
    """Remembers the event loop it was created on, like nodriver's objects.

    A real nodriver object used from another loop hangs or raises "attached to
    a different loop"; this records the misuse and raises, so a test sees it
    even where the backend's best-effort handlers swallow the exception.
    """

    def __init__(self, misuse: list[str]) -> None:
        self.loop = asyncio.get_running_loop()
        self._misuse = misuse

    async def _on_own_loop(self, what: str) -> None:
        if asyncio.get_running_loop() is not self.loop:
            self._misuse.append(what)
            raise AssertionError(f"{what} used from an event loop it was not created on")
        await asyncio.sleep(0)


class LoopBoundTab(_LoopBound, FakeCdpTab):
    """A nodriver Tab stand-in that only works on the loop that created it."""

    def __init__(self, url: str, misuse: list[str]) -> None:
        _LoopBound.__init__(self, misuse)
        FakeCdpTab.__init__(self)
        self.url = url

    async def evaluate(self, expression: str) -> str:
        await self._on_own_loop("Tab.evaluate")
        return {
            "window.location.href": self.url,
            "document.title": "Example Domain",
            "navigator.userAgent": "FakeChrome/1.0",
        }[expression]

    async def get_content(self) -> str:
        await self._on_own_loop("Tab.get_content")
        return "<html><body>Example Domain</body></html>"

    async def send(self, cmd: Any) -> Any:
        try:
            await self._on_own_loop("Tab.send")
        except AssertionError:
            cmd.close()
            raise
        return await FakeCdpTab.send(self, cmd)


class LoopBoundBrowser(_LoopBound):
    """A nodriver Browser stand-in that only works on the loop that created it."""

    def __init__(
        self, misuse: list[str], start_kwargs: dict[str, Any], chrome: "FakeChrome"
    ) -> None:
        super().__init__(misuse)
        self.start_kwargs = start_kwargs
        self._chrome = chrome
        self.tabs: list[LoopBoundTab] = []
        self.stopped_on: asyncio.AbstractEventLoop | None = None
        # No subprocess to reap, and a profile the stop path must not touch.
        self._process = None
        self.config = SimpleNamespace(uses_custom_data_dir=True, user_data_dir=None)

    async def get(self, url: str) -> LoopBoundTab:
        await self._on_own_loop("Browser.get")
        if self._chrome.hang_get:
            # A page that never loads: only a cancellation ends this.
            self._chrome.get_entered.set()
            await asyncio.Event().wait()
        tab = LoopBoundTab(url, self._misuse)
        self.tabs.append(tab)
        return tab

    def stop(self) -> None:
        # nodriver's Browser.stop() is synchronous; record where it ran.
        self.stopped_on = asyncio.get_running_loop()


class FakeChrome:
    """What ``fake_chrome`` patched in: every browser started, and every misuse."""

    def __init__(self) -> None:
        self.browsers: list[LoopBoundBrowser] = []
        self.misuse: list[str] = []
        self.fail_with: Exception | None = None
        # Set hang_get to make Browser.get wait until cancelled; get_entered
        # is set once it is waiting.
        self.hang_get = False
        self.get_entered = threading.Event()

    async def start(self, **kwargs: Any) -> LoopBoundBrowser:
        if self.fail_with is not None:
            raise self.fail_with
        await asyncio.sleep(0)
        browser = LoopBoundBrowser(self.misuse, kwargs, self)
        self.browsers.append(browser)
        return browser


@pytest.fixture()
def fake_chrome(monkeypatch: pytest.MonkeyPatch) -> FakeChrome:
    """Replace ``nodriver.start`` with loop-bound fakes, so a real start runs."""
    nodriver = pytest.importorskip("nodriver")
    chrome = FakeChrome()
    monkeypatch.setattr(nodriver, "start", chrome.start)
    return chrome


def with_private_loop(backend: Any) -> Any:
    """Give a hand-built "started" NoDriverBackend the loop ``start()`` would make.

    Only ``start()`` creates the private loop, so a test that sets
    ``_started`` and a browser by hand has to supply one for its sync calls to
    reach the coroutine at all.
    """
    backend._ensure_private_loop()
    return backend


@pytest.fixture()
def release_nodriver_loops(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stop the private event loop of every NoDriverBackend a test made.

    A test that leaves a backend started, or drives a sync call on a
    hand-built "running" backend, leaves the backend's loop thread alive. It
    is a daemon, so it would not hang the run, but one per test adds up.
    """
    from graftpunk.backends.nodriver import NoDriverBackend

    created: list[NoDriverBackend] = []
    original_init = NoDriverBackend.__init__

    def tracking_init(self: NoDriverBackend, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(NoDriverBackend, "__init__", tracking_init)
    yield
    for backend in created:
        backend._release_loop()
