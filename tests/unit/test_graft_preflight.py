"""The graft skill's preflight script, against the real gp and a fake one
(graft skill spec, 2026-09-21, "Preflight").

Preflight asks gp every version question in one call and relays the answers; it
parses no JSON and compares nothing. The fake gp below answers as told and
records the arguments it was given, so these tests pin what preflight asks and
what it does with each exit status.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from packaging.version import Version

import graftpunk
from tests.unit.guide_harness import REPO_ROOT

PREFLIGHT = REPO_ROOT / "skills" / "graft" / "scripts" / "preflight.sh"
BASH = shutil.which("bash")
# The only programs preflight may use besides bash builtins, gp, and uv (which it
# only looks for). The PATH the fake-gp tests build holds nothing else, so a
# preflight that reached for a JSON or text tool would fail there; the real-gp
# tests also put the environment's own bin directory on the PATH, so they do not
# prove it.
_TOOLS = ("mktemp", "cat", "rm")
_REAL_GP_DIR = Path(sys.executable).parent
_VERSION_OK = '{"contracts": {"endpoints": 1, "info": 1}, "graftpunk": "9.9.9"}'
_INFO_EMPTY = '{"directory": "empty", "plugins": [], "schema": 1}'
_OLDER_CALLER = (
    "info: this graftpunk writes schema 2 and the caller reads 1; "
    "the caller is older than graftpunk."
)
# What gp version prints, with exit 1, for an --at-least value it cannot read.
_UNREADABLE_FLOOR = "--at-least: 'one.two' is not a version graftpunk can order."


def _constant(name: str) -> str:
    match = re.search(rf'^{name}="?([^"\n]+)"?$', PREFLIGHT.read_text(), re.M)
    assert match, f"preflight.sh declares no {name}"
    return match.group(1)


def _tools(tmp_path: Path) -> Path:
    """A directory holding only the programs preflight needs, so no host binary called
    gp can stand in for the one a test means. Preflight only checks that uv is on
    the PATH and never runs it, so a stub that exits 99 stands in for it."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in _TOOLS:
        found = shutil.which(name)
        assert found, name
        (tools / name).symlink_to(found)
    uv = tools / "uv"
    uv.write_text(f"#!{BASH}\nexit 99\n")
    uv.chmod(0o755)
    return tools


def _env(home: Path, *path_dirs: Path) -> dict[str, str]:
    return {
        "PATH": os.pathsep.join(str(d) for d in path_dirs),
        "HOME": str(home),
        "NO_COLOR": "1",
        "GRAFTPUNK_CONFIG_DIR": str(home / "gp-config"),
    }


def _preflight(cwd: Path, home: Path, *path_dirs: Path) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [BASH, str(PREFLIGHT)],
        cwd=cwd,
        env=_env(home, *path_dirs),
        capture_output=True,
        text=True,
        timeout=180,
    )


def _real_gp(cwd: Path, home: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [str(_REAL_GP_DIR / "gp"), *args],
        cwd=cwd,
        env=_env(home, _REAL_GP_DIR),
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )
    return result.stdout


def _fake_gp(
    tmp_path: Path,
    *,
    version_json: str = _VERSION_OK,
    version_exit: int = 0,
    version_message: str = "",
    info_json: str = _INFO_EMPTY,
    info_exit: int = 0,
) -> Path:
    """A gp that answers exactly the two calls preflight makes, as told, and writes the
    arguments of its version call, one per line, to version-args beside itself."""
    fake = tmp_path / "fake"
    fake.mkdir()
    script = fake / "gp"
    script.write_text(
        f"""#!{BASH}
if [ "$1" = version ]; then
  printf '%s\\n' "$@" > "{fake}/version-args"
  if [ {version_exit} -eq 2 ]; then echo "No such option: --contract" >&2; exit 2; fi
  printf '%s\\n' '{version_json}'
  if [ -n "{version_message}" ]; then printf '%s\\n' "{version_message}" >&2; fi
  exit {version_exit}
fi
if [ "$1" = plugin ] && [ "$2" = info ]; then
  if [ {info_exit} -eq 2 ]; then echo "No such option: --json" >&2; exit 2; fi
  printf '%s\\n' '{info_json}'
  exit {info_exit}
fi
exit 99
"""
    )
    script.chmod(0o755)
    return fake


@pytest.fixture()
def dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """(work directory, home, tools directory)."""
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return work, home, _tools(tmp_path)


def test_the_installed_graftpunk_meets_the_skill_floor() -> None:
    """The skill must not merge ahead of the graftpunk release it needs."""
    floor = _constant("SKILL_REQUIRES_GRAFTPUNK")
    assert Version(graftpunk.__version__) >= Version(floor), (
        f"graftpunk {graftpunk.__version__} is below the skill's floor {floor}"
    )


def test_preflight_is_executable() -> None:
    assert os.access(PREFLIGHT, os.X_OK)


@pytest.mark.skipif(BASH is None, reason="preflight is a bash script")
class TestPreflightWithTheRealGp:
    @pytest.mark.parametrize("kind", ["empty", "plugin", "foreign"])
    def test_both_answers_are_relayed_unchanged(
        self, dirs: tuple[Path, Path, Path], kind: str
    ) -> None:
        """Exit 0 also means the real gp accepted the skill's floor and both of its
        contract numbers."""
        work, home, tools = dirs
        if kind == "plugin":
            _real_gp(
                work,
                home,
                "plugin",
                "new",
                "myshop",
                "--url",
                "https://myshop.example",
                "--dir",
                str(work),
            )
        elif kind == "foreign":
            (work / "pyproject.toml").write_text('[project]\nname = "other"\n')
        result = _preflight(work, home, _REAL_GP_DIR, tools)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert set(payload) == {"installation", "project"}
        assert payload["installation"] == json.loads(_real_gp(work, home, "version", "--json"))
        assert payload["project"] == json.loads(_real_gp(work, home, "plugin", "info", "--json"))
        assert payload["project"]["directory"] == kind


@pytest.mark.skipif(BASH is None, reason="preflight is a bash script")
class TestPreflightWithAFakeGp:
    def test_it_asks_for_its_floor_and_both_contract_numbers_in_one_call(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        fake = _fake_gp(tmp_path)
        assert _preflight(work, home, fake, tools).returncode == 0
        assert (fake / "version-args").read_text().splitlines() == [
            "version",
            "--json",
            "--at-least",
            _constant("SKILL_REQUIRES_GRAFTPUNK"),
            "--contract",
            f"info={_constant('SKILL_READS_INFO_SCHEMA')}",
            "--contract",
            f"endpoints={_constant('SKILL_READS_ENDPOINTS_SCHEMA')}",
        ]

    def test_gp_missing_exits_1_with_the_install_line(self, dirs: tuple[Path, Path, Path]) -> None:
        work, home, tools = dirs
        result = _preflight(work, home, tools)
        assert result.returncode == 1
        assert "gp is not on PATH" in result.stderr
        assert "uv tool install graftpunk" in result.stderr
        assert result.stdout == ""

    def test_uv_missing_exits_1_with_its_install_page_before_asking_gp(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        """The commands that load the plugin run through uv run, so without uv the
        skill stops at preflight rather than at the harden step."""
        work, home, tools = dirs
        (tools / "uv").unlink()
        fake = _fake_gp(tmp_path)
        result = _preflight(work, home, fake, tools)
        assert result.returncode == 1
        assert "uv is not on PATH" in result.stderr
        assert "https://docs.astral.sh/uv/" in result.stderr
        assert not (fake / "version-args").exists()
        assert result.stdout == ""

    def test_gp_version_exit_1_exits_1_with_gps_message_and_the_upgrade_line(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        """gp's exit 1 with a message of its own: a floor it cannot read prints
        nothing on stdout and says so on stderr (src/graftpunk/cli/main.py, version)."""
        work, home, tools = dirs
        fake = _fake_gp(
            tmp_path, version_json="", version_exit=1, version_message=_UNREADABLE_FLOOR
        )
        result = _preflight(work, home, fake, tools)
        assert result.returncode == 1
        assert _UNREADABLE_FLOOR in result.stderr
        assert "uv tool upgrade graftpunk" in result.stderr
        for reading in ("older than", "version floor", "--contract value"):
            assert reading in result.stderr, reading
        assert result.stdout == ""

    def test_a_floor_not_met_exits_1_with_the_upgrade_line_and_the_readings(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        """gp's exit 1 with no message: the installed graftpunk is below the floor."""
        work, home, tools = dirs
        result = _preflight(work, home, _fake_gp(tmp_path, version_exit=1), tools)
        assert result.returncode == 1
        assert "uv tool upgrade graftpunk" in result.stderr
        for reading in ("older than", "version floor", "--contract value"):
            assert reading in result.stderr, reading
        assert result.stdout == ""

    def test_a_contract_mismatch_exits_1_relaying_gp_and_the_fix_for_each_side(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        fake = _fake_gp(tmp_path, version_exit=3, version_message=_OLDER_CALLER)
        result = _preflight(work, home, fake, tools)
        assert result.returncode == 1
        assert "different schemas" in result.stderr
        assert _OLDER_CALLER in result.stderr
        assert "/plugin marketplace update graftpunk" in result.stderr
        assert "Installed tab of /plugin" in result.stderr
        assert "claude plugin update graftpunk@graftpunk" in result.stderr
        # /plugin update has no session form; /plugin would open the Discover tab.
        assert "/plugin update" not in result.stderr
        assert "uv tool upgrade graftpunk" in result.stderr
        assert result.stdout == ""

    def test_a_rejected_version_option_exits_1_naming_both_readings(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        result = _preflight(work, home, _fake_gp(tmp_path, version_exit=2), tools)
        assert result.returncode == 1
        assert "older than" in result.stderr
        assert "misspelled flag" in result.stderr
        assert "No such option: --contract" in result.stderr
        assert "uv tool upgrade graftpunk" in result.stderr

    def test_a_rejected_info_option_exits_1_naming_the_call(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        result = _preflight(work, home, _fake_gp(tmp_path, info_exit=2), tools)
        assert result.returncode == 1
        assert "gp rejected an option preflight passed (gp plugin info --json)" in result.stderr
        assert "No such option: --json" in result.stderr

    def test_an_unexpected_version_status_exits_1_relaying_gps_output(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        """A gp version crash has its own message, never the unreadable-project one."""
        work, home, tools = dirs
        fake = _fake_gp(tmp_path, version_exit=70, version_message="Traceback: boom")
        result = _preflight(work, home, fake, tools)
        assert result.returncode == 1
        assert "exited 70" in result.stderr
        assert "Traceback: boom" in result.stderr
        assert "could not read the project" not in result.stderr

    def test_a_project_gp_cannot_read_exits_1_with_gps_message(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        fake = _fake_gp(tmp_path, info_json="pyproject.toml: not valid TOML", info_exit=1)
        result = _preflight(work, home, fake, tools)
        assert result.returncode == 1
        assert "could not read the project" in result.stderr
        assert "pyproject.toml: not valid TOML" in result.stderr
        assert result.stdout == ""

    def test_a_floor_that_is_met_exits_0_and_relays(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        result = _preflight(work, home, _fake_gp(tmp_path), tools)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "installation": json.loads(_VERSION_OK),
            "project": json.loads(_INFO_EMPTY),
        }
