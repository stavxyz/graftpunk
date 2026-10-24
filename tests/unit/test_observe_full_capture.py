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
from tests.unit.observe_http_site import LATIN1_TEXT
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
        # The run directory it opened is removed, not left empty.
        assert _run_dirs(tmp_path) == []

    @pytest.mark.asyncio
    async def test_a_body_whose_fetch_fails_at_stop_is_marked(self) -> None:
        from tests.unit.test_observe_post_data import _never, _record

        tab = FakeTab({}, _always, response_available=_never)
        backend = await _record(tab, _fetch_post("1.1", inline=BODY))

        (entry,) = backend.get_har_entries()
        content = entry["response"]["content"]
        assert content["_bodyNotCaptured"] is True
        assert content["_bodyNotCapturedReason"] == "fetch_failed: ProtocolException"

    @pytest.mark.asyncio
    async def test_bodies_left_when_the_browser_goes_away_are_marked(self) -> None:
        from tests.unit.test_observe_post_data import _never, _record

        class GoneAtStop(FakeTab):
            async def send(self, cmd: Any, **kwargs: Any) -> Any:
                if self.stopping:
                    raise ConnectionError("browser went away")
                return await super().send(cmd, **kwargs)

        tab = GoneAtStop({}, _always, response_available=_never)
        events = [*_fetch_post("1.1", inline=BODY), *_fetch_post("1.2", inline=BODY)]
        backend = await _record(tab, events)

        reasons = [
            e["response"]["content"].get("_bodyNotCapturedReason")
            for e in backend.get_har_entries()
        ]
        assert reasons == ["browser_disconnected", "browser_disconnected"]

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

    @pytest.mark.parametrize(
        ("mime", "kept"),
        [
            ("application/csv", "text"),
            ("application/x-www-form-urlencoded", "text"),
            ("application/x-protobuf", "file"),
            ("", "file"),
        ],
    )
    def test_a_body_of_any_type_is_fetched_and_kept(
        self, tmp_path: Path, mime: str, kept: str
    ) -> None:
        from graftpunk.observe.capture import SeleniumCaptureBackend

        log = [
            _perf(
                "Network.requestWillBeSent",
                {"requestId": "6.1", "request": {"url": SEARCH, "method": "GET", "headers": {}}},
            ),
            _perf(
                "Network.responseReceived",
                {"requestId": "6.1", "response": {"status": 200, "headers": {}, "mimeType": mime}},
            ),
        ]
        driver = MagicMock()
        driver.get_log = lambda kind: log if kind == "performance" else []
        driver.execute_cdp_cmd = MagicMock(
            return_value={"body": "a,b\n1,2", "base64Encoded": False}
        )
        capture = SeleniumCaptureBackend(driver, bodies_dir=tmp_path / "bodies")
        capture.stop_capture()

        (entry,) = capture.get_har_entries()
        content = entry["response"]["content"]
        if kept == "text":
            assert content["text"] == "a,b\n1,2"
        else:
            assert (tmp_path / content["_bodyFile"]).read_bytes() == b"a,b\n1,2"

    @staticmethod
    def _one_response(method: str, status: int, execute: Any) -> dict[str, Any]:
        from graftpunk.observe.capture import SeleniumCaptureBackend

        log = [
            _perf(
                "Network.requestWillBeSent",
                {"requestId": "6.2", "request": {"url": SEARCH, "method": method, "headers": {}}},
            ),
            _perf(
                "Network.responseReceived",
                {
                    "requestId": "6.2",
                    "response": {"status": status, "headers": {}, "mimeType": "text/html"},
                },
            ),
        ]
        driver = MagicMock()
        driver.get_log = lambda kind: log if kind == "performance" else []
        driver.execute_cdp_cmd = execute
        capture = SeleniumCaptureBackend(driver)
        capture.stop_capture()
        (entry,) = capture.get_har_entries()
        return entry["response"]["content"]

    @pytest.mark.parametrize(("method", "status"), [("GET", 204), ("HEAD", 200)])
    def test_a_response_with_no_body_is_not_fetched(self, method: str, status: int) -> None:
        def execute(cdp_method: str, _params: dict[str, Any]) -> dict[str, Any]:
            return {"body": "<p>fetched</p>", "base64Encoded": False}

        content = self._one_response(method, status, execute)

        assert content["text"] is None
        assert content["_bodyFile"] is None
        assert "_bodyNotCaptured" not in content

    def test_a_body_whose_fetch_fails_is_marked(self) -> None:
        def execute(cdp_method: str, _params: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("No resource with given id was found")

        content = self._one_response("GET", 200, execute)

        assert content["_bodyNotCaptured"] is True
        assert content["_bodyNotCapturedReason"] == "fetch_failed: RuntimeError"


# ---------------------------------------------------------------------------
# HTTP: a plugin command's requests session, and ``gp http``
# ---------------------------------------------------------------------------


def _recording(**kwargs: Any) -> tuple[requests.Session, HttpCaptureBackend]:
    session = requests.Session()
    capture = HttpCaptureBackend(session, **kwargs)
    capture.start_capture()
    return session, capture


class TestHttpCapture:
    def test_a_post_is_recorded_with_both_bodies(self, site: str, tmp_path: Path) -> None:
        session, capture = _recording(bodies_dir=tmp_path / "bodies")
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
        session, capture = _recording()
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
        # Each hop is timed from when it was sent.
        assert hop["startedDateTime"] <= landing["startedDateTime"]

    def test_requests_that_skip_session_hooks_are_recorded(self, site: str) -> None:
        session, capture = _recording()
        session.get(f"{site}/home", hooks={"response": [lambda r, *a, **k: r]}, timeout=5)
        session.send(requests.Request("GET", f"{site}/csv").prepare(), timeout=5)
        capture.stop_capture()

        urls = [e["request"]["url"] for e in capture.get_har_entries()]
        assert urls == [f"{site}/home", f"{site}/csv"]

    def test_binary_and_oversized_bodies_go_to_bodies(self, site: str, tmp_path: Path) -> None:
        session, capture = _recording(max_body_size=10, bodies_dir=tmp_path / "bodies")
        session.get(f"{site}/logo.png", timeout=5)
        session.post(f"{site}/api/search", json={"query": "x" * 50}, timeout=5)
        capture.stop_capture()

        image, search = capture.get_har_entries()
        assert image["response"]["content"]["_bodyFile"] == "bodies/http-1.png"
        assert (tmp_path / "bodies" / "http-1.png").read_bytes() == b"\x89PNG\r\n\x1a\n\x00\xff"
        request_file = search["request"]["_postDataFile"]
        assert json.loads((tmp_path / request_file).read_text()) == {"query": "x" * 50}
        assert search["response"]["content"]["_bodyFile"] == "bodies/http-2.json"

    @pytest.mark.parametrize(
        ("path", "text"),
        [
            ("/csv", "a,b\n1,2\n"),
            ("/form", "token=abc&x=1"),
            ("/latin1", LATIN1_TEXT),
        ],
    )
    def test_text_bodies_are_kept_as_their_own_text(self, site: str, path: str, text: str) -> None:
        session, capture = _recording()
        session.get(f"{site}{path}", timeout=5)
        capture.stop_capture()

        (entry,) = capture.get_har_entries()
        assert entry["response"]["content"]["text"] == text

    def test_undeclared_text_that_is_not_utf8_is_kept_as_its_bytes(
        self, site: str, tmp_path: Path
    ) -> None:
        session, capture = _recording(bodies_dir=tmp_path / "bodies")
        session.get(f"{site}/latin1-bare", timeout=5)
        capture.stop_capture()

        (entry,) = capture.get_har_entries()
        content = entry["response"]["content"]
        # No charset is guessed: the HAR holds no text, and the bytes are kept.
        assert content["text"] is None
        assert content["_bodyFileReason"] == "not_decodable"
        assert (tmp_path / content["_bodyFile"]).read_bytes() == LATIN1_TEXT.encode("latin-1")

    def test_a_body_with_no_type_is_kept_as_raw_bytes(self, site: str, tmp_path: Path) -> None:
        session, capture = _recording(bodies_dir=tmp_path / "bodies")
        session.get(f"{site}/no-ct", timeout=5)
        capture.stop_capture()

        (entry,) = capture.get_har_entries()
        assert entry["response"]["content"]["_bodyFile"] == "bodies/http-1.bin"
        assert (tmp_path / "bodies" / "http-1.bin").read_bytes() == b'{"a": 1}'

    def test_a_body_left_out_says_why(self, site: str) -> None:
        session, capture = _recording()  # no bodies/ to put it in
        session.get(f"{site}/no-ct", timeout=5)
        capture.stop_capture()

        content = capture.get_har_entries()[0]["response"]["content"]
        assert content["_bodyNotCaptured"] is True
        assert content["_bodyNotCapturedReason"] == "not_text"

    def test_after_stop_nothing_more_is_recorded(self, site: str) -> None:
        session, capture = _recording()
        session.get(f"{site}/home", timeout=5)
        capture.stop_capture()
        session.get(f"{site}/home", timeout=5)

        assert len(capture.get_har_entries()) == 1
        assert all("send" not in vars(a) for a in session.adapters.values())

    def test_a_streamed_response_is_left_for_the_caller(self, site: str) -> None:
        session, capture = _recording()
        response = session.get(f"{site}/home", stream=True, timeout=5)

        assert response.raw.read() == b"<h1>home</h1>"
        (entry,) = capture.get_har_entries()
        assert entry["response"]["content"]["_bodyNotCaptured"] is True
        assert entry["response"]["content"]["_bodyNotCapturedReason"] == "streamed"
        assert entry["response"]["content"]["text"] is None

    def test_a_streamed_redirect_hop_is_not_marked(self, site: str) -> None:
        session, capture = _recording()
        session.post(f"{site}/login", data={"user": "alice"}, stream=True, timeout=5)
        capture.stop_capture()

        hop, landing = capture.get_har_entries()
        assert "_bodyNotCaptured" not in hop["response"]["content"]
        assert landing["response"]["content"]["_bodyNotCaptured"] is True

    def test_a_streamed_request_body_is_marked_not_captured(self, site: str) -> None:
        import io

        session, capture = _recording()
        session.post(
            f"{site}/api/search",
            data=io.BytesIO(b'{"query": "widget"}'),
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        capture.stop_capture()

        (entry,) = capture.get_har_entries()
        assert entry["request"]["_postDataNotCaptured"] is True
        assert "postData" not in entry["request"]

    def test_a_body_that_fails_to_read_still_gets_an_entry(self, site: str) -> None:
        session, capture = _recording()
        with pytest.raises(requests.exceptions.ChunkedEncodingError):
            session.get(f"{site}/truncated", timeout=5)
        capture.stop_capture()

        (entry,) = capture.get_har_entries()
        assert entry["response"]["status"] == 200
        content = entry["response"]["content"]
        assert content["_bodyNotCaptured"] is True
        assert content["_bodyNotCapturedReason"].startswith("read_failed")

    def test_a_response_it_cannot_record_still_reaches_the_caller(self, site: str) -> None:
        session, capture = _recording()
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

    def test_entries_are_spooled_to_disk_as_they_arrive(self, site: str, tmp_path: Path) -> None:
        spool = tmp_path / "network.har.spool"
        session, capture = _recording(spool_path=spool)
        session.get(f"{site}/home", timeout=5)

        assert capture._entries == []
        (line,) = spool.read_text().splitlines()
        assert json.loads(line)["response"]["content"]["text"] == "<h1>home</h1>"
        session.get(f"{site}/csv", timeout=5)
        assert len(capture.get_har_entries()) == 2

    def test_a_session_pickled_mid_capture_carries_no_recorder(self, site: str) -> None:
        import dill

        session, capture = _recording()
        session.get(f"{site}/home", timeout=5)
        restored = dill.loads(dill.dumps(session))  # noqa: S301 - our own bytes

        assert b"<h1>home</h1>" not in dill.dumps(session)
        assert all("send" not in vars(a) for a in restored.adapters.values())
        restored.get(f"{site}/home", timeout=5)
        assert len(capture.get_har_entries()) == 1


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

    @command(help="Fetch a page that fails")
    def broken(self, ctx: Any) -> dict[str, Any]:
        response = ctx.session.get(f"{self.base_url}/error")
        response.raise_for_status()
        return response.json()


def _invoke(base_url: str, observe_mode: str, name: str = "search", *args: str) -> Any:
    plugin = _ShopPlugin()
    plugin.base_url = base_url
    spec = next(s for s in plugin.get_commands() if s.name == name)

    def body(ctx: typer.Context, **kwargs: Any) -> None:
        run_plugin_command(plugin, spec, ctx, **kwargs)

    app = typer.Typer()
    app.command(name=name)(
        synthesize_command_fn(name=name, param_specs=list(spec.params), body=body)
    )
    with patch("graftpunk.cli.plugin_runtime.format_output"):
        return CliRunner().invoke(app, list(args), obj={"observe_mode": observe_mode})


def _invoke_search(base_url: str, observe_mode: str) -> Any:
    return _invoke(base_url, observe_mode, "search", "--query", "widget")


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
        # Only what was written: no empty screenshots/ or bodies/, and no spool.
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

    def test_a_response_before_the_failure_is_kept(self, site: str, tmp_path: Path) -> None:
        with patch("graftpunk.observe.context.OBSERVE_BASE_DIR", tmp_path):
            result = _invoke(site, "full", "broken")

        assert result.exit_code == 1
        (entry,) = _har_entries(_only_run(tmp_path))
        assert entry["response"]["status"] == 500
        assert json.loads(entry["response"]["content"]["text"]) == {"error": "boom"}

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
        # The run directory it opened is removed, not left empty.
        assert _run_dirs(tmp_path) == []

    def test_a_run_directory_that_cannot_be_made_does_not_stop_the_command(
        self, site: str, tmp_path: Path
    ) -> None:
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("")
        with patch("graftpunk.observe.context.OBSERVE_BASE_DIR", blocker / "observe"):
            result = _invoke_search(site, "full")

        assert result.exit_code == 0, result.output
        assert "Observability capture unavailable" in result.output

    def test_the_plugins_login_credentials_are_scrubbed(
        self, site: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.observe.run import REDACTED

        # The command sends the password it was given; the run must not keep it.
        monkeypatch.setenv("MYSHOP_PASSWORD", "hunter2-correct-horse")
        with patch("graftpunk.observe.context.OBSERVE_BASE_DIR", tmp_path):
            result = _invoke(site, "full", "search", "--query", "hunter2-correct-horse")

        assert result.exit_code == 0, result.output
        har = (_only_run(tmp_path) / "network.har").read_text()
        assert "hunter2-correct-horse" not in har
        assert REDACTED in har

    @staticmethod
    def _env(password: Any) -> Any:
        from graftpunk import workstation_env

        return workstation_env.WorkstationEnv(
            {
                "MYSHOP_USERNAME": workstation_env.Entry(
                    "MYSHOP_USERNAME", "alice@example.com", "static", 1
                ),
                "MYSHOP_PASSWORD": password,
            }
        )

    def test_a_static_password_is_known_and_the_username_is_not(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk import workstation_env
        from graftpunk.cli.login_commands import known_secret_credentials

        monkeypatch.delenv("MYSHOP_PASSWORD", raising=False)
        monkeypatch.delenv("MYSHOP_USERNAME", raising=False)
        env = self._env(workstation_env.Entry("MYSHOP_PASSWORD", "s3cret-static", "static", 2))
        with patch.object(workstation_env, "load", return_value=env):
            assert known_secret_credentials(_ShopPlugin()) == ["s3cret-static"]

    def test_a_credential_kept_as_a_command_is_not_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk import workstation_env
        from graftpunk.cli.login_commands import known_secret_credentials

        monkeypatch.delenv("MYSHOP_PASSWORD", raising=False)
        monkeypatch.delenv("MYSHOP_USERNAME", raising=False)
        ran = tmp_path / "ran"
        command = f"touch {ran} && echo command-secret"
        env = self._env(
            workstation_env.Entry("MYSHOP_PASSWORD", f"$({command})", "command", 2, command=command)
        )
        with patch.object(workstation_env, "load", return_value=env):
            values = known_secret_credentials(_ShopPlugin())

        assert values == []
        assert not ran.exists()

    def test_basic_auth_built_from_the_login_is_scrubbed_whole(
        self, site: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import base64

        from graftpunk.observe.run import REDACTED

        # len("bob@example.com") + 1 is not a multiple of 3, so the password's
        # own base64 is not a substring of the token.
        user, password = "bob@example.com", "hunter2-correct-horse"
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        assert base64.b64encode(password.encode()).decode() not in token
        monkeypatch.setenv("MYSHOP_USERNAME", user)
        monkeypatch.setenv("MYSHOP_PASSWORD", password)

        class _AuthPlugin(_ShopPlugin):
            @command(help="Who am I")
            def whoami(self, ctx: Any) -> dict[str, Any]:
                ctx.session.auth = (user, password)
                return ctx.session.get(f"{self.base_url}/whoami", params={"u": user}).json()

        plugin = _AuthPlugin()
        plugin.base_url = site
        spec = next(s for s in plugin.get_commands() if s.name == "whoami")
        app = typer.Typer()
        app.command(name="whoami")(
            synthesize_command_fn(
                name="whoami",
                param_specs=list(spec.params),
                body=lambda ctx, **kw: run_plugin_command(plugin, spec, ctx, **kw),
            )
        )
        with (
            patch("graftpunk.observe.context.OBSERVE_BASE_DIR", tmp_path),
            patch("graftpunk.cli.plugin_runtime.format_output"),
        ):
            result = CliRunner().invoke(app, [], obj={"observe_mode": "full"})

        assert result.exit_code == 0, result.output
        har = (_only_run(tmp_path) / "network.har").read_text()
        assert token not in har
        assert password not in har
        (entry,) = _har_entries(_only_run(tmp_path))
        headers = {h["name"]: h["value"] for h in entry["request"]["headers"]}
        assert headers["Authorization"] == f"Basic {REDACTED}"
        # The echo in the response body is replaced whole too.
        assert json.loads(entry["response"]["content"]["text"]) == {
            "authorization": f"Basic {REDACTED}"
        }
        # A username is not a secret: the URL keeps it.
        assert "u=bob%40example.com" in entry["request"]["url"]


class TestRunFilesAreOwnerOnly:
    def test_a_plugin_command_run_is_owner_only_under_umask_022(
        self, site: str, tmp_path: Path
    ) -> None:
        import os
        import stat

        old = os.umask(0o022)
        try:
            with patch("graftpunk.observe.context.OBSERVE_BASE_DIR", tmp_path):

                class _Big(_ShopPlugin):
                    @command(help="Fetch a picture")
                    def logo(self, ctx: Any) -> dict[str, Any]:
                        ctx.session.get(f"{self.base_url}/logo.png")
                        return {}

                plugin = _Big()
                plugin.base_url = site
                spec = next(s for s in plugin.get_commands() if s.name == "logo")
                app = typer.Typer()
                app.command(name="logo")(
                    synthesize_command_fn(
                        name="logo",
                        param_specs=list(spec.params),
                        body=lambda ctx, **kw: run_plugin_command(plugin, spec, ctx, **kw),
                    )
                )
                with patch("graftpunk.cli.plugin_runtime.format_output"):
                    result = CliRunner().invoke(app, [], obj={"observe_mode": "full"})
        finally:
            os.umask(old)

        assert result.exit_code == 0, result.output
        run = _only_run(tmp_path)

        def mode(path: Path) -> int:
            return stat.S_IMODE(path.stat().st_mode)

        assert mode(run.parent) == 0o700
        assert mode(run) == 0o700
        assert mode(run / "bodies") == 0o700
        assert mode(run / "network.har") == 0o600
        assert mode(run / "bodies" / "http-1.png") == 0o600

    def test_every_storage_file_is_owner_only_under_umask_022(self, tmp_path: Path) -> None:
        import os
        import stat

        from graftpunk.observe.storage import ObserveStorage

        old = os.umask(0o022)
        try:
            storage = ObserveStorage(tmp_path, "myshop", "run-1")
            storage.save_screenshot(1, "page", b"\x89PNG")
            storage.write_event("mark", {})
            storage.write_console_logs([{"text": "hi"}])
            storage.write_metadata({})
            storage.write_har([])
        finally:
            os.umask(old)

        run = storage.run_dir
        modes = {
            str(p.relative_to(tmp_path)): stat.S_IMODE(p.stat().st_mode)
            for p in [run.parent, *run.parent.rglob("*")]
        }
        assert modes == {
            "myshop": 0o700,
            "myshop/run-1": 0o700,
            "myshop/run-1/screenshots": 0o700,
            "myshop/run-1/screenshots/001-page.png": 0o600,
            "myshop/run-1/events.jsonl": 0o600,
            "myshop/run-1/console.jsonl": 0o600,
            "myshop/run-1/metadata.json": 0o600,
            "myshop/run-1/network.har": 0o600,
        }

    def test_an_older_session_directory_keeps_its_mode(self, tmp_path: Path) -> None:
        import stat

        from graftpunk.observe.storage import ObserveStorage

        (tmp_path / "myshop").mkdir(mode=0o755)
        (tmp_path / "myshop").chmod(0o755)
        ObserveStorage(tmp_path, "myshop", "run-2")

        assert stat.S_IMODE((tmp_path / "myshop").stat().st_mode) == 0o755
        assert stat.S_IMODE((tmp_path / "myshop" / "run-2").stat().st_mode) == 0o700


class TestGpHttpRun:
    def _gp_http(self, tmp_path: Path, *args: str) -> Any:
        from graftpunk.cli.main import app

        with patch("graftpunk.observe.context.OBSERVE_BASE_DIR", tmp_path):
            return CliRunner().invoke(app, ["http", *args])

    def test_each_hop_is_recorded_as_it_is_sent(self, site: str, tmp_path: Path) -> None:
        import datetime

        before = datetime.datetime.now(datetime.UTC)
        result = self._gp_http(
            tmp_path, "post", "--no-session", "-d", "user=alice", f"{site}/login"
        )
        after = datetime.datetime.now(datetime.UTC)

        assert result.exit_code == 0, result.output
        hop, landing = _har_entries(_only_run(tmp_path))
        assert hop["response"]["status"] == 302
        assert hop["request"]["postData"]["text"] == "user=alice"
        assert landing["response"]["content"]["text"] == "<h1>home</h1>"
        started = [datetime.datetime.fromisoformat(e["startedDateTime"]) for e in (hop, landing)]
        assert before <= started[0] <= started[1] <= after

    @pytest.mark.parametrize(
        ("path", "text"),
        [("/csv", "a,b\n1,2\n"), ("/form", "token=abc&x=1"), ("/latin1", LATIN1_TEXT)],
    )
    def test_text_bodies_are_kept(self, site: str, tmp_path: Path, path: str, text: str) -> None:
        result = self._gp_http(tmp_path, "get", "--no-session", f"{site}{path}")

        assert result.exit_code == 0, result.output
        (entry,) = _har_entries(_only_run(tmp_path))
        assert entry["response"]["content"]["text"] == text

    def test_a_body_with_no_type_is_kept_in_bodies(self, site: str, tmp_path: Path) -> None:
        result = self._gp_http(tmp_path, "get", "--no-session", f"{site}/no-ct")

        assert result.exit_code == 0, result.output
        run = _only_run(tmp_path)
        (entry,) = _har_entries(run)
        assert (run / entry["response"]["content"]["_bodyFile"]).read_bytes() == b'{"a": 1}'

    def test_no_observe_writes_nothing(self, site: str, tmp_path: Path) -> None:
        result = self._gp_http(tmp_path, "get", "--no-session", "--no-observe", f"{site}/home")

        assert result.exit_code == 0, result.output
        assert _run_dirs(tmp_path) == []
