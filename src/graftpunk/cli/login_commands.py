"""Login command generation for plugins.

Creates auto-generated 'login' CLI commands from plugin login() methods
or declarative LoginConfig.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Callable
from typing import Any

import click
import typer.core
from rich.status import Status

from graftpunk import console as gp_console
from graftpunk.logging import get_logger
from graftpunk.plugins.cli_plugin import (
    CLIPluginProtocol,
    LoginConfig,
    has_declarative_login,
)
from graftpunk.plugins.login_engine import generate_login_method

LOG = get_logger(__name__)


def has_login_method(plugin: CLIPluginProtocol) -> bool:
    """Check if plugin has a login method that's not a CLI command.

    Returns True if the plugin has a 'login' attribute that is callable
    and NOT decorated with @command (i.e., not already exposed as CLI).
    After the login_config rename, 'login' on a plugin is only ever a method.
    """
    login_attr = getattr(plugin, "login", None)
    if not callable(login_attr):
        return False
    # Skip if already decorated as a CLI command
    return not hasattr(login_attr, "_command_meta")


def resolve_login_callable(plugin: CLIPluginProtocol) -> Callable[..., Any] | None:
    """Return the login callable for a plugin, or None if login is not available.

    Checks two sources in order:
    1. A user-defined ``login()`` method (not decorated as a CLI command).
    2. A declarative ``login_config`` that can generate a login method.

    Args:
        plugin: Plugin instance to inspect.

    Returns:
        A callable that accepts a credentials dict, or None.
    """
    if has_login_method(plugin):
        return plugin.login  # type: ignore[union-attr]
    if has_declarative_login(plugin):
        login_func = generate_login_method(plugin)  # type: ignore[arg-type]
        LOG.debug("declarative_login_generated", plugin=plugin.site_name)
        return login_func
    return None


def resolve_login_fields(plugin: CLIPluginProtocol) -> dict[str, str]:
    """Return the login credential fields for a plugin.

    Uses ``login_config.fields`` if available, otherwise defaults
    to ``{"username": "", "password": ""}``.

    Args:
        plugin: Plugin instance to inspect.

    Returns:
        Dictionary of field names to default values.
    """
    login_cfg = getattr(plugin, "login_config", None)
    if isinstance(login_cfg, LoginConfig) and login_cfg.fields:
        return login_cfg.fields
    LOG.info(
        "login_fields_default_assumed",
        plugin=plugin.site_name,
        hint="No login fields configured. Defaulting to username/password.",
    )
    return {"username": "", "password": ""}


def create_login_command(
    plugin: CLIPluginProtocol,
    login_callable: Callable[..., Any],
    fields: dict[str, str],
) -> typer.core.TyperCommand:
    """Create an auto-generated login command for plugins with login() method.

    Generates a 'login' CLI command that dynamically prompts for credentials
    based on the plugin's ``LoginConfig.fields``. Credential resolution order:

    1. Environment variables ({SITE_PREFIX}_{FIELD_NAME} or plugin-level overrides)
    2. Interactive prompts (fields with "password", "secret", "token", or "key"
       in the name are masked during input)

    Args:
        plugin: Plugin instance with a login() method.
        login_callable: The callable to invoke for login (user-defined or generated).
        fields: Dictionary of field names to default values for credential prompts.

    Returns:
        Click Command for the login operation.
    """
    secret_keywords = {"password", "secret", "token", "key"}

    # Build envvar override map before the credential loop
    envvar_overrides: dict[str, str] = {}
    username_envvar = getattr(plugin, "username_envvar", "")
    password_envvar = getattr(plugin, "password_envvar", "")
    if username_envvar:
        envvar_overrides["username"] = username_envvar
    if password_envvar:
        envvar_overrides["password"] = password_envvar

    def callback() -> None:
        login_method = login_callable
        credentials: dict[str, str] = {}
        site_prefix = plugin.site_name.upper().replace("-", "_").replace(" ", "_")

        for field_name in fields:
            is_secret = any(kw in field_name.lower() for kw in secret_keywords)
            envvar = envvar_overrides.get(field_name, f"{site_prefix}_{field_name.upper()}")

            env_value = os.environ.get(envvar)
            if env_value:
                credentials[field_name] = env_value
            else:
                if env_value is not None:
                    LOG.debug("login_envvar_empty", field=field_name, envvar=envvar)
                credentials[field_name] = click.prompt(
                    field_name.replace("_", " ").title(),
                    hide_input=is_secret,
                )

        try:
            with Status("Logging in...", console=gp_console.err_console):
                if asyncio.iscoroutinefunction(login_method):
                    # Suppress asyncio "Loop ... is closed" warning that fires when
                    # asyncio.run() closes the event loop while nodriver's subprocess
                    # handlers are still pending. Suppression covers the entire
                    # asyncio.run() call because the warning fires during shutdown,
                    # which is inseparable from the run() call itself.
                    from graftpunk.logging import suppress_asyncio_noise

                    with suppress_asyncio_noise():
                        result = asyncio.run(login_method(credentials))
                else:
                    result = login_method(credentials)

            if result is False:
                gp_console.error(
                    f"Login failed for {plugin.site_name}. Check your credentials and try again."
                )
                raise SystemExit(1)
            if result is not True:
                LOG.warning(
                    "login_unexpected_return",
                    plugin=plugin.site_name,
                    result_type=type(result).__name__,
                    result=repr(result),
                )
            gp_console.success(f"Logged in to {plugin.site_name} (session cached)")
        except (SystemExit, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 — CLI boundary: present user-friendly error instead of traceback
            LOG.exception(
                "plugin_login_failed",
                plugin=plugin.site_name,
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            gp_console.error(f"Login failed: {exc}")
            raise SystemExit(1) from exc

    # Get help text from login method docstring if available
    help_text = inspect.getdoc(login_callable) or f"Log in to {plugin.site_name}"
    # Use first line of docstring for help
    help_text = help_text.split("\n")[0]

    return typer.core.TyperCommand(
        name="login",
        callback=callback,
        params=[],
        help=help_text,
    )
