"""The element a login step acts on, and when it acts (#275).

Two failures from a first run against a real site. A click-only first step
clicked a header link before the page's script had attached its handler, so the
sign-in modal never opened. The same selector could also match a hidden
mobile-menu copy of the link ahead of the visible one, and ``tab.select`` acts
on the first match in document order.

The pages here are modelled, not browsed: :class:`_FakePage` answers the
engine's two page probes (readiness, and which match to act on) the way a
browser would for the page it describes, and records every click.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from graftpunk.exceptions import PluginError
from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep, SitePlugin

pytestmark = pytest.mark.usefixtures("_fast_login_timings")

_URL = "https://app.example.com/"


class _FakeElement:
    def __init__(self, page: _FakePage, name: str, *, visible: bool, disabled: bool) -> None:
        self._page = page
        self.name = name
        self.visible = visible
        self.disabled = disabled

    async def click(self) -> None:
        self._page.clicks.append((self.name, self._page.hydrated))

    async def clear_input(self) -> None:
        return None

    async def send_keys(self, value: str) -> None:
        return None


class _FakePage:
    """One page: elements by selector in document order, and a script that hydrates.

    The page counts as hydrated once the engine has asked about readiness
    ``hydrate_after`` times; until then its DOM is still changing, so the
    readiness probe reports no quiet time. ``never_quiet`` keeps it changing
    forever (an animated page). A click records whether the page had hydrated.
    """

    def __init__(self, *, hydrate_after: int = 0, never_quiet: bool = False) -> None:
        self.url = _URL
        self.elements: dict[str, list[_FakeElement]] = {}
        self.clicks: list[tuple[str, bool]] = []
        self.ready_reads = 0
        self._hydrate_after = hydrate_after
        self._never_quiet = never_quiet
        self.send = AsyncMock()

    def __getattr__(self, name: str) -> MagicMock:
        return MagicMock()

    @property
    def hydrated(self) -> bool:
        return self.ready_reads > self._hydrate_after

    def add(
        self, selector: str, name: str, *, visible: bool = True, disabled: bool = False
    ) -> _FakeElement:
        element = _FakeElement(self, name, visible=visible, disabled=disabled)
        self.elements.setdefault(selector, []).append(element)
        return element

    async def select(self, selector: str, timeout: float | None = None) -> Any:
        matches = self.elements.get(selector, [])
        return matches[0] if matches else None

    async def query_selector(self, selector: str) -> Any:
        return await self.select(selector)

    async def query_selector_all(self, selector: str) -> list[_FakeElement]:
        return list(self.elements.get(selector, []))

    async def get_content(self) -> str:
        return "<html><body>Signed in</body></html>"

    async def evaluate(self, expression: str, **kwargs: Any) -> Any:
        if "MutationObserver" in expression:
            self.ready_reads += 1
            quiet = 0 if self._never_quiet or not self.hydrated else 5000
            return json.dumps({"state": "complete", "quiet_ms": quiet})
        if "querySelectorAll" in expression:
            for selector, matches in self.elements.items():
                if json.dumps(selector) in expression:
                    return json.dumps(
                        {
                            "count": len(matches),
                            "visible": [m.visible for m in matches],
                            "disabled": [m.disabled for m in matches],
                        }
                    )
            return json.dumps({"count": 0, "visible": [], "disabled": []})
        if expression == "document.readyState":
            return "complete"
        return json.dumps({"found": True, "value": None})


def _plugin(*steps: LoginStep, timeout: float = 30.0) -> SitePlugin:
    class P(SitePlugin):
        site_name = "hydra"
        session_name = "hydra"
        help_text = "t"
        base_url = "https://app.example.com"
        backend = "nodriver"
        login_config = LoginConfig(steps=list(steps), timeout=timeout, settle=0.0)

    return P()


async def _login(plugin: SitePlugin, page: _FakePage) -> bool:
    from graftpunk.plugins.login_engine import generate_login_method

    mock_bs = MagicMock()
    instance = MagicMock()
    mock_bs.return_value = instance
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)
    instance.driver = MagicMock()
    instance.driver.get = AsyncMock(return_value=page)
    instance.transfer_nodriver_cookies_to_session = AsyncMock()
    with (
        patch("graftpunk.BrowserSession", mock_bs),
        patch("graftpunk.plugins.login_engine.cache_login_session"),
    ):
        return await generate_login_method(plugin)({})


class TestAmbiguousSelector:
    @pytest.mark.asyncio
    async def test_a_hidden_copy_first_in_the_document_is_passed_over(self) -> None:
        """The reported page: a hidden mobile-menu link ahead of the visible one."""
        page = _FakePage()
        page.add("a.signin", "mobile-copy", visible=False)
        page.add("a.signin", "header-link")

        with patch("graftpunk.plugins.login_ready.LOG") as log:
            assert await _login(_plugin(LoginStep(submit="a.signin")), page) is True

        assert [name for name, _ in page.clicks] == ["header-link"]
        (call,) = [c for c in log.warning.call_args_list if c.args[0] == "login_selector_ambiguous"]
        assert call.kwargs["matches"] == 2
        assert call.kwargs["visible"] == 1
        assert call.kwargs["chosen"] == 2

    @pytest.mark.asyncio
    async def test_no_visible_match_fails_naming_the_count(self) -> None:
        page = _FakePage()
        page.add("a.signin", "copy-1", visible=False)
        page.add("a.signin", "copy-2", visible=False)

        with pytest.raises(PluginError, match=r"matches 2 elements and none of them is visible"):
            await _login(_plugin(LoginStep(submit="a.signin")), page)
        assert page.clicks == []

    @pytest.mark.asyncio
    async def test_a_field_selector_is_held_to_the_same_rule(self) -> None:
        page = _FakePage()
        hidden = page.add("input.user", "hidden-user", visible=False)
        shown = page.add("input.user", "user")
        hidden.send_keys = AsyncMock()  # type: ignore[method-assign]
        shown.send_keys = AsyncMock()  # type: ignore[method-assign]

        await _login(_plugin(LoginStep(fields={"username": "input.user"})), page)

        hidden.send_keys.assert_not_awaited()
        shown.send_keys.assert_awaited()

    @pytest.mark.asyncio
    async def test_one_match_is_acted_on_without_a_warning(self) -> None:
        page = _FakePage()
        page.add("#go", "go")

        with patch("graftpunk.plugins.login_ready.LOG") as log:
            await _login(_plugin(LoginStep(submit="#go")), page)

        assert [name for name, _ in page.clicks] == ["go"]
        assert not [
            c for c in log.warning.call_args_list if c.args[0] == "login_selector_ambiguous"
        ]

    @pytest.mark.asyncio
    async def test_a_disabled_target_fails_instead_of_taking_a_click_that_does_nothing(
        self,
    ) -> None:
        page = _FakePage()
        page.add("#go", "go", disabled=True)

        with pytest.raises(PluginError, match=r"disabled"):
            await _login(_plugin(LoginStep(submit="#go")), page)
        assert page.clicks == []


class TestReadinessBeforeActing:
    @pytest.mark.asyncio
    async def test_a_click_waits_for_the_page_to_hydrate(self) -> None:
        """The reported failure: the link was in the server HTML before its handler."""
        page = _FakePage(hydrate_after=3)
        page.add("a.signin", "signin")

        await _login(_plugin(LoginStep(submit="a.signin")), page)

        assert page.clicks == [("signin", True)]

    @pytest.mark.asyncio
    async def test_a_page_that_never_goes_quiet_costs_the_cap_and_no_more(self) -> None:
        page = _FakePage(never_quiet=True)
        page.add("a.signin", "signin")

        with patch("graftpunk.plugins.login_ready.LOG") as log:
            await _login(_plugin(LoginStep(submit="a.signin")), page)

        assert [name for name, _ in page.clicks] == ["signin"]
        (call,) = [c for c in log.info.call_args_list if c.args[0] == "login_page_not_quiet"]
        assert call.kwargs["step"] == 1

    @pytest.mark.asyncio
    async def test_the_wait_is_bounded_by_the_login_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A login timeout below the readiness cap wins: the default never outlasts it."""
        import asyncio

        monkeypatch.setattr("graftpunk.plugins.login_ready._READY_MAX_WAIT", 60.0)
        page = _FakePage(never_quiet=True)
        page.add("a.signin", "signin")

        await asyncio.wait_for(_login(_plugin(LoginStep(submit="a.signin"), timeout=0.2), page), 5)

        assert [name for name, _ in page.clicks] == ["signin"]

    @pytest.mark.asyncio
    async def test_an_unreadable_probe_does_not_hold_the_login(self) -> None:
        """A backend that cannot evaluate is no reason to stop: the steps run as before."""
        page = _FakePage()
        page.add("#go", "go")
        page.evaluate = AsyncMock(side_effect=RuntimeError("no runtime"))  # type: ignore[method-assign]

        await _login(_plugin(LoginStep(submit="#go")), page)

        assert [name for name, _ in page.clicks] == ["go"]


class TestExplicitMarkerStandsInForTheDefault:
    @pytest.mark.asyncio
    async def test_a_step_wait_for_replaces_the_readiness_wait(self) -> None:
        page = _FakePage()
        page.add(".hydrated", "marker")
        page.add("#go", "go")

        await _login(_plugin(LoginStep(submit="#go", wait_for=".hydrated")), page)

        assert page.ready_reads == 0
        assert [name for name, _ in page.clicks] == ["go"]

    @pytest.mark.asyncio
    async def test_a_login_wait_for_replaces_it_for_the_first_step_only(self) -> None:
        page = _FakePage()
        page.add(".hydrated", "marker")
        page.add("#next", "next")
        page.add("#go", "go")

        class P(SitePlugin):
            site_name = "hydra"
            session_name = "hydra"
            help_text = "t"
            base_url = "https://app.example.com"
            backend = "nodriver"
            login_config = LoginConfig(
                steps=[LoginStep(submit="#next"), LoginStep(submit="#go")],
                wait_for=".hydrated",
                settle=0.0,
            )

        with patch("graftpunk.plugins.login_ready.LOG") as log:
            await _login(P(), page)

        steps_waited = {
            c.kwargs["step"] for c in log.debug.call_args_list if c.args[0] == "login_page_ready"
        }
        assert steps_waited == {2}
        assert [name for name, _ in page.clicks] == ["next", "go"]


class TestChooseTarget:
    @staticmethod
    def _probe(visible: list[bool], disabled: list[bool] | None = None) -> Any:
        from graftpunk.plugins.login_ready import TargetProbe

        return TargetProbe(
            count=len(visible),
            visible=tuple(visible),
            disabled=tuple(disabled or [False] * len(visible)),
        )

    def test_a_single_hidden_match_is_still_acted_on(self) -> None:
        """A field hidden behind a styled label is common, and was always typed into."""
        from graftpunk.plugins.login_ready import choose_target

        assert choose_target(self._probe([False])).index == 0

    def test_the_first_visible_of_several_is_chosen(self) -> None:
        from graftpunk.plugins.login_ready import choose_target

        assert choose_target(self._probe([False, False, True, True])).index == 2

    def test_no_match_is_pending(self) -> None:
        from graftpunk.plugins.login_ready import choose_target

        choice = choose_target(self._probe([]))
        assert choice.index is None
        assert choice.problem == "matches no element"

    def test_a_disabled_first_visible_is_pending_and_named(self) -> None:
        from graftpunk.plugins.login_ready import choose_target

        choice = choose_target(self._probe([False, True, True], [False, True, False]))
        assert choice.index is None
        assert "match 2" in choice.problem
        assert "disabled" in choice.problem


class TestProbeParsing:
    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "not json",
            "[]",
            json.dumps({"error": "SyntaxError"}),
            json.dumps({"count": True, "visible": [], "disabled": []}),
            json.dumps({"count": 2, "visible": [True], "disabled": [False]}),
            json.dumps({"count": 1, "visible": [True], "disabled": []}),
        ],
    )
    def test_anything_but_a_whole_answer_is_no_answer(self, raw: Any) -> None:
        from graftpunk.plugins.login_ready import parse_target_probe

        assert parse_target_probe(raw) is None

    def test_a_long_match_list_is_described_up_to_the_limit(self) -> None:
        from graftpunk.plugins.login_ready import _TARGET_PROBE_LIMIT, parse_target_probe

        raw = json.dumps(
            {
                "count": 500,
                "visible": [True] * _TARGET_PROBE_LIMIT,
                "disabled": [False] * _TARGET_PROBE_LIMIT,
            }
        )
        probe = parse_target_probe(raw)
        assert probe is not None
        assert probe.count == 500

    @pytest.mark.parametrize(
        "raw",
        [None, "{}", json.dumps({"state": "complete", "quiet_ms": True}), json.dumps([1])],
    )
    def test_a_readiness_read_that_is_not_one_is_none(self, raw: Any) -> None:
        from graftpunk.plugins.login_ready import parse_page_readiness

        assert parse_page_readiness(raw) is None

    def test_a_readiness_read_is_in_seconds(self) -> None:
        from graftpunk.plugins.login_ready import parse_page_readiness

        reading = parse_page_readiness(json.dumps({"state": "interactive", "quiet_ms": 750}))
        assert reading is not None
        assert reading.state == "interactive"
        assert reading.quiet_for == pytest.approx(0.75)


class TestNetworkActivity:
    @staticmethod
    def _event(request_id: str, kind: str | None = None) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            request_id=request_id, type_=SimpleNamespace(value=kind) if kind else None
        )

    def test_a_script_in_flight_keeps_the_page_busy_until_it_ends(self) -> None:
        from graftpunk.plugins.login_ready import NetworkActivity

        activity = NetworkActivity()
        activity._on_request(self._event("1", "Script"))
        assert activity.quiet_for() == 0.0
        activity._on_done(self._event("1"))
        assert not activity._in_flight

    def test_images_and_event_streams_are_not_waited_for(self) -> None:
        from graftpunk.plugins.login_ready import NetworkActivity

        activity = NetworkActivity()
        for kind in ("Image", "Font", "Stylesheet", "Ping", "EventSource"):
            activity._on_request(self._event(kind, kind))
        assert not activity._in_flight

    @pytest.mark.asyncio
    async def test_the_readiness_wait_holds_while_a_chunk_downloads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The DOM can be quiet while the hydration chunk is still on the wire."""
        from graftpunk.plugins import login_ready

        monkeypatch.setattr(login_ready, "_READY_MAX_WAIT", 5.0)
        activity = login_ready.NetworkActivity()
        activity._on_request(self._event("chunk", "Script"))
        reads = 0

        async def evaluate(expression: str, **kwargs: Any) -> str:
            nonlocal reads
            reads += 1
            if reads == 5:
                activity._on_done(self._event("chunk"))
            return json.dumps({"state": "complete", "quiet_ms": 60_000})

        tab = MagicMock()
        tab.evaluate = evaluate
        await login_ready.await_page_ready_nodriver(
            tab, budget=30.0, step=1, site_name="t", network=activity
        )

        assert reads > 5


class _FakeDriver:
    """A selenium driver over a :class:`_FakePage`'s elements."""

    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.current_url = _URL
        self.page_source = "<html>Signed in</html>"

    def __getattr__(self, name: str) -> MagicMock:
        return MagicMock()

    def _element(self, fake: _FakeElement) -> MagicMock:
        element = MagicMock()
        element.click.side_effect = lambda: self.page.clicks.append((fake.name, True))
        return element

    def find_element(self, by: str, selector: str) -> MagicMock:
        from selenium.common.exceptions import NoSuchElementException

        matches = self.page.elements.get(selector, [])
        if not matches:
            raise NoSuchElementException(selector)
        return self._element(matches[0])

    def find_elements(self, by: str, selector: str) -> list[MagicMock]:
        return [self._element(m) for m in self.page.elements.get(selector, [])]

    def execute_script(self, script: str) -> Any:
        import asyncio

        if script == "return document.readyState":
            return "complete"
        return asyncio.run(self.page.evaluate(script.removeprefix("return ")))


class TestSeleniumTwin:
    def _login(self, page: _FakePage, *steps: LoginStep) -> bool:
        from graftpunk.plugins.login_engine import generate_login_method

        class P(SitePlugin):
            site_name = "hydra"
            session_name = "hydra"
            help_text = "t"
            base_url = "https://app.example.com"
            backend = "selenium"
            login_config = LoginConfig(steps=list(steps), settle=0.0)

        mock_bs = MagicMock()
        instance = MagicMock()
        mock_bs.return_value = instance
        instance.__enter__ = MagicMock(return_value=instance)
        instance.__exit__ = MagicMock(return_value=False)
        instance.driver = _FakeDriver(page)
        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_login_session"),
        ):
            return generate_login_method(P())({})

    def test_the_visible_match_is_clicked(self) -> None:
        page = _FakePage()
        page.add("a.signin", "mobile-copy", visible=False)
        page.add("a.signin", "header-link")

        assert self._login(page, LoginStep(submit="a.signin")) is True
        assert [name for name, _ in page.clicks] == ["header-link"]

    def test_all_hidden_fails_naming_the_count(self) -> None:
        page = _FakePage()
        page.add("a.signin", "a", visible=False)
        page.add("a.signin", "b", visible=False)
        page.add("a.signin", "c", visible=False)

        with pytest.raises(PluginError, match=r"matches 3 elements and none of them is visible"):
            self._login(page, LoginStep(submit="a.signin"))

    def test_the_readiness_wait_runs_until_the_page_is_quiet(self) -> None:
        page = _FakePage(hydrate_after=2)
        page.add("#go", "go")

        self._login(page, LoginStep(submit="#go"))

        assert page.ready_reads == 3
