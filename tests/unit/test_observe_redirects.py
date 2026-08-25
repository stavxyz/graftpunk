"""Redirect chains produce one HAR entry per hop (issue #153).

CDP reuses ``requestId`` across a redirect chain and announces each hop as a
new ``Network.requestWillBeSent`` carrying ``redirectResponse`` for the hop
it replaces. Keeping only the final request dropped the first hop -- for a
server-rendered login that is the ``POST /login`` itself.
"""

from __future__ import annotations

import dataclasses
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


_CLOCK = {"t": 1700000000.0}


def _will_be_sent(
    rid: str, request: MagicMock, redirect_response: MagicMock | None = None
) -> MagicMock:
    """Each event gets a later wall time than the previous one, so ordering
    assertions exercise the sort rather than map insertion order."""
    ev = MagicMock()
    ev.request_id = rid
    ev.request = request
    ev.redirect_response = redirect_response
    _CLOCK["t"] += 0.5
    ev.wall_time = _CLOCK["t"]
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


def _selenium_login_chain(*, inline_post_data: bool = True) -> list[dict[str, Any]]:
    first_request: dict[str, Any] = {
        "url": LOGIN,
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "hasPostData": True,
    }
    if inline_post_data:
        first_request["postData"] = "u=alice&p=hunter2"
    return [
        _perf(
            "Network.requestWillBeSent",
            {"requestId": "s1", "request": first_request, "wallTime": 1700000000.0},
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


def _sel_request(
    rid: str,
    url: str,
    method: str = "GET",
    *,
    post: str | None = None,
    redirect: dict[str, Any] | None = None,
    wall: float = 1.0,
) -> dict[str, Any]:
    request: dict[str, Any] = {"url": url, "method": method, "headers": {}}
    if post is not None:
        request.update(postData=post, hasPostData=True)
    params: dict[str, Any] = {"requestId": rid, "request": request, "wallTime": wall}
    if redirect is not None:
        params["redirectResponse"] = redirect
    return _perf("Network.requestWillBeSent", params)


class TestSeleniumRedirectHops:
    def _driver(self, perf: list[dict[str, Any]] | None = None) -> MagicMock:
        chain = perf if perf is not None else _selenium_login_chain()
        driver = MagicMock()
        driver.get_log = MagicMock(side_effect=lambda kind: chain if kind == "performance" else [])
        driver.execute_cdp_cmd = MagicMock(return_value={"body": "<html/>", "base64Encoded": False})
        return driver

    def test_hop_post_body_not_inlined_warns_and_is_not_fetched(self) -> None:
        """Mirror of the nodriver case: the hop's body is gone; never ask for it by
        the real id (which now addresses the final hop)."""
        driver = self._driver(_selenium_login_chain(inline_post_data=False))
        backend = SeleniumCaptureBackend(driver)

        with patch("graftpunk.observe.capture.LOG") as mock_log:
            backend.stop_capture()

        post_fetches = [
            c
            for c in driver.execute_cdp_cmd.call_args_list
            if c.args[0] == "Network.getRequestPostData"
        ]
        assert post_fetches == []
        assert "redirect_hop_post_data_unavailable" in [
            c.args[0] for c in mock_log.warning.call_args_list
        ]
        assert backend.get_har_entries()[0]["request"].get("postData") is None

    def test_multi_hop_chain_yields_one_entry_per_hop(self) -> None:
        perf = [
            _sel_request("s2", LOGIN, "POST", post="x=1", wall=1.0),
            _sel_request(
                "s2",
                HOME,
                redirect={"url": LOGIN, "status": 302, "headers": {"Location": "/"}},
                wall=2.0,
            ),
            _sel_request(
                "s2",
                DASH,
                redirect={"url": HOME, "status": 302, "headers": {"Location": "/dashboard"}},
                wall=3.0,
            ),
            _perf(
                "Network.responseReceived",
                {
                    "requestId": "s2",
                    "response": {"url": DASH, "status": 200, "headers": {}, "mimeType": ""},
                },
            ),
        ]
        backend = SeleniumCaptureBackend(self._driver(perf))
        backend.stop_capture()

        hops = [
            (e["request"]["url"], e["response"]["status"], e["response"]["redirectURL"])
            for e in backend.get_har_entries()
        ]
        assert hops == [(LOGIN, 302, HOME), (HOME, 302, DASH), (DASH, 200, "")]

    def test_entries_are_chronological_even_with_a_concurrent_request(self) -> None:
        perf = [
            _sel_request("s3", LOGIN, "POST", post="x=1", wall=100.0),
            _sel_request("s4", HOME + "static/a.css", wall=100.5),
            _sel_request(
                "s3", HOME, redirect={"url": LOGIN, "status": 302, "headers": {}}, wall=101.0
            ),
        ]
        backend = SeleniumCaptureBackend(self._driver(perf))
        backend.stop_capture()

        assert [e["request"]["url"] for e in backend.get_har_entries()] == [
            LOGIN,
            HOME + "static/a.css",
            HOME,
        ]

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
# ordering + fidelity
# ---------------------------------------------------------------------------


class TestOrdering:
    def test_entries_without_wall_time_sort_last(self) -> None:
        """An entry with no wall time renders startedDateTime as 'now'; it must not
        lead the HAR -- detect_auth_flow reads list order as chronology."""
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_response(_response_received("orphan", _response(HOME + "late", 200)))
        first = _will_be_sent("r6", _request(LOGIN, "POST", "x=1"))
        first.wall_time = 10.0
        backend._on_request(first)
        backend._request_map["orphan"]["timestamp"] = None  # legacy/no-time entry

        assert [e["request"]["url"] for e in backend.get_har_entries()] == [LOGIN, HOME + "late"]

    def test_orphan_entry_gets_a_real_timestamp(self) -> None:
        """A response for a never-seen request is stamped when it is seen, so its
        order and its startedDateTime agree by construction."""
        import time

        backend = NodriverCaptureBackend(MagicMock())
        earlier = _will_be_sent("r7", _request(LOGIN, "POST", "x=1"))
        earlier.wall_time = 1700000000.0
        backend._on_request(earlier)
        before = time.time()
        backend._on_response(_response_received("orphan2", _response(HOME, 200)))
        stamp = backend._request_map["orphan2"]["timestamp"]
        assert before <= stamp <= time.time()
        assert [e["request"]["url"] for e in backend.get_har_entries()] == [LOGIN, HOME]

    def test_selenium_orphan_entry_is_stamped_and_sorts_last(self) -> None:
        perf = [
            _perf(
                "Network.responseReceived",
                {
                    "requestId": "s9",
                    "response": {
                        "url": HOME + "late",
                        "status": 200,
                        "headers": {},
                        "mimeType": "",
                    },
                },
            ),
            _sel_request("s8", LOGIN, "POST", post="x=1", wall=1700000000.0),
        ]
        driver = MagicMock()
        driver.get_log = MagicMock(side_effect=lambda kind: perf if kind == "performance" else [])
        driver.execute_cdp_cmd = MagicMock(return_value={"body": "", "base64Encoded": False})
        backend = SeleniumCaptureBackend(driver)
        backend.stop_capture()

        assert isinstance(backend._request_map["s9"]["timestamp"], float)
        assert [e["request"]["url"] for e in backend.get_har_entries()] == [LOGIN, HOME + "late"]

    def test_header_roles_see_the_same_order_as_the_har(self) -> None:
        """The POST hop is re-inserted at the end of the map; header-role
        extraction must still see it before the request it redirected to."""
        backend = NodriverCaptureBackend(MagicMock())
        _nodriver_login_chain(backend)
        seen: list[str] = []
        with patch(
            "graftpunk.observe.headers.extract_header_roles",
            side_effect=lambda m: seen.extend(d["url"] for d in m.values()) or {},
        ):
            backend.get_header_roles()
        assert seen == [LOGIN, HOME]

    def test_header_roles_include_the_post_hop_headers(self) -> None:
        """Real extract_header_roles: the POST hop's request headers were previously
        invisible; now the hop is part of the map role extraction sees."""
        backend = NodriverCaptureBackend(MagicMock())
        post = _request(LOGIN, "POST", "u=alice")
        post.headers = {"Content-Type": "application/x-www-form-urlencoded", "X-Shop-Client": "web"}
        backend._on_request(_will_be_sent("r10", post))
        backend._on_request(_will_be_sent("r10", _request(HOME), _response(LOGIN, 302)))

        roles = backend.get_header_roles()
        # The POST hop classifies as the "form" role; its custom header survives
        # (Content-Type itself is in EXCLUDED_HEADERS).
        assert roles["form"]["X-Shop-Client"] == "web"


class TestRealCdpDataclasses:
    """Pin the production code's attribute names against nodriver's real types."""

    @staticmethod
    def _real_response(url: str, status: int, headers: dict[str, str]) -> Any:
        import nodriver.cdp.network as n

        return n.Response(
            url=url,
            status=status,
            status_text="Found" if status == 302 else "OK",
            headers=n.Headers(headers),
            mime_type="text/html",
            charset="utf-8",
            connection_reused=False,
            connection_id=1,
            encoded_data_length=0,
            security_state="secure",
        )

    @staticmethod
    def _real_event(
        rid: str, url: str, method: str, redirect: Any = None, post: str | None = None
    ) -> Any:
        import nodriver.cdp.network as n

        request = n.Request(
            url=url,
            method=method,
            headers=n.Headers({}),
            initial_priority="High",
            referrer_policy="no-referrer",
            post_data=post,
            has_post_data=post is not None,
        )
        explicit = {
            "request_id": n.RequestId(rid),
            "loader_id": n.LoaderId("L"),
            "document_url": url,
            "request": request,
            "timestamp": n.MonotonicTime(1.0),
            "wall_time": n.TimeSinceEpoch(1700000000.0),
            "initiator": n.Initiator(type_="other"),
            "redirect_has_extra_info": False,
            "redirect_response": redirect,
        }
        # nodriver adds required fields as the CDP spec grows (0.50.x added
        # render_blocking_behavior); pass None for any we do not care about so
        # the test pins attribute names on every supported version.
        required = {
            f.name: None
            for f in dataclasses.fields(n.RequestWillBeSent)
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        }
        return n.RequestWillBeSent(**{**required, **explicit})

    def test_login_chain_with_real_dataclasses(self) -> None:
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(self._real_event("real1", LOGIN, "POST", post="u=alice"))
        backend._on_request(
            self._real_event(
                "real1", HOME, "GET", redirect=self._real_response(LOGIN, 302, {"Location": "/"})
            )
        )

        entries = backend.get_har_entries()
        assert [
            (e["request"]["method"], e["request"]["url"], e["response"]["status"]) for e in entries
        ] == [
            ("POST", LOGIN, 302),
            ("GET", HOME, 0),
        ]
        assert entries[0]["response"]["redirectURL"] == HOME
        assert entries[0]["response"]["statusText"] == "Found"
        assert {h["name"]: h["value"] for h in entries[0]["response"]["headers"]} == {
            "Location": "/"
        }


# ---------------------------------------------------------------------------
# HAR builder
# ---------------------------------------------------------------------------


class TestHarEntryRedirectUrl:
    def test_redirect_url_present_and_empty_by_default(self) -> None:
        entry = _build_har_entry(
            {"url": HOME, "method": "GET", "headers": {}, "response": {"status": 200}}
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
        )
        assert entry["response"]["redirectURL"] == HOME
