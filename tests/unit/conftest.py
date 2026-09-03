"""Shared test helpers for unit tests."""

from typing import Any

import pytest

# Rich builds a Console at import time and takes its width from the ambient
# COLUMNS, so on an 80-column terminal table cells truncate and messages wrap
# mid-word — and assertions on rendered output fail for reasons that have
# nothing to do with the code under test. CliRunner's env={"COLUMNS": ...}
# comes too late: the console already exists. Pin every module-level console
# wide instead, once, for the whole unit suite.
_CONSOLE_LOCATIONS = (
    ("graftpunk.console", "out_console"),
    ("graftpunk.console", "err_console"),
    ("graftpunk.cli.main", "console"),
    ("graftpunk.cli.session_commands", "console"),
    ("graftpunk.cli.config_commands", "console"),
    ("graftpunk.cli.keepalive_commands", "console"),
    ("graftpunk.cli.import_har", "console"),
    ("graftpunk.cli.plugin_runtime", "_format_console"),
)


@pytest.fixture(autouse=True)
def _wide_consoles(monkeypatch):  # noqa: ANN001, ANN201
    """Render as if on a wide terminal, whatever the developer's terminal is."""
    import importlib

    for module_name, attr in _CONSOLE_LOCATIONS:
        console = getattr(importlib.import_module(module_name), attr)
        monkeypatch.setattr(console, "width", 220)


@pytest.fixture()
def fresh_backend(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    """A private tmp cache dir + a reset of cache.py's backend singleton.

    ``isolated_config`` (autouse) isolates GRAFTPUNK_CONFIG_DIR, but the
    module-global storage-backend singleton survives across tests; reset it
    on both sides so each test builds its backend from this test's env.
    """
    from graftpunk import cache as cache_mod

    monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
    cache_mod._reset_session_storage_backend()
    yield
    cache_mod._reset_session_storage_backend()


def close_coro_and_return(return_value: Any = None) -> Any:
    """Create a side_effect function that closes coroutines to avoid RuntimeWarnings.

    When mocking asyncio.run(), the coroutine passed to it is created but never
    awaited. This creates RuntimeWarnings about unawaited coroutines. This helper
    creates a side_effect that properly closes the coroutine before returning.

    Args:
        return_value: Value to return from the mock.

    Returns:
        A function suitable for use as side_effect on a Mock.
    """

    def _side_effect(coro: Any) -> Any:
        # Close the coroutine to prevent "never awaited" warnings
        if hasattr(coro, "close"):
            coro.close()
        return return_value

    return _side_effect


def close_coro_and_raise(exc: Exception) -> Any:
    """Create a side_effect function that closes coroutines then raises an exception.

    Similar to close_coro_and_return, but raises an exception after closing
    the coroutine. Used for tests that verify error handling paths.

    Args:
        exc: Exception instance to raise.

    Returns:
        A function suitable for use as side_effect on a Mock.
    """

    def _side_effect(coro: Any) -> Any:
        if hasattr(coro, "close"):
            coro.close()
        raise exc

    return _side_effect


@pytest.fixture()
def _fast_login_timings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch login engine timing constants so tests don't wait real time.

    _ELEMENT_WAIT_TIMEOUT (30s) causes deadline-based loops to spin for 30
    real seconds even when asyncio.sleep is mocked.  _POST_SUBMIT_DELAY (3s)
    causes a real asyncio.sleep(3) in tests that don't mock sleep.
    _ELEMENT_RETRY_INTERVAL (1s) adds real delay between retry attempts in
    _select_with_retry's deadline loop.
    """
    monkeypatch.setattr("graftpunk.plugins.login_engine._POST_SUBMIT_DELAY", 0.001)
    monkeypatch.setattr("graftpunk.plugins.login_engine._ELEMENT_WAIT_TIMEOUT", 0.05)
    monkeypatch.setattr("graftpunk.plugins.login_engine._ELEMENT_RETRY_INTERVAL", 0.001)
    monkeypatch.setattr("graftpunk.plugins.login_engine._LOGIN_NAV_TIMEOUT", 0.05)
    monkeypatch.setattr("graftpunk.plugins.login_engine._FIELD_SETTLE_DELAY", 0.001)
