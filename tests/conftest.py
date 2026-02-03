"""Pytest configuration for graftpunk tests."""

import sys
from pathlib import Path

import pytest
import structlog
from structlog._config import BoundLoggerLazyProxy

# Add src directory to sys.path for test imports
src_dir = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(src_dir))


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate each test with its own config directory.

    This fixture:
    - Creates a temporary config directory for each test
    - Sets GRAFTPUNK_CONFIG_DIR to the temp directory
    - Resets the global settings instance before each test
    """
    config_dir = tmp_path / "graftpunk"
    config_dir.mkdir(parents=True, exist_ok=True)

    # Set environment variable for config directory
    monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(config_dir))

    # Reset global settings to pick up new config
    from graftpunk.config import reset_settings

    reset_settings()

    return config_dir


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
