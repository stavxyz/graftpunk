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


def _loaded(session: Any = None, name: str = "testsite") -> tuple[Any, str]:
    """What the patched loader hands back: ``(session, the slot it loaded)``.

    ``load_session_for_api_resolved`` reports the name it actually loaded — a
    bare pin resolves at load time — so the caller keys its write-backs off
    that slot rather than the name it asked for (#182).
    """
    return (session if session is not None else MagicMock(spec=requests.Session), name)


def _cache_account(name: str, account_identifier: str) -> None:
    """Cache a real (cookie-less) session under *name* in the local backend.

    For the tests that exercise the real cache rather than a mocked loader --
    only meaningful under the ``fresh_backend`` fixture.
    """
    from graftpunk.cache import cache_session
    from graftpunk.session import BrowserSession
    from graftpunk.session_identity import GP_ACCOUNT_ATTR

    stored = BrowserSession.__new__(BrowserSession)
    requests.Session.__init__(stored)
    stored._backend_type = "nodriver"
    setattr(stored, GP_ACCOUNT_ATTR, account_identifier)
    cache_session(stored, name)


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

    @patch("graftpunk.client.load_session_for_api_resolved")
    @patch("graftpunk.client.get_plugin")
    def test_call_delegates_to_execute_command(
        self, mock_get: MagicMock, mock_load: MagicMock
    ) -> None:
        handler = MagicMock(return_value={"ok": True})
        spec = _make_spec("login", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = _loaded()
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

    @patch("graftpunk.client.load_session_for_api_resolved")
    @patch("graftpunk.client.get_plugin")
    def test_execute_delegates(self, mock_get: MagicMock, mock_load: MagicMock) -> None:
        handler = MagicMock(return_value={"ok": True})
        spec = _make_spec("login", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = _loaded()
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

    @patch("graftpunk.client.load_session_for_api_resolved")
    @patch("graftpunk.client.get_plugin")
    def test_basic_execution(self, mock_get: MagicMock, mock_load: MagicMock) -> None:
        """Handler returns a dict, result is CommandResult with that data."""
        handler = MagicMock(return_value={"items": [1, 2]})
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = _loaded()
        client = GraftpunkClient("testsite")
        result = client.fetch()
        assert isinstance(result, CommandResult)
        assert result.data == {"items": [1, 2]}

    @patch("graftpunk.client.load_session_for_api_resolved")
    @patch("graftpunk.client.get_plugin")
    def test_returns_command_result_passthrough(
        self, mock_get: MagicMock, mock_load: MagicMock
    ) -> None:
        """Handler returns CommandResult, it passes through."""
        cr = CommandResult(data={"x": 1}, metadata={"page": 2})
        handler = MagicMock(return_value=cr)
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = _loaded()
        client = GraftpunkClient("testsite")
        result = client.fetch()
        assert result is cr

    @patch("graftpunk.client.load_session_for_api_resolved")
    @patch("graftpunk.client.get_plugin")
    def test_passes_kwargs_to_handler(self, mock_get: MagicMock, mock_load: MagicMock) -> None:
        """Handler receives the kwargs."""
        handler = MagicMock(return_value={"ok": True})
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = _loaded()
        client = GraftpunkClient("testsite")
        client.fetch(status="open", limit=10)
        _, call_kwargs = handler.call_args
        assert call_kwargs["status"] == "open"
        assert call_kwargs["limit"] == 10

    @patch("graftpunk.client.load_session_for_api_resolved")
    @patch("graftpunk.client.get_plugin")
    def test_handler_receives_command_context(
        self, mock_get: MagicMock, mock_load: MagicMock
    ) -> None:
        """Handler's first arg is a CommandContext."""
        handler = MagicMock(return_value={})
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = _loaded()
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

    @patch("graftpunk.client.load_session_for_api_resolved")
    @patch("graftpunk.client.get_plugin")
    def test_session_loaded_lazily(self, mock_get: MagicMock, mock_load: MagicMock) -> None:
        """Session not loaded at init, only on first command."""
        handler = MagicMock(return_value={})
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = _loaded()
        client = GraftpunkClient("testsite")
        mock_load.assert_not_called()
        client.fetch()
        # Unpinned: the operating name is resolved just above the load, on
        # first use (#181), so the load itself is exact-only (#182).
        mock_load.assert_called_once_with("testsite", resolve=False)

    @patch("graftpunk.client.load_session_for_api_resolved")
    @patch("graftpunk.client.get_plugin")
    def test_session_reused_across_calls(self, mock_get: MagicMock, mock_load: MagicMock) -> None:
        """Second call reuses the same session."""
        handler = MagicMock(return_value={})
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = _loaded()
        client = GraftpunkClient("testsite")
        client.fetch()
        client.fetch()
        mock_load.assert_called_once()

    @patch("graftpunk.client.load_session_for_api_resolved")
    @patch("graftpunk.client.get_plugin")
    def test_string_dispatch_executes(self, mock_get: MagicMock, mock_load: MagicMock) -> None:
        """client.execute('group', 'cmd') works."""
        handler = MagicMock(return_value={"ok": True})
        spec = _make_spec("list", group="invoice", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = _loaded()
        client = GraftpunkClient("testsite")
        result = client.execute("invoice", "list")
        assert result.data == {"ok": True}


# ---------------------------------------------------------------------------
# Token injection
# ---------------------------------------------------------------------------


class TestTokenInjection:
    """Tests for token injection in the execution pipeline."""

    @patch("graftpunk.client.prepare_session")
    @patch("graftpunk.client.load_session_for_api_resolved")
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
        mock_load.return_value = _loaded()
        client = GraftpunkClient("testsite")
        client.fetch()
        mock_prep.assert_called_once_with(mock_load.return_value[0], token_config, "https://ex.com")

    @patch("graftpunk.client.prepare_session")
    @patch("graftpunk.client.load_session_for_api_resolved")
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
        mock_load.return_value = _loaded()
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
    @patch("graftpunk.client.load_session_for_api_resolved")
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
        mock_load.return_value = _loaded()
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
    @patch("graftpunk.client.load_session_for_api_resolved")
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
        mock_load.return_value = _loaded()
        client = GraftpunkClient("testsite")
        client.login()
        mock_update.assert_called_once_with(mock_load.return_value[0], "testsite")

    @patch("graftpunk.client.update_session_cookies")
    @patch("graftpunk.client.load_session_for_api_resolved")
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
        mock_load.return_value = _loaded()
        client = GraftpunkClient("testsite")
        client.fetch()
        mock_update.assert_not_called()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Tests for error propagation from the execution pipeline."""

    @patch("graftpunk.client.load_session_for_api_resolved")
    @patch("graftpunk.client.get_plugin")
    def test_command_error_propagates(self, mock_get: MagicMock, mock_load: MagicMock) -> None:
        """CommandError from handler propagates to caller."""
        handler = MagicMock(side_effect=CommandError("bad input"))
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])
        mock_load.return_value = _loaded()
        client = GraftpunkClient("testsite")
        with pytest.raises(CommandError, match="bad input"):
            client.fetch()

    @patch("graftpunk.client.load_session_for_api_resolved")
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
    @patch("graftpunk.client.load_session_for_api_resolved")
    @patch("graftpunk.client.get_plugin")
    def test_retries_on_transient_failure(
        self, mock_get: MagicMock, mock_load: MagicMock, mock_time: MagicMock
    ) -> None:
        """Handler succeeds on retry after transient failure."""
        mock_time.monotonic.return_value = 0.0
        mock_load.return_value = _loaded()
        handler = MagicMock(side_effect=[requests.ConnectionError("reset"), {"ok": True}])
        spec = _make_spec("fetch", handler=handler, max_retries=1)
        mock_get.return_value = _make_plugin(commands=[spec])

        client = GraftpunkClient("testsite")
        result = client.fetch()
        assert result.data == {"ok": True}
        assert handler.call_count == 2
        mock_time.sleep.assert_called_once_with(1)  # 2**0

    @patch("graftpunk.client.time")
    @patch("graftpunk.client.load_session_for_api_resolved")
    @patch("graftpunk.client.get_plugin")
    def test_raises_after_retries_exhausted(
        self, mock_get: MagicMock, mock_load: MagicMock, mock_time: MagicMock
    ) -> None:
        """Raises last exception when all retries exhausted."""
        mock_time.monotonic.return_value = 0.0
        mock_load.return_value = _loaded()
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

    @patch("graftpunk.client.load_session_for_api_resolved")
    @patch("graftpunk.client.get_plugin")
    def test_non_retryable_error_propagates_immediately(
        self, mock_get: MagicMock, mock_load: MagicMock
    ) -> None:
        """ValueError is not retried."""
        mock_load.return_value = _loaded()
        handler = MagicMock(side_effect=ValueError("bad"))
        spec = _make_spec("fetch", handler=handler, max_retries=3)
        mock_get.return_value = _make_plugin(commands=[spec])

        client = GraftpunkClient("testsite")
        with pytest.raises(ValueError, match="bad"):
            client.fetch()
        assert handler.call_count == 1

    @patch("graftpunk.client.load_session_for_api_resolved")
    @patch("graftpunk.client.get_plugin")
    def test_403_without_tokens_propagates(self, mock_get: MagicMock, mock_load: MagicMock) -> None:
        """403 HTTPError without token_config is not retried."""
        mock_load.return_value = _loaded()
        response_403 = MagicMock()
        response_403.status_code = 403
        handler = MagicMock(side_effect=requests.exceptions.HTTPError(response=response_403))
        spec = _make_spec("fetch", handler=handler)
        mock_get.return_value = _make_plugin(commands=[spec])  # no token_config

        client = GraftpunkClient("testsite")
        with pytest.raises(requests.exceptions.HTTPError):
            client.fetch()
        assert handler.call_count == 1

    @patch("graftpunk.client.load_session_for_api_resolved")
    @patch("graftpunk.client.get_plugin")
    def test_500_with_tokens_propagates(self, mock_get: MagicMock, mock_load: MagicMock) -> None:
        """500 HTTPError propagates even when token_config is set."""
        mock_load.return_value = _loaded()
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
    @patch("graftpunk.client.load_session_for_api_resolved")
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
        mock_load.return_value = _loaded()
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
        """close() calls update_session_cookies when session is dirty.

        A loaded session means the name was resolved with it (#181), so the
        state set up here is the post-load one: the write-back keys off the
        slot the load reported, never the plugin's bare base.
        """
        mock_get.return_value = _make_plugin(commands=[])
        client = GraftpunkClient("testsite")
        mock_session = MagicMock(spec=requests.Session)
        client._session = mock_session
        client._session_name = "testsite@alice"
        client._session_dirty = True
        client.close()
        mock_update.assert_called_once_with(mock_session, "testsite@alice")

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
    @patch("graftpunk.client.load_session_for_api_resolved")
    @patch("graftpunk.client.get_plugin")
    def test_ctx_dirty_sets_self_dirty_before_persist(
        self, mock_get: MagicMock, mock_load: MagicMock, mock_update: MagicMock
    ) -> None:
        """If persist fails mid-pipeline, close() retries because self is dirty."""
        mock_load.return_value = _loaded()

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
    @patch("graftpunk.client.load_session_for_api_resolved")
    @patch("graftpunk.client.get_plugin")
    def test_ctx_dirty_resets_after_successful_persist(
        self, mock_get: MagicMock, mock_load: MagicMock, mock_update: MagicMock
    ) -> None:
        """Successful persist resets self._session_dirty to False."""
        mock_load.return_value = _loaded()

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

    @patch("graftpunk.client.load_session_for_api_resolved")
    @patch("graftpunk.client.get_plugin")
    def test_none_falls_back_to_plugin_default(
        self, mock_get: MagicMock, mock_load: MagicMock
    ) -> None:
        """requires_session=None uses plugin.requires_session (True)."""
        handler = MagicMock(return_value={})
        spec = _make_spec("fetch", handler=handler, requires_session=None)
        mock_get.return_value = _make_plugin(commands=[spec], requires_session=True)
        mock_load.return_value = _loaded()
        client = GraftpunkClient("testsite")
        client.fetch()
        mock_load.assert_called_once()

    @patch("graftpunk.client.load_session_for_api_resolved")
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

    @patch("graftpunk.client.load_session_for_api_resolved")
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
        mock_load.return_value = _loaded(session)
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


class TestKebabCaseNamesThroughTheClient:
    """A real @command method reaches the client under both spellings (#147)."""

    def _plugin(self):  # noqa: ANN202
        from graftpunk.plugins.cli_plugin import SitePlugin, command

        class KebabPlugin(SitePlugin):
            site_name = "kebab"
            base_url = "https://kebab.example.com"

            @command(help="multi-word")
            def by_parcel(self, ctx):  # noqa: ANN001, ANN202
                return {"ok": True}

        return KebabPlugin()

    def test_both_spellings_resolve(self) -> None:
        plugin = self._plugin()
        real_specs = plugin.get_commands()  # by-parcel, via the real @command decorator
        grouped = _make_spec(name="list-recent", group="account-statements")
        plugin.get_commands = lambda: [*real_specs, grouped]  # type: ignore[method-assign]
        with patch("graftpunk.client.get_plugin", return_value=plugin):
            client = GraftpunkClient("kebab")
        assert client._resolve_command("by_parcel").name == "by-parcel"
        assert client._resolve_command("by-parcel").name == "by-parcel"
        assert client.by_parcel._spec.name == "by-parcel"
        assert client._resolve_command("account_statements", "list_recent") is grouped
        assert client.account_statements.list_recent._spec is grouped

    def test_kebab_collision_is_an_error(self) -> None:
        from graftpunk.exceptions import PluginError
        from graftpunk.plugins.cli_plugin import SitePlugin, command

        class Colliding(SitePlugin):
            site_name = "collide"
            base_url = "https://collide.example.com"

            @command(help="a")
            def get_data(self, ctx):  # noqa: ANN001, ANN202
                return {}

            @command(help="b")
            def getData(self, ctx):  # noqa: ANN001, ANN202, N802
                return {}

        with (
            patch("graftpunk.client.get_plugin", return_value=Colliding()),
            pytest.raises(PluginError, match="twice"),
        ):
            GraftpunkClient("collide")


class TestClientOperatingSession:
    """The operating session name: pinned, or resolved on FIRST USE (#181).

    Construction performs no session-storage I/O and never raises for session
    STATE reasons; every resolution happens at the lazy load in the first
    command that needs a session. An illegal pin *string* is the exception:
    pure policy, no I/O, refused immediately.
    """

    def test_pin_is_stored_without_listing(self) -> None:
        plugin = _make_plugin(
            site_name="fmtsite",
            session_name="myshop",
            commands=[_make_spec("items", requires_session=True)],
        )
        with (
            patch("graftpunk.client.get_plugin", return_value=plugin),
            patch("graftpunk.client.list_sessions") as mock_list,
        ):
            client = GraftpunkClient("fmtsite", session="myshop@bob")
        assert client._session_name == "myshop@bob"
        mock_list.assert_not_called()  # a pinned client pays no listing

    def test_illegal_pin_raises_value_error_at_construction(self) -> None:
        """A pin that cannot name a session is refused immediately.

        Validating the pin STRING is pure policy — no listing, no load — so it
        stays in ``__init__`` even though every session-STATE error (ambiguity,
        not-found, expiry) moved to first use (#181). A typo fails where it was
        typed rather than one command later.
        """
        plugin = _make_plugin(
            site_name="fmtsite",
            session_name="myshop",
            commands=[_make_spec("items", requires_session=True)],
        )
        for bad in ("MyShop@Alice", "../../x", "my.shop", "myshop@"):
            with (
                patch("graftpunk.client.get_plugin", return_value=plugin),
                patch("graftpunk.client.list_sessions") as mock_list,
                patch("graftpunk.client.load_session_for_api_resolved") as mock_load,
                pytest.raises(ValueError),
            ):
                GraftpunkClient("fmtsite", session=bad)
            mock_list.assert_not_called()
            mock_load.assert_not_called()

    def test_sessionless_plugin_never_lists_or_loads(self) -> None:
        """requires_session=False has nothing to resolve — and nothing to list.

        Not at construction, and not at the sessionless command either: the
        lazy path is never reached, so no listing and no load, ever.
        """
        plugin = _make_plugin(
            site_name="fmtsite",
            session_name="myshop",
            requires_session=False,
            commands=[_make_spec("items", requires_session=False)],
        )
        with (
            patch("graftpunk.client.get_plugin", return_value=plugin),
            patch("graftpunk.client.list_sessions") as mock_list,
            patch("graftpunk.client.load_session_for_api_resolved") as mock_load,
        ):
            client = GraftpunkClient("fmtsite")
            assert client._session_name == "myshop"
            mock_list.assert_not_called()
            client.execute("items")
            mock_list.assert_not_called()
            mock_load.assert_not_called()

    def test_unpinned_resolves_on_first_use_ignoring_ambient(self, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.setenv("GRAFTPUNK_SESSION", "myshop@env")
        plugin = _make_plugin(
            site_name="fmtsite",
            session_name="myshop",
            commands=[_make_spec("items", requires_session=True)],
        )
        with (
            patch("graftpunk.client.get_plugin", return_value=plugin),
            patch("graftpunk.client.list_sessions", return_value=["myshop@alice"]) as mock_list,
            patch(
                "graftpunk.client.load_session_for_api_resolved",
                return_value=_loaded(name="myshop@alice"),
            ) as mock_load,
        ):
            client = GraftpunkClient("fmtsite")
            mock_list.assert_not_called()  # nothing is resolved at construction
            assert client._session_name == "myshop"
            client.execute("items")
            # Resolved against the listing here, so the load is exact-only.
            mock_load.assert_called_once_with("myshop@alice", resolve=False)
        assert client._session_name == "myshop@alice"  # env deliberately ignored

    def test_unpinned_ambiguity_surfaces_at_first_use_not_construction(
        self,
        fresh_backend,  # noqa: ANN001
    ) -> None:
        """Two accounts cached, unpinned: construction is silent (#181).

        The raise moves into the first ``execute()`` — the call a consumer
        already wraps — and construction lists nothing at all.
        """
        from graftpunk.cache import list_sessions as real_list_sessions
        from graftpunk.exceptions import AmbiguousSessionError

        _cache_account("myshop@alice", "alice@example.com")
        _cache_account("myshop@bob", "bob@example.com")

        plugin = _make_plugin(
            site_name="fmtsite",
            session_name="myshop",
            commands=[_make_spec("items", requires_session=True)],
        )
        spy = MagicMock(wraps=real_list_sessions)
        with (
            patch("graftpunk.client.get_plugin", return_value=plugin),
            patch("graftpunk.client.list_sessions", spy),
        ):
            client = GraftpunkClient("fmtsite")  # must not raise
            spy.assert_not_called()
            with pytest.raises(AmbiguousSessionError) as excinfo:
                client.execute("items")
        message = str(excinfo.value)
        assert "myshop@alice" in message
        assert "myshop@bob" in message

    def test_unpinned_lists_once_at_first_use_and_the_save_follows_it(
        self,
        fresh_backend,  # noqa: ANN001
    ) -> None:
        """One account cached: the first command resolves it, later ones reuse it.

        The real local backend, so the write-back is a real re-cache: it must
        land on ``myshop@alice``, never on a bare ``myshop`` slot.
        """
        from graftpunk.cache import get_session_metadata, list_sessions
        from graftpunk.cache import list_sessions as real_list_sessions

        _cache_account("myshop@alice", "alice@example.com")

        seen: list[str] = []

        def handler(ctx: CommandContext, **_kw: Any) -> dict[str, bool]:
            seen.append(ctx._session_name)
            ctx.session.cookies.set("visited", "1")
            return {"ok": True}

        plugin = _make_plugin(
            site_name="fmtsite",
            session_name="myshop",
            commands=[
                _make_spec("items", handler=handler, requires_session=True, saves_session=True)
            ],
        )
        spy = MagicMock(wraps=real_list_sessions)
        with (
            patch("graftpunk.client.get_plugin", return_value=plugin),
            patch("graftpunk.client.list_sessions", spy),
        ):
            with GraftpunkClient("fmtsite") as client:
                assert spy.call_count == 0
                client.execute("items")
                assert spy.call_count == 1
                assert client._session_name == "myshop@alice"
                client.execute("items")
                assert spy.call_count == 1  # resolved once, reused after
            assert seen == ["myshop@alice", "myshop@alice"]

        refreshed = get_session_metadata("myshop@alice")
        assert refreshed["cookie_count"] == 1
        assert refreshed["account_identifier"] == "alice@example.com"
        assert "myshop" not in list_sessions()

    def test_bare_pin_resolves_at_load_and_the_refresh_follows_it(self, fresh_backend) -> None:  # noqa: ANN001
        """A bare pin names a base (#182): both write-backs key off what loaded.

        The real local backend, so the refresh is a real re-cache: only
        ``myshop@alice`` is cached, and keying the save off the bare "myshop"
        the caller pinned drops it silently (nothing is cached under that name).
        """
        from graftpunk.cache import get_session_metadata, list_sessions
        from graftpunk.cache import list_sessions as real_list_sessions

        _cache_account("myshop@alice", "alice@example.com")

        def handler(ctx: CommandContext, **_kw: Any) -> dict[str, bool]:
            ctx.session.cookies.set("visited", "1")
            ctx.save_session()
            return {"ok": True}

        plugin = _make_plugin(
            site_name="fmtsite",
            session_name="myshop",
            commands=[_make_spec("items", handler=handler, requires_session=True)],
        )
        spy = MagicMock(wraps=real_list_sessions)
        with (
            patch("graftpunk.client.get_plugin", return_value=plugin),
            patch("graftpunk.client.list_sessions", spy),
        ):
            with GraftpunkClient("fmtsite", session="myshop") as client:
                spy.assert_not_called()  # a pin resolves at the load, not here
                client.execute("items")
            assert client._session_name == "myshop@alice"

        refreshed = get_session_metadata("myshop@alice")
        assert refreshed["cookie_count"] == 1
        assert refreshed["account_identifier"] == "alice@example.com"
        assert "myshop" not in list_sessions()

    def test_labelled_pin_lists_nowhere(self, fresh_backend) -> None:  # noqa: ANN001
        """A labelled pin is exact: an existing slot is an exact hit.

        No listing at construction (nothing is resolved there) and none at the
        load either (the exact slot answers first).
        """
        from graftpunk.cache import list_sessions as real_list_sessions

        _cache_account("myshop@alice", "alice@example.com")

        plugin = _make_plugin(
            site_name="fmtsite",
            session_name="myshop",
            commands=[_make_spec("items", requires_session=True)],
        )
        client_spy = MagicMock(wraps=real_list_sessions)
        cache_spy = MagicMock(wraps=real_list_sessions)
        with (
            patch("graftpunk.client.get_plugin", return_value=plugin),
            patch("graftpunk.client.list_sessions", client_spy),
            patch("graftpunk.cache.list_sessions", cache_spy),
        ):
            with GraftpunkClient("fmtsite", session="myshop@alice") as client:
                client.execute("items")
            assert client._session_name == "myshop@alice"
            client_spy.assert_not_called()
            cache_spy.assert_not_called()

    def test_labelled_pin_that_misses_is_not_found_and_still_lists_nowhere(
        self,
        fresh_backend,  # noqa: ANN001
    ) -> None:
        """A labelled pin is exact, so a miss is a miss — never ambiguity.

        And it costs no listing on the way there: the bare-base fallback is
        keyed on the BASE, which a labelled miss can never match, so resolving
        one could only list uselessly. Two accounts are cached precisely to
        show the fallback is not consulted.
        """
        from graftpunk.cache import list_sessions as real_list_sessions

        _cache_account("myshop@alice", "alice@example.com")
        _cache_account("myshop@bob", "bob@example.com")

        plugin = _make_plugin(
            site_name="fmtsite",
            session_name="myshop",
            commands=[_make_spec("items", requires_session=True)],
        )
        client_spy = MagicMock(wraps=real_list_sessions)
        cache_spy = MagicMock(wraps=real_list_sessions)
        with (
            patch("graftpunk.client.get_plugin", return_value=plugin),
            patch("graftpunk.client.list_sessions", client_spy),
            patch("graftpunk.cache.list_sessions", cache_spy),
        ):
            client = GraftpunkClient("fmtsite", session="myshop@nobody")  # silent
            with pytest.raises(SessionNotFoundError):
                client.execute("items")
            client_spy.assert_not_called()
            cache_spy.assert_not_called()

    def test_command_override_on_a_sessionless_plugin_resolves_at_load(
        self,
        fresh_backend,  # noqa: ANN001
    ) -> None:
        """A requires_session=True command on a requires_session=False plugin.

        Nothing is pinned, so the lazy path resolves the plugin's base name
        exactly as it would for any unpinned client — and only here, at the
        first command that actually asks for a session (#181, #182).
        """
        from graftpunk.cache import get_session_metadata, list_sessions

        _cache_account("myshop@alice", "alice@example.com")

        def handler(ctx: CommandContext, **_kw: Any) -> dict[str, bool]:
            ctx.session.cookies.set("visited", "1")
            ctx.save_session()
            return {"ok": True}

        plugin = _make_plugin(
            site_name="fmtsite",
            session_name="myshop",
            requires_session=False,
            commands=[_make_spec("items", handler=handler, requires_session=True)],
        )
        with patch("graftpunk.client.get_plugin", return_value=plugin):
            with GraftpunkClient("fmtsite") as client:
                client.execute("items")
            assert client._session_name == "myshop@alice"

        refreshed = get_session_metadata("myshop@alice")
        assert refreshed["cookie_count"] == 1
        assert refreshed["account_identifier"] == "alice@example.com"
        assert "myshop" not in list_sessions()

    def test_close_on_a_never_used_client_lists_nothing_and_loads_nothing(
        self,
        fresh_backend,  # noqa: ANN001
    ) -> None:
        """A client that ran no command has no session to write back.

        close() must not become the resolution that moved out of __init__:
        with two accounts cached it stays silent rather than raising.
        """
        from graftpunk.cache import list_sessions as real_list_sessions

        _cache_account("myshop@alice", "alice@example.com")
        _cache_account("myshop@bob", "bob@example.com")

        plugin = _make_plugin(
            site_name="fmtsite",
            session_name="myshop",
            commands=[_make_spec("items", requires_session=True)],
        )
        client_spy = MagicMock(wraps=real_list_sessions)
        cache_spy = MagicMock(wraps=real_list_sessions)
        with (
            patch("graftpunk.client.get_plugin", return_value=plugin),
            patch("graftpunk.client.list_sessions", client_spy),
            patch("graftpunk.cache.list_sessions", cache_spy),
            patch("graftpunk.client.load_session_for_api_resolved") as mock_load,
        ):
            with GraftpunkClient("fmtsite"):
                pass  # constructed, never executed
            client_spy.assert_not_called()
            cache_spy.assert_not_called()
            mock_load.assert_not_called()

    def test_close_persists_onto_the_slot_the_load_resolved(
        self,
        fresh_backend,  # noqa: ANN001
    ) -> None:
        """The close() write-back keys off the resolved slot, not the base.

        The handler dirties the session without saving it; the client is
        marked dirty afterwards, so the only write is the one in close().
        """
        from graftpunk.cache import get_session_metadata, list_sessions

        _cache_account("myshop@alice", "alice@example.com")

        def handler(ctx: CommandContext, **_kw: Any) -> dict[str, bool]:
            ctx.session.cookies.set("visited", "1")  # dirtied, never saved
            return {"ok": True}

        plugin = _make_plugin(
            site_name="fmtsite",
            session_name="myshop",
            commands=[_make_spec("items", handler=handler, requires_session=True)],
        )
        with patch("graftpunk.client.get_plugin", return_value=plugin):
            with GraftpunkClient("fmtsite") as client:
                client.execute("items")
                assert get_session_metadata("myshop@alice")["cookie_count"] == 0
                client._session_dirty = True
            assert client._session_name == "myshop@alice"

        refreshed = get_session_metadata("myshop@alice")
        assert refreshed["cookie_count"] == 1
        assert refreshed["account_identifier"] == "alice@example.com"
        assert "myshop" not in list_sessions()

    def test_ambiguity_repeats_on_every_call_and_a_pin_recovers(
        self,
        fresh_backend,  # noqa: ANN001
    ) -> None:
        """Nothing is cached from a failed resolution, and pinning fixes it.

        The unpinned client raises identically on the second call (it did not
        half-resolve into some slot), and a client pinned to one of the two
        accounts runs and writes back to exactly that account.
        """
        from graftpunk.cache import get_session_metadata, list_sessions
        from graftpunk.exceptions import AmbiguousSessionError

        _cache_account("myshop@alice", "alice@example.com")
        _cache_account("myshop@bob", "bob@example.com")

        def handler(ctx: CommandContext, **_kw: Any) -> dict[str, bool]:
            ctx.session.cookies.set("visited", "1")
            ctx.save_session()
            return {"ok": True}

        plugin = _make_plugin(
            site_name="fmtsite",
            session_name="myshop",
            commands=[_make_spec("items", handler=handler, requires_session=True)],
        )
        with patch("graftpunk.client.get_plugin", return_value=plugin):
            client = GraftpunkClient("fmtsite")
            for _ in range(2):
                with pytest.raises(AmbiguousSessionError) as excinfo:
                    client.execute("items")
                assert sorted(excinfo.value.candidates) == ["myshop@alice", "myshop@bob"]
                assert client._session_name == "myshop"  # unchanged by the failure

            with GraftpunkClient("fmtsite", session="myshop@bob") as pinned:
                pinned.execute("items")
                assert pinned._session_name == "myshop@bob"

        assert get_session_metadata("myshop@bob")["cookie_count"] == 1
        assert get_session_metadata("myshop@alice")["cookie_count"] == 0
        assert "myshop" not in list_sessions()
