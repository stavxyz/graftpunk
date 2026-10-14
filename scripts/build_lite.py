#!/usr/bin/env python3
"""Build the graftpunk-lite sdist and wheel from the root pyproject.toml.

graftpunk-lite is the same import package, version, scripts and entry points as
graftpunk, without the browser stack in its base dependencies, so it resolves
under Pyodide and Cloudflare Workers (#121). Its [browser] extra holds the
packages removed, so graftpunk-lite[browser] installs what graftpunk does.

The root pyproject.toml stays the only source: this script derives the lite
pyproject from it, stages it next to copies of src/graftpunk, README.md, LICENSE
and CHANGELOG.md in a temporary directory, and builds there. Staging is needed
because `uv build` builds the wheel from the sdist, and an sdist cannot reach
files outside its own project directory.

Usage: python scripts/build_lite.py [--out-dir dist]
"""

from __future__ import annotations

import argparse
import copy
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_NAME = "graftpunk"
LITE_NAME = "graftpunk-lite"
LITE_DESCRIPTION_SUFFIX = (
    " Lite install: no browser stack, so it resolves under Pyodide and Cloudflare"
    " Workers; graftpunk-lite[browser] equals graftpunk."
)
STAGED_FILES = ("README.md", "LICENSE", "CHANGELOG.md")
STAGED_TREES = ("src/graftpunk",)

# A requirement on the root distribution itself, with extras: graftpunk[...].
_SELF_REFERENCE = re.compile(rf"^{re.escape(ROOT_NAME)}(?=\[)")


def browser_only_names(root: dict[str, Any]) -> list[str]:
    """The distribution names [tool.graftpunk.lite] browser-only lists."""
    return list(root["tool"]["graftpunk"]["lite"]["browser-only"])


def is_browser_only(requirement: str, names: list[str]) -> bool:
    wanted = {canonicalize_name(n) for n in names}
    return canonicalize_name(Requirement(requirement).name) in wanted


def lite_pyproject(root: dict[str, Any]) -> dict[str, Any]:
    """Derive graftpunk-lite's pyproject from graftpunk's (both parsed TOML).

    Every key is copied unchanged except: the name; the description, which says
    this is the lite install; dependencies, which lose each browser-only
    requirement; the browser extra, which holds exactly those requirements; and
    extras requiring graftpunk[...], which require graftpunk-lite[...] instead so
    they resolve against this distribution.
    """
    lite = copy.deepcopy(root)
    project = lite["project"]
    if project["name"] != ROOT_NAME:
        raise ValueError(f"expected project name {ROOT_NAME!r}, got {project['name']!r}")
    names = browser_only_names(root)

    project["name"] = LITE_NAME
    project["description"] = project["description"] + LITE_DESCRIPTION_SUFFIX
    project["dependencies"] = [
        r for r in root["project"]["dependencies"] if not is_browser_only(r, names)
    ]
    removed = [r for r in root["project"]["dependencies"] if is_browser_only(r, names)]

    extras = project.setdefault("optional-dependencies", {})
    for extra, requirements in extras.items():
        extras[extra] = [_SELF_REFERENCE.sub(LITE_NAME, r) for r in requirements]
    extras["browser"] = removed
    return lite


# A small TOML writer for the value shapes pyproject.toml uses: tables, arrays of
# strings, arrays of inline tables of strings, strings and booleans. Anything
# else raises, so an unexpected shape fails the build instead of being dropped.


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\t", "\\t")
    return f'"{escaped}"'


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else _toml_string(key)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        if not value:
            return "[]"
        return "[\n" + "".join(f"    {_toml_value(v)},\n" for v in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items())
        return "{ " + inner + " }"
    raise TypeError(f"cannot write {type(value).__name__} to TOML: {value!r}")


def dumps_toml(data: dict[str, Any]) -> str:
    """Serialize *data*; every table (including empty ones) gets a header."""
    lines: list[str] = []

    def emit(table: dict[str, Any], path: list[str]) -> None:
        scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
        tables = {k: v for k, v in table.items() if isinstance(v, dict)}
        if path:
            if lines:
                lines.append("")
            lines.append("[" + ".".join(_toml_key(p) for p in path) + "]")
        for key, value in scalars.items():
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        for key, value in tables.items():
            emit(value, [*path, key])

    emit(data, [])
    return "\n".join(lines) + "\n"


def stage(root_dir: Path, staging: Path, lite: dict[str, Any]) -> None:
    for name in STAGED_FILES:
        shutil.copy2(root_dir / name, staging / name)
    for tree in STAGED_TREES:
        shutil.copytree(
            root_dir / tree,
            staging / tree,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    (staging / "pyproject.toml").write_text(dumps_toml(lite), encoding="utf-8")


def build(out_dir: Path, root_dir: Path = REPO_ROOT) -> None:
    with (root_dir / "pyproject.toml").open("rb") as f:
        root = tomllib.load(f)
    lite = lite_pyproject(root)
    out_dir = out_dir.resolve()
    with tempfile.TemporaryDirectory(prefix="graftpunk-lite-") as tmp:
        staging = Path(tmp)
        stage(root_dir, staging, lite)
        subprocess.run(  # noqa: S603 - fixed argv; uv found on PATH like the Justfile's
            ["uv", "build", "--out-dir", str(out_dir), str(staging)],  # noqa: S607
            check=True,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "dist")
    args = parser.parse_args(argv)
    build(args.out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
