"""Raw headers via *ExtraInfo events (#157) and console timestamp units (#158).

Chromium delivers only filtered headers on ``Network.responseReceived`` /
``redirectResponse``; ``Set-Cookie`` (and the raw ``Cookie`` request header)
arrive on ``Network.responseReceivedExtraInfo`` / ``requestWillBeSentExtraInfo``.
``Runtime.consoleAPICalled.timestamp`` is milliseconds since epoch.
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import MagicMock

from graftpunk.observe.capture import (
    NodriverCaptureBackend,
    SeleniumCaptureBackend,
    _build_har_entry,
    _console_timestamp,
)

LOGIN = "https://shop.example.com/login"
HOME = "https://shop.example.com/"


# ---------------------------------------------------------------------------
# nodriver fakes
# ---------------------------------------------------------------------------


def _request(url: str, method: str = "GET") -> MagicMock:
    r = MagicMock()
    r.url, r.method, r.headers, r.post_data, r.has_post_data = (
        url,
        method,
        {"Accept": "text/html"},
        None,
        False,
    )
    return r


def _response(url: str, status: int, headers: dict[str, str] | None = None) -> MagicMock:
    r = MagicMock()
    r.url, r.status, r.status_text, r.headers, r.mime_type = (
        url,
        status,
        "",
        headers or {},
        "text/html",
    )
    return r


def _will_be_sent(
    rid: str, request: MagicMock, redirect: MagicMock | None = None, wall: float = 1.0
) -> MagicMock:
    ev = MagicMock()
    ev.request_id, ev.request, ev.redirect_response, ev.wall_time = rid, request, redirect, wall
    return ev


def _extra_request(rid: str, headers: dict[str, str]) -> MagicMock:
    ev = MagicMock()
    ev.request_id, ev.headers = rid, headers
    return ev


def _extra_response(rid: str, headers: dict[str, str], status_code: int = 200) -> MagicMock:
    ev = MagicMock()
    ev.request_id, ev.headers, ev.status_code = rid, headers, status_code
    return ev


def _received(rid: str, response: MagicMock) -> MagicMock:
    ev = MagicMock()
    ev.request_id, ev.response = rid, response
    return ev


class TestNodriverExtraInfo:
    def test_set_cookie_from_response_extra_info_lands_in_har(self) -> None:
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r1", _request(HOME)))
        backend._on_response_extra_info(
            _extra_response("r1", {"set-cookie": "session=abc; Path=/; HttpOnly"})
        )
        backend._on_response(_received("r1", _response(HOME, 200, {"Content-Type": "text/html"})))

        (entry,) = backend.get_har_entries()
        headers = {h["name"].lower(): h["value"] for h in entry["response"]["headers"]}
        assert headers["set-cookie"] == "session=abc; Path=/; HttpOnly"
        assert headers["content-type"] == "text/html"  # filtered headers kept too
        assert entry["response"]["cookies"] == [{"name": "session", "value": "abc"}]

    def test_extra_info_after_response_received_also_works(self) -> None:
        """CDP documents ExtraInfo may fire before OR after responseReceived."""
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r2", _request(HOME)))
        backend._on_response(_received("r2", _response(HOME, 200)))
        backend._on_response_extra_info(_extra_response("r2", {"Set-Cookie": "a=1"}))

        (entry,) = backend.get_har_entries()
        assert entry["response"]["cookies"] == [{"name": "a", "value": "1"}]

    def test_multiple_set_cookie_lines(self) -> None:
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r3", _request(HOME)))
        backend._on_response_extra_info(
            _extra_response("r3", {"set-cookie": "a=1; Path=/\nb=2; Secure"})
        )
        backend._on_response(_received("r3", _response(HOME, 200)))

        (entry,) = backend.get_har_entries()
        assert entry["response"]["cookies"] == [
            {"name": "a", "value": "1"},
            {"name": "b", "value": "2"},
        ]

    def test_raw_cookie_request_header_and_request_cookies(self) -> None:
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r4", _request(HOME)))
        backend._on_request_extra_info(
            _extra_request("r4", {"cookie": "session=abc; theme=dark", "accept": "text/html"})
        )

        (entry,) = backend.get_har_entries()
        headers = {h["name"].lower(): h["value"] for h in entry["request"]["headers"]}
        assert headers["cookie"] == "session=abc; theme=dark"
        assert entry["request"]["cookies"] == [
            {"name": "session", "value": "abc"},
            {"name": "theme", "value": "dark"},
        ]

    def test_redirect_hop_keeps_its_own_set_cookie(self) -> None:
        """The 302's ExtraInfo arrives before the continuation's requestWillBeSent;
        it must stay on the hop, and the successor's ExtraInfo on the successor."""
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r5", _request(LOGIN, "POST"), wall=1.0))
        backend._on_response_extra_info(
            _extra_response("r5", {"set-cookie": "session=minted"}, 302)
        )
        backend._on_request(
            _will_be_sent("r5", _request(HOME), _response(LOGIN, 302, {"Location": "/"}), wall=2.0)
        )
        backend._on_response_extra_info(_extra_response("r5", {"set-cookie": "seen=1"}))
        backend._on_response(_received("r5", _response(HOME, 200)))

        hop, final = backend.get_har_entries()
        assert hop["response"]["status"] == 302
        assert hop["response"]["cookies"] == [{"name": "session", "value": "minted"}]
        assert final["response"]["cookies"] == [{"name": "seen", "value": "1"}]

    def test_extra_info_for_unknown_request_is_ignored(self) -> None:
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_response_extra_info(_extra_response("ghost", {"set-cookie": "x=1"}))
        backend._on_request_extra_info(_extra_request("ghost", {"cookie": "x=1"}))
        assert backend.get_har_entries() == []

    def test_start_capture_registers_extra_info_handlers(self) -> None:
        import asyncio

        tab = MagicMock()

        async def send(*a: Any, **k: Any) -> None:
            return None

        tab.send = send
        backend = NodriverCaptureBackend(MagicMock(), get_tab=lambda: tab)
        asyncio.run(backend.start_capture_async())
        registered = {c.args[1].__name__ for c in tab.add_handler.call_args_list}
        assert {"_on_request_extra_info", "_on_response_extra_info"} <= registered


# ---------------------------------------------------------------------------
# selenium (performance log)
# ---------------------------------------------------------------------------


def _perf(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "message": json.dumps({"message": {"method": method, "params": params}}),
        "level": "INFO",
    }


def _driver(perf: list[dict[str, Any]]) -> MagicMock:
    driver = MagicMock()
    driver.get_log = MagicMock(side_effect=lambda kind: perf if kind == "performance" else [])
    driver.execute_cdp_cmd = MagicMock(return_value={"body": "", "base64Encoded": False})
    return driver


class TestSeleniumExtraInfo:
    def test_set_cookie_and_cookie_headers_land_in_har(self) -> None:
        perf = [
            _perf(
                "Network.requestWillBeSent",
                {
                    "requestId": "s1",
                    "request": {"url": HOME, "method": "GET", "headers": {}},
                    "wallTime": 1.0,
                },
            ),
            _perf(
                "Network.requestWillBeSentExtraInfo",
                {"requestId": "s1", "headers": {"cookie": "session=abc"}},
            ),
            _perf(
                "Network.responseReceivedExtraInfo",
                {
                    "requestId": "s1",
                    "headers": {"set-cookie": "session=new; Path=/"},
                    "statusCode": 200,
                },
            ),
            _perf(
                "Network.responseReceived",
                {
                    "requestId": "s1",
                    "response": {
                        "url": HOME,
                        "status": 200,
                        "headers": {"Content-Type": "text/html"},
                        "mimeType": "text/html",
                    },
                },
            ),
        ]
        backend = SeleniumCaptureBackend(_driver(perf))
        backend.stop_capture()

        (entry,) = backend.get_har_entries()
        assert entry["request"]["cookies"] == [{"name": "session", "value": "abc"}]
        assert entry["response"]["cookies"] == [{"name": "session", "value": "new"}]
        assert {h["name"].lower() for h in entry["response"]["headers"]} >= {
            "set-cookie",
            "content-type",
        }

    def test_redirect_hop_keeps_its_own_set_cookie(self) -> None:
        perf = [
            _perf(
                "Network.requestWillBeSent",
                {
                    "requestId": "s2",
                    "request": {
                        "url": LOGIN,
                        "method": "POST",
                        "headers": {},
                        "postData": "u=x",
                        "hasPostData": True,
                    },
                    "wallTime": 1.0,
                },
            ),
            _perf(
                "Network.responseReceivedExtraInfo",
                {"requestId": "s2", "headers": {"set-cookie": "session=minted"}, "statusCode": 302},
            ),
            _perf(
                "Network.requestWillBeSent",
                {
                    "requestId": "s2",
                    "request": {"url": HOME, "method": "GET", "headers": {}},
                    "redirectResponse": {"url": LOGIN, "status": 302, "headers": {"Location": "/"}},
                    "wallTime": 2.0,
                },
            ),
            _perf(
                "Network.responseReceived",
                {
                    "requestId": "s2",
                    "response": {"url": HOME, "status": 200, "headers": {}, "mimeType": ""},
                },
            ),
        ]
        backend = SeleniumCaptureBackend(_driver(perf))
        backend.stop_capture()

        hop, final = backend.get_har_entries()
        assert hop["response"]["cookies"] == [{"name": "session", "value": "minted"}]
        assert final["response"]["cookies"] == []


# ---------------------------------------------------------------------------
# HAR builder cookie parsing
# ---------------------------------------------------------------------------


class TestHarCookies:
    def test_malformed_cookie_fragments_are_skipped(self) -> None:
        entry = _build_har_entry(
            {
                "url": HOME,
                "method": "GET",
                "headers": {"Cookie": "good=1; ; novalue; =empty"},
                "response": {"status": 200},
                "_response_extra_headers": {"Set-Cookie": "ok=2; Path=/\n\nbroken"},
            }
        )
        assert entry["request"]["cookies"] == [{"name": "good", "value": "1"}]
        assert entry["response"]["cookies"] == [{"name": "ok", "value": "2"}]

    def test_extra_headers_override_filtered_ones_of_same_name(self) -> None:
        entry = _build_har_entry(
            {
                "url": HOME,
                "method": "GET",
                "headers": {},
                "response": {"status": 200, "headers": {"content-length": "1"}},
                "_response_extra_headers": {"content-length": "2"},
            }
        )
        assert [
            h["value"] for h in entry["response"]["headers"] if h["name"] == "content-length"
        ] == ["2"]


# ---------------------------------------------------------------------------
# console timestamps (#158)
# ---------------------------------------------------------------------------


class TestConsoleTimestamp:
    def test_cdp_milliseconds_become_seconds(self) -> None:
        assert _console_timestamp(1700000000000.0) == 1700000000.0

    def test_missing_falls_back_to_now(self) -> None:
        before = time.time()
        assert before <= _console_timestamp(None) <= time.time()

    def test_nodriver_console_event_normalised(self) -> None:
        backend = NodriverCaptureBackend(MagicMock())
        event = MagicMock()
        event.type_ = "log"
        event.args = []
        event.timestamp = 1700000000000.0
        backend._on_console(event)
        assert backend.get_console_logs()[0]["timestamp"] == 1700000000.0

    def test_selenium_console_event_normalised(self) -> None:
        perf = [
            _perf(
                "Runtime.consoleAPICalled",
                {"type": "log", "args": [], "timestamp": 1700000000000.0},
            )
        ]
        backend = SeleniumCaptureBackend(_driver(perf))
        backend.stop_capture()
        assert backend.get_console_logs()[0]["timestamp"] == 1700000000.0
