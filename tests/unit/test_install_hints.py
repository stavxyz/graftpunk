"""Every missing-dependency message names the extra on the installed distribution.

graftpunk and graftpunk-lite ship the same package, so a hint naming the wrong
one would install the second distribution over the first. Each case below makes
one site fail for real (its import hidden, or the dependency flag off) and reads
the message the user sees. The metadata lookup is replaced with one that sees a
single chosen distribution, since a test environment holds only the one it was
built with.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from graftpunk import _distribution
from tests.unit.test_cache import SimpleSession, _create_session_on_disk, _setup_local_env


def _as(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    def version(wanted: str) -> str:
        if wanted != name:
            raise PackageNotFoundError(wanted)
        return "1.20.0"

    monkeypatch.setattr(_distribution, "_pkg_version", version)


def _raised(call: Callable[[], object]) -> str:
    with pytest.raises(Exception) as caught:  # noqa: PT011 - each site raises its own type
        call()
    return str(caught.value)


def _backends(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    from graftpunk.backends import get_backend

    monkeypatch.setitem(sys.modules, "graftpunk.backends.nodriver", None)
    return _raised(lambda: get_backend("nodriver"))


def _selenium(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    from graftpunk.backends.selenium import SeleniumBackend

    monkeypatch.setitem(sys.modules, "graftpunk.stealth", None)
    return _raised(SeleniumBackend(use_stealth=True)._start_stealth_driver)


def _nodriver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    from graftpunk.backends.nodriver import NoDriverBackend

    monkeypatch.setitem(sys.modules, "nodriver", None)
    return _raised(NoDriverBackend().start)


def _cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    from graftpunk.cache import load_session

    _setup_local_env(tmp_path, monkeypatch)
    _create_session_on_disk(tmp_path, "myshop", SimpleSession())
    # What unpickling a BrowserSession raises when the browser stack is absent.
    missing = ModuleNotFoundError("No module named 'httpie'")
    with patch("graftpunk.cache.pickle.loads", side_effect=missing):
        return _raised(lambda: load_session("myshop"))


def _encryption(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    from graftpunk.encryption import _get_key_from_supabase_vault

    monkeypatch.setitem(sys.modules, "supabase", None)
    return _raised(_get_key_from_supabase_vault)


def _s3(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    from graftpunk.storage.s3 import S3SessionStorage

    monkeypatch.setitem(sys.modules, "boto3", None)
    return _raised(lambda: S3SessionStorage(bucket="sessions"))


def _supabase(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    from graftpunk.storage.supabase import SupabaseSessionStorage

    monkeypatch.setitem(sys.modules, "supabase", None)
    return _raised(lambda: SupabaseSessionStorage(url="https://myshop.example", service_key="k"))


def _capture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    from graftpunk.observe import capture

    monkeypatch.setattr(capture, "_HAS_SELENIUM", False)
    return _raised(lambda: capture.create_capture_backend("selenium", driver=object()))


def _yaml_jmespath(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    from graftpunk import console as gp_console
    from graftpunk.plugins import yaml_plugin
    from tests.unit.test_yaml_plugin import (
        _make_command,
        _make_config,
        _mock_ctx,
        _mock_response,
    )

    printed: list[str] = []
    # The warning goes to a stderr Console made at import; this one writes here.
    monkeypatch.setattr(gp_console, "err_console", _RecordingConsole(printed))
    monkeypatch.setattr(yaml_plugin, "HAS_JMESPATH", False)
    plugin = yaml_plugin.create_yaml_site_plugin(
        _make_config(), [_make_command(url="/data", jmespath="items[0]")]
    )
    handler = {c.name: c for c in plugin.get_commands()}["test"].handler
    session = MagicMock(spec=requests.Session)
    session.request.return_value = _mock_response(json_data={"items": ["first"]})
    handler(_mock_ctx(session))
    return "\n".join(printed)


class _RecordingConsole:
    def __init__(self, printed: list[str]) -> None:
        self._printed = printed

    def print(self, message: str, **_: object) -> None:
        self._printed.append(message)


_SITES = {
    "backends": (_backends, "browser"),
    "selenium": (_selenium, "browser"),
    "nodriver": (_nodriver, "browser"),
    "cache": (_cache, "browser"),
    "encryption": (_encryption, "supabase"),
    "s3": (_s3, "s3"),
    "supabase": (_supabase, "supabase"),
    "capture": (_capture, "browser"),
    "yaml_plugin": (_yaml_jmespath, "jmespath"),
}


@pytest.mark.parametrize("distribution", ["graftpunk", "graftpunk-lite"])
@pytest.mark.parametrize("site", sorted(_SITES))
def test_the_hint_names_the_installed_distribution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, site: str, distribution: str
) -> None:
    trigger, extra = _SITES[site]
    _as(monkeypatch, distribution)
    message = trigger(monkeypatch, tmp_path)
    assert f"pip install '{distribution}[{extra}]'" in message, message
    if distribution == "graftpunk":
        assert "graftpunk-lite" not in message, message
