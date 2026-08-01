"""Tests for graftpunk.paths — settings-free path resolution."""

from pathlib import Path

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
