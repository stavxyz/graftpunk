"""Tests for _fill_field: verify-and-retype so typing into a detached node is caught.

Issue #148: on a page that re-renders its login form shortly after load, the
element handle resolved by ``_select_with_retry`` can be detached from the DOM
by the time ``send_keys`` runs. nodriver dispatches the key events happily, the
characters go nowhere, and nothing raises. ``_fill_field`` reads the value back
*by selector* (not via the handle) after a settle delay, and re-selects and
retypes on mismatch.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from graftpunk.exceptions import PluginError
from graftpunk.plugins import login_engine
from graftpunk.plugins.login_engine import _fill_field

pytestmark = pytest.mark.usefixtures("_fast_login_timings")


def _make_element() -> AsyncMock:
    return AsyncMock()


def _make_tab(elements: list[Any], readbacks: list[Any]) -> MagicMock:
    """Tab whose select() yields ``elements`` in order and evaluate() yields ``readbacks``."""
    tab = MagicMock()
    tab.select = AsyncMock(side_effect=elements)
    tab.evaluate = AsyncMock(side_effect=readbacks)
    return tab


class TestFillFieldHappyPath:
    @pytest.mark.asyncio
    async def test_value_verified_first_attempt_types_once(self) -> None:
        element = _make_element()
        tab = _make_tab([element], ["alice@example.com"])

        await _fill_field(
            tab, "input[name=email]", "alice@example.com", field_name="email", step_idx=1
        )

        element.click.assert_awaited_once()
        element.send_keys.assert_awaited_once_with("alice@example.com")
        element.clear_input.assert_not_awaited()
        assert tab.select.await_count == 1

    @pytest.mark.asyncio
    async def test_readback_queries_dom_by_selector_not_handle(self) -> None:
        """The check must ask the *document* what the field holds, not the stale handle."""
        element = _make_element()
        tab = _make_tab([element], ["secret"])

        await _fill_field(tab, "#pw", "secret", field_name="password", step_idx=1)

        js = tab.evaluate.await_args.args[0]
        assert "querySelector" in js
        assert '"#pw"' in js
        assert tab.evaluate.await_args.kwargs.get("return_by_value") is True

    @pytest.mark.asyncio
    async def test_settle_delay_precedes_readback(self) -> None:
        """Input.dispatchKeyEvent is acked before the DOM updates; read too early and
        even a successful fill looks empty (issue #148 gotcha)."""
        element = _make_element()
        tab = _make_tab([element], ["v"])
        order: list[str] = []
        tab.evaluate = AsyncMock(side_effect=lambda *a, **k: order.append("read") or "v")

        async def fake_sleep(delay: float) -> None:
            order.append(f"sleep:{delay}")

        with patch("graftpunk.plugins.login_engine.asyncio.sleep", side_effect=fake_sleep):
            await _fill_field(tab, "#f", "v", field_name="f", step_idx=1)

        assert order == [f"sleep:{login_engine._FIELD_SETTLE_DELAY}", "read"]

    @pytest.mark.asyncio
    async def test_empty_value_skips_verification(self) -> None:
        element = _make_element()
        tab = _make_tab([element], [])

        await _fill_field(tab, "#f", "", field_name="f", step_idx=1)

        element.send_keys.assert_awaited_once_with("")
        tab.evaluate.assert_not_awaited()


class TestFillFieldStaleNodeRecovery:
    @pytest.mark.asyncio
    async def test_stale_handle_is_reselected_and_retyped(self) -> None:
        """First handle was detached (readback empty); second select gets the live node."""
        stale, live = _make_element(), _make_element()
        tab = _make_tab([stale, live], ["", "alice"])

        await _fill_field(tab, "input[name=email]", "alice", field_name="email", step_idx=1)

        assert tab.select.await_count == 2
        stale.send_keys.assert_awaited_once_with("alice")
        live.send_keys.assert_awaited_once_with("alice")
        # The retry clears first so a late-arriving partial fill is not doubled.
        stale.clear_input.assert_not_awaited()
        live.clear_input.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_partial_value_triggers_retry(self) -> None:
        first, second = _make_element(), _make_element()
        tab = _make_tab([first, second], ["ali", "alice"])

        await _fill_field(tab, "#e", "alice", field_name="email", step_idx=1)

        second.clear_input.assert_awaited_once()
        second.send_keys.assert_awaited_once_with("alice")

    @pytest.mark.asyncio
    async def test_gives_up_with_clear_error_after_max_attempts(self) -> None:
        elements = [_make_element() for _ in range(login_engine._FIELD_FILL_ATTEMPTS)]
        tab = _make_tab(elements, [""] * login_engine._FIELD_FILL_ATTEMPTS)

        with pytest.raises(PluginError) as excinfo:
            await _fill_field(tab, "input[name=email]", "alice", field_name="email", step_idx=2)

        msg = str(excinfo.value)
        assert "Step 2" in msg
        assert "email" in msg
        assert "input[name=email]" in msg
        assert "did not accept input" in msg
        assert "alice" not in msg  # never leak the credential
        assert tab.select.await_count == login_engine._FIELD_FILL_ATTEMPTS

    @pytest.mark.asyncio
    async def test_element_missing_on_first_select_raises_not_found(self) -> None:
        tab = _make_tab([], [])
        tab.select = AsyncMock(return_value=None)

        with pytest.raises(PluginError, match="not found"):
            await _fill_field(tab, "#gone", "x", field_name="user", step_idx=1)

    @pytest.mark.asyncio
    async def test_mismatch_log_never_contains_value(self) -> None:
        stale, live = _make_element(), _make_element()
        tab = _make_tab([stale, live], ["", "hunter2"])

        with patch("graftpunk.plugins.login_engine.LOG") as mock_log:
            await _fill_field(tab, "#pw", "hunter2", field_name="password", step_idx=1)

        for call in mock_log.method_calls:
            assert "hunter2" not in repr(call)


class TestFillFieldReadback:
    @pytest.mark.asyncio
    async def test_readback_error_is_best_effort_no_retry(self) -> None:
        """If the value cannot be read back (evaluate raises), keep the single fill."""
        element = _make_element()
        tab = _make_tab([element], [RuntimeError("evaluate unavailable")])

        await _fill_field(tab, "#f", "v", field_name="f", step_idx=1)

        assert tab.select.await_count == 1
        element.send_keys.assert_awaited_once_with("v")

    @pytest.mark.asyncio
    async def test_remote_object_with_empty_value_counts_as_empty(self) -> None:
        """nodriver's evaluate(return_by_value=True) returns the RemoteObject itself
        when the value is falsy, so '' arrives wrapped."""
        remote = MagicMock()
        remote.value = ""
        remote.type_ = "string"
        stale, live = _make_element(), _make_element()
        tab = _make_tab([stale, live], [remote, "alice"])

        await _fill_field(tab, "#e", "alice", field_name="email", step_idx=1)

        assert tab.select.await_count == 2

    @pytest.mark.asyncio
    async def test_non_string_readback_is_unverifiable(self) -> None:
        """A null result (selector matched nothing at read time) cannot be verified;
        do not loop on it."""
        remote = MagicMock()
        remote.value = None
        remote.type_ = "object"
        element = _make_element()
        tab = _make_tab([element], [remote])

        await _fill_field(tab, "#e", "alice", field_name="email", step_idx=1)

        assert tab.select.await_count == 1
