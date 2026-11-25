"""A saved header that is a default of some ``requests`` release is not a captured
value: it never replaces what the loading session has (#399, recurrence of #52)."""

import json
from collections.abc import Callable

import dill
import pytest

from graftpunk.cache import load_session_for_api, load_session_for_api_from_bytes
from graftpunk.client_headers import is_library_default
from graftpunk.graftpunk_session import GraftpunkSession
from tests.unit.session_compat_cases import (
    TEST_KEY,
    encrypt,
    nodriver_session,
    record_bytes,
)

BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/144.0.0.0"
OTHER_VERSION_UA = "python-requests/0.0.1"
BROWSER_ROLES = {"navigation": {"User-Agent": BROWSER_UA, "Accept": "text/html"}}

Build = Callable[[dict[str, str], dict[str, dict[str, str]]], bytes]


def _record(headers: dict[str, str], roles: dict[str, dict[str, str]]) -> bytes:
    plain = json.loads(record_bytes())
    plain["headers"] = [list(h) for h in headers.items()]
    plain["riders"]["header_roles"] = roles
    plain["riders"]["header_role_origins"] = {}
    return json.dumps(plain).encode()


def _pickle(headers: dict[str, str], roles: dict[str, dict[str, str]]) -> bytes:
    session = nodriver_session(tokens=True)
    session.headers.clear()
    session.headers.update(headers)
    session._gp_header_roles = roles
    session._gp_header_role_origins = {}
    return dill.dumps(session)


BUILDERS = pytest.mark.parametrize("build", [_record, _pickle], ids=["record", "pickle"])


def _loaded(build: Build, session_store, headers, roles) -> list[GraftpunkSession]:
    plain = build(headers, roles)
    session_store.seed_plain(plain)
    return [
        load_session_for_api("myshop", key=TEST_KEY),
        load_session_for_api_from_bytes(encrypt(plain), key=TEST_KEY),
    ]


@BUILDERS
def test_a_saved_python_requests_user_agent_of_another_version_does_not_replace_the_browsers(
    build, session_store
):
    for api in _loaded(build, session_store, {"User-Agent": OTHER_VERSION_UA}, BROWSER_ROLES):
        assert api.headers["User-Agent"] == BROWSER_UA


@BUILDERS
def test_with_no_header_roles_the_session_keeps_its_own_user_agent(build, session_store):
    fresh = GraftpunkSession().headers["User-Agent"]
    assert fresh != OTHER_VERSION_UA
    for api in _loaded(build, session_store, {"User-Agent": OTHER_VERSION_UA}, {}):
        assert api.headers["User-Agent"] == fresh


@BUILDERS
def test_a_header_a_plugin_saved_on_purpose_is_restored(build, session_store):
    saved = {"User-Agent": "MyClient/1.0", "Accept": "application/json", "X-Compat": "1"}
    for api in _loaded(build, session_store, saved, BROWSER_ROLES):
        assert api.headers["User-Agent"] == "MyClient/1.0"
        assert api.headers["Accept"] == "application/json"
        assert api.headers["X-Compat"] == "1"


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("User-Agent", "python-requests/0.0.1", True),
        ("user-agent", "python-requests/2.99.0", True),
        ("User-Agent", "MyClient/1.0", False),
        ("User-Agent", "Mozilla/5.0 python-requests/2.0", False),
        ("Accept", "*/*", True),
        ("Accept", "application/json", False),
        ("Accept-Encoding", "gzip, deflate, br, zstd", True),
        ("Accept-Encoding", "gzip", True),
        ("Connection", "keep-alive", True),
        ("X-Compat", "*/*", False),
    ],
)
def test_is_library_default(name, value, expected):
    assert is_library_default(name, value) is expected
