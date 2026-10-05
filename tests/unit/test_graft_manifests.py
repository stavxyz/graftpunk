"""The graft skill's manifests (graft skill spec, 2026-09-21, "Testing").

Nothing here runs Claude Code. The skill's tests are split along its files: the
manifests here, the frontmatter and consent in tests/unit/test_graft_consent.py,
the references' citations and the copy check in
tests/unit/test_graft_references.py, and preflight in
tests/unit/test_graft_preflight.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.unit.guide_harness import REPO_ROOT

MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class TestManifests:
    def test_plugin_json_carries_the_one_version(self) -> None:
        """plugin.json's version is the one that pins an installed plugin (plugins
        reference, the version field). marketplace.json's root version is only the
        marketplace manifest's own (marketplace reference, top-level fields), and
        nothing here reads it, so neither the root nor the entry carries one."""
        assert _json(PLUGIN_MANIFEST)["version"]
        marketplace = _json(MARKETPLACE)
        (entry,) = marketplace["plugins"]
        assert "version" not in marketplace
        assert "version" not in entry

    def test_the_plugin_name_matches_the_marketplace_entry(self) -> None:
        (entry,) = _json(MARKETPLACE)["plugins"]
        assert entry["name"] == _json(PLUGIN_MANIFEST)["name"] == "graftpunk"

    def test_the_plugin_is_the_repository_root(self) -> None:
        (entry,) = _json(MARKETPLACE)["plugins"]
        assert entry["source"] == "./"

    def test_the_marketplace_is_named_for_the_install_line(self) -> None:
        assert _json(MARKETPLACE)["name"] == "graftpunk"

    def test_no_email_is_published(self) -> None:
        for path in (MARKETPLACE, PLUGIN_MANIFEST):
            assert "@" not in path.read_text(encoding="utf-8"), path
