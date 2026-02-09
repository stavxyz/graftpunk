"""First-class Python API for executing plugin commands.

Provides ``GraftpunkClient`` -- a stateful, context-manager-friendly
client that wraps a single plugin.  Commands are accessible via
attribute access (``client.login()``) or string dispatch
(``client.execute("login")``).

The module-level ``execute_plugin_command()`` function encapsulates the
shared execution core (handler execution with retry / rate-limit and
result normalization) used by the CLI callback.  Callers handle
session loading, token injection, 403 retry, and session persistence.

Example::

    from graftpunk.client import GraftpunkClient

    with GraftpunkClient("mysite") as client:
        result = client.login(username="alice")
        invoices = client.invoice.list(status="open")
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import requests

from graftpunk.cache import load_session_for_api, update_session_cookies
from graftpunk.logging import get_logger
from graftpunk.observe import NoOpObservabilityContext
from graftpunk.plugins import get_plugin
from graftpunk.plugins.cli_plugin import (
    CLIPluginProtocol,
    CommandContext,
    CommandResult,
    CommandSpec,
)
from graftpunk.tokens import clear_cached_tokens, prepare_session

LOG = get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level rate-limit state used by execute_plugin_command()
# ---------------------------------------------------------------------------

_rate_limit_state: dict[str, float] = {}


def _enforce_shared_rate_limit(
    command_key: str,
    rate_limit: float,
    state: dict[str, float],
) -> None:
    """Enforce a minimum interval between command executions.

    Args:
        command_key: Unique key for the command.
        rate_limit: Minimum seconds between executions.
        state: Mutable dict tracking last-execution timestamps.
    """
    now = time.monotonic()
    last = state.get(command_key)
    if last is not None:
        elapsed = now - last
        if elapsed < rate_limit:
            time.sleep(rate_limit - elapsed)
    state[command_key] = time.monotonic()


def _run_handler_with_limits(
    handler: Any,
    ctx: CommandContext,
    spec: CommandSpec,
    rate_limit_state: dict[str, float],
    **kwargs: Any,
) -> Any:
    """Execute handler with retry and rate-limit support.

    Retries the handler up to ``spec.max_retries`` times with
    exponential backoff on transient failures.

    Args:
        handler: The command handler callable.
        ctx: CommandContext to pass to the handler.
        spec: CommandSpec with retry/rate-limit configuration.
        rate_limit_state: Mutable dict tracking last-execution times.
        **kwargs: Additional keyword arguments for the handler.

    Returns:
        The handler's return value.

    Raises:
        Exception: The last exception if all attempts fail.
    """
    attempts = 1 + spec.max_retries
    last_exc: Exception | None = None
    command_key = f"{ctx.plugin_name}.{spec.name}"

    for attempt in range(attempts):
        try:
            if spec.rate_limit:
                _enforce_shared_rate_limit(
                    command_key,
                    spec.rate_limit,
                    rate_limit_state,
                )
            result = handler(ctx, **kwargs)
            if asyncio.iscoroutine(result):
                LOG.warning(
                    "async_handler_auto_executed",
                    command=spec.name,
                    plugin=ctx.plugin_name,
                )
                result = asyncio.run(result)
            return result
        except (
            requests.RequestException,
            ConnectionError,
            TimeoutError,
            OSError,
        ) as exc:
            last_exc = exc
            if attempt < attempts - 1:
                backoff = 2**attempt
                LOG.warning(
                    "command_retry",
                    command=spec.name,
                    attempt=attempt + 1,
                    backoff=backoff,
                )
                time.sleep(backoff)

    assert last_exc is not None  # for type narrowing
    raise last_exc


def execute_plugin_command(
    spec: CommandSpec,
    ctx: CommandContext,
    *,
    rate_limit_state: dict[str, float] | None = None,
    **kwargs: Any,
) -> CommandResult:
    """Shared execution pipeline for plugin commands.

    Executes the handler from *spec* with retry / rate-limit
    support and normalises the return value to a
    ``CommandResult``.

    Token injection, 403 token refresh, and session
    persistence are the **caller's** responsibility so that
    each caller (CLI, Python client) can use its own error
    handling and mock targets.

    Args:
        spec: The resolved command specification.
        ctx: Pre-built ``CommandContext``.
        rate_limit_state: Mutable dict for rate-limit
            tracking.  Defaults to a module-level dict.
        **kwargs: Arguments forwarded to the handler.

    Returns:
        A ``CommandResult`` wrapping the handler's return
        value.
    """
    rl_state = rate_limit_state if rate_limit_state is not None else _rate_limit_state

    # Execute with retry / rate-limit
    result = _run_handler_with_limits(
        spec.handler,
        ctx,
        spec,
        rl_state,
        **kwargs,
    )

    # Normalise to CommandResult
    if isinstance(result, CommandResult):
        return result
    return CommandResult(data=result)


class GraftpunkClient:
    """Stateful client for a single plugin.

    Builds a command hierarchy on init from the plugin's
    ``get_commands()`` list.  Top-level commands become
    ``_CommandCallable`` objects; grouped commands are nested
    under ``_GroupProxy`` objects.

    Args:
        plugin_name: The ``site_name`` of the plugin to load.
    """

    def __init__(self, plugin_name: str) -> None:
        self._plugin: CLIPluginProtocol = get_plugin(plugin_name)
        self._session = None
        self._session_dirty = False
        self._last_execution: dict[str, float] = {}

        # Build command hierarchy
        self._top_commands: dict[str, CommandSpec] = {}
        self._groups: dict[str, dict[str, CommandSpec]] = {}

        for spec in self._plugin.get_commands():
            if spec.group is None:
                self._top_commands[spec.name] = spec
            else:
                self._groups.setdefault(spec.group, {})[spec.name] = spec

    # -- attribute-based dispatch ------------------------------------------

    def __getattr__(self, name: str) -> _GroupProxy | _CommandCallable:
        """Return a proxy for a command group or a callable for a command.

        Raises:
            AttributeError: If *name* is not a known command or group.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._top_commands:
            return _CommandCallable(self, self._top_commands[name])
        if name in self._groups:
            return _GroupProxy(self, self._groups[name])
        raise AttributeError(f"Plugin '{self._plugin.site_name}' has no command or group '{name}'")

    # -- string dispatch ---------------------------------------------------

    def execute(self, *args: str, **kwargs: Any) -> CommandResult:
        """Execute a command by name.

        Accepts one positional argument for a top-level command or two
        for a grouped command (group, command).

        Args:
            *args: Command path -- ``("login",)`` or ``("invoice", "list")``.
            **kwargs: Keyword arguments forwarded to the command handler.

        Returns:
            The ``CommandResult`` from the handler.
        """
        spec = self._resolve_command(*args)
        return self._execute_command(spec, **kwargs)

    def _resolve_command(self, *args: str) -> CommandSpec:
        """Resolve positional args to a ``CommandSpec``.

        Args:
            *args: One arg (top-level) or two args (group, command).

        Returns:
            The matching ``CommandSpec``.

        Raises:
            AttributeError: If the command or group is unknown.
            ValueError: If the wrong number of args is provided.
        """
        if len(args) == 1:
            name = args[0]
            if name in self._top_commands:
                return self._top_commands[name]
            raise AttributeError(f"Plugin '{self._plugin.site_name}' has no command '{name}'")
        if len(args) == 2:
            group_name, cmd_name = args
            group = self._groups.get(group_name)
            if group is None:
                raise AttributeError(
                    f"Plugin '{self._plugin.site_name}' has no group '{group_name}'"
                )
            spec = group.get(cmd_name)
            if spec is None:
                raise AttributeError(f"Group '{group_name}' has no command '{cmd_name}'")
            return spec
        raise ValueError("execute() takes 1 arg (command) or 2 args (group, command)")

    # -- execution ---------------------------------------------------------

    def _execute_command(self, spec: CommandSpec, **kwargs: Any) -> CommandResult:
        """Execute a resolved command through the full pipeline.

        Pipeline steps:
        1. Resolve whether a session is needed.
        2. Lazy-load session via ``load_session_for_api`` if needed.
        3. Inject tokens via ``prepare_session`` if plugin has ``token_config``.
        4. Build ``CommandContext``.
        5. Run handler with retry/rate-limit via ``_execute_with_limits``.
        6. On 403 + token_config: clear tokens, re-prepare, retry once.
        7. Persist session if dirty or ``spec.saves_session``.
        8. Normalize return to ``CommandResult``.

        Args:
            spec: The resolved command specification.
            **kwargs: Arguments forwarded to the handler.

        Returns:
            A ``CommandResult`` wrapping the handler's return value.
        """
        plugin = self._plugin
        needs_session = (
            spec.requires_session if spec.requires_session is not None else plugin.requires_session
        )

        # Step 1 — lazy-load session
        if needs_session and self._session is None:
            self._session = load_session_for_api(plugin.session_name)
            base_url = getattr(plugin, "base_url", "")
            if base_url and hasattr(self._session, "gp_base_url"):
                setattr(self._session, "gp_base_url", base_url)  # noqa: B010

        session = self._session if needs_session else requests.Session()

        # Step 2 — token injection
        token_config = getattr(plugin, "token_config", None)
        if token_config is not None and needs_session:
            base_url = getattr(plugin, "base_url", "")
            prepare_session(session, token_config, base_url)

        # Step 3 — build CommandContext
        ctx = CommandContext(
            session=session,
            plugin_name=plugin.site_name,
            command_name=spec.name,
            api_version=plugin.api_version,
            base_url=getattr(plugin, "base_url", ""),
            config=getattr(plugin, "_plugin_config", None),
            observe=NoOpObservabilityContext(),
            _session_name=(plugin.session_name if needs_session else ""),
        )

        # Step 4 — execute with retry/rate-limit
        try:
            result = _run_handler_with_limits(
                spec.handler, ctx, spec, self._last_execution, **kwargs
            )
        except requests.exceptions.HTTPError as exc:
            if (
                exc.response is not None
                and exc.response.status_code == 403
                and token_config is not None
            ):
                LOG.info(
                    "token_403_retry",
                    command=spec.name,
                    url=(exc.response.url if exc.response else "unknown"),
                )
                clear_cached_tokens(session)
                base_url = getattr(plugin, "base_url", "")
                prepare_session(session, token_config, base_url)
                self._session_dirty = True
                result = _run_handler_with_limits(
                    spec.handler, ctx, spec, self._last_execution, **kwargs
                )
            else:
                raise

        # Step 5 — persist session if dirty
        if (spec.saves_session or ctx._session_dirty or self._session_dirty) and needs_session:
            update_session_cookies(session, plugin.session_name)
            self._session_dirty = False

        # Step 6 — normalize to CommandResult
        if isinstance(result, CommandResult):
            return result
        return CommandResult(data=result)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Persist dirty session and tear down the plugin."""
        if self._session is not None and self._session_dirty:
            update_session_cookies(self._session, self._plugin.session_name)
            self._session_dirty = False
        try:
            self._plugin.teardown()
        except Exception:  # noqa: BLE001
            LOG.debug("plugin_teardown_error", exc_info=True)

    def __enter__(self) -> GraftpunkClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _GroupProxy:
    """Proxy for a command group.

    Attribute access returns a ``_CommandCallable`` for the named
    sub-command.
    """

    __slots__ = ("_client", "_commands")

    def __init__(
        self,
        client: GraftpunkClient,
        commands: dict[str, CommandSpec],
    ) -> None:
        self._client = client
        self._commands = commands

    def __getattr__(self, name: str) -> _CommandCallable:
        """Return a callable for *name*.

        Raises:
            AttributeError: If *name* is not a command in this group.
        """
        spec = self._commands.get(name)
        if spec is None:
            raise AttributeError(
                f"Group has no command '{name}'. Available: {', '.join(sorted(self._commands))}"
            )
        return _CommandCallable(self._client, spec)


class _CommandCallable:
    """A bound command ready to call.

    Calling an instance delegates to
    ``GraftpunkClient._execute_command``.
    """

    __slots__ = ("_client", "_spec")

    def __init__(self, client: GraftpunkClient, spec: CommandSpec) -> None:
        self._client = client
        self._spec = spec

    def __call__(self, **kwargs: Any) -> CommandResult:
        """Execute the command with the given keyword arguments."""
        return self._client._execute_command(self._spec, **kwargs)
