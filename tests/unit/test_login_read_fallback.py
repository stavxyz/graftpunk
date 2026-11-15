"""The nodriver post-submit wait reads the page itself, not nodriver's copies (#392).

Two things kept a nodriver login from seeing the page it landed on:

- ``Tab.url`` in nodriver 0.50.6 is ``Tab.target.url``, a copy of the target's
  info that nothing refreshes while the poll runs, so every tick read the URL
  from before the submit. The tests here use nodriver's own ``Tab`` class over a
  fake CDP endpoint (:class:`_CdpTab`), so ``Tab.url`` behaves as nodriver's does.
- ``Tab.get_content`` and ``Tab.query_selector`` carry a node id from one CDP
  call to the next. In #392 ``get_content`` failed with "No node found for given
  backend id" on every tick; why is unverified, and the fake fails every ``DOM.*``
  call that way.

The wait reads each tick's URL, success element and document in one
``Runtime.evaluate`` that carries no node id, and falls back to ``location.href``
(``graftpunk.plugins.login_settle.page_url_nodriver``) and nodriver's node-id reads
when that fails.

A browser that answers each command slowly (#395) is judged on the page it shows:
the tick costs it one round trip, and the no-success-signal wait lets the read in
flight when its grace ends answer within ``LoginConfig.timeout``. Those tests run
on a virtual clock (``_run_virtual``), so the delays cost no real time.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nodriver import cdp
from nodriver.core.connection import ProtocolException
from nodriver.core.tab import Tab

from graftpunk.exceptions import PluginError
from graftpunk.plugins import SitePlugin, login_settle
from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep
from graftpunk.plugins.login_settle import (
    LoginVerdict,
    _wait_for_login_signal_nodriver,
    _warn_login_page_unreadable,
    _watch_for_failure_nodriver,
    recording_login_verdict,
    wait_for_login_outcome_nodriver,
)
from tests.unit.test_login_engine import (
    _LOGIN_CREDENTIALS,
    _nodriver_session_for,
    _ScriptedNodriverTab,
    _warning_kwargs,
    scripted_tab_ticks,  # noqa: F401  (autouse: tells the scripted tab its ticks)
)
from tests.unit.test_login_poll_bounds import _FOREVER, _run_virtual

IDP = "https://idp.example.com/sso/login"
DASHBOARD = "https://myshop.example/dashboard"
_NO_NODE = {"code": -32000, "message": "No node found for given backend id"}


class _CdpTab(Tab):
    """nodriver 0.50.6's ``Tab``, with ``send`` answering as a page's CDP endpoint
    would; nothing else is overridden, so ``Tab.url``, ``get_content``,
    ``query_selector`` and ``evaluate`` are nodriver's own.

    The page is at ``IDP`` and lands on ``DASHBOARD`` (showing ``html_after``, and
    the ``.dashboard`` element when ``element_after``) once the loop's clock passes
    ``lands_at``. Every ``DOM.*`` call fails as in #392 while ``node_ids_fail``;
    every ``Runtime.evaluate`` raises *evaluate_error* when one is set.
    ``Target.getTargetInfo`` reports the page's current URL, which is how nodriver
    would refresh ``Tab.url``. Every command answers *delay* seconds after it is
    sent. ``methods`` records the CDP methods sent.
    """

    def __init__(
        self,
        *,
        lands_at: float,
        html_after: str = "<html>Welcome</html>",
        element_after: bool = False,
        node_ids_fail: bool = True,
        evaluate_error: Exception | None = None,
        page_read: Any = None,
        tab_url: str = IDP,
        html_before: str = "<html>Sign in</html>",
        delay: float = 0.0,
    ) -> None:
        info = cdp.target.TargetInfo(
            target_id=cdp.target.TargetID("T"),
            type_="page",
            title="",
            url=tab_url,
            attached=True,
            can_access_opener=False,
        )
        super().__init__(target=info, parent=None)
        self.session_id = "S"
        self.lands_at = lands_at
        self.html_after = html_after
        self.element_after = element_after
        self.node_ids_fail = node_ids_fail
        self.evaluate_error = evaluate_error
        self.page_read = page_read
        self.html_before = html_before
        self.delay = delay
        self.methods: list[str] = []
        self.expressions: list[str] = []

    def _landed(self) -> bool:
        return asyncio.get_running_loop().time() >= self.lands_at

    def current_url(self) -> str:
        return DASHBOARD if self._landed() else IDP

    def _html(self) -> str:
        return self.html_after if self._landed() else self.html_before

    def _evaluate(self, expression: str) -> Any:
        if self.evaluate_error is not None:
            raise self.evaluate_error
        self.expressions.append(expression)
        if "graftpunk:page-read" in expression:
            if self.page_read is not None:
                return self.page_read
            found = None
            if json.dumps(".dashboard") in expression:
                found = self.element_after and self._landed()
            return json.dumps(
                {
                    "html": self._html(),
                    "href": self.current_url(),
                    "found": found,
                    "rejected": False,
                }
            )
        if "graftpunk:href" in expression:
            return self.current_url()
        if expression == "document.readyState":
            return "complete"
        # The selector check and anything else: a false answer.
        return False

    async def send(self, cdp_obj: Any, _is_update: bool = False, **kwargs: Any) -> Any:
        request = next(cdp_obj)
        method = request["method"]
        self.methods.append(method)
        if self.delay:
            await asyncio.sleep(self.delay)
        if method.startswith("DOM.") and self.node_ids_fail:
            raise ProtocolException(dict(_NO_NODE))
        if method == "DOM.getDocument":
            result = {
                "root": {
                    "nodeId": 1,
                    "backendNodeId": 1,
                    "nodeType": 9,
                    "nodeName": "#document",
                    "localName": "",
                    "nodeValue": "",
                }
            }
        elif method == "DOM.getOuterHTML":
            result = {"outerHTML": self._html()}
        elif method == "Runtime.evaluate":
            value = self._evaluate(request["params"]["expression"])
            kind = {str: "string", bool: "boolean"}.get(type(value), "object")
            result: dict[str, Any] = {"result": {"type": kind, "value": value}}
        elif method == "Target.getTargetInfo":
            result = {
                "targetInfo": {
                    "targetId": "T",
                    "type": "page",
                    "title": "",
                    "url": self.current_url(),
                    "attached": True,
                    "canAccessOpener": False,
                }
            }
        else:
            result = {}
        try:
            cdp_obj.send(result)
        except StopIteration as stop:
            return stop.value
        raise AssertionError(f"{method} did not finish after one response")


def _config(**overrides: Any) -> LoginConfig:
    values: dict[str, Any] = {
        "steps": [LoginStep(fields={"username": "#user"}, submit="#submit")],
        "url": "/login",
        "failure": "Invalid credentials",
        "timeout": 2.0,
        "settle": 0.0,
    }
    values.update(overrides)
    return LoginConfig(**values)


async def _signal_wait(tab: _CdpTab, config: LoginConfig) -> bool:
    loop = asyncio.get_running_loop()
    return await _wait_for_login_signal_nodriver(
        tab=tab,
        login_config=config,
        failure_text=config.failure or "",
        site_name="myshop",
        deadline=loop.time() + config.timeout,
        pre_submit_url=IDP,
    )


def _warnings(mock_log: MagicMock, event: str) -> list[Any]:
    return [c for c in mock_log.warning.call_args_list if c[0][0] == event]


class TestTabUrlIsNodriversStaleCopy:
    async def test_nodrivers_tab_url_keeps_the_url_from_before_the_navigation(self) -> None:
        """The model, checked: nodriver's own Tab.url does not follow the page until
        something refreshes the target's info."""
        loop = asyncio.get_running_loop()
        tab = _CdpTab(lands_at=loop.time())

        assert tab.url == IDP
        assert (await login_settle.page_url_nodriver(tab)).url == DASHBOARD
        tab.target = await tab.send(cdp.target.get_target_info(cdp.target.TargetID("T")))
        assert tab.url == DASHBOARD

    async def test_a_page_url_read_that_fails_settles_for_tab_url(self) -> None:
        loop = asyncio.get_running_loop()
        tab = _CdpTab(lands_at=loop.time(), evaluate_error=ConnectionResetError("gone"))

        with patch.object(login_settle, "LOG"):
            page_url = await login_settle.page_url_nodriver(tab)

        assert page_url == login_settle.PageUrl(IDP, False)

    async def test_a_page_url_read_that_hangs_is_abandoned_not_cancelled(self) -> None:
        loop = asyncio.get_running_loop()
        tab = _CdpTab(lands_at=loop.time())
        released = asyncio.Event()
        outcome: list[str] = []

        async def hanging(expression: str, return_by_value: bool = False) -> Any:
            try:
                await released.wait()
            except asyncio.CancelledError:
                outcome.append("cancelled")
                raise
            outcome.append("answered")
            return DASHBOARD

        tab.evaluate = hanging  # type: ignore[method-assign]
        with patch.object(login_settle, "LOG"):
            page_url = await login_settle.page_url_nodriver(tab, within=0.05)
        released.set()
        for _ in range(5):
            await asyncio.sleep(0)

        assert page_url == login_settle.PageUrl(IDP, False)
        assert outcome == ["answered"]


class TestTheSignalPollReadsThePage:
    async def test_a_login_that_lands_on_the_success_url_passes(self) -> None:
        """The reviewers' reproduction: every node-id read fails, the page lands on
        the success URL 0.3 s in, and Tab.url never moves."""
        loop = asyncio.get_running_loop()
        tab = _CdpTab(lands_at=loop.time() + 0.3)

        with patch.object(login_settle, "LOG") as mock_log:
            result = await _signal_wait(tab, _config(success_url="*/dashboard*"))

        assert result is True
        assert tab.url == IDP
        assert _warnings(mock_log, "login_signal_timeout") == []

    async def test_a_tick_is_one_evaluate_when_the_page_answers_it(self) -> None:
        """#395: the URL, the element and the document come back in one command, and
        nodriver's node-id reads are never made while the evaluate serves."""
        loop = asyncio.get_running_loop()
        tab = _CdpTab(lands_at=loop.time() + 0.3, node_ids_fail=False)

        with patch.object(login_settle, "LOG"):
            result = await _signal_wait(tab, _config(success_url="*/dashboard*"))

        assert result is True
        assert tab.methods
        assert set(tab.methods) == {"Runtime.evaluate"}
        assert all("graftpunk:page-read" in e for e in tab.expressions)
        assert len(tab.expressions) == len(tab.methods)

    async def test_a_success_url_login_passes_on_the_node_id_reads(self) -> None:
        """The fallback: a page whose one-evaluate read answers no record is read
        through nodriver's calls."""
        loop = asyncio.get_running_loop()
        tab = _CdpTab(lands_at=loop.time() + 0.3, node_ids_fail=False, page_read="not json")

        with patch.object(login_settle, "LOG"):
            result = await _signal_wait(tab, _config(success_url="*/dashboard*"))

        assert result is True
        assert "DOM.getOuterHTML" in tab.methods
        assert any("graftpunk:href" in e for e in tab.expressions)

    async def test_a_url_only_tick_reads_the_document_only_at_the_landing_url(self) -> None:
        """No failure text: the document is asked for only once the URL wants it,
        in a second evaluate on that tick."""
        loop = asyncio.get_running_loop()
        tab = _CdpTab(lands_at=loop.time() + 0.7)

        with patch.object(login_settle, "LOG"):
            result = await _signal_wait(
                tab, _config(success_url="*/dashboard*", failure=None, timeout=5.0)
            )

        assert result is True
        page_reads = [e for e in tab.expressions if "graftpunk:page-read" in e]
        with_html = [i for i, e in enumerate(page_reads) if "const html = true" in e]
        assert len(with_html) == 1
        # The document comes in a second read, right after the URL-only read of the
        # same tick, and that tick is the last one.
        assert with_html[0] >= 1
        assert "const html = false" in page_reads[with_html[0] - 1]
        assert with_html[0] == len(page_reads) - 1

    async def test_a_success_element_read_without_node_ids_passes(self) -> None:
        loop = asyncio.get_running_loop()
        tab = _CdpTab(lands_at=loop.time() + 0.3, element_after=True)

        with patch.object(login_settle, "LOG"):
            result = await _signal_wait(tab, _config(success=".dashboard"))

        assert result is True
        page_reads = [e for e in tab.expressions if "graftpunk:page-read" in e]
        assert page_reads
        # The selector reaches the page as a JSON string literal.
        assert all(json.dumps(".dashboard") in e for e in page_reads)

    async def test_failure_text_read_without_node_ids_fails_the_login(self) -> None:
        loop = asyncio.get_running_loop()
        tab = _CdpTab(lands_at=loop.time() + 0.3, html_after="<html>Invalid credentials</html>")

        with patch.object(login_settle, "LOG") as mock_log:
            result = await _signal_wait(tab, _config(success_url="*/dashboard*"))

        assert result is False
        assert _warning_kwargs(mock_log, "login_failure_text_detected")["text"] == (
            "Invalid credentials"
        )

    async def test_the_verdict_comes_from_the_read_at_the_deadline(self) -> None:
        """The page lands on the success URL only as the deadline passes: the last
        tick, at the deadline, reads it."""
        loop = asyncio.get_running_loop()
        config = _config(success_url="*/dashboard*", timeout=1.0)
        tab = _CdpTab(lands_at=loop.time() + config.timeout - 0.01)

        with patch.object(login_settle, "LOG") as mock_log:
            result = await _signal_wait(tab, config)

        assert result is True
        assert _warnings(mock_log, "login_signal_timeout") == []

    async def test_a_timeout_names_the_page_s_url_not_tab_url(self) -> None:
        loop = asyncio.get_running_loop()
        tab = _CdpTab(lands_at=loop.time() + 0.2)

        with patch.object(login_settle, "LOG") as mock_log:
            result = await _signal_wait(tab, _config(success_url="*/account*", timeout=0.6))

        assert result is False
        assert _warning_kwargs(mock_log, "login_signal_timeout")["url"] == DASHBOARD

    async def test_a_browser_that_answers_nothing_is_unreadable_and_says_how(self) -> None:
        """Tab.url's stale copy is not the browser answering: a poll whose every read
        fails is reported as unreadable, with the count and the exception types."""
        loop = asyncio.get_running_loop()
        tab = _CdpTab(lands_at=loop.time(), evaluate_error=ConnectionResetError("socket text"))

        with patch.object(login_settle, "LOG") as mock_log:
            result = await _signal_wait(tab, _config(success_url="*/dashboard*", timeout=1.0))

        assert result is False
        assert _warnings(mock_log, "login_signal_timeout") == []
        warning = _warning_kwargs(mock_log, "login_page_unreadable")
        assert warning["failed_ticks"] == 2
        # The one-evaluate read fails first, then the node-id read the tick falls
        # back to, whose error the warning carries.
        assert warning["error_types"] == "ConnectionResetError, ProtocolException"
        assert "No node found" in warning["error"]
        for field in ("failed_ticks", "error_types"):
            assert "socket text" not in str(warning[field])
            assert "No node found" not in str(warning[field])

    async def test_a_selector_the_page_read_rejects_is_still_reported(self) -> None:
        loop = asyncio.get_running_loop()
        tab = _CdpTab(
            lands_at=loop.time(),
            page_read=json.dumps({"html": "", "href": IDP, "found": None, "rejected": True}),
        )

        with patch.object(login_settle, "LOG"), pytest.raises(PluginError) as raised:
            await _signal_wait(tab, _config(success=".dashboard"))

        assert "not a valid CSS selector" in str(raised.value)
        assert "'.dashboard'" in str(raised.value)

    async def test_a_page_read_of_the_wrong_shape_leaves_the_tick_unreadable(self) -> None:
        loop = asyncio.get_running_loop()
        tab = _CdpTab(lands_at=loop.time(), page_read=json.dumps({"found": True, "value": None}))

        with patch.object(login_settle, "LOG") as mock_log:
            result = await _signal_wait(tab, _config(success=".dashboard", timeout=0.6))

        assert result is False
        warning = _warning_kwargs(mock_log, "login_signal_timeout")
        assert "No node found" in warning["error"]


class TestTheGraceWindowReadsThePage:
    async def test_a_clean_page_read_without_node_ids_passes(self) -> None:
        """The reporter's configuration: no success signal, every node-id read fails."""
        loop = asyncio.get_running_loop()
        tab = _CdpTab(lands_at=loop.time() + 0.3)

        with patch.object(login_settle, "LOG") as mock_log:
            result = await _watch_for_failure_nodriver(
                tab=tab,
                failure_text="Invalid credentials",
                site_name="myshop",
                deadline=loop.time() + 1.0,
            )

        assert result is True
        assert _warnings(mock_log, "login_page_unreadable") == []

    async def test_an_unreadable_window_names_the_page_s_last_url(self) -> None:
        """Only the documents fail here; the URL the warning names is the page's."""
        loop = asyncio.get_running_loop()
        tab = _CdpTab(lands_at=loop.time() + 0.2, page_read="not json")

        with patch.object(login_settle, "LOG") as mock_log:
            result = await _watch_for_failure_nodriver(
                tab=tab,
                failure_text="Invalid credentials",
                site_name="myshop",
                deadline=loop.time() + 1.0,
            )

        assert result is False
        warning = _warning_kwargs(mock_log, "login_page_unreadable")
        assert warning["url"] == DASHBOARD
        assert warning["error_types"] == "PageReadAnswerError, ProtocolException"


_SLOW_DELAYS = [0.5, 1.6, 2.5, 4.0]
_FAILED = "<html>Invalid credentials</html>"


def _no_signal_config(**overrides: Any) -> LoginConfig:
    """The #395 configuration: failure text and no success signal, at the default
    timeout unless a test sets one."""
    values: dict[str, Any] = {
        "steps": [LoginStep(fields={"username": "#user"}, submit="#submit")],
        "url": "/login",
        "failure": "Invalid credentials",
    }
    values.update(overrides)
    return LoginConfig(**values)


def _slow_outcome(tab_factory: Any, config: LoginConfig) -> tuple[bool, float, MagicMock]:
    """``wait_for_login_outcome_nodriver`` on a virtual clock: the verdict, the
    virtual seconds from the submit to the verdict, and the patched LOG."""

    async def wait() -> bool:
        return await wait_for_login_outcome_nodriver(
            tab=tab_factory(),
            login_config=config,
            failure_text=config.failure or "",
            site_name="myshop",
            pre_submit_url=IDP,
        )

    with patch.object(login_settle, "LOG") as mock_log:
        result, elapsed = _run_virtual(wait)
    return result, elapsed, mock_log


class TestASlowPageIsJudgedOnWhatItShows:
    """#395: with every CDP command answering after a fixed delay, a login with no
    success signal ended in ``login_page_unreadable`` from 1.6 s up in 1.43.0 (two
    commands a tick, and the read abandoned half a second after the grace). The
    page's verdict now comes from the first read that answers within
    ``LoginConfig.timeout`` of the submit."""

    @pytest.mark.parametrize("delay", _SLOW_DELAYS)
    def test_a_page_without_the_failure_text_passes(self, delay: float) -> None:
        result, _elapsed, mock_log = _slow_outcome(
            lambda: _CdpTab(lands_at=0.0, node_ids_fail=False, delay=delay),
            _no_signal_config(),
        )

        assert result is True
        assert mock_log.warning.call_args_list == []

    @pytest.mark.parametrize("delay", _SLOW_DELAYS)
    def test_a_page_with_the_failure_text_fails_on_it(self, delay: float) -> None:
        result, _elapsed, mock_log = _slow_outcome(
            lambda: _CdpTab(lands_at=0.0, node_ids_fail=False, html_after=_FAILED, delay=delay),
            _no_signal_config(),
        )

        assert result is False
        assert _warning_kwargs(mock_log, "login_failure_text_detected")["text"] == (
            "Invalid credentials"
        )
        assert _warnings(mock_log, "login_page_unreadable") == []

    @pytest.mark.parametrize("delay", _SLOW_DELAYS)
    def test_each_tick_is_one_command(self, delay: float) -> None:
        tabs: list[_CdpTab] = []

        def make() -> _CdpTab:
            tabs.append(_CdpTab(lands_at=0.0, node_ids_fail=False, delay=delay))
            return tabs[-1]

        result, elapsed, _mock_log = _slow_outcome(make, _no_signal_config())

        assert result is True
        assert set(tabs[0].methods) == {"Runtime.evaluate"}
        # The ticks run back to back, each one command long, from half a second in.
        assert len(tabs[0].methods) <= 1 + elapsed / delay

    @pytest.mark.parametrize("timeout", [10.0, 30.0])
    def test_a_tab_that_never_answers_is_unreadable_at_the_timeout(self, timeout: float) -> None:
        result, elapsed, mock_log = _slow_outcome(
            lambda: _CdpTab(lands_at=0.0, node_ids_fail=False, delay=_FOREVER),
            _no_signal_config(timeout=timeout),
        )

        assert result is False
        assert elapsed == pytest.approx(timeout)
        warning = _warning_kwargs(mock_log, "login_page_unreadable")
        assert warning["failed_ticks"] == 1
        assert warning["error_types"] == "TimeoutError"
        assert "the login's timeout counted from the submit" in warning["error"]

    def test_a_timeout_shorter_than_the_grace_does_not_shorten_it(self) -> None:
        """The bound is never sooner than the grace's own end, half a second past it."""
        result, elapsed, mock_log = _slow_outcome(
            lambda: _CdpTab(lands_at=0.0, node_ids_fail=False, delay=_FOREVER),
            _no_signal_config(timeout=1.0),
        )

        assert result is False
        assert elapsed == pytest.approx(login_settle._NO_SIGNAL_GRACE + 0.5)
        warning = _warning_kwargs(mock_log, "login_page_unreadable")
        assert "half a second past its three second window" in warning["error"]
        assert "timeout" not in warning["error"]

    def test_a_page_that_answered_is_not_held_by_a_later_read(self) -> None:
        """The extended bound is for a wait with nothing to rule on: once a read has
        answered the page, a later read that never answers is given up half a second
        past the window, and the page that answered decides."""

        class _AnswersOnceTab(_CdpTab):
            async def send(self, cdp_obj: Any, _is_update: bool = False, **kwargs: Any) -> Any:
                if self.methods:
                    self.methods.append("unanswered")
                    await asyncio.sleep(_FOREVER)
                return await super().send(cdp_obj, _is_update, **kwargs)

        tabs: list[_CdpTab] = []

        def make() -> _CdpTab:
            tabs.append(_AnswersOnceTab(lands_at=0.0, node_ids_fail=False))
            return tabs[-1]

        result, elapsed, mock_log = _slow_outcome(make, _no_signal_config())

        assert result is True
        assert elapsed == pytest.approx(login_settle._NO_SIGNAL_GRACE + 0.5)
        assert mock_log.warning.call_args_list == []
        assert tabs[0].methods[0] == "Runtime.evaluate"
        assert "unanswered" in tabs[0].methods

    def test_a_read_that_answers_after_the_timeout_is_not_waited_for(self) -> None:
        """A page slower than the whole budget is unreadable, at the budget."""
        result, elapsed, mock_log = _slow_outcome(
            lambda: _CdpTab(lands_at=0.0, node_ids_fail=False, delay=6.0),
            _no_signal_config(timeout=5.0),
        )

        assert result is False
        assert elapsed == pytest.approx(5.0)
        assert _warning_kwargs(mock_log, "login_page_unreadable")["failed_ticks"] == 1

    @pytest.mark.parametrize("delay", _SLOW_DELAYS)
    def test_the_success_signal_poll_reads_a_slow_page_in_one_command(self, delay: float) -> None:
        """Ruling 3 of #395: the signal poll's tick is one command too, so a slow page
        lands within the timeout it is given."""
        tabs: list[_CdpTab] = []

        def make() -> _CdpTab:
            # The page lands on the success URL a second after the submit.
            lands_at = asyncio.get_running_loop().time() + 1.0
            tabs.append(_CdpTab(lands_at=lands_at, node_ids_fail=False, delay=delay))
            return tabs[-1]

        result, _elapsed, mock_log = _slow_outcome(
            make, _no_signal_config(success_url="*/dashboard*", settle=0.0, timeout=12.0)
        )

        assert result is True
        assert mock_log.warning.call_args_list == []
        polls = [m for m in tabs[0].methods if m != "Runtime.evaluate"]
        assert polls == []
        page_reads = [e for e in tabs[0].expressions if "graftpunk:page-read" in e]
        # Every command but the readiness read is the tick's one page read.
        assert len(page_reads) == len(tabs[0].methods) - 1


class DeclarativeNodriverUrlWithFailure(SitePlugin):
    """A nodriver plugin whose success signal is the URL, with failure text."""

    site_name = "ndurlfail"
    session_name = "ndurlfail"
    help_text = "ND URL Failure"
    base_url = "https://myshop.example"
    backend = "nodriver"
    login_config = LoginConfig(
        steps=[LoginStep(fields={"username": "#user", "password": "#pass"}, submit="#submit")],
        url="/login",
        failure="Invalid credentials",
        success_url="*/dashboard*",
        timeout=5.0,
        settle=0.0,
    )


class TestTheLoginFlowReadsThePage:
    """The URLs the engine reads around the wait. The scripted tab's ``Tab.url`` is
    nodriver's stale copy (the login page) while ``location.href`` moves."""

    async def test_the_session_records_the_page_the_login_landed_on(self) -> None:
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _ScriptedNodriverTab(
            urls=("https://myshop.example/login", "https://myshop.example/login", DASHBOARD),
        )
        mock_bs, instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch.object(login_settle, "LOG"),
        ):
            result = await generate_login_method(DeclarativeNodriverUrlWithFailure())(
                _LOGIN_CREDENTIALS
            )

        assert result is True
        assert tab.url == "https://myshop.example/login"
        assert instance.current_url == DASHBOARD

    async def test_the_baseline_is_the_page_s_url_before_the_submit(self) -> None:
        """Tab.url still names the page before a redirect the login page made itself;
        the baseline is the page the submit was clicked on."""
        from graftpunk.plugins.login_engine import generate_login_method

        form = "https://idp.example.com/sso/form"

        class StaleCopyTab(_ScriptedNodriverTab):
            url = "https://myshop.example/dashboard-preview"  # an earlier page's copy

        stale = StaleCopyTab(urls=(form, form, DASHBOARD))
        mock_bs, _instance = _nodriver_session_for(stale)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch.object(login_settle, "LOG"),
            patch(
                "graftpunk.plugins.login_engine.wait_for_login_outcome_nodriver",
                wraps=wait_for_login_outcome_nodriver,
            ) as wait,
        ):
            result = await generate_login_method(DeclarativeNodriverUrlWithFailure())(
                _LOGIN_CREDENTIALS
            )

        assert result is True
        assert wait.call_args.kwargs["pre_submit_url"] == form

    async def test_the_failure_summary_names_the_page_the_login_ended_on(self) -> None:
        from graftpunk.plugins.login_engine import generate_login_method

        class Short(DeclarativeNodriverUrlWithFailure):
            site_name = "ndurlshort"
            session_name = "ndurlshort"
            login_config = LoginConfig(
                steps=[
                    LoginStep(fields={"username": "#user", "password": "#pass"}, submit="#submit")
                ],
                url="/login",
                failure="Invalid credentials",
                success_url="*/account*",
                timeout=0.6,
                settle=0.0,
            )

        tab = _ScriptedNodriverTab(
            urls=("https://myshop.example/login", "https://myshop.example/login", DASHBOARD),
        )
        mock_bs, _instance = _nodriver_session_for(tab)
        verdict = LoginVerdict()

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch.object(login_settle, "LOG"),
            recording_login_verdict(verdict),
        ):
            result = await generate_login_method(Short())(_LOGIN_CREDENTIALS)

        assert result is False
        assert verdict.url == DASHBOARD


_NEVER = float("inf")
_CONTEXT_GONE = ProtocolException({"code": -32000, "message": "Execution context was destroyed."})


class TestAUrlThePageDidNotAnswerDecidesNothing:
    """``Tab.url``'s copy is the fallback when ``location.href`` fails, and it may
    name a page from before the navigation, so it never passes the URL signal."""

    @pytest.mark.parametrize(
        ("copy", "success_url"),
        [
            # The toolkit reviewer's reproduction: the copy is the site's login page.
            ("https://myshop.example/login", "https://myshop.example/*"),
            # The second reviewer's: the copy is the site root, before the redirect.
            ("https://myshop.example/", "https://myshop.example/*"),
            # A glob that matches any URL, the empty one included.
            ("https://myshop.example/", "*"),
        ],
    )
    async def test_a_login_still_on_the_identity_provider_does_not_pass(
        self, copy: str, success_url: str
    ) -> None:
        """Every evaluate fails mid-navigation while the DOM reads answer, the page
        never leaves the identity provider, and Tab.url's copy matches success_url."""
        tab = _CdpTab(
            lands_at=_NEVER,
            node_ids_fail=False,
            evaluate_error=_CONTEXT_GONE,
            tab_url=copy,
            html_before="<html>Signing you in...</html>",
        )

        with patch.object(login_settle, "LOG") as mock_log:
            result = await _signal_wait(tab, _config(success_url=success_url, timeout=0.6))

        assert result is False
        assert tab.url == copy
        warning = _warning_kwargs(mock_log, "login_signal_timeout")
        assert warning["missing"] == f"success URL pattern '{success_url}'"
        assert warning["url"] == copy
        assert warning["url_source"] == "tab_url_copy"

    async def test_a_page_s_own_url_is_reported_as_such(self) -> None:
        loop = asyncio.get_running_loop()
        tab = _CdpTab(lands_at=loop.time(), node_ids_fail=False)

        with patch.object(login_settle, "LOG") as mock_log:
            result = await _signal_wait(tab, _config(success_url="*/account*", timeout=0.6))

        assert result is False
        warning = _warning_kwargs(mock_log, "login_signal_timeout")
        assert (warning["url"], warning["url_source"]) == (DASHBOARD, "page")

    async def test_the_baseline_is_unknown_when_the_page_does_not_answer(self) -> None:
        """Tab.url's copy names an earlier page of the site, the page before the
        submit is the login page, and the glob is loose enough to match it: with
        the copy as the baseline the login page itself would pass."""
        from graftpunk.plugins.login_engine import generate_login_method

        class Loose(DeclarativeNodriverUrlWithFailure):
            site_name = "ndloose"
            session_name = "ndloose"
            login_config = LoginConfig(
                steps=[
                    LoginStep(fields={"username": "#user", "password": "#pass"}, submit="#submit")
                ],
                url="/login",
                failure="Invalid credentials",
                success_url="*myshop.example*",
                timeout=0.6,
                settle=0.0,
            )

        login_page = "https://myshop.example/login"

        class CopyTab(_ScriptedNodriverTab):
            url = "https://myshop.example/start"  # an earlier page's copy

            def _href(self) -> Any:
                answer = super()._href()
                # The read just before the submit gets no answer; the poll's do.
                return MagicMock(name="RemoteObject") if self._url_reads == 1 else answer

        tab = CopyTab(urls=(login_page,))
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch.object(login_settle, "LOG"),
            patch(
                "graftpunk.plugins.login_engine.wait_for_login_outcome_nodriver",
                wraps=wait_for_login_outcome_nodriver,
            ) as wait,
        ):
            result = await generate_login_method(Loose())(_LOGIN_CREDENTIALS)

        assert result is False
        assert wait.call_args.kwargs["pre_submit_url"] == ""

    async def test_the_session_records_the_login_url_when_the_page_does_not_answer(
        self,
    ) -> None:
        """The session's recorded domain comes from its current_url: Tab.url's copy
        (an identity provider's, here) is not used for it."""
        from graftpunk.plugins.login_engine import generate_login_method

        class Plugin(SitePlugin):
            site_name = "ndsel"
            session_name = "ndsel"
            help_text = "ND Selector"
            base_url = "https://myshop.example"
            backend = "nodriver"
            login_config = LoginConfig(
                steps=[
                    LoginStep(fields={"username": "#user", "password": "#pass"}, submit="#submit")
                ],
                url="/login",
                success=".dashboard",
                timeout=5.0,
                settle=0.0,
            )

        class QuietAfterTab(_ScriptedNodriverTab):
            url = IDP  # the copy

            def _href(self) -> Any:
                if self._wait_over:
                    return MagicMock(name="RemoteObject")
                return super()._href()

        tab = QuietAfterTab(success_selector=".dashboard", success_from=1, urls=(DASHBOARD,))
        mock_bs, instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch.object(login_settle, "LOG"),
        ):
            result = await generate_login_method(Plugin())(_LOGIN_CREDENTIALS)

        assert result is True
        assert instance.current_url == "https://myshop.example/login"

    async def test_the_summary_says_when_its_url_is_the_copy(self) -> None:
        from graftpunk.cli.login_commands import _page_line

        tab = _CdpTab(lands_at=_NEVER, evaluate_error=_CONTEXT_GONE)
        verdict = LoginVerdict()

        with patch.object(login_settle, "LOG"), recording_login_verdict(verdict):
            await login_settle.note_login_url_nodriver(tab)

        assert (verdict.url, verdict.url_live) == (IDP, False)
        line = _page_line(verdict)
        assert IDP in line
        assert "may be stale" in line
        assert "may be stale" not in _page_line(LoginVerdict(url=DASHBOARD))


class TestTheHandWrittenLoginSessionReadsThePage:
    """``SitePlugin.browser_session()``, the escape hatch for a hand-written nodriver
    login, records the session's URL the same way."""

    @pytest.mark.parametrize(
        ("evaluate_error", "expected"),
        [(None, DASHBOARD), (_CONTEXT_GONE, "https://myshop.example/")],
    )
    async def test_the_recorded_url_is_the_page_s_or_the_site_root(
        self, evaluate_error: Exception | None, expected: str
    ) -> None:
        class Hand(SitePlugin):
            site_name = "ndhand"
            session_name = "ndhand"
            help_text = "ND Hand"
            base_url = "https://myshop.example"
            backend = "nodriver"

        loop = asyncio.get_running_loop()
        tab = _CdpTab(lands_at=loop.time(), evaluate_error=evaluate_error)

        with (
            patch("graftpunk.BrowserSession") as mock_bs,
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch.object(login_settle, "LOG"),
        ):
            instance = mock_bs.return_value
            instance.start_async = AsyncMock()
            instance.driver = MagicMock()
            instance.driver.get = AsyncMock(return_value=tab)
            instance.transfer_nodriver_cookies_to_session = AsyncMock()
            async with Hand().browser_session() as (session, _tab):
                pass

        assert tab.url == IDP
        assert session.current_url == expected


class TestUnreadableWarningFields:
    def test_the_existing_signature_logs_exactly_the_existing_fields(self) -> None:
        with patch("graftpunk.plugins.login_settle.LOG") as mock_log:
            _warn_login_page_unreadable(
                site_name="myshop", error="boom", url="https://myshop.example/login"
            )

        mock_log.warning.assert_called_once_with(
            "login_page_unreadable",
            plugin="myshop",
            error="boom",
            url="https://myshop.example/login",
            hint=(
                "The page could not be read at any point after the submit, so the "
                "login cannot be confirmed. The browser may have closed or navigated "
                "away; run with --observe=full to capture what happened."
            ),
        )

    def test_the_new_fields_are_added_after_the_existing_ones(self) -> None:
        with patch("graftpunk.plugins.login_settle.LOG") as mock_log:
            _warn_login_page_unreadable(
                site_name="myshop",
                error="boom",
                url="",
                failed_ticks=3,
                error_types=("ProtocolException", "TimeoutError"),
            )

        kwargs = mock_log.warning.call_args[1]
        assert list(kwargs) == [
            "plugin",
            "error",
            "url",
            "hint",
            "failed_ticks",
            "error_types",
            "url_source",
        ]
        assert kwargs["url_source"] == "page"
        assert kwargs["failed_ticks"] == 3
        assert kwargs["error_types"] == "ProtocolException, TimeoutError"
