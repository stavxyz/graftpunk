"""``gp --observe=full`` records network data on every backend (#283).

Before the fix a run recorded no network data at all: on nodriver
``BrowserSession`` never registered the CDP handlers, on Selenium Chrome was
never asked to keep the performance log the capture reads, and a plugin
command (which runs on a ``requests`` session, not a browser) left only an
empty ``screenshots/`` directory. The browser tests drive the real capture
code through the fake tab and fake driver the other observe tests use; the
HTTP tests run against a local ``http.server``.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests
import selenium.common.exceptions
import typer
from structlog.testing import capture_logs
from typer.testing import CliRunner

from graftpunk.cli.command_factory import synthesize_command_fn
from graftpunk.cli.plugin_runtime import run_plugin_command
from graftpunk.observe.http_capture import HttpCaptureBackend
from graftpunk.plugins.cli_plugin import SitePlugin, command
from tests.unit.test_observe_post_data import (
    BODY,
    RESPONSE,
    SEARCH,
    FakeTab,
    _always,
    _fetch_post,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run_dirs(base: Path) -> list[Path]:
    # The suite's conftest keeps a dot-directory of its own in tmp_path.
    return [p for p in base.glob("[!.]*/*") if p.is_dir()]


def _only_run(base: Path) -> Path:
    (run,) = _run_dirs(base)
    return run


def _har_entries(run: Path) -> list[dict[str, Any]]:
    return json.loads((run / "network.har").read_text())["log"]["entries"]


# ---------------------------------------------------------------------------
# nodriver: ``async with BrowserSession(...)``
# ---------------------------------------------------------------------------


def _nodriver_session(tab: Any, observe_mode: str = "full") -> Any:
    from graftpunk.session import BrowserSession

    with patch.object(BrowserSession, "__init__", return_value=None):
        session = BrowserSession.__new__(BrowserSession)
    session._backend_type = "nodriver"
    session._backend_instance = SimpleNamespace(
        _browser=SimpleNamespace(main_tab=tab),
        start_async=AsyncMock(),
        stop_async=AsyncMock(),
    )
    session._observe_mode = observe_mode
    session._capture = None
    session._observe_storage = None
    session._observe_redact = ()
    session._session_name = "myshop"
    return session


class TestNodriverSessionRecordsTheNetwork:
    @pytest.mark.asyncio
    async def test_a_fetch_post_lands_in_the_har_with_both_bodies(self, tmp_path: Path) -> None:
        tab = FakeTab({}, _always)
        session = _nodriver_session(tab)
        with patch("graftpunk.session.OBSERVE_BASE_DIR", tmp_path):
            async with session:
                for event in _fetch_post("1.1", inline=BODY):
                    await tab.dispatch(event)

        (entry,) = _har_entries(_only_run(tmp_path))
        assert entry["request"]["method"] == "POST"
        assert entry["request"]["url"] == SEARCH
        assert entry["request"]["postData"]["text"] == BODY
        assert entry["response"]["content"]["text"] == RESPONSE

    @pytest.mark.asyncio
    async def test_a_capture_that_cannot_start_leaves_the_session_working(
        self, tmp_path: Path
    ) -> None:
        tab = MagicMock()
        tab.send = AsyncMock(side_effect=ConnectionError("browser went away"))
        session = _nodriver_session(tab)
        ran = False
        with (
            patch("graftpunk.session.OBSERVE_BASE_DIR", tmp_path),
            patch("graftpunk.session.gp_console.warn") as warn,
            capture_logs() as logs,
        ):
            async with session:
                ran = True

        assert ran
        assert session.capture is None
        assert "could not start" in warn.call_args.args[0]
        assert [e["event"] for e in logs if e["log_level"] == "error"] == [
            "observe_start_capture_failed"
        ]
        assert not (_only_run(tmp_path) / "network.har").exists()

    def test_the_sync_start_says_it_records_no_network(self, tmp_path: Path) -> None:
        session = _nodriver_session(MagicMock())
        with (
            patch("graftpunk.session.OBSERVE_BASE_DIR", tmp_path),
            patch("graftpunk.session.gp_console.warn") as warn,
        ):
            session._start_observe()

        assert "no HAR entries" in warn.call_args.args[0]

    @pytest.mark.asyncio
    async def test_observe_off_starts_no_capture(self, tmp_path: Path) -> None:
        tab = MagicMock()
        tab.send = AsyncMock()
        session = _nodriver_session(tab, observe_mode="off")
        with patch("graftpunk.session.OBSERVE_BASE_DIR", tmp_path):
            async with session:
                pass

        tab.send.assert_not_called()
        assert session.capture is None
        assert _run_dirs(tmp_path) == []


# ---------------------------------------------------------------------------
# Selenium: Chrome keeps the performance log only when asked at launch
# ---------------------------------------------------------------------------


def _perf(method: str, params: dict[str, Any]) -> dict[str, str]:
    return {"message": json.dumps({"message": {"method": method, "params": params}})}


class _PerfLogDriver:
    """A WebDriver double that answers ``get_log("performance")`` the way
    ChromeDriver does: with Chrome's events when the session was created with
    ``goog:loggingPrefs``, and ``invalid argument: log type 'performance' not
    found`` otherwise (seen live on Chrome 155, 2026-10-23)."""

    def __init__(self, performance_log: bool) -> None:
        self.performance_log = performance_log
        self.quit = MagicMock()

    def get_log(self, kind: str) -> list[dict[str, str]]:
        if kind != "performance":
            return []
        if not self.performance_log:
            raise selenium.common.exceptions.InvalidArgumentException(
                "invalid argument: log type 'performance' not found"
            )
        return [
            _perf(
                "Network.requestWillBeSent",
                {
                    "requestId": "5.1",
                    "wallTime": 1_700_000_000.0,
                    "request": {
                        "url": SEARCH,
                        "method": "POST",
                        "headers": {"Content-Type": "application/json"},
                        "hasPostData": True,
                        "postData": BODY,
                    },
                },
            ),
            _perf(
                "Network.responseReceived",
                {
                    "requestId": "5.1",
                    "response": {
                        "status": 200,
                        "headers": {"Content-Type": "application/json"},
                        "mimeType": "application/json",
                    },
                },
            ),
        ]

    def execute_cdp_cmd(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "Network.getResponseBody":
            return {"body": RESPONSE, "base64Encoded": False}
        raise AssertionError(f"unexpected CDP command {method}")


def _stealth_session(observe_mode: str) -> Any:
    from graftpunk.session import BrowserSession

    def create(*, headless: bool, performance_log: bool = False) -> _PerfLogDriver:
        return _PerfLogDriver(performance_log)

    with (
        patch("graftpunk.stealth.create_stealth_driver", side_effect=create),
        patch("requestium.Session.__init__"),
    ):
        session = BrowserSession(backend="selenium", headless=True, observe_mode=observe_mode)
    session.session_name = "myshop"
    return session


class TestSeleniumSessionRecordsTheNetwork:
    def test_a_fetch_post_lands_in_the_har_with_both_bodies(self, tmp_path: Path) -> None:
        with patch("graftpunk.session.OBSERVE_BASE_DIR", tmp_path), _stealth_session("full"):
            pass

        (entry,) = _har_entries(_only_run(tmp_path))
        assert entry["request"]["url"] == SEARCH
        assert entry["request"]["postData"]["text"] == BODY
        assert entry["response"]["content"]["text"] == RESPONSE

    def test_observe_off_does_not_ask_for_the_performance_log(self) -> None:
        session = _stealth_session("off")
        assert session.driver.performance_log is False

    @pytest.mark.parametrize("performance_log", [True, False])
    def test_the_stealth_driver_asks_chrome_for_the_performance_log(
        self, performance_log: bool
    ) -> None:
        from graftpunk import stealth

        with (
            patch.object(stealth.uc, "Chrome") as chrome,
            patch.object(stealth, "stealth"),
            patch.object(stealth, "get_chrome_version", return_value="155"),
            patch.object(Path, "mkdir"),
        ):
            stealth.create_stealth_driver(
                headless=True,
                profile_dir=Path("/nonexistent-unused"),
                performance_log=performance_log,
            )

        capabilities = chrome.call_args.kwargs["options"].to_capabilities()
        assert ("goog:loggingPrefs" in capabilities) is performance_log
        if performance_log:
            assert capabilities["goog:loggingPrefs"] == {"performance": "ALL"}


# ---------------------------------------------------------------------------
# HTTP: a plugin command's requests session, and ``gp http``
# ---------------------------------------------------------------------------


class _Site(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str, **headers: Any) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            for one in value if isinstance(value, list) else [value]:
                self.send_header(name.replace("_", "-"), one)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/home":
            self._send(200, b"<h1>home</h1>", "text/html; charset=utf-8")
        elif self.path == "/logo.png":
            self._send(200, b"\x89PNG\r\n\x1a\n\x00\xff", "image/png")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/api/search":
            body = json.dumps({"echo": json.loads(raw)}).encode()
            self._send(200, body, "application/json")
        elif self.path == "/login":
            self._send(
                302,
                b"",
                "text/html",
                Location="/home",
                Set_Cookie=["sid=abc123; Path=/; HttpOnly", "theme=dark; Path=/"],
            )
        else:
            self._send(404, b"not found", "text/plain")


@pytest.fixture
def site() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class TestHttpCapture:
    def test_a_post_is_recorded_with_both_bodies(self, site: str, tmp_path: Path) -> None:
        session = requests.Session()
        capture = HttpCaptureBackend(session, bodies_dir=tmp_path / "bodies")
        capture.start_capture()
        session.post(f"{site}/api/search", json={"query": "widget"}, timeout=5)
        capture.stop_capture()

        (entry,) = capture.get_har_entries()
        assert entry["request"]["method"] == "POST"
        assert json.loads(entry["request"]["postData"]["text"]) == {"query": "widget"}
        assert entry["request"]["postData"]["mimeType"] == "application/json"
        assert json.loads(entry["response"]["content"]["text"]) == {"echo": {"query": "widget"}}
        assert entry["response"]["status"] == 200
        assert not (tmp_path / "bodies").exists()

    def test_each_redirect_hop_is_its_own_entry(self, site: str) -> None:
        session = requests.Session()
        capture = HttpCaptureBackend(session)
        capture.start_capture()
        final = session.post(f"{site}/login", data={"user": "alice"}, timeout=5)
        capture.stop_capture()

        assert final.status_code == 200
        hop, landing = capture.get_har_entries()
        assert (hop["request"]["method"], hop["response"]["status"]) == ("POST", 302)
        assert hop["request"]["postData"]["text"] == "user=alice"
        assert hop["response"]["redirectURL"] == f"{site}/home"
        assert [c["name"] for c in hop["response"]["cookies"]] == ["sid", "theme"]
        assert hop["response"]["cookies"][0]["httpOnly"] is True
        assert (landing["request"]["method"], landing["request"]["url"]) == ("GET", f"{site}/home")
        assert landing["response"]["content"]["text"] == "<h1>home</h1>"
        # The session sent the cookies the hop set, and the HAR shows them.
        assert {c["name"] for c in landing["request"]["cookies"]} == {"sid", "theme"}

    def test_binary_and_oversized_bodies_go_to_bodies(self, site: str, tmp_path: Path) -> None:
        session = requests.Session()
        capture = HttpCaptureBackend(session, max_body_size=10, bodies_dir=tmp_path / "bodies")
        capture.start_capture()
        session.get(f"{site}/logo.png", timeout=5)
        session.post(f"{site}/api/search", json={"query": "x" * 50}, timeout=5)
        capture.stop_capture()

        image, search = capture.get_har_entries()
        assert image["response"]["content"]["_bodyFile"] == "bodies/http-1.png"
        assert (tmp_path / "bodies" / "http-1.png").read_bytes() == b"\x89PNG\r\n\x1a\n\x00\xff"
        request_file = search["request"]["_postDataFile"]
        assert json.loads((tmp_path / request_file).read_text()) == {"query": "x" * 50}
        assert search["response"]["content"]["_bodyFile"] == "bodies/http-2.json"

    def test_after_stop_nothing_more_is_recorded(self, site: str) -> None:
        session = requests.Session()
        capture = HttpCaptureBackend(session)
        capture.start_capture()
        session.get(f"{site}/home", timeout=5)
        capture.stop_capture()
        session.get(f"{site}/home", timeout=5)

        assert len(capture.get_har_entries()) == 1
        assert session.hooks["response"] == []

    def test_a_streamed_response_is_left_for_the_caller(self, site: str) -> None:
        session = requests.Session()
        capture = HttpCaptureBackend(session)
        capture.start_capture()
        response = session.get(f"{site}/home", stream=True, timeout=5)

        assert response.raw.read() == b"<h1>home</h1>"
        (entry,) = capture.get_har_entries()
        assert entry["response"]["content"]["_bodyNotCaptured"] is True
        assert entry["response"]["content"]["text"] is None

    def test_a_response_it_cannot_record_still_reaches_the_caller(self, site: str) -> None:
        session = requests.Session()
        capture = HttpCaptureBackend(session)
        capture.start_capture()
        with (
            patch(
                "graftpunk.observe.http_capture.har_entry_for_response",
                side_effect=ValueError("boom"),
            ),
            capture_logs() as logs,
        ):
            response = session.get(f"{site}/home", timeout=5)

        assert response.text == "<h1>home</h1>"
        assert capture.get_har_entries() == []
        assert [e["event"] for e in logs] == ["http_capture_record_failed"]


class _ShopPlugin(SitePlugin):
    site_name = "myshop"
    session_name = "myshop"
    help_text = "Shop plugin"
    requires_session = False
    base_url = ""

    @command(help="Search the shop")
    def search(self, ctx: Any, query: str = "") -> dict[str, Any]:
        response = ctx.session.post(f"{self.base_url}/api/search", json={"query": query})
        return response.json()


def _invoke_search(base_url: str, observe_mode: str) -> Any:
    plugin = _ShopPlugin()
    plugin.base_url = base_url
    spec = next(s for s in plugin.get_commands() if s.name == "search")

    def body(ctx: typer.Context, **kwargs: Any) -> None:
        run_plugin_command(plugin, spec, ctx, **kwargs)

    app = typer.Typer()
    app.command(name="search")(
        synthesize_command_fn(name="search", param_specs=list(spec.params), body=body)
    )
    with patch("graftpunk.cli.plugin_runtime.format_output"):
        return CliRunner().invoke(app, ["--query", "widget"], obj={"observe_mode": observe_mode})


class TestPluginCommandRun:
    def test_observe_full_writes_the_commands_traffic(self, site: str, tmp_path: Path) -> None:
        with patch("graftpunk.observe.context.OBSERVE_BASE_DIR", tmp_path):
            result = _invoke_search(site, "full")

        assert result.exit_code == 0, result.output
        run = _only_run(tmp_path)
        assert run.parent.name == "myshop"
        (entry,) = _har_entries(run)
        assert entry["request"]["url"] == f"{site}/api/search"
        assert json.loads(entry["request"]["postData"]["text"]) == {"query": "widget"}
        assert json.loads(entry["response"]["content"]["text"]) == {"echo": {"query": "widget"}}
        # Only what was written: no empty screenshots/ or bodies/ beside the HAR.
        assert sorted(p.name for p in run.iterdir()) == ["network.har"]

    def test_observe_off_writes_nothing(self, site: str, tmp_path: Path) -> None:
        with patch("graftpunk.observe.context.OBSERVE_BASE_DIR", tmp_path):
            result = _invoke_search(site, "off")

        assert result.exit_code == 0, result.output
        assert _run_dirs(tmp_path) == []

    def test_the_run_is_written_when_the_command_fails(self, tmp_path: Path) -> None:
        # Nothing listens on port 9 on the loopback: the request fails.
        with patch("graftpunk.observe.context.OBSERVE_BASE_DIR", tmp_path):
            result = _invoke_search("http://127.0.0.1:9", "full")

        assert result.exit_code == 1
        assert _har_entries(_only_run(tmp_path)) == []

    def test_a_capture_that_cannot_start_does_not_stop_the_command(
        self, site: str, tmp_path: Path
    ) -> None:
        with (
            patch("graftpunk.observe.context.OBSERVE_BASE_DIR", tmp_path),
            patch.object(HttpCaptureBackend, "start_capture", side_effect=RuntimeError("no")),
        ):
            result = _invoke_search(site, "full")

        assert result.exit_code == 0, result.output
        assert "could not start" in result.output
        assert list(_only_run(tmp_path).iterdir()) == []
