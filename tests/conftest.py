"""Pytest configuration for graftpunk tests."""

import os
import sys
import tempfile
from pathlib import Path

import pytest
import structlog
from structlog._config import BoundLoggerLazyProxy

# Add src directory to sys.path for test imports
src_dir = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(src_dir))

# MODULE SCOPE, not a fixture: this root conftest.py is imported before ANY
# test module, at collection time — before per-test fixtures ever run.
# graftpunk.cli.main runs workstation_env.ensure_bootstrap() at ITS module
# scope too, so merely collecting a test module that imports (directly or
# transitively) graftpunk.cli.main triggers bootstrap against whatever
# GRAFTPUNK_CONFIG_DIR is live in the real process environment — which, on a
# developer machine with a populated ~/.config/graftpunk/env, means real
# statics (e.g. GRAFTPUNK_BROWSER_EXECUTABLE_PATH) get injected into
# os.environ. ensure_bootstrap()/inject_static() are idempotent-once-per-
# process, so that leak is permanent for the rest of the pytest run and
# contaminates unrelated tests (isolated_config's per-test monkeypatch runs
# far too late to prevent it). Point GRAFTPUNK_CONFIG_DIR at a fresh, empty
# temp directory (no `env` file in it) before collection can import anything,
# so collection-time bootstrap reads an absent file instead of the real one.
# setdefault (not setenv) so an operator/CI override of GRAFTPUNK_CONFIG_DIR
# still wins.
os.environ.setdefault(
    "GRAFTPUNK_CONFIG_DIR",
    tempfile.mkdtemp(prefix="graftpunk-test-config-"),
)


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate each test with its own config directory.

    This fixture:
    - Creates a temporary config directory for each test
    - Sets GRAFTPUNK_CONFIG_DIR to the temp directory
    - Resets the global settings instance before each test
    - Invalidates workstation_env's process-global cache/bootstrap flag so
      load() re-reads from THIS test's config dir instead of whatever was
      cached under a previous config dir (collection-time or another test's)
    """
    config_dir = tmp_path / "graftpunk"
    config_dir.mkdir(parents=True, exist_ok=True)

    # Set environment variable for config directory
    monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(config_dir))

    from graftpunk import workstation_env
    from graftpunk.config import reset_settings

    # Reset global settings to pick up new config
    reset_settings()
    # Retire the per-file autouse _invalidate_cache() fixtures' reason for
    # existing: this single call, at this single seam, makes every test see
    # its own config_dir's workstation env file (or lack of one).
    workstation_env._invalidate_cache()

    yield config_dir

    workstation_env._invalidate_cache()


@pytest.fixture(autouse=True)
def _reset_structlog():
    """Reset structlog after each test to prevent closed file handle errors.

    CliRunner captures stderr with a temporary file. When configure_logging()
    runs inside CliRunner, structlog binds loggers to that temp file. After
    the test, CliRunner closes the file. We reset structlog to prevent stale
    references.

    Note: With cache_logger_on_first_use=False (our current setting), the
    bind-cache clearing loop below is no longer strictly necessary, but we
    keep it as defense-in-depth in case caching is re-enabled.
    """
    yield
    structlog.reset_defaults()
    for module in list(sys.modules.values()):
        for attr in getattr(module, "__dict__", {}).values():
            if isinstance(attr, BoundLoggerLazyProxy):
                attr.__dict__.pop("bind", None)
