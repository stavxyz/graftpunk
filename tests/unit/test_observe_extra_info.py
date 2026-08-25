"""Raw headers via *ExtraInfo events (#157) and console timestamp units (#158).

Chromium delivers only filtered headers on ``Network.responseReceived`` /
``redirectResponse``; ``Set-Cookie`` (and the raw ``Cookie`` request header)
arrive on ``Network.responseReceivedExtraInfo`` / ``requestWillBeSentExtraInfo``,
in an order CDP does not guarantee. ``Runtime.consoleAPICalled.timestamp`` is
milliseconds since epoch.
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import MagicMock, patch

from graftpunk.observe.capture import (
    NodriverCaptureBackend,
    SeleniumCaptureBackend,
    _build_har_entry,
    _console_timestamp,
    _merge_headers_ci,
)

LOGIN = "https://shop.example.com/login"
HOME = "https://shop.example.com/"


# ---------------------------------------------------------------------------
# nodriver fakes
# ---------------------------------------------------------------------------


def _request(url: str, method: str = "GET", headers: dict[str, str] | None = None) -> MagicMock:
    r = MagicMock()
    r.url, r.method, r.post_data, r.has_post_data = url, method, None, False
    r.headers = headers if headers is not None else {"Accept": "text/html"}
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
    rid: str,
    request: MagicMock,
    redirect: MagicMock | None = None,
    wall: float = 1.0,
    redirect_has_extra_info: bool = False,
) -> MagicMock:
    ev = MagicMock()
    ev.request_id, ev.request, ev.redirect_response, ev.wall_time = rid, request, redirect, wall
    ev.redirect_has_extra_info = redirect_has_extra_info
    return ev


def _extra_request(rid: str, headers: dict[str, str]) -> MagicMock:
    ev = MagicMock()
    ev.request_id, ev.headers = rid, headers
    return ev


def _extra_response(rid: str, headers: dict[str, str], status_code: int | None = None) -> MagicMock:
    """statusCode is what disambiguates a late hop ExtraInfo from the successor's."""
    ev = MagicMock()
    ev.request_id, ev.headers, ev.status_code = rid, headers, status_code
    return ev


def _received(rid: str, response: MagicMock) -> MagicMock:
    ev = MagicMock()
    ev.request_id, ev.response = rid, response
    return ev


def _names(headers: list[dict[str, str]]) -> list[str]:
    return [h["name"] for h in headers]


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
        assert entry["response"]["cookies"] == [
            {"name": "session", "value": "abc", "path": "/", "httpOnly": True}
        ]

    def test_extra_info_after_response_received_also_works(self) -> None:
        """CDP documents ExtraInfo may fire before OR after responseReceived."""
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r2", _request(HOME)))
        backend._on_response(_received("r2", _response(HOME, 200)))
        backend._on_response_extra_info(_extra_response("r2", {"Set-Cookie": "a=1"}))

        (entry,) = backend.get_har_entries()
        assert entry["response"]["cookies"] == [{"name": "a", "value": "1"}]

    def test_multiple_set_cookie_lines_with_attributes(self) -> None:
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r3", _request(HOME)))
        backend._on_response_extra_info(
            _extra_response(
                "r3",
                {
                    "set-cookie": (
                        "a=1; Path=/; Expires=Wed, 21 Oct 2026 07:28:00 GMT\n"
                        "b=2; Secure; Domain=.example.com"
                    )
                },
            )
        )
        backend._on_response(_received("r3", _response(HOME, 200)))

        (entry,) = backend.get_har_entries()
        assert entry["response"]["cookies"] == [
            {"name": "a", "value": "1", "path": "/", "expires": "Wed, 21 Oct 2026 07:28:00 GMT"},
            {"name": "b", "value": "2", "secure": True, "domain": ".example.com"},
        ]

    def test_raw_cookie_header_wins_and_no_case_duplicates(self) -> None:
        """Renderer headers (canonical case) and wire headers (lowercase on h2)
        must merge into one row per name, raw winning."""
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(
            _will_be_sent(
                "r4", _request(HOME, headers={"Accept": "text/html", "Cookie": "stale=1"})
            )
        )
        backend._on_request_extra_info(
            _extra_request("r4", {"cookie": "session=abc; theme=dark", "accept": "text/html"})
        )

        (entry,) = backend.get_har_entries()
        names = _names(entry["request"]["headers"])
        assert sorted(n.lower() for n in names) == ["accept", "cookie"]
        assert len(names) == 2
        assert {h["name"]: h["value"] for h in entry["request"]["headers"]}[
            "cookie"
        ] == "session=abc; theme=dark"
        assert entry["request"]["cookies"] == [
            {"name": "session", "value": "abc"},
            {"name": "theme", "value": "dark"},
        ]

    def test_raw_set_cookie_beats_filtered_one(self) -> None:
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r5", _request(HOME)))
        backend._on_response(
            _received(
                "r5",
                _response(HOME, 200, {"Set-Cookie": "filtered=1", "Content-Type": "text/html"}),
            )
        )
        backend._on_response_extra_info(
            _extra_response(
                "r5", {"set-cookie": "session=real; Path=/", "content-type": "text/html"}
            )
        )

        (entry,) = backend.get_har_entries()
        names = _names(entry["response"]["headers"])
        assert len(names) == len({n.lower() for n in names})  # no case duplicates
        assert entry["response"]["cookies"] == [{"name": "session", "value": "real", "path": "/"}]

    def test_request_extra_info_before_request_is_buffered(self) -> None:
        """CDP: 'no guarantee whether requestWillBeSent or requestWillBeSentExtraInfo
        will be fired first'."""
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request_extra_info(_extra_request("r6", {"cookie": "session=abc"}))
        backend._on_request(_will_be_sent("r6", _request(HOME)))

        (entry,) = backend.get_har_entries()
        assert entry["request"]["cookies"] == [{"name": "session", "value": "abc"}]

    def test_response_extra_info_before_orphan_response_is_buffered(self) -> None:
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_response_extra_info(_extra_response("r7", {"set-cookie": "x=1"}))
        backend._on_response(_received("r7", _response(HOME, 200)))  # request never seen

        (entry,) = backend.get_har_entries()
        assert entry["response"]["cookies"] == [{"name": "x", "value": "1"}]

    def test_redirect_hop_keeps_its_own_set_cookie_in_measured_order(self) -> None:
        """Measured order on nodriver 0.48.1 and 0.50.3: the 302's ExtraInfo arrives
        before the continuation's requestWillBeSent."""
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r8", _request(LOGIN, "POST"), wall=1.0))
        backend._on_response_extra_info(_extra_response("r8", {"set-cookie": "session=minted"}))
        backend._on_request(
            _will_be_sent(
                "r8",
                _request(HOME),
                _response(LOGIN, 302, {"Location": "/"}),
                wall=2.0,
                redirect_has_extra_info=True,
            )
        )
        backend._on_response_extra_info(_extra_response("r8", {"set-cookie": "seen=1"}))
        backend._on_response(_received("r8", _response(HOME, 200)))

        hop, final = backend.get_har_entries()
        assert hop["response"]["cookies"] == [{"name": "session", "value": "minted"}]
        assert final["response"]["cookies"] == [{"name": "seen", "value": "1"}]

    def test_late_hop_extra_info_still_lands_on_the_hop(self) -> None:
        """Unguaranteed order: the 302's ExtraInfo arrives AFTER the continuation's
        requestWillBeSent (which flags redirectHasExtraInfo). It must land on the
        hop, not on the successor -- otherwise the HAR says the 200 minted the cookie."""
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r9", _request(LOGIN, "POST"), wall=1.0))
        backend._on_request(
            _will_be_sent(
                "r9",
                _request(HOME),
                _response(LOGIN, 302, {"Location": "/"}),
                wall=2.0,
                redirect_has_extra_info=True,
            )
        )
        backend._on_response_extra_info(
            _extra_response("r9", {"set-cookie": "session=minted"}, 302)
        )  # late, for the hop
        backend._on_response_extra_info(
            _extra_response("r9", {"set-cookie": "seen=1"}, 200)
        )  # for the successor
        backend._on_response(_received("r9", _response(HOME, 200)))

        hop, final = backend.get_har_entries()
        assert hop["response"]["cookies"] == [{"name": "session", "value": "minted"}]
        assert final["response"]["cookies"] == [{"name": "seen", "value": "1"}]

    def test_hop_without_extra_info_does_not_steal_the_successors(self) -> None:
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r10", _request(LOGIN, "POST"), wall=1.0))
        backend._on_request(
            _will_be_sent(
                "r10",
                _request(HOME),
                _response(LOGIN, 302),
                wall=2.0,
                redirect_has_extra_info=False,
            )
        )
        backend._on_response_extra_info(_extra_response("r10", {"set-cookie": "seen=1"}))
        backend._on_response(_received("r10", _response(HOME, 200)))

        hop, final = backend.get_har_entries()
        assert hop["response"]["cookies"] == []
        assert final["response"]["cookies"] == [{"name": "seen", "value": "1"}]

    def test_successor_extra_info_before_the_late_hop_one(self) -> None:
        """Reordered: the 200's ExtraInfo arrives before the 302's late one. The
        status code keeps them apart in both directions."""
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r12", _request(LOGIN, "POST"), wall=1.0))
        backend._on_request(
            _will_be_sent(
                "r12", _request(HOME), _response(LOGIN, 302), wall=2.0, redirect_has_extra_info=True
            )
        )
        backend._on_response_extra_info(_extra_response("r12", {"set-cookie": "seen=1"}, 200))
        backend._on_response_extra_info(
            _extra_response("r12", {"set-cookie": "session=minted"}, 302)
        )
        backend._on_response(_received("r12", _response(HOME, 200)))

        hop, final = backend.get_har_entries()
        assert hop["response"]["cookies"] == [{"name": "session", "value": "minted"}]
        assert final["response"]["cookies"] == [{"name": "seen", "value": "1"}]

    def test_dropped_hop_extra_info_does_not_steal_the_successors(self) -> None:
        """The hop's ExtraInfo never arrives (nodriver drops events; measured). The
        successor's 200 ExtraInfo must not be handed to the 302, and the drain
        reports the hop still waiting."""
        import asyncio

        tab = MagicMock()
        backend = NodriverCaptureBackend(MagicMock(), get_tab=lambda: tab)
        backend._on_request(_will_be_sent("r13", _request(LOGIN, "POST"), wall=1.0))
        backend._on_request(
            _will_be_sent(
                "r13", _request(HOME), _response(LOGIN, 302), wall=2.0, redirect_has_extra_info=True
            )
        )
        backend._on_response_extra_info(_extra_response("r13", {"set-cookie": "seen=1"}, 200))
        backend._on_response(_received("r13", _response(HOME, 200)))

        hop, final = backend.get_har_entries()
        assert hop["response"]["cookies"] == []
        assert final["response"]["cookies"] == [{"name": "seen", "value": "1"}]
        with patch("graftpunk.observe.capture.LOG") as mock_log:
            asyncio.run(backend.stop_capture_async())
        unmatched = {c.args[0]: c.kwargs for c in mock_log.debug.call_args_list}[
            "extra_info_unmatched"
        ]
        assert unmatched["hops_without_response_extra"] == 1

    def test_empty_hop_extra_info_still_consumes_its_slot(self) -> None:
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r14", _request(LOGIN, "POST"), wall=1.0))
        backend._on_request(
            _will_be_sent(
                "r14", _request(HOME), _response(LOGIN, 302), wall=2.0, redirect_has_extra_info=True
            )
        )
        backend._on_response_extra_info(_extra_response("r14", {}, 302))
        backend._on_response_extra_info(_extra_response("r14", {"set-cookie": "seen=1"}, 200))
        backend._on_response(_received("r14", _response(HOME, 200)))

        hop, final = backend.get_har_entries()
        assert hop["response"]["cookies"] == []
        assert final["response"]["cookies"] == [{"name": "seen", "value": "1"}]

    def test_late_hop_request_extra_info_matched_by_h2_pseudo_headers(self) -> None:
        """HTTP/2 wire headers carry :method/:path/:authority, so a hop's request
        ExtraInfo arriving after the continuation still finds the hop -- in
        either arrival order."""
        for order in ("hop-first", "successor-first"):
            backend = NodriverCaptureBackend(MagicMock())
            backend._on_request(_will_be_sent("r15", _request(LOGIN, "POST"), wall=1.0))
            backend._on_request(
                _will_be_sent(
                    "r15",
                    _request(HOME),
                    _response(LOGIN, 302),
                    wall=2.0,
                    redirect_has_extra_info=True,
                )
            )
            hop_ev = _extra_request(
                "r15",
                {
                    ":method": "POST",
                    ":path": "/login",
                    ":authority": "shop.example.com",
                    "cookie": "late=hop",
                },
            )
            succ_ev = _extra_request(
                "r15",
                {
                    ":method": "GET",
                    ":path": "/",
                    ":authority": "shop.example.com",
                    "cookie": "real=successor",
                },
            )
            for ev in (hop_ev, succ_ev) if order == "hop-first" else (succ_ev, hop_ev):
                backend._on_request_extra_info(ev)

            hop, final = backend.get_har_entries()
            assert hop["request"]["cookies"] == [{"name": "late", "value": "hop"}], order
            assert final["request"]["cookies"] == [{"name": "real", "value": "successor"}], order

    def test_undeterminable_request_extra_info_prefers_the_live_request(self) -> None:
        """HTTP/1.1 (Host only, same host): a hop's dropped event must not make the
        successor's land on the hop; the documented residual ambiguity is the
        reverse (a late hop event lands on the successor)."""
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r18", _request(LOGIN, "POST"), wall=1.0))
        backend._on_request(
            _will_be_sent(
                "r18", _request(HOME), _response(LOGIN, 302), wall=2.0, redirect_has_extra_info=True
            )
        )
        backend._on_request_extra_info(
            _extra_request("r18", {"host": "shop.example.com", "cookie": "real=successor"})
        )

        hop, final = backend.get_har_entries()
        assert final["request"]["cookies"] == [{"name": "real", "value": "successor"}]
        assert hop["request"]["cookies"] == []

    def test_request_extra_info_for_unannounced_successor_is_buffered(self) -> None:
        """Every entry already has its own: the event belongs to a continuation not
        yet announced. It must be kept and applied when the entry appears."""
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r19", _request(LOGIN, "POST"), wall=1.0))
        backend._on_request_extra_info(_extra_request("r19", {"cookie": "hop=1"}))
        backend._on_request_extra_info(
            _extra_request("r19", {"cookie": "succ=2"})
        )  # before continuation
        backend._on_request(
            _will_be_sent(
                "r19", _request(HOME), _response(LOGIN, 302), wall=2.0, redirect_has_extra_info=True
            )
        )

        hop, final = backend.get_har_entries()
        assert hop["request"]["cookies"] == [{"name": "hop", "value": "1"}]
        assert final["request"]["cookies"] == [{"name": "succ", "value": "2"}]

    def test_successor_response_extra_info_before_continuation_is_buffered(self) -> None:
        """The 200's ExtraInfo arrives while the live entry is still the hop and no
        status is known for either: it must wait, not land on the hop."""
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r20", _request(LOGIN, "POST"), wall=1.0))
        backend._on_response_extra_info(
            _extra_response("r20", {"set-cookie": "seen=1"}, 200)
        )  # too early
        backend._on_request(
            _will_be_sent(
                "r20", _request(HOME), _response(LOGIN, 302), wall=2.0, redirect_has_extra_info=True
            )
        )
        backend._on_response_extra_info(
            _extra_response("r20", {"set-cookie": "session=minted"}, 302)
        )
        backend._on_response(_received("r20", _response(HOME, 200)))

        hop, final = backend.get_har_entries()
        assert hop["response"]["cookies"] == [{"name": "session", "value": "minted"}]
        assert final["response"]["cookies"] == [{"name": "seen", "value": "1"}]

    def test_same_status_chain_with_dropped_event_uses_location(self) -> None:
        """302 -> 302 -> 200 with hop1's ExtraInfo dropped: hop2's must land on hop2,
        told apart by Location."""
        dash = HOME + "dashboard"
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r21", _request(LOGIN, "POST"), wall=1.0))
        backend._on_request(
            _will_be_sent(
                "r21",
                _request(HOME),
                _response(LOGIN, 302, {"Location": "/"}),
                wall=2.0,
                redirect_has_extra_info=True,
            )
        )
        backend._on_request(
            _will_be_sent(
                "r21",
                _request(dash),
                _response(HOME, 302, {"Location": "/dashboard"}),
                wall=3.0,
                redirect_has_extra_info=True,
            )
        )
        backend._on_response_extra_info(
            _extra_response("r21", {"set-cookie": "hop2=2", "location": "/dashboard"}, 302)
        )
        backend._on_response_extra_info(_extra_response("r21", {"set-cookie": "final=3"}, 200))
        backend._on_response(_received("r21", _response(dash, 200)))

        cookies = [[c["name"] for c in e["response"]["cookies"]] for e in backend.get_har_entries()]
        assert cookies == [[], ["hop2"], ["final"]]

    def test_async_start_resets_correlator_state(self) -> None:
        import asyncio

        tab = MagicMock()

        async def send(*a: Any, **k: Any) -> None:
            return None

        tab.send = send
        backend = NodriverCaptureBackend(MagicMock(), get_tab=lambda: tab)
        backend._on_request_extra_info(_extra_request("stale2", {"cookie": "x=1"}))
        asyncio.run(backend.start_capture_async())
        backend._on_request(_will_be_sent("stale2", _request(HOME)))
        (entry,) = backend.get_har_entries()
        assert entry["request"]["cookies"] == []

    def test_request_extra_info_never_overwrites_an_entry_that_has_its_own(self) -> None:
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r16", _request(HOME)))
        backend._on_request_extra_info(_extra_request("r16", {"cookie": "first=1"}))
        backend._on_request_extra_info(_extra_request("r16", {"cookie": "second=2"}))
        (entry,) = backend.get_har_entries()
        assert entry["request"]["cookies"] == [{"name": "first", "value": "1"}]

    def test_three_hop_chain_with_all_late_extra_info(self) -> None:
        dash = HOME + "dashboard"
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("r17", _request(LOGIN, "POST"), wall=1.0))
        backend._on_request(
            _will_be_sent(
                "r17", _request(HOME), _response(LOGIN, 302), wall=2.0, redirect_has_extra_info=True
            )
        )
        backend._on_request(
            _will_be_sent(
                "r17", _request(dash), _response(HOME, 301), wall=3.0, redirect_has_extra_info=True
            )
        )
        backend._on_response_extra_info(_extra_response("r17", {"set-cookie": "a=1"}, 302))
        backend._on_response_extra_info(_extra_response("r17", {"set-cookie": "b=2"}, 301))
        backend._on_response_extra_info(_extra_response("r17", {"set-cookie": "c=3"}, 200))
        backend._on_response(_received("r17", _response(dash, 200)))

        cookies = [[c["name"] for c in e["response"]["cookies"]] for e in backend.get_har_entries()]
        assert cookies == [["a"], ["b"], ["c"]]

    def test_start_capture_resets_correlator_state(self) -> None:
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request_extra_info(_extra_request("stale", {"cookie": "x=1"}))
        backend.start_capture()
        backend._on_request(_will_be_sent("stale", _request(HOME)))
        (entry,) = backend.get_har_entries()
        assert entry["request"]["cookies"] == []

    def test_header_roles_still_see_renderer_headers_only(self) -> None:
        """Raw wire headers stay beside the entry; role extraction (persisted to
        the session cache) keeps the renderer headers it was designed for."""
        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(
            _will_be_sent("r11", _request(HOME, headers={"Accept": "text/html", "X-App": "1"}))
        )
        backend._on_request_extra_info(
            _extra_request("r11", {"accept": "text/html", "x-app": "1", "cookie": "s=1"})
        )

        assert backend._request_map["r11"]["headers"] == {"Accept": "text/html", "X-App": "1"}
        assert backend.get_header_roles()["navigation"] == {"Accept": "text/html", "X-App": "1"}

    def test_unmatched_extra_info_is_logged_at_drain(self) -> None:
        import asyncio

        tab = MagicMock()
        backend = NodriverCaptureBackend(MagicMock(), get_tab=lambda: tab)
        backend._on_request_extra_info(_extra_request("ghost", {"cookie": "x=1"}))
        backend._on_response_extra_info(_extra_response("ghost", {"set-cookie": "x=1"}))
        with patch("graftpunk.observe.capture.LOG") as mock_log:
            asyncio.run(backend.stop_capture_async())
        debug_events = {c.args[0]: c.kwargs for c in mock_log.debug.call_args_list}
        unmatched = debug_events["extra_info_unmatched"]
        assert (unmatched["request_events"], unmatched["response_events"]) == (1, 1)

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

    def test_start_capture_tolerates_codegen_without_extra_info_classes(self) -> None:
        import asyncio
        import sys
        from types import SimpleNamespace

        network = SimpleNamespace(
            enable=lambda **k: None,
            RequestWillBeSent=object,
            ResponseReceived=object,
            LoadingFinished=object,
        )
        runtime = SimpleNamespace(enable=lambda: None, ConsoleAPICalled=object)
        fake = {
            "nodriver": SimpleNamespace(cdp=SimpleNamespace(network=network, runtime=runtime)),
            "nodriver.cdp": SimpleNamespace(network=network, runtime=runtime),
            "nodriver.cdp.network": network,
            "nodriver.cdp.runtime": runtime,
        }
        tab = MagicMock()

        async def send(*a: Any, **k: Any) -> None:
            return None

        tab.send = send
        backend = NodriverCaptureBackend(MagicMock(), get_tab=lambda: tab)
        with patch.dict(sys.modules, fake):
            asyncio.run(backend.start_capture_async())
        registered = {c.args[1].__name__ for c in tab.add_handler.call_args_list}
        assert "_on_request_extra_info" not in registered
        assert "_on_request" in registered


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


def _req(
    rid: str,
    url: str,
    method: str = "GET",
    *,
    redirect: dict[str, Any] | None = None,
    wall: float = 1.0,
    has_extra: bool = False,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "requestId": rid,
        "request": {"url": url, "method": method, "headers": {"Accept": "text/html"}},
        "wallTime": wall,
    }
    if redirect is not None:
        params["redirectResponse"] = redirect
        params["redirectHasExtraInfo"] = has_extra
    return _perf("Network.requestWillBeSent", params)


def _resp(rid: str, url: str, status: int = 200) -> dict[str, Any]:
    return _perf(
        "Network.responseReceived",
        {
            "requestId": rid,
            "response": {
                "url": url,
                "status": status,
                "headers": {"Content-Type": "text/html"},
                "mimeType": "text/html",
            },
        },
    )


class TestSeleniumExtraInfo:
    def test_set_cookie_and_cookie_headers_land_in_har(self) -> None:
        perf = [
            _req("s1", HOME),
            _perf(
                "Network.requestWillBeSentExtraInfo",
                {"requestId": "s1", "headers": {"cookie": "session=abc", "accept": "text/html"}},
            ),
            _perf(
                "Network.responseReceivedExtraInfo",
                {
                    "requestId": "s1",
                    "headers": {"set-cookie": "session=new; Path=/", "content-type": "text/html"},
                },
            ),
            _resp("s1", HOME),
        ]
        backend = SeleniumCaptureBackend(_driver(perf))
        backend.stop_capture()

        (entry,) = backend.get_har_entries()
        assert entry["request"]["cookies"] == [{"name": "session", "value": "abc"}]
        assert entry["response"]["cookies"] == [{"name": "session", "value": "new", "path": "/"}]
        for side in ("request", "response"):
            names = _names(entry[side]["headers"])
            assert len(names) == len({n.lower() for n in names})

    def test_extra_info_before_request_is_buffered(self) -> None:
        perf = [
            _perf(
                "Network.requestWillBeSentExtraInfo",
                {"requestId": "s2", "headers": {"cookie": "session=abc"}},
            ),
            _req("s2", HOME),
            _resp("s2", HOME),
        ]
        backend = SeleniumCaptureBackend(_driver(perf))
        backend.stop_capture()
        assert backend.get_har_entries()[0]["request"]["cookies"] == [
            {"name": "session", "value": "abc"}
        ]

    def test_late_hop_extra_info_lands_on_the_hop(self) -> None:
        perf = [
            _req("s3", LOGIN, "POST", wall=1.0),
            _req(
                "s3",
                HOME,
                redirect={"url": LOGIN, "status": 302, "headers": {"Location": "/"}},
                wall=2.0,
                has_extra=True,
            ),
            _perf(
                "Network.responseReceivedExtraInfo",
                {"requestId": "s3", "headers": {"set-cookie": "session=minted"}, "statusCode": 302},
            ),
            _perf(
                "Network.responseReceivedExtraInfo",
                {"requestId": "s3", "headers": {"set-cookie": "seen=1"}, "statusCode": 200},
            ),
            _resp("s3", HOME),
        ]
        backend = SeleniumCaptureBackend(_driver(perf))
        backend.stop_capture()

        hop, final = backend.get_har_entries()
        assert hop["response"]["cookies"] == [{"name": "session", "value": "minted"}]
        assert final["response"]["cookies"] == [{"name": "seen", "value": "1"}]

    def test_dropped_hop_extra_info_does_not_steal_the_successors(self) -> None:
        perf = [
            _req("s4", LOGIN, "POST", wall=1.0),
            _req(
                "s4",
                HOME,
                redirect={"url": LOGIN, "status": 302, "headers": {}},
                wall=2.0,
                has_extra=True,
            ),
            _perf(
                "Network.responseReceivedExtraInfo",
                {"requestId": "s4", "headers": {"set-cookie": "seen=1"}, "statusCode": 200},
            ),
            _resp("s4", HOME),
        ]
        backend = SeleniumCaptureBackend(_driver(perf))
        backend.stop_capture()
        hop, final = backend.get_har_entries()
        assert hop["response"]["cookies"] == []
        assert final["response"]["cookies"] == [{"name": "seen", "value": "1"}]

    def test_browser_log_timestamps_are_normalised_too(self) -> None:
        driver = _driver([])
        driver.get_log = MagicMock(
            side_effect=lambda kind: (
                [{"level": "SEVERE", "message": "net err", "timestamp": 1700000000123}]
                if kind == "browser"
                else []
            )
        )
        backend = SeleniumCaptureBackend(driver)
        backend.stop_capture()
        assert backend.get_console_logs()[0]["timestamp"] == 1700000000.123

    def test_unmatched_extra_info_logged_at_drain(self) -> None:
        perf = [
            _perf(
                "Network.responseReceivedExtraInfo",
                {"requestId": "ghost", "headers": {"set-cookie": "x=1"}},
            )
        ]
        backend = SeleniumCaptureBackend(_driver(perf))
        with patch("graftpunk.observe.capture.LOG") as mock_log:
            backend.stop_capture()
        assert any(c.args[0] == "extra_info_unmatched" for c in mock_log.debug.call_args_list)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_merge_headers_ci_raw_wins_and_dedupes(self) -> None:
        merged = _merge_headers_ci({"Content-Type": "a", "X-Keep": "k"}, {"content-type": "b"})
        assert merged == {"X-Keep": "k", "content-type": "b"}

    def test_merge_headers_ci_joins_override_names_differing_only_in_case(self) -> None:
        merged = _merge_headers_ci(
            {"Set-Cookie": "f=1"}, {"set-cookie": "a=1", "Set-Cookie": "b=2"}
        )
        assert merged == {"set-cookie": "a=1\nb=2"}
        merged = _merge_headers_ci({}, {"cookie": "a=1", "Cookie": "b=2"})
        assert merged == {"cookie": "a=1; b=2"}

    def test_merge_headers_ci_handles_none(self) -> None:
        assert _merge_headers_ci(None, None) == {}
        assert _merge_headers_ci({"A": "1"}, None) == {"A": "1"}

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
        assert entry["response"]["cookies"] == [{"name": "ok", "value": "2", "path": "/"}]

    def test_value_containing_equals_is_kept_whole(self) -> None:
        entry = _build_har_entry(
            {
                "url": HOME,
                "method": "GET",
                "headers": {},
                "response": {"status": 200},
                "_response_extra_headers": {"set-cookie": "tok=a=b=c; Secure"},
            }
        )
        assert entry["response"]["cookies"] == [{"name": "tok", "value": "a=b=c", "secure": True}]


# ---------------------------------------------------------------------------
# console timestamps (#158)
# ---------------------------------------------------------------------------


class TestRealExtraInfoEvents:
    """Real nodriver dataclasses (built via from_json) through the real handlers,
    so an upstream attribute rename fails here rather than only in the field."""

    def test_real_events_reach_the_har(self) -> None:
        import nodriver.cdp.network as n

        backend = NodriverCaptureBackend(MagicMock())
        backend._on_request(_will_be_sent("real", _request(HOME)))
        req_extra = n.RequestWillBeSentExtraInfo.from_json(
            {
                "requestId": "real",
                "associatedCookies": [],
                "headers": {"cookie": "s=1"},
                "connectTiming": {"requestTime": 1.0},
            }
        )
        resp_extra = n.ResponseReceivedExtraInfo.from_json(
            {
                "requestId": "real",
                "blockedCookies": [],
                "headers": {"set-cookie": "s=2; Path=/"},
                "resourceIPAddressSpace": "Public",
                "statusCode": 200,
            }
        )
        backend._on_request_extra_info(req_extra)
        backend._on_response_extra_info(resp_extra)
        backend._on_response(_received("real", _response(HOME, 200)))

        (entry,) = backend.get_har_entries()
        assert entry["request"]["cookies"] == [{"name": "s", "value": "1"}]
        assert entry["response"]["cookies"] == [{"name": "s", "value": "2", "path": "/"}]


class TestConsoleTimestamp:
    def test_cdp_milliseconds_become_seconds(self) -> None:
        assert _console_timestamp(1700000000000.0) == 1700000000.0

    def test_seconds_pass_through(self) -> None:
        assert _console_timestamp(1700000000.5) == 1700000000.5

    def test_missing_zero_or_garbage_fall_back_to_now(self) -> None:
        for raw in (None, 0, "abc", object(), float("nan"), float("inf")):
            before = time.time()
            assert before <= _console_timestamp(raw) <= time.time()

    def test_nodriver_console_event_normalised(self) -> None:
        backend = NodriverCaptureBackend(MagicMock())
        event = MagicMock()
        event.type_ = "log"
        event.args = []
        event.timestamp = 1700000000000.0
        backend._on_console(event)
        assert backend.get_console_logs()[0]["timestamp"] == 1700000000.0

    def test_selenium_console_event_normalised_and_garbage_does_not_abort_drain(self) -> None:
        perf = [
            _perf("Runtime.consoleAPICalled", {"type": "log", "args": [], "timestamp": "garbage"}),
            _perf(
                "Runtime.consoleAPICalled",
                {"type": "log", "args": [], "timestamp": 1700000000000.0},
            ),
            _req("s9", HOME),
        ]
        backend = SeleniumCaptureBackend(_driver(perf))
        backend.stop_capture()
        logs = backend.get_console_logs()
        assert logs[1]["timestamp"] == 1700000000.0
        assert len(backend.get_har_entries()) == 1  # drain continued past the bad timestamp
