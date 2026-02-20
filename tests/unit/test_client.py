"""Tests for graftpunk.client — GraftpunkClient and shared execution functions."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
import requests

from graftpunk.client import (
    GraftpunkClient,
    _CommandCallable,
    _enforce_shared_rate_limit,
    _GroupProxy,
    _run_handler_with_limits,
    execute_plugin_command,
)
from graftpunk.exceptions import CommandError, SessionNotFoundError
from graftpunk.plugins.cli_plugin import CommandContext, CommandResult, CommandSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(
    name: str,
    group: str | None = None,
    handler: Any = None,
    requires_session: bool | None = None,
    saves_session: bool = False,
    max_retries: int = 0,
    rate_limit: float | None = None,
) -> CommandSpec:
    """Create a minimal CommandSpec for testing."""
    return CommandSpec(
        name=name,
        handler=handler or (lambda ctx, **kw: kw),
        group=group,
        requires_session=requires_session,
        saves_session=saves_session,
        max_retries=max_retries,
        rate_limit=rate_limit,
    )


def _make_plugin(
    site_name: str = "testsite",
    commands: list[CommandSpec] | None = None,
    requires_session: bool = True,
    session_name: str = "",
    token_config: Any = None,
    base_url: str = "",
    backend: str = "selenium",
    api_version: int = 1,
) -> MagicMock:
    """Create a mock plugin with configurable commands."""
    plugin = MagicMock()
    plugin.site_name = site_name
    plugin.session_name = session_name or site_name
    plugin.requires_session = requires_session
    plugin.token_config = token_config
    plugin.base_url = base_url
    plugin.backend = backend
    plugin.api_version = api_version
    plugin._plugin_config = None
    plugin.format_overrides = None
    plugin.get_commands.return_value = commands or []
    return plugin


# ---------------------------------------------------------------------------
# GraftpunkClient initialisation
# ---------------------------------------------------------------------------


class TestClientInit:
    """Tests for GraftpunkClient.__init__."""

    @patch("graftpunk.client.get_plugin")
    def test_creates_client(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _make_plugin(commands=[])
        client = GraftpunkClient("testsite")
        mock_get.assert_called_once_with("testsite")
        assert client._plugin.site_name == "testsite"

    @patch("graftpunk.client.get_plugin")
    def test_builds_top_commands(self, mock_get: MagicMock) -> None:
        specs = [_make_spec("login"), _make_spec("status")]
        mock_get.return_value = _make_plugin(commands=specs)
        client = GraftpunkClient("testsite")
        assert set(client._top_commands) == {"login", "status"}
        assert client._groups == {}

    @patch("graftpunk.client.get_plugin")
    def test_builds_grouped_commands(self, mock_get: MagicMock) -> None:
        specs = [
            _make_spec("list", group="invoice"),
            _make_spec("create", group="invoice"),
            _make_spec("login"),
        ]
        mock_get.return_value = _make_plugin(commands=specs)
        client = GraftpunkClient("testsite")
        assert "login" in client._top_commands
        assert "invoice" in client._groups
        assert set(client._groups["invoice"]) == {"list", "create"}


# ---------------------------------------------------------------------------
# Attribute access
# ---------------------------------------------------------------------------


class TestClientAttributeAccess:
    """Tests for GraftpunkClient.__getattr__."""

    @patch("graftpunk.client.get_plugin")
    def test_top_command_returns_callable(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _make_plugin(commands=[_make_spec("login")])
        client = GraftpunkClient("testsite")
        result = client.login
        assert isinstance(result, _CommandCallable)

    @patch("graftpunk.client.get_plugin")
    def test_group_returns_proxy(self, mock_get: MagicMock) -> None:
        specs = [_make_spec("list", group="invoice")]
        mock_get.return_value = _make_plugin(commands=specs)
        client = GraftpunkClient("testsite")
        result = client.invoice
        assert isinstance(result, _GroupProxy)

    @patch("graftpunk.client.get_plugin")
    def test_unknown_raises_attribute_error(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _make_plugin(commands=[])
        client = GraftpunkClient("testsite")
        with pytest.raises(AttributeError, match="no command or group 'nope'"):
            _ = client.nope


# ---------------------------------------------------------------------------
# _GroupProxy
# ---------------------------------------------------------------------------


class TestGroupProxy:
    """Tests for _GroupProxy.__getattr__."""

    @patch("graftpunk.client.get_plugin")
    def test_group_command_returns_callable(self, mock_get: MagicMock) -> None:
        specs = [_make_spec("list", group="invoice")]
        mock_get.return_value = _make_plugin(commands=specs)
        client = GraftpunkClient("testsite")
        proxy = client.invoice
        result = proxy.list
        assert isinstance(result, _CommandCallable)

    @patch("graftpunk.client.get_plugin")
    def test_group_unknown_raises_attribute_error(self, mock_get: MagicMock) -> None:
        specs = [_make_spec("list", group="invoice")]
        mock_get.return_value = _make_plugin(commands=specs)
        client = GraftpunkClient("testsite")
        proxy = client.invoice
        with pytest.raises(AttributeError, match="no command 'nope'"):
            _ = proxy.nope


# ---------------------------------------------------------------------------
# _CommandCallable
# ---------------------------------------------------------------------------


class TestCommandCallable:
    """Tests for _CommandCallable.__call__."""

    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_call_delegates_to_execute_command(
        self, mock_get: MagicMock, mock_load: MagicMock
    ) -> None:
        handler = MagicMock(return_value={"ok": True})
        spec = _make_spec("login", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = MagicMock(spec=requests.Session)
        client = GraftpunkClient("testsite")
        result = client.login()
        assert result.data == {"ok": True}


# ---------------------------------------------------------------------------
# _resolve_command (string dispatch)
# ---------------------------------------------------------------------------


class TestResolveCommand:
    """Tests for GraftpunkClient._resolve_command."""

    @patch("graftpunk.client.get_plugin")
    def test_resolve_top_level(self, mock_get: MagicMock) -> None:
        spec = _make_spec("login")
        mock_get.return_value = _make_plugin(commands=[spec])
        client = GraftpunkClient("testsite")
        assert client._resolve_command("login") is spec

    @patch("graftpunk.client.get_plugin")
    def test_resolve_grouped(self, mock_get: MagicMock) -> None:
        spec = _make_spec("list", group="invoice")
        mock_get.return_value = _make_plugin(commands=[spec])
        client = GraftpunkClient("testsite")
        assert client._resolve_command("invoice", "list") is spec

    @patch("graftpunk.client.get_plugin")
    def test_resolve_unknown_top_level(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _make_plugin(commands=[])
        client = GraftpunkClient("testsite")
        with pytest.raises(AttributeError, match="no command 'nope'"):
            client._resolve_command("nope")

    @patch("graftpunk.client.get_plugin")
    def test_resolve_unknown_group(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _make_plugin(commands=[])
        client = GraftpunkClient("testsite")
        with pytest.raises(AttributeError, match="no group 'nope'"):
            client._resolve_command("nope", "cmd")

    @patch("graftpunk.client.get_plugin")
    def test_resolve_unknown_group_command(self, mock_get: MagicMock) -> None:
        specs = [_make_spec("list", group="invoice")]
        mock_get.return_value = _make_plugin(commands=specs)
        client = GraftpunkClient("testsite")
        with pytest.raises(AttributeError, match="no command 'nope'"):
            client._resolve_command("invoice", "nope")

    @patch("graftpunk.client.get_plugin")
    def test_resolve_bad_arg_count(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _make_plugin(commands=[])
        client = GraftpunkClient("testsite")
        with pytest.raises(ValueError, match="1 arg .* or 2 args"):
            client._resolve_command("a", "b", "c")


# ---------------------------------------------------------------------------
# execute (string dispatch entry point)
# ---------------------------------------------------------------------------


class TestExecute:
    """Tests for GraftpunkClient.execute string dispatch."""

    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_execute_delegates(self, mock_get: MagicMock, mock_load: MagicMock) -> None:
        handler = MagicMock(return_value={"ok": True})
        spec = _make_spec("login", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = MagicMock(spec=requests.Session)
        client = GraftpunkClient("testsite")
        result = client.execute("login")
        assert result.data == {"ok": True}


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


class TestContextManager:
    """Tests for __enter__ and __exit__."""

    @patch("graftpunk.client.get_plugin")
    def test_enter_returns_self(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _make_plugin(commands=[])
        client = GraftpunkClient("testsite")
        assert client.__enter__() is client

    @patch("graftpunk.client.get_plugin")
    def test_exit_calls_close(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _make_plugin(commands=[])
        client = GraftpunkClient("testsite")
        with client:
            pass
        # teardown should have been called
        client._plugin.teardown.assert_called_once()

    @patch("graftpunk.client.get_plugin")
    def test_close_handles_teardown_exception(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _make_plugin(commands=[])
        client = GraftpunkClient("testsite")
        client._plugin.teardown.side_effect = RuntimeError("boom")
        # Should not raise
        client.close()


# ---------------------------------------------------------------------------
# __getattr__ infinite recursion guard
# ---------------------------------------------------------------------------


class TestGetAttrGuard:
    """Tests for __getattr__ guard against internal attribute lookups."""

    @patch("graftpunk.client.get_plugin")
    def test_underscore_attr_raises_attribute_error(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _make_plugin(commands=[])
        client = GraftpunkClient("testsite")
        with pytest.raises(AttributeError, match="_missing"):
            _ = client._missing


# ---------------------------------------------------------------------------
# Execution pipeline
# ---------------------------------------------------------------------------


class TestExecutionPipeline:
    """Tests for _execute_command pipeline."""

    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_basic_execution(self, mock_get: MagicMock, mock_load: MagicMock) -> None:
        """Handler returns a dict, result is CommandResult with that data."""
        handler = MagicMock(return_value={"items": [1, 2]})
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = MagicMock(spec=requests.Session)
        client = GraftpunkClient("testsite")
        result = client.fetch()
        assert isinstance(result, CommandResult)
        assert result.data == {"items": [1, 2]}

    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_returns_command_result_passthrough(
        self, mock_get: MagicMock, mock_load: MagicMock
    ) -> None:
        """Handler returns CommandResult, it passes through."""
        cr = CommandResult(data={"x": 1}, metadata={"page": 2})
        handler = MagicMock(return_value=cr)
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = MagicMock(spec=requests.Session)
        client = GraftpunkClient("testsite")
        result = client.fetch()
        assert result is cr

    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_passes_kwargs_to_handler(self, mock_get: MagicMock, mock_load: MagicMock) -> None:
        """Handler receives the kwargs."""
        handler = MagicMock(return_value={"ok": True})
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = MagicMock(spec=requests.Session)
        client = GraftpunkClient("testsite")
        client.fetch(status="open", limit=10)
        _, call_kwargs = handler.call_args
        assert call_kwargs["status"] == "open"
        assert call_kwargs["limit"] == 10

    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_handler_receives_command_context(
        self, mock_get: MagicMock, mock_load: MagicMock
    ) -> None:
        """Handler's first arg is a CommandContext."""
        handler = MagicMock(return_value={})
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = MagicMock(spec=requests.Session)
        client = GraftpunkClient("testsite")
        client.fetch()
        ctx = handler.call_args[0][0]
        assert isinstance(ctx, CommandContext)
        assert ctx.plugin_name == "testsite"
        assert ctx.command_name == "fetch"

    @patch("graftpunk.client.get_plugin")
    def test_sessionless_command(self, mock_get: MagicMock) -> None:
        """Command with requires_session=False works without session."""
        handler = MagicMock(return_value={"public": True})
        spec = _make_spec("health", handler=handler, requires_session=False)
        mock_get.return_value = _make_plugin(commands=[spec])
        client = GraftpunkClient("testsite")
        result = client.health()
        assert result.data == {"public": True}

    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_session_loaded_lazily(self, mock_get: MagicMock, mock_load: MagicMock) -> None:
        """Session not loaded at init, only on first command."""
        handler = MagicMock(return_value={})
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = MagicMock(spec=requests.Session)
        client = GraftpunkClient("testsite")
        mock_load.assert_not_called()
        client.fetch()
        mock_load.assert_called_once_with("testsite")

    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_session_reused_across_calls(self, mock_get: MagicMock, mock_load: MagicMock) -> None:
        """Second call reuses the same session."""
        handler = MagicMock(return_value={})
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = MagicMock(spec=requests.Session)
        client = GraftpunkClient("testsite")
        client.fetch()
        client.fetch()
        mock_load.assert_called_once()

    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_string_dispatch_executes(self, mock_get: MagicMock, mock_load: MagicMock) -> None:
        """client.execute('group', 'cmd') works."""
        handler = MagicMock(return_value={"ok": True})
        spec = _make_spec("list", group="invoice", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = MagicMock(spec=requests.Session)
        client = GraftpunkClient("testsite")
        result = client.execute("invoice", "list")
        assert result.data == {"ok": True}


# ---------------------------------------------------------------------------
# Token injection
# ---------------------------------------------------------------------------


class TestTokenInjection:
    """Tests for token injection in the execution pipeline."""

    @patch("graftpunk.client.prepare_session")
    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_tokens_injected_when_configured(
        self,
        mock_get: MagicMock,
        mock_load: MagicMock,
        mock_prep: MagicMock,
    ) -> None:
        """Plugin with token_config triggers prepare_session."""
        handler = MagicMock(return_value={})
        spec = _make_spec("fetch", handler=handler)
        token_config = MagicMock()
        mock_get.return_value = _make_plugin(
            commands=[spec], token_config=token_config, base_url="https://ex.com"
        )
        mock_load.return_value = MagicMock(spec=requests.Session)
        client = GraftpunkClient("testsite")
        client.fetch()
        mock_prep.assert_called_once_with(mock_load.return_value, token_config, "https://ex.com")

    @patch("graftpunk.client.prepare_session")
    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_no_tokens_when_not_configured(
        self,
        mock_get: MagicMock,
        mock_load: MagicMock,
        mock_prep: MagicMock,
    ) -> None:
        """Plugin without token_config skips token injection."""
        handler = MagicMock(return_value={})
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec], token_config=None)
        mock_load.return_value = MagicMock(spec=requests.Session)
        client = GraftpunkClient("testsite")
        client.fetch()
        mock_prep.assert_not_called()


# ---------------------------------------------------------------------------
# Token retry (403)
# ---------------------------------------------------------------------------


class TestTokenRetry:
    """Tests for 403 token refresh and retry."""

    @patch("graftpunk.client.clear_cached_tokens")
    @patch("graftpunk.client.prepare_session")
    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_403_triggers_token_refresh_and_retry(
        self,
        mock_get: MagicMock,
        mock_load: MagicMock,
        mock_prep: MagicMock,
        mock_clear: MagicMock,
    ) -> None:
        """Handler throws 403 HTTPError first, succeeds on retry."""
        response_403 = MagicMock()
        response_403.status_code = 403
        response_403.url = "https://ex.com/api"
        http_err = requests.exceptions.HTTPError(response=response_403)

        handler = MagicMock(side_effect=[http_err, {"retried": True}])
        spec = _make_spec("fetch", handler=handler)
        token_config = MagicMock()
        mock_get.return_value = _make_plugin(
            commands=[spec],
            token_config=token_config,
            base_url="https://ex.com",
        )
        mock_load.return_value = MagicMock(spec=requests.Session)
        client = GraftpunkClient("testsite")
        result = client.fetch()
        assert result.data == {"retried": True}
        mock_clear.assert_called_once()
        # prepare_session called twice: initial + retry
        assert mock_prep.call_count == 2


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------


class TestSessionPersistence:
    """Tests for session persistence after command execution."""

    @patch("graftpunk.client.update_session_cookies")
    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_persists_when_saves_session(
        self,
        mock_get: MagicMock,
        mock_load: MagicMock,
        mock_update: MagicMock,
    ) -> None:
        """Command with saves_session=True triggers update_session_cookies."""
        handler = MagicMock(return_value={})
        spec = _make_spec("login", handler=handler, saves_session=True)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = MagicMock(spec=requests.Session)
        client = GraftpunkClient("testsite")
        client.login()
        mock_update.assert_called_once_with(mock_load.return_value, "testsite")

    @patch("graftpunk.client.update_session_cookies")
    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_no_persist_when_not_dirty(
        self,
        mock_get: MagicMock,
        mock_load: MagicMock,
        mock_update: MagicMock,
    ) -> None:
        """Regular command does not trigger persist."""
        handler = MagicMock(return_value={})
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = MagicMock(spec=requests.Session)
        client = GraftpunkClient("testsite")
        client.fetch()
        mock_update.assert_not_called()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Tests for error propagation from the execution pipeline."""

    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_command_error_propagates(self, mock_get: MagicMock, mock_load: MagicMock) -> None:
        """CommandError from handler propagates to caller."""
        handler = MagicMock(side_effect=CommandError("bad input"))
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = MagicMock(spec=requests.Session)
        client = GraftpunkClient("testsite")
        with pytest.raises(CommandError, match="bad input"):
            client.fetch()

    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_session_not_found_propagates(self, mock_get: MagicMock, mock_load: MagicMock) -> None:
        """SessionNotFoundError from load_session propagates."""
        handler = MagicMock(return_value={})
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.side_effect = SessionNotFoundError("no session")
        client = GraftpunkClient("testsite")
        with pytest.raises(SessionNotFoundError, match="no session"):
            client.fetch()


class TestRetryLogic:
    """Tests for _execute_with_limits retry behavior."""

    @patch("graftpunk.client.time")
    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_retries_on_transient_failure(
        self, mock_get: MagicMock, mock_load: MagicMock, mock_time: MagicMock
    ) -> None:
        """Handler succeeds on retry after transient failure."""
        mock_time.monotonic.return_value = 0.0
        mock_load.return_value = MagicMock(spec=requests.Session)
        handler = MagicMock(side_effect=[requests.ConnectionError("reset"), {"ok": True}])
        spec = _make_spec("fetch", handler=handler, max_retries=1)
        mock_get.return_value = _make_plugin(commands=[spec])

        client = GraftpunkClient("testsite")
        result = client.fetch()
        assert result.data == {"ok": True}
        assert handler.call_count == 2
        mock_time.sleep.assert_called_once_with(1)  # 2**0

    @patch("graftpunk.client.time")
    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_raises_after_retries_exhausted(
        self, mock_get: MagicMock, mock_load: MagicMock, mock_time: MagicMock
    ) -> None:
        """Raises last exception when all retries exhausted."""
        mock_time.monotonic.return_value = 0.0
        mock_load.return_value = MagicMock(spec=requests.Session)
        handler = MagicMock(
            side_effect=[
                requests.ConnectionError("fail1"),
                requests.ConnectionError("fail2"),
            ]
        )
        spec = _make_spec("fetch", handler=handler, max_retries=1)
        mock_get.return_value = _make_plugin(commands=[spec])

        client = GraftpunkClient("testsite")
        with pytest.raises(requests.ConnectionError, match="fail2"):
            client.fetch()

    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_non_retryable_error_propagates_immediately(
        self, mock_get: MagicMock, mock_load: MagicMock
    ) -> None:
        """ValueError is not retried."""
        mock_load.return_value = MagicMock(spec=requests.Session)
        handler = MagicMock(side_effect=ValueError("bad"))
        spec = _make_spec("fetch", handler=handler, max_retries=3)
        mock_get.return_value = _make_plugin(commands=[spec])

        client = GraftpunkClient("testsite")
        with pytest.raises(ValueError, match="bad"):
            client.fetch()
        assert handler.call_count == 1

    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_403_without_tokens_propagates(self, mock_get: MagicMock, mock_load: MagicMock) -> None:
        """403 HTTPError without token_config is not retried."""
        mock_load.return_value = MagicMock(spec=requests.Session)
        response_403 = MagicMock()
        response_403.status_code = 403
        handler = MagicMock(side_effect=requests.exceptions.HTTPError(response=response_403))
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])  # no token_config

        client = GraftpunkClient("testsite")
        with pytest.raises(requests.exceptions.HTTPError):
            client.fetch()
        assert handler.call_count == 1

    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_500_with_tokens_propagates(self, mock_get: MagicMock, mock_load: MagicMock) -> None:
        """500 HTTPError propagates even when token_config is set."""
        mock_load.return_value = MagicMock(spec=requests.Session)
        response_500 = MagicMock()
        response_500.status_code = 500
        handler = MagicMock(side_effect=requests.exceptions.HTTPError(response=response_500))
        spec = _make_spec("fetch", handler=handler)
        plugin = _make_plugin(commands=[spec])
        plugin.token_config = MagicMock()
        mock_get.return_value = plugin

        client = GraftpunkClient("testsite")
        with pytest.raises(requests.exceptions.HTTPError):
            client.fetch()


class TestSessionDirtyReset:
    """Tests for session dirty flag reset after persist."""

    @patch("graftpunk.client.update_session_cookies")
    @patch("graftpunk.client.clear_cached_tokens")
    @patch("graftpunk.client.prepare_session")
    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_dirty_flag_reset_after_persist(
        self,
        mock_get: MagicMock,
        mock_load: MagicMock,
        mock_prepare: MagicMock,
        mock_clear: MagicMock,
        mock_update: MagicMock,
    ) -> None:
        """_session_dirty is reset after session is persisted."""
        mock_load.return_value = MagicMock(spec=requests.Session)
        response_403 = MagicMock()
        response_403.status_code = 403
        handler = MagicMock(
            side_effect=[
                requests.exceptions.HTTPError(response=response_403),
                {"ok": True},
            ]
        )
        spec = _make_spec("fetch", handler=handler)
        plugin = _make_plugin(commands=[spec])
        plugin.token_config = MagicMock()
        mock_get.return_value = plugin

        client = GraftpunkClient("testsite")
        client.fetch()
        assert not client._session_dirty  # reset after persist


# ---------------------------------------------------------------------------
# execute_plugin_command (module-level function)
# ---------------------------------------------------------------------------


class TestExecutePluginCommand:
    """Tests for the module-level execute_plugin_command() function."""

    def _make_ctx(self, **overrides: Any) -> CommandContext:
        defaults = {
            "session": MagicMock(),
            "plugin_name": "testplugin",
            "command_name": "test",
            "api_version": 1,
        }
        defaults.update(overrides)
        return CommandContext(**defaults)

    def test_returns_command_result(self) -> None:
        """Handler return value is wrapped in CommandResult."""
        handler = MagicMock(return_value={"ok": True})
        spec = _make_spec("cmd", handler=handler)
        ctx = self._make_ctx()
        result = execute_plugin_command(spec, ctx)
        assert isinstance(result, CommandResult)
        assert result.data == {"ok": True}

    def test_passthrough_command_result(self) -> None:
        """Handler returning CommandResult passes through unchanged."""
        cr = CommandResult(data={"x": 1}, metadata={"page": 2})
        handler = MagicMock(return_value=cr)
        spec = _make_spec("cmd", handler=handler)
        ctx = self._make_ctx()
        result = execute_plugin_command(spec, ctx)
        assert result is cr

    def test_uses_custom_rate_limit_state(self) -> None:
        """Custom rate_limit_state dict is passed through."""
        state: dict[str, float] = {}
        handler = MagicMock(return_value={})
        spec = _make_spec("cmd", handler=handler, rate_limit=1.0)
        ctx = self._make_ctx()
        with patch("graftpunk.client.time"):
            execute_plugin_command(spec, ctx, rate_limit_state=state)
        assert "testplugin.cmd" in state

    def test_forwards_kwargs_to_handler(self) -> None:
        """Keyword arguments are passed to the handler."""
        handler = MagicMock(return_value={})
        spec = _make_spec("cmd", handler=handler)
        ctx = self._make_ctx()
        execute_plugin_command(spec, ctx, status="active")
        _, kw = handler.call_args
        assert kw["status"] == "active"


# ---------------------------------------------------------------------------
# _run_handler_with_limits (shared retry/rate-limit function)
# ---------------------------------------------------------------------------


class TestRunHandlerWithLimits:
    """Tests for _run_handler_with_limits retry and rate-limit logic."""

    def _make_ctx(self) -> CommandContext:
        return CommandContext(
            session=MagicMock(),
            plugin_name="testplugin",
            command_name="test",
            api_version=1,
        )

    def test_retry_succeeds_after_transient_failure(self) -> None:
        """Handler fails once then succeeds."""
        handler = MagicMock(side_effect=[requests.ConnectionError("transient"), {"ok": True}])
        spec = _make_spec("cmd", handler=handler, max_retries=2)
        ctx = self._make_ctx()
        with patch("graftpunk.client.time.sleep"):
            result = _run_handler_with_limits(handler, ctx, spec, {})
        assert result == {"ok": True}
        assert handler.call_count == 2

    def test_exhausts_all_attempts(self) -> None:
        """Raises last exception when all retries exhausted."""
        handler = MagicMock(side_effect=requests.ConnectionError("permanent"))
        spec = _make_spec("cmd", handler=handler, max_retries=2)
        ctx = self._make_ctx()
        with (
            patch("graftpunk.client.time.sleep"),
            pytest.raises(requests.ConnectionError, match="permanent"),
        ):
            _run_handler_with_limits(handler, ctx, spec, {})
        assert handler.call_count == 3

    def test_exponential_backoff_timing(self) -> None:
        """time.sleep called with 1, 2, 4 for 3 retries."""
        handler = MagicMock(side_effect=requests.ConnectionError("fail"))
        spec = _make_spec("cmd", handler=handler, max_retries=3)
        ctx = self._make_ctx()
        with (
            patch("graftpunk.client.time.sleep") as mock_sleep,
            pytest.raises(requests.ConnectionError),
        ):
            _run_handler_with_limits(handler, ctx, spec, {})
        assert mock_sleep.call_args_list == [call(1), call(2), call(4)]

    def test_no_retry_on_programming_error(self) -> None:
        """TypeError/ValueError propagate immediately without retry."""
        for exc_class in (TypeError, ValueError):
            handler = MagicMock(side_effect=exc_class("bug"))
            spec = _make_spec("cmd", handler=handler, max_retries=3)
            ctx = self._make_ctx()
            with pytest.raises(exc_class, match="bug"):
                _run_handler_with_limits(handler, ctx, spec, {})
            assert handler.call_count == 1

    @pytest.mark.parametrize(
        "exc_type",
        [requests.RequestException, ConnectionError, TimeoutError, OSError],
    )
    def test_each_retryable_exception_type(self, exc_type: type[Exception]) -> None:
        """Each retryable exception type triggers retry."""
        handler = MagicMock(side_effect=[exc_type("transient"), {"ok": True}])
        spec = _make_spec("cmd", handler=handler, max_retries=1)
        ctx = self._make_ctx()
        with patch("graftpunk.client.time.sleep"):
            result = _run_handler_with_limits(handler, ctx, spec, {})
        assert result == {"ok": True}
        assert handler.call_count == 2

    def test_zero_retries_raises_immediately(self) -> None:
        """max_retries=0 means single attempt then raise."""
        handler = MagicMock(side_effect=requests.ConnectionError("once"))
        spec = _make_spec("cmd", handler=handler, max_retries=0)
        ctx = self._make_ctx()
        with pytest.raises(requests.ConnectionError, match="once"):
            _run_handler_with_limits(handler, ctx, spec, {})
        assert handler.call_count == 1


# ---------------------------------------------------------------------------
# _enforce_shared_rate_limit
# ---------------------------------------------------------------------------


class TestEnforceSharedRateLimit:
    """Tests for _enforce_shared_rate_limit."""

    def test_first_call_no_sleep(self) -> None:
        """First call does not sleep."""
        state: dict[str, float] = {}
        with (
            patch("graftpunk.client.time.sleep") as mock_sleep,
            patch("graftpunk.client.time.monotonic", return_value=100.0),
        ):
            _enforce_shared_rate_limit("key", 1.0, state)
        mock_sleep.assert_not_called()
        assert "key" in state

    def test_rapid_second_call_sleeps(self) -> None:
        """Second call within rate limit sleeps for remainder."""
        state: dict[str, float] = {}
        with (
            patch("graftpunk.client.time.sleep") as mock_sleep,
            patch(
                "graftpunk.client.time.monotonic",
                side_effect=[100.0, 100.0, 100.5, 100.5],
            ),
        ):
            _enforce_shared_rate_limit("key", 1.0, state)
            mock_sleep.assert_not_called()
            _enforce_shared_rate_limit("key", 1.0, state)
            mock_sleep.assert_called_once_with(0.5)


# ---------------------------------------------------------------------------
# Async handler detection
# ---------------------------------------------------------------------------


class TestAsyncHandlerDetection:
    """Tests for async handler auto-execution in _run_handler_with_limits."""

    def _make_ctx(self) -> CommandContext:
        return CommandContext(
            session=MagicMock(),
            plugin_name="testplugin",
            command_name="test",
            api_version=1,
        )

    def test_async_handler_auto_executed_with_warning(self) -> None:
        """Async handlers are auto-executed via asyncio.run with a warning."""

        async def async_handler(ctx: Any, **kwargs: Any) -> dict[str, str]:
            return {"async": "result"}

        spec = _make_spec("asynccmd", handler=async_handler)
        ctx = self._make_ctx()
        with patch("graftpunk.client.LOG") as mock_log:
            result = _run_handler_with_limits(async_handler, ctx, spec, {})
        assert result == {"async": "result"}
        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args[0][0] == "async_handler_auto_executed"

    def test_async_handler_result_returned(self) -> None:
        """Return value from async handler is returned correctly."""

        async def async_handler(ctx: Any, **kwargs: Any) -> list[int]:
            return [1, 2, 3]

        spec = _make_spec("asynccmd2", handler=async_handler)
        ctx = self._make_ctx()
        result = _run_handler_with_limits(async_handler, ctx, spec, {})
        assert result == [1, 2, 3]

    def test_sync_handler_no_warning(self) -> None:
        """Sync handlers do not trigger the async warning."""

        def sync_handler(ctx: Any, **kwargs: Any) -> dict[str, str]:
            return {"sync": "result"}

        spec = _make_spec("synccmd", handler=sync_handler)
        ctx = self._make_ctx()
        with patch("graftpunk.client.LOG") as mock_log:
            result = _run_handler_with_limits(sync_handler, ctx, spec, {})
        assert result == {"sync": "result"}
        mock_log.warning.assert_not_called()

    def test_async_handler_retried_on_failure(self) -> None:
        """Async handler that fails is retried with backoff."""
        call_count = 0

        async def flaky_async(ctx: Any, **kwargs: Any) -> dict[str, str]:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("transient")
            return {"recovered": "yes"}

        spec = _make_spec("flakyasync", handler=flaky_async, max_retries=1)
        ctx = self._make_ctx()
        with patch("graftpunk.client.time.sleep"):
            result = _run_handler_with_limits(flaky_async, ctx, spec, {})
        assert result == {"recovered": "yes"}
        assert call_count == 2


# ---------------------------------------------------------------------------
# close() session persistence
# ---------------------------------------------------------------------------


class TestClosePersistence:
    """Tests for close() dirty session persistence."""

    @patch("graftpunk.client.update_session_cookies")
    @patch("graftpunk.client.get_plugin")
    def test_close_persists_dirty_session(
        self, mock_get: MagicMock, mock_update: MagicMock
    ) -> None:
        """close() calls update_session_cookies when session is dirty."""
        mock_get.return_value = _make_plugin(commands=[])
        client = GraftpunkClient("testsite")
        mock_session = MagicMock(spec=requests.Session)
        client._session = mock_session
        client._session_dirty = True
        client.close()
        mock_update.assert_called_once_with(mock_session, "testsite")

    @patch("graftpunk.client.update_session_cookies")
    @patch("graftpunk.client.get_plugin")
    def test_close_persist_failure_still_tears_down(
        self, mock_get: MagicMock, mock_update: MagicMock
    ) -> None:
        """If update_session_cookies fails, teardown still runs."""
        mock_get.return_value = _make_plugin(commands=[])
        mock_update.side_effect = RuntimeError("persist failed")
        client = GraftpunkClient("testsite")
        client._session = MagicMock(spec=requests.Session)
        client._session_dirty = True
        client.close()
        client._plugin.teardown.assert_called_once()

    @patch("graftpunk.client.update_session_cookies")
    @patch("graftpunk.client.get_plugin")
    def test_close_skips_persist_when_clean(
        self, mock_get: MagicMock, mock_update: MagicMock
    ) -> None:
        """close() does not persist when session is not dirty."""
        mock_get.return_value = _make_plugin(commands=[])
        client = GraftpunkClient("testsite")
        client._session = MagicMock(spec=requests.Session)
        client._session_dirty = False
        client.close()
        mock_update.assert_not_called()


# ---------------------------------------------------------------------------
# _session_dirty propagation from ctx to self
# ---------------------------------------------------------------------------


class TestSessionDirtyPropagation:
    """Ensure ctx._session_dirty propagates to self for close() safety."""

    @patch("graftpunk.client.update_session_cookies")
    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_ctx_dirty_sets_self_dirty_before_persist(
        self, mock_get: MagicMock, mock_load: MagicMock, mock_update: MagicMock
    ) -> None:
        """If persist fails mid-pipeline, close() retries because self is dirty."""
        mock_load.return_value = MagicMock(spec=requests.Session)

        def handler_sets_ctx_dirty(ctx: Any, **kw: Any) -> dict:
            ctx._session_dirty = True
            return {"ok": True}

        spec = _make_spec("fetch", handler=handler_sets_ctx_dirty)
        mock_get.return_value = _make_plugin(commands=[spec])

        client = GraftpunkClient("testsite")
        # Make persist fail mid-pipeline
        mock_update.side_effect = RuntimeError("disk full")

        with pytest.raises(RuntimeError, match="disk full"):
            client.fetch()

        # self._session_dirty should be True so close() retries
        assert client._session_dirty is True

    @patch("graftpunk.client.update_session_cookies")
    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_ctx_dirty_resets_after_successful_persist(
        self, mock_get: MagicMock, mock_load: MagicMock, mock_update: MagicMock
    ) -> None:
        """Successful persist resets self._session_dirty to False."""
        mock_load.return_value = MagicMock(spec=requests.Session)

        def handler_sets_ctx_dirty(ctx: Any, **kw: Any) -> dict:
            ctx._session_dirty = True
            return {"ok": True}

        spec = _make_spec("fetch", handler=handler_sets_ctx_dirty)
        mock_get.return_value = _make_plugin(commands=[spec])

        client = GraftpunkClient("testsite")
        client.fetch()

        assert client._session_dirty is False
        mock_update.assert_called_once()


# ---------------------------------------------------------------------------
# requires_session=None fallback
# ---------------------------------------------------------------------------


class TestRequiresSessionFallback:
    """Tests for requires_session=None defaulting to plugin.requires_session."""

    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_none_falls_back_to_plugin_default(
        self, mock_get: MagicMock, mock_load: MagicMock
    ) -> None:
        """requires_session=None uses plugin.requires_session (True)."""
        handler = MagicMock(return_value={})
        spec = _make_spec("fetch", handler=handler, requires_session=None)
        mock_get.return_value = _make_plugin(commands=[spec], requires_session=True)
        mock_load.return_value = MagicMock(spec=requests.Session)
        client = GraftpunkClient("testsite")
        client.fetch()
        mock_load.assert_called_once()

    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_none_falls_back_to_plugin_false(
        self, mock_get: MagicMock, mock_load: MagicMock
    ) -> None:
        """requires_session=None with plugin.requires_session=False skips session."""
        handler = MagicMock(return_value={})
        spec = _make_spec("health", handler=handler, requires_session=None)
        mock_get.return_value = _make_plugin(commands=[spec], requires_session=False)
        client = GraftpunkClient("testsite")
        client.health()
        mock_load.assert_not_called()


# ---------------------------------------------------------------------------
# gp_base_url attribute
# ---------------------------------------------------------------------------


class TestGpBaseUrl:
    """Tests for gp_base_url session attribute setting."""

    @patch("graftpunk.client.load_session_for_api")
    @patch("graftpunk.client.get_plugin")
    def test_sets_gp_base_url_when_available(
        self, mock_get: MagicMock, mock_load: MagicMock
    ) -> None:
        """Session's gp_base_url is set from plugin's base_url."""
        handler = MagicMock(return_value={})
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec], base_url="https://example.com")
        session = MagicMock(spec=requests.Session)
        session.gp_base_url = ""
        mock_load.return_value = session
        client = GraftpunkClient("testsite")
        client.fetch()
        assert session.gp_base_url == "https://example.com"


# ---------------------------------------------------------------------------
# _GroupProxy error message
# ---------------------------------------------------------------------------


class TestGroupProxyErrorMessage:
    """Tests for _GroupProxy error message listing available commands."""

    @patch("graftpunk.client.get_plugin")
    def test_error_lists_available_commands(self, mock_get: MagicMock) -> None:
        """AttributeError message lists available commands."""
        specs = [
            _make_spec("list", group="invoice"),
            _make_spec("create", group="invoice"),
        ]
        mock_get.return_value = _make_plugin(commands=specs)
        client = GraftpunkClient("testsite")
        with pytest.raises(AttributeError, match="Available: create, list"):
            _ = client.invoice.nope


# ---------------------------------------------------------------------------
# __repr__ methods
# ---------------------------------------------------------------------------


class TestReprMethods:
    """Tests for __repr__ on client classes."""

    @patch("graftpunk.client.get_plugin")
    def test_client_repr(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _make_plugin(commands=[])
        client = GraftpunkClient("testsite")
        assert repr(client) == "GraftpunkClient('testsite')"

    @patch("graftpunk.client.get_plugin")
    def test_group_proxy_repr(self, mock_get: MagicMock) -> None:
        specs = [_make_spec("list", group="invoice")]
        mock_get.return_value = _make_plugin(commands=specs)
        client = GraftpunkClient("testsite")
        proxy = client.invoice
        assert "list" in repr(proxy)

    @patch("graftpunk.client.get_plugin")
    def test_command_callable_repr(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _make_plugin(commands=[_make_spec("login")])
        client = GraftpunkClient("testsite")
        cmd = client.login
        assert repr(cmd) == "_CommandCallable('login')"
