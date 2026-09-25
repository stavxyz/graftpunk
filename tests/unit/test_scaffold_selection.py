"""Which commands a render or an insert produces, under which names (graft skill
spec, 2026-09-21)."""

from __future__ import annotations

import ast
from pathlib import Path
from types import MappingProxyType

import pytest

import graftpunk.devtools.scaffold.selection as selection
from graftpunk.devtools.plugin_project import CommandView, Span
from graftpunk.devtools.scaffold.selection import CommandSelection, PlannedCommand
from graftpunk.har.digest import Endpoint
from graftpunk.har.naming import registered_name
from graftpunk.plugins import command


def _endpoint() -> Endpoint:
    return Endpoint(
        host="myshop.example.com",
        template="/orders/{order_id}",
        methods=("GET",),
        count=1,
        statuses=(200,),
        content_type="application/json",
        query_params={},
        body_params={},
        body_kind="none",
        shape=None,
        custom_headers=(),
        examples=(),
    )


def test_the_registered_name_is_the_pin_or_the_kebab_cased_identifier() -> None:
    assert PlannedCommand("order_detail", None, "GET", _endpoint()).registered_name == (
        "order-detail"
    )
    assert PlannedCommand("Orders", "Orders", "GET", _endpoint()).registered_name == "Orders"


@pytest.mark.parametrize(("pin", "expected"), [(None, "order-detail"), ("order", "order")])
def test_the_decorator_the_reader_and_the_plan_register_one_name(
    pin: str | None, expected: str
) -> None:
    """The runtime registers, the reader reports, and the scaffold plans the same
    name for a command, pinned or not: all three call naming.registered_name."""

    def order_detail(self: object, ctx: object) -> None:
        return None

    runtime = command(name=pin)(order_detail)._command_meta.name
    keywords = MappingProxyType({} if pin is None else {"name": pin})
    reader = CommandView("order_detail", Span(1, 2), keywords).cli_name
    planned = PlannedCommand("order_detail", pin, "GET", _endpoint()).registered_name
    assert runtime == reader == planned == registered_name(pin, "order_detail") == expected


def test_a_selection_is_a_plain_triple() -> None:
    assert CommandSelection("order", "GET", "/orders/{order_id}").name == "order"


def test_selection_does_not_import_the_renderer() -> None:
    """render.py imports selection.py, never the reverse."""
    assert selection.__file__ is not None
    tree = ast.parse(Path(selection.__file__).read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "graftpunk.devtools.scaffold.render" not in imported


def test_soft_keywords_cover_the_running_interpreters_own_list() -> None:
    """_SOFT_KEYWORDS is fixed to 3.13's list (the newest CI runs), not read from
    keyword.softkwlist, so a name refused on one Python is refused on every one;
    this pins that the hand-copied tuple has not fallen behind the interpreter
    actually running the suite."""
    import keyword

    assert set(selection._SOFT_KEYWORDS) >= set(keyword.softkwlist)
