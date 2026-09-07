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
from graftpunk.session_identity import GP_ACCOUNT_ATTR, GP_SESSION_NAME_ATTR
from graftpunk.session_scope import operating_session


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


def test_successful_bare_name_resolution_emits_no_warnings(fresh_backend, captured_logs) -> None:  # noqa: ANN001
    """A miss on the way to a hit is a normal query result, not a problem (#178).

    ``load_session_for_api("myshop")`` misses the bare key before it resolves
    to the single ``myshop@alice`` account underneath it. That miss must not
    leave a WARNING behind, from either the backend's own load or cache.py's
    ``session_not_found_for_api``: the raised ``SessionNotFoundError`` is the
    signal, and this call never raises one.
    """
    cache_session(_cached_session("alice@example.com"), "myshop@alice")

    load_session_for_api("myshop")

    warnings = [e for e in captured_logs if e.get("log_level") == "warning"]
    assert not warnings, f"expected no WARNING-level events, got: {warnings}"


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
    # INFO, not WARNING: invisible under the library's WARNING default and
    # useful when tracing with logging turned up. The raised
    # SessionNotFoundError is the signal a caller acts on, not this log
    # line -- a WARNING here would triple-report the same miss once the
    # CLI boundary logs and renders its own error too (#178, PR #189 review).
    assert events[0]["log_level"] == "info"


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


def test_update_session_cookies_uses_an_unstamped_name_literally(fresh_backend) -> None:  # noqa: ANN001
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


def test_bare_write_back_from_get_session_follows_the_loaded_slot(fresh_backend) -> None:  # noqa: ANN001
    """#174: the author-facing pair (get_session + the bare name) must persist."""
    cache_session(_cached_session("alice@example.com"), "myshop@alice")

    class MyPlugin(SitePlugin):
        site_name = "myshop"
        session_name = "myshop"
        help_text = "Test plugin"

    plugin = MyPlugin()
    session = plugin.get_session()
    session.cookies.set("visited", "1")
    update_session_cookies(session, plugin.session_name)

    stored = get_session_metadata("myshop@alice")
    assert stored["cookie_count"] == 1
    assert stored["account_identifier"] == "alice@example.com"
    assert "myshop" not in list_sessions()


def test_bare_write_back_from_load_session_for_api_follows_the_loaded_slot(
    fresh_backend,  # noqa: ANN001
    captured_logs,  # noqa: ANN001
) -> None:
    cache_session(_cached_session("alice@example.com"), "myshop@alice")

    session = load_session_for_api("myshop")
    session.cookies.set("visited", "1")
    update_session_cookies(session, "myshop")

    assert get_session_metadata("myshop@alice")["cookie_count"] == 1
    assert "myshop" not in list_sessions()
    followed = [e for e in captured_logs if e["event"] == "session_write_back_follows_loaded_slot"]
    assert followed, f"expected session_write_back_follows_loaded_slot, got: {captured_logs}"
    assert followed[0]["requested"] == "myshop"
    assert followed[0]["target"] == "myshop@alice"


def test_a_rider_from_another_base_does_not_redirect_the_write_back(
    fresh_backend,  # noqa: ANN001
    captured_logs,  # noqa: ANN001
) -> None:
    """Following only ever happens inside one base: `othersite@bob` is not `myshop`."""
    cache_session(_cached_session("alice@example.com"), "myshop@alice")
    cache_session(_cached_session("bob@example.com"), "othersite@bob")

    session = load_session_for_api("othersite")
    session.cookies.set("visited", "1")
    update_session_cookies(session, "myshop")

    assert "myshop" not in list_sessions()
    assert get_session_metadata("myshop@alice")["cookie_count"] == 0
    assert get_session_metadata("othersite@bob")["cookie_count"] == 0
    assert not [e for e in captured_logs if e["event"] == "session_write_back_follows_loaded_slot"]


def test_a_labelled_argument_that_differs_from_the_rider_stays_literal(fresh_backend) -> None:  # noqa: ANN001
    """An explicit `base@label` is an instruction, and is still used verbatim."""
    cache_session(_cached_session("alice@example.com"), "myshop@alice")
    cache_session(_cached_session("bob@example.com"), "myshop@bob")

    session = load_session_for_api("myshop@alice")
    session.cookies.set("visited", "1")
    update_session_cookies(session, "myshop@bob")

    assert get_session_metadata("myshop@bob")["cookie_count"] == 1
    assert get_session_metadata("myshop@alice")["cookie_count"] == 0


def test_cache_session_stamps_the_slot_it_saved_under(fresh_backend) -> None:  # noqa: ANN001
    session = _cached_session("alice@example.com")
    cache_session(session, "myshop@alice")

    assert getattr(session, GP_SESSION_NAME_ATTR) == "myshop@alice"
    copied = cache_mod._api_session_from_session(session)
    assert getattr(copied, GP_SESSION_NAME_ATTR) == "myshop@alice"


def test_the_loaded_slot_name_never_rides_the_pickle(fresh_backend) -> None:  # noqa: ANN001
    """A cached blob must not carry a slot name that goes stale under another key."""
    import dill

    session = _cached_session("alice@example.com")
    setattr(session, GP_SESSION_NAME_ATTR, "myshop@alice")

    restored = dill.loads(dill.dumps(session))  # noqa: S301  (bytes this test just produced)

    assert getattr(restored, GP_ACCOUNT_ATTR) == "alice@example.com"
    assert getattr(restored, GP_SESSION_NAME_ATTR, None) is None

    # The loader is what puts the name back, from the key it read.
    cache_session(session, "myshop@alice")
    loaded = cache_mod.load_session("myshop@alice")
    assert getattr(loaded, GP_SESSION_NAME_ATTR) == "myshop@alice"


def test_an_existing_bare_slot_wins_over_the_stamped_one(fresh_backend) -> None:  # noqa: ANN001
    """The literal name answers first, so a bare slot is never passed over."""
    cache_session(_cached_session("legacy@example.com"), "myshop")
    cache_session(_cached_session("alice@example.com"), "myshop@alice")

    session = load_session_for_api("myshop@alice")
    session.cookies.set("visited", "1")
    update_session_cookies(session, "myshop")

    assert get_session_metadata("myshop")["cookie_count"] == 1
    assert get_session_metadata("myshop@alice")["cookie_count"] == 0


def test_a_non_string_stamp_keeps_the_write_literal(fresh_backend, captured_logs) -> None:  # noqa: ANN001
    """Only a real slot name may redirect a write; a test double answers anything."""
    cache_session(_cached_session("alice@example.com"), "myshop@alice")

    session = requests.Session()
    setattr(session, GP_SESSION_NAME_ATTR, object())
    session.cookies.set("visited", "1")
    update_session_cookies(session, "myshop")

    assert "myshop" not in list_sessions()
    assert get_session_metadata("myshop@alice")["cookie_count"] == 0
    # The no-op is the plain miss on "myshop", not a stamp that blew up.
    skipped = [e for e in captured_logs if e["event"] == "session_save_skipped_load_failed"]
    assert skipped, f"expected session_save_skipped_load_failed, got: {captured_logs}"
    assert skipped[0]["session_name"] == "myshop"
    assert skipped[0]["requested"] == "myshop"
    assert "not found" in skipped[0]["error"]


def test_the_slot_name_is_excluded_from_both_pickle_whitelists() -> None:
    """The registry, not a round-trip, is where the exclusion has to hold."""
    from graftpunk.tokens import PICKLED_SESSION_RIDER_ATTRS, SESSION_RIDER_ATTRS

    assert GP_SESSION_NAME_ATTR in SESSION_RIDER_ATTRS
    assert GP_SESSION_NAME_ATTR not in PICKLED_SESSION_RIDER_ATTRS
    # The selenium __getstate__ branch inherits requests' own whitelist.
    assert GP_SESSION_NAME_ATTR not in requests.Session.__attrs__


def _scope_plugin():  # noqa: ANN202
    """A plugin whose base session name is ``myshop``, with no session of its own."""

    class MyPlugin(SitePlugin):
        site_name = "myshop"
        session_name = "myshop"
        help_text = "Test plugin"

    return MyPlugin()


def test_get_session_explicit_labelled_name_loads_that_slot(fresh_backend) -> None:  # noqa: ANN001
    """An explicit name is the top of the chain, and a labelled one is exact."""
    cache_session(_cached_session("alice@example.com"), "myshop@alice")
    cache_session(_cached_session("bob@example.com"), "myshop@bob")

    api = _scope_plugin().get_session("myshop@bob")

    assert getattr(api, GP_ACCOUNT_ATTR) == "bob@example.com"


def test_get_session_explicit_bare_name_resolves_a_single_account(fresh_backend) -> None:  # noqa: ANN001
    cache_session(_cached_session("alice@example.com"), "myshop@alice")

    api = _scope_plugin().get_session("myshop")

    assert getattr(api, GP_ACCOUNT_ATTR) == "alice@example.com"


def test_get_session_explicit_bare_name_refuses_two_accounts(fresh_backend) -> None:  # noqa: ANN001
    cache_session(_cached_session("alice@example.com"), "myshop@alice")
    cache_session(_cached_session("bob@example.com"), "myshop@bob")

    with pytest.raises(AmbiguousSessionError) as exc:
        _scope_plugin().get_session("myshop")

    assert exc.value.candidates == ["myshop@alice", "myshop@bob"]


def test_get_session_follows_the_scope_with_two_accounts_cached(fresh_backend) -> None:  # noqa: ANN001
    """The issue's scenario: the CLI pinned myshop@bob, the handler calls get_session()."""
    cache_session(_cached_session("alice@example.com"), "myshop@alice")
    cache_session(_cached_session("bob@example.com"), "myshop@bob")

    with operating_session("myshop@bob"):
        api = _scope_plugin().get_session()

    assert getattr(api, GP_ACCOUNT_ATTR) == "bob@example.com"


def test_get_session_loads_the_scope_exact_only(fresh_backend) -> None:  # noqa: ANN001
    """The scope names a slot the dispatcher already resolved: a miss is a miss.

    With ``resolve=True`` this would have fallen back to ``myshop@alice``, so
    the raise is what proves the scope path does not resolve.
    """
    cache_session(_cached_session("alice@example.com"), "myshop@alice")

    with operating_session("myshop@bob"), pytest.raises(SessionNotFoundError):
        _scope_plugin().get_session()


def test_get_session_ignores_a_scope_for_a_foreign_base(fresh_backend) -> None:  # noqa: ANN001
    """A scope set while running another plugin must not steer this one."""
    cache_session(_cached_session("alice@example.com"), "myshop@alice")
    cache_session(_cached_session("carol@example.com"), "othersite@carol")

    with operating_session("othersite@carol"):
        api = _scope_plugin().get_session()

    assert getattr(api, GP_ACCOUNT_ATTR) == "alice@example.com"


def test_get_session_without_a_scope_still_refuses_two_accounts(fresh_backend) -> None:  # noqa: ANN001
    """Unchanged where nothing set a scope: the bare base resolves on its own terms."""
    cache_session(_cached_session("alice@example.com"), "myshop@alice")
    cache_session(_cached_session("bob@example.com"), "myshop@bob")

    with pytest.raises(AmbiguousSessionError):
        _scope_plugin().get_session()
