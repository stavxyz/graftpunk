"""Tests for graftpunk.paths — settings-free path resolution."""

from pathlib import Path

import pytest

from graftpunk import paths


def test_config_dir_defaults_to_home_config(monkeypatch):
    monkeypatch.delenv("GRAFTPUNK_CONFIG_DIR", raising=False)
    assert paths.config_dir() == Path.home() / ".config" / "graftpunk"


def test_config_dir_honors_real_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path / "custom"))
    assert paths.config_dir() == tmp_path / "custom"


def test_config_dir_creates_no_directories(monkeypatch, tmp_path):
    # Settings-free AND side-effect-free: resolving must not mkdir
    # (GraftpunkSettings.__init__ creates dirs; this helper must not).
    target = tmp_path / "never-created"
    monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(target))
    paths.config_dir()
    paths.workstation_env_path()
    assert not target.exists()


def test_workstation_env_path_is_env_under_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
    assert paths.workstation_env_path() == tmp_path / "env"


def test_pydantic_config_dir_default_routes_through_paths(monkeypatch, tmp_path):
    # Single owner: the settings field default and paths.config_dir() can
    # never diverge because the former calls the latter. Delete the env var
    # (else pydantic's env source supplies the field and the default never
    # runs) and monkeypatch the resolver to prove the DEFAULT routes
    # through it.
    monkeypatch.delenv("GRAFTPUNK_CONFIG_DIR", raising=False)
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / "routed")
    from graftpunk.config import GraftpunkSettings

    settings = GraftpunkSettings(_env_file=None)
    assert settings.config_dir == tmp_path / "routed"


class TestOneResolver:
    """settings.config_dir and paths.config_dir() cannot disagree (#282)."""

    @staticmethod
    def _fresh_settings():  # noqa: ANN205
        from graftpunk import cache, config, workstation_env

        config.reset_settings()
        workstation_env._invalidate_cache()
        cache._reset_session_storage_backend()
        return config.get_settings()

    def test_an_empty_value_is_unset_for_both(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", "")

        settings = self._fresh_settings()

        default = Path.home() / ".config" / "graftpunk"
        assert paths.config_dir() == default
        assert settings.config_dir == default
        assert paths.workstation_env_path().parent == settings.config_dir
        assert not (tmp_path / "sessions").exists()

    def test_a_value_only_in_a_cwd_dotenv_is_ignored_by_both_and_warned_about(
        self, monkeypatch, tmp_path, captured_logs
    ):
        project = tmp_path / "project-config"
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        (cwd / ".env").write_text(f"GRAFTPUNK_CONFIG_DIR={project}\n")
        monkeypatch.chdir(cwd)
        monkeypatch.delenv("GRAFTPUNK_CONFIG_DIR", raising=False)

        settings = self._fresh_settings()

        default = Path.home() / ".config" / "graftpunk"
        assert settings.config_dir == default
        assert paths.workstation_env_path().parent == settings.config_dir
        assert not project.exists()
        [warning] = [e for e in captured_logs if e["event"] == "config_dir_in_dotenv_ignored"]
        assert warning["log_level"] == "warning"
        assert warning["ignored"] == str(project)
        assert warning["using"] == str(default)
        assert warning["env_file"] == str(cwd / ".env")

    def test_a_dotenv_value_shadowed_by_the_real_environment_is_not_warned_about(
        self, monkeypatch, tmp_path, captured_logs
    ):
        real = tmp_path / "real-config"
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        (cwd / ".env").write_text(f"GRAFTPUNK_CONFIG_DIR={tmp_path / 'other'}\n")
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(real))

        settings = self._fresh_settings()

        assert settings.config_dir == real
        assert [e for e in captured_logs if e["event"] == "config_dir_in_dotenv_ignored"] == []

    def test_a_variable_set_after_the_settings_exist_reaches_settings_and_sessions(
        self, monkeypatch, tmp_path
    ):
        import requests

        from graftpunk import cache, encryption, workstation_env
        from graftpunk.cache import cache_session, load_session

        first = tmp_path / "first"
        later = tmp_path / "later"
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(first))
        settings = self._fresh_settings()
        encryption.reset_encryption_key_cache()
        session = requests.Session()
        session.cookies.set("sid", "1", domain="myshop.example")
        cache_session(session, "early")
        first_key = (first / ".session_key").read_bytes()

        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(later))
        session.cookies.set("sid", "2", domain="myshop.example")
        cache_session(session, "myshop")

        assert settings.config_dir == paths.config_dir() == later
        assert settings.sessions_dir == later / "sessions"
        assert paths.workstation_env_path() == later / "env"
        assert (later / "sessions" / "myshop").is_dir()
        assert not (first / "sessions" / "myshop").exists()
        # The session went to the new directory with that directory's own key,
        # so a fresh process there reads it back (#282 review).
        assert (later / ".session_key").read_bytes() != first_key
        encryption.reset_encryption_key_cache()
        cache._reset_session_storage_backend()
        workstation_env._invalidate_cache()
        loaded = load_session("myshop")
        assert loaded.cookies.get("sid", domain="myshop.example") == "2"

    def test_the_workstation_env_file_follows_a_late_change(self, monkeypatch, tmp_path):
        from graftpunk import workstation_env

        first = tmp_path / "first"
        later = tmp_path / "later"
        for directory, user in ((first, "alice"), (later, "bob")):
            directory.mkdir()
            (directory / "env").write_text(f"MYSHOP_USERNAME={user}@myshop.example\n")
            (directory / "env").chmod(0o600)
        monkeypatch.delenv("MYSHOP_USERNAME", raising=False)
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(first))
        self._fresh_settings()
        assert (
            workstation_env.load().resolve_file_value("MYSHOP_USERNAME") == "alice@myshop.example"
        )

        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(later))

        assert workstation_env.load().resolve_file_value("MYSHOP_USERNAME") == "bob@myshop.example"

    def test_a_static_config_dir_in_the_workstation_env_file_is_not_applied(
        self, monkeypatch, tmp_path, captured_logs
    ):
        """The file cannot move the directory it lives in; it warns as the command form does."""
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        (config_dir / "env").write_text(f"GRAFTPUNK_CONFIG_DIR={tmp_path / 'elsewhere'}\n")
        (config_dir / "env").chmod(0o600)
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(config_dir))

        settings = self._fresh_settings()

        assert settings.config_dir == config_dir
        assert paths.workstation_env_path() == config_dir / "env"
        assert not (tmp_path / "elsewhere").exists()
        events = [
            (e["event"], e.get("envvar"))
            for e in captured_logs
            if e["event"] == "workstation_env_static_unsupported_for_field"
        ]
        assert events == [("workstation_env_static_unsupported_for_field", "GRAFTPUNK_CONFIG_DIR")]

    @pytest.mark.parametrize("env_file", [None, "other.env", ["missing.env", "other.env"]])
    def test_the_dotenv_warning_reads_the_files_the_settings_read(
        self, monkeypatch, tmp_path, captured_logs, env_file
    ):
        from graftpunk.config import GraftpunkSettings

        cwd = tmp_path / "cwd"
        cwd.mkdir()
        (cwd / ".env").write_text(f"GRAFTPUNK_CONFIG_DIR={tmp_path / 'from-dotenv'}\n")
        (cwd / "other.env").write_text(f"GRAFTPUNK_CONFIG_DIR={tmp_path / 'from-other'}\n")
        monkeypatch.chdir(cwd)
        monkeypatch.delenv("GRAFTPUNK_CONFIG_DIR", raising=False)

        GraftpunkSettings(_env_file=env_file)

        warnings = [e for e in captured_logs if e["event"] == "config_dir_in_dotenv_ignored"]
        if env_file is None:
            assert warnings == []
        else:
            [warning] = warnings
            assert warning["env_file"] == str(cwd / "other.env")
            assert warning["ignored"] == str(tmp_path / "from-other")

    def test_a_relative_or_tilde_value_resolves_to_an_absolute_path(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", "relative/gp")
        assert paths.config_dir() == tmp_path / "relative" / "gp"

        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", "~/gp-config")
        assert paths.config_dir() == Path.home() / "gp-config"
        assert self._fresh_settings().config_dir == Path.home() / "gp-config"

    def test_a_command_value_for_the_config_dir_is_unsupported_not_a_typo(
        self, monkeypatch, tmp_path, captured_logs
    ):
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        (config_dir / "env").write_text("GRAFTPUNK_CONFIG_DIR=$(echo /elsewhere)\n")
        (config_dir / "env").chmod(0o600)
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(config_dir))

        self._fresh_settings()

        events = [
            (e["event"], e.get("envvar"))
            for e in captured_logs
            if e["event"].startswith("workstation_env_command_")
        ]
        assert events == [("workstation_env_command_unsupported_for_field", "GRAFTPUNK_CONFIG_DIR")]
