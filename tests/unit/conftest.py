"""Shared test helpers for unit tests."""

import asyncio
import functools
import importlib
import os
import shlex
import shutil
import subprocess
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

# A module that imports a factory by name keeps whatever is bound when it is
# first imported, so graftpunk.session must be imported before any test patches
# graftpunk.observe.capture.create_capture_backend (#352).
import graftpunk.session  # noqa: F401

# The local site the HTTP observe tests record (fixture: site).
from tests.unit.observe_http_site import site  # noqa: F401

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


def signals_the_suite(target: int, *, group: bool, sig: int) -> str | None:
    """Why signalling *target* would reach the process running the tests, or None.

    *target* is what ``os.kill`` (``group=False``) or ``os.killpg`` (``group=True``)
    was given. Signal 0 delivers nothing and only asks whether a process exists, so
    it is always allowed. Otherwise refused: this process, its parent (the xdist
    controller, or the shell that started pytest), and this process's group, which
    holds the controller and every other worker, whether named as a group, as
    ``0`` (the caller's own group), or as ``-1`` (every process the caller may
    signal).
    """
    if sig == 0:
        return None
    own_group = os.getpgid(0) if hasattr(os, "getpgid") else None
    if group:
        if target in (0, own_group):
            return f"process group {target}, which holds the test run"
        return None
    if target in (os.getpid(), os.getppid()):
        return f"pid {target}, the test run itself"
    if target in (0, -1) or (own_group is not None and target == -own_group):
        return f"pid {target}, which reaches the test run's process group"
    return None


@pytest.fixture(autouse=True)
def _never_signal_the_suite(request, monkeypatch):  # noqa: ANN001, ANN201
    """No unit test may send a signal to the test run, its parent or its process
    group through ``os.kill``, ``os.killpg``, ``signal.raise_signal`` or
    ``signal.pthread_kill``; such a send ends with a test failure naming it
    instead of being sent.

    A signal that reaches the xdist controller or a sibling worker ends or stalls
    the whole run with no report of which test sent it. What an in-process patch
    cannot see stays out of reach: a ``kill`` command run in a subprocess, and a
    child process that signals its parent. A test that raises a signal on itself
    on purpose (a Ctrl+C under a handler it installed) is marked
    ``signals_itself``, which allows ``raise_signal`` and ``pthread_kill`` for it
    and nothing else.
    """
    real_kill = os.kill
    real_killpg = getattr(os, "killpg", None)

    if request.node.get_closest_marker("signals_itself") is None:

        def raise_signal(sig: int) -> None:
            pytest.fail(
                f"a unit test tried to raise signal {sig} on the test run; mark it "
                "signals_itself if that is the point of the test",
                pytrace=True,
            )

        def pthread_kill(thread_id: int, sig: int) -> None:
            if sig != 0:
                pytest.fail(
                    f"a unit test tried to send signal {sig} to a thread of the test run; "
                    "mark it signals_itself if that is the point of the test",
                    pytrace=True,
                )
            real_pthread_kill(thread_id, sig)

        import signal as signal_module

        real_pthread_kill = getattr(signal_module, "pthread_kill", None)
        monkeypatch.setattr(signal_module, "raise_signal", raise_signal)
        if real_pthread_kill is not None:
            monkeypatch.setattr(signal_module, "pthread_kill", pthread_kill)

    def kill(pid: int, sig: int) -> None:
        reason = signals_the_suite(pid, group=False, sig=sig)
        if reason is not None:
            pytest.fail(f"a unit test tried to send signal {sig} to {reason}", pytrace=True)
        real_kill(pid, sig)

    monkeypatch.setattr(os, "kill", kill)
    if real_killpg is not None:

        def killpg(pgid: int, sig: int) -> None:
            reason = signals_the_suite(pgid, group=True, sig=sig)
            if reason is not None:
                pytest.fail(f"a unit test tried to send signal {sig} to {reason}", pytrace=True)
            real_killpg(pgid, sig)

        monkeypatch.setattr(os, "killpg", killpg)


@pytest.fixture(autouse=True)
def _disarm_chrome_orphan_cleanup(monkeypatch):  # noqa: ANN001, ANN201
    """No unit test may signal a process or delete a directory it does not own.

    The reaper reads the real process table and sends real signals, and the
    browser launch sites call it for real in tests that drive a start. Disarm
    it at the origin, once, for the whole unit suite: reap_orphans and
    remove_stale_temp_profiles then do nothing, whoever calls them, so a new
    caller is covered the day it is written. The reaper's own tests re-arm it
    and inject a fake process table.

    Re-arming is per test, but the process table is the machine's. A test that
    arms and then lets a sweep read the real table ends every marked process
    whose owner is gone, including the stand-in orphan another xdist worker's
    real-process test is about to reap: that is how
    ``test_a_real_orphan_is_ended_and_a_live_owners_browser_is_not`` found no
    orphan on CI (#258). So an armed sweep without an injected ``ops`` fails
    the test that started it instead of running.
    """
    from graftpunk import chrome_orphans

    monkeypatch.setattr(chrome_orphans, "_ARMED", False)

    def guarded(sweep: Any) -> Any:
        @functools.wraps(sweep)
        def guarded_sweep(*args: Any, ops: Any = None, **kwargs: Any) -> Any:
            if ops is None and chrome_orphans._ARMED:
                pytest.fail(
                    f"an armed unit test ran {sweep.__name__} on the real process table; "
                    "stub the sweep or pass ops= with a table of its own (#258)",
                    pytrace=True,
                )
            return sweep(*args, ops=ops, **kwargs)

        return guarded_sweep

    monkeypatch.setattr(chrome_orphans, "reap_orphans", guarded(chrome_orphans.reap_orphans))
    monkeypatch.setattr(
        chrome_orphans,
        "remove_stale_temp_profiles",
        guarded(chrome_orphans.remove_stale_temp_profiles),
    )


_BROWSER_EXECUTABLE_MARKERS = ("chrome", "chromium")


def _first_word(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    return parts[0] if parts else ""


def _browser_executable(args: Any, kwargs: dict[str, Any]) -> str | None:
    """The Chrome, Chromium or chromedriver executable a Popen call would run, if any."""
    executable = kwargs.get("executable")
    if executable is None:
        if isinstance(args, (str, bytes, os.PathLike)):
            command = os.fsdecode(args)
            executable = _first_word(command) if kwargs.get("shell") else command
        else:
            argv = list(args)
            executable = argv[0] if argv else ""
    return _browser_name(executable)


def _browser_name(executable: Any) -> str | None:
    name = os.path.basename(os.fsdecode(executable)).lower()
    if any(marker in name for marker in _BROWSER_EXECUTABLE_MARKERS):
        return os.fsdecode(executable)
    return None


def _resolve_executable(executable: str, cwd: Any, path: str | None) -> str:
    """The file the call would run: a relative path is relative to the call's own
    ``cwd``, and a bare name is looked up on the call's PATH."""
    if os.sep in executable or (os.altsep and os.altsep in executable):
        if not os.path.isabs(executable) and cwd is not None:
            return os.path.join(os.fsdecode(cwd), executable)
        return executable
    return shutil.which(executable, path=path) or executable


@pytest.fixture(autouse=True)
def _no_real_browser(request, tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    """Fail any unit test that starts a real Chrome or chromedriver (#351).

    A launch with the default profile can touch the developer's real Chrome
    data and, on macOS, the Keychain. graftpunk's launch paths end at one of
    the two points patched here: ``subprocess.Popen`` (and ``os.posix_spawn``,
    ``os.posix_spawnp`` and ``os.system``) for a Chrome, Chromium
    or chromedriver executable (undetected-chromedriver's Chrome, selenium's
    driver service, webdriver-manager's version probe, and nodriver through
    ``asyncio.create_subprocess_exec``), and undetected-chromedriver's
    ``start_detached`` (its ``use_subprocess=False`` launch, from a child
    process this patch of ``Popen`` does not reach). The library functions
    themselves are left alone because seam tests read their source. An
    executable under the test's own ``tmp_path`` is a fake the test wrote
    and may run, found by the call's own ``cwd`` and PATH. An attempt raises
    ``pytest.fail``'s exception, which ``except Exception`` does not catch,
    and is also recorded so the test fails at teardown when the attempt was
    made on a thread or swallowed some other way. A test that needs a real browser says so with
    ``@pytest.mark.real_browser``; no unit test does today.

    Yields the list of recorded attempts, for the guard's own tests.
    """
    attempts: list[str] = []
    if request.node.get_closest_marker("real_browser") is not None:
        yield attempts
        return

    def refuse(what: str) -> NoReturn:
        attempts.append(what)
        pytest.fail(f"a unit test tried to start a real browser: {what}", pytrace=True)

    own_fakes = os.path.realpath(tmp_path)

    def check(executable: str | None, what: str, cwd: Any = None, env: Any = None) -> None:
        if executable is None:
            return
        path = (env if env is not None else os.environ).get("PATH")
        resolved = _resolve_executable(executable, cwd, path)
        if not os.path.realpath(resolved).startswith(own_fakes + os.sep):
            refuse(f"{what}({executable!r})")

    class _GuardedPopen(subprocess.Popen):
        def __init__(self, args: Any, *pargs: Any, **kwargs: Any) -> None:
            check(
                _browser_executable(args, kwargs),
                "subprocess.Popen",
                kwargs.get("cwd"),
                kwargs.get("env"),
            )
            super().__init__(args, *pargs, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _GuardedPopen)

    def guard_spawn(name: str) -> None:
        original = getattr(os, name, None)
        if original is None:
            return

        def guarded(path: Any, *pargs: Any, **kwargs: Any) -> Any:
            check(_browser_name(path), f"os.{name}")
            return original(path, *pargs, **kwargs)

        monkeypatch.setattr(os, name, guarded)

    guard_spawn("posix_spawn")
    guard_spawn("posix_spawnp")

    original_system = os.system

    def guarded_system(command: str) -> int:
        check(_browser_name(_first_word(os.fsdecode(command))), "os.system")
        return original_system(command)

    monkeypatch.setattr(os, "system", guarded_system)

    def refuse_detached(*args: Any, **kwargs: Any) -> NoReturn:
        refuse("undetected_chromedriver.start_detached")

    try:
        uc = importlib.import_module("undetected_chromedriver")
    except ImportError:
        pass
    else:
        monkeypatch.setattr(uc, "start_detached", refuse_detached)

    yield attempts
    if attempts:
        pytest.fail(f"a unit test tried to start a real browser: {attempts}", pytrace=False)


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

    The readiness wait before each step (login_ready) polls in real seconds up
    to its own cap, and needs a quiet window that the scripted pages only
    report after a real interval.

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
    monkeypatch.setattr("graftpunk.plugins.login_ready._READY_MAX_WAIT", 0.05)
    monkeypatch.setattr("graftpunk.plugins.login_ready._READY_QUIET_WINDOW", 0.001)
    monkeypatch.setattr("graftpunk.plugins.login_ready._READY_POLL_INTERVAL", 0.001)


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
        self.stop_loading_calls = 0

    async def send(self, cmd: Any) -> Any:
        request = next(cmd)
        if self.fail_with is not None:
            cmd.close()
            raise self.fail_with
        handler = {
            "Storage.setCookies": self._set_cookies,
            "Storage.getCookies": self._get_cookies,
            "Network.clearBrowserCookies": self._clear,
            "Page.stopLoading": self._stop_loading,
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

    def _stop_loading(self, params: dict[str, Any]) -> dict[str, Any]:
        self.stop_loading_calls += 1
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
        # Set hang to make every CDP call wait until cancelled, like a Chrome
        # that stopped answering without closing its websocket. hung_calls
        # counts the calls that started waiting, cancelled_calls the ones a
        # cancellation ended.
        self.hang = False
        self.hung_calls = 0
        self.cancelled_calls = 0
        self.target = SimpleNamespace(type_="page")
        # Cleared while Chrome is still loading a URL in this tab (a
        # Browser.get that timed out). Like Chrome, the tab then answers no
        # evaluate until Page.stopLoading ends the load.
        self.idle = asyncio.Event()
        self.idle.set()

    def _stop_loading(self, params: dict[str, Any]) -> dict[str, Any]:
        self.idle.set()
        return super()._stop_loading(params)

    async def _maybe_hang(self) -> None:
        if self.hang:
            self.hung_calls += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled_calls += 1
                raise

    async def evaluate(self, expression: str) -> str:
        await self._on_own_loop("Tab.evaluate")
        await self.idle.wait()
        await self._maybe_hang()
        return {
            "window.location.href": self.url,
            "document.title": "Example Domain",
            "navigator.userAgent": "FakeChrome/1.0",
        }[expression]

    async def get_content(self) -> str:
        await self._on_own_loop("Tab.get_content")
        await self.idle.wait()
        await self._maybe_hang()
        return "<html><body>Example Domain</body></html>"

    async def send(self, cmd: Any) -> Any:
        try:
            await self._on_own_loop("Tab.send")
        except AssertionError:
            cmd.close()
            raise
        try:
            await self._maybe_hang()
        except asyncio.CancelledError:
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
        # Like Chrome, one tab to start with, which Browser.get navigates.
        self.tabs: list[LoopBoundTab] = [LoopBoundTab("about:blank", misuse)]
        self.stopped_on: asyncio.AbstractEventLoop | None = None
        # No subprocess to reap, and a profile the stop path must not touch.
        self._process = None
        self.config = SimpleNamespace(uses_custom_data_dir=True, user_data_dir=None)

    async def get(self, url: str) -> LoopBoundTab:
        await self._on_own_loop("Browser.get")
        tab = self.tabs[0]
        if self._chrome.hang_get:
            # A page that never loads: only a cancellation ends this, and
            # the tab stays busy loading it until Page.stopLoading.
            tab.idle.clear()
            self._chrome.get_entered.set()
            await asyncio.Event().wait()
        tab.url = url
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
    with backend._lock:
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
