"""Tests for the observability module."""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
import selenium.common.exceptions

from graftpunk.observe import NoOpObservabilityContext, ObservabilityContext
from graftpunk.observe.capture import (
    MAX_RESPONSE_BODY_SIZE,
    NodriverCaptureBackend,
    SeleniumCaptureBackend,
    _build_har_entry,
    _is_binary_mime,
    _is_text_mime,
    _mime_to_extension,
    _should_stream_to_disk,
    _wall_time_to_iso,
    create_capture_backend,
)
from graftpunk.observe.storage import ObserveStorage

# ---------------------------------------------------------------------------
# Test helpers for NodriverCaptureBackend tests
# ---------------------------------------------------------------------------


def _make_request_entry(
    url: str = "https://example.com/api",
    method: str = "GET",
    has_post_data: bool = False,
    post_data: str | None = None,
    status: int = 200,
    mime_type: str = "application/json",
) -> dict[str, Any]:
    """Build a request-map entry for NodriverCaptureBackend tests."""
    return {
        "url": url,
        "method": method,
        "headers": {},
        "post_data": post_data,
        "has_post_data": has_post_data,
        "timestamp": None,
        "response": {
            "status": status,
            "statusText": "OK",
            "headers": {},
            "mimeType": mime_type,
        },
    }


@contextmanager
def _patch_cdp_modules(
    *,
    include_runtime: bool = False,
) -> Generator[MagicMock, None, None]:
    """Patch sys.modules to provide mock CDP network (and optionally runtime) modules.

    Yields the mock network module so tests can assert on network.enable() etc.
    """
    mock_network = MagicMock()
    mock_cdp = MagicMock()
    mock_cdp.network = mock_network
    mock_nodriver = MagicMock()
    mock_nodriver.cdp = mock_cdp

    modules: dict[str, Any] = {
        "nodriver": mock_nodriver,
        "nodriver.cdp": mock_cdp,
        "nodriver.cdp.network": mock_network,
    }
    if include_runtime:
        mock_runtime = MagicMock()
        mock_cdp.runtime = mock_runtime
        modules["nodriver.cdp.runtime"] = mock_runtime

    with patch.dict("sys.modules", modules):
        yield mock_network


# ---------------------------------------------------------------------------
# ObserveStorage tests
# ---------------------------------------------------------------------------


class TestObserveStorage:
    """Tests for ObserveStorage file operations."""

    def test_creates_run_directory(self, tmp_path: Path) -> None:
        storage = ObserveStorage(tmp_path, "mysession", "run-001")
        assert storage.run_dir.exists()
        assert (storage.run_dir / "screenshots").is_dir()

    def test_save_screenshot(self, tmp_path: Path) -> None:
        storage = ObserveStorage(tmp_path, "mysession", "run-001")
        png_data = b"\x89PNG\r\n\x1a\nfake"
        path = storage.save_screenshot(1, "login-page", png_data)
        assert path.name == "001-login-page.png"
        assert path.read_bytes() == png_data

    def test_write_and_read_events(self, tmp_path: Path) -> None:
        storage = ObserveStorage(tmp_path, "mysession", "run-001")
        storage.write_event("click", {"target": "button"})
        storage.write_event("navigate", {"url": "https://example.com"})
        events = storage.read_events()
        assert len(events) == 2
        assert events[0]["event"] == "click"
        assert events[0]["target"] == "button"
        assert "timestamp" in events[0]
        assert events[1]["event"] == "navigate"

    def test_read_events_empty(self, tmp_path: Path) -> None:
        storage = ObserveStorage(tmp_path, "mysession", "run-001")
        assert storage.read_events() == []

    def test_read_events_skips_corrupt_lines(self, tmp_path: Path) -> None:
        storage = ObserveStorage(tmp_path, "mysession", "run-001")
        # Write a valid event, then corrupt data, then another valid event
        storage.write_event("click", {"target": "button"})
        with storage._events_path.open("a") as f:
            f.write("this is not valid json\n")
        storage.write_event("navigate", {"url": "https://example.com"})

        events = storage.read_events()
        assert len(events) == 2
        assert events[0]["event"] == "click"
        assert events[1]["event"] == "navigate"

    def test_init_rejects_empty_session_name(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid session_name"):
            ObserveStorage(tmp_path, "", "run-001")

    def test_init_rejects_slash_in_session_name(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid session_name"):
            ObserveStorage(tmp_path, "foo/bar", "run-001")

    def test_init_rejects_backslash_in_session_name(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid session_name"):
            ObserveStorage(tmp_path, "foo\\bar", "run-001")

    def test_init_rejects_dotdot_in_session_name(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid session_name"):
            ObserveStorage(tmp_path, "..", "run-001")

    def test_init_rejects_empty_run_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid run_id"):
            ObserveStorage(tmp_path, "mysession", "")

    def test_init_rejects_slash_in_run_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid run_id"):
            ObserveStorage(tmp_path, "mysession", "foo/bar")

    def test_init_rejects_backslash_in_run_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid run_id"):
            ObserveStorage(tmp_path, "mysession", "foo\\bar")

    def test_init_rejects_dotdot_in_run_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid run_id"):
            ObserveStorage(tmp_path, "mysession", "..")

    def test_init_rejects_space_in_session_name(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid session_name"):
            ObserveStorage(tmp_path, "foo bar", "run-001")

    def test_init_rejects_leading_dot_in_session_name(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid session_name"):
            ObserveStorage(tmp_path, ".hidden", "run-001")

    def test_init_rejects_special_chars_in_run_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid run_id"):
            ObserveStorage(tmp_path, "mysession", "run@001")

    def test_init_accepts_valid_names_with_dots_and_dashes(self, tmp_path: Path) -> None:
        storage = ObserveStorage(tmp_path, "my-session.v2", "run-001.1")
        assert storage.run_dir.exists()

    def test_write_har(self, tmp_path: Path) -> None:
        storage = ObserveStorage(tmp_path, "mysession", "run-001")
        entries = [{"request": {"url": "https://example.com"}}]
        storage.write_har(entries)
        har = json.loads((storage.run_dir / "network.har").read_text())
        assert har["log"]["version"] == "1.2"
        assert har["log"]["creator"]["name"] == "graftpunk"
        assert har["log"]["entries"] == entries

    def test_write_console_logs(self, tmp_path: Path) -> None:
        storage = ObserveStorage(tmp_path, "mysession", "run-001")
        logs = [{"level": "error", "message": "oops"}]
        storage.write_console_logs(logs)
        console_path = storage.run_dir / "console.jsonl"
        lines = console_path.read_text().strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0])["message"] == "oops"

    def test_write_metadata(self, tmp_path: Path) -> None:
        storage = ObserveStorage(tmp_path, "mysession", "run-001")
        meta = {"plugin": "mybank", "started_at": "2025-01-01T00:00:00"}
        storage.write_metadata(meta)
        loaded = json.loads((storage.run_dir / "metadata.json").read_text())
        assert loaded == meta


# ---------------------------------------------------------------------------
# ObservabilityContext tests
# ---------------------------------------------------------------------------


class TestObservabilityContext:
    """Tests for ObservabilityContext plugin-facing API."""

    def _make_context(
        self,
        tmp_path: Path,
        mode: str = "full",
        screenshot_data: bytes | None = b"\x89PNGfake",
    ) -> tuple[ObservabilityContext, ObserveStorage, MagicMock]:
        storage = ObserveStorage(tmp_path, "test", "run-001")
        capture = MagicMock()
        capture.take_screenshot_sync.return_value = screenshot_data
        ctx = ObservabilityContext(capture=capture, storage=storage, mode=mode)
        return ctx, storage, capture

    def test_screenshot_saves_file(self, tmp_path: Path) -> None:
        ctx, storage, capture = self._make_context(tmp_path)
        path = ctx.screenshot("after-login")
        assert path is not None
        assert path.exists()
        assert path.name == "001-after-login.png"
        capture.take_screenshot_sync.assert_called_once()

    def test_screenshot_increments_counter(self, tmp_path: Path) -> None:
        ctx, storage, capture = self._make_context(tmp_path)
        ctx.screenshot("first")
        path = ctx.screenshot("second")
        assert path is not None
        assert path.name == "002-second.png"

    def test_screenshot_returns_none_when_capture_fails(self, tmp_path: Path) -> None:
        ctx, storage, capture = self._make_context(tmp_path, screenshot_data=None)
        result = ctx.screenshot("fail")
        assert result is None

    def test_screenshot_noop_when_off(self, tmp_path: Path) -> None:
        ctx, storage, capture = self._make_context(tmp_path, mode="off")
        result = ctx.screenshot("ignored")
        assert result is None
        capture.take_screenshot_sync.assert_not_called()

    def test_log_writes_event(self, tmp_path: Path) -> None:
        ctx, storage, _capture = self._make_context(tmp_path)
        ctx.log("user_action", {"action": "click"})
        events = storage.read_events()
        assert len(events) == 1
        assert events[0]["event"] == "user_action"
        assert events[0]["action"] == "click"

    def test_log_noop_when_off(self, tmp_path: Path) -> None:
        ctx, storage, _capture = self._make_context(tmp_path, mode="off")
        ctx.log("ignored")
        assert storage.read_events() == []

    def test_log_defaults_data_to_empty(self, tmp_path: Path) -> None:
        ctx, storage, _capture = self._make_context(tmp_path)
        ctx.log("simple_event")
        events = storage.read_events()
        assert len(events) == 1
        assert events[0]["event"] == "simple_event"

    def test_mark_writes_mark_event(self, tmp_path: Path) -> None:
        ctx, storage, _capture = self._make_context(tmp_path)
        ctx.mark("before-submit")
        events = storage.read_events()
        assert len(events) == 1
        assert events[0]["event"] == "mark"
        assert events[0]["label"] == "before-submit"
        assert "timestamp" in events[0]

    def test_mark_noop_when_off(self, tmp_path: Path) -> None:
        ctx, storage, _capture = self._make_context(tmp_path, mode="off")
        ctx.mark("ignored")
        assert storage.read_events() == []

    @pytest.mark.asyncio
    async def test_screenshot_async_saves_file(self, tmp_path: Path) -> None:
        storage = ObserveStorage(tmp_path, "test-session", "run-001")
        capture = AsyncMock()
        capture.take_screenshot = AsyncMock(return_value=b"\x89PNGfake")
        ctx = ObservabilityContext(capture=capture, storage=storage, mode="full")
        path = await ctx.screenshot_async("test-label")
        assert path is not None
        assert path.exists()
        assert b"\x89PNGfake" in path.read_bytes()

    @pytest.mark.asyncio
    async def test_screenshot_async_noop_when_off(self) -> None:
        ctx = NoOpObservabilityContext()
        result = await ctx.screenshot_async("test")
        assert result is None

    @pytest.mark.asyncio
    async def test_screenshot_async_returns_none_on_capture_failure(self, tmp_path: Path) -> None:
        storage = ObserveStorage(tmp_path, "test-session", "run-001")
        capture = AsyncMock()
        capture.take_screenshot = AsyncMock(return_value=None)
        ctx = ObservabilityContext(capture=capture, storage=storage, mode="full")
        result = await ctx.screenshot_async("fail-label")
        assert result is None


# ---------------------------------------------------------------------------
# NoOpObservabilityContext tests
# ---------------------------------------------------------------------------


class TestNoOpObservabilityContext:
    """Tests for the no-op implementation."""

    def test_screenshot_returns_none(self) -> None:
        ctx = NoOpObservabilityContext()
        assert ctx.screenshot("anything") is None

    def test_log_does_nothing(self) -> None:
        ctx = NoOpObservabilityContext()
        ctx.log("event", {"key": "value"})  # Should not raise

    def test_mark_does_nothing(self) -> None:
        ctx = NoOpObservabilityContext()
        ctx.mark("label")  # Should not raise

    def test_mode_is_off(self) -> None:
        ctx = NoOpObservabilityContext()
        assert ctx._mode == "off"


# ---------------------------------------------------------------------------
# Capture backend tests
# ---------------------------------------------------------------------------


class TestSeleniumCaptureBackend:
    """Tests for SeleniumCaptureBackend."""

    def test_start_capture(self) -> None:
        driver = MagicMock()
        backend = SeleniumCaptureBackend(driver)
        backend.start_capture()  # Should not raise

    def test_take_screenshot_sync(self) -> None:
        driver = MagicMock()
        driver.get_screenshot_as_png.return_value = b"png-data"
        backend = SeleniumCaptureBackend(driver)
        result = backend.take_screenshot_sync()
        assert result == b"png-data"

    def test_take_screenshot_sync_handles_error(self) -> None:
        driver = MagicMock()
        driver.get_screenshot_as_png.side_effect = selenium.common.exceptions.WebDriverException(
            "no browser"
        )
        backend = SeleniumCaptureBackend(driver)
        result = backend.take_screenshot_sync()
        assert result is None

    def test_stop_capture_collects_browser_logs(self) -> None:
        driver = MagicMock()
        # Performance log returns empty, browser log returns entries
        driver.get_log.side_effect = lambda log_type: (
            [] if log_type == "performance" else [{"level": "ERROR", "message": "oops"}]
        )
        backend = SeleniumCaptureBackend(driver)
        backend.stop_capture()
        logs = backend.get_console_logs()
        assert {"level": "ERROR", "message": "oops"} in logs

    def test_stop_capture_handles_browser_log_error(self) -> None:
        driver = MagicMock()

        def side_effect(log_type: str) -> list:
            if log_type == "performance":
                return []
            raise selenium.common.exceptions.WebDriverException("not supported")

        driver.get_log.side_effect = side_effect
        backend = SeleniumCaptureBackend(driver)
        backend.stop_capture()  # Should not raise
        # Console logs should be empty (perf log had nothing, browser log failed)
        assert backend.get_console_logs() == []

    def test_stop_capture_warns_on_perf_log_failure(self) -> None:
        """Performance log failure triggers gp_console.warn to notify the user."""
        driver = MagicMock()

        def get_log_side_effect(log_type: str) -> list:
            if log_type == "performance":
                raise selenium.common.exceptions.WebDriverException("unavailable")
            return []

        driver.get_log.side_effect = get_log_side_effect
        backend = SeleniumCaptureBackend(driver)

        with patch("graftpunk.console.warn") as mock_warn:
            backend.stop_capture()

        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        assert "Performance log collection failed" in msg

    def test_get_har_entries_empty_initially(self) -> None:
        driver = MagicMock()
        backend = SeleniumCaptureBackend(driver)
        assert backend.get_har_entries() == []

    def test_get_har_entries_returns_har_format(self) -> None:
        driver = MagicMock()
        backend = SeleniumCaptureBackend(driver)
        # Seed a request directly
        backend._request_map["r1"] = {
            "url": "https://example.com",
            "method": "GET",
            "headers": {},
            "timestamp": 1700000000.0,
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": {},
                "mimeType": "text/html",
            },
        }
        entries = backend.get_har_entries()
        assert len(entries) == 1
        assert entries[0]["request"]["method"] == "GET"
        assert entries[0]["response"]["status"] == 200

    @pytest.mark.asyncio
    async def test_take_screenshot_async_wraps_sync(self) -> None:
        driver = MagicMock()
        driver.get_screenshot_as_png.return_value = b"png-data"
        backend = SeleniumCaptureBackend(driver)
        result = await backend.take_screenshot()
        assert result == b"png-data"

    @pytest.mark.asyncio
    async def test_get_page_source_async(self) -> None:
        driver = MagicMock()
        driver.page_source = "<html>hello</html>"
        backend = SeleniumCaptureBackend(driver)
        result = await backend.get_page_source()
        assert result == "<html>hello</html>"

    @pytest.mark.asyncio
    async def test_get_page_source_async_handles_error(self) -> None:
        driver = MagicMock()
        type(driver).page_source = PropertyMock(
            side_effect=selenium.common.exceptions.WebDriverException("no browser")
        )
        backend = SeleniumCaptureBackend(driver)
        result = await backend.get_page_source()
        assert result is None


class TestSeleniumHARCapture:
    """Tests for full network capture in SeleniumCaptureBackend."""

    def _make_perf_entry(self, method: str, params: dict) -> dict:
        """Create a performance log entry in CDP format."""
        return {
            "message": json.dumps({"message": {"method": method, "params": params}}),
            "level": "INFO",
        }

    def _make_driver(
        self,
        perf_entries: list[dict] | None = None,
        browser_logs: list[dict] | None = None,
    ) -> MagicMock:
        """Create a mock driver with configurable log responses."""
        driver = MagicMock()

        def get_log(log_type: str) -> list:
            if log_type == "performance":
                return perf_entries or []
            if log_type == "browser":
                return browser_logs or []
            return []

        driver.get_log.side_effect = get_log
        return driver

    def test_stop_capture_parses_cdp_request_events(self) -> None:
        """Performance log requestWillBeSent events are parsed into _request_map."""
        perf_entries = [
            self._make_perf_entry(
                "Network.requestWillBeSent",
                {
                    "requestId": "req-1",
                    "request": {
                        "url": "https://example.com/api",
                        "method": "GET",
                        "headers": {"Accept": "text/html"},
                        "hasPostData": False,
                        "postData": None,
                    },
                    "wallTime": 1700000000.0,
                },
            ),
        ]
        driver = self._make_driver(perf_entries=perf_entries)
        driver.execute_cdp_cmd.side_effect = Exception("no body")
        backend = SeleniumCaptureBackend(driver)
        backend.stop_capture()

        assert "req-1" in backend._request_map
        data = backend._request_map["req-1"]
        assert data["url"] == "https://example.com/api"
        assert data["method"] == "GET"
        assert data["headers"] == {"Accept": "text/html"}
        assert data["timestamp"] == 1700000000.0

    def test_stop_capture_parses_cdp_response_events(self) -> None:
        """Performance log responseReceived events correlate with requests."""
        perf_entries = [
            self._make_perf_entry(
                "Network.requestWillBeSent",
                {
                    "requestId": "req-2",
                    "request": {
                        "url": "https://example.com/data",
                        "method": "GET",
                        "headers": {},
                        "hasPostData": False,
                    },
                    "wallTime": 1700000000.0,
                },
            ),
            self._make_perf_entry(
                "Network.responseReceived",
                {
                    "requestId": "req-2",
                    "response": {
                        "url": "https://example.com/data",
                        "status": 200,
                        "statusText": "OK",
                        "headers": {"Content-Type": "application/json"},
                        "mimeType": "application/json",
                    },
                },
            ),
        ]
        driver = self._make_driver(perf_entries=perf_entries)
        driver.execute_cdp_cmd.return_value = {
            "body": '{"ok": true}',
            "base64Encoded": False,
        }
        backend = SeleniumCaptureBackend(driver)
        backend.stop_capture()

        data = backend._request_map["req-2"]
        assert data["response"]["status"] == 200
        assert data["response"]["mimeType"] == "application/json"

    def test_stop_capture_fetches_post_body(self) -> None:
        """POST body fetched via execute_cdp_cmd for entries with hasPostData."""
        perf_entries = [
            self._make_perf_entry(
                "Network.requestWillBeSent",
                {
                    "requestId": "req-3",
                    "request": {
                        "url": "https://example.com/login",
                        "method": "POST",
                        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                        "hasPostData": True,
                        "postData": None,
                    },
                    "wallTime": 1700000000.0,
                },
            ),
            self._make_perf_entry(
                "Network.responseReceived",
                {
                    "requestId": "req-3",
                    "response": {
                        "url": "https://example.com/login",
                        "status": 302,
                        "statusText": "Found",
                        "headers": {},
                        "mimeType": "text/html",
                    },
                },
            ),
        ]
        driver = self._make_driver(perf_entries=perf_entries)

        def cdp_cmd(cmd: str, params: dict) -> dict:
            if cmd == "Network.getRequestPostData":
                return {"postData": "user=foo&pass=bar"}
            if cmd == "Network.getResponseBody":
                return {"body": "<html></html>", "base64Encoded": False}
            return {}

        driver.execute_cdp_cmd.side_effect = cdp_cmd
        backend = SeleniumCaptureBackend(driver)
        backend.stop_capture()

        assert backend._request_map["req-3"]["post_data"] == "user=foo&pass=bar"

    def test_stop_capture_fetches_response_body(self) -> None:
        """Response body fetched via execute_cdp_cmd for text MIME types."""
        perf_entries = [
            self._make_perf_entry(
                "Network.requestWillBeSent",
                {
                    "requestId": "req-4",
                    "request": {
                        "url": "https://example.com/api",
                        "method": "GET",
                        "headers": {},
                        "hasPostData": False,
                    },
                    "wallTime": 1700000000.0,
                },
            ),
            self._make_perf_entry(
                "Network.responseReceived",
                {
                    "requestId": "req-4",
                    "response": {
                        "url": "https://example.com/api",
                        "status": 200,
                        "statusText": "OK",
                        "headers": {},
                        "mimeType": "application/json",
                    },
                },
            ),
        ]
        driver = self._make_driver(perf_entries=perf_entries)
        driver.execute_cdp_cmd.return_value = {
            "body": '{"result": 42}',
            "base64Encoded": False,
        }
        backend = SeleniumCaptureBackend(driver)
        backend.stop_capture()

        resp = backend._request_map["req-4"]["response"]
        assert resp["body"] == '{"result": 42}'
        assert resp["bodySize"] == len(b'{"result": 42}')

    def test_stop_capture_skips_binary_response_body_without_disk(self) -> None:
        """Binary MIME without bodies_dir: body not stored."""
        perf_entries = [
            self._make_perf_entry(
                "Network.requestWillBeSent",
                {
                    "requestId": "req-5",
                    "request": {
                        "url": "https://example.com/image.png",
                        "method": "GET",
                        "headers": {},
                        "hasPostData": False,
                    },
                    "wallTime": 1700000000.0,
                },
            ),
            self._make_perf_entry(
                "Network.responseReceived",
                {
                    "requestId": "req-5",
                    "response": {
                        "url": "https://example.com/image.png",
                        "status": 200,
                        "statusText": "OK",
                        "headers": {},
                        "mimeType": "image/png",
                    },
                },
            ),
        ]
        import base64 as b64

        png_data = b"\x89PNG\r\n\x1a\nfake"
        driver = self._make_driver(perf_entries=perf_entries)
        driver.execute_cdp_cmd.return_value = {
            "body": b64.b64encode(png_data).decode(),
            "base64Encoded": True,
        }
        backend = SeleniumCaptureBackend(driver, bodies_dir=None)
        backend.stop_capture()

        resp = backend._request_map["req-5"]["response"]
        # Body was fetched (bodySize set) but not stored (no bodies_dir, binary)
        assert resp["bodySize"] == len(png_data)
        assert resp.get("body") is None
        assert resp.get("_bodyFile") is None

    def test_stop_capture_streams_binary_to_disk(self, tmp_path: Path) -> None:
        """Binary MIME types written to bodies_dir."""
        bodies_dir = tmp_path / "bodies"
        perf_entries = [
            self._make_perf_entry(
                "Network.requestWillBeSent",
                {
                    "requestId": "req-6",
                    "request": {
                        "url": "https://example.com/photo.png",
                        "method": "GET",
                        "headers": {},
                        "hasPostData": False,
                    },
                    "wallTime": 1700000000.0,
                },
            ),
            self._make_perf_entry(
                "Network.responseReceived",
                {
                    "requestId": "req-6",
                    "response": {
                        "url": "https://example.com/photo.png",
                        "status": 200,
                        "statusText": "OK",
                        "headers": {},
                        "mimeType": "image/png",
                    },
                },
            ),
        ]
        import base64 as b64

        png_data = b"\x89PNG\r\n\x1a\nfake_image"
        driver = self._make_driver(perf_entries=perf_entries)
        driver.execute_cdp_cmd.return_value = {
            "body": b64.b64encode(png_data).decode(),
            "base64Encoded": True,
        }
        backend = SeleniumCaptureBackend(driver, bodies_dir=bodies_dir)
        backend.stop_capture()

        resp = backend._request_map["req-6"]["response"]
        assert resp["_bodyFile"] == "bodies/req-6.png"
        assert (bodies_dir / "req-6.png").exists()
        assert (bodies_dir / "req-6.png").read_bytes() == png_data

    def test_stop_capture_streams_large_text_to_disk(self, tmp_path: Path) -> None:
        """Text responses over max_body_size written to bodies_dir."""
        bodies_dir = tmp_path / "bodies"
        max_size = 100
        perf_entries = [
            self._make_perf_entry(
                "Network.requestWillBeSent",
                {
                    "requestId": "req-7",
                    "request": {
                        "url": "https://example.com/large.html",
                        "method": "GET",
                        "headers": {},
                        "hasPostData": False,
                    },
                    "wallTime": 1700000000.0,
                },
            ),
            self._make_perf_entry(
                "Network.responseReceived",
                {
                    "requestId": "req-7",
                    "response": {
                        "url": "https://example.com/large.html",
                        "status": 200,
                        "statusText": "OK",
                        "headers": {},
                        "mimeType": "text/html",
                    },
                },
            ),
        ]
        large_body = "x" * 200
        driver = self._make_driver(perf_entries=perf_entries)
        driver.execute_cdp_cmd.return_value = {
            "body": large_body,
            "base64Encoded": False,
        }
        backend = SeleniumCaptureBackend(driver, max_body_size=max_size, bodies_dir=bodies_dir)
        backend.stop_capture()

        resp = backend._request_map["req-7"]["response"]
        assert resp["_bodyFile"] == "bodies/req-7.html"
        assert (bodies_dir / "req-7.html").exists()

    def test_get_har_entries_produces_valid_har_format(self) -> None:
        """get_har_entries produces HAR 1.2 spec entries after stop_capture."""
        perf_entries = [
            self._make_perf_entry(
                "Network.requestWillBeSent",
                {
                    "requestId": "req-8",
                    "request": {
                        "url": "https://example.com/page",
                        "method": "GET",
                        "headers": {"Accept": "text/html"},
                        "hasPostData": False,
                    },
                    "wallTime": 1700000000.0,
                },
            ),
            self._make_perf_entry(
                "Network.responseReceived",
                {
                    "requestId": "req-8",
                    "response": {
                        "url": "https://example.com/page",
                        "status": 200,
                        "statusText": "OK",
                        "headers": {"Content-Type": "text/html"},
                        "mimeType": "text/html",
                    },
                },
            ),
        ]
        driver = self._make_driver(perf_entries=perf_entries)
        driver.execute_cdp_cmd.return_value = {
            "body": "<html>hello</html>",
            "base64Encoded": False,
        }
        backend = SeleniumCaptureBackend(driver)
        backend.stop_capture()

        entries = backend.get_har_entries()
        assert len(entries) == 1
        entry = entries[0]
        # HAR 1.2 required fields
        assert "startedDateTime" in entry
        assert "time" in entry
        assert entry["request"]["method"] == "GET"
        assert entry["request"]["url"] == "https://example.com/page"
        assert isinstance(entry["request"]["headers"], list)
        assert entry["response"]["status"] == 200
        assert entry["response"]["content"]["text"] == "<html>hello</html>"

    def test_stop_capture_handles_evicted_request(self) -> None:
        """CDP call failure (evicted data) is handled gracefully."""
        perf_entries = [
            self._make_perf_entry(
                "Network.requestWillBeSent",
                {
                    "requestId": "req-9",
                    "request": {
                        "url": "https://example.com/evicted",
                        "method": "POST",
                        "headers": {},
                        "hasPostData": True,
                        "postData": None,
                    },
                    "wallTime": 1700000000.0,
                },
            ),
            self._make_perf_entry(
                "Network.responseReceived",
                {
                    "requestId": "req-9",
                    "response": {
                        "url": "https://example.com/evicted",
                        "status": 200,
                        "statusText": "OK",
                        "headers": {},
                        "mimeType": "text/html",
                    },
                },
            ),
        ]
        driver = self._make_driver(perf_entries=perf_entries)
        driver.execute_cdp_cmd.side_effect = Exception("No resource with given identifier")
        backend = SeleniumCaptureBackend(driver)
        backend.stop_capture()  # Should not raise

        # Request is still in map, just without body data
        assert "req-9" in backend._request_map
        resp = backend._request_map["req-9"]["response"]
        assert resp.get("body") is None
        assert backend._request_map["req-9"]["post_data"] is None

    def test_stop_capture_parses_console_logs(self) -> None:
        """Runtime.consoleAPICalled events are parsed from performance log."""
        perf_entries = [
            self._make_perf_entry(
                "Runtime.consoleAPICalled",
                {
                    "type": "log",
                    "args": [
                        {"type": "string", "value": "hello world"},
                    ],
                    "timestamp": 1700000000.0,
                },
            ),
            self._make_perf_entry(
                "Runtime.consoleAPICalled",
                {
                    "type": "error",
                    "args": [
                        {"type": "string", "value": "something broke"},
                    ],
                    "timestamp": 1700000001.0,
                },
            ),
        ]
        driver = self._make_driver(perf_entries=perf_entries)
        backend = SeleniumCaptureBackend(driver)
        backend.stop_capture()

        logs = backend.get_console_logs()
        # Should have the 2 CDP console entries (plus any browser log entries)
        cdp_logs = [log for log in logs if "args" in log]
        assert len(cdp_logs) == 2
        assert cdp_logs[0]["level"] == "log"
        assert cdp_logs[0]["args"] == ["hello world"]
        assert cdp_logs[1]["level"] == "error"
        assert cdp_logs[1]["args"] == ["something broke"]


class TestNodriverCaptureBackend:
    """Tests for NodriverCaptureBackend."""

    def test_start_capture(self) -> None:
        browser = MagicMock()
        backend = NodriverCaptureBackend(browser)
        backend.start_capture()  # Should not raise

    def test_stop_capture(self) -> None:
        browser = MagicMock()
        backend = NodriverCaptureBackend(browser)
        backend.stop_capture()  # Should not raise

    def test_take_screenshot_sync_returns_none(self) -> None:
        browser = MagicMock()
        backend = NodriverCaptureBackend(browser)
        assert backend.take_screenshot_sync() is None

    def test_get_har_entries_empty_initially(self) -> None:
        browser = MagicMock()
        backend = NodriverCaptureBackend(browser)
        assert backend.get_har_entries() == []

    def test_get_console_logs_empty(self) -> None:
        browser = MagicMock()
        backend = NodriverCaptureBackend(browser)
        assert backend.get_console_logs() == []

    def test_init_accepts_get_tab(self) -> None:
        browser = MagicMock()
        tab = MagicMock()
        backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)
        assert backend._tab is tab

    def test_tab_property_returns_none_without_get_tab(self) -> None:
        browser = MagicMock()
        backend = NodriverCaptureBackend(browser)
        assert backend._tab is None

    @pytest.mark.asyncio
    async def test_take_screenshot_async_returns_none_without_tab(self) -> None:
        browser = MagicMock()
        backend = NodriverCaptureBackend(browser)
        result = await backend.take_screenshot()
        assert result is None

    @pytest.mark.asyncio
    async def test_take_screenshot_async_returns_bytes_with_tab(self, tmp_path: Path) -> None:
        browser = MagicMock()
        tab = MagicMock()
        png_data = b"\x89PNG\r\n\x1a\nfake_screenshot"

        async def mock_save_screenshot(path: str) -> None:
            Path(path).write_bytes(png_data)

        tab.save_screenshot = mock_save_screenshot
        backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)
        result = await backend.take_screenshot()
        assert result == png_data

    @pytest.mark.asyncio
    async def test_take_screenshot_async_handles_exception(self) -> None:
        browser = MagicMock()
        tab = MagicMock()

        async def mock_save_screenshot(path: str) -> None:
            msg = "browser crashed"
            raise RuntimeError(msg)

        tab.save_screenshot = mock_save_screenshot
        backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)
        result = await backend.take_screenshot()
        assert result is None

    @pytest.mark.asyncio
    async def test_get_page_source_async_returns_html(self) -> None:
        browser = MagicMock()
        tab = MagicMock()
        tab.get_content = AsyncMock(return_value="<html><body>Hello</body></html>")
        backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)
        result = await backend.get_page_source()
        assert result == "<html><body>Hello</body></html>"

    @pytest.mark.asyncio
    async def test_get_page_source_async_returns_none_without_tab(self) -> None:
        browser = MagicMock()
        backend = NodriverCaptureBackend(browser)
        result = await backend.get_page_source()
        assert result is None

    @pytest.mark.asyncio
    async def test_start_capture_async_enables_network_and_runtime(self) -> None:
        browser = MagicMock()
        tab = MagicMock()
        tab.send = AsyncMock()
        tab.add_handler = MagicMock()
        backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)
        await backend.start_capture_async()
        # network.enable() and runtime.enable()
        assert tab.send.call_count == 2
        # RequestWillBeSent, ResponseReceived, LoadingFinished, ConsoleAPICalled
        assert tab.add_handler.call_count == 4

    @pytest.mark.asyncio
    async def test_start_capture_async_passes_buffer_params(self) -> None:
        """network.enable() is called with large buffer sizes to prevent body eviction."""
        with _patch_cdp_modules(include_runtime=True) as mock_network:
            browser = MagicMock()
            tab = MagicMock()
            tab.send = AsyncMock()
            tab.add_handler = MagicMock()
            backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)
            await backend.start_capture_async()

        mock_network.enable.assert_called_once_with(
            max_total_buffer_size=100 * 1024 * 1024,
            max_resource_buffer_size=10 * 1024 * 1024,
            enable_durable_messages=True,
        )

    def test_on_response_correlates_with_request_map(self) -> None:
        browser = MagicMock()
        backend = NodriverCaptureBackend(browser)

        # Create a mock ResponseReceived-like event
        mock_response = MagicMock()
        mock_response.url = "https://example.com/api"
        mock_response.status = 200
        mock_response.status_text = "OK"
        mock_response.mime_type = "application/json"
        mock_response.headers = {"Content-Type": "application/json"}

        mock_event = MagicMock()
        mock_event.response = mock_response
        mock_event.request_id = "req-1"

        backend._on_response(mock_event)
        entries = backend.get_har_entries()
        assert len(entries) == 1
        assert entries[0]["response"]["status"] == 200
        assert entries[0]["request"]["url"] == "https://example.com/api"

    def test_on_response_handles_exception_gracefully(self) -> None:
        browser = MagicMock()
        backend = NodriverCaptureBackend(browser)

        mock_event = MagicMock()
        mock_event.response = None

        # Should not raise
        backend._on_response(mock_event)
        # The entry is still added (with defaults) even if response is None
        # because MagicMock returns MagicMock for attribute access

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "tab_attr", "operation"),
        [
            ("take_screenshot", "save_screenshot", "screenshot"),
            ("get_page_source", "get_content", "page_source"),
        ],
    )
    async def test_browser_disconnect_warns_not_traces(
        self, method: str, tab_attr: str, operation: str
    ) -> None:
        """ConnectionError produces warning, not exception traceback."""
        browser = MagicMock()
        tab = MagicMock()
        setattr(tab, tab_attr, AsyncMock(side_effect=ConnectionRefusedError("[Errno 61]")))
        backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)

        with patch("graftpunk.observe.capture.LOG") as mock_log:
            result = await getattr(backend, method)()

        assert result is None
        mock_log.warning.assert_called_once_with(
            "nodriver_browser_disconnected", operation=operation
        )
        mock_log.exception.assert_not_called()


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


class TestCreateCaptureBackend:
    """Tests for create_capture_backend factory."""

    def test_creates_selenium_backend(self) -> None:
        driver = MagicMock()
        backend = create_capture_backend("selenium", driver)
        assert isinstance(backend, SeleniumCaptureBackend)

    def test_creates_nodriver_backend(self) -> None:
        browser = MagicMock()
        backend = create_capture_backend("nodriver", browser)
        assert isinstance(backend, NodriverCaptureBackend)

    def test_creates_nodriver_backend_with_get_tab(self) -> None:
        browser = MagicMock()
        tab = MagicMock()
        get_tab = lambda: tab  # noqa: E731
        backend = create_capture_backend("nodriver", browser, get_tab=get_tab)
        assert isinstance(backend, NodriverCaptureBackend)
        assert backend._tab is tab

    def test_get_tab_ignored_for_selenium(self) -> None:
        driver = MagicMock()
        backend = create_capture_backend("selenium", driver, get_tab=lambda: MagicMock())
        assert isinstance(backend, SeleniumCaptureBackend)

    def test_raises_for_unknown_backend(self) -> None:
        driver = MagicMock()
        with pytest.raises(ValueError, match="Unknown capture backend type"):
            create_capture_backend("unknown", driver)


# ---------------------------------------------------------------------------
# build_observe_context factory tests
# ---------------------------------------------------------------------------


class TestBuildObserveContext:
    """Tests for the build_observe_context factory function."""

    def test_off_mode_returns_noop_context(self) -> None:
        """Off mode returns a NoOpObservabilityContext regardless of other args."""
        from graftpunk.observe.context import build_observe_context

        ctx = build_observe_context(
            site_name="test",
            backend_type="selenium",
            driver=MagicMock(),
            mode="off",
        )
        assert isinstance(ctx, NoOpObservabilityContext)

    def test_full_mode_with_driver_creates_context_with_capture(self) -> None:
        """Full mode with a driver creates ObservabilityContext with capture and storage."""
        from graftpunk.observe.context import build_observe_context

        mock_driver = MagicMock()

        with (
            patch("graftpunk.observe.capture.create_capture_backend") as mock_create_capture,
            patch("graftpunk.observe.storage.ObserveStorage") as mock_storage_cls,
        ):
            mock_capture = MagicMock()
            mock_create_capture.return_value = mock_capture
            mock_storage = MagicMock()
            mock_storage_cls.return_value = mock_storage

            ctx = build_observe_context(
                site_name="mysite",
                backend_type="selenium",
                driver=mock_driver,
                mode="full",
            )

        assert isinstance(ctx, ObservabilityContext)
        assert not isinstance(ctx, NoOpObservabilityContext)
        assert ctx._capture is mock_capture
        assert ctx._storage is mock_storage
        assert ctx._mode == "full"
        mock_create_capture.assert_called_once_with(
            "selenium", mock_driver, bodies_dir=mock_storage.run_dir / "bodies"
        )

    def test_full_mode_without_driver_creates_context_without_capture(self) -> None:
        """Full mode with driver=None creates context without capture."""
        from graftpunk.observe.context import build_observe_context

        with (
            patch("graftpunk.observe.storage.ObserveStorage") as mock_storage_cls,
            patch("graftpunk.observe.context.LOG") as mock_log,
        ):
            mock_storage = MagicMock()
            mock_storage_cls.return_value = mock_storage

            ctx = build_observe_context(
                site_name="mysite",
                backend_type="selenium",
                driver=None,
                mode="full",
            )

        assert isinstance(ctx, ObservabilityContext)
        assert not isinstance(ctx, NoOpObservabilityContext)
        assert ctx._capture is None
        assert ctx._storage is mock_storage
        assert ctx._mode == "full"
        mock_log.warning.assert_called_once_with(
            "observe_capture_unavailable",
            site_name="mysite",
            reason="no browser driver",
        )

    def test_off_mode_does_not_create_bodies_dir(self) -> None:
        """Off mode returns NoOpObservabilityContext without creating bodies_dir."""
        from graftpunk.observe.context import build_observe_context

        ctx = build_observe_context(
            site_name="test",
            backend_type="selenium",
            driver=MagicMock(),
            mode="off",
        )
        assert isinstance(ctx, NoOpObservabilityContext)
        # No capture backend created, so no bodies_dir interaction
        assert ctx._capture is None

    def test_full_mode_passes_bodies_dir_to_capture_backend(self, tmp_path: Path) -> None:
        """Full mode passes bodies_dir derived from storage.run_dir to capture backend."""
        from graftpunk.observe.context import build_observe_context

        mock_driver = MagicMock()

        with (
            patch("graftpunk.observe.capture.create_capture_backend") as mock_create_capture,
            patch("graftpunk.observe.storage.ObserveStorage") as mock_storage_cls,
        ):
            mock_capture = MagicMock()
            mock_create_capture.return_value = mock_capture
            mock_storage = MagicMock()
            mock_storage.run_dir = tmp_path / "test-run"
            mock_storage_cls.return_value = mock_storage

            build_observe_context(
                site_name="mysite",
                backend_type="nodriver",
                driver=mock_driver,
                mode="full",
            )

        # Verify bodies_dir is derived from run_dir
        mock_create_capture.assert_called_once_with(
            "nodriver", mock_driver, bodies_dir=tmp_path / "test-run" / "bodies"
        )


# ---------------------------------------------------------------------------
# Screenshot label sanitization tests
# ---------------------------------------------------------------------------


class TestScreenshotLabelSanitization:
    """Tests that screenshot labels are sanitized to prevent path traversal."""

    def test_path_traversal_label_sanitized(self, tmp_path: Path) -> None:
        """Labels with ../ are sanitized so path traversal does not occur."""
        storage = ObserveStorage(tmp_path, "test", "run1")
        path = storage.save_screenshot(1, "../../etc/passwd", b"png_data")
        # File must be inside the screenshots directory (no traversal)
        assert path.parent == storage.run_dir / "screenshots"
        # Slashes are replaced with dashes; the file cannot escape
        assert "/" not in path.name
        assert path.exists()
        assert path.read_bytes() == b"png_data"

    def test_spaces_in_label_sanitized(self, tmp_path: Path) -> None:
        """Spaces in labels are replaced with dashes."""
        storage = ObserveStorage(tmp_path, "test", "run1")
        path = storage.save_screenshot(1, "my screenshot", b"png_data")
        assert " " not in path.name
        assert path.exists()
        assert path.read_bytes() == b"png_data"

    def test_slashes_in_label_sanitized(self, tmp_path: Path) -> None:
        """Slashes in labels are replaced with dashes."""
        storage = ObserveStorage(tmp_path, "test", "run1")
        path = storage.save_screenshot(1, "evil/path", b"png_data")
        assert "/" not in path.name
        assert path.parent == storage.run_dir / "screenshots"
        assert path.exists()
        assert path.read_bytes() == b"png_data"


# ---------------------------------------------------------------------------
# Shared HAR helper tests
# ---------------------------------------------------------------------------


class TestSharedHARHelpers:
    """Tests for shared MIME helper functions and HAR entry builder."""

    def test_is_binary_mime_image(self) -> None:
        assert _is_binary_mime("image/png") is True
        assert _is_binary_mime("Image/JPEG") is True

    def test_is_binary_mime_application_pdf(self) -> None:
        assert _is_binary_mime("application/pdf") is True

    def test_is_binary_mime_text_html_is_false(self) -> None:
        assert _is_binary_mime("text/html") is False

    def test_is_binary_mime_application_json_is_false(self) -> None:
        assert _is_binary_mime("application/json") is False

    def test_is_text_mime_json(self) -> None:
        assert _is_text_mime("application/json") is True

    def test_is_text_mime_html(self) -> None:
        assert _is_text_mime("text/html") is True

    def test_is_text_mime_javascript(self) -> None:
        assert _is_text_mime("application/javascript") is True

    def test_is_text_mime_image_is_false(self) -> None:
        assert _is_text_mime("image/png") is False

    def test_should_stream_to_disk_binary(self) -> None:
        assert _should_stream_to_disk("image/png", 100, MAX_RESPONSE_BODY_SIZE) is True

    def test_should_stream_to_disk_large_text(self) -> None:
        large = MAX_RESPONSE_BODY_SIZE + 1
        assert _should_stream_to_disk("text/html", large, MAX_RESPONSE_BODY_SIZE) is True

    def test_should_stream_to_disk_small_text(self) -> None:
        assert _should_stream_to_disk("text/html", 100, MAX_RESPONSE_BODY_SIZE) is False

    def test_should_stream_to_disk_unknown_mime(self) -> None:
        assert _should_stream_to_disk("application/x-unknown", 100, MAX_RESPONSE_BODY_SIZE) is False

    def test_mime_to_extension_known(self) -> None:
        assert _mime_to_extension("application/json") == ".json"
        assert _mime_to_extension("text/html") == ".html"
        assert _mime_to_extension("image/png") == ".png"

    def test_mime_to_extension_with_charset(self) -> None:
        assert _mime_to_extension("application/json; charset=utf-8") == ".json"

    def test_mime_to_extension_unknown(self) -> None:
        assert _mime_to_extension("application/x-custom") == ".bin"

    def test_wall_time_to_iso_with_value(self) -> None:
        result = _wall_time_to_iso(0.0)
        assert "1970-01-01" in result

    def test_wall_time_to_iso_none_returns_current(self) -> None:
        result = _wall_time_to_iso(None)
        # Should be a valid ISO string with current date
        assert "T" in result

    def test_build_har_entry_basic(self) -> None:
        data = {
            "url": "https://example.com/api",
            "method": "GET",
            "headers": {"Accept": "application/json"},
            "timestamp": 1700000000.0,
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": {"Content-Type": "application/json"},
                "mimeType": "application/json",
                "bodySize": 42,
                "body": '{"ok": true}',
            },
        }
        entry = _build_har_entry(data, "req-1")
        assert entry["request"]["method"] == "GET"
        assert entry["request"]["url"] == "https://example.com/api"
        assert entry["response"]["status"] == 200
        assert entry["response"]["content"]["text"] == '{"ok": true}'
        assert entry["response"]["content"]["size"] == 42

    def test_build_har_entry_with_post_data(self) -> None:
        data = {
            "url": "https://example.com/login",
            "method": "POST",
            "headers": {"Content-Type": "application/x-www-form-urlencoded"},
            "post_data": "user=foo&pass=bar",
            "timestamp": None,
        }
        entry = _build_har_entry(data, "req-2")
        assert entry["request"]["method"] == "POST"
        assert "postData" in entry["request"]
        assert entry["request"]["postData"]["text"] == "user=foo&pass=bar"

    def test_build_har_entry_no_response(self) -> None:
        data = {"url": "https://example.com", "method": "GET", "headers": {}}
        entry = _build_har_entry(data, "req-3")
        assert entry["response"]["status"] == 0

    def test_build_har_entry_body_file(self) -> None:
        data = {
            "url": "https://example.com/image.png",
            "method": "GET",
            "headers": {},
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": {},
                "mimeType": "image/png",
                "bodySize": 1024,
                "_bodyFile": "bodies/req-4.png",
            },
        }
        entry = _build_har_entry(data, "req-4")
        assert entry["response"]["content"]["_bodyFile"] == "bodies/req-4.png"
        assert entry["response"]["content"]["text"] is None


# ---------------------------------------------------------------------------
# Nodriver HAR capture tests
# ---------------------------------------------------------------------------


class TestNodriverHARCapture:
    """Tests for full network capture in NodriverCaptureBackend."""

    def test_on_request_stores_data(self) -> None:
        browser = MagicMock()
        backend = NodriverCaptureBackend(browser)

        event = MagicMock()
        event.request_id = "req-100"
        event.request.url = "https://example.com/api"
        event.request.method = "POST"
        event.request.headers = {"Content-Type": "application/json"}
        event.request.post_data = '{"key": "value"}'
        event.request.has_post_data = True
        event.wall_time = 1700000000.0

        backend._on_request(event)

        assert "req-100" in backend._request_map
        data = backend._request_map["req-100"]
        assert data["url"] == "https://example.com/api"
        assert data["method"] == "POST"
        assert data["post_data"] == '{"key": "value"}'

    def test_on_response_correlates_with_existing_request(self) -> None:
        browser = MagicMock()
        backend = NodriverCaptureBackend(browser)

        # First add a request
        req_event = MagicMock()
        req_event.request_id = "req-200"
        req_event.request.url = "https://example.com/data"
        req_event.request.method = "GET"
        req_event.request.headers = {}
        req_event.request.post_data = None
        req_event.request.has_post_data = False
        req_event.wall_time = 1700000000.0
        backend._on_request(req_event)

        # Then add response
        resp_event = MagicMock()
        resp_event.request_id = "req-200"
        resp_event.response.url = "https://example.com/data"
        resp_event.response.status = 200
        resp_event.response.status_text = "OK"
        resp_event.response.headers = {"Content-Type": "text/html"}
        resp_event.response.mime_type = "text/html"
        backend._on_response(resp_event)

        data = backend._request_map["req-200"]
        assert "response" in data
        assert data["response"]["status"] == 200
        assert data["response"]["mimeType"] == "text/html"

    def test_on_response_creates_entry_if_no_request(self) -> None:
        browser = MagicMock()
        backend = NodriverCaptureBackend(browser)

        resp_event = MagicMock()
        resp_event.request_id = "orphan-1"
        resp_event.response.url = "https://example.com/orphan"
        resp_event.response.status = 304
        resp_event.response.status_text = "Not Modified"
        resp_event.response.headers = {}
        resp_event.response.mime_type = "text/html"
        backend._on_response(resp_event)

        assert "orphan-1" in backend._request_map
        data = backend._request_map["orphan-1"]
        assert data["url"] == "https://example.com/orphan"
        assert data["response"]["status"] == 304

    def test_get_har_entries_produces_har_format(self) -> None:
        browser = MagicMock()
        backend = NodriverCaptureBackend(browser)

        # Simulate request + response
        req_event = MagicMock()
        req_event.request_id = "req-300"
        req_event.request.url = "https://example.com/page"
        req_event.request.method = "GET"
        req_event.request.headers = {"Accept": "text/html"}
        req_event.request.post_data = None
        req_event.request.has_post_data = False
        req_event.wall_time = 1700000000.0
        backend._on_request(req_event)

        resp_event = MagicMock()
        resp_event.request_id = "req-300"
        resp_event.response.url = "https://example.com/page"
        resp_event.response.status = 200
        resp_event.response.status_text = "OK"
        resp_event.response.headers = {"Content-Type": "text/html"}
        resp_event.response.mime_type = "text/html"
        backend._on_response(resp_event)

        entries = backend.get_har_entries()
        assert len(entries) == 1
        entry = entries[0]
        assert entry["request"]["method"] == "GET"
        assert entry["response"]["status"] == 200
        assert "startedDateTime" in entry

    @pytest.mark.asyncio
    async def test_stop_capture_async_fetches_text_body(self) -> None:
        browser = MagicMock()
        tab = MagicMock()
        tab.send = AsyncMock(return_value=('{"ok": true}', False))
        backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)

        # Seed a request with text response
        backend._request_map["req-500"] = {
            "url": "https://example.com/api",
            "method": "GET",
            "headers": {},
            "has_post_data": False,
            "post_data": None,
            "timestamp": None,
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": {},
                "mimeType": "application/json",
            },
        }
        await backend.stop_capture_async()
        resp = backend._request_map["req-500"]["response"]
        assert resp["body"] == '{"ok": true}'
        assert resp["bodySize"] == len(b'{"ok": true}')

    @pytest.mark.asyncio
    async def test_stop_capture_async_fetches_post_data(self) -> None:
        browser = MagicMock()
        tab = MagicMock()

        call_count = 0

        async def mock_send(cmd: Any) -> Any:
            nonlocal call_count
            call_count += 1
            # First call is get_request_post_data, second is get_response_body
            if call_count == 1:
                return "user=foo"
            return ("<html></html>", False)

        tab.send = mock_send
        backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)

        backend._request_map["req-600"] = {
            "url": "https://example.com/login",
            "method": "POST",
            "headers": {},
            "has_post_data": True,
            "post_data": None,
            "timestamp": None,
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": {},
                "mimeType": "text/html",
            },
        }
        await backend.stop_capture_async()
        # Post data should be fetched
        assert backend._request_map["req-600"]["post_data"] == "user=foo"

    @pytest.mark.asyncio
    async def test_stop_capture_async_streams_binary_to_disk(self, tmp_path: Path) -> None:
        browser = MagicMock()
        tab = MagicMock()
        bodies_dir = tmp_path / "bodies"

        import base64

        png_data = b"\x89PNG\r\n\x1a\nfake"
        encoded = base64.b64encode(png_data).decode()
        tab.send = AsyncMock(return_value=(encoded, True))

        backend = NodriverCaptureBackend(browser, get_tab=lambda: tab, bodies_dir=bodies_dir)
        backend._request_map["req-700"] = {
            "url": "https://example.com/image.png",
            "method": "GET",
            "headers": {},
            "has_post_data": False,
            "post_data": None,
            "timestamp": None,
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": {},
                "mimeType": "image/png",
            },
        }
        await backend.stop_capture_async()

        resp = backend._request_map["req-700"]["response"]
        assert resp.get("_bodyFile") == "bodies/req-700.png"
        assert (bodies_dir / "req-700.png").exists()
        assert (bodies_dir / "req-700.png").read_bytes() == png_data

    @pytest.mark.asyncio
    async def test_stop_capture_async_streams_large_text_to_disk(self, tmp_path: Path) -> None:
        browser = MagicMock()
        tab = MagicMock()
        bodies_dir = tmp_path / "bodies"
        max_size = 100  # Small max for testing

        large_body = "x" * 200
        tab.send = AsyncMock(return_value=(large_body, False))

        backend = NodriverCaptureBackend(
            browser,
            get_tab=lambda: tab,
            max_body_size=max_size,
            bodies_dir=bodies_dir,
        )
        backend._request_map["req-800"] = {
            "url": "https://example.com/large",
            "method": "GET",
            "headers": {},
            "has_post_data": False,
            "post_data": None,
            "timestamp": None,
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": {},
                "mimeType": "text/html",
            },
        }
        await backend.stop_capture_async()

        resp = backend._request_map["req-800"]["response"]
        assert resp.get("_bodyFile") == "bodies/req-800.html"
        assert (bodies_dir / "req-800.html").exists()

    @pytest.mark.asyncio
    async def test_stop_capture_async_returns_early_without_tab(self) -> None:
        browser = MagicMock()
        backend = NodriverCaptureBackend(browser)
        # Should not raise
        await backend.stop_capture_async()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc_cls,exc_msg",
        [
            (ConnectionRefusedError, "[Errno 61] Connection refused"),
            (ConnectionError, "browser died"),
            (ConnectionResetError, "[Errno 54] Connection reset by peer"),
            (BrokenPipeError, "[Errno 32] Broken pipe"),
        ],
    )
    async def test_stop_capture_async_bails_on_connection_death(
        self, exc_cls: type, exc_msg: str
    ) -> None:
        """stop_capture_async returns immediately on connection errors."""
        browser = MagicMock()
        tab = MagicMock()
        tab.send = AsyncMock(side_effect=exc_cls(exc_msg))

        with _patch_cdp_modules():
            backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)
            for i in range(3):
                backend._request_map[f"req-{i}"] = _make_request_entry(
                    url=f"https://example.com/page{i}",
                )
            await backend.stop_capture_async()

        # Should bail after first connection error, not try all 3
        assert tab.send.call_count == 1

    @pytest.mark.asyncio
    async def test_stop_capture_async_bails_on_post_data_connection_death(self) -> None:
        """stop_capture_async returns immediately on ConnectionError during post data fetch."""
        browser = MagicMock()
        tab = MagicMock()
        tab.send = AsyncMock(side_effect=ConnectionError("browser died"))

        with _patch_cdp_modules():
            backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)
            backend._request_map["req-post"] = _make_request_entry(
                url="https://example.com/login",
                method="POST",
                has_post_data=True,
            )
            backend._request_map["req-get"] = _make_request_entry()
            await backend.stop_capture_async()

        # Should bail after first post_data connection error
        assert tab.send.call_count == 1

    @pytest.mark.asyncio
    async def test_stop_capture_async_logs_phase_on_disconnect(self) -> None:
        """Disconnect warning includes phase kwarg for diagnostic context."""
        browser = MagicMock()
        tab = MagicMock()
        tab.send = AsyncMock(side_effect=ConnectionRefusedError("[Errno 61] Connection refused"))

        with _patch_cdp_modules(), patch("graftpunk.observe.capture.LOG") as mock_log:
            backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)
            backend._request_map["req-0"] = _make_request_entry()
            await backend.stop_capture_async()

        mock_log.warning.assert_called_once()
        call_kwargs = mock_log.warning.call_args[1]
        assert call_kwargs["phase"] == "response_body"
        assert call_kwargs["request_id"] == "req-0"
        assert "Connection refused" in call_kwargs["error"]
        assert call_kwargs["exc_type"] == "ConnectionRefusedError"

    @pytest.mark.asyncio
    async def test_stop_capture_async_does_not_bail_on_non_connection_oserror(self) -> None:
        """OSError subclasses like PermissionError don't trigger early bail-out."""
        browser = MagicMock()
        tab = MagicMock()
        # First call raises PermissionError (an OSError but not ConnectionError),
        # second call succeeds with a body
        tab.send = AsyncMock(
            side_effect=[
                PermissionError("Permission denied"),
                ('{"ok": true}', False),
            ]
        )

        with _patch_cdp_modules():
            backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)
            backend._request_map["req-0"] = _make_request_entry()
            backend._request_map["req-1"] = _make_request_entry(
                url="https://example.com/other",
            )
            await backend.stop_capture_async()

        # Both requests attempted — PermissionError doesn't trigger bail-out
        assert tab.send.call_count == 2

    def test_on_request_handles_exception_gracefully(self) -> None:
        browser = MagicMock()
        backend = NodriverCaptureBackend(browser)

        event = MagicMock()
        event.request_id = None  # Will cause str(None) = "None" but headers access fails
        event.request = None  # This will cause AttributeError

        # Should not raise
        backend._on_request(event)

    @pytest.mark.asyncio
    async def test_on_loading_finished_eagerly_fetches_body(self) -> None:
        """LoadingFinished handler fetches response body immediately."""
        browser = MagicMock()
        tab = MagicMock()
        tab.send = AsyncMock(return_value=('{"result": "ok"}', False))
        backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)

        # Seed request + response metadata
        backend._request_map["req-eager"] = {
            "url": "https://example.com/api",
            "method": "GET",
            "headers": {},
            "post_data": None,
            "has_post_data": False,
            "timestamp": None,
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": {},
                "mimeType": "application/json",
            },
        }

        event = MagicMock()
        event.request_id = "req-eager"
        await backend._on_loading_finished(event)

        assert "req-eager" in backend._bodies_fetched
        assert backend._request_map["req-eager"]["response"]["body"] == '{"result": "ok"}'

    @pytest.mark.asyncio
    async def test_stop_capture_skips_eagerly_fetched_bodies(self) -> None:
        """stop_capture_async skips bodies already fetched by LoadingFinished."""
        browser = MagicMock()
        tab = MagicMock()
        tab.send = AsyncMock(return_value=("unused", False))
        backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)

        # Seed a request that was already eagerly fetched
        backend._request_map["req-done"] = {
            "url": "https://example.com/api",
            "method": "GET",
            "headers": {},
            "post_data": None,
            "has_post_data": False,
            "timestamp": None,
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": {},
                "mimeType": "application/json",
                "content": {"text": "already fetched"},
            },
        }
        backend._bodies_fetched.add("req-done")

        await backend.stop_capture_async()

        # tab.send should NOT have been called (no post_data to fetch, body already done)
        tab.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_loading_finished_handles_failure_gracefully(self) -> None:
        """Eager fetch failure is logged but does not raise."""
        browser = MagicMock()
        tab = MagicMock()
        tab.send = AsyncMock(side_effect=RuntimeError("CDP -32000"))
        backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)

        backend._request_map["req-fail"] = {
            "url": "https://example.com/api",
            "method": "GET",
            "headers": {},
            "post_data": None,
            "has_post_data": False,
            "timestamp": None,
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": {},
                "mimeType": "application/json",
            },
        }

        event = MagicMock()
        event.request_id = "req-fail"
        # Should not raise
        await backend._on_loading_finished(event)
        assert "req-fail" not in backend._bodies_fetched

    @pytest.mark.asyncio
    async def test_on_loading_finished_logs_exception_details(self) -> None:
        """Eager fetch failure warning includes error message and exception type."""
        browser = MagicMock()
        tab = MagicMock()
        tab.send = AsyncMock(side_effect=RuntimeError("CDP -32000: no body available"))

        with _patch_cdp_modules(), patch("graftpunk.observe.capture.LOG") as mock_log:
            backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)
            backend._request_map["req-err"] = _make_request_entry()

            event = MagicMock()
            event.request_id = "req-err"
            await backend._on_loading_finished(event)

        mock_log.warning.assert_called_once_with(
            "nodriver_eager_body_fetch_failed",
            request_id="req-err",
            url="https://example.com/api",
            error="CDP -32000: no body available",
            exc_type="RuntimeError",
        )

    @pytest.mark.asyncio
    async def test_on_loading_finished_passes_is_update_true(self) -> None:
        """Eager fetch uses _is_update=True to skip _register_handlers() overhead."""
        browser = MagicMock()
        tab = MagicMock()
        tab.send = AsyncMock(return_value=('{"ok": true}', False))

        with _patch_cdp_modules():
            backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)
            backend._request_map["req-fast"] = _make_request_entry()

            event = MagicMock()
            event.request_id = "req-fast"
            await backend._on_loading_finished(event)

        # Verify _is_update=True was passed to tab.send()
        tab.send.assert_called_once()
        _, kwargs = tab.send.call_args
        assert kwargs.get("_is_update") is True


# ---------------------------------------------------------------------------
# Nodriver console capture tests
# ---------------------------------------------------------------------------


class TestNodriverConsoleCapture:
    """Tests for console log capture in NodriverCaptureBackend."""

    def test_on_console_stores_log_entry(self) -> None:
        browser = MagicMock()
        backend = NodriverCaptureBackend(browser)

        event = MagicMock()
        event.type_ = MagicMock()
        event.type_.value = "log"
        arg1 = MagicMock()
        arg1.value = "hello world"
        event.args = [arg1]
        event.timestamp = 1700000000.0

        backend._on_console(event)

        logs = backend.get_console_logs()
        assert len(logs) == 1
        assert logs[0]["level"] == "log"
        assert logs[0]["args"] == ["hello world"]
        assert logs[0]["timestamp"] == 1700000000.0

    def test_on_console_handles_missing_args(self) -> None:
        browser = MagicMock()
        backend = NodriverCaptureBackend(browser)

        event = MagicMock()
        event.type_ = MagicMock()
        event.type_.value = "error"
        event.args = None
        event.timestamp = None

        backend._on_console(event)

        logs = backend.get_console_logs()
        assert len(logs) == 1
        assert logs[0]["level"] == "error"
        assert logs[0]["args"] == []

    def test_on_console_handles_type_without_value_attr(self) -> None:
        """When type_ has no .value attribute, str(type_) is used."""
        browser = MagicMock()
        backend = NodriverCaptureBackend(browser)

        event = MagicMock()
        # Use a simple string-like object that has no .value
        event.type_ = "warning"
        arg1 = MagicMock()
        arg1.value = "test"
        event.args = [arg1]
        event.timestamp = 1700000000.0

        backend._on_console(event)

        logs = backend.get_console_logs()
        assert len(logs) == 1
        # str("warning") -> "warning", hasattr(str, "value") is False
        assert logs[0]["level"] == "warning"

    @pytest.mark.asyncio
    async def test_start_capture_async_enables_runtime(self) -> None:
        browser = MagicMock()
        tab = MagicMock()
        tab.send = AsyncMock()
        tab.add_handler = MagicMock()
        backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)
        await backend.start_capture_async()
        # Verify runtime.enable() was called (second send call)
        assert tab.send.call_count == 2

    @pytest.mark.asyncio
    async def test_stop_capture_binary_without_bodies_dir(self) -> None:
        """Binary MIME with no bodies_dir: body is fetched but not stored."""
        browser = MagicMock()
        tab = MagicMock()

        import base64

        png_data = b"\x89PNG\r\n\x1a\nfake"
        encoded = base64.b64encode(png_data).decode()
        tab.send = AsyncMock(return_value=(encoded, True))

        backend = NodriverCaptureBackend(browser, get_tab=lambda: tab, bodies_dir=None)
        backend._request_map["r1"] = {
            "url": "https://example.com/image.png",
            "method": "GET",
            "headers": {},
            "has_post_data": False,
            "post_data": None,
            "timestamp": None,
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": {},
                "mimeType": "image/png",
            },
        }

        await backend.stop_capture_async()

        # Body was fetched but not stored (no bodies_dir, not text)
        response = backend._request_map["r1"]["response"]
        assert response.get("body") is None
        assert response.get("_bodyFile") is None
