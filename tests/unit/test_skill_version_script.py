"""scripts/check-skill-version.sh against throwaway git repositories
(graft skill spec, 2026-09-21, "Versioning and release")."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.guide_harness import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "check-skill-version.sh"
BASH = shutil.which("bash")
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(BASH is None or GIT is None, reason="needs bash and git")


def _git_env(home: Path) -> dict[str, str]:
    """No user or system git config: no hooks path, no signing, a fixed identity."""
    return {
        **os.environ,
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "alice",
        "GIT_AUTHOR_EMAIL": "alice@example.com",
        "GIT_COMMITTER_NAME": "alice",
        "GIT_COMMITTER_EMAIL": "alice@example.com",
    }


class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.env = _git_env(root.parent)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        assert GIT is not None
        return subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [GIT, *args], cwd=self.root, env=self.env, capture_output=True, text=True, check=True
        ).stdout.strip()

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def manifests(self, version: str) -> None:
        """The two manifests as Task 2 writes them: the version in plugin.json alone."""
        self.write(
            ".claude-plugin/marketplace.json",
            json.dumps({"name": "graftpunk", "plugins": [{"name": "graftpunk"}]}, indent=2),
        )
        self.write(
            ".claude-plugin/plugin.json",
            json.dumps({"name": "graftpunk", "version": version}, indent=2),
        )

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def check(self, base: str) -> subprocess.CompletedProcess[str]:
        assert BASH is not None
        return subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [BASH, str(SCRIPT), base], cwd=self.root, env=self.env, capture_output=True, text=True
        )


@pytest.fixture()
def repo(tmp_path: Path) -> _Repo:
    root = tmp_path / "repo"
    root.mkdir()
    return _Repo(root)


def test_a_skill_change_without_a_bump_fails_and_names_the_next_patch(repo: _Repo) -> None:
    repo.manifests("0.1.0")
    repo.write("skills/graft/SKILL.md", "first\n")
    base = repo.commit("base")
    repo.write("skills/graft/SKILL.md", "second\n")
    repo.commit("change without a bump")
    result = repo.check(base)
    assert result.returncode == 1
    assert "0.1.1" in result.stdout + result.stderr


def test_a_skill_change_with_a_bump_passes(repo: _Repo) -> None:
    repo.manifests("0.1.0")
    repo.write("skills/graft/SKILL.md", "first\n")
    base = repo.commit("base")
    repo.write("skills/graft/SKILL.md", "second\n")
    repo.manifests("0.1.1")
    repo.commit("change with a bump")
    assert repo.check(base).returncode == 0


def test_a_change_elsewhere_needs_no_bump(repo: _Repo) -> None:
    repo.manifests("0.1.0")
    base = repo.commit("base")
    repo.write("src/module.py", "x = 1\n")
    repo.commit("unrelated")
    assert repo.check(base).returncode == 0


def test_a_first_introduction_passes(repo: _Repo) -> None:
    repo.write("README.md", "before the skill\n")
    base = repo.commit("base")
    repo.manifests("0.1.0")
    repo.write("skills/graft/SKILL.md", "first\n")
    repo.commit("introduce the skill")
    assert repo.check(base).returncode == 0


def test_a_base_that_moved_on_after_the_branch_point_is_not_this_branchs_change(
    repo: _Repo,
) -> None:
    """The base bumped the skill after the branch point and the branch never touched
    skills/: compared against the merge base, the branch changed nothing there, and
    the base's newer version is not reported as this branch's downgrade."""
    repo.manifests("0.1.0")
    repo.write("skills/graft/SKILL.md", "first\n")
    repo.commit("branch point")
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("src/module.py", "x = 1\n")
    repo.commit("unrelated, on the branch")
    repo.git("checkout", "-q", "main")
    repo.write("skills/graft/SKILL.md", "second\n")
    repo.manifests("0.1.1")
    base = repo.commit("the base bumps the skill")
    repo.git("checkout", "-q", "feature")
    result = repo.check(base)
    assert result.returncode == 0, result.stderr
    assert "no bump needed" in result.stdout
    assert "0.1.1" not in result.stdout + result.stderr


def test_a_bump_the_base_already_used_fails_and_names_the_next(repo: _Repo) -> None:
    """The branch and the base both bumped to 0.1.1 after the branch point: once
    both land, installed users would see no new version for the branch's change, so
    the version is compared with the base itself, not with the merge base."""
    repo.manifests("0.1.0")
    repo.write("skills/graft/SKILL.md", "first\n")
    repo.commit("branch point")
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("skills/graft/references/rules.md", "a branch change\n")
    repo.manifests("0.1.1")
    repo.commit("the branch bumps")
    repo.git("checkout", "-q", "main")
    repo.write("skills/graft/SKILL.md", "second\n")
    repo.manifests("0.1.1")
    base = repo.commit("the base bumps too")
    repo.git("checkout", "-q", "feature")
    result = repo.check(base)
    assert result.returncode == 1
    assert "0.1.2" in result.stderr


def test_a_lower_version_fails(repo: _Repo) -> None:
    repo.manifests("0.2.0")
    repo.write("skills/graft/SKILL.md", "first\n")
    base = repo.commit("base")
    repo.write("skills/graft/SKILL.md", "second\n")
    repo.manifests("0.1.9")
    repo.commit("a downgrade")
    result = repo.check(base)
    assert result.returncode == 1
    assert "0.2.1" in result.stderr


@pytest.mark.parametrize(("head", "passes"), [("1.0.0", False), ("1.0.1", True)])
def test_a_trailing_zero_part_is_not_a_bump(repo: _Repo, head: str, passes: bool) -> None:
    """``1.0`` and ``1.0.0`` name the same version, so a head that only adds a zero
    part is refused, and one that raises the added part passes."""
    repo.manifests("1.0")
    repo.write("skills/graft/SKILL.md", "first\n")
    base = repo.commit("base")
    repo.write("skills/graft/SKILL.md", "second\n")
    repo.manifests(head)
    repo.commit("a longer version")
    result = repo.check(base)
    assert (result.returncode == 0) is passes, result.stderr


def test_a_non_numeric_head_over_a_numeric_base_fails(repo: _Repo) -> None:
    """``0.1.0rc1`` differs from ``0.2.0`` but no order makes it higher, so a
    numeric base refuses it rather than letting a downgrade through."""
    repo.manifests("0.2.0")
    repo.write("skills/graft/SKILL.md", "first\n")
    base = repo.commit("base")
    repo.write("skills/graft/SKILL.md", "second\n")
    repo.manifests("0.1.0rc1")
    repo.commit("a non-numeric version")
    result = repo.check(base)
    assert result.returncode == 1
    assert "0.2.1" in result.stderr


def test_a_guide_change_needs_a_bump(repo: _Repo) -> None:
    """The installed plugin carries the guide the skill reads, so a guide change
    reaches installed users only with a new version."""
    repo.manifests("0.1.0")
    repo.write("docs/PLUGIN_DEVELOPMENT.md", "first\n")
    base = repo.commit("base")
    repo.write("docs/PLUGIN_DEVELOPMENT.md", "second\n")
    repo.commit("a guide change without a bump")
    assert repo.check(base).returncode == 1


def test_the_script_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)


def test_a_version_without_a_numeric_last_part_still_fails_in_one_line(repo: _Repo) -> None:
    """A suffix such as ``0.1.0rc1`` has no next patch to name; the script still
    refuses with its own line rather than a Python traceback."""
    repo.manifests("0.1.0rc1")
    repo.write("skills/graft/SKILL.md", "first\n")
    base = repo.commit("base")
    repo.write("skills/graft/SKILL.md", "second\n")
    repo.commit("change without a bump")
    result = repo.check(base)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert result.stderr.strip() == (
        "skill-version: the skill changed, but .claude-plugin/plugin.json is 0.1.0rc1 "
        "and the base is at 0.1.0rc1. Bump it to a higher version."
    )


def test_the_workflow_filter_and_the_script_watch_the_same_paths() -> None:
    """The workflow decides whether to run the script and the script decides what
    counts as a skill change; a path added to one and not the other would let a
    change slip past, or run the check for nothing."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "skill-version.yml").read_text()
    skill_filter = workflow.split("skill:", 1)[1].split("\n\n", 1)[0]
    filtered = {entry.removesuffix("**") for entry in re.findall(r"- '([^']+)'", skill_filter)}
    script = SCRIPT.read_text()
    match = re.search(r"^watched='\^\(([^)]*)\)'$", script, re.MULTILINE)
    assert match, "the script's path pattern moved"
    watched = {part.replace("\\.", ".").removesuffix("$") for part in match.group(1).split("|")}
    assert filtered == watched == {"skills/", ".claude-plugin/", "docs/PLUGIN_DEVELOPMENT.md"}
