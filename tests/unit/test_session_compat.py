"""Sessions saved by released graftpunk versions load with the working tree.

The fixtures are frozen pickles written by each release
(``scripts/session_compat.py generate``); these tests pin what today's loaders
return for each, so a change to the stored format cannot silently lose a
cookie, header, role, token or account that an older release saved.
"""

import pytest
import requests

from graftpunk.cache import (
    load_session,
    load_session_for_api,
    load_session_for_api_from_bytes,
    load_session_for_api_resolved,
)
from graftpunk.client_headers import client_sets_header
from tests.unit.session_compat_cases import (
    EXPECTED_KIND,
    FIXTURES,
    case_id,
    cookie_dicts,
    decrypt,
    iter_cases,
    pickled_global_names,
    token_dicts,
)

ALL = iter_cases()


@pytest.mark.parametrize("case", ALL, ids=case_id)
def test_a_released_versions_session_loads_for_api_from_bytes(case):
    api = load_session_for_api_from_bytes(case.encrypted, key=case.key)
    assert cookie_dicts(api.cookies) == case.expected["cookies"]
    assert api.headers["X-Compat"] == "1"  # a saved header no loader filter drops
    # A header the HTTP client sets itself (Accept-Encoding, Connection) is the
    # client's own on load, never the saved one (#370); every other saved
    # header the loader keeps has its saved value.
    kept = [(k, v) for k, v in case.expected["headers"] if not client_sets_header(k)]
    assert all(api.headers[k] == v for k, v in kept if k in api.headers)
    client_defaults = requests.utils.default_headers()
    assert all(
        api.headers[k] == client_defaults[k]
        for k, _ in case.expected["headers"]
        if client_sets_header(k) and k in api.headers and k in client_defaults
    )
    assert getattr(api, "_gp_header_roles", {}) == case.expected["header_roles"]
    assert token_dicts(api) == case.expected["cached_tokens"]


@pytest.mark.parametrize("case", ALL, ids=case_id)
def test_a_released_versions_session_loads_from_a_local_store(case, session_store):
    session_store.put_case(case)
    for api in (
        load_session_for_api("myshop", key=case.key),
        load_session_for_api_resolved("myshop", key=case.key)[0],
    ):
        assert cookie_dicts(api.cookies) == case.expected["cookies"]
        assert token_dicts(api) == case.expected["cached_tokens"]
        assert getattr(api, "_gp_account_identifier", None) == case.expected["account"]


@pytest.mark.parametrize("case", ALL, ids=case_id)
def test_load_session_returns_what_was_saved(case, session_store):
    session_store.put_case(case)
    obj = load_session("myshop", key=case.key)
    assert type(obj).__qualname__ == case.expected["class"].rsplit(".", 1)[-1]
    assert cookie_dicts(obj.cookies) == case.expected["cookies"]


@pytest.mark.parametrize("case", ALL, ids=case_id)
def test_each_fixture_names_its_class_as_saved(case):
    module, _, name = case.expected["class"].rpartition(".")
    names = pickled_global_names(decrypt(case))
    if EXPECTED_KIND[case.case] is None:
        # A __main__ class is embedded by value: no module to import it from.
        assert f"{module} {name}" not in names
        assert "graftpunk.graftpunk_session GraftpunkSession" in names
    else:
        assert f"{module} {name}" in names


_LOCAL_PATHS = (b"/Users/", b"/home/", b"/private/", b"/tmp/", b"/var/folders/")


def test_no_fixture_carries_a_local_path():
    """The fixtures are public: an absolute path from the machine that wrote
    them (a storage location, a by-value function's file name) names a person."""
    leaks = [
        f"{case_id(case)}: {needle!r}"
        for case in ALL
        for needle in _LOCAL_PATHS
        if needle in decrypt(case)
    ]
    leaks += [
        f"{path.relative_to(FIXTURES)}: {needle!r}"
        for path in FIXTURES.rglob("*.json")
        for needle in _LOCAL_PATHS
        if needle in path.read_bytes()
    ]
    assert leaks == []
