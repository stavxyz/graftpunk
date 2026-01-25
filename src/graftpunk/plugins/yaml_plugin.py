"""YAML-based site plugin adapter.

Converts YAMLPluginDef into a SitePlugin-compatible object that
can be used by the CLI registration system.
"""

from __future__ import annotations

import re
from typing import Any

import requests

from graftpunk.cache import load_session_for_api
from graftpunk.logging import get_logger
from graftpunk.plugins.cli_plugin import CommandSpec, ParamSpec
from graftpunk.plugins.yaml_loader import (
    YAMLCommandDef,
    YAMLPluginDef,
    discover_yaml_plugins,
    expand_env_vars,
)

LOG = get_logger(__name__)

# URL parameter pattern: {param_name}
URL_PARAM_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# Try to import jmespath, but it's optional
try:
    import jmespath as jmespath_module

    HAS_JMESPATH = True
except ImportError:
    jmespath_module = None
    HAS_JMESPATH = False


class YAMLSitePlugin:
    """Adapter that makes a YAML plugin definition behave like a SitePlugin.

    This class implements CLIPluginProtocol and converts YAML command
    definitions into callable handlers.
    """

    def __init__(self, plugin_def: YAMLPluginDef) -> None:
        """Initialize from a parsed YAML plugin definition.

        Args:
            plugin_def: Parsed YAML plugin definition.
        """
        self._def = plugin_def
        self._commands: dict[str, CommandSpec] | None = None

    @property
    def site_name(self) -> str:
        """Plugin identifier used as CLI subcommand group name."""
        return self._def.site_name

    @property
    def session_name(self) -> str:
        """graftpunk session name to load for API calls."""
        return self._def.session_name

    @property
    def help_text(self) -> str:
        """Help text for the plugin's command group."""
        return self._def.help_text

    @property
    def requires_session(self) -> bool:
        """Whether this plugin requires a cached session.

        Returns False if session_name is empty string.
        """
        return bool(self._def.session_name)

    def get_session(self) -> requests.Session:
        """Load the graftpunk session for API calls.

        If session_name is empty, returns a plain requests.Session.
        """
        if not self.requires_session:
            return requests.Session()
        return load_session_for_api(self.session_name)

    def get_commands(self) -> dict[str, CommandSpec]:
        """Return all commands defined by this plugin."""
        if self._commands is not None:
            return self._commands

        self._commands = {}
        for cmd_def in self._def.commands:
            handler = self._create_handler(cmd_def)
            params = self._convert_params(cmd_def)

            self._commands[cmd_def.name] = CommandSpec(
                name=cmd_def.name,
                handler=handler,
                help_text=cmd_def.help_text,
                params=params,
            )

        return self._commands

    def _convert_params(self, cmd_def: YAMLCommandDef) -> list[ParamSpec]:
        """Convert YAML param definitions to ParamSpec objects."""
        params = []
        type_map: dict[str, type] = {
            "str": str,
            "string": str,
            "int": int,
            "integer": int,
            "float": float,
            "bool": bool,
            "boolean": bool,
        }

        for param in cmd_def.params:
            param_type = type_map.get(str(param["type"]).lower(), str)
            params.append(
                ParamSpec(
                    name=param["name"],
                    param_type=param_type,
                    required=param["required"],
                    default=param["default"],
                    help_text=param["help"],
                    is_option=param["is_option"],
                )
            )

        return params

    def _create_handler(self, cmd_def: YAMLCommandDef) -> Any:
        """Create a callable handler for a YAML command.

        Returns a function that:
        1. Builds the full URL with parameter substitution
        2. Expands headers with environment variables
        3. Makes the HTTP request
        4. Applies jmespath filter if specified
        """
        # Capture self and cmd_def in closure
        plugin_def = self._def

        def handler(session: requests.Session, **kwargs: Any) -> Any:
            # Build full URL
            url = cmd_def.url
            if plugin_def.base_url:
                base = plugin_def.base_url.rstrip("/")
                path = url.lstrip("/")
                url = f"{base}/{path}"

            # Substitute URL parameters: {param} -> value
            for param_name, param_value in kwargs.items():
                if param_value is not None:
                    url = url.replace(f"{{{param_name}}}", str(param_value))

            # Check for unsubstituted parameters
            remaining = URL_PARAM_PATTERN.findall(url)
            if remaining:
                missing = ", ".join(remaining)
                raise ValueError(f"Missing required URL parameters: {missing}")

            # Expand headers with environment variables
            headers = {}
            for key, value in plugin_def.headers.items():
                headers[key] = expand_env_vars(value)

            # Make request
            LOG.debug("yaml_plugin_request", method=cmd_def.method, url=url)

            response = session.request(
                method=cmd_def.method,
                url=url,
                headers=headers,
            )
            response.raise_for_status()

            # Parse JSON response
            try:
                data = response.json()
            except ValueError:
                # Return raw text if not JSON
                return response.text

            # Apply jmespath filter if specified
            if cmd_def.jmespath:
                if not HAS_JMESPATH:
                    LOG.warning(
                        "jmespath_not_installed",
                        hint="Install jmespath: pip install 'graftpunk[jmespath]'",
                    )
                    # Import here to avoid circular import at module level
                    from rich.console import Console

                    console = Console(stderr=True)
                    console.print(
                        "[yellow]Warning: jmespath filter ignored "
                        "(install with: pip install 'graftpunk[jmespath]')[/yellow]"
                    )
                    return data
                data = jmespath_module.search(cmd_def.jmespath, data)

            return data

        return handler


def create_yaml_plugins() -> list[YAMLSitePlugin]:
    """Create YAMLSitePlugin instances from discovered YAML files.

    Returns:
        List of YAMLSitePlugin instances ready for CLI registration.
    """
    plugins = []
    for plugin_def in discover_yaml_plugins():
        plugins.append(YAMLSitePlugin(plugin_def))

    return plugins
