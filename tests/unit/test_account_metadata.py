"""account_identifier flows session-attr → metadata → serializers → pickle (#151)."""

from __future__ import annotations

import pickle
import sys

import pytest
import requests

from graftpunk.cache import _extract_session_metadata
from graftpunk.session import BrowserSession
from graftpunk.session_identity import GP_ACCOUNT_ATTR
from graftpunk.storage.base import SessionMetadata, dict_to_metadata, metadata_to_dict


def _metadata(**kw):  # noqa: ANN003, ANN202
    from datetime import UTC, datetime

    base = {
        "name": "myshop@alice",
        "checksum": "c",
        "created_at": datetime.now(UTC),
        "modified_at": datetime.now(UTC),
        "expires_at": None,
        "domain": None,
        "current_url": None,
        "cookie_count": 0,
        "cookie_domains": [],
        "storage_backend": "local",
        "storage_location": "~/x",
    }
    base.update(kw)
    return SessionMetadata(**base)


class TestSerializers:
    def test_round_trip_with_identifier(self) -> None:
        m = _metadata(account_identifier="alice@example.com")
        again = dict_to_metadata(metadata_to_dict(m))
        assert again.account_identifier == "alice@example.com"

    def test_pre_upgrade_dict_without_field_loads_as_none(self) -> None:
        d = metadata_to_dict(_metadata())
        d.pop("account_identifier")
        assert dict_to_metadata(d).account_identifier is None


class TestExtraction:
    def test_extracts_attribute_from_session(self) -> None:
        s = requests.Session()
        setattr(s, GP_ACCOUNT_ATTR, "alice@example.com")
        raw = _extract_session_metadata(s, "myshop@alice")
        assert raw["account_identifier"] == "alice@example.com"

    def test_absent_attribute_is_none(self) -> None:
        raw = _extract_session_metadata(requests.Session(), "myshop")
        assert raw["account_identifier"] is None


@pytest.mark.parametrize("backend_type", ["nodriver", "selenium"])
def test_identifier_survives_pickle_round_trip(backend_type: str) -> None:
    session = BrowserSession.__new__(BrowserSession)
    requests.Session.__init__(session)
    session._backend_type = backend_type
    session._session_name = "myshop@alice"
    if backend_type == "selenium":
        session._driver = None
        session._driver_initializer = None
        session.webdriver_path = None
        session.default_timeout = 30
        session.webdriver_options = {}
        session._last_requests_url = None
    setattr(session, GP_ACCOUNT_ATTR, "alice@example.com")
    restored = pickle.loads(pickle.dumps(session))  # noqa: S301 — our own object
    assert getattr(restored, GP_ACCOUNT_ATTR, None) == "alice@example.com"


LABELLED = "myshop@alice"


def _local_locations(tmp_path) -> list[str]:  # noqa: ANN001
    from graftpunk.storage.local import LocalSessionStorage

    backend = LocalSessionStorage(base_dir=tmp_path)
    session_dir = backend.base_dir / LABELLED
    return [str(session_dir / "session.pickle"), str(session_dir / "metadata.json")]


def _s3_locations(_tmp_path) -> list[str]:  # noqa: ANN001
    from graftpunk.storage.s3 import S3SessionStorage

    backend = S3SessionStorage.__new__(S3SessionStorage)
    return [backend._session_key(LABELLED), backend._metadata_key(LABELLED)]


def _supabase_locations(_tmp_path) -> list[str]:  # noqa: ANN001
    from graftpunk.storage.supabase import SupabaseSessionStorage

    backend = SupabaseSessionStorage.__new__(SupabaseSessionStorage)
    return [backend._session_path(LABELLED), backend._metadata_path(LABELLED)]


_BACKEND_LOCATIONS = {
    "local": _local_locations,
    "s3": _s3_locations,
    "supabase": _supabase_locations,
}


class TestBackendConformance:
    """Every backend round-trips a labelled name and its identifier (spec: Naming).

    Parametrized over every backend, so a fourth one inherits a failing test
    rather than a reading assignment. Local exercises a real save/get on a tmp
    dir; S3 and Supabase are exercised at their key/path-construction seams
    plus the shared serializers (no network).

    UNVERIFIED: that S3 and Supabase *accept* an "@" in an object key/path is
    not proven here — only that graftpunk builds the key with the name intact.
    The end-to-end acceptance is proven for the local backend alone; the remote
    ones would need a live bucket (or a moto/respx double) to confirm.
    """

    @pytest.mark.parametrize("backend", sorted(_BACKEND_LOCATIONS))
    def test_labelled_name_survives_key_construction(self, backend, tmp_path) -> None:  # noqa: ANN001
        for location in _BACKEND_LOCATIONS[backend](tmp_path):
            assert LABELLED in location

    @pytest.mark.parametrize("backend", sorted(_BACKEND_LOCATIONS))
    @pytest.mark.parametrize("label", [None, "alice"])
    def test_shared_serializers_round_trip(self, backend, label) -> None:  # noqa: ANN001
        # Every backend persists metadata through these two functions, so one
        # round-trip per backend is the shared half of the conformance.
        name = "myshop" if label is None else f"myshop@{label}"
        meta = _metadata(name=name, account_identifier="alice@example.com", storage_backend=backend)
        assert dict_to_metadata(metadata_to_dict(meta)) == meta

    def test_local_backend_real_round_trip(self, tmp_path) -> None:  # noqa: ANN001
        import pickle

        from graftpunk.storage.local import LocalSessionStorage

        backend = LocalSessionStorage(base_dir=tmp_path)
        meta = _metadata(account_identifier="alice@example.com")
        backend.save_session(LABELLED, pickle.dumps({"ok": True}), meta)
        again = backend.get_session_metadata(LABELLED)
        assert again is not None
        assert again.name == LABELLED
        assert again.account_identifier == "alice@example.com"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_metadata_file_is_written_owner_only(tmp_path) -> None:  # noqa: ANN001
    """metadata.json carries the account identifier; give it the pickle's mode."""
    import pickle

    from graftpunk.storage.local import LocalSessionStorage

    backend = LocalSessionStorage(base_dir=tmp_path)
    backend.save_session(
        LABELLED, pickle.dumps({"ok": True}), _metadata(account_identifier="a@example.com")
    )
    metadata_path = tmp_path / LABELLED / "metadata.json"
    assert metadata_path.stat().st_mode & 0o777 == 0o600


def test_cache_dict_conversions_carry_the_identifier(fresh_backend) -> None:  # noqa: ANN001
    """get_session_metadata and list_sessions_with_metadata expose the field (C1-C3)."""
    import requests

    from graftpunk.cache import cache_session, get_session_metadata, list_sessions_with_metadata

    s = requests.Session()
    setattr(s, GP_ACCOUNT_ATTR, "alice@example.com")
    cache_session(s, "myshop@alice")
    assert get_session_metadata("myshop@alice")["account_identifier"] == "alice@example.com"
    rows = list_sessions_with_metadata()
    assert any(r.get("account_identifier") == "alice@example.com" for r in rows)


def test_identity_survives_load_and_refresh_recache(fresh_backend) -> None:  # noqa: ANN001
    """The spec's load → update_session_cookies re-cache round-trip (NN-C)."""
    import requests

    from graftpunk.cache import cache_session, get_session_metadata, update_session_cookies

    s = requests.Session()
    setattr(s, GP_ACCOUNT_ATTR, "alice@example.com")
    cache_session(s, "myshop@alice")
    api = requests.Session()  # a fresh API-side session for the refresh
    update_session_cookies(api, "myshop@alice")
    assert get_session_metadata("myshop@alice")["account_identifier"] == "alice@example.com"


def test_direct_recache_of_an_api_session_keeps_the_identifier(fresh_backend) -> None:  # noqa: ANN001
    """load_session_for_api -> cache_session must not wipe account_identifier.

    The API session is built by copying riders off the stored session; when
    the identifier was not among them, re-caching that session (the direct
    re-cache path, no update_session_cookies) blanked the metadata field.
    """
    import requests

    from graftpunk.cache import cache_session, get_session_metadata, load_session_for_api

    stored = BrowserSession.__new__(BrowserSession)
    requests.Session.__init__(stored)
    stored._backend_type = "nodriver"
    stored._session_name = "myshop@alice"
    setattr(stored, GP_ACCOUNT_ATTR, "alice@example.com")
    cache_session(stored, "myshop@alice")

    api = load_session_for_api("myshop@alice")
    assert getattr(api, GP_ACCOUNT_ATTR, None) == "alice@example.com"

    cache_session(api, "myshop@alice")
    assert get_session_metadata("myshop@alice")["account_identifier"] == "alice@example.com"


def test_update_session_cookies_recovers_identifier_without_extra_read(
    fresh_backend, monkeypatch
) -> None:
    """update_session_cookies must recover account_identifier from the metadata
    its own load already returned, not via a second backend.get_session_metadata
    call — that would double a round-trip (remote on S3/Supabase)."""
    import requests

    from graftpunk import cache as cache_mod
    from graftpunk.cache import cache_session, get_session_metadata, update_session_cookies

    s = requests.Session()
    setattr(s, GP_ACCOUNT_ATTR, "alice@example.com")
    cache_session(s, "myshop@alice")

    backend = cache_mod._get_session_storage_backend()
    calls = {"n": 0}
    real_get_session_metadata = backend.get_session_metadata

    def _counting_get_session_metadata(name):  # noqa: ANN001, ANN202
        calls["n"] += 1
        return real_get_session_metadata(name)

    monkeypatch.setattr(backend, "get_session_metadata", _counting_get_session_metadata)

    api = requests.Session()  # a fresh API-side session for the refresh (no identifier set)
    update_session_cookies(api, "myshop@alice")

    assert calls["n"] == 0
    assert get_session_metadata("myshop@alice")["account_identifier"] == "alice@example.com"
