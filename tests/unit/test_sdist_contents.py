"""The source distribution ships the package and nothing else.

Earlier releases leaked private example data through ``tests/`` and ``docs/``
inside the sdist. ``pyproject.toml`` restricts the sdist to an allowlist; this
test builds it and pins the exact top-level contents so a new top-level
directory or a hatchling behaviour change cannot silently widen it.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# hatchling always adds the .gitignore it used to compute exclusions.
EXPECTED_TOP_LEVEL = {
    "src",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "pyproject.toml",
    "PKG-INFO",
    ".gitignore",
}


UV = shutil.which("uv")


@pytest.mark.skipif(UV is None, reason="uv is needed to build the sdist")
def test_sdist_contains_only_the_allowlist(tmp_path: Path) -> None:
    assert UV is not None
    subprocess.run(  # noqa: S603
        [UV, "build", "--sdist", "--out-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    (sdist,) = tmp_path.glob("graftpunk-*.tar.gz")
    with tarfile.open(sdist) as tar:
        members = [m.name for m in tar.getmembers()]

    # Every member is "<dist-root>/<path>"; compare the first path component.
    top_level = {name.split("/", 1)[1].split("/", 1)[0] for name in members if "/" in name}
    assert top_level == EXPECTED_TOP_LEVEL, sorted(top_level ^ EXPECTED_TOP_LEVEL)

    # And nothing under src/ except the package itself.
    src_children = {
        name.split("/", 3)[2]
        for name in members
        if name.split("/", 2)[1:2] == ["src"] and name.count("/") >= 2
    }
    assert src_children == {"graftpunk"}
