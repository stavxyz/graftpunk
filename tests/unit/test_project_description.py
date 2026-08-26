"""The project describes itself the same way everywhere, in the intended register.

Pins the outcome of docs/superpowers/specs/2026-08-25-readme-overhaul-design.md:
the marketing tagline is gone from every surface and the subtitle is verbatim.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import graftpunk
from graftpunk.cli.main import app


def _repo_root() -> Path:
    """Walk up from this file to the directory holding pyproject.toml, so the test
    does not depend on its own depth under tests/ (it reads repo files on purpose:
    README.md and pyproject.toml are two of the surfaces being pinned)."""
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
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return {
        "graftpunk.__doc__": graftpunk.__doc__ or "",
        "gp --help banner (app.info.help)": app.info.help or "",
        "pyproject description": pyproject["project"]["description"],
        "README.md": (REPO_ROOT / "README.md").read_text(),
    }


def test_subtitle_is_verbatim_on_every_surface() -> None:
    for name, text in _surfaces().items():
        assert SUBTITLE in _flat(text), f"subtitle missing from {name}"


def test_package_constants_are_the_single_owner() -> None:
    assert graftpunk.DESCRIPTION == SUBTITLE
    assert "Log in through a real browser once;" in graftpunk.LONG_DESCRIPTION
    assert "\n" not in graftpunk.DESCRIPTION and "\n" not in graftpunk.LONG_DESCRIPTION


def test_old_tagline_is_gone_from_every_surface() -> None:
    for name, text in _surfaces().items():
        for pattern in OLD_SENTENCES:
            assert not pattern.search(text), f"{pattern.pattern!r} still in {name}"


def test_pyproject_description_is_the_subtitle_only() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["description"] == SUBTITLE


def test_readme_cli_banner_matches_live_help() -> None:
    """README's CLI Reference block quotes the first banner line; it must be the first
    non-blank line of the Typer app's help= string (spec: location 3 is regenerated from
    the source, never hand-edited — rendered output wraps at the terminal width)."""
    readme = (REPO_ROOT / "README.md").read_text()
    first_banner_line = next(
        line.strip() for line in (app.info.help or "").splitlines() if line.strip()
    )
    assert first_banner_line in readme
