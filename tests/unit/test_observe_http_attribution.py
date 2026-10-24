"""The HTTP recorder blames a body only for what failed, and times what it saw (#283).

A body is ``read_failed`` only when reading it raised. A hook that raises
leaves the response's body recorded, a refused connection after a hop is
recorded as that request's error, and a body nothing read is ``not_read``.
Also: hop order and times through a late-mounted adapter, a re-send's real
time, the session wrapper's pickling gate and restore, and the browser
captures' marks for bodies stop could not fetch.
"""

from __future__ import annotations

import datetime
import json
from typing import Any

import pytest
import requests
import requests.adapters
from structlog.testing import capture_logs

from graftpunk.observe.http_capture import HttpCaptureBackend


def _recording(session: requests.Session | None = None) -> tuple[requests.Session, Any]:
    session = session or requests.Session()
    capture = HttpCaptureBackend(session)
    capture.start_capture()
    return session, capture


def _start(entry: dict[str, Any]) -> float:
    return datetime.datetime.fromisoformat(entry["startedDateTime"]).timestamp()


def _raise_for_status(response: requests.Response, *_a: Any, **_k: Any) -> None:
    response.raise_for_status()


class TestBlameOnlyWhatFailed:
    @pytest.mark.parametrize("record", [False, True])
    def test_a_hook_that_raises_leaves_the_body_recorded(self, site: str, record: bool) -> None:
        session = requests.Session()
        session.hooks["response"].append(_raise_for_status)
        capture = None
        if record:
            session, capture = _recording(session)
        with pytest.raises(requests.HTTPError) as raised:
            session.get(f"{site}/bad400", timeout=5)

        assert raised.value.response.json() == {"error": "bad thing"}
        if capture is not None:
            capture.stop_capture()
            (entry,) = capture.get_har_entries()
            content = entry["response"]["content"]
            assert json.loads(content["text"]) == {"error": "bad thing"}
            assert "_bodyNotCaptured" not in content

    def test_a_refused_connection_after_a_hop_is_that_requests_error(self, site: str) -> None:
        session, capture = _recording()
        with pytest.raises(requests.ConnectionError):
            session.get(f"{site}/badgz-to-closed", timeout=5)
        capture.stop_capture()

        hop, failed = capture.get_har_entries()
        assert hop["response"]["status"] == 302
        assert hop["response"]["content"]["_bodyNotCapturedReason"] == "read_failed"
        assert failed["request"]["url"] == "http://127.0.0.1:9/gone"
        assert failed["response"]["status"] == 0
        assert failed["response"]["_error"].startswith("ConnectionError: ")

    def test_a_401_a_hook_replaced_is_not_read(self, site: str) -> None:
        session, capture = _recording()

        def retry_with_auth(response: requests.Response, *_a: Any, **_k: Any) -> Any:
            if response.status_code != 401:
                return response
            request = response.request.copy()
            request.headers["Authorization"] = "ok"
            return session.send(request, timeout=5)

        session.hooks["response"].append(retry_with_auth)
        final = session.get(f"{site}/needauth", timeout=5)
        capture.stop_capture()

        assert final.status_code == 200
        first, second = capture.get_har_entries()
        assert first["response"]["status"] == 401
        assert first["response"]["content"]["_bodyNotCapturedReason"] == "not_read"
        assert json.loads(second["response"]["content"]["text"]) == {"ok": 1}

    def test_a_direct_send_on_a_streaming_session_is_streamed(self, site: str) -> None:
        session, capture = _recording()
        session.stream = True
        response = session.send(requests.Request("GET", f"{site}/home").prepare(), timeout=5)
        capture.stop_capture()

        assert response.raw.read() == b"<h1>home</h1>"
        (entry,) = capture.get_har_entries()
        assert entry["response"]["content"]["_bodyNotCapturedReason"] == "streamed"

    def test_an_adapter_used_directly_is_not_read(self, site: str) -> None:
        session, capture = _recording()
        adapter = session.get_adapter(site)
        adapter.send(requests.Request("GET", f"{site}/home").prepare(), timeout=5)
        capture.stop_capture()

        (entry,) = capture.get_har_entries()
        assert entry["response"]["content"]["_bodyNotCapturedReason"] == "not_read"


class TestTiming:
    def test_a_late_adapter_keeps_hop_order_and_pairs_times(self, site: str) -> None:
        session, capture = _recording()
        # Only the hop goes through an adapter the capture did not wrap.
        session.mount(f"{site}/fastredir", requests.adapters.HTTPAdapter())
        session.get(f"{site}/fastredir", timeout=5)
        capture.stop_capture()

        hop, landing = capture.get_har_entries()
        assert (hop["response"]["status"], landing["response"]["status"]) == (302, 200)
        assert _start(hop) <= _start(landing)

    def test_late_adapter_hops_are_timed_in_chain_order(self, site: str) -> None:
        session, capture = _recording()
        session.mount("http://", requests.adapters.HTTPAdapter())
        session.get(f"{site}/fastredir", timeout=5)
        capture.stop_capture()

        hop, landing = capture.get_har_entries()
        assert (hop["response"]["status"], landing["response"]["status"]) == (302, 200)
        # The slow landing page starts after the hop, never before it.
        assert _start(hop) <= _start(landing)
        assert _start(landing) >= _start(hop) + hop["time"] / 1000 - 0.001

    def test_a_resend_through_the_adapter_gets_its_real_time(self, site: str) -> None:
        session, capture = _recording()

        def resend_slow(response: requests.Response, *_a: Any, **_k: Any) -> Any:
            if response.status_code != 401:
                return response
            request = requests.Request("GET", f"{response.url.rsplit('/', 1)[0]}/slow").prepare()
            # What HTTPDigestAuth does: send again on the same adapter.
            return response.connection.send(request, timeout=5)

        session.hooks["response"].append(resend_slow)
        session.get(f"{site}/needauth", timeout=5)
        capture.stop_capture()

        resent = capture.get_har_entries()[-1]
        assert resent["request"]["url"].endswith("/slow")
        assert resent["time"] >= 250


class _OwnStateSession(requests.Session):
    """A session that pickles its whole instance dict."""

    def __getstate__(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)


class TestSessionWrapper:
    def test_a_session_with_its_own_pickled_state_is_not_wrapped(self, site: str) -> None:
        import dill

        session = _OwnStateSession()
        capture = HttpCaptureBackend(session)
        with capture_logs() as logs:
            capture.start_capture()

        assert "send" not in vars(session)
        assert "http_capture_session_not_recorded" in [e["event"] for e in logs]
        session.get(f"{site}/home", timeout=5)
        dill.dumps(session)  # pickles as with observe off
        capture.stop_capture()

    def test_the_session_send_is_restored_at_stop(self, site: str) -> None:
        session, capture = _recording()
        assert "send" in vars(session)
        capture.stop_capture()

        assert "send" not in vars(session)


class TestTypeAndBodyMarks:
    def test_a_parameter_does_not_make_a_type_text(self, tmp_path: Any) -> None:
        from graftpunk.observe.capture import _process_response_body

        response: dict[str, Any] = {}
        _process_response_body(
            response, b"plain", False, "r1", 'application/x-thing; name="text"', 100, None
        )

        assert response["_bodyNotCaptured"] == "not_text"

    def test_a_request_body_file_is_scrubbed_as_it_is_recorded(
        self, site: str, tmp_path: Any
    ) -> None:
        hidden = "hunter2-correct-horse"
        session = requests.Session()
        capture = HttpCaptureBackend(
            session, max_body_size=10, bodies_dir=tmp_path / "bodies", redact=(hidden,)
        )
        capture.start_capture()
        session.post(f"{site}/api/search", json={"query": hidden}, timeout=5)

        # Before the run is written: the file in bodies/ is already scrubbed.
        (body_file,) = (tmp_path / "bodies").glob("*.request.*")
        assert hidden not in body_file.read_text()
        capture.stop_capture()
