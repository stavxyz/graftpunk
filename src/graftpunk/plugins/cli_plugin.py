"""CLI plugin protocol and base class for extensible commands.

This module provides the infrastructure for defining custom CLI commands
that can be registered via entry points or YAML configuration.

Example Python plugin:
    from graftpunk.plugins import SitePlugin, command

    class MyBankPlugin(SitePlugin):
        site_name = "mybank"
        session_name = "mybank"

        @command(help="List accounts")
        def accounts(self, session):
            return session.get("https://mybank.com/api/accounts").json()

Example YAML plugin (in ~/.config/graftpunk/plugins/mybank.yaml):
    site_name: mybank
    session_name: mybank
    commands:
      accounts:
        help: "List accounts"
        url: "https://mybank.com/api/accounts"
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Protocol, runtime_checkable

import requests

from graftpunk.cache import load_session_for_api
from graftpunk.logging import get_logger

LOG = get_logger(__name__)


@dataclass
class ParamSpec:
    """Specification for a command parameter."""

    name: str
    param_type: type = str
    required: bool = False
    default: Any = None
    help_text: str = ""
    is_option: bool = True  # True = --flag, False = positional argument


@dataclass
class CommandSpec:
    """Specification for a single CLI command."""

    name: str
    handler: Callable[..., Any]
    help_text: str = ""
    params: list[ParamSpec] = field(default_factory=list)


@runtime_checkable
class CLIPluginProtocol(Protocol):
    """Protocol defining CLI plugin interface."""

    @property
    def site_name(self) -> str:
        """Plugin identifier used as CLI subcommand group name."""
        ...

    @property
    def session_name(self) -> str:
        """graftpunk session name to load for API calls."""
        ...

    @property
    def help_text(self) -> str:
        """Help text for the plugin's command group."""
        ...

    def get_commands(self) -> dict[str, CommandSpec]:
        """Return all commands defined by this plugin."""
        ...


def command(
    help: str = "",  # noqa: A002 - shadows builtin but matches typer convention
    params: list[ParamSpec] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to mark a method as a CLI command.

    Args:
        help: Help text for the command.
        params: Optional list of parameter specifications.

    Example:
        @command(help="List all accounts")
        def accounts(self, session: requests.Session) -> dict:
            return session.get("https://api.example.com/accounts").json()
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        # Store metadata on the function
        wrapper._is_cli_command = True  # type: ignore[attr-defined]
        wrapper._help_text = help  # type: ignore[attr-defined]
        wrapper._params = params or []  # type: ignore[attr-defined]
        return wrapper

    return decorator


class SitePlugin:
    """Base class for Python-based site plugins.

    Subclass this and use the @command decorator to define commands.
    Commands receive a requests.Session with cookies/headers pre-loaded
    from the cached graftpunk session.

    Example:
        class MyBankPlugin(SitePlugin):
            site_name = "mybank"
            session_name = "mybank"
            help_text = "Commands for MyBank API"

            @command(help="List accounts")
            def accounts(self, session: requests.Session) -> dict:
                return session.get("https://mybank.com/api/accounts").json()

            @command(help="Get statements")
            def statements(self, session: requests.Session, month: str) -> dict:
                return session.get(f"https://mybank.com/api/statements/{month}").json()
    """

    site_name: str = ""
    session_name: str = ""
    help_text: str = ""

    def get_commands(self) -> dict[str, CommandSpec]:
        """Discover all @command decorated methods."""
        commands: dict[str, CommandSpec] = {}
        for attr_name in dir(self):
            if attr_name.startswith("_"):
                continue
            attr = getattr(self, attr_name)
            if callable(attr) and getattr(attr, "_is_cli_command", False):
                commands[attr_name] = CommandSpec(
                    name=attr_name,
                    handler=attr,
                    help_text=getattr(attr, "_help_text", ""),
                    params=getattr(attr, "_params", []),
                )
        return commands

    def get_session(self) -> requests.Session:
        """Load the graftpunk session for API calls."""
        return load_session_for_api(self.session_name)
