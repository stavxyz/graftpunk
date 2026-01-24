"""YAML plugin loader for declarative command definitions.

This module handles loading, validating, and converting YAML plugin
definitions into data structures that can be used by YAMLSitePlugin.

YAML plugins are discovered from ~/.config/graftpunk/plugins/*.yaml
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from graftpunk.config import get_settings
from graftpunk.exceptions import PluginError
from graftpunk.logging import get_logger

LOG = get_logger(__name__)

# Environment variable pattern: ${VAR_NAME}
ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

# Valid HTTP methods for YAML commands
VALID_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}


@dataclass
class YAMLCommandDef:
    """Parsed YAML command definition."""

    name: str
    help_text: str
    method: str
    url: str
    params: list[dict[str, Any]] = field(default_factory=list)
    jmespath: str | None = None


@dataclass
class YAMLPluginDef:
    """Parsed YAML plugin definition."""

    site_name: str
    session_name: str
    help_text: str
    base_url: str
    headers: dict[str, str]
    commands: list[YAMLCommandDef]


def expand_env_vars(value: str) -> str:
    """Expand ${VAR} patterns in a string with environment variables.

    Args:
        value: String potentially containing ${VAR} patterns.

    Returns:
        String with environment variables expanded.

    Raises:
        PluginError: If referenced environment variable is not set.
    """

    def replacer(match: re.Match[str]) -> str:
        var_name = match.group(1)
        var_value = os.environ.get(var_name)
        if var_value is None:
            raise PluginError(
                f"Environment variable ${var_name} is not set. Set it before using this plugin."
            )
        return var_value

    return ENV_VAR_PATTERN.sub(replacer, value)


def validate_yaml_schema(data: dict[str, Any], filepath: Path) -> None:
    """Validate YAML plugin schema with helpful error messages.

    Args:
        data: Parsed YAML data.
        filepath: Path to YAML file (for error messages).

    Raises:
        PluginError: If schema validation fails.
    """
    # Required: site_name
    if "site_name" not in data:
        raise PluginError(
            f"Plugin '{filepath}' missing required field 'site_name'. "
            f"Add 'site_name: mysite' at the top of the file."
        )

    site_name = data["site_name"]
    if not isinstance(site_name, str) or not site_name.strip():
        raise PluginError(f"Plugin '{filepath}': 'site_name' must be a non-empty string.")

    # Required: commands
    if "commands" not in data or not data["commands"]:
        raise PluginError(
            f"Plugin '{filepath}' has no commands defined. "
            f"Add a 'commands:' section with at least one command."
        )

    if not isinstance(data["commands"], dict):
        raise PluginError(
            f"Plugin '{filepath}': 'commands' must be a mapping of command names "
            f"to command definitions."
        )

    # Validate each command
    for cmd_name, cmd_def in data["commands"].items():
        if not isinstance(cmd_def, dict):
            raise PluginError(
                f"Plugin '{filepath}': command '{cmd_name}' must be a mapping, "
                f"not {type(cmd_def).__name__}."
            )

        # url is required
        if "url" not in cmd_def:
            raise PluginError(f"Plugin '{filepath}': command '{cmd_name}' missing 'url' field.")

        # Validate method
        method = cmd_def.get("method", "GET").upper()
        if method not in VALID_METHODS:
            raise PluginError(
                f"Plugin '{filepath}': command '{cmd_name}' has invalid method "
                f"'{method}'. Valid methods: {', '.join(sorted(VALID_METHODS))}"
            )

        # Validate params if present
        params = cmd_def.get("params", [])
        if not isinstance(params, list):
            raise PluginError(f"Plugin '{filepath}': command '{cmd_name}' params must be a list.")

        for i, param in enumerate(params):
            if not isinstance(param, dict):
                raise PluginError(
                    f"Plugin '{filepath}': command '{cmd_name}' param #{i + 1} must be a mapping."
                )
            if "name" not in param:
                raise PluginError(
                    f"Plugin '{filepath}': command '{cmd_name}' param #{i + 1} "
                    f"missing 'name' field."
                )

            # Validate param type if specified
            param_type = param.get("type", "str").lower()
            valid_types = {"str", "string", "int", "integer", "float", "bool", "boolean"}
            if param_type not in valid_types:
                raise PluginError(
                    f"Plugin '{filepath}': command '{cmd_name}' param "
                    f"'{param['name']}' has invalid type '{param_type}'. "
                    f"Valid types: str, int, float, bool"
                )


def parse_yaml_plugin(filepath: Path) -> YAMLPluginDef:
    """Parse and validate a YAML plugin file.

    Args:
        filepath: Path to YAML plugin file.

    Returns:
        Parsed YAMLPluginDef.

    Raises:
        PluginError: If parsing or validation fails.
    """
    try:
        with open(filepath) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise PluginError(f"Invalid YAML in '{filepath}': {exc}") from exc
    except OSError as exc:
        raise PluginError(f"Cannot read plugin file '{filepath}': {exc}") from exc

    if data is None:
        raise PluginError(f"Plugin '{filepath}' is empty.")

    if not isinstance(data, dict):
        raise PluginError(f"Plugin '{filepath}' must be a YAML mapping, not {type(data).__name__}")

    validate_yaml_schema(data, filepath)

    # Parse headers (store raw values; expansion happens at request time)
    headers: dict[str, str] = {}
    for key, value in data.get("headers", {}).items():
        headers[str(key)] = str(value)

    # Parse commands
    commands: list[YAMLCommandDef] = []
    for cmd_name, cmd_def in data["commands"].items():
        params = []
        for param in cmd_def.get("params", []):
            params.append(
                {
                    "name": param["name"],
                    "type": param.get("type", "str"),
                    "required": param.get("required", False),
                    "default": param.get("default"),
                    "help": param.get("help", ""),
                    "is_option": param.get("is_option", True),
                }
            )

        commands.append(
            YAMLCommandDef(
                name=str(cmd_name),
                help_text=cmd_def.get("help", ""),
                method=cmd_def.get("method", "GET").upper(),
                url=cmd_def["url"],
                params=params,
                jmespath=cmd_def.get("jmespath"),
            )
        )

    # session_name defaults to site_name, but empty string means no session
    session_name = data.get("session_name")
    if session_name is None:
        session_name = data["site_name"]

    return YAMLPluginDef(
        site_name=data["site_name"],
        session_name=session_name,
        help_text=data.get("help", f"Commands for {data['site_name']}"),
        base_url=data.get("base_url", ""),
        headers=headers,
        commands=commands,
    )


def discover_yaml_plugins() -> list[YAMLPluginDef]:
    """Discover all YAML plugins in the config directory.

    Looks for *.yaml and *.yml files in ~/.config/graftpunk/plugins/

    Returns:
        List of parsed YAML plugin definitions.
    """
    settings = get_settings()
    plugins_dir = settings.config_dir / "plugins"

    if not plugins_dir.exists():
        LOG.debug("yaml_plugins_dir_not_found", path=str(plugins_dir))
        return []

    plugins: list[YAMLPluginDef] = []

    # Support both .yaml and .yml extensions
    yaml_files = list(plugins_dir.glob("*.yaml")) + list(plugins_dir.glob("*.yml"))

    for yaml_file in yaml_files:
        try:
            plugin = parse_yaml_plugin(yaml_file)
            plugins.append(plugin)
            LOG.info("yaml_plugin_loaded", site_name=plugin.site_name, path=str(yaml_file))
        except PluginError as exc:
            LOG.warning("yaml_plugin_load_failed", path=str(yaml_file), error=str(exc))

    return plugins
