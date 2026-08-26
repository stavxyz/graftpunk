"""Login command generation for plugins.

Creates auto-generated 'login' CLI commands from plugin login() methods
or declarative LoginConfig.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

import typer
from rich.status import Status

from graftpunk import console as gp_console
from graftpunk import workstation_env
from graftpunk.logging import get_logger
from graftpunk.plugins.cli_plugin import (
    CLIPluginProtocol,
    LoginConfig,
    has_declarative_login,
)
from graftpunk.plugins.login_engine import generate_login_method

LOG = get_logger(__name__)

# Field-name keywords that mark a credential as secret. Shared policy:
# make_login_body masks prompts with it, and gp config's set guardrail
# (cli/config_commands.py) warns on literal values for such names.
SECRET_KEYWORDS = frozenset({"password", "secret", "token", "key"})


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
        login: Callable[..., Any] | None = getattr(plugin, "login", None)
        assert callable(login)  # has_login_method checked this
        return login
    if has_declarative_login(plugin):
        login_func = generate_login_method(plugin)
        LOG.debug("declarative_login_generated", plugin=plugin.site_name)
        return login_func
    return None


def resolve_login_fields(plugin: CLIPluginProtocol) -> dict[str, str]:
    """Return the login credential fields for a plugin.

    Aggregates fields from all steps in the ``login_config``. Returns a dict
    mapping credential names to CSS selectors. If no login_config exists or
    no fields are defined, defaults to ``{"username": "", "password": ""}``
    with empty selector values (credential names only for prompting).

    Args:
        plugin: Plugin instance to inspect.

    Returns:
        Dictionary mapping credential field names to CSS selectors
        (or empty strings if using default fields).
    """
    login_cfg = getattr(plugin, "login_config", None)
    if isinstance(login_cfg, LoginConfig) and login_cfg.steps:
        all_fields = {k: v for step in login_cfg.steps for k, v in step.fields.items()}
        if all_fields:
            return all_fields
    LOG.info(
        "login_fields_default_assumed",
        plugin=plugin.site_name,
        hint="No login fields configured. Defaulting to username/password.",
    )
    return {"username": "", "password": ""}


def _observe_mode_from_ctx(ctx: Any) -> str:
    """Read ``--observe`` from the root Typer context; "off" when unavailable."""
    try:
        return str((ctx.find_root().obj or {}).get("observe_mode", "off"))
    except AttributeError:
        return "off"


def _accepted_login_kwargs(login_callable: Callable[..., Any], **candidates: Any) -> dict[str, Any]:
    """Keep only the kwargs ``login_callable`` declares as optional (or ``**kwargs``).

    Generated declarative logins accept ``headless`` and ``observe_mode``; a
    plugin's hand-written ``login(credentials)`` usually does not, and must
    not receive arguments it never asked for. A same-named *required*
    positional parameter does not count either: it would receive ``None``
    for an unset flag, which is not what such a signature means.
    """
    try:
        params = inspect.signature(login_callable).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(candidates)
    return {
        k: v
        for k, v in candidates.items()
        if k in params
        and (
            params[k].kind is inspect.Parameter.KEYWORD_ONLY
            or params[k].default is not inspect.Parameter.empty
        )
    }


def _supports_headless(login_callable: Callable[..., Any]) -> bool:
    """True when the login callable takes a ``headless`` kwarg (generated logins do)."""
    return "headless" in _accepted_login_kwargs(login_callable, headless=None)


def make_login_body(
    plugin: CLIPluginProtocol,
    login_callable: Callable[..., Any],
    fields: dict[str, str],
) -> Callable[..., None]:
    """Build the login command BODY: credential resolution -> login callable.

    Credential resolution order: environment variables ({SITE_PREFIX}_{FIELD}
    or plugin-level username_envvar/password_envvar overrides), then the
    workstation env file (static or $(…) command values, resolved lazily), then
    interactive prompts (masked for password/secret/token/key fields).
    The resolved credentials dict is passed INTO the login callable.
    """
    secret_keywords = SECRET_KEYWORDS

    envvar_overrides: dict[str, str] = {}
    username_envvar = getattr(plugin, "username_envvar", "")
    password_envvar = getattr(plugin, "password_envvar", "")
    if username_envvar:
        envvar_overrides["username"] = username_envvar
    if password_envvar:
        envvar_overrides["password"] = password_envvar

    def body(ctx: typer.Context, **kwargs: Any) -> None:
        login_method = login_callable
        # --headless -> True, --headful -> False, neither -> None (LoginConfig decides).
        want_headless = bool(kwargs.pop("headless", False))
        want_headful = bool(kwargs.pop("headful", False))
        if want_headless and want_headful:
            raise typer.BadParameter("--headless and --headful are mutually exclusive")
        headless_override: bool | None = (
            True if want_headless else (False if want_headful else None)
        )
        login_kwargs = _accepted_login_kwargs(
            login_method,
            headless=headless_override,
            observe_mode=_observe_mode_from_ctx(ctx),
        )
        credentials: dict[str, str] = {}
        site_prefix = plugin.site_name.upper().replace("-", "_").replace(" ", "_")

        for field_name in fields:
            is_secret = any(kw in field_name.lower() for kw in secret_keywords)
            envvar = envvar_overrides.get(field_name, f"{site_prefix}_{field_name.upper()}")

            # env -> workstation file, via the single precedence-owning
            # lookup() (empty-string env counts as a miss and falls through
            # to the file tier — deliberate change from the old
            # login_envvar_empty behavior). Command values (e.g.
            # $(op read ...)) execute here, at login time only.
            env_value = workstation_env.load().lookup(envvar)
            if env_value:
                credentials[field_name] = env_value
            else:
                credentials[field_name] = typer.prompt(
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
                        result = asyncio.run(login_method(credentials, **login_kwargs))
                else:
                    result = login_method(credentials, **login_kwargs)

            if result is False:
                # The engine logged what it actually detected (failure text,
                # missing success element, rate limiting, a field that never
                # accepted input). Do not name a cause that was not measured.
                gp_console.error(
                    f"Login did not complete for {plugin.site_name}: the page did not "
                    "reach the expected post-login state. See the warning above for "
                    "what was detected. Re-run with --observe=full to capture the flow."
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

    return body


def create_login_fn(
    plugin: CLIPluginProtocol,
    login_callable: Callable[..., Any],
    fields: dict[str, str],
) -> Callable[..., None]:
    """Synthesize the ``login`` command function.

    Credentials are gathered at runtime (env, workstation file, prompts), so
    the only parameters are ``--headless`` / ``--headful``, and those only
    when the login callable can honour them (generated declarative logins;
    a hand-written ``login(credentials)`` gets neither, rather than an
    advertised flag that silently does nothing).
    """
    from graftpunk.cli.command_factory import synthesize_command_fn
    from graftpunk.plugins.cli_plugin import PluginParamSpec

    if getattr(login_callable, "_gp_generated_login", False):
        # The generated login's docstring describes the engine, not the site.
        help_text = f"Log in to {plugin.site_name}"
    else:
        help_text = inspect.getdoc(login_callable) or f"Log in to {plugin.site_name}"
        help_text = help_text.split("\n")[0]

    param_specs: list[PluginParamSpec] = []
    if _supports_headless(login_callable):
        param_specs = [
            PluginParamSpec.option(
                "headless",
                type=bool,
                default=False,
                help="Run the login browser headless (overrides LoginConfig.headless).",
            ),
            PluginParamSpec.option(
                "headful",
                type=bool,
                default=False,
                help="Show the browser window even if LoginConfig.headless is true "
                "(e.g. to solve a CAPTCHA or 2FA prompt).",
            ),
        ]
    return synthesize_command_fn(
        name="login",
        param_specs=param_specs,
        body=make_login_body(plugin, login_callable, fields),
        plugin_name=plugin.site_name,
        include_builtin_options=False,
        help_text=help_text,
    )
