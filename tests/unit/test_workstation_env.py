"""Tests for graftpunk.workstation_env (spec: docs/rfcs/2026-07-28-workstation-env.md)."""

import os
import stat
import subprocess
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from graftpunk import workstation_env


@pytest.fixture(autouse=True)
def _fresh_cache():
    workstation_env._invalidate_cache()
    yield
    workstation_env._invalidate_cache()


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
    return tmp_path / "env"


def _write(env_file: Path, content: str) -> None:
    env_file.write_text(content, encoding="utf-8")


class TestParser:
    def test_static_value_verbatim(self, env_file):
        _write(env_file, "SHOPKEEP_STORE_NAME=the-french-co\n")
        entry = workstation_env.load().get("SHOPKEEP_STORE_NAME")
        assert entry.kind == "static"
        assert entry.raw_value == "the-french-co"

    def test_whole_value_command_substitution(self, env_file):
        _write(env_file, 'SHOPKEEP_PASSWORD=$(op read "op://v/i/password")\n')
        entry = workstation_env.load().get("SHOPKEEP_PASSWORD")
        assert entry.kind == "command"
        assert entry.command == 'op read "op://v/i/password"'

    def test_single_quoted_is_always_static(self, env_file):
        # The escape hatch for a literal $( — single quotes suppress detection.
        _write(env_file, "WEIRD='$(not a command)'\n")
        entry = workstation_env.load().get("WEIRD")
        assert entry.kind == "static"
        assert entry.raw_value == "$(not a command)"

    def test_double_quoted_command_detected(self, env_file):
        _write(env_file, 'K="$(echo hi)"\n')
        assert workstation_env.load().get("K").kind == "command"

    def test_mixed_literal_and_command_is_static_with_warning(self, env_file):
        _write(env_file, "MIXED=pre$(echo x)post\n")
        with capture_logs() as cap:
            entry = workstation_env.load().get("MIXED")
        assert entry.kind == "static"
        assert entry.raw_value == "pre$(echo x)post"
        assert any(
            e["event"] == "workstation_env_partial_substitution_unsupported"
            and e.get("name") == "MIXED"
            for e in cap
        )

    def test_inline_hash_is_not_a_comment(self, env_file):
        _write(env_file, "K=value#notcomment\n")
        assert workstation_env.load().get("K").raw_value == "value#notcomment"

    def test_comments_and_blank_lines_skipped(self, env_file):
        _write(env_file, "# a comment\n\n  # indented comment\nK=v\n")
        entries = workstation_env.load().entries()
        assert [e.name for e in entries] == ["K"]

    def test_malformed_line_warns_and_skips(self, env_file):
        _write(env_file, "NO_EQUALS_HERE\n9BAD=name\nGOOD=1\n")
        with capture_logs() as cap:
            entries = workstation_env.load().entries()
        assert [e.name for e in entries] == ["GOOD"]
        assert any(e["event"] == "workstation_env_malformed_line" for e in cap)

    def test_duplicate_name_last_wins(self, env_file):
        _write(env_file, "K=first\nK=second\n")
        assert workstation_env.load().get("K").raw_value == "second"

    def test_absent_file_is_empty(self, env_file):
        assert workstation_env.load().entries() == []

    def test_world_readable_file_warns(self, env_file):
        _write(env_file, "K=v\n")
        os.chmod(env_file, 0o644)
        with capture_logs() as cap:
            workstation_env.load()
        assert any(e["event"] == "workstation_env_permissive_mode" for e in cap)
