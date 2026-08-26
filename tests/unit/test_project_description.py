"""The project describes itself the same way everywhere, in the intended register.

Pins the outcome of docs/superpowers/specs/2026-08-25-readme-overhaul-design.md:
the marketing tagline is gone from every surface and the subtitle is verbatim.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

import graftpunk
from graftpunk.cli.main import app


def _repo_root() -> Path:
    """Return the directory holding pyproject.toml.

    Walking up from this file keeps the test independent of its own depth under
    tests/; it reads repo files on purpose (README.md and pyproject.toml are two of
    the surfaces being pinned).
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("pyproject.toml not found above this test file")


REPO_ROOT = _repo_root()

SUBTITLE = (
    "Authenticated browser sessions, captured once and replayed over plain HTTP: "
    "stealth login, encrypted at rest, pluggable storage."
)


def _flat(text: str) -> str:
    """Collapse whitespace so a docstring that wraps the subtitle still matches."""
    return " ".join(text.split())


# Sentences that must not survive anywhere the project describes itself.
OLD_SENTENCES = (
    re.compile(r"turn any website into an api", re.IGNORECASE),
    re.compile(r"Graft scriptable access"),
    re.compile(r"Log in once, script forever"),
)


def _surfaces() -> dict[str, str]:
    """Every place the project describes itself, keyed by a human-readable name.

    app.info.help is the raw help= string handed to typer.Typer; reading it (rather
    than rendering through CliRunner) keeps the check independent of terminal width.
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return {
        "graftpunk.__doc__": graftpunk.__doc__ or "",
        "gp --help banner (app.info.help)": app.info.help or "",
        "pyproject description": pyproject["project"]["description"],
        "README.md": (REPO_ROOT / "README.md").read_text(),
    }


SURFACE_NAMES = list(_surfaces())


@pytest.mark.parametrize("name", SURFACE_NAMES)
def test_subtitle_is_verbatim_on_every_surface(name: str) -> None:
    assert SUBTITLE in _flat(_surfaces()[name]), f"subtitle missing from {name}"


def test_package_constants_are_the_single_owner() -> None:
    assert graftpunk.DESCRIPTION == SUBTITLE
    assert "Log in through a real browser once;" in graftpunk.LONG_DESCRIPTION
    assert "\n" not in graftpunk.DESCRIPTION and "\n" not in graftpunk.LONG_DESCRIPTION


@pytest.mark.parametrize("name", SURFACE_NAMES)
def test_old_tagline_is_gone_from_every_surface(name: str) -> None:
    text = _flat(_surfaces()[name])
    for pattern in OLD_SENTENCES:
        assert not pattern.search(text), f"{pattern.pattern!r} still in {name}"


def test_pyproject_description_is_the_subtitle_only() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["description"] == SUBTITLE


def test_readme_cli_banner_matches_live_help() -> None:
    """README's CLI Reference block quotes the banner and the description paragraph as
    gp --help prints them at 80 columns (spec: location 3 is pasted from live output,
    never hand-edited). Compare whitespace-flattened so terminal wrapping is irrelevant."""
    readme = _flat((REPO_ROOT / "README.md").read_text())
    banner_lines = [line.strip() for line in (app.info.help or "").splitlines() if line.strip()]
    assert banner_lines, "gp --help banner (app.info.help) is empty"
    assert _flat(banner_lines[0]) in readme, "README CLI Reference banner is stale"
    assert _flat(graftpunk.LONG_DESCRIPTION) in readme, (
        "README CLI Reference block lacks the description paragraph gp --help prints"
    )
