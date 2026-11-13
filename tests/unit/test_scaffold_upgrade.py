"""gp plugin upgrade's migrator (graft skill spec, 2026-09-21)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from graftpunk.devtools.plugin_check import check_project
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

    def test_check_and_upgrade_name_both_blocked_paths_in_one_run(self, tmp_path: Path) -> None:
        """tests/fixtures and tests/conftest.py both dangling symlinks: check
        lists both findings in one run, and upgrade must refuse listing every
        blocked path check lists, in the same order and the same words (the
        old two-owners bug refused on conftest.py alone, then on fixtures
        alone on a second call, once the first was fixed by hand), joined by
        a single space: each finding is already a full sentence ending in
        "." and contains its own semicolon, so "; " between two of them
        would read as a third sentence."""
        write_scaffold(tmp_path, _SPEC)
        shutil.rmtree(tmp_path / "tests" / "fixtures")
        (tmp_path / "tests" / "fixtures").symlink_to(tmp_path / "tests" / "nonexistent")
        (tmp_path / "tests" / "conftest.py").unlink()
        (tmp_path / "tests" / "conftest.py").symlink_to(tmp_path / "tests" / "nonexistent.py")
        blocked = [
            f for f in check_project(tmp_path) if f.path in {"tests/fixtures", "tests/conftest.py"}
        ]
        assert [f.path for f in blocked] == ["tests/fixtures", "tests/conftest.py"]
        assert str(blocked[0]) == (
            "tests/fixtures: exists but is not a directory; move it aside, "
            "then run gp plugin upgrade."
        )
        assert str(blocked[1]) == (
            "tests/conftest.py: exists but is not a regular file; move it "
            "aside, then run gp plugin upgrade."
        )
        with pytest.raises(UpgradeRefusedError) as refused:
            upgrade_project(tmp_path)
        assert str(refused.value) == " ".join(str(f) for f in blocked)

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


_LIB_SRC_RUFF = '\n[tool.ruff]\nsrc = ["lib"]\n'


def _lib_project(tmp_path: Path, ruff: str = _LIB_SRC_RUFF) -> Path:
    """A single-module plugin at the root whose conftest imports a package that
    lives under lib/, a directory only a ruff ``src`` setting makes first party."""
    conftest = _single_module_project(tmp_path, under_src=False)
    (tmp_path / "pyproject.toml").write_text(_SINGLE_MODULE_PYPROJECT + ruff)
    (tmp_path / "lib" / "myshop_shared").mkdir(parents=True)
    (tmp_path / "lib" / "myshop_shared" / "__init__.py").write_text("THING = 1\n")
    conftest.write_text("from myshop_shared import THING\n\nX = THING\n")
    return conftest


class TestRuffSrc:
    """A project's own ruff ``src`` replaces the default roots (#219), the way
    ruff applies it, so an import lands in the section ``ruff check`` expects."""

    def test_a_package_under_a_configured_src_root_is_placed_first_party(
        self, tmp_path: Path
    ) -> None:
        conftest = _lib_project(tmp_path)
        view = read_project(tmp_path)
        assert "myshop_shared" in view.first_party_packages
        upgrade_project(tmp_path)
        lines = conftest.read_text().splitlines()
        assert lines[:5] == [
            "from pathlib import Path",
            "",
            "from graftpunk.testing.plugin import fixtures_are_sanitised",
            "",
            "from myshop_shared import THING",
        ]
        _ruff_check_conftest(tmp_path)

    def test_a_configured_src_drops_the_default_roots(self, tmp_path: Path) -> None:
        """With src = ["lib"], ruff no longer counts the project root: neither
        the root-level plugin module nor tests/ nor lib itself is first party."""
        _lib_project(tmp_path)
        view = read_project(tmp_path)
        assert view.first_party_packages == frozenset({"myshop_shared"})

    def test_a_ruff_toml_wins_over_pyproject(self, tmp_path: Path) -> None:
        conftest = _lib_project(tmp_path, ruff="")
        (tmp_path / "ruff.toml").write_text('src = ["lib"]\n\n[lint]\nselect = ["I"]\n')
        assert read_project(tmp_path).first_party_packages == frozenset({"myshop_shared"})
        upgrade_project(tmp_path)
        assert "\n\nfrom myshop_shared import THING" in conftest.read_text()
        _ruff_check_conftest(tmp_path)

    def test_a_dot_ruff_toml_wins_over_ruff_toml(self, tmp_path: Path) -> None:
        _lib_project(tmp_path, ruff="")
        (tmp_path / "ruff.toml").write_text('src = ["elsewhere"]\n')
        (tmp_path / ".ruff.toml").write_text('src = ["lib"]\n')
        assert read_project(tmp_path).first_party_packages == frozenset({"myshop_shared"})

    def test_an_unparseable_ruff_toml_falls_back_to_the_defaults(self, tmp_path: Path) -> None:
        _lib_project(tmp_path)
        (tmp_path / "ruff.toml").write_text("src = [\n")
        names = read_project(tmp_path).first_party_packages
        assert {"aaa_plugin", "lib", "tests"} <= names
        assert "myshop_shared" not in names

    def test_a_glob_src_entry_expands_to_each_matching_directory(self, tmp_path: Path) -> None:
        _lib_project(tmp_path, ruff='\n[tool.ruff]\nsrc = ["packages/*"]\n')
        for package in ("alpha", "beta"):
            (tmp_path / "packages" / package / f"{package}_core").mkdir(parents=True)
        (tmp_path / "packages" / "beta" / "beta_util.pyi").write_text("X: int\n")
        assert read_project(tmp_path).first_party_packages == frozenset(
            {"alpha_core", "beta_core", "beta_util"}
        )

    def test_an_empty_src_makes_nothing_first_party_from_disk(self, tmp_path: Path) -> None:
        _lib_project(
            tmp_path,
            ruff=(
                "\n[tool.ruff]\nsrc = []\n"
                '\n[tool.ruff.lint.isort]\nknown-first-party = ["declared"]\n'
            ),
        )
        assert read_project(tmp_path).first_party_packages == frozenset({"declared"})

    def test_a_pyi_stub_at_a_default_root_is_first_party(self, tmp_path: Path) -> None:
        _single_module_project(tmp_path, under_src=False)
        (tmp_path / "typed_helpers.pyi").write_text("X: int\n")
        assert "typed_helpers" in read_project(tmp_path).first_party_packages

    def test_a_present_lint_isort_table_hides_the_legacy_isort_table(self, tmp_path: Path) -> None:
        """ruff 0.17.5 takes a file's [lint.isort] table whole when it exists,
        so a legacy [isort] known-first-party beside it is ignored, and the
        name it declares is third party to ruff."""
        conftest = _single_module_project(tmp_path, under_src=False)
        (tmp_path / "pyproject.toml").write_text(
            _SINGLE_MODULE_PYPROJECT
            + "\n[tool.ruff.lint.isort]\ncombine-as-imports = true\n"
            + '\n[tool.ruff.isort]\nknown-first-party = ["declared"]\n'
        )
        conftest.write_text("from declared import THING\n\nX = THING\n")
        assert "declared" not in read_project(tmp_path).first_party_packages
        upgrade_project(tmp_path)
        assert conftest.read_text().splitlines()[:4] == [
            "from pathlib import Path",
            "",
            "from declared import THING",
            "from graftpunk.testing.plugin import fixtures_are_sanitised",
        ]
        _ruff_check_conftest(tmp_path)

    def test_a_legacy_isort_table_counts_when_lint_isort_is_absent(self, tmp_path: Path) -> None:
        _single_module_project(tmp_path, under_src=False)
        (tmp_path / "pyproject.toml").write_text(
            _SINGLE_MODULE_PYPROJECT + '\n[tool.ruff.isort]\nknown-first-party = ["declared"]\n'
        )
        assert "declared" in read_project(tmp_path).first_party_packages


class TestRuffExtend:
    """``extend`` is followed as ruff follows it: each path from the directory
    of the file naming it, each file's ``src`` from its own directory, and the
    extending file's own ``src`` or ``known-first-party`` winning."""

    def test_a_src_set_only_in_an_extended_file_counts_from_that_files_directory(
        self, tmp_path: Path
    ) -> None:
        conftest = _lib_project(tmp_path, ruff='\n[tool.ruff]\nextend = "config/base.toml"\n')
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "base.toml").write_text('src = ["../lib"]\n')
        assert read_project(tmp_path).first_party_packages == frozenset({"myshop_shared"})
        upgrade_project(tmp_path)
        assert "\n\nfrom myshop_shared import THING" in conftest.read_text()
        _ruff_check_conftest(tmp_path)

    def test_an_extended_pyproject_is_read_through_its_tool_ruff(self, tmp_path: Path) -> None:
        _lib_project(tmp_path, ruff='\n[tool.ruff]\nextend = "shared/pyproject.toml"\n')
        (tmp_path / "shared").mkdir()
        (tmp_path / "shared" / "pyproject.toml").write_text('[tool.ruff]\nsrc = ["../lib"]\n')
        assert read_project(tmp_path).first_party_packages == frozenset({"myshop_shared"})

    def test_a_chain_of_extends_is_followed(self, tmp_path: Path) -> None:
        _lib_project(tmp_path, ruff='\n[tool.ruff]\nextend = "a/one.toml"\n')
        (tmp_path / "a" / "b").mkdir(parents=True)
        (tmp_path / "a" / "one.toml").write_text('extend = "b/two.toml"\n')
        (tmp_path / "a" / "b" / "two.toml").write_text('src = ["../../lib"]\n')
        assert read_project(tmp_path).first_party_packages == frozenset({"myshop_shared"})

    def test_the_extending_files_own_src_wins(self, tmp_path: Path) -> None:
        conftest = _lib_project(
            tmp_path, ruff='\n[tool.ruff]\nsrc = ["lib"]\nextend = "base.toml"\n'
        )
        (tmp_path / "base.toml").write_text('src = ["elsewhere"]\n')
        (tmp_path / "elsewhere" / "other_pkg").mkdir(parents=True)
        assert read_project(tmp_path).first_party_packages == frozenset({"myshop_shared"})
        upgrade_project(tmp_path)
        _ruff_check_conftest(tmp_path)
        assert "\n\nfrom myshop_shared import THING" in conftest.read_text()

    def test_known_first_party_is_inherited_unless_the_extending_file_sets_it(
        self, tmp_path: Path
    ) -> None:
        _lib_project(tmp_path, ruff='\n[tool.ruff]\nsrc = []\nextend = "base.toml"\n')
        base = tmp_path / "base.toml"
        base.write_text('[lint.isort]\nknown-first-party = ["inherited"]\n')
        assert read_project(tmp_path).first_party_packages == frozenset({"inherited"})
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            pyproject.read_text() + '\n[tool.ruff.lint.isort]\nknown-first-party = ["own"]\n'
        )
        assert read_project(tmp_path).first_party_packages == frozenset({"own"})

    def test_an_extending_lint_isort_without_the_key_inherits_it(self, tmp_path: Path) -> None:
        """Across extend, the isort keys combine one by one, so an extending
        [lint.isort] that does not set known-first-party keeps the base's."""
        _lib_project(
            tmp_path,
            ruff=(
                '\n[tool.ruff]\nsrc = []\nextend = "base.toml"\n'
                "\n[tool.ruff.lint.isort]\ncombine-as-imports = true\n"
            ),
        )
        (tmp_path / "base.toml").write_text('[lint.isort]\nknown-first-party = ["inherited"]\n')
        assert read_project(tmp_path).first_party_packages == frozenset({"inherited"})

    def test_an_environment_variable_in_extend_is_expanded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MYSHOP_RUFF_BASE", str(tmp_path / "config"))
        _lib_project(tmp_path, ruff='\n[tool.ruff]\nextend = "$MYSHOP_RUFF_BASE/base.toml"\n')
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "base.toml").write_text('src = ["../lib"]\n')
        assert read_project(tmp_path).first_party_packages == frozenset({"myshop_shared"})

    @pytest.mark.parametrize(
        "base",
        [None, 'extend = "../pyproject.toml"\nsrc = ["../lib"]\n', "src = [\n"],
        ids=["missing", "cycle", "unparseable"],
    )
    def test_a_chain_ruff_refuses_falls_back_to_the_defaults(
        self, tmp_path: Path, base: str | None
    ) -> None:
        _lib_project(tmp_path, ruff='\n[tool.ruff]\nsrc = ["lib"]\nextend = "config/base.toml"\n')
        if base is not None:
            (tmp_path / "config").mkdir()
            (tmp_path / "config" / "base.toml").write_text(base)
        names = read_project(tmp_path).first_party_packages
        assert {"aaa_plugin", "lib", "tests"} <= names
        assert "myshop_shared" not in names


class TestShellExpand:
    """``src`` and ``extend`` are expanded as ruff's ``shellexpand::full`` does."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("lib", "lib"),
            ("${GPX_SET}/x", "set-value/x"),
            ("$GPX_SET/x", "set-value/x"),
            ("${GPX_UNSET:-lib}", "lib"),
            ("${GPX_SET:-lib}", "set-value"),
            ("${GPX_EMPTY:-lib}", ""),
            ("${GPX_UNSET:-$GPX_SET}", "$GPX_SET"),
            ("a$$b", "a$b"),
            ("cost$", "cost$"),
            ("a$-b", "a$-b"),
            ("${unclosed", "${unclosed"),
            ("~/lib", "/home/myshop/lib"),
            ("~", "/home/myshop"),
            ("~other/lib", "~other/lib"),
            ("${GPX_TILDE}/lib", "~/lib"),
        ],
    )
    def test_expands_like_shellexpand(
        self, monkeypatch: pytest.MonkeyPatch, value: str, expected: str
    ) -> None:
        from graftpunk.devtools.plugin_project import _shell_expand

        monkeypatch.setenv("HOME", "/home/myshop")
        monkeypatch.setenv("GPX_SET", "set-value")
        monkeypatch.setenv("GPX_EMPTY", "")
        monkeypatch.setenv("GPX_TILDE", "~")
        monkeypatch.delenv("GPX_UNSET", raising=False)
        assert _shell_expand(value) == expected

    @pytest.mark.parametrize("value", ["${GPX_UNSET}", "$GPX_UNSET/lib", "${:-lib}"])
    def test_an_unset_variable_without_a_default_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        from graftpunk.devtools.plugin_project import _shell_expand, _ShellExpandError

        monkeypatch.delenv("GPX_UNSET", raising=False)
        with pytest.raises(_ShellExpandError):
            _shell_expand(value)

    def test_a_src_default_is_used_when_the_variable_is_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GPX_UNSET", raising=False)
        conftest = _lib_project(tmp_path, ruff='\n[tool.ruff]\nsrc = ["${GPX_UNSET:-lib}"]\n')
        assert read_project(tmp_path).first_party_packages == frozenset({"myshop_shared"})
        upgrade_project(tmp_path)
        assert "\n\nfrom myshop_shared import THING" in conftest.read_text()
        _ruff_check_conftest(tmp_path)

    def test_an_extend_default_is_used_when_the_variable_is_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GPX_UNSET", raising=False)
        conftest = _lib_project(
            tmp_path, ruff='\n[tool.ruff]\nextend = "${GPX_UNSET:-config}/base.toml"\n'
        )
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "base.toml").write_text('src = ["../lib"]\n')
        assert read_project(tmp_path).first_party_packages == frozenset({"myshop_shared"})
        upgrade_project(tmp_path)
        _ruff_check_conftest(tmp_path)
        assert "\n\nfrom myshop_shared import THING" in conftest.read_text()

    @pytest.mark.parametrize(
        "ruff",
        [
            '\n[tool.ruff]\nsrc = ["${GPX_UNSET}"]\n',
            '\n[tool.ruff]\nextend = "$GPX_UNSET/base.toml"\n',
            '\n[tool.ruff]\nsrc = ["lib"]\nextend = "base.toml"\n',
        ],
        ids=["src", "extend", "overridden-src"],
    )
    def test_an_unset_variable_without_a_default_leaves_the_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ruff: str
    ) -> None:
        """ruff refuses to run, even when the bad src is one an extending file
        overrides, since every file's src is resolved."""
        monkeypatch.delenv("GPX_UNSET", raising=False)
        _lib_project(tmp_path, ruff=ruff)
        (tmp_path / "base.toml").write_text('src = ["${GPX_UNSET}"]\n')
        names = read_project(tmp_path).first_party_packages
        assert {"aaa_plugin", "lib", "tests"} <= names
        assert "myshop_shared" not in names


def _nested_project(tmp_path: Path, repo_ruff: str) -> tuple[Path, Path]:
    """A repo whose pyproject.toml has [tool.ruff], and under plugins/myshop a
    plugin project whose own pyproject.toml has none, with a package beside
    it the conftest imports."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "repo"\n' + repo_ruff)
    project = repo / "plugins" / "myshop"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        '[project]\nname = "aaa"\n\n[project.entry-points."graftpunk.plugins"]\n'
        'aaa = "aaa_plugin:AaaPlugin"\n'
    )
    (project / "aaa_plugin.py").write_text(_SINGLE_MODULE_PLUGIN)
    (project / "myshop_pkg").mkdir()
    (project / "myshop_pkg" / "__init__.py").write_text("THING = 1\n")
    conftest = project / "tests" / "conftest.py"
    conftest.parent.mkdir()
    conftest.write_text("from myshop_pkg import THING\n\nX = THING\n")
    return project, conftest


class TestAncestorRuffSettings:
    """With no settings file of its own, ruff uses the nearest ancestor's, and
    its default src is that file's directory and its src/."""

    def test_an_ancestor_config_moves_the_default_roots_to_its_directory(
        self, tmp_path: Path
    ) -> None:
        project, conftest = _nested_project(tmp_path, '\n[tool.ruff.lint]\nselect = ["I"]\n')
        assert read_project(project).first_party_packages == frozenset({"plugins"})
        upgrade_project(project)
        assert conftest.read_text().splitlines()[:4] == [
            "from pathlib import Path",
            "",
            "from graftpunk.testing.plugin import fixtures_are_sanitised",
            "from myshop_pkg import THING",
        ]
        _ruff_check_conftest(project)

    def test_an_ancestor_src_is_taken_from_the_ancestors_directory(self, tmp_path: Path) -> None:
        project, conftest = _nested_project(
            tmp_path,
            '\n[tool.ruff]\nsrc = ["plugins/myshop"]\n\n[tool.ruff.lint]\nselect = ["I"]\n',
        )
        assert "myshop_pkg" in read_project(project).first_party_packages
        upgrade_project(project)
        assert "\n\nfrom myshop_pkg import THING" in conftest.read_text()
        _ruff_check_conftest(project)

    def test_an_ancestor_ruff_toml_counts_too(self, tmp_path: Path) -> None:
        project, _conftest = _nested_project(tmp_path, "")
        (tmp_path / "repo" / "ruff.toml").write_text('src = ["plugins/myshop"]\n')
        assert "myshop_pkg" in read_project(project).first_party_packages

    def test_the_projects_own_tool_ruff_stops_the_search(self, tmp_path: Path) -> None:
        project, _conftest = _nested_project(tmp_path, '\n[tool.ruff]\nsrc = ["elsewhere"]\n')
        pyproject = project / "pyproject.toml"
        pyproject.write_text(pyproject.read_text() + '\n[tool.ruff.lint]\nselect = ["I"]\n')
        assert {"aaa_plugin", "myshop_pkg", "tests"} <= read_project(project).first_party_packages
