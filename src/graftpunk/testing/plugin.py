"""The pytest half of :mod:`graftpunk.testing`: fixture factories a generated
``tests/conftest.py`` calls.

A generated conftest imports the factories here and assigns each one's result
to a module-level name, which is what registers that fixture with pytest.
:func:`site_env_scrubber` keeps the developer's own site variables out of the
tests; :func:`fixtures_are_sanitised` checks every committed fixture against
its sidecar, and :func:`check_fixtures_tree` is that check without pytest, for
a caller that wants the report itself. The generated file carries no framework
logic of its own to drift from graftpunk's (the scaffold rule, plugin tooling
spec, 2026-09-11).

It deliberately does not name this module in ``pytest_plugins``: that would ask
pytest to rewrite assertions in a module the import has already loaded, which
it warns about, and this module has no hooks of its own to register that way.
``fixtures_are_sanitised`` is handed the fixtures tree by the conftest, which
declares it as ``FIXTURES_TREE``; nothing here imports ``graftpunk.devtools``
(graft skill spec, 2026-09-21).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from graftpunk.contracts import current_schema
from graftpunk.testing.sidecar import (
    FIXTURES_PLACEHOLDER,
    Sidecar,
    SidecarError,
    is_sidecar,
    load_sidecar,
    sidecar_path,
    sidecar_scannable_text,
)

__all__ = [
    "FixturesTreeReport",
    "check_fixtures_tree",
    "fixtures_are_sanitised",
    "site_env_scrubber",
]


def site_env_scrubber(prefix: str) -> Any:
    """An autouse fixture that removes every env var starting with *prefix*
    for the duration of each test, and restores them afterward.

    A generated plugin's ``tests/conftest.py`` sets
    ``scrub_site_env = site_env_scrubber("<NAME>_")`` so a developer's own
    ``MYSHOP_USERNAME``/``MYSHOP_PASSWORD`` in the ambient environment never
    leaks into a test asserting on the plugin's behaviour without them.
    """

    @pytest.fixture(autouse=True)
    def _scrub() -> Iterator[None]:
        saved = {k: v for k, v in os.environ.items() if k.startswith(prefix)}
        for key in saved:
            del os.environ[key]
        try:
            yield
        finally:
            os.environ.update(saved)

    return _scrub


@dataclass(frozen=True)
class FixturesTreeReport:
    """What :func:`check_fixtures_tree` found: every problem, and the two counts."""

    problems: tuple[str, ...]
    verified: int
    declared: int

    @property
    def summary(self) -> str:
        """The line the check prints on every run, so the declared count shows in review."""
        return (
            f"fixtures_are_sanitised: {self.verified} fixture(s) verified against their "
            f"capture, {self.declared} accepted on declaration (no capture hash: "
            f"trusted, not checked)"
        )


def _missing_sidecar(relative: Path, sidecar_name: str) -> str:
    schema = current_schema("sidecar")
    return (
        f"{relative}: no sidecar ({sidecar_name}). For a fixture derived from a capture, "
        f"copy the sidecar gp observe fixtures wrote beside the capture. For a hand-made "
        f'fixture, write {sidecar_name} with "schema": {schema}, a status, a content type, '
        f'"body_params": [], "capture_sha256": null, "flagged_names": [], and '
        f'"redacted_names": 0, which declares that the file came off no account.'
    )


def check_fixtures_tree(tree: Path) -> FixturesTreeReport:
    """Check every fixture anywhere under *tree* against its sidecar.

    Catches a missing tree, a fixture with no sidecar, a sidecar outside its
    declared format (through the owner, :mod:`graftpunk.testing.sidecar`), a
    fixture that is byte for byte its capture, and a flagged cookie or token name
    in a fixture or in its sidecar's other fields. It does not judge whether
    invented content is invented well. A sidecar with no capture hash is the
    author's declaration that the file came off no account; it is trusted, not
    checked, and counted. A dotfile (any path component starting with ``.``,
    which covers ``FIXTURES_PLACEHOLDER`` itself, a macOS ``.DS_Store``, and an
    editor swap file) is never a fixture and is skipped.
    """
    if not tree.is_dir():
        return FixturesTreeReport(
            problems=(
                f"{tree}: the fixtures tree does not exist. The generated conftest names "
                f"it as FIXTURES_TREE; gp plugin new creates it with a {FIXTURES_PLACEHOLDER}, and "
                f"gp plugin upgrade creates it for a project that lacks it. Run "
                f"gp plugin upgrade, or recreate the directory by hand.",
            ),
            verified=0,
            declared=0,
        )
    problems: list[str] = []
    verified = declared = 0
    fixtures = sorted(
        p
        for p in tree.rglob("*")
        if p.is_file()
        and not is_sidecar(p)
        and not any(part.startswith(".") for part in p.relative_to(tree).parts)
    )
    for fixture in fixtures:
        relative = fixture.relative_to(tree)
        meta = sidecar_path(fixture)
        if not meta.is_file():
            problems.append(_missing_sidecar(relative, meta.name))
            continue
        try:
            sidecar = load_sidecar(meta)
        except SidecarError as exc:
            problems.append(f"{relative}: {exc}")
            continue
        body = fixture.read_bytes()
        if sidecar.declared:
            declared += 1
        else:
            verified += 1
            if hashlib.sha256(body).hexdigest() == sidecar.capture_sha256:
                problems.append(
                    f"{relative}: an unchanged copy of its capture. A fixture keeps the "
                    f"capture's structure and invents every value."
                )
        problems.extend(_flagged_name_problems(relative, meta.name, sidecar, body))
    return FixturesTreeReport(problems=tuple(problems), verified=verified, declared=declared)


def _flagged_name_problems(
    relative: Path, meta_name: str, sidecar: Sidecar, body: bytes
) -> list[str]:
    """One problem per flagged name found in the fixture's body or in a sidecar field
    other than ``flagged_names``. Matched case-insensitively: an HTTP header name is
    case-insensitive by the protocol, and a capture recorded over HTTP/2 lowercases
    every header name, so a flagged ``X-Csrf-Token`` copied into a fixture as
    ``x-csrf-token`` still counts."""
    problems: list[str] = []
    text = body.decode("utf-8", errors="replace").casefold()
    rest = sidecar_scannable_text(sidecar).casefold()
    for name in sidecar.flagged_names:
        if not name:
            # An empty string is a substring of every fixture; a sidecar
            # holding one would flag every file it checks.
            continue
        folded = name.casefold()
        if folded in text:
            problems.append(
                f"{relative}: contains the flagged name {name!r}, a cookie or token "
                f"name the capture's digest recorded; rename it in the fixture."
            )
        if folded in rest:
            problems.append(
                f"{relative.parent / meta_name}: carries the flagged name {name!r} "
                f"outside flagged_names."
            )
    return problems


def fixtures_are_sanitised(tree: Path | str) -> Any:
    """An autouse, session-scoped fixture that fails the run unless every fixture under
    *tree* passes :func:`check_fixtures_tree`.

    A generated ``tests/conftest.py`` sets
    ``sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)``; the assignment is
    what registers it. It prints :attr:`FixturesTreeReport.summary` once per run,
    and on a problem it fails every test with the list of problems.
    """
    root = Path(tree)

    @pytest.fixture(scope="session", autouse=True)
    def _sanitised(request: pytest.FixtureRequest) -> None:
        report = check_fixtures_tree(root)
        reporter = request.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(report.summary)
        if report.problems:
            listing = "\n".join(f"  {problem}" for problem in report.problems)
            pytest.fail(f"fixtures_are_sanitised:\n{listing}", pytrace=False)

    return _sanitised
