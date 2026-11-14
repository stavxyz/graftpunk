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

The wait reads ``location.href`` (``graftpunk.plugins.login_settle.page_url_nodriver``)
and, when a node-id read fails, the document and its URL in one
``Runtime.evaluate``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

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
)

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
    would refresh ``Tab.url``. ``methods`` records the CDP methods sent.
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
    ) -> None:
        info = cdp.target.TargetInfo(
            target_id=cdp.target.TargetID("T"),
            type_="page",
            title="",
            url=IDP,
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
        self.methods: list[str] = []
        self.expressions: list[str] = []

    def _landed(self) -> bool:
        return asyncio.get_running_loop().time() >= self.lands_at

    def current_url(self) -> str:
        return DASHBOARD if self._landed() else IDP

    def _html(self) -> str:
        return self.html_after if self._landed() else "<html>Sign in</html>"

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

    async def test_a_success_url_login_passes_when_the_node_id_reads_work(self) -> None:
        loop = asyncio.get_running_loop()
        tab = _CdpTab(lands_at=loop.time() + 0.3, node_ids_fail=False)

        with patch.object(login_settle, "LOG"):
            result = await _signal_wait(tab, _config(success_url="*/dashboard*"))

        assert result is True
        assert "DOM.getOuterHTML" in tab.methods

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
        assert warning["error_types"] == "ProtocolException, ConnectionResetError"
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
        assert warning["error_types"] == "ProtocolException, PageReadAnswerError"


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
        assert list(kwargs) == ["plugin", "error", "url", "hint", "failed_ticks", "error_types"]
        assert kwargs["failed_ticks"] == 3
        assert kwargs["error_types"] == "ProtocolException, TimeoutError"
