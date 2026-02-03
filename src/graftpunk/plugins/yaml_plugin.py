"""YAML-based site plugin factory.

Creates dynamic SitePlugin subclasses from YAML plugin definitions.
Handler and parameter conversion logic lives as module-level functions;
the factory dynamically builds a SitePlugin subclass per YAML file.
"""

from __future__ import annotations

import re
import types
from pathlib import Path
from typing import Any

from graftpunk import console as gp_console
from graftpunk.exceptions import PluginError
from graftpunk.logging import get_logger
from graftpunk.plugins.cli_plugin import (
    CommandContext,
    CommandSpec,
    PluginConfig,
    PluginParamSpec,
    SitePlugin,
)
from graftpunk.plugins.yaml_loader import (
    YAMLCommandDef,
    YAMLDiscoveryError,
    discover_yaml_plugins,
    expand_env_vars,
)

LOG = get_logger(__name__)

URL_PARAM_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

_jmespath: types.ModuleType | None = None
HAS_JMESPATH = False
try:
    import jmespath

    _jmespath = jmespath
    HAS_JMESPATH = True
except ImportError as _jmespath_err:
    LOG.debug("jmespath_import_failed", error=str(_jmespath_err))


def _convert_params(cmd_def: YAMLCommandDef) -> list[PluginParamSpec]:
    """Convert YAML param definitions to PluginParamSpec objects."""
    type_map: dict[str, type] = {
        "str": str,
        "string": str,
        "int": int,
        "integer": int,
        "float": float,
        "bool": bool,
        "boolean": bool,
    }
    params = []
    for param in cmd_def.params:
        param_type = type_map.get(str(param.type).lower(), str)
        params.append(
            PluginParamSpec(
                name=param.name,
                param_type=param_type,
                required=param.required,
                default=param.default,
                help_text=param.help,
                is_option=param.is_option,
            )
        )
    return params


def _create_handler(
    cmd_def: YAMLCommandDef,
    base_url: str,
    plugin_headers: dict[str, str],
) -> Any:
    """Create a callable handler for a YAML command."""

    def handler(ctx: CommandContext, **kwargs: Any) -> Any:
        session = ctx.session
        # Build full URL
        url = cmd_def.url
        if base_url:
            base = base_url.rstrip("/")
            path = url.lstrip("/")
            url = f"{base}/{path}"

        # Substitute URL parameters
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
        for key, value in plugin_headers.items():
            headers[key] = expand_env_vars(value)
        for key, value in cmd_def.headers.items():
            headers[key] = expand_env_vars(value)

        # Build query params from kwargs not in URL params
        url_params = set(URL_PARAM_PATTERN.findall(cmd_def.url))
        query_params = {k: v for k, v in kwargs.items() if k not in url_params and v is not None}

        # Make request
        LOG.debug("yaml_plugin_request", method=cmd_def.method, url=url, params=query_params)

        response = session.request(
            method=cmd_def.method,
            url=url,
            headers=headers,
            params=query_params if query_params else None,
        )
        if cmd_def.raise_for_status:
            response.raise_for_status()

        # Parse response
        try:
            data = response.json()
        except ValueError as exc:
            content_type = response.headers.get("Content-Type", "unknown")
            LOG.warning(
                "json_parse_failed_returning_text",
                url=url,
                content_type=content_type,
                error=str(exc),
            )
            if "<!DOCTYPE" in response.text or "<html" in response.text.lower():
                gp_console.warn(
                    "Expected JSON but received HTML. The API may have returned an error page."
                )
            return response.text

        # Apply jmespath filter
        if cmd_def.jmespath:
            if not HAS_JMESPATH:
                LOG.warning(
                    "jmespath_not_installed",
                    hint="Install jmespath: pip install 'graftpunk[jmespath]'",
                )
                gp_console.warn(
                    "jmespath filter ignored (install with: pip install 'graftpunk[jmespath]')"
                )
                return data
            assert _jmespath is not None
            data = _jmespath.search(cmd_def.jmespath, data)

        return data

    return handler


def create_yaml_site_plugin(
    config: PluginConfig,
    commands: list[YAMLCommandDef],
    plugin_headers: dict[str, str] | None = None,
) -> SitePlugin:
    """Create a SitePlugin instance from YAML-parsed config and commands.

    Dynamically creates a SitePlugin subclass with get_commands()
    returning CommandSpecs built from the YAML command definitions.

    Args:
        config: PluginConfig with plugin metadata.
        commands: YAML command definitions.
        plugin_headers: Plugin-level HTTP headers for request handlers.

    Returns:
        SitePlugin instance ready for CLI registration.
    """
    import dataclasses

    headers = plugin_headers or {}

    # Build CommandSpec list
    command_specs: list[CommandSpec] = []
    for cmd_def in commands:
        handler = _create_handler(cmd_def, config.base_url, headers)
        params = _convert_params(cmd_def)
        command_specs.append(
            CommandSpec(
                name=cmd_def.name,
                handler=handler,
                help_text=cmd_def.help_text,
                params=tuple(params),
                timeout=cmd_def.timeout,
                max_retries=cmd_def.max_retries,
                rate_limit=cmd_def.rate_limit,
            )
        )

    # Create dynamic subclass attrs from config
    attrs: dict[str, Any] = dict(dataclasses.asdict(config))
    # Restore LoginConfig instance (asdict deep-converts to plain dict)
    attrs["login_config"] = config.login_config
    # Restore TokenConfig instance (asdict deep-converts Token objects to dicts)
    attrs["token_config"] = config.token_config

    def get_commands(self: Any) -> list[CommandSpec]:
        return command_specs

    attrs["get_commands"] = get_commands

    # Create dynamic subclass -- __init_subclass__ fires but is safe to re-run
    plugin_class = type(f"YAMLPlugin_{config.site_name}", (SitePlugin,), attrs)
    return plugin_class()


def create_yaml_plugins() -> tuple[list[SitePlugin], list[YAMLDiscoveryError]]:
    """Create SitePlugin instances from discovered YAML files.

    Returns:
        Tuple of (plugins, errors) where:
        - plugins: List of SitePlugin instances ready for CLI registration.
        - errors: List of YAMLDiscoveryError for files that failed to load.
    """
    discovery_result = discover_yaml_plugins()
    plugins = []
    for config, cmds, headers in discovery_result.plugins:
        try:
            plugins.append(create_yaml_site_plugin(config, cmds, headers))
        except PluginError as exc:
            LOG.warning(
                "yaml_site_plugin_creation_failed",
                site_name=config.site_name,
                error=str(exc),
            )
            discovery_result.errors.append(
                YAMLDiscoveryError(filepath=Path(f"<{config.site_name}>"), error=str(exc))
            )
    return plugins, discovery_result.errors
