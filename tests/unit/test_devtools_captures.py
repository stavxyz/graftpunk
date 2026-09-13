"""The single owner of 'captures never enter git' (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from graftpunk.devtools.captures import CAPTURES_DIR, ensure_ignored, find_repo_root, is_tracked


def _git(argv: list[str], cwd: Path) -> None:
    # argv is always a fixed git invocation built by this test module, never
    # untrusted input; a runtime list argument (rather than a literal) also
    # keeps ruff's S607 (partial executable path) from flagging every call
    # site below, matching graftpunk.devtools.captures's own convention.
    subprocess.run(argv, cwd=cwd, check=True)  # noqa: S603


def _init_repo(root: Path) -> None:
    _git(["git", "init", "-q"], root)
    _git(["git", "config", "user.email", "alice@example.com"], root)
    _git(["git", "config", "user.name", "alice"], root)


class TestCapturesDir:
    def test_is_tests_captures(self) -> None:
        assert CAPTURES_DIR == "tests/captures"


class TestFindRepoRoot:
    def test_finds_the_root_of_a_git_work_tree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert find_repo_root(nested) == tmp_path.resolve()

    def test_none_outside_a_work_tree(self, tmp_path: Path) -> None:
        assert find_repo_root(tmp_path) is None

    def test_a_path_that_does_not_exist_yet_still_finds_the_root(self, tmp_path: Path) -> None:
        """The fixtures command's default target does not exist on a first run:
        the search starts from the nearest existing ancestor rather than failing
        to launch git at all."""
        _init_repo(tmp_path)
        not_yet_created = tmp_path / "tests" / "captures"
        assert not not_yet_created.exists()
        assert find_repo_root(not_yet_created) == tmp_path.resolve()

    def test_a_path_that_does_not_exist_yet_outside_a_work_tree_is_none(
        self, tmp_path: Path
    ) -> None:
        assert find_repo_root(tmp_path / "tests" / "captures") is None


class TestEnsureIgnored:
    def test_adds_the_line_when_absent(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        added = ensure_ignored(tmp_path, CAPTURES_DIR)
        assert added is True
        assert "tests/captures/" in (tmp_path / ".gitignore").read_text()

    def test_idempotent_second_call_reports_no_change(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        ensure_ignored(tmp_path, CAPTURES_DIR)
        added_again = ensure_ignored(tmp_path, CAPTURES_DIR)
        assert added_again is False
        assert (tmp_path / ".gitignore").read_text().count("tests/captures/") == 1

    def test_creates_gitignore_when_missing(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        ensure_ignored(tmp_path, CAPTURES_DIR)
        assert (tmp_path / ".gitignore").exists()


class TestIsTracked:
    def test_tracked_file_is_true(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        tracked = tmp_path / "committed.txt"
        tracked.write_text("hi")
        _git(["git", "add", "committed.txt"], tmp_path)
        _git(["git", "commit", "-q", "-m", "add"], tmp_path)
        assert is_tracked(tracked) is True

    def test_untracked_file_is_false(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        untracked = tmp_path / "scratch.txt"
        untracked.write_text("hi")
        assert is_tracked(untracked) is False
