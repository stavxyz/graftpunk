"""What a generated plugin project must look like, as data.

Knows nothing about the filesystem and imports nothing that touches it, so a
reader, a lint, and a writer can all import it without importing each other
(graft skill spec, 2026-09-21, "Project policy is declarative and lives apart
from the writer").
"""

from __future__ import annotations

from typing import Final

__all__ = ["FIXTURES_TREE", "TESTS_DIR", "fixtures_root"]

TESTS_DIR: Final = "tests/"
"""The directory a generated project's tests live in, project-relative: the one
spelling every other tests-relative path here is built from."""

FIXTURES_TREE: Final = f"{TESTS_DIR}fixtures/"
"""The one directory every plugin's fixtures live under, project-relative.

The same in a standalone project and in a suite, and true for every member a
suite ever gains, which is why a generated conftest can carry it although it
is written once."""


def fixtures_root(*, suite_member: bool, module_name: str) -> str:
    """Where one plugin's fixtures live, project-relative, with a trailing slash.

    A standalone project uses the tree itself. A suite member owns a directory
    under it named for its module, because fixture names come from endpoint paths
    and two plugins in one suite can share a path.
    The generator supplies the two facts from its spec; a reader supplies them
    from the project on disk.
    """
    if suite_member:
        return f"{FIXTURES_TREE}{module_name}/"
    return FIXTURES_TREE
