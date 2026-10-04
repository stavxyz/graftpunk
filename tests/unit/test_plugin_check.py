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
from graftpunk.devtools.scaffold.upgrade import UpgradeRefusedError, upgrade_project

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

    def test_a_module_without_exactly_one_plugin_class_is_reported_once(
        self, tmp_path: Path
    ) -> None:
        """A plugin defect's message already starts with its own module path
        ("{relative}: ..."), so the finding must carry no path of its own, or
        the path prints twice."""
        module = _clean_project(tmp_path)
        module.write_text(_CLEAN_MODULE + "\n\nclass Other(SitePlugin):\n    site_name = 'o'\n")
        (finding,) = check_project(tmp_path)
        assert finding.path is None
        assert finding.line is None
        assert str(finding) == (
            "src/graftpunk_myshop/plugin.py: expected exactly one SitePlugin subclass, "
            "found 2 (MyshopPlugin, Other). A plugin module holds one plugin class."
        )
        result = runner.invoke(app, ["plugin", "check", "--dir", str(tmp_path)])
        assert result.exit_code == 1
        assert result.output.count("src/graftpunk_myshop/plugin.py:") == 1

    def test_a_missing_requirement_is_reported_and_names_the_fix(self, tmp_path: Path) -> None:
        _clean_project(tmp_path)
        (tmp_path / "tests" / "conftest.py").write_text("")
        messages = [f.message for f in check_project(tmp_path)]
        assert len(messages) == 2
        assert all("gp plugin upgrade" in m for m in messages)

    def test_a_missing_fixtures_tree_is_reported_then_created_by_upgrade(
        self, tmp_path: Path
    ) -> None:
        """check and upgrade take the fact from one place (the reader's view),
        so the two cannot disagree."""
        _clean_project(tmp_path)
        shutil.rmtree(tmp_path / "tests" / "fixtures")
        (finding,) = check_project(tmp_path)
        assert finding.path == "tests/fixtures/"
        assert finding.message == "missing; gp plugin upgrade creates it."
        upgrade_project(tmp_path)
        assert (tmp_path / "tests" / "fixtures").is_dir()
        assert check_project(tmp_path) == []

    def test_a_fixtures_tree_that_is_a_file_is_reported_as_blocked_and_upgrade_agrees(
        self, tmp_path: Path
    ) -> None:
        """The reader tells "missing" from "blocked": a regular file at
        tests/fixtures is not something gp plugin upgrade can create, so check
        must not tell the reader it will."""
        _clean_project(tmp_path)
        shutil.rmtree(tmp_path / "tests" / "fixtures")
        (tmp_path / "tests" / "fixtures").write_text("not a directory")
        (finding,) = check_project(tmp_path)
        assert finding.path == "tests/fixtures"
        assert finding.message == (
            "exists but is not a directory; move it aside, then run gp plugin upgrade."
        )
        with pytest.raises(UpgradeRefusedError) as excinfo:
            upgrade_project(tmp_path)
        assert str(excinfo.value) == str(finding)

    def test_a_conftest_that_is_a_directory_is_reported_as_unreadable_and_upgrade_agrees(
        self, tmp_path: Path
    ) -> None:
        """The same rule at a second site: a directory at tests/conftest.py is
        not a file gp plugin upgrade can add its wiring to by parsing, so
        check must not say "adds it" the way it does for a merely missing
        binding."""
        _clean_project(tmp_path)
        (tmp_path / "tests" / "conftest.py").unlink()
        (tmp_path / "tests" / "conftest.py").mkdir()
        (finding,) = check_project(tmp_path)
        assert finding.path == "tests/conftest.py"
        assert finding.message == (
            "exists but is not a regular file; move it aside, then run gp plugin upgrade."
        )
        with pytest.raises(UpgradeRefusedError) as excinfo:
            upgrade_project(tmp_path)
        assert str(excinfo.value) == str(finding)

    def test_a_tests_dir_that_is_a_file_blocks_both_the_tree_and_the_conftest(
        self, tmp_path: Path
    ) -> None:
        """tests itself, not just the leaf, can be the wrong kind: the fixtures
        tree and the requirement file both live under it, so both findings
        name the same blocking ancestor, deduplicated to one line that
        upgrade's refusal (its own first blocker) agrees with."""
        _clean_project(tmp_path)
        shutil.rmtree(tmp_path / "tests")
        (tmp_path / "tests").write_text("not a directory")
        (finding,) = check_project(tmp_path)
        assert finding.path == "tests"
        assert finding.message == (
            "exists but is not a directory; move it aside, then run gp plugin upgrade."
        )
        with pytest.raises(UpgradeRefusedError) as excinfo:
            upgrade_project(tmp_path)
        assert str(excinfo.value) == str(finding)

    def test_a_dangling_symlink_at_fixtures_is_blocked_not_missing(self, tmp_path: Path) -> None:
        _clean_project(tmp_path)
        shutil.rmtree(tmp_path / "tests" / "fixtures")
        (tmp_path / "tests" / "fixtures").symlink_to(tmp_path / "tests" / "nonexistent")
        (finding,) = check_project(tmp_path)
        assert finding.path == "tests/fixtures"
        assert finding.message == (
            "exists but is not a directory; move it aside, then run gp plugin upgrade."
        )
        with pytest.raises(UpgradeRefusedError) as excinfo:
            upgrade_project(tmp_path)
        assert str(excinfo.value) == str(finding)

    def test_a_dangling_symlink_at_conftest_is_blocked_not_missing(self, tmp_path: Path) -> None:
        _clean_project(tmp_path)
        (tmp_path / "tests" / "conftest.py").unlink()
        (tmp_path / "tests" / "conftest.py").symlink_to(tmp_path / "tests" / "nonexistent.py")
        (finding,) = check_project(tmp_path)
        assert finding.path == "tests/conftest.py"
        assert finding.message == (
            "exists but is not a regular file; move it aside, then run gp plugin upgrade."
        )
        with pytest.raises(UpgradeRefusedError) as excinfo:
            upgrade_project(tmp_path)
        assert str(excinfo.value) == str(finding)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root traverses a 000-mode directory")
    def test_a_conftest_symlink_into_an_unreadable_directory_blames_the_symlink(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """tests/conftest.py is a symlink whose target sits under a directory
        with no execute bit; tests/ itself is fine. Following the symlink is
        what fails, so the symlink is the one at fault: the finding and the
        refusal must both name tests/conftest.py with "move it aside", never
        tests/ with "fix its permissions" (tests/ has nothing wrong with it)."""
        _clean_project(tmp_path)
        locked = tmp_path_factory.mktemp("locked")
        (locked / "real").mkdir()
        (locked / "real" / "conftest.py").write_text("")
        locked.chmod(0o000)
        conftest = tmp_path / "tests" / "conftest.py"
        conftest.unlink()
        conftest.symlink_to(locked / "real" / "conftest.py")
        try:
            (finding,) = check_project(tmp_path)
            with pytest.raises(UpgradeRefusedError) as excinfo:
                upgrade_project(tmp_path)
        finally:
            locked.chmod(0o755)
        assert finding.path == "tests/conftest.py"
        assert finding.message == (
            "exists but is not a regular file; move it aside, then run gp plugin upgrade."
        )
        assert str(excinfo.value) == str(finding)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root traverses a 000-mode directory")
    def test_a_fixtures_symlink_into_an_unreadable_directory_blames_the_symlink(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Same shape as the conftest case above, for tests/fixtures: a
        symlink to a directory that sits under a 000-mode directory. The
        symlink is the one at fault, not tests/."""
        _clean_project(tmp_path)
        locked = tmp_path_factory.mktemp("locked")
        (locked / "real").mkdir()
        locked.chmod(0o000)
        fixtures = tmp_path / "tests" / "fixtures"
        shutil.rmtree(fixtures)
        fixtures.symlink_to(locked / "real")
        try:
            (finding,) = check_project(tmp_path)
            with pytest.raises(UpgradeRefusedError) as excinfo:
                upgrade_project(tmp_path)
        finally:
            locked.chmod(0o755)
        assert finding.path == "tests/fixtures"
        assert finding.message == (
            "exists but is not a directory; move it aside, then run gp plugin upgrade."
        )
        assert str(excinfo.value) == str(finding)

    def test_a_conftest_symlink_loop_blames_the_symlink_not_tests(self, tmp_path: Path) -> None:
        """tests/conftest.py -> conftest.py is a self-loop (ELOOP on follow):
        the finding and the refusal must name the symlink itself with "move
        it aside", never tests/ with permissions advice."""
        _clean_project(tmp_path)
        conftest = tmp_path / "tests" / "conftest.py"
        conftest.unlink()
        conftest.symlink_to("conftest.py")
        (finding,) = check_project(tmp_path)
        assert finding.path == "tests/conftest.py"
        assert finding.message == (
            "exists but is not a regular file; move it aside, then run gp plugin upgrade."
        )
        with pytest.raises(UpgradeRefusedError) as excinfo:
            upgrade_project(tmp_path)
        assert str(excinfo.value) == str(finding)

    def test_a_fixtures_symlink_loop_blames_the_symlink_not_tests(self, tmp_path: Path) -> None:
        """tests/fixtures -> fixtures is the same self-loop shape, for a
        directory."""
        _clean_project(tmp_path)
        fixtures = tmp_path / "tests" / "fixtures"
        shutil.rmtree(fixtures)
        fixtures.symlink_to("fixtures")
        (finding,) = check_project(tmp_path)
        assert finding.path == "tests/fixtures"
        assert finding.message == (
            "exists but is not a directory; move it aside, then run gp plugin upgrade."
        )
        with pytest.raises(UpgradeRefusedError) as excinfo:
            upgrade_project(tmp_path)
        assert str(excinfo.value) == str(finding)

    def test_a_directory_that_is_not_a_plugin_project_is_a_finding(self, tmp_path: Path) -> None:
        (finding,) = check_project(tmp_path)
        assert "not a graftpunk plugin project" in finding.message
        # A reader refusal names no path of its own; printing it should not
        # prepend a "." that reads like one.
        assert finding.path is None
        assert str(finding) == f"{tmp_path} is not a graftpunk plugin project (empty)."

    def test_a_dir_that_does_not_exist_is_a_finding_not_read_as_empty(self, tmp_path: Path) -> None:
        """A mistyped --dir must not read as a fresh, empty project."""
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
        """A PermissionError (any OSError), not just a parse error, is a finding
        rather than a traceback."""
        _clean_project(tmp_path)
        conftest = tmp_path / "tests" / "conftest.py"
        conftest.chmod(0)
        try:
            (finding,) = check_project(tmp_path)
        finally:
            conftest.chmod(0o644)
        assert finding.path == "tests/conftest.py"
        assert "cannot be read" in finding.message

    @pytest.mark.skipif(os.geteuid() == 0, reason="root traverses a 000-mode directory")
    def test_an_unreadable_src_refuses_without_a_traceback(self, tmp_path: Path) -> None:
        """gp plugin info --json and gp plugin check both name a clean refusal
        when src/ cannot be traversed, never a PermissionError traceback, and
        the refusal says "cannot be read" (the module exists; a "neither ...
        exists" refusal would be false)."""
        _clean_project(tmp_path)
        src = tmp_path / "src"
        src.chmod(0o000)
        try:
            info_result = runner.invoke(
                app, ["plugin", "info", "--json", "--dir", str(tmp_path)], catch_exceptions=False
            )
            check_result = runner.invoke(
                app, ["plugin", "check", "--dir", str(tmp_path)], catch_exceptions=False
            )
        finally:
            src.chmod(0o755)
        expected = "src/graftpunk_myshop/plugin.py: cannot be read (Permission denied)."
        assert info_result.exit_code == 1, info_result.output
        assert expected in info_result.output
        assert check_result.exit_code == 1, check_result.output
        assert expected in check_result.output

    @pytest.mark.skipif(os.geteuid() == 0, reason="root traverses a 000/600-mode directory")
    @pytest.mark.parametrize("mode", [0o000, 0o600])
    def test_an_untraversable_tests_dir_refuses_in_one_line_everywhere(
        self, tmp_path: Path, mode: int
    ) -> None:
        """tests/ without its execute bit cannot be entered to reach
        tests/fixtures, whether or not it is also unreadable (0o000) or
        readable-but-not-traversable (0o600). gp plugin check and gp plugin
        upgrade both read fixtures_tree_blocked and must name it in one line,
        never a PermissionError traceback, and must agree; gp plugin info
        --json does not read that fact at all, so it reads the rest of the
        project cleanly, with no traceback either."""
        _clean_project(tmp_path)
        tests_dir = tmp_path / "tests"
        tests_dir.chmod(mode)
        try:
            check_result = runner.invoke(
                app, ["plugin", "check", "--dir", str(tmp_path)], catch_exceptions=False
            )
            info_result = runner.invoke(
                app, ["plugin", "info", "--json", "--dir", str(tmp_path)], catch_exceptions=False
            )
            upgrade_result = runner.invoke(
                app, ["plugin", "upgrade", "--dir", str(tmp_path)], catch_exceptions=False
            )
        finally:
            tests_dir.chmod(0o755)
        expected = "tests: cannot be read (Permission denied)"
        assert check_result.exit_code == 1, check_result.output
        assert expected in check_result.output
        assert info_result.exit_code == 0, info_result.output
        assert upgrade_result.exit_code == 1, upgrade_result.output
        assert expected in upgrade_result.output

    @pytest.mark.skipif(os.geteuid() == 0, reason="root traverses a 600-mode directory")
    def test_a_project_dir_with_no_execute_bit_refuses_naming_pyproject(
        self, tmp_path: Path
    ) -> None:
        """A project directory at 0o600 stats fine on its own (no execute bit
        needed to stat a path, only to list into it), so the "is this even a
        directory" guard does not catch it; the first read that must enter
        it (pyproject.toml) has to refuse in one line instead of raising."""
        project = tmp_path / "proj"
        _clean_project(project)
        project.chmod(0o600)
        try:
            result = runner.invoke(
                app,
                ["plugin", "info", "--json", "--dir", str(project)],
                catch_exceptions=False,
            )
        finally:
            project.chmod(0o755)
        assert result.exit_code == 1, result.output
        assert f"{project / 'pyproject.toml'}: cannot be read (Permission denied)." in (
            result.output
        )


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
