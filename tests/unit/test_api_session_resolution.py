"""load_session_for_api / SitePlugin.get_session() resolve a bare base name (#151).

Since #176, ``gp <site> login`` writes sessions as ``base@label``. A library
consumer holding the bare base name (the exact example in ``graftpunk/__init__.py``'s
docstring) must keep working after a labelled login: exact key first, and only
on a not-found fall back to account resolution via
:func:`graftpunk.session_identity.resolve_account_session`.
"""

from __future__ import annotations

from typing import Any

import requests

from graftpunk.cache import (
    cache_session,
    list_sessions,
    load_session_for_api,
    update_session_cookies,
)
from graftpunk.exceptions import AmbiguousSessionError, SessionNotFoundError
from graftpunk.plugins.cli_plugin import SitePlugin
from graftpunk.session_identity import GP_ACCOUNT_ATTR


def _cached_session(account: str) -> requests.Session:
    s = requests.Session()
    setattr(s, GP_ACCOUNT_ATTR, account)
    return s


def test_bare_name_resolves_to_single_labelled_account(fresh_backend, monkeypatch) -> None:  # noqa: ANN001
    cache_session(_cached_session("alice@example.com"), "myshop@alice")

    import graftpunk.cache as cache_mod

    info_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    real_info = cache_mod.LOG.info

    def _recording_info(*args, **kwargs):  # noqa: ANN001, ANN202
        info_calls.append((args, kwargs))
        return real_info(*args, **kwargs)

    monkeypatch.setattr(cache_mod.LOG, "info", _recording_info)

    api = load_session_for_api("myshop")

    assert isinstance(api, requests.Session)
    resolved_events = [c for c in info_calls if c[0] and c[0][0] == "api_session_resolved_account"]
    assert resolved_events, f"expected api_session_resolved_account log event, got: {info_calls}"
    _, kwargs = resolved_events[0]
    assert kwargs.get("requested") == "myshop"
    assert kwargs.get("resolved") == "myshop@alice"


def test_bare_name_ambiguous_raises_naming_both_candidates(fresh_backend) -> None:  # noqa: ANN001
    cache_session(_cached_session("alice@example.com"), "myshop@alice")
    cache_session(_cached_session("bob@example.com"), "myshop@bob")

    try:
        load_session_for_api("myshop")
        raise AssertionError("expected AmbiguousSessionError")
    except AmbiguousSessionError as exc:
        message = str(exc)
        assert "myshop@alice" in message
        assert "myshop@bob" in message


def test_bare_name_with_nothing_cached_raises_original_not_found(fresh_backend) -> None:  # noqa: ANN001
    try:
        load_session_for_api("myshop")
        raise AssertionError("expected SessionNotFoundError")
    except SessionNotFoundError as exc:
        assert "myshop" in str(exc)


def test_exact_labelled_name_does_not_list_sessions(fresh_backend, monkeypatch) -> None:  # noqa: ANN001
    cache_session(_cached_session("alice@example.com"), "myshop@alice")

    calls = {"count": 0}
    import graftpunk.cache as cache_mod

    real_list_sessions = cache_mod.list_sessions

    def _counting_list_sessions(*args, **kwargs):  # noqa: ANN001, ANN202
        calls["count"] += 1
        return real_list_sessions(*args, **kwargs)

    monkeypatch.setattr(cache_mod, "list_sessions", _counting_list_sessions)

    api = load_session_for_api("myshop@alice")

    assert isinstance(api, requests.Session)
    assert calls["count"] == 0


def test_write_path_stays_literal_no_bare_slot_created(fresh_backend) -> None:  # noqa: ANN001
    cache_session(_cached_session("alice@example.com"), "myshop@alice")

    # Best-effort no-op: "myshop" isn't a cached key, so the literal load fails
    # and update_session_cookies swallows it rather than resolving to
    # "myshop@alice" and re-caching under the bare name.
    update_session_cookies(requests.Session(), "myshop")

    assert "myshop" not in list_sessions()


def test_site_plugin_get_session_resolves_bare_base_name(fresh_backend) -> None:  # noqa: ANN001
    cache_session(_cached_session("alice@example.com"), "myshop@alice")

    class MyPlugin(SitePlugin):
        site_name = "myshop"
        session_name = "myshop"
        help_text = "Test plugin"

    plugin = MyPlugin()
    api = plugin.get_session()

    assert isinstance(api, requests.Session)
