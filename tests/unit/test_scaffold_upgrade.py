"""gp plugin upgrade's migrator (graft skill spec, 2026-09-21)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from graftpunk.devtools.plugin_project import NotAPluginProjectError, read_project
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
        assert [r.name for r in applied] == ["FIXTURES_TREE", "sanitised_fixtures"]
        assert conftest.read_text() == render(_SPEC)["tests/conftest.py"]
        states = {s.state for s in read_project(tmp_path).requirements.values()}
        assert states == {"bound"}
        _ruff_check(tmp_path)

    def test_a_project_that_has_it_is_left_byte_identical(self, tmp_path: Path) -> None:
        write_scaffold(tmp_path, _SPEC)
        conftest = tmp_path / "tests" / "conftest.py"
        before = conftest.read_bytes()
        assert upgrade_project(tmp_path) == ()
        assert conftest.read_bytes() == before

    def test_twice_in_a_row_writes_each_statement_once(self, tmp_path: Path) -> None:
        conftest = _project(tmp_path, _OLD_CONFTEST)
        upgrade_project(tmp_path)
        assert upgrade_project(tmp_path) == ()
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
        assert upgrade_project(tmp_path) == ()
        assert conftest.read_text() == wired

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

    def test_a_conftest_whose_imports_follow_code_is_refused_and_left_alone(
        self, tmp_path: Path
    ) -> None:
        odd = "X = 1\nfrom graftpunk.testing.plugin import site_env_scrubber\n"
        conftest = _project(tmp_path, odd)
        with pytest.raises(UpgradeRefusedError, match="ruff check --fix"):
            upgrade_project(tmp_path)
        assert conftest.read_text() == odd
