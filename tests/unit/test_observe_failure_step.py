"""#268 part 3: the wrong-password step of ``gp observe interactive``.

The recorder side: with ``failure_step``, the page is read raw FAILURE_READ_S after
each credential post's response and again when the user marks the last one failed;
only the failure texts that survive are kept, on that post's HAR entry. Driven with
the fake tab and fake clock of the after-read tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from graftpunk.observe import capture
from graftpunk.observe.capture import NodriverCaptureBackend
from tests.unit.test_observe_post_data import (
    _LOGIN_MODAL,
    _POST_MS,
    LOGIN,
    FakeClock,
    FakeTab,
    _binding_called,
    _idle,
    _never,
    _response_received,
    _start,
    _stop,
    _will_be_sent,
)

_WRONG = json.dumps({"username": "jsmith", "password": "wrongpass1"})
_RIGHT = json.dumps({"username": "jsmith", "password": "realsecret9"})
_TOAST = "Invalid username or password."
_STAYS = "Too many attempts may lock you out."
_FAILED_PAGE = _LOGIN_MODAL.replace(
    '<div id="app">', f'<div id="app"><div role="alert">{_TOAST}</div><p>{_STAYS}</p>'
)
# The toast has dismissed itself by the time the user presses Enter.
_FAILED_PAGE_LATER = _LOGIN_MODAL.replace('<div id="app">', f'<div id="app"><p>{_STAYS}</p>')
_LANDED = "<html><body><div id='account-menu'>Your orders</div></body></html>"


async def _recording(
    tmp_path: Path, *, step: bool = True
) -> tuple[FakeTab, NodriverCaptureBackend, FakeClock, list[float]]:
    tab = FakeTab({}, _never)
    tab.dom_html = _FAILED_PAGE
    clock = FakeClock()
    backend = await _start(tab, dom_snapshots_dir=tmp_path / "snapshots", failure_step=step)
    backend._now, backend._sleep = clock.time, clock.sleep
    reads: list[float] = []
    tab.on_dom_read = lambda: reads.append(clock.now)
    return tab, backend, clock, reads


async def _post(tab: FakeTab, rid: str, body: str = _WRONG, *, respond: bool = True) -> None:
    # The listener's snapshot is the post's own, so every DOM read is a read after it.
    await tab.dispatch(_binding_called(_LOGIN_MODAL, time_ms=_POST_MS - 40))
    await tab.dispatch(_will_be_sent(rid, inline=body, url=LOGIN))
    if respond:
        await tab.dispatch(_response_received(rid, url=LOGIN))


def _request(backend: NodriverCaptureBackend, rid_url_index: int = 0) -> dict[str, Any]:
    return backend.get_har_entries()[rid_url_index]["request"]


def _run_text(tmp_path: Path) -> str:
    return "".join(path.read_text() for path in tmp_path.rglob("*") if path.is_file())


class TestTheReadsForTheFailedPost:
    @pytest.mark.asyncio
    async def test_the_1_5_s_read_and_the_enter_read_give_the_candidates(
        self, tmp_path: Path
    ) -> None:
        tab, backend, clock, reads = await _recording(tmp_path)
        await _post(tab, "90.1")
        await clock.advance(1.4, backend)
        assert reads == []
        await clock.advance(0.1, backend)
        assert reads == [capture.FAILURE_READ_S]
        assert capture.FAILURE_READ_S == 1.5
        tab.dom_html = _FAILED_PAGE_LATER
        await clock.advance(0.5, backend)
        assert backend.mark_failed_login() is True
        await _idle(backend, clock)
        assert reads == [1.5, 2.0]
        await _stop(backend, tab)

        request = _request(backend)
        assert request["_userMarkedFailed"] is True
        # The toast only the 1.5 s read caught ranks first: it was in an alert.
        assert request["_failureTextCandidates"] == [_TOAST, _STAYS]
        # Text only: no failure page is written, and the after-read keeps no text.
        assert _TOAST not in _run_text(tmp_path)
        assert _STAYS not in _run_text(tmp_path)

    @pytest.mark.asyncio
    async def test_enter_before_the_1_5_s_read_waits_for_it(self, tmp_path: Path) -> None:
        tab, backend, clock, reads = await _recording(tmp_path)
        await _post(tab, "90.2")
        await clock.advance(1, backend)
        assert backend.mark_failed_login() is True
        await _idle(backend, clock)
        assert reads == [1.0]
        tab.dom_html = _FAILED_PAGE_LATER
        await clock.advance(0.5, backend)
        assert reads == [1.0, 1.5]
        await _stop(backend, tab)
        assert _request(backend)["_failureTextCandidates"] == [_TOAST, _STAYS]

    @pytest.mark.asyncio
    async def test_the_last_credential_post_before_enter_is_the_one_marked(
        self, tmp_path: Path
    ) -> None:
        tab, backend, clock, _reads = await _recording(tmp_path)
        await _post(tab, "91.1")
        await clock.advance(2, backend)
        await _post(tab, "91.2")
        await clock.advance(2, backend)
        assert backend.mark_failed_login() is True
        await _idle(backend, clock)
        await _stop(backend, tab)

        first, second = (entry["request"] for entry in backend.get_har_entries())
        assert "_userMarkedFailed" not in first
        assert "_failureTextCandidates" not in first
        assert second["_userMarkedFailed"] is True
        assert second["_failureTextCandidates"] == [_TOAST, _STAYS]

    @pytest.mark.asyncio
    async def test_enter_with_no_credential_post_marks_nothing_and_keeps_the_step(
        self, tmp_path: Path
    ) -> None:
        tab, backend, clock, _reads = await _recording(tmp_path)
        assert backend.mark_failed_login() is False
        await _post(tab, "92.1")
        await clock.advance(2, backend)
        assert backend.mark_failed_login() is True
        assert backend.mark_failed_login() is False
        await _idle(backend, clock)
        await _stop(backend, tab)
        assert _request(backend)["_failureTextCandidates"] == [_TOAST, _STAYS]

    @pytest.mark.asyncio
    async def test_posts_after_the_mark_get_no_1_5_s_read(self, tmp_path: Path) -> None:
        tab, backend, clock, reads = await _recording(tmp_path)
        await _post(tab, "93.1")
        await clock.advance(2, backend)
        backend.mark_failed_login()
        await _idle(backend, clock)
        assert reads == [1.5, 2.0]
        await _post(tab, "93.2", _RIGHT)
        await clock.advance(2, backend)
        # The failed post's own after-read at 3 s; none at 3.5 s for the new post.
        assert reads == [1.5, 2.0, 3.0]
        await clock.advance(1, backend)
        # The new post's after-read, 3 s after its response.
        assert reads == [1.5, 2.0, 3.0, 5.0]
        await _stop(backend, tab)


class TestTheRealLogin:
    @pytest.mark.asyncio
    async def test_a_candidate_on_the_real_login_s_page_is_dropped(self, tmp_path: Path) -> None:
        tab, backend, clock, _reads = await _recording(tmp_path)
        await _post(tab, "94.1")
        await clock.advance(2, backend)
        backend.mark_failed_login()
        await _idle(backend, clock)
        tab.dom_html = _LANDED.replace("Your orders", f"Your orders <p>{_STAYS}</p>")
        await _post(tab, "94.2", _RIGHT)
        await clock.advance(3, backend)
        await _stop(backend, tab)

        failed = _request(backend, 0)
        assert failed["_failureTextCandidates"] == [_TOAST]
        assert "_failureTextCandidates" not in _request(backend, 1)

    @pytest.mark.asyncio
    async def test_a_candidate_holding_the_real_post_s_value_is_dropped(
        self, tmp_path: Path
    ) -> None:
        tab, backend, clock, _reads = await _recording(tmp_path)
        tab.dom_html = _FAILED_PAGE.replace(_STAYS, "No account named jsmith2024 here.")
        await _post(tab, "95.1", json.dumps({"username": "bob_tester", "password": "wrongpass1"}))
        await clock.advance(2, backend)
        backend.mark_failed_login()
        await _idle(backend, clock)
        tab.dom_html = _LANDED
        real = json.dumps({"username": "jsmith2024", "password": "realsecret9"})
        await _post(tab, "95.2", real, respond=False)
        await _stop(backend, tab)
        assert _request(backend, 0)["_failureTextCandidates"] == [_TOAST]

    @pytest.mark.asyncio
    async def test_a_real_post_whose_values_cannot_be_read_drops_them_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.unit.test_observe_post_data import _unreadable_credential_body

        tab, backend, clock, _reads = await _recording(tmp_path)
        await _post(tab, "96.1")
        await clock.advance(2, backend)
        backend.mark_failed_login()
        await _idle(backend, clock)
        unreadable = _unreadable_credential_body(monkeypatch)
        await _post(tab, "96.2", unreadable, respond=False)
        await _stop(backend, tab)
        assert _request(backend, 0)["_failureTextCandidates"] == []


class TestStopAndPrivacy:
    @pytest.mark.asyncio
    async def test_stop_cancels_a_pending_1_5_s_read(self, tmp_path: Path) -> None:
        tab, backend, clock, reads = await _recording(tmp_path)
        await _post(tab, "97.1")
        await clock.advance(1, backend)
        await _stop(backend, tab)
        await clock.advance(60, backend)
        assert reads == []
        assert backend._after_read_tasks == set()
        assert backend._step_reads == {}
        assert backend._step_before == {}
        assert backend._step_posts == []

    @pytest.mark.asyncio
    async def test_stop_cancels_the_enter_read(self, tmp_path: Path) -> None:
        import asyncio

        tab, backend, clock, _reads = await _recording(tmp_path)
        await _post(tab, "97.2")
        await clock.advance(2, backend)
        tab.dom_gate = asyncio.Event()
        earlier = set(backend._after_read_tasks)
        backend.mark_failed_login()
        (enter_read,) = backend._after_read_tasks - earlier
        for _ in range(5):
            await asyncio.sleep(0)
        assert not enter_read.done()
        await _stop(backend, tab)
        assert enter_read.cancelled()
        request = _request(backend)
        assert request["_userMarkedFailed"] is True
        assert request["_failureTextCandidates"] == []

    @pytest.mark.asyncio
    async def test_without_the_step_nothing_is_read_or_kept(self, tmp_path: Path) -> None:
        tab, backend, clock, reads = await _recording(tmp_path, step=False)
        await _post(tab, "98.1")
        await clock.advance(2, backend)
        assert reads == []
        assert backend.mark_failed_login() is False
        assert backend.failed_login_secrets() == ()
        await clock.advance(1, backend)
        assert reads == [3.0]
        await _stop(backend, tab)
        request = _request(backend)
        assert "_userMarkedFailed" not in request
        assert "_failureTextCandidates" not in request
        assert backend._step_posts == []
        assert backend._step_before == {}
        assert backend._step_reads == {}

    @pytest.mark.asyncio
    async def test_the_failed_password_is_handed_to_the_run_s_redaction(
        self, tmp_path: Path
    ) -> None:
        from graftpunk.observe.run import save_observe_run
        from graftpunk.observe.storage import ObserveStorage

        storage = ObserveStorage(tmp_path, "myshop", "r1")
        tab = FakeTab({}, _never)
        tab.dom_html = _FAILED_PAGE
        clock = FakeClock()
        backend = NodriverCaptureBackend(
            object(),
            get_tab=lambda: tab,
            bodies_dir=storage.run_dir / "bodies",
            dom_snapshots_dir=storage.run_dir / "snapshots",
            failure_step=True,
        )
        backend._now, backend._sleep = clock.time, clock.sleep
        await backend.start_capture_async()
        await _post(tab, "99.1")
        await clock.advance(2, backend)
        backend.mark_failed_login()
        await _idle(backend, clock)
        assert backend.failed_login_secrets() == ("wrongpass1",)
        tab.dom_html = _LANDED
        await save_observe_run(
            storage, backend, "interactive-final", redact=backend.failed_login_secrets()
        )

        har = (storage.run_dir / "network.har").read_text()
        assert "wrongpass1" not in har
        (entry,) = json.loads(har)["log"]["entries"]
        assert entry["request"]["_userMarkedFailed"] is True
        assert entry["request"]["_failureTextCandidates"] == [_TOAST, _STAYS]
        assert _TOAST not in _run_text(storage.run_dir).replace(har, "")


class TestTheLoginPageAsServed:
    def test_the_latest_html_response_of_the_sending_page(self) -> None:
        backend = NodriverCaptureBackend(object(), dom_snapshots_dir=Path("/nonexistent"))
        post = {"url": LOGIN, "method": "POST", "sender_url": LOGIN + "?next=%2F"}
        page = {"url": LOGIN, "response": {"mimeType": "text/html", "body": "<p>old</p>"}}
        again = {"url": LOGIN + "#top", "response": {"mimeType": "text/html", "body": "<p>new</p>"}}
        script = {"url": LOGIN, "response": {"mimeType": "application/json", "body": "{}"}}
        backend._request_map = {"1": page, "2": again, "3": script, "4": post}
        assert backend._served_page(post) == "<p>new</p>"
        backend._request_map = {"3": script, "4": post}
        assert backend._served_page(post) is None
