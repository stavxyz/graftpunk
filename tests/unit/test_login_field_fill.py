"""Tests for _fill_field: verify-and-retype so typing into a detached node is caught.

Issue #148: on a page that re-renders its login form shortly after load, the
element handle resolved by ``_select_with_retry`` can be detached from the DOM
by the time ``send_keys`` runs. nodriver dispatches the key events happily, the
characters go nowhere, and nothing raises. ``_fill_field`` reads the value back
*by selector* (not via the handle) after a settle delay, and re-selects and
retypes on mismatch.

Read-back protocol: the page-side JS returns ``JSON.stringify({found, value})``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from graftpunk.exceptions import PluginError
from graftpunk.plugins import login_engine
from graftpunk.plugins.login_engine import _fill_field, _read_field_value

pytestmark = pytest.mark.usefixtures("_fast_login_timings")

N = login_engine._FIELD_FILL_ATTEMPTS


def _rb(value: str) -> str:
    """A read-back result for a present element holding ``value``."""
    return json.dumps({"found": True, "value": value})


_MISSING = json.dumps({"found": False, "value": None})


def _make_element() -> AsyncMock:
    return AsyncMock()


def _make_tab(elements: list[Any], readbacks: list[Any]) -> MagicMock:
    """Tab whose select() yields ``elements`` in order and evaluate() yields ``readbacks``.

    Each attempt reads back twice: once after clearing (must be empty) and
    once after typing (must equal the value); an empty ``value`` skips the
    second read.
    """
    tab = MagicMock()
    tab.select = AsyncMock(side_effect=elements)
    tab.evaluate = AsyncMock(side_effect=readbacks)
    return tab


class TestFillFieldHappyPath:
    @pytest.mark.asyncio
    async def test_value_verified_first_attempt_types_once(self) -> None:
        element = _make_element()
        tab = _make_tab([element], [_rb(""), _rb("alice@example.com")])

        await _fill_field(
            tab, "input[name=email]", "alice@example.com", field_name="email", step_idx=1
        )

        element.click.assert_awaited_once()
        element.clear_input.assert_awaited_once()  # every attempt starts from empty
        element.send_keys.assert_awaited_once_with("alice@example.com")
        assert tab.select.await_count == 1

    @pytest.mark.asyncio
    async def test_readback_queries_dom_by_selector_not_handle(self) -> None:
        """The check must ask the *document* what the field holds, not the stale handle."""
        element = _make_element()
        tab = _make_tab([element], [_rb(""), _rb("secret")])

        await _fill_field(tab, "#pw", "secret", field_name="password", step_idx=1)

        js = tab.evaluate.await_args.args[0]
        assert "querySelector" in js
        assert '"#pw"' in js
        assert "JSON.stringify" in js
        assert tab.evaluate.await_args.kwargs.get("return_by_value") is True

    @pytest.mark.asyncio
    async def test_settle_delay_precedes_verification_readback(self) -> None:
        """Input.dispatchKeyEvent is acked before the DOM updates; read too early and
        even a successful fill looks empty (issue #148 gotcha)."""
        element = _make_element()
        order: list[str] = []
        tab = MagicMock()
        tab.select = AsyncMock(return_value=element)
        reads = iter([_rb(""), _rb("v")])
        tab.evaluate = AsyncMock(side_effect=lambda *a, **k: order.append("read") or next(reads))
        element.send_keys = AsyncMock(side_effect=lambda v: order.append("type"))

        async def fake_sleep(delay: float) -> None:
            order.append(f"sleep:{delay}")

        with patch("graftpunk.plugins.login_engine.asyncio.sleep", side_effect=fake_sleep):
            await _fill_field(tab, "#f", "v", field_name="f", step_idx=1)

        assert order == ["read", "type", f"sleep:{login_engine._FIELD_SETTLE_DELAY}", "read"]

    @pytest.mark.asyncio
    async def test_empty_value_skips_verification(self) -> None:
        element = _make_element()
        tab = _make_tab([element], [_rb("")])

        await _fill_field(tab, "#f", "", field_name="f", step_idx=1)

        element.send_keys.assert_awaited_once_with("")
        assert tab.evaluate.await_count == 1  # only the post-clear check

    @pytest.mark.asyncio
    async def test_prefilled_field_is_cleared_before_typing(self) -> None:
        """A browser-remembered value must not be appended to."""
        element = _make_element()
        tab = _make_tab([element], [_rb(""), _rb("alice")])

        await _fill_field(tab, "#e", "alice", field_name="email", step_idx=1)

        assert element.clear_input.await_count == 1
        assert element.send_keys.await_count == 1


class TestFillFieldStaleNodeRecovery:
    @pytest.mark.asyncio
    async def test_stale_handle_is_reselected_and_retyped(self) -> None:
        """First handle was detached (readback empty); second select gets the live node."""
        stale, live = _make_element(), _make_element()
        tab = _make_tab([stale, live], [_rb(""), _rb(""), _rb(""), _rb("alice")])

        await _fill_field(tab, "input[name=email]", "alice", field_name="email", step_idx=1)

        assert tab.select.await_count == 2
        stale.send_keys.assert_awaited_once_with("alice")
        live.send_keys.assert_awaited_once_with("alice")
        live.clear_input.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_partial_value_triggers_retry(self) -> None:
        first, second = _make_element(), _make_element()
        tab = _make_tab([first, second], [_rb(""), _rb("ali"), _rb(""), _rb("alice")])

        await _fill_field(tab, "#e", "alice", field_name="email", step_idx=1)

        second.clear_input.assert_awaited_once()
        second.send_keys.assert_awaited_once_with("alice")

    @pytest.mark.asyncio
    async def test_late_keystrokes_after_clear_are_cleared_again(self) -> None:
        """A previous attempt's characters can flush after the clear; never type on
        top of them (doubled text submits as something that looks deliberate)."""
        element = _make_element()
        tab = _make_tab([element], [_rb("stray"), _rb("alice")])

        await _fill_field(tab, "#e", "alice", field_name="email", step_idx=1)

        assert element.clear_input.await_count == 2
        element.send_keys.assert_awaited_once_with("alice")

    @pytest.mark.asyncio
    async def test_gives_up_with_clear_error_when_value_never_lands(self) -> None:
        elements = [_make_element() for _ in range(N)]
        tab = _make_tab(elements, [_rb("")] * (2 * N))

        with pytest.raises(PluginError) as excinfo:
            await _fill_field(tab, "input[name=email]", "alice", field_name="email", step_idx=2)

        msg = str(excinfo.value)
        assert "Step 2" in msg
        assert "email" in msg
        assert "input[name=email]" in msg
        assert "did not accept input" in msg
        assert "alice" not in msg  # never leak the credential
        assert tab.select.await_count == N

    @pytest.mark.asyncio
    async def test_selector_vanishing_after_select_is_a_failure_not_unverifiable(self) -> None:
        """The form was re-rendered into a shape the selector cannot address: the
        silent empty submit from #148. Must not be accepted."""
        elements = [_make_element() for _ in range(N)]
        tab = _make_tab(elements, [_MISSING] * (2 * N))

        with pytest.raises(PluginError, match="did not accept input"):
            await _fill_field(tab, "#e", "alice", field_name="email", step_idx=1)

    @pytest.mark.asyncio
    async def test_non_empty_but_different_value_proceeds_with_warning(self) -> None:
        """Sites that lowercase, trim or mask input hold a value that differs from
        what was typed; that is not the detached-node bug and must not fail login."""
        elements = [_make_element() for _ in range(N)]
        tab = _make_tab(elements, [_rb(""), _rb("alice")] * N)

        with patch("graftpunk.plugins.login_engine.LOG") as mock_log:
            await _fill_field(tab, "#e", "Alice", field_name="email", step_idx=1)

        events = [c.args[0] for c in mock_log.warning.call_args_list]
        assert events[-1] == "login_field_value_normalized"
        assert tab.select.await_count == N

    @pytest.mark.asyncio
    async def test_interaction_exception_is_retried(self) -> None:
        """click/clear on a detached handle can raise; that is an attempt, not the end."""
        stale, live = _make_element(), _make_element()
        stale.click = AsyncMock(side_effect=RuntimeError("Could not find node with given id"))
        tab = _make_tab([stale, live], [_rb(""), _rb("alice")])

        await _fill_field(tab, "#e", "alice", field_name="email", step_idx=1)

        live.send_keys.assert_awaited_once_with("alice")
        assert tab.select.await_count == 2

    @pytest.mark.asyncio
    async def test_interaction_exception_on_final_attempt_propagates(self) -> None:
        elements = [_make_element() for _ in range(N)]
        for el in elements:
            el.click = AsyncMock(side_effect=RuntimeError("node gone"))
        tab = _make_tab(elements, [])

        with pytest.raises(RuntimeError, match="node gone"):
            await _fill_field(tab, "#e", "alice", field_name="email", step_idx=1)

        assert tab.select.await_count == N

    @pytest.mark.asyncio
    async def test_element_missing_on_first_select_raises_not_found(self) -> None:
        tab = _make_tab([], [])
        tab.select = AsyncMock(return_value=None)

        with pytest.raises(PluginError, match="not found"):
            await _fill_field(tab, "#gone", "x", field_name="user", step_idx=1)

    @pytest.mark.asyncio
    async def test_logs_never_contain_the_value(self) -> None:
        stale, live = _make_element(), _make_element()
        tab = _make_tab([stale, live], [_rb(""), _rb(""), _rb(""), _rb("hunter2")])

        with patch("graftpunk.plugins.login_engine.LOG") as mock_log:
            await _fill_field(tab, "#pw", "hunter2", field_name="password", step_idx=1)

        for call in mock_log.method_calls:
            assert "hunter2" not in repr(call)


class TestReadFieldValue:
    @pytest.mark.asyncio
    async def test_present_element_returns_value(self) -> None:
        tab = MagicMock()
        tab.evaluate = AsyncMock(return_value=_rb("alice"))
        assert await _read_field_value(tab, "#e") == "alice"

    @pytest.mark.asyncio
    async def test_empty_value_is_empty_string_not_none(self) -> None:
        tab = MagicMock()
        tab.evaluate = AsyncMock(return_value=_rb(""))
        assert await _read_field_value(tab, "#e") == ""

    @pytest.mark.asyncio
    async def test_missing_element_is_empty_string(self) -> None:
        tab = MagicMock()
        tab.evaluate = AsyncMock(return_value=_MISSING)
        assert await _read_field_value(tab, "#e") == ""

    @pytest.mark.asyncio
    async def test_evaluate_raising_is_none(self) -> None:
        tab = MagicMock()
        tab.evaluate = AsyncMock(side_effect=RuntimeError("evaluate unavailable"))
        assert await _read_field_value(tab, "#e") is None

    @pytest.mark.asyncio
    async def test_exception_details_object_is_none_and_logged(self) -> None:
        """nodriver returns an ExceptionDetails object (not a str) when the JS throws."""
        details = MagicMock()
        details.text = "Uncaught"
        tab = MagicMock()
        tab.evaluate = AsyncMock(return_value=details)

        with patch("graftpunk.plugins.login_engine.LOG") as mock_log:
            assert await _read_field_value(tab, "#e") is None

        assert mock_log.debug.call_args.args[0] == "login_field_readback_unparseable"
        assert mock_log.debug.call_args.kwargs["detail"] == "Uncaught"

    @pytest.mark.asyncio
    async def test_unparseable_string_is_none(self) -> None:
        tab = MagicMock()
        tab.evaluate = AsyncMock(return_value="not json")
        assert await _read_field_value(tab, "#e") is None


class TestFillFieldUnverifiable:
    @pytest.mark.asyncio
    async def test_readback_error_is_best_effort_no_retry(self) -> None:
        """If the value cannot be read back (evaluate raises), keep the single fill."""
        element = _make_element()
        tab = _make_tab([element], [RuntimeError("no"), RuntimeError("no")])

        await _fill_field(tab, "#f", "v", field_name="f", step_idx=1)

        assert tab.select.await_count == 1
        element.send_keys.assert_awaited_once_with("v")
