"""The browser-free modules that hold the stored session format."""

import dill

import graftpunk.session_riders
import graftpunk.tokens
from tests.unit.session_compat_cases import (
    assert_leaf_imports,
    nodriver_session,
    pickled_global_names,
)

MOVED = (
    "CachedToken",
    "_CACHE_ATTR",
    "_CSRF_TOKENS_ATTR",
    "_CSRF_ORIGIN_ATTR",
    "_HEADER_ROLES_ATTR",
    "_HEADER_ROLE_ORIGINS_ATTR",
    "SESSION_RIDER_ATTRS",
    "SESSION_RIDER_DEFAULTS",
    "_MEMORY_ONLY_SESSION_RIDER_ATTRS",
    "PICKLED_SESSION_RIDER_ATTRS",
)


def test_session_riders_is_a_leaf():
    assert_leaf_imports("graftpunk.session_riders", {"graftpunk.session_identity"})


def test_a_pickle_with_cached_tokens_names_only_the_frozen_paths():
    names = pickled_global_names(dill.dumps(nodriver_session(tokens=True)))
    assert {"graftpunk.tokens CachedToken", "graftpunk.session BrowserSession"} <= names
    assert not any("session_riders" in n for n in names)


def test_cached_tokens_round_trip_through_dill():
    session = nodriver_session(tokens=True)
    restored = dill.loads(dill.dumps(session))  # noqa: S301 (written by this test)
    assert restored._gp_cached_tokens == session._gp_cached_tokens
    assert restored._gp_cached_tokens


def test_tokens_still_exports_every_moved_name():
    for name in MOVED:
        assert getattr(graftpunk.tokens, name) is getattr(graftpunk.session_riders, name)


def test_tokens_still_exports_the_identity_names_it_imported_in_1_44_0():
    import graftpunk.session_identity

    for name in ("GP_ACCOUNT_ATTR", "GP_SESSION_NAME_ATTR"):
        assert getattr(graftpunk.tokens, name) is getattr(graftpunk.session_identity, name)


def test_the_loader_exceptions_of_the_record_format_are_exported_from_the_package():
    import graftpunk
    import graftpunk.exceptions

    for name in ("NewerSessionRecord", "RealSessionClassUnavailable"):
        assert name in graftpunk.__all__
        assert getattr(graftpunk, name) is getattr(graftpunk.exceptions, name)


def test_session_class_paths_name_the_three_kinds():
    assert dict(graftpunk.session_riders.SESSION_CLASS_PATHS) == {
        ("graftpunk.session", "BrowserSession"): "browser",
        ("graftpunk.graftpunk_session", "GraftpunkSession"): "api",
        ("requests.sessions", "Session"): "requests",
    }
