"""gp plugin check, a lint over the project reader's view (graft skill spec, 2026-09-21)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from graftpunk.cli.main import app
from graftpunk.devtools.plugin_check import check_project
from graftpunk.devtools.scaffold import policy
from graftpunk.devtools.scaffold.policy import ProjectRequirement
from graftpunk.devtools.scaffold.project import write_scaffold
from graftpunk.devtools.scaffold.render import ScaffoldSpec, render
from graftpunk.devtools.scaffold.upgrade import upgrade_project

runner = CliRunner()
_SPEC = ScaffoldSpec(
    name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example"
)
_CLEAN_MODULE = """\
from graftpunk.plugins import CommandContext, SitePlugin, command


class MyshopPlugin(SitePlugin):
    site_name = "myshop"
    base_url = "https://myshop.example"

    @command(help="List orders", endpoint="GET /api/orders")
    def orders(self, ctx: CommandContext) -> dict:
        return ctx.request_json("GET", "/api/orders", role="xhr")
"""


_TEST_MODULE_MARKER = (
    "\n\n\n# GP-FILL: add a test per command, against a fixture in tests/fixtures/\n"
)


def _clean_project(root: Path) -> Path:
    """A generated project with no GP-FILL marker left anywhere: the plugin
    module gets a filled-in command, and the un-filled test module's own
    marker (render() writes one with no digest) is removed too, so a test
    asserting no findings is not tripped up by check_project now reading
    tests/ as well as the plugin module."""
    write_scaffold(root, _SPEC)
    module = root / "src" / "graftpunk_myshop" / "plugin.py"
    module.write_text(_CLEAN_MODULE)
    test_module = root / "tests" / "test_plugin.py"
    text = test_module.read_text()
    assert text.endswith(_TEST_MODULE_MARKER), "render()'s test-module marker text changed"
    test_module.write_text(text.removesuffix(_TEST_MODULE_MARKER) + "\n")
    return module


@pytest.mark.usefixtures("gp_logging")
class TestFindings:
    def test_a_clean_project_has_none(self, tmp_path: Path) -> None:
        _clean_project(tmp_path)
        assert check_project(tmp_path) == []
        result = runner.invoke(app, ["plugin", "check", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "no findings" in result.output

    def test_a_remaining_marker_is_reported_with_its_line(self, tmp_path: Path) -> None:
        write_scaffold(tmp_path, _SPEC)
        findings = check_project(tmp_path)
        assert findings
        assert {f.path for f in findings} == {
            "src/graftpunk_myshop/plugin.py",
            "tests/test_plugin.py",
        }
        for finding in findings:
            text = (tmp_path / finding.path).read_text().splitlines()
            assert finding.line is not None and "GP-FILL" in text[finding.line - 1]
        result = runner.invoke(app, ["plugin", "check", "--dir", str(tmp_path)])
        assert result.exit_code == 1
        assert "src/graftpunk_myshop/plugin.py:" in result.output
        assert "tests/test_plugin.py:" in result.output

    def test_a_module_without_exactly_one_plugin_class_is_reported(self, tmp_path: Path) -> None:
        module = _clean_project(tmp_path)
        module.write_text(_CLEAN_MODULE + "\n\nclass Other(SitePlugin):\n    site_name = 'o'\n")
        (finding,) = check_project(tmp_path)
        assert finding.path == "src/graftpunk_myshop/plugin.py"
        assert "exactly one SitePlugin subclass, found 2" in finding.message

    def test_a_missing_requirement_is_reported_and_names_the_fix(self, tmp_path: Path) -> None:
        _clean_project(tmp_path)
        (tmp_path / "tests" / "conftest.py").write_text("")
        messages = [f.message for f in check_project(tmp_path)]
        assert len(messages) == 2
        assert all("gp plugin upgrade" in m for m in messages)

    def test_a_missing_fixtures_tree_is_reported_then_created_by_upgrade(
        self, tmp_path: Path
    ) -> None:
        """B7: check and upgrade take the fact from one place (the reader's
        view), so the two cannot disagree."""
        _clean_project(tmp_path)
        shutil.rmtree(tmp_path / "tests" / "fixtures")
        (finding,) = check_project(tmp_path)
        assert finding.path == "tests/fixtures/"
        assert finding.message == "missing; gp plugin upgrade creates it."
        upgrade_project(tmp_path)
        assert (tmp_path / "tests" / "fixtures").is_dir()
        assert check_project(tmp_path) == []

    def test_a_directory_that_is_not_a_plugin_project_is_a_finding(self, tmp_path: Path) -> None:
        (finding,) = check_project(tmp_path)
        assert "not a graftpunk plugin project" in finding.message

    def test_a_dir_that_does_not_exist_is_a_finding_not_read_as_empty(self, tmp_path: Path) -> None:
        """B9: a mistyped --dir must not read as a fresh, empty project."""
        missing = tmp_path / "nonexistent"
        (finding,) = check_project(missing)
        assert finding.message == f"{missing}: no such directory."
        result = runner.invoke(app, ["plugin", "check", "--dir", str(missing)])
        assert result.exit_code == 1
        assert f"{missing}: no such directory." in result.output

    def test_a_conftest_that_does_not_parse_is_one_finding(self, tmp_path: Path) -> None:
        """One finding per unreadable file, however many requirements it holds."""
        _clean_project(tmp_path)
        (tmp_path / "tests" / "conftest.py").write_text("def (:\n")
        (finding,) = check_project(tmp_path)
        assert finding.path == "tests/conftest.py"
        assert "does not parse" in finding.message

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000-mode file")
    def test_a_conftest_that_cannot_be_read_is_one_finding(self, tmp_path: Path) -> None:
        """B6d: a PermissionError (any OSError), not just a parse error, is a
        finding rather than a traceback (polish-r1 P7)."""
        _clean_project(tmp_path)
        conftest = tmp_path / "tests" / "conftest.py"
        conftest.chmod(0)
        try:
            (finding,) = check_project(tmp_path)
        finally:
            conftest.chmod(0o644)
        assert finding.path == "tests/conftest.py"
        assert "cannot be read" in finding.message


def test_the_three_consumers_follow_the_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One entry added to PROJECT_REQUIREMENTS: the renderer emits it, check reports
    it on a project that lacks it, and upgrade applies it."""
    _clean_project(tmp_path)
    probe = ProjectRequirement(
        path="tests/conftest.py", name="extra_probe", statement="extra_probe = 1"
    )
    monkeypatch.setattr(policy, "PROJECT_REQUIREMENTS", (*policy.PROJECT_REQUIREMENTS, probe))
    assert "extra_probe = 1" in render(_SPEC)["tests/conftest.py"]
    (finding,) = check_project(tmp_path)
    assert "extra_probe" in finding.message
    assert [r.name for r in upgrade_project(tmp_path).requirements] == ["extra_probe"]
    assert check_project(tmp_path) == []


def test_the_lint_never_imports_the_writer() -> None:
    script = (
        "import sys\n"
        "import graftpunk.devtools.plugin_check\n"
        "print('graftpunk.devtools.scaffold.write' in sys.modules)\n"
    )
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout.strip() == "False"
