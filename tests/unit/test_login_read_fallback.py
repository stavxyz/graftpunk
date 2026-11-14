"""The nodriver post-submit wait reads the page without node ids when the node-id
read fails (#392).

nodriver 0.50.6's ``Tab.get_content`` is ``DOM.getDocument`` then
``DOM.getOuterHTML(backend_node_id=...)``, and ``Tab.query_selector`` is
``DOM.getDocument`` then ``DOM.querySelector(node_id, ...)``: both carry a node id
from one CDP call to the next, and a document replaced in between answers the
second with "No node found for given backend id". A reporter saw that on every
tick of logins that had in fact succeeded, and the wait ended in
``login_page_unreadable``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from graftpunk.exceptions import PluginError
from graftpunk.plugins import SitePlugin
from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep
from graftpunk.plugins.login_settle import _warn_login_page_unreadable
from tests.unit.test_login_engine import (
    _LOGIN_CREDENTIALS,
    DeclarativeNodriverFailureOnly,
    DeclarativeNodriverPoll,
    _nodriver_session_for,
    _ScriptedNodriverTab,
    _warning_kwargs,
)

_NO_NODE = {"code": -32000, "message": "No node found for given backend id"}


def _no_node() -> Exception:
    from nodriver.core.connection import ProtocolException

    return ProtocolException(dict(_NO_NODE))


class _NodeIdsGoneTab(_ScriptedNodriverTab):
    """A tab whose every node-id read fails as a replaced document does, while a
    ``Runtime.evaluate`` of the current document answers from the script.

    ``page_reads`` records each evaluate of the current document, so a test can
    tell that the verdict came from one.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.page_reads: list[str] = []

    async def get_content(self) -> str:
        self.events.append(f"tick:{self.tick}:no-node")
        raise _no_node()

    async def query_selector(self, selector: str) -> Any:
        if selector and selector == self._success_selector:
            self.order.append("query")
            raise _no_node()
        return await super().query_selector(selector)

    async def evaluate(self, expression: str, **kwargs: Any) -> Any:
        if "graftpunk:selector-check" in expression:
            # The selector is valid: nodriver hands a false answer back as the
            # RemoteObject, which is not True.
            return object()
        if "graftpunk:page-read" in expression:
            self.page_reads.append(expression)
            found = self._success_from is not None and self.tick >= self._success_from
            return json.dumps(
                {
                    "html": self._at(self._contents, self.tick),
                    "found": found if self._success_selector else None,
                    "rejected": False,
                }
            )
        return await super().evaluate(expression, **kwargs)


class DeclarativeNodriverUrlWithFailure(SitePlugin):
    """A nodriver plugin whose success signal is the URL, with failure text, so every
    tick asks for the document."""

    site_name = "ndurlfail"
    session_name = "ndurlfail"
    help_text = "ND URL Failure"
    base_url = "https://example.com"
    backend = "nodriver"
    login_config = LoginConfig(
        steps=[LoginStep(fields={"username": "#user", "password": "#pass"}, submit="#submit")],
        url="/login",
        failure="Invalid credentials",
        success_url="*/dashboard*",
        timeout=5.0,
        settle=0.0,
    )


def _unreadable_warnings(mock_log: MagicMock) -> list[Any]:
    return [c for c in mock_log.warning.call_args_list if c[0][0] == "login_page_unreadable"]


class TestNodeIdReadFallback:
    @pytest.mark.asyncio
    async def test_a_success_element_read_without_node_ids_passes_the_login(self) -> None:
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _NodeIdsGoneTab(success_selector=".dashboard", success_from=2)
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(DeclarativeNodriverPoll())(_LOGIN_CREDENTIALS)

        assert result is True
        assert "cookies" in tab.events
        assert tab.tick == 2
        assert tab.page_reads
        # The selector reaches the page as a JSON string literal.
        assert all(json.dumps(".dashboard") in expression for expression in tab.page_reads)
        assert _unreadable_warnings(mock_log) == []

    @pytest.mark.asyncio
    async def test_a_success_url_with_failure_text_passes_when_only_node_ids_fail(
        self,
    ) -> None:
        """Every tick wants the document here (failure text is set), so a tick whose
        document could not be read never passed, even on the success URL."""
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _NodeIdsGoneTab(
            urls=(
                "https://idp.example.com/sso/login",
                "https://idp.example.com/sso/login",
                "https://myshop.example/dashboard",
            ),
            contents=("<html>Welcome</html>",),
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(DeclarativeNodriverUrlWithFailure())(
                _LOGIN_CREDENTIALS
            )

        assert result is True
        assert "cookies" in tab.events
        assert _unreadable_warnings(mock_log) == []

    @pytest.mark.asyncio
    async def test_failure_text_read_without_node_ids_still_fails_the_login(self) -> None:
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _NodeIdsGoneTab(
            success_selector=".dashboard",
            contents=("<html>Invalid credentials</html>",),
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(DeclarativeNodriverPoll())(_LOGIN_CREDENTIALS)

        assert result is False
        assert "cookies" not in tab.events
        assert _warning_kwargs(mock_log, "login_failure_text_detected")["text"] == (
            "Invalid credentials"
        )

    @pytest.mark.asyncio
    async def test_the_grace_window_rules_on_a_page_read_without_node_ids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reporter's case: a login with no success signal whose every node-id read
        fails ended in login_page_unreadable although the page was there."""
        from graftpunk.plugins.login_engine import generate_login_method

        monkeypatch.setattr("graftpunk.plugins.login_settle._NO_SIGNAL_GRACE", 1.0)
        tab = _NodeIdsGoneTab(
            urls=("https://idp.example.com/sso/login",),
            contents=("<html>Welcome</html>",),
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(DeclarativeNodriverFailureOnly())(
                _LOGIN_CREDENTIALS
            )

        assert result is True
        assert "cookies" in tab.events
        assert _unreadable_warnings(mock_log) == []

    @pytest.mark.asyncio
    async def test_the_final_verdict_comes_from_the_deadline_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every node-id read fails and the page reaches the success element only on
        the deadline tick: the login passes on that read."""
        from graftpunk.plugins.login_engine import generate_login_method
        from graftpunk.plugins.login_settle import _LOGIN_POLL_INTERVAL

        class ShortPoll(DeclarativeNodriverPoll):
            login_config = LoginConfig(
                steps=[
                    LoginStep(fields={"username": "#user", "password": "#pass"}, submit="#submit")
                ],
                url="/login",
                failure="Invalid credentials",
                success=".dashboard",
                timeout=2 * _LOGIN_POLL_INTERVAL,
                settle=0.0,
            )

        class DeadlineTab(_NodeIdsGoneTab):
            """The element is there only once the wait's deadline has passed."""

            deadline_passed = False

            async def evaluate(self, expression: str, **kwargs: Any) -> Any:
                if "graftpunk:page-read" in expression:
                    self.page_reads.append(expression)
                    return json.dumps(
                        {"html": "<html></html>", "found": self.deadline_passed, "rejected": False}
                    )
                return await super().evaluate(expression, **kwargs)

        tab = DeadlineTab(success_selector=".dashboard")
        mock_bs, _instance = _nodriver_session_for(tab)

        from graftpunk.plugins import login_settle

        real_is_deadline_tick = login_settle._is_deadline_tick

        def mark_deadline(remaining: float) -> bool:
            last = real_is_deadline_tick(remaining)
            tab.deadline_passed = tab.deadline_passed or last
            return last

        monkeypatch.setattr(login_settle, "_is_deadline_tick", mark_deadline)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(ShortPoll())(_LOGIN_CREDENTIALS)

        assert result is True
        assert "cookies" in tab.events
        assert _unreadable_warnings(mock_log) == []
        assert [
            c for c in mock_log.warning.call_args_list if c[0][0] == "login_signal_timeout"
        ] == []

    @pytest.mark.asyncio
    async def test_a_selector_the_page_rejects_is_still_reported(self) -> None:
        """The selector check could not run, and the page read says the selector is
        malformed: the login ends naming it, as it does when the check answers."""
        from graftpunk.plugins.login_engine import generate_login_method

        class RejectingTab(_NodeIdsGoneTab):
            async def evaluate(self, expression: str, **kwargs: Any) -> Any:
                if "graftpunk:selector-check" in expression:
                    raise RuntimeError("Execution context was destroyed")
                if "graftpunk:page-read" in expression:
                    self.page_reads.append(expression)
                    return json.dumps({"html": None, "found": None, "rejected": True})
                return await super().evaluate(expression, **kwargs)

        tab = RejectingTab(success_selector=".dashboard")
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            pytest.raises(PluginError) as raised,
        ):
            await generate_login_method(DeclarativeNodriverPoll())(_LOGIN_CREDENTIALS)

        assert "not a valid CSS selector" in str(raised.value)
        assert "'.dashboard'" in str(raised.value)
        assert len(tab.page_reads) == 1

    @pytest.mark.asyncio
    async def test_a_page_read_answer_of_the_wrong_shape_leaves_the_tick_unreadable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.plugins.login_engine import generate_login_method

        monkeypatch.setattr("graftpunk.plugins.login_settle._NO_SIGNAL_GRACE", 0.6)

        class OddAnswerTab(_NodeIdsGoneTab):
            async def evaluate(self, expression: str, **kwargs: Any) -> Any:
                if "graftpunk:page-read" in expression:
                    self.page_reads.append(expression)
                    return json.dumps({"found": True, "value": None})
                return await super().evaluate(expression, **kwargs)

        tab = OddAnswerTab(urls=("https://idp.example.com/sso/login",))
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(DeclarativeNodriverFailureOnly())(
                _LOGIN_CREDENTIALS
            )

        assert result is False
        assert tab.page_reads
        warning = _warning_kwargs(mock_log, "login_page_unreadable")
        assert warning["error_types"] == "ProtocolException, PageReadAnswerError"


class TestUnreadableWarningFields:
    @pytest.mark.asyncio
    async def test_the_warning_counts_failed_ticks_and_names_their_exception_types(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A browser whose node-id reads and page reads both fail: the warning carries
        how many ticks failed and the exception types, and no exception text."""
        from graftpunk.plugins.login_engine import generate_login_method

        monkeypatch.setattr("graftpunk.plugins.login_settle._NO_SIGNAL_GRACE", 1.0)

        class DeadTab(_NodeIdsGoneTab):
            async def evaluate(self, expression: str, **kwargs: Any) -> Any:
                if "graftpunk:page-read" in expression:
                    self.page_reads.append(expression)
                    raise ConnectionResetError("secret-looking text from the socket")
                return await super().evaluate(expression, **kwargs)

        tab = DeadTab(urls=("https://idp.example.com/sso/login",))
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(DeclarativeNodriverFailureOnly())(
                _LOGIN_CREDENTIALS
            )

        assert result is False
        warning = _warning_kwargs(mock_log, "login_page_unreadable")
        ticks = len(tab.page_reads)
        assert ticks >= 2
        assert warning["failed_ticks"] == ticks
        assert warning["error_types"] == "ProtocolException, ConnectionResetError"
        assert "No node found" in warning["error"]
        assert warning["url"] == "https://idp.example.com/sso/login"
        for field in ("failed_ticks", "error_types"):
            assert "secret-looking" not in str(warning[field])
            assert "No node found" not in str(warning[field])

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
