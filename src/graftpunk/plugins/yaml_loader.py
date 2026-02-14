"""YAML plugin loader for declarative command definitions.

This module handles loading, validating, and converting YAML plugin
definitions into data structures used to create SitePlugin instances.

YAML plugins are discovered from ~/.config/graftpunk/plugins/*.yaml (and *.yml)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

import yaml

if TYPE_CHECKING:
    from graftpunk.plugins.cli_plugin import PluginConfig

from graftpunk.config import get_settings
from graftpunk.exceptions import PluginError
from graftpunk.logging import get_logger
from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep
from graftpunk.plugins.output_config import ColumnFilter, OutputConfig, ViewConfig
from graftpunk.tokens import Token, TokenConfig

LOG = get_logger(__name__)

# Environment variable pattern: ${VAR_NAME}
ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

# Valid HTTP methods for YAML commands
VALID_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}


@dataclass(frozen=True)
class YAMLParamDef:
    """Parsed YAML parameter definition."""

    name: str
    type: Literal["str", "string", "int", "integer", "float", "bool", "boolean"] = "str"
    required: bool = False
    default: Any = None
    help: str = ""
    is_option: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Parameter name cannot be empty")


@dataclass(frozen=True)
class YAMLCommandDef:
    """Parsed YAML command definition."""

    name: str
    help_text: str
    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
    url: str
    params: tuple[YAMLParamDef, ...] = ()
    jmespath: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    raise_for_status: bool = True
    timeout: float | None = None
    max_retries: int = 0
    rate_limit: float | None = None
    output_config: OutputConfig | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Command name cannot be empty")
        if self.max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {self.max_retries}")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError(f"timeout must be positive, got {self.timeout}")
        if self.rate_limit is not None and self.rate_limit <= 0:
            raise ValueError(f"rate_limit must be positive, got {self.rate_limit}")


@dataclass(frozen=True)
class YAMLDiscoveryError:
    """Error encountered while loading a YAML plugin file.

    Attributes:
        filepath: Path to the YAML file that failed to load.
        error: Human-readable error message describing the failure.
    """

    filepath: Path
    error: str


class YAMLPluginBundle(NamedTuple):
    """A successfully loaded YAML plugin with its config, commands, and headers."""

    config: PluginConfig
    commands: list[YAMLCommandDef]
    headers: dict[str, str]


@dataclass
class YAMLDiscoveryResult:
    """Result of YAML plugin discovery.

    Supports partial success: plugins that fail to load are recorded as
    errors while valid plugins are still returned.

    Attributes:
        plugins: Successfully loaded plugin bundles.
        errors: Errors for plugins that could not be loaded.
    """

    plugins: list[YAMLPluginBundle] = field(default_factory=list)
    errors: list[YAMLDiscoveryError] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        """Return True if any load errors occurred."""
        return bool(self.errors)


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


def _parse_output_config(config_dict: dict[str, Any] | None) -> OutputConfig | None:
    """Parse output_config from YAML dict to OutputConfig dataclass.

    Supports shorthand column syntax (list of column names) and explicit
    mode syntax (dict with mode and columns).

    Args:
        config_dict: The output_config dict from YAML, or None.

    Returns:
        OutputConfig instance or None if config_dict is None.
    """
    if config_dict is None:
        return None

    views = []
    for view_dict in config_dict.get("views", []):
        columns = None
        if "columns" in view_dict:
            cols = view_dict["columns"]
            if isinstance(cols, list):
                # Shorthand: just a list means include mode
                columns = ColumnFilter(mode="include", columns=cols)
            elif isinstance(cols, dict):
                columns = ColumnFilter(
                    mode=cols.get("mode", "include"),
                    columns=cols.get("columns", []),
                )

        views.append(
            ViewConfig(
                name=view_dict.get("name", "default"),
                path=view_dict.get("path", ""),
                title=view_dict.get("title", ""),
                columns=columns,
            )
        )

    return OutputConfig(
        views=tuple(views),
        default_view=config_dict.get("default_view", ""),
    )


def validate_yaml_schema(data: dict[str, Any], filepath: Path) -> None:
    """Validate YAML plugin schema with helpful error messages.

    Args:
        data: Parsed YAML data.
        filepath: Path to YAML file (for error messages).

    Raises:
        PluginError: If schema validation fails.
    """
    # site_name: optional here -- build_plugin_config handles inference and required check.
    # But if present, it must be a non-empty string.
    if "site_name" in data:
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


def parse_yaml_plugin(
    filepath: Path,
) -> YAMLPluginBundle:
    """Parse and validate a YAML plugin file.

    Args:
        filepath: Path to YAML plugin file.

    Returns:
        YAMLPluginBundle containing PluginConfig, commands, and
        plugin-level HTTP headers for request handlers.

    Raises:
        PluginError: If parsing or validation fails.
    """
    from graftpunk.plugins.cli_plugin import build_plugin_config

    try:
        with open(filepath, encoding="utf-8") as f:
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

    # Parse headers (plugin-level, for request handlers)
    headers: dict[str, str] = {}
    for key, value in data.get("headers", {}).items():
        headers[str(key)] = str(value)

    # Parse commands
    commands: list[YAMLCommandDef] = []
    for cmd_name, cmd_def in data["commands"].items():
        params = []
        for param in cmd_def.get("params", []):
            params.append(
                YAMLParamDef(
                    name=param["name"],
                    type=param.get("type", "str"),
                    required=param.get("required", False),
                    default=param.get("default"),
                    help=param.get("help", ""),
                    is_option=param.get("is_option", True),
                )
            )

        commands.append(
            YAMLCommandDef(
                name=str(cmd_name),
                help_text=cmd_def.get("help", ""),
                method=cmd_def.get("method", "GET").upper(),
                url=cmd_def["url"],
                params=tuple(params),
                jmespath=cmd_def.get("jmespath"),
                headers=cmd_def.get("headers", {}),
                raise_for_status=cmd_def.get("raise_for_status", True),
                timeout=cmd_def.get("timeout"),
                max_retries=cmd_def.get("max_retries", 0),
                rate_limit=cmd_def.get("rate_limit"),
                output_config=_parse_output_config(cmd_def.get("output_config")),
            )
        )

    # Build LoginConfig from nested login: block with steps
    login_block = data.get("login")
    login_config: LoginConfig | None = None
    if login_block is not None:
        if not isinstance(login_block, dict):
            raise PluginError(
                f"Plugin '{filepath}': 'login' must be a mapping, not {type(login_block).__name__}."
            )

        # Parse steps (required)
        steps_data = login_block.get("steps")
        if steps_data is None:
            raise PluginError(
                f"Plugin '{filepath}': login block missing required 'steps' field. "
                f"A login block requires 'steps' as a list of step definitions."
            )
        if not isinstance(steps_data, list):
            raise PluginError(
                f"Plugin '{filepath}': login 'steps' must be a list, "
                f"not {type(steps_data).__name__}."
            )
        if len(steps_data) == 0:
            raise PluginError(f"Plugin '{filepath}': login 'steps' must contain at least one step.")

        # Convert each step dict to LoginStep
        steps: list[LoginStep] = []
        for i, step_dict in enumerate(steps_data):
            if not isinstance(step_dict, dict):
                raise PluginError(
                    f"Plugin '{filepath}': login step #{i + 1} must be a mapping, "
                    f"not {type(step_dict).__name__}."
                )
            try:
                step = LoginStep(
                    fields=step_dict.get("fields", {}),
                    submit=step_dict.get("submit", ""),
                    wait_for=step_dict.get("wait_for", ""),
                    delay=step_dict.get("delay", 0.0),
                )
                steps.append(step)
            except ValueError as exc:
                raise PluginError(
                    f"Plugin '{filepath}': login step #{i + 1} is invalid: {exc}"
                ) from exc

        login_config = LoginConfig(
            steps=steps,
            url=login_block.get("url", ""),
            wait_for=login_block.get("wait_for", ""),
            failure=login_block.get("failure", ""),
            success=login_block.get("success", ""),
        )

    # Parse token config
    tokens_block = data.get("tokens")
    token_config: TokenConfig | None = None
    if tokens_block is not None:
        if not isinstance(tokens_block, list):
            raise PluginError(f"Plugin '{filepath}': 'tokens' must be a list of token definitions.")
        tokens = []
        for i, token_def in enumerate(tokens_block):
            if not isinstance(token_def, dict):
                raise PluginError(f"Plugin '{filepath}': token #{i + 1} must be a mapping.")
            if "name" not in token_def or "source" not in token_def:
                raise PluginError(
                    f"Plugin '{filepath}': token #{i + 1} missing required "
                    f"field(s) 'name' and/or 'source'."
                )
            tokens.append(
                Token(
                    name=token_def["name"],
                    source=token_def["source"],
                    pattern=token_def.get("pattern"),
                    cookie_name=token_def.get("cookie_name"),
                    response_header=token_def.get("response_header"),
                    page_url=token_def.get("page_url", "/"),
                    cache_duration=token_def.get("cache_duration", 300),
                )
            )
        token_config = TokenConfig(tokens=tuple(tokens))

    # Build PluginConfig via shared factory (without mutating data dict)
    config = build_plugin_config(
        site_name=data.get("site_name", ""),
        session_name=data.get("session_name", ""),
        help_text=data.get("help", ""),
        base_url=data.get("base_url", ""),
        requires_session=data.get("requires_session", True),
        backend=data.get("backend", "selenium"),
        username_envvar=data.get("username_envvar", ""),
        password_envvar=data.get("password_envvar", ""),
        api_version=data.get("api_version", 1),
        login_config=login_config,
        token_config=token_config,
        source_filepath=filepath,
    )

    return YAMLPluginBundle(config=config, commands=commands, headers=headers)


def discover_yaml_plugins() -> YAMLDiscoveryResult:
    """Discover all YAML plugins in the config directory.

    Looks for *.yaml and *.yml files in ~/.config/graftpunk/plugins/

    Supports partial success: valid plugins are returned even if some files
    fail to load. Check result.has_errors to see if any failures occurred.

    Returns:
        YAMLDiscoveryResult containing loaded plugins and any errors.
    """
    settings = get_settings()
    plugins_dir = settings.config_dir / "plugins"

    if not plugins_dir.exists():
        LOG.debug("yaml_plugins_dir_not_found", path=str(plugins_dir))
        return YAMLDiscoveryResult()

    result = YAMLDiscoveryResult()

    # Support both .yaml and .yml extensions
    yaml_files = list(plugins_dir.glob("*.yaml")) + list(plugins_dir.glob("*.yml"))

    for yaml_file in yaml_files:
        try:
            bundle = parse_yaml_plugin(yaml_file)
            result.plugins.append(bundle)
            LOG.info(
                "yaml_plugin_loaded",
                site_name=bundle.config.site_name,
                path=str(yaml_file),
            )
        except (PluginError, yaml.YAMLError, ValueError, TypeError) as exc:
            LOG.warning("yaml_plugin_load_failed", path=str(yaml_file), error=str(exc))
            result.errors.append(YAMLDiscoveryError(filepath=yaml_file, error=str(exc)))

    return result
