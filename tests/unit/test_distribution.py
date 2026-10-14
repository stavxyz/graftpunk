"""graftpunk and graftpunk-lite ship the same package; it must know which is installed.

The metadata lookup is replaced with one that sees a chosen set of installed
distributions, since a test environment can hold only the one it was built with.
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError

import pytest

from graftpunk import _distribution


def _only(installed: dict[str, str]):
    def version(name: str) -> str:
        if name not in installed:
            raise PackageNotFoundError(name)
        return installed[name]

    return version


def test_this_environment_is_graftpunk() -> None:
    import graftpunk

    found = _distribution.installed()
    assert found is not None
    assert found[0] == "graftpunk"
    assert graftpunk.__version__ == found[1]


@pytest.mark.parametrize(
    ("installed", "expected", "command"),
    [
        ({"graftpunk": "1.20.0"}, ("graftpunk", "1.20.0"), "pip install 'graftpunk[browser]'"),
        (
            {"graftpunk-lite": "1.20.0"},
            ("graftpunk-lite", "1.20.0"),
            "pip install 'graftpunk-lite[browser]'",
        ),
        (
            {"graftpunk": "1.20.0", "graftpunk-lite": "1.19.0"},
            ("graftpunk", "1.20.0"),
            "pip install 'graftpunk[browser]'",
        ),
        ({}, None, "pip install 'graftpunk[browser]'"),
    ],
)
def test_installed_and_install_command(
    monkeypatch: pytest.MonkeyPatch,
    installed: dict[str, str],
    expected: tuple[str, str] | None,
    command: str,
) -> None:
    monkeypatch.setattr(_distribution, "_pkg_version", _only(installed))
    assert _distribution.installed() == expected
    assert _distribution.install_command("browser") == command


@pytest.mark.parametrize(
    ("installed", "expected"),
    [
        ({"graftpunk-lite": "1.20.0"}, [("graftpunk-lite", "1.20.0")]),
        (
            {"graftpunk": "1.20.0", "graftpunk-lite": "1.19.0"},
            [("graftpunk", "1.20.0"), ("graftpunk-lite", "1.19.0")],
        ),
        ({}, []),
    ],
)
def test_all_installed_reports_every_distribution(
    monkeypatch: pytest.MonkeyPatch,
    installed: dict[str, str],
    expected: list[tuple[str, str]],
) -> None:
    monkeypatch.setattr(_distribution, "_pkg_version", _only(installed))
    assert _distribution.all_installed() == expected


def test_missing_browser_error_names_lite_extra_on_lite(monkeypatch: pytest.MonkeyPatch) -> None:
    import graftpunk

    monkeypatch.setattr(_distribution, "_pkg_version", _only({"graftpunk-lite": "1.20.0"}))
    monkeypatch.setitem(sys.modules, "graftpunk.session", None)
    with pytest.raises(ImportError, match=r"pip install 'graftpunk-lite\[browser\]'"):
        getattr(graftpunk, "BrowserSession")  # noqa: B009 - must go through __getattr__
