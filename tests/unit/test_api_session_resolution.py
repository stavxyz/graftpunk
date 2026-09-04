"""The API loader's pin contract: a bare name is a BASE, resolved at load (#151, #182).

Since #176, ``gp <site> login`` writes sessions as ``base@label``. A library
consumer holding the bare base name (the exact example in
``graftpunk/__init__.py``'s docstring) must keep working after a labelled
login: exact key first, and only on a not-found fall back to account
resolution via :func:`graftpunk.session_identity.resolve_account_session`.

Whatever slot the load lands on is the operating name for every write-back of
that invocation, which is why the loader has to hand that name back
(:func:`graftpunk.cache.load_session_for_api_resolved`): keying the refresh off
the bare name the caller typed silently drops it (#182).
"""

from __future__ import annotations

import pytest
import requests
import structlog
from structlog.testing import capture_logs

import graftpunk.cache as cache_mod
from graftpunk.cache import (
    cache_session,
    get_session_metadata,
    list_sessions,
    load_session_for_api,
    load_session_for_api_resolved,
    update_session_cookies,
)
from graftpunk.exceptions import (
    AmbiguousSessionError,
    SessionExpiredError,
    SessionNotFoundError,
)
from graftpunk.plugins.cli_plugin import SitePlugin
from graftpunk.session import BrowserSession
from graftpunk.session_identity import GP_ACCOUNT_ATTR


@pytest.fixture
def captured_logs():  # noqa: ANN201
    """``capture_logs()`` with graftpunk's import-time WARNING filter lifted.

    ``graftpunk.logging.ensure_library_defaults()`` runs at import and installs
    a filtering bound logger at WARNING, which ``capture_logs`` keeps — so
    INFO events would never reach the capture. ``reset_defaults()`` restores
    structlog's unfiltered builtins (the autouse ``_reset_structlog`` fixture
    does the same after every test, so this changes nothing for its neighbours).
    """
    structlog.reset_defaults()
    with capture_logs() as logs:
        yield logs


def _cached_session(account: str) -> BrowserSession:
    """A cached session whose account identifier survives the pickle round-trip.

    A plain ``requests.Session`` pickles through its ``__attrs__`` whitelist,
    which drops the rider attributes; the real ``BrowserSession`` round-trips
    them in ``__getstate__``/``__setstate__``. Standing in for the real thing
    is what makes the identity of the LOADED session observable — the point of
    every assertion below (a fresh Session would satisfy ``isinstance``).
    """
    session = BrowserSession.__new__(BrowserSession)
    requests.Session.__init__(session)
    session._backend_type = "nodriver"
    setattr(session, GP_ACCOUNT_ATTR, account)
    return session


def _count_listings(monkeypatch) -> dict[str, int]:  # noqa: ANN001
    """Count list_sessions() calls made through cache.py's own module global."""
    calls = {"count": 0}
    real_list_sessions = cache_mod.list_sessions

    def _counting_list_sessions(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls["count"] += 1
        return real_list_sessions(*args, **kwargs)

    monkeypatch.setattr(cache_mod, "list_sessions", _counting_list_sessions)
    return calls


def test_bare_name_resolves_to_single_labelled_account(fresh_backend, captured_logs) -> None:  # noqa: ANN001
    cache_session(_cached_session("alice@example.com"), "myshop@alice")

    api = load_session_for_api("myshop")

    assert getattr(api, GP_ACCOUNT_ATTR) == "alice@example.com"
    resolved = [e for e in captured_logs if e["event"] == "api_session_resolved_account"]
    assert resolved, f"expected api_session_resolved_account log event, got: {captured_logs}"
    assert resolved[0]["requested"] == "myshop"
    assert resolved[0]["resolved"] == "myshop@alice"
    # The creation event names the LOADED slot, and says what was asked for.
    created = [e for e in captured_logs if e["event"] == "created_api_session_from_cached_session"]
    assert created, f"expected created_api_session_from_cached_session, got: {captured_logs}"
    assert created[0]["name"] == "myshop@alice"
    assert created[0]["requested"] == "myshop"


def test_tuple_variant_returns_the_loaded_name_on_the_fallback(fresh_backend) -> None:  # noqa: ANN001
    cache_session(_cached_session("alice@example.com"), "myshop@alice")

    api, loaded = load_session_for_api_resolved("myshop")

    assert loaded == "myshop@alice"
    assert getattr(api, GP_ACCOUNT_ATTR) == "alice@example.com"


def test_tuple_variant_returns_the_requested_name_on_an_exact_hit(fresh_backend) -> None:  # noqa: ANN001
    cache_session(_cached_session("alice@example.com"), "myshop@alice")

    api, loaded = load_session_for_api_resolved("myshop@alice")

    assert loaded == "myshop@alice"
    assert getattr(api, GP_ACCOUNT_ATTR) == "alice@example.com"


def test_bare_name_ambiguous_raises_naming_both_candidates(fresh_backend) -> None:  # noqa: ANN001
    cache_session(_cached_session("alice@example.com"), "myshop@alice")
    cache_session(_cached_session("bob@example.com"), "myshop@bob")

    with pytest.raises(AmbiguousSessionError, match="myshop@alice") as exc_info:
        load_session_for_api("myshop")

    assert "myshop@bob" in str(exc_info.value)
    # The ambiguity is the whole story: the not-found miss that led here must
    # not ride along as "During handling of the above exception...".
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None


def test_bare_name_with_nothing_cached_raises_not_found_chained(fresh_backend) -> None:  # noqa: ANN001
    with pytest.raises(SessionNotFoundError, match="no account is cached under it") as exc_info:
        load_session_for_api("myshop")

    assert "myshop" in str(exc_info.value)
    # The original miss survives as __cause__ for anyone debugging the chain.
    assert isinstance(exc_info.value.__cause__, SessionNotFoundError)


def test_miss_logs_session_not_found_for_api(fresh_backend, captured_logs) -> None:  # noqa: ANN001
    with pytest.raises(SessionNotFoundError):
        load_session_for_api("myshop")

    events = [e for e in captured_logs if e["event"] == "session_not_found_for_api"]
    assert events, f"expected session_not_found_for_api log event, got: {captured_logs}"
    assert events[0]["name"] == "myshop"


def test_exact_labelled_name_does_not_list_sessions(fresh_backend, monkeypatch) -> None:  # noqa: ANN001
    cache_session(_cached_session("alice@example.com"), "myshop@alice")
    calls = _count_listings(monkeypatch)

    api = load_session_for_api("myshop@alice")

    assert getattr(api, GP_ACCOUNT_ATTR) == "alice@example.com"
    assert calls["count"] == 0


def test_expired_session_on_the_exact_key_propagates_without_listing(
    fresh_backend,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
) -> None:
    """A non-not-found failure is the answer, not a reason to go looking."""
    calls = _count_listings(monkeypatch)

    def _expired(name: str):  # noqa: ANN202
        raise SessionExpiredError(f"Session '{name}' failed integrity check.")

    monkeypatch.setattr(cache_mod, "load_session", _expired)

    with pytest.raises(SessionExpiredError, match="integrity check"):
        load_session_for_api("myshop")

    assert calls["count"] == 0


def test_labelled_name_that_misses_does_not_borrow_another_account(fresh_backend) -> None:  # noqa: ANN001
    """``myshop@carol`` is exact: alice's session is not a substitute for it."""
    cache_session(_cached_session("alice@example.com"), "myshop@alice")

    with pytest.raises(SessionNotFoundError, match="myshop@carol"):
        load_session_for_api("myshop@carol")


def test_legacy_bare_slot_wins_over_a_labelled_sibling(fresh_backend, monkeypatch) -> None:  # noqa: ANN001
    """A slot cached under the bare name itself is an exact hit — deliberately.

    This is the one input where the loader does NOT raise where the CLI's
    unpinned resolution would: the exact key answers first, and never lists.
    """
    cache_session(_cached_session("legacy@example.com"), "myshop")
    cache_session(_cached_session("alice@example.com"), "myshop@alice")
    calls = _count_listings(monkeypatch)

    api, loaded = load_session_for_api_resolved("myshop")

    assert loaded == "myshop"
    assert getattr(api, GP_ACCOUNT_ATTR) == "legacy@example.com"
    assert calls["count"] == 0


def test_miss_path_performs_exactly_one_listing(fresh_backend, monkeypatch) -> None:  # noqa: ANN001
    cache_session(_cached_session("alice@example.com"), "myshop@alice")
    calls = _count_listings(monkeypatch)

    load_session_for_api("myshop")

    assert calls["count"] == 1


def test_resolve_false_performs_no_listing_and_raises_on_a_bare_miss(
    fresh_backend,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
) -> None:
    """The callers that already resolved (CLI chain) must not list twice."""
    cache_session(_cached_session("alice@example.com"), "myshop@alice")
    calls = _count_listings(monkeypatch)

    with pytest.raises(SessionNotFoundError, match="myshop"):
        load_session_for_api("myshop", resolve=False)

    assert calls["count"] == 0


def test_invalid_name_raises_value_error_before_any_listing(fresh_backend, monkeypatch) -> None:  # noqa: ANN001
    """A name that cannot exist is a caller error, refused before storage."""
    calls = _count_listings(monkeypatch)

    with pytest.raises(ValueError, match=r"\[a-z0-9\]"):
        load_session_for_api("MyShop@Alice")

    assert calls["count"] == 0


def test_write_back_with_the_returned_name_refreshes_the_loaded_slot(fresh_backend) -> None:  # noqa: ANN001
    """The Critical (#182): the refresh has to land on the slot that was loaded."""
    cache_session(_cached_session("alice@example.com"), "myshop@alice")
    assert get_session_metadata("myshop@alice")["cookie_count"] == 0

    api, loaded = load_session_for_api_resolved("myshop")
    api.cookies.set("visited", "1")
    update_session_cookies(api, loaded)

    stored = get_session_metadata("myshop@alice")
    assert stored["cookie_count"] == 1
    assert stored["account_identifier"] == "alice@example.com"
    # No bare slot was forked open by the refresh.
    assert "myshop" not in list_sessions()


def test_update_session_cookies_uses_the_name_literally(fresh_backend) -> None:  # noqa: ANN001
    cache_session(_cached_session("alice@example.com"), "myshop@alice")

    # Best-effort no-op: "myshop" isn't a cached key, so the literal load fails
    # and update_session_cookies swallows it rather than resolving to
    # "myshop@alice" and re-caching under the bare name.
    update_session_cookies(requests.Session(), "myshop")

    assert "myshop" not in list_sessions()
    assert get_session_metadata("myshop@alice")["cookie_count"] == 0


def test_site_plugin_get_session_resolves_bare_base_name(fresh_backend) -> None:  # noqa: ANN001
    stored = _cached_session("alice@example.com")
    stored.cookies.set("planted", "1")
    cache_session(stored, "myshop@alice")

    class MyPlugin(SitePlugin):
        site_name = "myshop"
        session_name = "myshop"
        help_text = "Test plugin"

    plugin = MyPlugin()
    api = plugin.get_session()

    # Identity, not shape: a fresh requests.Session would satisfy isinstance.
    assert getattr(api, GP_ACCOUNT_ATTR) == "alice@example.com"
    assert api.cookies.get("planted") == "1"
