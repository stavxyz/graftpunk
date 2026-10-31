"""#268 part 3: the wrong-password step of ``gp observe interactive``.

The recorder side: with ``failure_step``, the page is read raw FAILURE_READ_S after
each credential post's response and again when the user marks the last one failed;
only the failure texts that survive are kept, on that post's HAR entry. Driven with
the fake tab and fake clock of the after-read tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import signal
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from graftpunk.cli.main import app
from graftpunk.cli.observe_browser import ObserveSession, run_observe_interactive
from graftpunk.observe import capture
from graftpunk.observe.capture import NodriverCaptureBackend
from tests.unit.cli_harness import strip_ansi
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
    '<div id="app">',
    f'<div id="app"><div role="alert">{_TOAST}</div><p class="form-error">{_STAYS}</p>',
)
# The toast has dismissed itself by the time the user presses Enter.
_FAILED_PAGE_LATER = _LOGIN_MODAL.replace(
    '<div id="app">', f'<div id="app"><p class="form-error">{_STAYS}</p>'
)
_LANDED = "<html><body><div id='account-menu'>Your orders</div></body></html>"
_FORM = "application/x-www-form-urlencoded"


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


async def _post(
    tab: FakeTab,
    rid: str,
    body: str = _WRONG,
    *,
    respond: bool = True,
    content_type: str = "application/json",
    page: str = _LOGIN_MODAL,
) -> None:
    # The listener's snapshot is the post's own, so every DOM read is a read after it.
    await tab.dispatch(_binding_called(page, time_ms=_POST_MS - 40))
    await tab.dispatch(
        _will_be_sent(rid, inline=body, url=LOGIN, headers={"Content-Type": content_type})
    )
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
        # The toast only the 1.5 s read caught ranks first: that read comes first.
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
        # The FAILURE_READ_S read is taken first, then the read on Enter.
        assert _request(backend)["_failureTextCandidates"] == [_STAYS, _TOAST]

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
        assert "_failureTextCandidates" not in _request(backend, 0)
        assert backend.marked_failure_candidates() == ()


class TestAnIdentifierFirstLogin:
    """The username is posted by a step before the password post, so neither
    password post's own values hold it."""

    _IDENTIFIER = "https://myshop.example/login/identifier"

    async def _identifier_step(self, tab: FakeTab, rid: str, username: str) -> None:
        await tab.dispatch(
            _will_be_sent(
                rid,
                inline=f"username={username}",
                url=self._IDENTIFIER,
                headers={"Content-Type": _FORM},
            )
        )
        await tab.dispatch(_response_received(rid, url=self._IDENTIFIER))

    @pytest.mark.asyncio
    async def test_a_text_holding_the_username_of_the_failed_login_is_dropped(
        self, tmp_path: Path
    ) -> None:
        tab, backend, clock, _reads = await _recording(tmp_path)
        tab.dom_html = _FAILED_PAGE.replace(_TOAST, "The password for jsmith is not right.")
        await self._identifier_step(tab, "79.1", "jsmith")
        await _post(tab, "79.2", "password=wrong-x7k2q9", content_type=_FORM)
        await clock.advance(2, backend)
        assert backend.mark_failed_login() is True
        await _idle(backend, clock)
        await _stop(backend, tab)
        assert backend.marked_failure_candidates() == (_STAYS,)

    async def _step(self, tab: FakeTab, rid: str, body: str) -> None:
        await tab.dispatch(
            _will_be_sent(rid, inline=body, url=self._IDENTIFIER, headers={"Content-Type": _FORM})
        )
        await tab.dispatch(_response_received(rid, url=self._IDENTIFIER))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["j_username", "user_login", "loginfmt", "handle", "uid"])
    async def test_a_step_field_under_any_name_is_the_username(
        self, tmp_path: Path, name: str
    ) -> None:
        """No identifier field's name and no login form's: every value of a POST
        counts."""
        tab, backend, clock, _reads = await _recording(tmp_path)
        tab.dom_html = _FAILED_PAGE.replace(_TOAST, "The password for jsmith is not right.")
        await self._step(tab, "76.1", f"{name}=jsmith")
        await _post(tab, "76.2", "j_password=wrong-x7k2q9", content_type=_FORM)
        await clock.advance(2, backend)
        assert backend.mark_failed_login() is True
        await _idle(backend, clock)
        await _stop(backend, tab)
        assert backend.marked_failure_candidates() == (_STAYS,)

    @pytest.mark.asyncio
    async def test_a_login_form_s_username_name_counts(self, tmp_path: Path) -> None:
        """``handle`` is no identifier field's name; the login form on the page at the
        post takes it as its username, and a script posted it in a step first. A
        value under three characters is no account value by the every-value rule, so
        only this rule finds it."""
        page = _LOGIN_MODAL.replace('name="username"', 'name="handle"')
        tab, backend, clock, _reads = await _recording(tmp_path)
        tab.dom_html = _FAILED_PAGE.replace(_TOAST, "The password for jo is not right.")
        await self._step(tab, "77.1", "handle=jo")
        await _post(tab, "77.2", "password=wrong-x7k2q9", content_type=_FORM, page=page)
        await clock.advance(2, backend)
        assert backend.mark_failed_login() is True
        await _idle(backend, clock)
        await _stop(backend, tab)
        assert backend.marked_failure_candidates() == (_STAYS,)

    @pytest.mark.asyncio
    async def test_the_real_login_is_checked_with_the_login_form_s_username_name(
        self, tmp_path: Path
    ) -> None:
        """Once the texts are settled, the username names of the login form at the
        failed post count for each later post too."""
        page = _LOGIN_MODAL.replace('name="username"', 'name="handle"')
        tab, backend, clock, _reads = await _recording(tmp_path)
        tab.dom_html = _FAILED_PAGE.replace(_STAYS, "No account for jo here.")
        await _post(tab, "75.1", "password=wrong-x7k2q9", content_type=_FORM, page=page)
        await clock.advance(2, backend)
        assert backend.mark_failed_login() is True
        await _idle(backend, clock)
        assert backend.marked_failure_candidates() == (_TOAST, "No account for jo here.")
        tab.dom_html = _LANDED
        await self._step(tab, "75.2", "handle=jo")
        await _post(tab, "75.3", "password=RealSecret9", content_type=_FORM, respond=False)
        await _stop(backend, tab)
        assert backend.marked_failure_candidates() == (_TOAST,)

    @pytest.mark.asyncio
    async def test_a_text_holding_the_username_of_the_real_login_is_dropped(
        self, tmp_path: Path
    ) -> None:
        tab, backend, clock, _reads = await _recording(tmp_path)
        tab.dom_html = _FAILED_PAGE.replace(_STAYS, "Too many tries for jsmith2024 today.")
        await self._identifier_step(tab, "78.1", "bob_tester")
        await _post(tab, "78.2", "password=wrong-x7k2q9", content_type=_FORM)
        await clock.advance(2, backend)
        assert backend.mark_failed_login() is True
        await _idle(backend, clock)
        assert backend.marked_failure_candidates() == (
            _TOAST,
            "Too many tries for jsmith2024 today.",
        )
        tab.dom_html = _LANDED
        await self._identifier_step(tab, "78.3", "jsmith2024")
        await _post(tab, "78.4", "password=RealSecret9", content_type=_FORM, respond=False)
        await _stop(backend, tab)
        assert backend.marked_failure_candidates() == (_TOAST,)


class TestOnlyFailureReadsCount:
    @pytest.mark.asyncio
    async def test_enter_pressed_after_the_real_login_suggests_nothing(
        self, tmp_path: Path
    ) -> None:
        """The user forgot Enter, logged in for real, then pressed it: the real login
        is marked, its reads are the landed page, which holds no login, so they are
        ignored and no text is kept."""
        tab, backend, clock, _reads = await _recording(tmp_path)
        tab.dom_html = (
            "<html><body><div class='alert-banner'>Free shipping on orders over 50</div>"
            "<p>Your recent orders</p></body></html>"
        )
        await _post(tab, "89.1", _RIGHT)
        await clock.advance(2, backend)
        assert backend.mark_failed_login() is True
        await _idle(backend, clock)
        await _stop(backend, tab)
        assert backend.marked_failure_candidates() == ()
        assert "_userMarkedFailed" not in _request(backend)


class TestThePageAtThePost:
    """Without the request-time snapshot of the page at the failed post, nothing is
    suggested, whatever the HAR holds of the login page as served."""

    _SHELL = "<html><body><div id='root'></div><script src='/app.js'></script></body></html>"
    _FORM_PAGE = (
        "<html><body><form action='/login' method='post'><input name='username'>"
        "<input type='password' name='password'><button>Sign in</button></form>"
        "<script src='/app.js'></script></body></html>"
    )
    # A script adds a cookie banner to the served form page; the error is no alert.
    _RENDERED = _FORM_PAGE.replace(
        "<form",
        "<div class='cookie-alert'>We use cookies to improve your visit.</div>"
        "<p class='msg'>That did not work, try again.</p><form",
    )

    async def _without_a_snapshot(
        self, tmp_path: Path, served: str, read: str = _FAILED_PAGE
    ) -> NodriverCaptureBackend:
        """A post no snapshot was kept for: no listener snapshot, and the late read
        finds the tab on another page. The login page as served is *served*."""
        tab, backend, clock, _reads = await _recording(tmp_path)
        tab.dom_html = read
        backend._request_map["page"] = {
            "url": LOGIN,
            "response": {"mimeType": "text/html", "body": served},
        }
        await tab.dispatch(_will_be_sent("88.1", inline=_WRONG, url=LOGIN, document_url=LOGIN))
        await tab.dispatch(_response_received("88.1", url=LOGIN))
        await clock.advance(2, backend)
        assert backend._step_before == {}
        assert backend.mark_failed_login() is True
        await _idle(backend, clock)
        await _stop(backend, tab)
        return backend

    @pytest.mark.asyncio
    async def test_a_script_rendered_shell_as_served_suggests_nothing(self, tmp_path: Path) -> None:
        backend = await self._without_a_snapshot(tmp_path, self._SHELL)
        assert backend.marked_failure_candidates() == ()

    @pytest.mark.asyncio
    async def test_a_served_page_holding_the_login_form_suggests_nothing(
        self, tmp_path: Path
    ) -> None:
        backend = await self._without_a_snapshot(tmp_path, _LOGIN_MODAL)
        assert backend.marked_failure_candidates() == ()

    @pytest.mark.asyncio
    async def test_a_cookie_banner_a_script_adds_is_never_suggested(self, tmp_path: Path) -> None:
        """The banner is on the login page the engine reads after every submit, and
        its class holds ``alert``: against the served page it would be the one
        suggestion, and fail every login."""
        backend = await self._without_a_snapshot(tmp_path, self._FORM_PAGE, self._RENDERED)
        assert backend.marked_failure_candidates() == ()


class TestTheStepCloses:
    @pytest.mark.asyncio
    async def test_after_a_skip_no_post_is_held_or_read_for_the_step(self, tmp_path: Path) -> None:
        tab, backend, clock, reads = await _recording(tmp_path)
        await _post(tab, "87.1")
        await clock.advance(1, backend)
        backend.skip_failure_step()
        await clock.advance(0.9, backend)
        # The 1.5 s read due for the post before the skip is cancelled too.
        assert reads == []
        await _post(tab, "87.2", _RIGHT)
        await clock.advance(1.6, backend)
        # Only 87.1's own after-read, 3 s after its response: none 1.5 s after 87.2's.
        assert reads == [3.0]
        assert backend._step_posts == []
        assert backend._step_before == {}
        assert backend._step_reads == {}
        assert backend._step_read_tasks == {}
        assert backend.mark_failed_login() is False
        await _stop(backend, tab)

    @pytest.mark.asyncio
    async def test_the_mark_drops_every_other_post_s_pages_and_reads(self, tmp_path: Path) -> None:
        tab, backend, clock, reads = await _recording(tmp_path)
        await _post(tab, "86.1")
        await clock.advance(1.6, backend)
        assert "86.1" in backend._step_reads
        await _post(tab, "86.2")
        await clock.advance(0.5, backend)
        # The later post's own 1.5 s read is still due; the earlier one's is held.
        assert backend.mark_failed_login() is True
        assert backend._step_reads == {}
        assert backend._step_before == {}
        assert backend._step_posts == []
        await _idle(backend, clock)
        await clock.advance(2, backend)
        assert backend._step_reads == {}
        await _stop(backend, tab)

    @pytest.mark.asyncio
    async def test_the_mark_cancels_another_post_s_pending_read(self, tmp_path: Path) -> None:
        tab, backend, clock, reads = await _recording(tmp_path)
        await _post(tab, "85.1")
        await clock.advance(0.2, backend)
        await _post(tab, "85.2")
        await clock.advance(0.1, backend)
        pending = backend._step_read_tasks["85.1"]
        # Marks 85.2, the last post: 85.1's read, due at 1.5 s, is not wanted.
        assert backend.mark_failed_login() is True
        await _idle(backend, clock)
        assert pending.cancelled()
        await _stop(backend, tab)

    @pytest.mark.asyncio
    async def test_stop_drops_the_raw_pages_of_the_marked_post(self, tmp_path: Path) -> None:
        tab, backend, clock, _reads = await _recording(tmp_path)
        await _post(tab, "84.1")
        await clock.advance(2, backend)
        tab.dom_gate = asyncio.Event()
        backend.mark_failed_login()
        failed = backend._failed
        assert failed is not None
        assert failed.before is not None
        assert failed.first_read is not None
        assert failed.body_text == _WRONG
        # A real login's page waiting for the suggestions to settle.
        failed.drop_on(_LANDED, None)
        assert len(failed.pending) == 1
        await _stop(backend, tab)
        assert (failed.before, failed.first_read, failed.enter_read, failed.body_text) == (
            None,
            None,
            None,
            None,
        )
        assert failed.posted == []
        assert failed.pending == []
        assert backend._failure_step_open is False


class TestDropsWaitingForTheRealLogin:
    """Enter, then a real login sent before the 1.5 s read settles the suggestions:
    the real post's values wait and are applied at the settle."""

    async def _real_login_first(self, tmp_path: Path, real: str) -> NodriverCaptureBackend:
        tab, backend, clock, _reads = await _recording(tmp_path)
        tab.dom_html = _FAILED_PAGE.replace(_STAYS, "No match for jsmith2024 found.")
        await _post(tab, "83.1", json.dumps({"username": "bob_tester", "password": "wrongpass1"}))
        await clock.advance(0.5, backend)
        assert backend.mark_failed_login() is True
        await _idle(backend, clock)
        await _post(tab, "83.2", real, respond=False)
        failed = backend._failed
        assert failed is not None
        assert failed.candidates is None
        assert len(failed.pending) == 1
        await clock.advance(1, backend)
        await _idle(backend, clock)
        assert failed.candidates is not None
        await _stop(backend, tab)
        return backend

    @pytest.mark.asyncio
    async def test_a_text_holding_its_value_is_dropped(self, tmp_path: Path) -> None:
        real = json.dumps({"username": "jsmith2024", "password": "realsecret9"})
        backend = await self._real_login_first(tmp_path, real)
        assert backend.marked_failure_candidates() == (_TOAST,)

    @pytest.mark.asyncio
    async def test_an_unreadable_body_drops_them_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.unit.test_observe_post_data import _unreadable_credential_body

        unreadable = _unreadable_credential_body(monkeypatch)
        backend = await self._real_login_first(tmp_path, unreadable)
        assert backend.marked_failure_candidates() == ()


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
        # No text was kept, so the entry carries no mark at all.
        request = _request(backend)
        assert "_userMarkedFailed" not in request
        assert "_failureTextCandidates" not in request
        assert backend.marked_failure_candidates() == ()

    @pytest.mark.asyncio
    async def test_without_the_step_nothing_is_read_or_kept(self, tmp_path: Path) -> None:
        tab, backend, clock, reads = await _recording(tmp_path, step=False)
        await _post(tab, "98.1")
        await clock.advance(2, backend)
        assert reads == []
        assert backend.mark_failed_login() is False
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
    async def test_the_mark_scrubs_the_failed_password_from_its_own_body(
        self, tmp_path: Path
    ) -> None:
        from graftpunk.observe.run import save_observe_run

        storage, tab, backend, clock = await _saved_recording(tmp_path)
        await _post(tab, "99.1")
        await clock.advance(2, backend)
        backend.mark_failed_login()
        await _idle(backend, clock)
        tab.dom_html = _LANDED
        await save_observe_run(storage, backend, "interactive-final")

        har = (storage.run_dir / "network.har").read_text()
        assert "wrongpass1" not in har
        (entry,) = json.loads(har)["log"]["entries"]
        assert json.loads(entry["request"]["postData"]["text"]) == {
            "username": "jsmith",
            "password": "***REDACTED***",
        }
        assert entry["request"]["_userMarkedFailed"] is True
        assert entry["request"]["_failureTextCandidates"] == [_TOAST, _STAYS]
        assert _TOAST not in _run_text(storage.run_dir).replace(har, "")

    @pytest.mark.asyncio
    async def test_a_failed_body_written_to_a_file_is_scrubbed_there(self, tmp_path: Path) -> None:
        from graftpunk.observe.run import save_observe_run

        storage, tab, backend, clock = await _saved_recording(tmp_path, max_body_size=10)
        await _post(tab, "99.2")
        await clock.advance(2, backend)
        backend.mark_failed_login()
        await _idle(backend, clock)
        await save_observe_run(storage, backend, "interactive-final")

        (entry,) = json.loads((storage.run_dir / "network.har").read_text())["log"]["entries"]
        request = entry["request"]
        written = (storage.run_dir / request["_postDataFile"]).read_bytes()
        assert json.loads(written) == {"username": "jsmith", "password": "***REDACTED***"}
        assert request["_postDataSize"] == len(written)
        assert "wrongpass1" not in _run_text(storage.run_dir)


class TestARedirectedFailedPost:
    """A 307 or 308 answer to the failed post makes Chrome send the same body again
    under the same request id; that entry holds the wrong password too."""

    _AGAIN = LOGIN + "/"
    _JSON = {"Content-Type": "application/json"}

    @pytest.mark.asyncio
    async def test_a_resend_before_the_mark_is_scrubbed(self, tmp_path: Path) -> None:
        from graftpunk.observe.run import save_observe_run

        storage, tab, backend, clock = await _saved_recording(tmp_path)
        await tab.dispatch(_binding_called(_LOGIN_MODAL, time_ms=_POST_MS - 40))
        await tab.dispatch(_will_be_sent("91.1", inline=_WRONG, url=LOGIN, headers=self._JSON))
        await tab.dispatch(
            _will_be_sent(
                "91.1", inline=_WRONG, url=self._AGAIN, redirect_from=LOGIN, headers=self._JSON
            )
        )
        await tab.dispatch(_response_received("91.1", url=self._AGAIN))
        await clock.advance(2, backend)
        assert backend.mark_failed_login() is True
        await _idle(backend, clock)
        tab.dom_html = _LANDED
        await save_observe_run(storage, backend, "interactive-final")

        entries = json.loads((storage.run_dir / "network.har").read_text())["log"]["entries"]
        assert [entry["request"]["url"] for entry in entries] == [LOGIN, self._AGAIN]
        for entry in entries:
            assert json.loads(entry["request"]["postData"]["text"]) == {
                "username": "jsmith",
                "password": "***REDACTED***",
            }
        assert "wrongpass1" not in _run_text(storage.run_dir)

    @pytest.mark.asyncio
    async def test_a_resend_after_the_mark_is_scrubbed_at_stop(self, tmp_path: Path) -> None:
        tab, backend, clock, _reads = await _recording(tmp_path)
        await _post(tab, "92.1", respond=False)
        await clock.advance(2, backend)
        assert backend.mark_failed_login() is True
        await tab.dispatch(
            _will_be_sent(
                "92.1", inline=_WRONG, url=self._AGAIN, redirect_from=LOGIN, headers=self._JSON
            )
        )
        await tab.dispatch(_response_received("92.1", url=self._AGAIN))
        await _idle(backend, clock)
        await _stop(backend, tab)
        entries = backend.get_har_entries()
        assert len(entries) == 2
        assert "wrongpass1" not in json.dumps(entries)
        assert backend._failed is not None
        assert backend._failed.secrets == ()

    @pytest.mark.asyncio
    async def test_two_resends_before_the_mark_are_scrubbed(self, tmp_path: Path) -> None:
        """Two 307/308 hops (an HTTPS upgrade, then a trailing slash): the middle
        resend is recorded as ``<id>:redirect:<n>``, neither the failed entry nor
        ``<id>``."""
        tab, backend, clock, _reads = await _recording(tmp_path)
        await _post(tab, "94.1", respond=False)
        for url, before in ((self._AGAIN, LOGIN), (self._AGAIN + "x", self._AGAIN)):
            await tab.dispatch(
                _will_be_sent(
                    "94.1", inline=_WRONG, url=url, redirect_from=before, headers=self._JSON
                )
            )
        await tab.dispatch(_response_received("94.1", url=self._AGAIN + "x"))
        await clock.advance(2, backend)
        assert backend.mark_failed_login() is True
        await _idle(backend, clock)
        await _stop(backend, tab)
        entries = backend.get_har_entries()
        assert [entry["request"]["url"] for entry in entries] == [
            LOGIN,
            self._AGAIN,
            self._AGAIN + "x",
        ]
        assert "wrongpass1" not in json.dumps(entries)

    @pytest.mark.asyncio
    async def test_a_resend_after_the_mark_is_scrubbed_at_a_stop_with_no_tab(
        self, tmp_path: Path
    ) -> None:
        """The browser window closed before stop: the stop with no tab scrubs too."""
        tab, backend, clock, _reads = await _recording(tmp_path)
        await _post(tab, "95.1", respond=False)
        await clock.advance(2, backend)
        assert backend.mark_failed_login() is True
        await tab.dispatch(
            _will_be_sent(
                "95.1", inline=_WRONG, url=self._AGAIN, redirect_from=LOGIN, headers=self._JSON
            )
        )
        await tab.dispatch(_response_received("95.1", url=self._AGAIN))
        await _idle(backend, clock)
        backend._get_tab = lambda: None
        await backend.stop_capture_async()
        entries = backend.get_har_entries()
        assert len(entries) == 2
        assert "wrongpass1" not in json.dumps(entries)
        assert backend._failed is not None
        assert backend._failed.secrets == ()

    @pytest.mark.asyncio
    async def test_another_request_id_is_not_touched(self, tmp_path: Path) -> None:
        tab, backend, clock, _reads = await _recording(tmp_path)
        await _post(tab, "93.1")
        await clock.advance(2, backend)
        assert backend.mark_failed_login() is True
        await _idle(backend, clock)
        tab.dom_html = _LANDED
        other = json.dumps({"username": "jsmith", "password": "wrongpass1"})
        await tab.dispatch(_will_be_sent("93.2", inline=other, url=self._AGAIN, headers=self._JSON))
        await _stop(backend, tab)
        failed, later = (entry["request"] for entry in backend.get_har_entries())
        assert "wrongpass1" not in failed["postData"]["text"]
        assert later["postData"]["text"] == other


async def _saved_recording(
    tmp_path: Path, **kwargs: Any
) -> tuple[Any, FakeTab, NodriverCaptureBackend, FakeClock]:
    """A recording with the step that writes to a run directory, for save_observe_run."""
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
        **kwargs,
    )
    backend._now, backend._sleep = clock.time, clock.sleep
    await backend.start_capture_async()
    return storage, tab, backend, clock


class TestTheThrowawayScrub:
    """The wrong password is scrubbed only from the failed post's own body, and only
    under a password-like name: a throwaway such as ``password`` or ``login`` leaves
    the real login's body (an OAuth ``grant_type=password`` included), the field
    names, and the URLs intact."""

    @staticmethod
    def _body(password: str, content_type: str) -> str:
        if content_type == _FORM:
            return f"grant_type=password&action=login&login=jsmith&password={password}"
        return json.dumps(
            {"grant_type": "password", "action": "login", "login": "jsmith", "password": password}
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "content_type", "scrubbed"),
        [
            (
                "username=jsmith\r\npassword=pa&ss9\r\n",
                "text/plain",
                "username=jsmith\r\npassword=***REDACTED***\r\n",
            ),
            (
                '--b\r\nContent-Disposition: form-data; name="username"\r\n\r\njsmith\r\n'
                '--b\r\nContent-Disposition: form-data; name="password"\r\n\r\npa&ss9\r\n'
                "--b--\r\n",
                "multipart/form-data; boundary=b",
                '--b\r\nContent-Disposition: form-data; name="username"\r\n\r\njsmith\r\n'
                '--b\r\nContent-Disposition: form-data; name="password"\r\n\r\n'
                "***REDACTED***\r\n--b--\r\n",
            ),
        ],
    )
    async def test_each_entry_is_split_by_its_own_content_type(
        self, tmp_path: Path, body: str, content_type: str, scrubbed: str
    ) -> None:
        """Read as a form, neither body gives ``pa&ss9`` whole, so the scrub must
        split each entry by the type it declared. The recorder marks only a post
        whose field names it reads (JSON or a form), so the failed post is set here
        by hand."""
        tab, backend, _clock, _reads = await _recording(tmp_path)
        await tab.dispatch(
            _will_be_sent("71.1", inline=body, url=LOGIN, headers={"Content-Type": content_type})
        )
        await tab.dispatch(_response_received("71.1", url=LOGIN))
        data = backend._request_map["71.1"]
        failed = capture._FailedLogin(capture._StepPost("71.1", data, body, content_type), [])
        failed.secrets = ("pa&ss9",)
        backend._scrub_failed_post(failed)
        (entry,) = backend.get_har_entries()
        assert entry["request"]["postData"]["text"] == scrubbed
        assert "pa&ss9" not in json.dumps(entry)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("throwaway", ["password", "login"])
    @pytest.mark.parametrize("content_type", [_FORM, "application/json"])
    async def test_a_dictionary_word_leaves_the_run_readable(
        self, tmp_path: Path, throwaway: str, content_type: str
    ) -> None:
        from graftpunk.har.digest import DigestSource, digest
        from graftpunk.observe.run import save_observe_run

        storage, tab, backend, clock = await _saved_recording(tmp_path)
        await _post(tab, "70.1", self._body(throwaway, content_type), content_type=content_type)
        await clock.advance(2, backend)
        assert backend.mark_failed_login() is True
        await _idle(backend, clock)
        tab.dom_html = _LANDED
        real = self._body("RealSecret9", content_type)
        await _post(tab, "70.2", real, content_type=content_type)
        await clock.advance(3, backend)
        await _idle(backend, clock)
        kept = backend.marked_failure_candidates()
        await save_observe_run(storage, backend, "interactive-final")

        har = storage.run_dir / "network.har"
        failed, right = (
            entry["request"] for entry in json.loads(har.read_text())["log"]["entries"]
        )
        assert failed["url"] == right["url"] == LOGIN
        assert failed["postData"]["text"] == self._body("***REDACTED***", content_type)
        assert right["postData"]["text"] == real
        result = digest(DigestSource(har_path=har))
        assert any(set(form.fields) >= {"password"} for form in result.login_forms)
        assert kept
        assert result.failure_candidates == kept


# The prompt: gp observe interactive --wrong-password-first reads Enter on stdin
# inside the asyncio loop, beside the SIGINT handler that stops the recording. A pipe
# stands in for the terminal; the loop's own reader and signal handler are real.


class _FakeTerminal:
    """A stdin whose file descriptor is a pipe's read end."""

    def __init__(self, *, tty: bool = True) -> None:
        self.read_fd, self.write_fd = os.pipe()
        self.tty = tty

    def fileno(self) -> int:
        return self.read_fd

    def isatty(self) -> bool:
        return self.tty

    def type(self, data: bytes) -> None:
        os.write(self.write_fd, data)

    def close_input(self) -> None:
        os.close(self.write_fd)

    def close(self) -> None:
        for fd in (self.read_fd, self.write_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


class _Untouchable:
    """A stdin no part of the command may use."""

    def fileno(self) -> int:
        raise AssertionError("stdin was read")

    def isatty(self) -> bool:
        raise AssertionError("stdin was read")


async def _until(condition: Callable[[], bool]) -> None:
    """Until *condition* holds; the loop turns between checks let the reader and the
    signal handler run."""
    for _ in range(2000):
        if condition():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("the condition never held")


async def _turns(count: int = 20) -> None:
    for _ in range(count):
        await asyncio.sleep(0)


_THROWAWAY = "wrong-x7k2q9"


class _Interactive:
    """run_observe_interactive with the browser set-up, the save, and the stop
    replaced; the console writes to a buffer."""

    def __init__(self, marks: list[bool] | None = None) -> None:
        self.backend = MagicMock()
        self.backend.mark_failed_login = MagicMock(side_effect=marks or [True])
        self.backend.close_after_reads = AsyncMock()
        self.backend.marked_failure_candidates = MagicMock(return_value=None)
        self.out = StringIO()
        self.console = Console(file=self.out, no_color=True, width=200)
        self.setup = AsyncMock(
            return_value=ObserveSession(
                browser=MagicMock(),
                handle=MagicMock(),
                tab=MagicMock(),
                storage=MagicMock(),
                backend=self.backend,
            )
        )
        self.save = AsyncMock()

    @property
    def text(self) -> str:
        return " ".join(self.out.getvalue().split())

    def patches(self) -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        where = "graftpunk.cli.observe_browser"
        stack.enter_context(patch(f"{where}.setup_observe_session", self.setup))
        stack.enter_context(patch(f"{where}.save_observe_run", self.save))
        stack.enter_context(patch(f"{where}.stop_observe_browser"))
        stack.enter_context(patch(f"{where}.console", self.console))
        stack.enter_context(patch(f"{where}._throwaway_password", lambda: _THROWAWAY))
        return stack

    def start(self, stdin: Any, *, step: bool = True) -> asyncio.Future[None]:
        kwargs: dict[str, Any] = {"session_name": None, "stdin": stdin}
        if step:
            kwargs["wrong_password_first"] = True
        return asyncio.ensure_future(
            run_observe_interactive("myshop", "https://myshop.example/login", 1024, **kwargs)
        )


def _ctrl_c() -> None:
    signal.raise_signal(signal.SIGINT)


class TestThePrompt:
    @pytest.mark.asyncio
    async def test_enter_marks_the_failed_post_and_ctrl_c_saves(
        self,
    ) -> None:
        run, terminal = _Interactive(), _FakeTerminal()
        try:
            with run.patches():
                task = run.start(terminal)
                await _until(lambda: "press Enter here" in run.text)
                assert f"with this throwaway wrong password, {_THROWAWAY} (never" in run.text
                assert "never your real one with a typo" in run.text
                run.backend.mark_failed_login.assert_not_called()
                terminal.type(b"\n")
                await _until(lambda: "Marked" in run.text)
                assert "log in for real" in run.text.split("Marked")[-1]
                _ctrl_c()
                await task
        finally:
            terminal.close()
        assert run.setup.await_args.kwargs["failure_step"] is True
        run.backend.mark_failed_login.assert_called_once_with()
        # The recorder scrubbed the failed post's own body at the mark; the save
        # scrubs the run of its credential posts' values, as without the flag (#318).
        assert run.save.await_args.kwargs == {
            "console": run.console,
            "scrub_credential_posts": True,
        }

    @pytest.mark.asyncio
    async def test_enter_before_any_credential_post_asks_again(self) -> None:
        run, terminal = _Interactive([False, True]), _FakeTerminal()
        try:
            with run.patches():
                task = run.start(terminal)
                await _until(lambda: "press Enter here" in run.text)
                terminal.type(b"\n")
                await _until(lambda: "No credential post yet" in run.text)
                assert "Marked" not in run.text
                terminal.type(b"typed by mistake\n")
                await _until(lambda: "Marked" in run.text)
                assert run.backend.mark_failed_login.call_count == 2
                _ctrl_c()
                await task
        finally:
            terminal.close()

    @pytest.mark.asyncio
    async def test_input_after_the_mark_is_not_read(self) -> None:
        run, terminal = _Interactive([True]), _FakeTerminal()
        try:
            with run.patches():
                task = run.start(terminal)
                await _until(lambda: "press Enter here" in run.text)
                terminal.type(b"\n")
                await _until(lambda: "Marked" in run.text)
                terminal.type(b"\n\n")
                await _turns()
                _ctrl_c()
                await task
        finally:
            terminal.close()
        run.backend.mark_failed_login.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_ctrl_c_during_the_step_saves_and_stops_reading_stdin(self) -> None:
        run, terminal = _Interactive(), _FakeTerminal()
        try:
            with run.patches():
                task = run.start(terminal)
                await _until(lambda: "press Enter here" in run.text)
                _ctrl_c()
                await task
                # The loop no longer watches stdin: there is no reader left to remove.
                assert asyncio.get_running_loop().remove_reader(terminal.read_fd) is False
        finally:
            terminal.close()
        run.backend.mark_failed_login.assert_not_called()
        run.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_end_of_input_skips_the_step_and_keeps_recording(self) -> None:
        run, terminal = _Interactive(), _FakeTerminal()
        try:
            with run.patches():
                task = run.start(terminal)
                await _until(lambda: "press Enter here" in run.text)
                terminal.close_input()
                await _until(lambda: "skipped" in run.text)
                await _turns()
                assert not task.done()
                _ctrl_c()
                await task
        finally:
            terminal.close()
        run.backend.mark_failed_login.assert_not_called()
        # The backend is told, so it holds and reads nothing more for the step.
        run.backend.skip_failure_step.assert_called_once_with()
        run.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_text_with_no_newline_then_end_of_input_marks_nothing(self) -> None:
        run, terminal = _Interactive(), _FakeTerminal()
        try:
            with run.patches():
                task = run.start(terminal)
                await _until(lambda: "press Enter here" in run.text)
                terminal.type(b"typed but no Enter")
                await _turns()
                terminal.close_input()
                await _until(lambda: "skipped" in run.text)
                _ctrl_c()
                await task
        finally:
            terminal.close()
        run.backend.mark_failed_login.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_stdin_that_is_no_terminal_refuses_before_the_browser_opens(self) -> None:
        run, terminal = _Interactive(), _FakeTerminal(tty=False)
        # Without the refusal the browser would open here, and the test fails at once
        # instead of waiting on a pipe nobody writes to.
        run.setup.side_effect = AssertionError("the browser was opened")
        try:
            with run.patches():
                task = run.start(terminal)
                with pytest.raises(typer.Exit) as exited:
                    await task
        finally:
            terminal.close()
        assert exited.value.exit_code == 1
        assert "needs a terminal" in run.text
        run.setup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_without_the_flag_stdin_is_untouched_and_the_run_is_main_s(self) -> None:
        run = _Interactive()
        with run.patches():
            task = run.start(_Untouchable(), step=False)
            await _until(lambda: "Recording..." in run.text)
            _ctrl_c()
            await task
        assert run.setup.await_args.kwargs["failure_step"] is False
        # main's lines, word for word.
        assert run.text == (
            "Recording... press Ctrl+C to stop and save Tip: Use Ctrl+C in this terminal "
            "for a complete capture (screenshot, page source, and network data). Closing "
            "the browser window will preserve network data only. Recording stopped. "
            "Saving capture..."
        )
        run.backend.mark_failed_login.assert_not_called()
        assert run.save.await_args.kwargs == {
            "console": run.console,
            "scrub_credential_posts": True,
        }

    @pytest.mark.asyncio
    async def test_a_chrome_sharing_the_session_gets_a_warning_in_place_of_the_tip(
        self,
    ) -> None:
        """#333: when Chrome did not start in a session of its own, the Ctrl+C
        stops Chrome too, so no complete capture is promised."""
        run = _Interactive()
        run.setup.return_value = dataclasses.replace(run.setup.return_value, own_session=False)
        with run.patches():
            task = run.start(_Untouchable(), step=False)
            await _until(lambda: "Recording..." in run.text)
            _ctrl_c()
            await task
        assert run.text == (
            "Chrome did not start in a session of its own, so a Ctrl+C in this terminal "
            "also stops Chrome, and the run's page source and screenshot can be lost. "
            "Recording... press Ctrl+C to stop and save Recording stopped. Saving capture..."
        )


class TestAnEmptyMark:
    async def _marked_then_stopped(
        self, kept: tuple[str, ...] | None
    ) -> tuple[_Interactive, list[str]]:
        run, terminal = _Interactive(), _FakeTerminal()
        run.backend.marked_failure_candidates.return_value = kept
        order: list[str] = []
        run.backend.close_after_reads.side_effect = lambda: order.append("closed")
        run.save.side_effect = lambda *args, **kwargs: order.append(
            "saved after the line" if "No failure text" in run.text else "saved"
        )
        try:
            with run.patches():
                task = run.start(terminal)
                await _until(lambda: "press Enter here" in run.text)
                terminal.type(b"\n")
                await _until(lambda: "Marked" in run.text)
                _ctrl_c()
                await task
        finally:
            terminal.close()
        return run, order

    @pytest.mark.asyncio
    async def test_a_mark_that_kept_no_text_says_so_before_the_save(self) -> None:
        run, order = await self._marked_then_stopped(())
        assert "No failure text was kept from the wrong password" in run.text
        # What the reads kept is read once they are closed, and said before the save.
        assert order == ["closed", "saved after the line"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kept", [(_TOAST,), None])
    async def test_a_mark_with_text_or_no_mark_says_nothing(
        self, kept: tuple[str, ...] | None
    ) -> None:
        run, order = await self._marked_then_stopped(kept)
        assert "No failure text" not in run.text
        assert order == ["closed", "saved"]


class TestTheThrowaway:
    def test_it_is_wrong_and_six_random_letters_and_digits(self) -> None:
        import re

        from graftpunk.cli.observe_browser import _throwaway_password

        offered = {_throwaway_password() for _ in range(20)}
        assert all(re.fullmatch(r"wrong-[a-z0-9]{6}", value) for value in offered)
        # From secrets: twenty draws are not one value.
        assert len(offered) > 1

    def test_the_draw_comes_from_secrets(self) -> None:
        from graftpunk.cli import observe_browser

        with patch.object(observe_browser.secrets, "choice", side_effect=list("ab12cd")):
            assert observe_browser._throwaway_password() == "wrong-ab12cd"


class TestTheFlag:
    @pytest.mark.parametrize("flag", [True, False])
    def test_the_command_passes_it_on(self, flag: bool) -> None:
        args = ["observe", "--no-session", "interactive", "https://myshop.example/login"]
        with (
            patch(
                "graftpunk.cli.observe_commands.run_observe_interactive", new_callable=MagicMock
            ) as interactive,
            patch("graftpunk.cli.observe_commands.asyncio"),
        ):
            result = CliRunner().invoke(app, [*args, "--wrong-password-first"] if flag else args)
        assert result.exit_code == 0, result.output
        assert interactive.call_args.kwargs["wrong_password_first"] is flag

    def test_the_help_names_it(self) -> None:
        result = CliRunner().invoke(app, ["observe", "interactive", "--help"])
        assert "--wrong-password-first" in strip_ansi(result.output)
