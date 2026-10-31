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
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from graftpunk.exceptions import PluginError
from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep, SitePlugin

pytestmark = pytest.mark.usefixtures("_fast_login_timings")

# A readiness cap no loaded runner outlasts before a page that hydrates in a few
# reads is ready (#326): under the fast timings' 0.05 s cap, two slow reads could
# end the wait, and the step acted before the page hydrated.
_GENEROUS_READY_CAP = 30.0


@pytest.fixture
def _generous_ready_cap(_fast_login_timings: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Requested after the fast timings, so this cap wins."""
    monkeypatch.setattr("graftpunk.plugins.login_ready._READY_MAX_WAIT", _GENEROUS_READY_CAP)


_URL = "https://app.example.com/"


class _FakeElement:
    """An element that keeps what is typed into it, as an input does."""

    def __init__(self, page: _FakePage, name: str, *, visible: bool, disabled: bool) -> None:
        self._page = page
        self.name = name
        self.visible = visible
        self.disabled = disabled
        self.value = ""

    async def click(self) -> None:
        self._page.clicks.append((self.name, self._page.hydrated))

    async def clear_input(self) -> None:
        self.value = ""

    async def send_keys(self, value: str) -> None:
        self.value += value


# The trailing arguments of the read-back the engine evaluates: ("selector", index).
_READBACK_ARGS = re.compile(r"\}\)\((\".*\"), (\d+)\)$")


class _FakePage:
    """One page: elements by selector in document order, and a script that hydrates.

    The page counts as hydrated once the engine has asked about readiness
    ``hydrate_after`` times; until then its DOM is still changing, so the
    readiness probe reports no quiet time. ``never_quiet`` keeps it changing
    forever (an animated page). A click records whether the page had hydrated.
    The field read-back answers with the value of the match it names, the way
    ``document.querySelectorAll(selector)[index]`` would.
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

    def _readiness(self) -> str:
        self.ready_reads += 1
        quiet = 0 if self._never_quiet or not self.hydrated else 5000
        return json.dumps({"state": "complete", "quiet_ms": quiet})

    def _probe(self, expression: str) -> str:
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

    def _read_back(self, expression: str) -> str:
        args = _READBACK_ARGS.search(expression)
        assert args is not None, expression
        matches = self.elements.get(json.loads(args.group(1)), [])
        index = int(args.group(2))
        if index >= len(matches):
            return json.dumps({"found": False, "value": None})
        return json.dumps({"found": True, "value": matches[index].value})

    async def evaluate(self, expression: str, **kwargs: Any) -> Any:
        if "MutationObserver" in expression:
            return self._readiness()
        if "checkVisibility" in expression:
            return self._probe(expression)
        if "String(el.value)" in expression:
            return self._read_back(expression)
        if expression == "document.readyState":
            return "complete"
        raise AssertionError(f"unexpected expression: {expression[:80]}")


def _plugin(*steps: LoginStep, timeout: float = 30.0) -> SitePlugin:
    class P(SitePlugin):
        site_name = "hydra"
        session_name = "hydra"
        help_text = "t"
        base_url = "https://app.example.com"
        backend = "nodriver"
        login_config = LoginConfig(steps=list(steps), timeout=timeout, settle=0.0)

    return P()


async def _login(
    plugin: SitePlugin, page: _FakePage, credentials: dict[str, str] | None = None
) -> bool:
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
        return await generate_login_method(plugin)(credentials or {})


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
    async def test_a_field_with_a_hidden_copy_first_is_filled_and_verified(self) -> None:
        """A page that duplicates its form (a mobile copy hidden ahead of the desktop
        one): the fill, the clear check and the read-back all use the visible match,
        so the typed value verifies on the first attempt."""
        page = _FakePage()
        hidden = page.add("input.user", "hidden-user", visible=False)
        shown = page.add("input.user", "user")

        with (
            patch("graftpunk.plugins.login_engine.LOG") as engine_log,
            patch("graftpunk.plugins.login_ready.LOG") as ready_log,
        ):
            ok = await _login(
                _plugin(LoginStep(fields={"username": "input.user"})), page, {"username": "bob"}
            )

        assert ok is True
        assert shown.value == "bob"
        assert hidden.value == ""
        assert not [
            c
            for c in engine_log.warning.call_args_list
            if c.args[0] == "login_field_value_mismatch"
        ]
        warned = [
            c for c in ready_log.warning.call_args_list if c.args[0] == "login_selector_ambiguous"
        ]
        assert len(warned) == 1

    @pytest.mark.asyncio
    async def test_the_clear_check_reads_the_field_that_was_typed_into(self) -> None:
        """A residue in the visible field is cleared again even when the hidden first
        copy is empty (#148's second clear)."""
        page = _FakePage()
        page.add("input.user", "hidden-user", visible=False)
        shown = page.add("input.user", "user")
        clears = 0

        async def clear_input() -> None:
            # The first clear leaves a late keystroke behind, as #148 measured.
            nonlocal clears
            clears += 1
            shown.value = "x" if clears == 1 else ""

        sends = 0
        real_send = shown.send_keys

        async def send_keys(value: str) -> None:
            nonlocal sends
            sends += 1
            await real_send(value)

        shown.clear_input = clear_input  # type: ignore[method-assign]
        shown.send_keys = send_keys  # type: ignore[method-assign]
        shown.value = "stale"

        await _login(
            _plugin(LoginStep(fields={"username": "input.user"})), page, {"username": "bob"}
        )

        # Cleared twice before the one and only typing: no "xbob", no retry.
        assert (clears, sends) == (2, 1)
        assert shown.value == "bob"

    @pytest.mark.asyncio
    async def test_a_copy_added_ahead_after_typing_is_named_in_the_mismatch_hint(self) -> None:
        """The read-back looks at the old position, which now holds the new copy: the
        warning says the selector now picks another match instead of blaming a
        detached node, and the next attempt finds the field again."""
        page = _FakePage()
        page.add("input.user", "hidden-user", visible=False)
        shown = page.add("input.user", "user")
        real_send = shown.send_keys
        inserted = False

        async def send_keys(value: str) -> None:
            nonlocal inserted
            await real_send(value)
            if not inserted:
                inserted = True
                late = _FakeElement(page, "late-copy", visible=False, disabled=False)
                page.elements["input.user"].insert(0, late)

        shown.send_keys = send_keys  # type: ignore[method-assign]

        with patch("graftpunk.plugins.login_engine.LOG") as log:
            ok = await _login(
                _plugin(LoginStep(fields={"username": "input.user"})), page, {"username": "bob"}
            )

        assert ok is True
        assert shown.value == "bob"
        (call,) = [
            c for c in log.warning.call_args_list if c.args[0] == "login_field_value_mismatch"
        ]
        assert "now picks match 3, not match 2" in call.kwargs["hint"]
        assert "detached" not in call.kwargs["hint"]

    @pytest.mark.asyncio
    async def test_ambiguity_is_warned_once_per_field_across_fill_attempts(self) -> None:
        page = _FakePage()
        page.add("input.user", "hidden-user", visible=False)
        shown = page.add("input.user", "user")
        typed = 0

        async def send_keys(value: str) -> None:
            # The first attempt's keystrokes go nowhere (a re-rendered form).
            nonlocal typed
            typed += 1
            if typed > 1:
                shown.value += value

        shown.send_keys = send_keys  # type: ignore[method-assign]

        with patch("graftpunk.plugins.login_ready.LOG") as log:
            await _login(
                _plugin(LoginStep(fields={"username": "input.user"})), page, {"username": "bob"}
            )

        assert typed == 2
        warned = [c for c in log.warning.call_args_list if c.args[0] == "login_selector_ambiguous"]
        assert len(warned) == 1

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
    @pytest.mark.usefixtures("_generous_ready_cap")
    async def test_a_click_waits_for_the_page_to_hydrate(self) -> None:
        """The reported failure: the link was in the server HTML before its handler."""
        page = _FakePage(hydrate_after=3)
        page.add("a.signin", "signin")

        with patch("graftpunk.plugins.login_ready.LOG") as log:
            await _login(_plugin(LoginStep(submit="a.signin")), page)

        assert page.clicks == [("signin", True)]
        assert page.ready_reads >= 4
        # The wait ended because the page was ready, not at its cap.
        assert not [c for c in log.info.call_args_list if c.args[0] == "login_page_not_quiet"]

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
        element.send_keys.side_effect = lambda value: setattr(fake, "value", fake.value + value)
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
    def _login(self, page: _FakePage, *steps: LoginStep, timeout: float = 30.0) -> bool:
        from graftpunk.plugins.login_engine import generate_login_method

        class P(SitePlugin):
            site_name = "hydra"
            session_name = "hydra"
            help_text = "t"
            base_url = "https://app.example.com"
            backend = "selenium"
            login_config = LoginConfig(steps=list(steps), settle=0.0, timeout=timeout)

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
            return generate_login_method(P())({"username": "bob"})

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

    @pytest.mark.usefixtures("_generous_ready_cap")
    def test_the_readiness_wait_runs_until_the_page_is_quiet(self) -> None:
        page = _FakePage(hydrate_after=2)
        page.add("#go", "go")

        with patch("graftpunk.plugins.login_ready.LOG") as log:
            self._login(page, LoginStep(submit="#go"))

        assert page.ready_reads >= 3
        assert page.clicks == [("go", True)]
        # The wait ended because the page was ready, not at its cap.
        assert not [c for c in log.info.call_args_list if c.args[0] == "login_page_not_quiet"]

    def test_a_field_with_a_hidden_copy_first_is_typed_into_the_visible_one(self) -> None:
        page = _FakePage()
        hidden = page.add("input.user", "hidden-user", visible=False)
        shown = page.add("input.user", "user")

        self._login(page, LoginStep(fields={"username": "input.user"}))

        assert (hidden.value, shown.value) == ("", "bob")

    def test_a_single_disabled_match_fails(self) -> None:
        page = _FakePage()
        page.add("#go", "go", disabled=True)

        with pytest.raises(PluginError, match=r"matches one element, and it is disabled"):
            self._login(page, LoginStep(submit="#go"))
        assert page.clicks == []

    def test_the_error_names_its_step_once(self) -> None:
        page = _FakePage()
        page.add("a.signin", "a", visible=False)
        page.add("a.signin", "b", visible=False)

        with pytest.raises(PluginError) as exc_info:
            self._login(page, LoginStep(submit="a.signin"))
        message = str(exc_info.value)
        assert message.count("Step 1") == 1
        assert "Failed to click" not in message

    def test_a_field_error_names_its_step_once(self) -> None:
        page = _FakePage()
        page.add("input.user", "a", visible=False)
        page.add("input.user", "b", visible=False)

        with pytest.raises(PluginError) as exc_info:
            self._login(page, LoginStep(fields={"username": "input.user"}))
        message = str(exc_info.value)
        assert "matches 2 elements and none of them is visible" in message
        assert message.count("Step 1") == 1
        assert "Failed to fill" not in message

    def test_after_a_retry_the_match_is_looked_up_again(self) -> None:
        """The handle find_element gave before the wait may be stale by the time the
        match is usable, so the click goes to a fresh lookup."""
        page = _FakePage()
        page.add("#go", "old-go", disabled=True)
        probes = 0
        real_probe = page._probe

        def probe(expression: str) -> str:
            nonlocal probes
            probes += 1
            if probes == 2:
                page.elements["#go"] = [_FakeElement(page, "new-go", visible=True, disabled=False)]
            return real_probe(expression)

        page._probe = probe  # type: ignore[method-assign]

        self._login(page, LoginStep(submit="#go"))

        assert [name for name, _ in page.clicks] == ["new-go"]

    def test_a_transition_during_the_visible_match_lookup_is_retried(self) -> None:
        from selenium.common.exceptions import StaleElementReferenceException

        page = _FakePage()
        page.add("a.signin", "mobile-copy", visible=False)
        page.add("a.signin", "header-link")
        driver_find_elements = _FakeDriver.find_elements
        calls = 0

        def flaky(self: _FakeDriver, by: str, selector: str) -> list[MagicMock]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise StaleElementReferenceException("the document changed")
            return driver_find_elements(self, by, selector)

        with patch.object(_FakeDriver, "find_elements", flaky):
            self._login(page, LoginStep(submit="a.signin"))

        assert calls == 2
        assert [name for name, _ in page.clicks] == ["header-link"]

    def test_the_wait_is_bounded_by_the_login_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time

        monkeypatch.setattr("graftpunk.plugins.login_ready._READY_MAX_WAIT", 60.0)
        page = _FakePage(never_quiet=True)
        page.add("#go", "go")

        started = time.monotonic()
        self._login(page, LoginStep(submit="#go"), timeout=0.2)

        assert time.monotonic() - started < 2.0
        assert [name for name, _ in page.clicks] == ["go"]


class _ReadinessScript:
    """A tab or driver whose readiness reads follow a script, one entry per read.

    Each entry is a JSON reading, or an exception to raise, or any other object
    to return as it is (an unparseable answer). The last entry repeats.
    """

    def __init__(self, *script: Any) -> None:
        self.script = script
        self.reads = 0

    def _next(self) -> Any:
        entry = self.script[min(self.reads, len(self.script) - 1)]
        self.reads += 1
        if isinstance(entry, BaseException):
            raise entry
        if isinstance(entry, tuple):
            state, quiet_ms = entry
            return json.dumps({"state": state, "quiet_ms": quiet_ms})
        return entry

    async def evaluate(self, expression: str, **kwargs: Any) -> Any:
        return self._next()

    def execute_script(self, script: str) -> Any:
        return self._next()


class TestReadinessClock:
    """The page keeps its quiet clock across steps; a wait counts only its own time."""

    @pytest.fixture(autouse=True)
    def _window(self, _fast_login_timings: None, monkeypatch: pytest.MonkeyPatch) -> None:
        """A window long enough to measure; requested after the fast timings so it wins."""
        monkeypatch.setattr("graftpunk.plugins.login_ready._READY_QUIET_WINDOW", 0.05)
        monkeypatch.setattr("graftpunk.plugins.login_ready._READY_MAX_WAIT", 5.0)

    @pytest.mark.asyncio
    async def test_quiet_from_before_the_wait_does_not_count_nodriver(self) -> None:
        import time

        from graftpunk.plugins.login_ready import await_page_ready_nodriver

        tab = _ReadinessScript(("complete", 60_000))
        started = time.monotonic()
        await await_page_ready_nodriver(tab, budget=30.0, step=2, site_name="t")

        assert time.monotonic() - started >= 0.05
        assert tab.reads > 1

    def test_quiet_from_before_the_wait_does_not_count_selenium(self) -> None:
        import time

        from graftpunk.plugins.login_ready import await_page_ready_selenium

        driver = _ReadinessScript(("complete", 60_000))
        started = time.monotonic()
        await_page_ready_selenium(driver, budget=30.0, step=2, site_name="t")

        assert time.monotonic() - started >= 0.05
        assert driver.reads > 1

    @pytest.mark.asyncio
    async def test_a_step_after_a_navigating_click_waits_for_the_new_page(self) -> None:
        """Step 1's click opens a new page. Step 2's first read still sees the old
        document, quiet since step 1; the new page hydrates a few reads later."""

        class TwoPages(_FakePage):
            navigated_at: int | None = None

            @property
            def hydrated(self) -> bool:
                return self.navigated_at is not None and self.ready_reads - self.navigated_at >= 4

            def _readiness(self) -> str:
                self.ready_reads += 1
                since = None if self.navigated_at is None else self.ready_reads - self.navigated_at
                if since is None or since == 1 or since >= 4:
                    return json.dumps({"state": "complete", "quiet_ms": 60_000})
                return json.dumps({"state": "loading", "quiet_ms": 0})

        page = TwoPages()
        nxt = page.add("#next", "next")
        page.add("#go", "go")

        async def navigate() -> None:
            page.clicks.append(("next", page.hydrated))
            page.navigated_at = page.ready_reads

        nxt.click = navigate  # type: ignore[method-assign]

        await _login(_plugin(LoginStep(submit="#next"), LoginStep(submit="#go")), page)

        assert page.clicks[-1] == ("go", True)

    @pytest.mark.asyncio
    async def test_a_document_still_loading_is_not_ready_however_quiet(self) -> None:
        from graftpunk.plugins.login_ready import await_page_ready_nodriver

        # Interactive for far longer than the window: only "complete" can end the wait.
        tab = _ReadinessScript(*([("interactive", 60_000)] * 200), ("complete", 60_000))
        await await_page_ready_nodriver(tab, budget=30.0, step=1, site_name="t")

        assert tab.reads > 200

    @pytest.mark.asyncio
    async def test_a_failed_or_odd_read_is_read_again(self) -> None:
        """Mid-navigation the execution context can vanish under the read; that is
        a not-ready read, not the end of the wait."""
        from graftpunk.plugins.login_ready import await_page_ready_nodriver

        tab = _ReadinessScript(
            RuntimeError("Execution context was destroyed"), object(), ("complete", 60_000)
        )
        await await_page_ready_nodriver(tab, budget=30.0, step=1, site_name="t")

        assert tab.reads > 3

    def test_a_failed_read_is_read_again_on_selenium(self) -> None:
        from graftpunk.plugins.login_ready import await_page_ready_selenium

        driver = _ReadinessScript(RuntimeError("stale"), None, ("complete", 60_000))
        await_page_ready_selenium(driver, budget=30.0, step=1, site_name="t")

        assert driver.reads > 3

    @pytest.mark.asyncio
    async def test_a_page_never_readable_ends_at_the_cap_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.plugins import login_ready

        monkeypatch.setattr(login_ready, "_READY_MAX_WAIT", 0.05)
        tab = _ReadinessScript(RuntimeError("no runtime"))
        with patch("graftpunk.plugins.login_ready.LOG") as log:
            await login_ready.await_page_ready_nodriver(tab, budget=30.0, step=3, site_name="t")

        (call,) = [c for c in log.info.call_args_list if c.args[0] == "login_page_ready_unreadable"]
        assert call.kwargs["step"] == 3
        assert "no runtime" in call.kwargs["error"]


class TestStaleRequests:
    def test_a_request_that_never_finishes_stops_counting_after_the_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time
        from types import SimpleNamespace

        from graftpunk.plugins import login_ready

        monkeypatch.setattr(login_ready, "_READY_MAX_WAIT", 0.02)
        activity = login_ready.NetworkActivity()
        activity._on_request(
            SimpleNamespace(request_id="poll", type_=SimpleNamespace(value="Fetch"))
        )
        assert activity.quiet_for() == 0.0
        time.sleep(0.03)
        assert activity.quiet_for() > 0.0

    def test_the_cutoff_is_the_cap_exactly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On a hand-stepped clock: busy just before the 5 s cap, quiet just after it."""
        from types import SimpleNamespace

        from graftpunk.plugins import login_ready

        monkeypatch.setattr(login_ready, "_READY_MAX_WAIT", 5.0)
        now = [100.0]
        monkeypatch.setattr(login_ready, "time", SimpleNamespace(monotonic=lambda: now[0]))
        activity = login_ready.NetworkActivity()
        activity._on_request(SimpleNamespace(request_id="poll", type_=SimpleNamespace(value="XHR")))

        now[0] = 104.9
        assert activity.quiet_for() == 0.0
        now[0] = 105.1
        assert activity.quiet_for() == pytest.approx(5.1)


class TestTransitions:
    @pytest.mark.asyncio
    async def test_a_transition_during_the_visible_match_lookup_is_retried(self) -> None:
        page = _FakePage()
        page.add("a.signin", "mobile-copy", visible=False)
        page.add("a.signin", "header-link")
        real = page.query_selector_all
        calls = 0

        async def flaky(selector: str) -> list[_FakeElement]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("Could not find node with given id")
            return await real(selector)

        page.query_selector_all = flaky  # type: ignore[method-assign]

        await _login(_plugin(LoginStep(submit="a.signin")), page)

        assert calls == 2
        assert [name for name, _ in page.clicks] == ["header-link"]

    @pytest.mark.asyncio
    async def test_after_a_retry_the_first_match_is_looked_up_again(self) -> None:
        """A handle taken before the wait may be detached by the time the match
        becomes usable, so it is not reused."""
        page = _FakePage()
        go = page.add("#go", "old-go", disabled=True)
        probes = 0
        real_probe = page._probe

        def probe(expression: str) -> str:
            nonlocal probes
            probes += 1
            if probes == 2:
                # The page re-rendered the button, enabled this time.
                page.elements["#go"] = [_FakeElement(page, "new-go", visible=True, disabled=False)]
            return real_probe(expression)

        page._probe = probe  # type: ignore[method-assign]
        assert go.name == "old-go"

        await _login(_plugin(LoginStep(submit="#go")), page)

        assert [name for name, _ in page.clicks] == ["new-go"]
