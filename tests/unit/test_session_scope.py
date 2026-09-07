"""The operating session scope: the slot one dispatch is about (#174)."""

from __future__ import annotations

import asyncio

import pytest

from graftpunk.session_scope import (
    OperatingSession,
    current_operating_session,
    operating_session,
    operating_session_for,
    resolve_load_target,
)


def test_no_scope_outside_a_dispatch() -> None:
    assert current_operating_session() is None
    assert operating_session_for("myshop") is None


def test_set_read_and_restore() -> None:
    with operating_session("myshop@alice", "alice@example.com") as scope:
        assert scope == OperatingSession("myshop@alice", "alice@example.com")
        current = current_operating_session()
        assert current is not None
        assert current.name == "myshop@alice"
        assert current.identifier == "alice@example.com"
    assert current_operating_session() is None


def test_identifier_defaults_to_none() -> None:
    with operating_session("myshop"):
        current = current_operating_session()
        assert current is not None
        assert current.identifier is None


def test_restores_when_the_block_raises() -> None:
    """The reset is in a finally: a failed handler must not leak the scope."""
    with pytest.raises(RuntimeError), operating_session("myshop@alice"):
        raise RuntimeError("handler blew up")
    assert current_operating_session() is None


def test_for_base_matches_a_labelled_scope() -> None:
    with operating_session("myshop@alice"):
        scope = operating_session_for("myshop")
        assert scope is not None
        assert scope.name == "myshop@alice"


def test_for_base_matches_a_bare_scope() -> None:
    with operating_session("myshop"):
        scope = operating_session_for("myshop")
        assert scope is not None
        assert scope.name == "myshop"


def test_for_base_ignores_a_foreign_base() -> None:
    """A scope set for one plugin must never steer another (the base-scoped guard)."""
    with operating_session("othersite@bob"):
        assert operating_session_for("myshop") is None
        assert current_operating_session() is not None


def test_nesting_restores_the_outer_value() -> None:
    with operating_session("myshop@alice"):
        with operating_session("myshop@bob"):
            inner = current_operating_session()
            assert inner is not None
            assert inner.name == "myshop@bob"
        outer = current_operating_session()
        assert outer is not None
        assert outer.name == "myshop@alice"
    assert current_operating_session() is None


@pytest.mark.parametrize("name", ["", "MyShop@Alice", "../../x", "myshop@", "a@b@c"])
def test_an_invalid_name_is_refused(name: str) -> None:
    with pytest.raises(ValueError), operating_session(name):
        pass  # pragma: no cover - the context manager raises before it yields


def test_an_invalid_name_leaves_the_outer_scope_untouched() -> None:
    """Validation runs before the ContextVar is set, so a refusal changes nothing."""
    with operating_session("myshop@alice"):
        with pytest.raises(ValueError), operating_session("MyShop@Alice"):
            pass  # pragma: no cover - the context manager raises before it yields
        current = current_operating_session()
        assert current is not None
        assert current.name == "myshop@alice"


def test_the_scope_is_visible_inside_asyncio_run() -> None:
    """The propagation proof, not the docs: the login command runs async logins
    via asyncio.run() inside the scope, and the coroutine must see it."""
    seen: list[str | None] = []

    async def coro() -> None:
        scope = current_operating_session()
        seen.append(scope.name if scope else None)

    with operating_session("myshop@bob", "bob@example.com"):
        asyncio.run(coro())

    assert seen == ["myshop@bob"]


class TestResolveLoadTarget:
    """The one precedence chain: explicit, then scope, then the bare base."""

    def test_explicit_labelled_name_loads_exact(self) -> None:
        assert resolve_load_target("myshop", "myshop@alice") == ("myshop@alice", False)

    def test_explicit_bare_name_resolves(self) -> None:
        assert resolve_load_target("myshop", "myshop") == ("myshop", True)

    def test_a_scope_for_the_base_loads_exact(self) -> None:
        """The dispatcher already resolved that name, so a second listing is waste."""
        with operating_session("myshop@bob"):
            assert resolve_load_target("myshop") == ("myshop@bob", False)

    def test_a_scope_for_a_foreign_base_is_ignored(self) -> None:
        with operating_session("othersite@bob"):
            assert resolve_load_target("myshop") == ("myshop", True)

    def test_no_scope_falls_back_to_the_resolving_base(self) -> None:
        assert resolve_load_target("myshop") == ("myshop", True)

    def test_an_explicit_name_wins_over_the_scope(self) -> None:
        with operating_session("myshop@bob"):
            assert resolve_load_target("myshop", "myshop@alice") == ("myshop@alice", False)
