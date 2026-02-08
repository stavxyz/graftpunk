"""Tests for graftpunk.client — GraftpunkClient."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from graftpunk.client import GraftpunkClient, _CommandCallable, _GroupProxy
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
