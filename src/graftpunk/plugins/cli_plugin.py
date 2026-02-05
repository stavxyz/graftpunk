"""CLI plugin protocol and base class for extensible commands.

This module provides the infrastructure for defining custom CLI commands
that can be registered via entry points or YAML configuration.

Example Python plugin:
    from graftpunk.plugins import SitePlugin, command

    class MyBankPlugin(SitePlugin):
        site_name = "mybank"
        session_name = "mybank"

        @command(help="List accounts")
        def accounts(self, ctx: CommandContext):
            return ctx.session.get("https://mybank.com/api/accounts").json()

Example YAML plugin (in ~/.config/graftpunk/plugins/mybank.yaml):
    site_name: mybank
    session_name: mybank
    commands:
      accounts:
        help: "List accounts"
        url: "https://mybank.com/api/accounts"
"""

# TODO: This file exceeds the 800-line project limit (~840 lines).
# Split into separate modules: move LoginConfig and related validation
# into a dedicated login_config.py module, or extract the command
# discovery/introspection logic into a discovery.py module.
from __future__ import annotations

import dataclasses
import inspect
import re
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

import requests

if TYPE_CHECKING:
    from graftpunk.tokens import TokenConfig

from graftpunk.cache import load_session_for_api
from graftpunk.exceptions import PluginError
from graftpunk.logging import get_logger
from graftpunk.observe import NoOpObservabilityContext, ObservabilityContext

LOG = get_logger(__name__)

SUPPORTED_API_VERSIONS: frozenset[int] = frozenset({1})


@dataclass(frozen=True)
class PluginParamSpec:
    """Specification for a command parameter.

    Holds the parameter name, whether it is a Click option or argument,
    and a ``click_kwargs`` dict that is splatted directly into
    ``click.Option()`` or ``click.Argument()`` at registration time.

    Use the convenience constructors :meth:`option` and :meth:`argument`
    for ergonomic creation with sensible defaults.
    """

    name: str
    is_option: bool = True  # True = --flag, False = positional argument
    click_kwargs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("PluginParamSpec.name must be non-empty")
        # Defensive copy to prevent external mutation of the kwargs dict
        object.__setattr__(self, "click_kwargs", dict(self.click_kwargs))

    @staticmethod
    def option(
        name: str,
        *,
        type: type = str,  # noqa: A002
        required: bool = False,
        default: Any = None,
        help: str = "",  # noqa: A002
        click_kwargs: dict[str, Any] | None = None,
    ) -> PluginParamSpec:
        """Create an option (``--flag``) parameter spec with sensible defaults.

        Builds a ``click_kwargs`` dict from the explicit parameters,
        then merges any extra *click_kwargs* on top (overriding explicit
        values on conflict).  The final dict is splatted into
        ``click.Option()`` at command registration time.

        When *type* is ``bool`` and *default* is ``False``, ``is_flag``
        is automatically set to ``True``.  Pass ``click_kwargs={"is_flag": False}``
        to override this auto-detection.

        Args:
            name: Parameter name.
            type: Click type for the parameter (e.g. ``str``, ``int``, ``bool``).
            required: Whether the option is required.
            default: Default value when not provided.
            help: Help text for ``--help`` output.
            click_kwargs: Extra kwargs merged into the dict passed to
                ``click.Option()``.  These override the explicit parameters.

        Returns:
            A new PluginParamSpec configured as a Click option.
        """
        if required and default is not None:
            raise ValueError(
                f"PluginParamSpec.option({name!r}): required=True conflicts with "
                f"default={default!r}. A required option cannot have a default value."
            )
        kw: dict[str, Any] = {"type": type, "required": required, "default": default}
        if help:
            kw["help"] = help
        # Smart default: bool with default=False -> is_flag=True
        if type is bool and default is False:
            kw["is_flag"] = True
        if click_kwargs:
            kw.update(click_kwargs)
        return PluginParamSpec(name=name, is_option=True, click_kwargs=kw)

    @staticmethod
    def argument(
        name: str,
        *,
        type: type = str,  # noqa: A002
        required: bool = True,
        default: Any = None,
        click_kwargs: dict[str, Any] | None = None,
    ) -> PluginParamSpec:
        """Create a positional argument parameter spec.

        Args:
            name: Parameter name.
            type: Click type for the parameter (e.g. ``str``, ``int``).
            required: Whether the argument is required (default ``True``).
            default: Default value when not provided.
            click_kwargs: Extra kwargs merged into the dict passed to
                ``click.Argument()``.  These override the explicit parameters.

        Returns:
            A new PluginParamSpec configured as a Click argument.
        """
        if required and default is not None:
            raise ValueError(
                f"PluginParamSpec.argument({name!r}): required=True conflicts with "
                f"default={default!r}. A required argument cannot have a default value."
            )
        kw: dict[str, Any] = {"type": type, "required": required, "default": default}
        if click_kwargs:
            kw.update(click_kwargs)
        return PluginParamSpec(name=name, is_option=False, click_kwargs=kw)


@dataclass(frozen=True)
class CommandGroupMeta:
    """Metadata stored on @command-decorated classes (command groups).

    Unlike :class:`CommandMetadata` and :class:`CommandSpec`, this class
    stores ``help_text`` as a plain field rather than in ``click_kwargs``,
    because command *groups* don't accept arbitrary Click kwargs — only
    individual commands do.
    """

    name: str
    help_text: str
    parent: type | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("CommandGroupMeta.name must be non-empty")


@dataclass(frozen=True)
class CommandMetadata:
    """Metadata stored on @command-decorated methods."""

    name: str
    params: tuple[PluginParamSpec, ...] = ()
    parent: type | None = None
    requires_session: bool | None = None
    saves_session: bool = False
    click_kwargs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("CommandMetadata.name must be non-empty")
        # Defensive copy to prevent external mutation of the kwargs dict
        object.__setattr__(self, "click_kwargs", dict(self.click_kwargs))

    @property
    def help_text(self) -> str:
        """Convenience accessor for ``click_kwargs["help"]``."""
        return self.click_kwargs.get("help") or ""


@dataclass
class CommandContext:
    """Execution context passed to command handlers."""

    session: requests.Session
    plugin_name: str
    command_name: str
    api_version: int
    base_url: str = ""
    config: PluginConfig | None = None
    observe: ObservabilityContext = field(default_factory=NoOpObservabilityContext)
    _session_name: str = field(default="", repr=False)
    _session_dirty: bool = field(default=False, repr=False, init=False)

    def __post_init__(self) -> None:
        if self.api_version not in SUPPORTED_API_VERSIONS:
            raise ValueError(
                f"api_version {self.api_version} not supported. "
                f"Supported: {sorted(SUPPORTED_API_VERSIONS)}"
            )
        if not self.plugin_name:
            raise ValueError("plugin_name must be non-empty")
        if not self.command_name:
            raise ValueError("command_name must be non-empty")

    def save_session(self) -> None:
        """Mark the session as dirty so it will be persisted after command execution.

        Raises:
            ValueError: If no session name is configured on this context.
        """
        if not self._session_name:
            raise ValueError("No session name configured on this CommandContext")
        self._session_dirty = True


@dataclass(frozen=True)
class CommandResult:
    """Structured return type for command handlers.

    Allows handlers to return data with optional metadata (pagination, status)
    and format hints. Handlers can still return raw data -- this is not required.
    """

    data: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    format_hint: Literal["json", "table", "raw", "csv"] | None = None


@dataclass(frozen=True)
class CommandSpec:
    """Specification for a single CLI command."""

    name: str
    handler: Callable[..., Any]
    params: tuple[PluginParamSpec, ...] = ()
    timeout: float | None = None
    max_retries: int = 0
    rate_limit: float | None = None
    requires_session: bool | None = None
    group: str | None = None
    saves_session: bool = False
    click_kwargs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("CommandSpec.name must be non-empty")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError("timeout must be positive when set")
        if self.rate_limit is not None and self.rate_limit <= 0:
            raise ValueError("rate_limit must be positive when set")
        # Defensive copy to prevent external mutation of the kwargs dict
        object.__setattr__(self, "click_kwargs", dict(self.click_kwargs))

    @property
    def help_text(self) -> str:
        """Convenience accessor for ``click_kwargs["help"]``."""
        return self.click_kwargs.get("help") or ""


@dataclass(frozen=True)
class LoginConfig:
    """Declarative browser-automated login configuration.

    Required fields (url, fields, submit) must be non-empty and non-whitespace.
    Optional fields (failure, success, wait_for) default to empty string,
    meaning "not configured".

    Attributes:
        url: Login page path (appended to base_url).
        fields: Maps credential names to CSS selectors for form inputs.
        submit: CSS selector for the submit button.
        failure: Text on the page indicating login failure.
        success: CSS selector for an element indicating login success.
        wait_for: CSS selector to wait for before interacting with the page.
            Useful when the login URL triggers a redirect or the form renders
            asynchronously. Empty string (default) means no explicit wait.
    """

    url: str
    fields: dict[str, str]
    submit: str
    # TODO: Validate failure and success for whitespace-only content, same as
    # wait_for. Currently empty string means "not configured" and any non-empty
    # value is accepted, but " " (whitespace-only) would silently pass validation
    # and never match anything at runtime. Needs coordination with YAML plugin
    # loader which also constructs LoginConfig from user input.
    failure: str = ""
    success: str = ""
    wait_for: str = ""

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError("LoginConfig.url must be non-empty")
        if not self.fields:
            raise ValueError("LoginConfig.fields must be non-empty")
        if not self.submit.strip():
            raise ValueError("LoginConfig.submit must be non-empty")
        if self.wait_for and not self.wait_for.strip():
            raise ValueError("LoginConfig.wait_for must not be whitespace-only")
        for name, selector in self.fields.items():
            if not selector.strip():
                raise ValueError(f"LoginConfig.fields['{name}'] selector must be non-empty")
        # Defensive copy: prevent external mutation of fields dict
        object.__setattr__(self, "fields", dict(self.fields))


@dataclass(frozen=True)
class PluginConfig:
    """Canonical plugin configuration produced by both YAML and Python paths.

    Use build_plugin_config() to create instances with proper defaulting
    and validation instead of constructing directly.
    """

    site_name: str
    session_name: str
    help_text: str
    base_url: str = ""
    requires_session: bool = True
    backend: Literal["selenium", "nodriver"] = "selenium"
    api_version: int = 1
    username_envvar: str = ""
    password_envvar: str = ""
    login_config: LoginConfig | None = None
    token_config: TokenConfig | None = None
    plugin_version: str = ""
    plugin_author: str = ""
    plugin_url: str = ""

    def __post_init__(self) -> None:
        if not self.site_name:
            raise ValueError("site_name must be non-empty")
        if not self.session_name:
            raise ValueError("session_name must be non-empty")
        if self.api_version not in SUPPORTED_API_VERSIONS:
            raise ValueError(
                f"api_version {self.api_version} not supported. "
                f"Supported: {sorted(SUPPORTED_API_VERSIONS)}"
            )


def build_plugin_config(**raw: Any) -> PluginConfig:
    """Build a PluginConfig with shared defaulting and validation.

    Accepts arbitrary kwargs, filters to known PluginConfig fields,
    applies defaults (site_name inference, session_name, help_text),
    and validates.

    Args:
        **raw: Plugin configuration values. Unknown keys are ignored.

    Returns:
        Validated PluginConfig.

    Raises:
        PluginError: If site_name cannot be determined.
    """
    from graftpunk.plugins import infer_site_name

    # Pop flat login fields (no longer PluginConfig fields)
    login_config = raw.pop("login_config", raw.pop("login", None))
    login_url = raw.pop("login_url", "")
    login_fields_val = raw.pop("login_fields", {})
    login_submit = raw.pop("login_submit", "")
    login_failure = raw.pop("login_failure", "")
    login_success = raw.pop("login_success", "")

    # Filter to known fields
    known_fields = {f.name for f in dataclasses.fields(PluginConfig)}
    filtered = {k: v for k, v in raw.items() if k in known_fields}

    # Auto-construct LoginConfig from flat fields if not already provided
    if login_config is None and login_url and login_fields_val and login_submit:
        login_config = LoginConfig(
            url=login_url,
            fields=login_fields_val,
            submit=login_submit,
            failure=login_failure,
            success=login_success,
        )
    filtered["login_config"] = login_config

    # Infer site_name: explicit → base_url domain → filename stem
    source_filepath = raw.pop("source_filepath", None)
    site_name = filtered.get("site_name")
    if not site_name:
        base_url = filtered.get("base_url")
        if base_url:
            site_name = infer_site_name(base_url)
    if not site_name and source_filepath:
        site_name = Path(source_filepath).stem.strip()
    if not site_name:
        raise PluginError(
            "Plugin missing 'site_name'. Set site_name explicitly, "
            "provide base_url to auto-infer from domain, "
            "or name the YAML file after the site."
        )
    filtered["site_name"] = site_name

    # Default session_name to site_name
    if not filtered.get("session_name"):
        filtered["session_name"] = site_name

    # Default help_text
    if not filtered.get("help_text"):
        filtered["help_text"] = f"Commands for {site_name}"

    return PluginConfig(**filtered)


@runtime_checkable
class CLIPluginProtocol(Protocol):
    """Structural typing contract for CLI plugin implementations.

    This protocol defines the interface that all plugin implementations
    (both Python subclasses and YAML-based plugins) must satisfy.
    Use isinstance(obj, CLIPluginProtocol) for runtime type checking.

    Plugins must provide:
        - Identification properties (site_name, session_name, help_text)
        - Configuration properties (api_version, backend, login_config, etc.)
        - Command discovery via get_commands()
        - Session management via get_session()
        - Lifecycle hooks (setup, teardown)
    """

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

    @property
    def api_version(self) -> int: ...

    @property
    def backend(self) -> Literal["selenium", "nodriver"]: ...

    @property
    def login_config(self) -> LoginConfig | None: ...

    @property
    def username_envvar(self) -> str: ...

    @property
    def password_envvar(self) -> str: ...

    @property
    def token_config(self) -> TokenConfig | None: ...

    @property
    def requires_session(self) -> bool: ...

    def get_commands(self) -> list[CommandSpec]:
        """Return all commands defined by this plugin."""
        ...

    def get_session(self) -> requests.Session:
        """Load the graftpunk session for API calls."""
        ...

    def setup(self) -> None: ...

    def teardown(self) -> None: ...


def _to_cli_name(name: str) -> str:
    """Convert PythonName to cli-name (CamelCase to kebab-case, underscores to hyphens).

    Args:
        name: Python identifier (e.g. "AccountStatements" or "account_statements").

    Returns:
        CLI-friendly name (e.g. "account-statements").
    """
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name)
    return s.lower().replace("_", "-")


def command(
    help: str = "",  # noqa: A002 - shadows builtin but matches typer convention
    params: list[PluginParamSpec] | None = None,
    parent: type | None = None,
    requires_session: bool | None = None,
    saves_session: bool = False,
) -> Callable[..., Any]:
    """Decorator to mark a function as a CLI command or a class as a command group.

    When applied to a function, stores CommandMetadata on the function.
    When applied to a class, stores CommandGroupMeta on the class and
    auto-discovers non-underscore methods as subcommands.

    Args:
        help: Help text for the command or group.
        params: Optional list of parameter specifications (functions only).
            If omitted, parameters are auto-discovered from function signature.
        parent: Parent command group class for nesting (optional).
        requires_session: Override plugin-level requires_session (functions only).
            None means inherit from the plugin's requires_session attribute.
        saves_session: If True, mark session dirty after execution to persist cookies.

    Returns:
        Decorated function or class with _command_meta or _command_group_meta attached.

    Example (function):
        @command(help="List all accounts")
        def accounts(self, ctx: CommandContext) -> dict:
            return ctx.session.get("https://api.example.com/accounts").json()

    Example (class / command group):
        @command(help="Account management")
        class Accounts:
            @command(help="List statements")
            def statements(self, ctx: CommandContext) -> dict:
                ...
    """

    def decorator(target: Any) -> Any:
        if isinstance(target, type):
            # Class -> command group
            target._command_group_meta = CommandGroupMeta(
                name=_to_cli_name(target.__name__),
                help_text=help,
                parent=parent,
            )
            # Auto-discover methods: attach default metadata to non-underscore callables
            for attr_name in list(vars(target)):
                if attr_name.startswith("_"):
                    continue
                attr = getattr(target, attr_name, None)
                if callable(attr) and not hasattr(attr, "_command_meta"):
                    attr._command_meta = CommandMetadata(
                        name=attr_name,
                        params=(),
                        parent=None,
                        requires_session=None,
                    )
            return target
        else:
            # Function -> command
            click_kw: dict[str, Any] = {}
            if help:
                click_kw["help"] = help
            target._command_meta = CommandMetadata(
                name=target.__name__,
                params=tuple(params) if params else (),
                parent=parent,
                requires_session=requires_session,
                saves_session=saves_session,
                click_kwargs=click_kw,
            )
            return target

    return decorator


class SitePlugin:
    """Base class for Python-based site plugins.

    Subclass this and use the @command decorator to define commands.
    Commands receive a CommandContext with a requests.Session (cookies/headers
    pre-loaded from the cached graftpunk session) plus plugin metadata.

    Example:
        class MyBankPlugin(SitePlugin):
            site_name = "mybank"
            session_name = "mybank"
            help_text = "Commands for MyBank API"

            @command(help="List accounts")
            def accounts(self, ctx: CommandContext) -> dict:
                return ctx.session.get("https://mybank.com/api/accounts").json()

            @command(help="Get statements")
            def statements(self, ctx: CommandContext, month: str) -> dict:
                return ctx.session.get(f"https://mybank.com/api/statements/{month}").json()
    """

    site_name: str = ""
    session_name: str = ""
    help_text: str = ""
    requires_session: bool = True
    api_version: int = 1
    username_envvar: str = ""
    password_envvar: str = ""

    # Declarative login configuration
    base_url: str = ""
    backend: Literal["selenium", "nodriver"] = "selenium"
    login_config: LoginConfig | None = None
    token_config: TokenConfig | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Apply shared defaults and store canonical config via metaclass behavior.

        Automatically called when a subclass of SitePlugin is defined.
        Calls build_plugin_config() to handle site_name inference,
        session_name defaulting, help_text generation, and validation.
        Writes inferred values back to class attributes.

        If configuration is incomplete (no site_name or base_url),
        the error is suppressed -- validation occurs at registration time.

        Args:
            **kwargs: Keyword arguments passed to super().__init_subclass__().
        """
        super().__init_subclass__(**kwargs)

        # Auto-construct LoginConfig from flat attrs if present
        flat_login_url = cls.__dict__.get("login_url", "")
        flat_login_fields = getattr(cls, "login_fields", None)
        flat_login_submit = cls.__dict__.get("login_submit", "")
        if flat_login_url and flat_login_fields and flat_login_submit:
            # Copy inherited login_fields to avoid shared mutable state
            if "login_fields" not in cls.__dict__:
                flat_login_fields = dict(flat_login_fields)
            cls.login_config = LoginConfig(
                url=flat_login_url,
                fields=dict(flat_login_fields),  # fresh copy
                submit=flat_login_submit,
                failure=cls.__dict__.get("login_failure", ""),
                success=cls.__dict__.get("login_success", ""),
            )

        # Extract non-private, non-callable class attributes
        raw = {
            k: v
            for k, v in cls.__dict__.items()
            if not k.startswith("_")
            and not callable(v)
            and not isinstance(v, (classmethod, staticmethod, property))
        }

        # Build config (may fail for incomplete plugins -- that's OK,
        # they'll be caught at registration time)
        try:
            cls._plugin_config = build_plugin_config(**raw)
            # Write back inferred/defaulted values
            cls.site_name = cls._plugin_config.site_name
            cls.session_name = cls._plugin_config.session_name
            cls.help_text = cls._plugin_config.help_text
        except PluginError as exc:
            cls._plugin_config_error = exc
            LOG.warning("plugin_config_deferred", class_name=cls.__name__, error=str(exc))

    def setup(self) -> None:
        """Called once after the plugin is registered successfully.

        Override to perform initialization (validate config, open connections,
        warm caches). Exceptions here are caught and reported as registration
        errors — the plugin is skipped but other plugins continue.
        """

    def teardown(self) -> None:
        """Called during application shutdown.

        Override to clean up resources (close connections, flush buffers).
        Exceptions here are logged but do not propagate — all plugins
        get their teardown called regardless of individual failures.
        """

    def _resolve_params(self, meta: CommandMetadata, handler: Any) -> tuple[PluginParamSpec, ...]:
        """Return explicit params from metadata, or auto-discover from handler signature."""
        return meta.params if meta.params else tuple(self._introspect_params(handler))

    def _build_command_spec(
        self,
        meta: CommandMetadata,
        handler: Any,
        group: str | None = None,
    ) -> CommandSpec:
        """Build a CommandSpec from metadata and a handler callable."""
        return CommandSpec(
            name=meta.name,
            handler=handler,
            params=self._resolve_params(meta, handler),
            requires_session=meta.requires_session,
            group=group,
            saves_session=meta.saves_session,
            click_kwargs=dict(meta.click_kwargs),
        )

    def get_commands(self) -> list[CommandSpec]:
        """Discover all @command decorated methods and command groups.

        Scans the plugin instance for:
        1. Methods decorated with @command (top-level commands)
        2. Classes decorated with @command in the plugin's module (command groups)

        Returns:
            List of CommandSpec objects representing all available commands.
        """
        commands: list[CommandSpec] = []

        # 1. Discover @command-decorated methods on self (group=None, no parent)
        for attr_name in dir(self):
            if attr_name.startswith("_"):
                continue
            attr = getattr(self, attr_name)
            meta: CommandMetadata | None = getattr(attr, "_command_meta", None)
            if callable(attr) and meta is not None and meta.parent is None:
                commands.append(self._build_command_spec(meta, attr))

        # 2. Discover command groups from the module where this plugin is defined
        commands.extend(self._discover_command_groups())

        return commands

    def _discover_command_groups(self) -> list[CommandSpec]:
        """Walk @command-decorated classes in this plugin's module and produce CommandSpecs."""
        plugin_module = sys.modules.get(type(self).__module__)
        if plugin_module is None:
            return []

        # Find all command group classes in the module
        group_classes: dict[type, CommandGroupMeta] = {}
        for _name, obj in inspect.getmembers(plugin_module, inspect.isclass):
            meta = getattr(obj, "_command_group_meta", None)
            if meta is not None:
                group_classes[obj] = meta

        if not group_classes:
            return []

        # Build group path map: class -> dotted path
        group_paths: dict[type, str] = {}

        def resolve_path(cls: type) -> str:
            if cls in group_paths:
                return group_paths[cls]
            meta = group_classes[cls]
            if meta.parent is not None and meta.parent in group_classes:
                parent_path = resolve_path(meta.parent)
                group_paths[cls] = f"{parent_path}.{meta.name}"
            else:
                group_paths[cls] = meta.name
            return group_paths[cls]

        for cls in group_classes:
            resolve_path(cls)

        # Produce CommandSpecs from group methods
        commands: list[CommandSpec] = []
        for cls in group_classes:
            instance = cls()
            group_path = group_paths[cls]
            for method_name in vars(cls):
                if method_name.startswith("_"):
                    continue
                method = getattr(instance, method_name, None)
                if method is None or not callable(method):
                    continue
                method_meta: CommandMetadata | None = getattr(method, "_command_meta", None)
                if method_meta is None:
                    continue
                commands.append(self._build_command_spec(method_meta, method, group=group_path))

        # Also handle functions with parent= references
        for _name, obj in inspect.getmembers(plugin_module, inspect.isfunction):
            func_meta: CommandMetadata | None = getattr(obj, "_command_meta", None)
            if (
                func_meta is not None
                and func_meta.parent is not None
                and func_meta.parent in group_classes
            ):
                parent_path = group_paths[func_meta.parent]
                commands.append(self._build_command_spec(func_meta, obj, group=parent_path))

        return commands

    def _introspect_params(self, method: Any) -> list[PluginParamSpec]:
        """Extract CLI options from method signature and type hints.

        All introspected parameters become **options** (``--flag`` style).
        Use explicit ``params=`` on the ``@command`` decorator to declare
        positional arguments.

        Uses :meth:`PluginParamSpec.option` so that bool-flag detection
        and other defaults are defined in exactly one place.

        Args:
            method: The method to introspect.

        Returns:
            List of PluginParamSpec from the method's parameters.
        """
        params: list[PluginParamSpec] = []
        sig = inspect.signature(method)

        for name, param in sig.parameters.items():
            # Skip self and ctx (injected by framework)
            if name in ("self", "ctx"):
                continue

            # Determine type (use actual type object, not string)
            param_type: type = str
            if param.annotation != inspect.Parameter.empty and param.annotation in (
                int,
                float,
                bool,
                str,
            ):
                param_type = param.annotation

            # Determine if required and default
            has_default = param.default != inspect.Parameter.empty
            default = param.default if has_default else None
            required = not has_default

            params.append(
                PluginParamSpec.option(
                    name,
                    type=param_type,
                    required=required,
                    default=default,
                )
            )

        return params

    @asynccontextmanager
    async def browser_session(self) -> AsyncIterator[tuple[Any, Any]]:
        """Async context manager for nodriver browser sessions.

        Handles browser creation, startup, cookie transfer, caching, and cleanup.
        On success (no exception), transfers cookies and caches session.
        On exception, quits browser without caching.

        Yields:
            tuple[BrowserSession, Tab]: The browser session and active tab.
                Tab is already navigated to self.base_url/.

        Usage:
            async with self.browser_session() as (session, tab):
                # tab is already at self.base_url/
                await tab.get(f"{self.base_url}/login")
                # ... custom login logic ...
        """
        from graftpunk import BrowserSession, cache_session

        session = BrowserSession(backend="nodriver", headless=False)
        await session.start_async()
        tab = await session.driver.get(f"{self.base_url}/")
        try:
            yield session, tab
            # Capture current URL before caching (used for domain display)
            try:
                session.current_url = getattr(tab, "url", "") or f"{self.base_url}/"
            except Exception:  # noqa: BLE001
                session.current_url = f"{self.base_url}/"
            # Success path: transfer cookies and cache
            await session.transfer_nodriver_cookies_to_session()
            cache_session(session, self.session_name)
        finally:
            try:
                session.driver.stop()
            except Exception as cleanup_exc:
                LOG.exception("browser_session_cleanup_failed", error=str(cleanup_exc))

    @contextmanager
    def browser_session_sync(self) -> Iterator[tuple[Any, Any]]:
        """Sync context manager for selenium browser sessions.

        Handles browser creation, cookie transfer, caching, and cleanup.
        On success (no exception), transfers cookies and caches session.
        On exception, quits browser without caching.

        Note: Unlike browser_session(), this does not navigate to base_url
        before yielding. The caller must navigate manually.

        Yields:
            tuple[BrowserSession, WebDriver]: The browser session and selenium driver.
                Caller must navigate to the target URL.

        Usage:
            with self.browser_session_sync() as (session, driver):
                driver.get(f"{self.base_url}/login")
                # ... custom login logic ...
        """
        from graftpunk import BrowserSession, cache_session

        session = BrowserSession(backend="selenium", headless=False)
        try:
            yield session, session.driver
            # Capture current URL before caching (used for domain display)
            try:
                session.current_url = session.driver.current_url
            except Exception:  # noqa: BLE001
                session.current_url = f"{self.base_url}/"
            # Success path: transfer cookies and cache
            session.transfer_driver_cookies_to_session()
            cache_session(session, self.session_name)
        finally:
            try:
                session.quit()
            except Exception as cleanup_exc:
                LOG.exception("browser_session_cleanup_failed", error=str(cleanup_exc))

    def get_session(self) -> requests.Session:
        """Load the graftpunk session for API calls.

        If requires_session is False, returns a plain requests.Session.
        """
        if not self.requires_session:
            return requests.Session()
        return load_session_for_api(self.session_name)


def has_declarative_login(plugin: CLIPluginProtocol) -> bool:
    """Check if a plugin has declarative login configuration.

    Args:
        plugin: Plugin instance to check.

    Returns:
        True if the plugin has a valid LoginConfig, False otherwise.
    """
    login = getattr(plugin, "login_config", None)
    return login is not None and isinstance(login, LoginConfig)
