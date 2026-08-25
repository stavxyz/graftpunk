"""Redirect chains produce one HAR entry per hop (issue #153).

CDP reuses ``requestId`` across a redirect chain and announces each hop as a
new ``Network.requestWillBeSent`` carrying ``redirectResponse`` for the hop
it replaces. Keeping only the final request dropped the first hop -- for a
server-rendered login that is the ``POST /login`` itself.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from graftpunk.observe.capture import (
    NodriverCaptureBackend,
    SeleniumCaptureBackend,
    _build_har_entry,
)

LOGIN = "https://shop.example.com/login"
HOME = "https://shop.example.com/"
DASH = "https://shop.example.com/dashboard"


# ---------------------------------------------------------------------------
# nodriver event fakes
# ---------------------------------------------------------------------------


def _request(url: str, method: str = "GET", post_data: str | None = None) -> MagicMock:
    r = MagicMock()
    r.url = url
    r.method = method
    r.headers = {"Content-Type": "application/x-www-form-urlencoded"} if post_data else {}
    r.post_data = post_data
    r.has_post_data = post_data is not None
    return r


def _response(
    url: str, status: int, headers: dict[str, str] | None = None, mime: str = ""
) -> MagicMock:
    r = MagicMock()
    r.url = url
    r.status = status
    r.status_text = {302: "Found", 200: "OK"}.get(status, "")
    r.headers = headers or {}
    r.mime_type = mime
    return r


def _will_be_sent(
    rid: str, request: MagicMock, redirect_response: MagicMock | None = None
) -> MagicMock:
    ev = MagicMock()
    ev.request_id = rid
    ev.request = request
    ev.redirect_response = redirect_response
    ev.wall_time = 1700000000.0
    return ev


def _response_received(rid: str, response: MagicMock) -> MagicMock:
    ev = MagicMock()
    ev.request_id = rid
    ev.response = response
    return ev


def _nodriver_login_chain(backend: NodriverCaptureBackend) -> None:
    """POST /login -> 302 -> GET / (200), all under request id 'r1'."""
    backend._on_request(_will_be_sent("r1", _request(LOGIN, "POST", "u=alice&p=hunter2")))
    backend._on_request(
        _will_be_sent(
            "r1",
            _request(HOME),
            redirect_response=_response(LOGIN, 302, {"Location": "/"}, mime="text/html"),
        )
    )
    backend._on_response(_response_received("r1", _response(HOME, 200, mime="text/html")))


class TestNodriverRedirectHops:
    def test_login_post_survives_the_redirect(self) -> None:
        backend = NodriverCaptureBackend(MagicMock())
        _nodriver_login_chain(backend)

        entries = backend.get_har_entries()
        assert [(e["request"]["method"], e["request"]["url"]) for e in entries] == [
            ("POST", LOGIN),
            ("GET", HOME),
        ]
        post, final = entries
        assert post["response"]["status"] == 302
        assert post["response"]["redirectURL"] == HOME
        assert {h["name"]: h["value"] for h in post["response"]["headers"]}["Location"] == "/"
        assert post["request"]["postData"]["text"] == "u=alice&p=hunter2"
        assert final["response"]["status"] == 200
        assert final["response"]["redirectURL"] == ""

    def test_multi_hop_chain_yields_one_entry_per_hop(self) -> None:
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r2", _request(LOGIN, "POST", "x=1")))
        backend._on_request(
            _will_be_sent("r2", _request(HOME), _response(LOGIN, 302, {"Location": "/"}))
        )
        backend._on_request(
            _will_be_sent("r2", _request(DASH), _response(HOME, 302, {"Location": "/dashboard"}))
        )
        backend._on_response(_response_received("r2", _response(DASH, 200)))

        urls = [
            (e["request"]["url"], e["response"]["status"], e["response"]["redirectURL"])
            for e in backend.get_har_entries()
        ]
        assert urls == [(LOGIN, 302, HOME), (HOME, 302, DASH), (DASH, 200, "")]

    def test_entries_are_chronological_even_with_a_concurrent_request(self) -> None:
        """The finalized hop is re-inserted at the end of the map; HAR output must
        still be ordered by start time so readers see POST before GET."""
        backend = NodriverCaptureBackend(MagicMock())
        post = _will_be_sent("r4", _request(LOGIN, "POST", "x=1"))
        post.wall_time = 100.0
        backend._on_request(post)
        asset = _will_be_sent("r5", _request(HOME + "static/a.css"))
        asset.wall_time = 100.5
        backend._on_request(asset)
        hop = _will_be_sent("r4", _request(HOME), _response(LOGIN, 302))
        hop.wall_time = 101.0
        backend._on_request(hop)

        assert [e["request"]["url"] for e in backend.get_har_entries()] == [
            LOGIN,
            HOME + "static/a.css",
            HOME,
        ]

    def test_redirect_without_prior_request_is_not_invented(self) -> None:
        """A redirect continuation whose first hop was never seen (capture started
        mid-chain) records only the request it announces."""
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r3", _request(HOME), _response(LOGIN, 302)))

        entries = backend.get_har_entries()
        assert [e["request"]["url"] for e in entries] == [HOME]

    @pytest.mark.asyncio
    async def test_stop_capture_does_not_fetch_bodies_for_redirect_hops(self) -> None:
        """A 302 has no body and the hop's synthetic key is not a CDP request id.

        The hop's 302 is text/html (as Chromium reports it), so without the
        skip the late fetch would try getResponseBody under the hop key."""
        tab = MagicMock()
        tab.send = AsyncMock(return_value=("<html/>", False))
        backend = NodriverCaptureBackend(MagicMock(), get_tab=lambda: tab)
        _nodriver_login_chain(backend)

        await backend.stop_capture_async()

        # Only the final GET / body is fetched; the POST hop is skipped.
        assert tab.send.await_count == 1
        entries = backend.get_har_entries()
        assert entries[0]["response"]["content"]["text"] is None
        assert entries[1]["response"]["content"]["text"] == "<html/>"

    @pytest.mark.asyncio
    async def test_hop_post_body_not_inlined_is_not_fetched_by_the_real_id(self) -> None:
        """After the redirect, getRequestPostData(id) refers to the final hop; the
        hop's body is lost and must be reported, not fetched under the wrong id."""
        tab = MagicMock()
        tab.send = AsyncMock(return_value="u=wrong-hop")
        backend = NodriverCaptureBackend(MagicMock(), get_tab=lambda: tab)
        big = _request(LOGIN, "POST")
        big.post_data = None
        big.has_post_data = True
        backend._on_request(_will_be_sent("r9", big))
        backend._on_request(
            _will_be_sent("r9", _request(HOME), _response(LOGIN, 302, {"Location": "/"}))
        )
        backend._on_response(_response_received("r9", _response(HOME, 200)))

        with patch("graftpunk.observe.capture.LOG") as mock_log:
            await backend.stop_capture_async()

        assert tab.send.await_count == 0
        events = [c.args[0] for c in mock_log.warning.call_args_list]
        assert "redirect_hop_post_data_unavailable" in events
        assert backend.get_har_entries()[0]["request"].get("postData") is None

    @pytest.mark.asyncio
    async def test_eager_fetch_ignores_hop_keys(self) -> None:
        """LoadingFinished arrives with the real request id; the hop entry must not
        be mistaken for it or fetched."""
        tab = MagicMock()
        tab.send = AsyncMock(return_value=("<html/>", False))
        backend = NodriverCaptureBackend(MagicMock(), get_tab=lambda: tab)
        _nodriver_login_chain(backend)

        ev = MagicMock()
        ev.request_id = "r1"
        await backend._on_loading_finished(ev)

        entries = backend.get_har_entries()
        assert entries[1]["response"]["content"]["text"] == "<html/>"
        assert entries[0]["response"]["content"]["text"] is None


# ---------------------------------------------------------------------------
# selenium (performance log)
# ---------------------------------------------------------------------------


def _perf(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "message": json.dumps({"message": {"method": method, "params": params}}),
        "level": "INFO",
    }


def _selenium_login_chain() -> list[dict[str, Any]]:
    return [
        _perf(
            "Network.requestWillBeSent",
            {
                "requestId": "s1",
                "request": {
                    "url": LOGIN,
                    "method": "POST",
                    "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                    "postData": "u=alice&p=hunter2",
                    "hasPostData": True,
                },
                "wallTime": 1700000000.0,
            },
        ),
        _perf(
            "Network.requestWillBeSent",
            {
                "requestId": "s1",
                "request": {"url": HOME, "method": "GET", "headers": {}, "hasPostData": False},
                "redirectResponse": {
                    "url": LOGIN,
                    "status": 302,
                    "statusText": "Found",
                    "headers": {"Location": "/"},
                    "mimeType": "text/html",
                },
                "wallTime": 1700000000.5,
            },
        ),
        _perf(
            "Network.responseReceived",
            {
                "requestId": "s1",
                "response": {
                    "url": HOME,
                    "status": 200,
                    "statusText": "OK",
                    "headers": {},
                    "mimeType": "text/html",
                },
            },
        ),
    ]


class TestSeleniumRedirectHops:
    def _driver(self) -> MagicMock:
        driver = MagicMock()
        driver.get_log = MagicMock(
            side_effect=lambda kind: _selenium_login_chain() if kind == "performance" else []
        )
        driver.execute_cdp_cmd = MagicMock(return_value={"body": "<html/>", "base64Encoded": False})
        return driver

    def test_login_post_survives_the_redirect(self) -> None:
        driver = self._driver()
        backend = SeleniumCaptureBackend(driver)
        backend.stop_capture()

        entries = backend.get_har_entries()
        assert [
            (e["request"]["method"], e["request"]["url"], e["response"]["status"]) for e in entries
        ] == [
            ("POST", LOGIN, 302),
            ("GET", HOME, 200),
        ]
        assert entries[0]["response"]["redirectURL"] == HOME
        assert entries[0]["request"]["postData"]["text"] == "u=alice&p=hunter2"

    def test_no_body_fetch_for_redirect_hop(self) -> None:
        driver = self._driver()
        backend = SeleniumCaptureBackend(driver)
        backend.stop_capture()

        fetched = [
            c.args[1]["requestId"]
            for c in driver.execute_cdp_cmd.call_args_list
            if c.args[0] == "Network.getResponseBody"
        ]
        assert fetched == ["s1"]


# ---------------------------------------------------------------------------
# HAR builder
# ---------------------------------------------------------------------------


class TestHarEntryRedirectUrl:
    def test_redirect_url_present_and_empty_by_default(self) -> None:
        entry = _build_har_entry(
            {"url": HOME, "method": "GET", "headers": {}, "response": {"status": 200}}, "x"
        )
        assert entry["response"]["redirectURL"] == ""

    def test_redirect_url_carried_from_response(self) -> None:
        entry = _build_har_entry(
            {
                "url": LOGIN,
                "method": "POST",
                "headers": {},
                "response": {"status": 302, "redirectURL": HOME},
            },
            "x",
        )
        assert entry["response"]["redirectURL"] == HOME
