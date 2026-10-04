"""gp plugin upgrade's migrator (graft skill spec, 2026-09-21)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from graftpunk.devtools.plugin_project import NotAPluginProjectError, read_project
from graftpunk.devtools.scaffold.policy import FIXTURES_PLACEHOLDER, FIXTURES_TREE
from graftpunk.devtools.scaffold.project import write_scaffold
from graftpunk.devtools.scaffold.render import ScaffoldSpec, render
from graftpunk.devtools.scaffold.upgrade import UpgradeRefusedError, upgrade_project

_SPEC = ScaffoldSpec(
    name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example"
)
_OLD_CONFTEST = (
    "from graftpunk.testing.plugin import site_env_scrubber\n"
    "\n"
    'scrub_site_env = site_env_scrubber("MYSHOP_")\n'
)


def _project(tmp_path: Path, conftest: str | None) -> Path:
    write_scaffold(tmp_path, _SPEC)
    path = tmp_path / "tests" / "conftest.py"
    if conftest is None:
        path.unlink()
    else:
        path.write_text(conftest)
    return path


def _ruff_check(project: Path) -> None:
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, "-m", "ruff", "check", "tests/conftest.py"],
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


class TestUpgrade:
    def test_a_project_lacking_the_wiring_gets_exactly_the_generated_conftest(
        self, tmp_path: Path
    ) -> None:
        conftest = _project(tmp_path, _OLD_CONFTEST)
        applied = upgrade_project(tmp_path)
        assert [r.name for r in applied.requirements] == ["FIXTURES_TREE", "sanitised_fixtures"]
        assert applied.created_fixtures_tree is False
        assert conftest.read_text() == render(_SPEC)["tests/conftest.py"]
        states = {s.state for s in read_project(tmp_path).requirements.values()}
        assert states == {"bound"}
        _ruff_check(tmp_path)

    def test_a_project_that_has_it_is_left_byte_identical(self, tmp_path: Path) -> None:
        write_scaffold(tmp_path, _SPEC)
        conftest = tmp_path / "tests" / "conftest.py"
        before = conftest.read_bytes()
        assert not upgrade_project(tmp_path).changed
        assert conftest.read_bytes() == before

    def test_twice_in_a_row_writes_each_statement_once(self, tmp_path: Path) -> None:
        conftest = _project(tmp_path, _OLD_CONFTEST)
        upgrade_project(tmp_path)
        assert not upgrade_project(tmp_path).changed
        text = conftest.read_text()
        assert text.count("FIXTURES_TREE = ") == 1
        assert text.count("sanitised_fixtures = ") == 1

    def test_a_wiring_in_another_spelling_is_left_alone(self, tmp_path: Path) -> None:
        wired = (
            "from pathlib import Path\n\n"
            "from graftpunk.testing.plugin import fixtures_are_sanitised as check_tree\n\n"
            'FIXTURES_TREE = (\n    Path(__file__).parent\n    / "fixtures"\n)\n'
            "sanitised_fixtures = check_tree(FIXTURES_TREE)\n"
        )
        conftest = _project(tmp_path, wired)
        assert not upgrade_project(tmp_path).changed
        assert conftest.read_text() == wired

    def test_a_missing_fixtures_tree_is_created(self, tmp_path: Path) -> None:
        write_scaffold(tmp_path, _SPEC)
        tree = tmp_path / "tests" / "fixtures"
        shutil.rmtree(tree)
        applied = upgrade_project(tmp_path)
        assert applied.created_fixtures_tree is True
        assert applied.requirements == ()
        assert applied.changed is True
        placeholder = tree / FIXTURES_PLACEHOLDER
        assert placeholder.is_file()
        assert placeholder.read_text() == ""

    def test_a_present_fixtures_tree_is_left_alone(self, tmp_path: Path) -> None:
        write_scaffold(tmp_path, _SPEC)
        placeholder = tmp_path / FIXTURES_TREE / FIXTURES_PLACEHOLDER
        before = placeholder.read_bytes()
        applied = upgrade_project(tmp_path)
        assert applied.created_fixtures_tree is False
        assert placeholder.read_bytes() == before

    def test_a_missing_wiring_and_a_missing_fixtures_tree_are_both_fixed_at_once(
        self, tmp_path: Path
    ) -> None:
        conftest = _project(tmp_path, _OLD_CONFTEST)
        shutil.rmtree(tmp_path / "tests" / "fixtures")
        applied = upgrade_project(tmp_path)
        assert [r.name for r in applied.requirements] == ["FIXTURES_TREE", "sanitised_fixtures"]
        assert applied.created_fixtures_tree is True
        assert conftest.read_text() == render(_SPEC)["tests/conftest.py"]
        assert (tmp_path / "tests" / "fixtures" / FIXTURES_PLACEHOLDER).is_file()

    def test_a_missing_conftest_is_created(self, tmp_path: Path) -> None:
        conftest = _project(tmp_path, None)
        upgrade_project(tmp_path)
        assert conftest.read_text() == (
            "from pathlib import Path\n"
            "\n"
            "from graftpunk.testing.plugin import fixtures_are_sanitised\n"
            "\n"
            'FIXTURES_TREE = Path(__file__).parent / "fixtures"\n'
            "sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)\n"
        )
        _ruff_check(tmp_path)

    def test_statements_after_a_function_keep_two_blank_lines(self, tmp_path: Path) -> None:
        conftest = _project(
            tmp_path,
            _OLD_CONFTEST + "\n\ndef helper_fixture() -> int:\n    return 1\n",
        )
        upgrade_project(tmp_path)
        assert "    return 1\n\n\nFIXTURES_TREE = " in conftest.read_text()
        _ruff_check(tmp_path)

    def test_a_conftest_holding_only_a_plain_stdlib_import_stays_ruff_clean(
        self, tmp_path: Path
    ) -> None:
        """A hand-written conftest with "import pytest" and no graftpunk from-import
        is exactly the upgrade target; the new from-imports must not jump ahead of
        the plain import isort keeps first in its section."""
        conftest = _project(
            tmp_path,
            "import pytest\n\n\n@pytest.fixture()\ndef something() -> int:\n    return 1\n",
        )
        upgrade_project(tmp_path)
        lines = conftest.read_text().splitlines()
        # "import pytest" is the only plain import in its (third-party) section;
        # isort keeps it before that section's from-import, here the one added
        # for sanitised_fixtures.
        assert lines.index("import pytest") < lines.index(
            "from graftpunk.testing.plugin import fixtures_are_sanitised"
        )
        _ruff_check(tmp_path)

    def test_crlf_line_endings_are_kept_throughout(self, tmp_path: Path) -> None:
        conftest = _project(tmp_path, None)
        conftest.write_bytes(_OLD_CONFTEST.replace("\n", "\r\n").encode())
        upgrade_project(tmp_path)
        written = conftest.read_bytes()
        assert b"\r\n" in written
        assert b"\n" not in written.replace(b"\r\n", b"")

    def test_a_directory_that_is_not_a_plugin_project_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(NotAPluginProjectError, match="empty"):
            upgrade_project(tmp_path)

    def test_a_conftest_that_does_not_parse_is_refused_and_left_alone(self, tmp_path: Path) -> None:
        broken = "def (:\n"
        conftest = _project(tmp_path, broken)
        with pytest.raises(UpgradeRefusedError, match="does not parse") as refused:
            upgrade_project(tmp_path)
        assert str(refused.value).count("tests/conftest.py") == 1, "one line per file"
        assert conftest.read_text() == broken

    def test_a_conftest_path_that_is_a_directory_is_refused(self, tmp_path: Path) -> None:
        conftest = _project(tmp_path, None)
        conftest.mkdir()
        with pytest.raises(UpgradeRefusedError, match="not a regular file"):
            upgrade_project(tmp_path)
        assert conftest.is_dir()
        assert list(conftest.iterdir()) == []

    def test_a_requirement_paths_parent_that_is_a_file_is_refused(self, tmp_path: Path) -> None:
        write_scaffold(tmp_path, _SPEC)
        shutil.rmtree(tmp_path / "tests")
        (tmp_path / "tests").write_text("not a directory")
        with pytest.raises(UpgradeRefusedError, match="tests: exists but is not a directory"):
            upgrade_project(tmp_path)

    def test_the_fixtures_tree_path_as_a_file_is_refused(self, tmp_path: Path) -> None:
        write_scaffold(tmp_path, _SPEC)
        shutil.rmtree(tmp_path / "tests" / "fixtures")
        (tmp_path / "tests" / "fixtures").write_text("not a directory")
        with pytest.raises(
            UpgradeRefusedError, match="tests/fixtures: exists but is not a directory"
        ):
            upgrade_project(tmp_path)

    def test_a_conftest_whose_imports_follow_code_is_refused_and_left_alone(
        self, tmp_path: Path
    ) -> None:
        odd = "X = 1\nfrom graftpunk.testing.plugin import site_env_scrubber\n"
        conftest = _project(tmp_path, odd)
        with pytest.raises(UpgradeRefusedError, match="ruff check --fix"):
            upgrade_project(tmp_path)
        assert conftest.read_text() == odd


_SINGLE_MODULE_PYPROJECT = """\
[project]
name = "aaa"

[project.entry-points."graftpunk.plugins"]
aaa = "aaa_plugin:AaaPlugin"

[tool.ruff.lint]
select = ["I"]
"""

_SINGLE_MODULE_PLUGIN = """\
from graftpunk.plugins import SitePlugin


class AaaPlugin(SitePlugin):
    site_name = "aaa"
    base_url = "https://aaa.example"
"""


def _single_module_project(tmp_path: Path, *, under_src: bool) -> Path:
    (tmp_path / "pyproject.toml").write_text(_SINGLE_MODULE_PYPROJECT)
    module_dir = tmp_path / "src" if under_src else tmp_path
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "aaa_plugin.py").write_text(_SINGLE_MODULE_PLUGIN)
    conftest = tmp_path / "tests" / "conftest.py"
    conftest.parent.mkdir(parents=True)
    conftest.write_text("from aaa_plugin import AaaPlugin\n\nX = AaaPlugin\n")
    return conftest


def _ruff_check_conftest(project: Path) -> None:
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, "-m", "ruff", "check", "tests/conftest.py"],
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


class TestFirstPartyPackages:
    """ruff's own default (``src = [".", "src"]``) treats a top-level directory
    or ``*.py`` stem under the project root or ``src/`` as first party;
    ``first_party_packages`` must agree, or ``gp plugin upgrade`` places an
    import ruff then reports as unsorted."""

    def test_a_single_module_plugin_at_the_root_is_first_party(self, tmp_path: Path) -> None:
        conftest = _single_module_project(tmp_path, under_src=False)
        view = read_project(tmp_path)
        assert "aaa_plugin" in view.first_party_packages
        upgrade_project(tmp_path)
        assert conftest.read_text().splitlines()[0] == "from pathlib import Path"
        _ruff_check_conftest(tmp_path)

    def test_a_single_module_plugin_under_src_is_first_party(self, tmp_path: Path) -> None:
        _single_module_project(tmp_path, under_src=True)
        view = read_project(tmp_path)
        assert "aaa_plugin" in view.first_party_packages
        upgrade_project(tmp_path)
        _ruff_check_conftest(tmp_path)

    def test_a_root_level_tests_package_is_first_party(self, tmp_path: Path) -> None:
        """A hand-written conftest importing a sibling helper module from the
        project's own tests package, which ruff classes first party because
        tests/ sits at the project root."""
        conftest = _project(tmp_path, "from tests.helpers import THING\n\nX = THING\n")
        (tmp_path / "tests" / "helpers.py").write_text("THING = 1\n")
        upgrade_project(tmp_path)
        _ruff_check(tmp_path)
        assert conftest.read_text().splitlines()[0] == "from pathlib import Path"
