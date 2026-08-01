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


class TestLookup:
    def test_real_env_beats_file(self, env_file, monkeypatch):
        _write(env_file, "K=fromfile\n")
        monkeypatch.setenv("K", "fromenv")
        assert workstation_env.load().lookup("K") == "fromenv"

    def test_empty_string_env_is_a_miss(self, env_file, monkeypatch):
        # Intended behavior change vs. today's login flow: empty env falls
        # through to the file tier.
        _write(env_file, "K=fromfile\n")
        monkeypatch.setenv("K", "")
        assert workstation_env.load().lookup("K") == "fromfile"

    def test_static_entry_resolves_without_subprocess(self, env_file, monkeypatch):
        _write(env_file, "K=v\n")
        monkeypatch.delenv("K", raising=False)

        def boom(*a, **kw):
            raise AssertionError("no subprocess for statics")

        monkeypatch.setattr(subprocess, "run", boom)
        assert workstation_env.load().lookup("K") == "v"

    def test_command_entry_executes_and_strips_one_newline(self, env_file, monkeypatch):
        _write(env_file, "K=$(printf 'secret\\n')\n")
        monkeypatch.delenv("K", raising=False)
        assert workstation_env.load().lookup("K") == "secret"

    def test_command_memoized_single_subprocess(self, env_file, monkeypatch):
        _write(env_file, "K=$(echo hi)\n")
        monkeypatch.delenv("K", raising=False)
        env = workstation_env.load()
        calls = []
        real_run = subprocess.run

        def counting_run(*a, **kw):
            calls.append(a)
            return real_run(*a, **kw)

        monkeypatch.setattr(subprocess, "run", counting_run)
        assert env.lookup("K") == "hi"
        assert env.lookup("K") == "hi"
        assert len(calls) == 1

    def test_command_failure_memoized_returns_none_and_logs(self, env_file, monkeypatch):
        _write(env_file, "K=$(exit 3)\n")
        monkeypatch.delenv("K", raising=False)
        env = workstation_env.load()
        calls = []
        real_run = subprocess.run

        def counting_run(*a, **kw):
            calls.append(a)
            return real_run(*a, **kw)

        monkeypatch.setattr(subprocess, "run", counting_run)
        with capture_logs() as cap:
            assert env.lookup("K") is None
            assert env.lookup("K") is None
        assert len(calls) == 1  # failure memoized, never re-run
        assert any(e["event"] == "workstation_env_command_failed" for e in cap)

    def test_unknown_name_is_none(self, env_file, monkeypatch):
        _write(env_file, "K=v\n")
        monkeypatch.delenv("MISSING", raising=False)
        assert workstation_env.load().lookup("MISSING") is None

    def test_command_entry_names(self, env_file):
        _write(env_file, "A=static\nB=$(echo x)\nC=$(echo y)\n")
        assert workstation_env.load().command_entry_names() == {"B", "C"}

    def test_resolve_file_value_skips_env_tier(self, env_file, monkeypatch):
        # The --resolve primitive answers "what does the FILE say" even when
        # the real environment defines the name.
        _write(env_file, "K=fromfile\n")
        monkeypatch.setenv("K", "fromenv")
        assert workstation_env.load().resolve_file_value("K") == "fromfile"
        assert workstation_env.load().lookup("K") == "fromenv"


class TestBootstrap:
    def test_inject_static_sets_absent_only(self, env_file, monkeypatch):
        _write(env_file, "INJ_A=filevalue\nINJ_B=filevalue\nINJ_C=$(echo never)\n")
        monkeypatch.setenv("INJ_B", "envwins")
        monkeypatch.delenv("INJ_A", raising=False)
        monkeypatch.delenv("INJ_C", raising=False)
        workstation_env.load().inject_static()
        assert os.environ["INJ_A"] == "filevalue"
        assert os.environ["INJ_B"] == "envwins"
        assert "INJ_C" not in os.environ  # commands never injected

    def test_ensure_bootstrap_idempotent(self, env_file, monkeypatch):
        _write(env_file, "INJ_D=v\n")
        monkeypatch.delenv("INJ_D", raising=False)
        workstation_env.ensure_bootstrap()
        os.environ["INJ_D"] = "changed"
        workstation_env.ensure_bootstrap()  # second call must not re-inject
        assert os.environ["INJ_D"] == "changed"


class TestWriter:
    @pytest.mark.parametrize("bad_value", ["x\nEVIL=1", "x\r\nEVIL=1", "trailing\r"])
    def test_set_rejects_embedded_newline(self, env_file, bad_value):
        # A newline in the value would let one set_entry() call smuggle a
        # second NAME=value line into the file (e.g. K=$'x\nEVIL=1').
        with pytest.raises(ValueError, match="newline"):
            workstation_env.set_entry("K", bad_value)
        assert not env_file.exists()

    def test_set_appends_new_name_and_chmods(self, env_file):
        workstation_env.set_entry("NEW", "val")
        assert "NEW=val" in env_file.read_text().splitlines()
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

    def test_set_preserves_comments_and_order(self, env_file):
        _write(env_file, "# header comment\nA=1\n\n# about B\nB=2\n")
        workstation_env.set_entry("B", "3")
        assert env_file.read_text() == "# header comment\nA=1\n\n# about B\nB=3\n"

    def test_set_replaces_last_duplicate_and_warns(self, env_file):
        _write(env_file, "K=first\nK=second\n")
        with capture_logs() as cap:
            workstation_env.set_entry("K", "third")
        lines = env_file.read_text().splitlines()
        assert lines == ["K=first", "K=third"]
        assert any(e["event"] == "workstation_env_duplicate_entries" for e in cap)

    def test_set_creates_parent_dir_and_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path / "deep" / "cfg"))
        workstation_env._invalidate_cache()
        workstation_env.set_entry("K", "v")
        target = tmp_path / "deep" / "cfg" / "env"
        assert target.read_text() == "K=v\n"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_unset_removes_all_occurrences(self, env_file):
        _write(env_file, "K=a\nOTHER=x\nK=b\n")
        removed = workstation_env.unset_entry("K")
        assert removed == 2
        assert env_file.read_text() == "OTHER=x\n"

    def test_unset_absent_is_zero_and_ok(self, env_file):
        _write(env_file, "A=1\n")
        assert workstation_env.unset_entry("MISSING") == 0

    def test_write_then_lookup_sees_fresh_value(self, env_file, monkeypatch):
        # Cache coherence contract: writers invalidate the load() cache.
        _write(env_file, "K=old\n")
        monkeypatch.delenv("K", raising=False)
        assert workstation_env.load().lookup("K") == "old"
        workstation_env.set_entry("K", "new")
        assert workstation_env.load().lookup("K") == "new"

    def test_ensure_file_creates_empty_0600_and_is_idempotent(self, env_file):
        workstation_env.ensure_file()
        assert env_file.read_text() == ""
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
        _write(env_file, "K=v\n")
        workstation_env.ensure_file()  # must not truncate an existing file
        assert env_file.read_text() == "K=v\n"
