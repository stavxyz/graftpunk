"""Login command generation for plugins.

Creates auto-generated 'login' CLI commands from plugin login() methods
or declarative LoginConfig.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import typer
from rich.status import Status

from graftpunk import console as gp_console
from graftpunk import workstation_env
from graftpunk.cache import get_session_metadata
from graftpunk.logging import get_logger
from graftpunk.plugins.cli_plugin import (
    CLIPluginProtocol,
    LoginConfig,
    has_declarative_login,
)
from graftpunk.plugins.login_engine import generate_login_method
from graftpunk.session_context import get_active_session
from graftpunk.session_identity import (
    GP_ACCOUNT_ATTR,
    derive_account_identity,
    join_session_name,
    validate_account_label,
)

LOG = get_logger(__name__)

# Field-name keywords that mark a credential as secret. Shared policy:
# make_login_body masks prompts with it, and gp config's set guardrail
# (cli/config_commands.py) warns on literal values for such names.
SECRET_KEYWORDS = frozenset({"password", "secret", "token", "key"})

# "attribute absent" sentinel for the login stamp's save/restore (None is a
# legitimate stored value, so it cannot mark absence).
_MISSING = object()


def login_method(plugin: CLIPluginProtocol) -> Callable[..., Any] | None:
    """Return the plugin's user-defined ``login()`` method, or None.

    A plugin's 'login' attribute counts only when it is callable and NOT
    decorated with @command (i.e., not already exposed as a CLI command).
    After the login_config rename, 'login' on a plugin is only ever a method.
    """
    login_attr = getattr(plugin, "login", None)
    if not callable(login_attr) or hasattr(login_attr, "_command_meta"):
        return None
    return login_attr


def has_login_method(plugin: CLIPluginProtocol) -> bool:
    """Check if plugin has a login method that's not a CLI command."""
    return login_method(plugin) is not None


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
    login = login_method(plugin)
    if login is not None:
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


@contextmanager
def _stamp_login_identity(
    plugin: CLIPluginProtocol, label: str | None, identifier: str | None
) -> Iterator[None]:
    """Stamp the plugin instance for the duration of one login flow.

    Confined transport: only the author-facing paths read this state --
    hand-written ``login(credentials)`` methods and the ``browser_session``
    helpers, whose API shape reads ``self.session_name``. Engine-controlled
    paths receive the name and identifier as explicit arguments and never
    depend on the stamp. Instance attributes shadow the class attributes;
    on exit the prior state is restored, so a plugin instance held across a
    login cannot leak the stamped identity into later reads. Retirement of
    this dual channel is tracked by #174.

    ``CLIPluginProtocol`` declares ``session_name`` as a read-only property,
    so a protocol-literal plugin can reject the name stamp; that degrades to
    a warning (the identifier still travels explicitly to the generated
    flows) rather than failing the login.
    """
    if not (label or identifier):
        yield
        return
    # Capture BEFORE mutating, with a missing-sentinel: a plugin that set
    # self.session_name in __init__ gets its own value back, not the class's.
    prior_name = plugin.__dict__.get("session_name", _MISSING)
    prior_attr = plugin.__dict__.get(GP_ACCOUNT_ATTR, _MISSING)
    stamped_name = False
    if label:
        try:
            # setattr, not attribute assignment: the protocol declares
            # session_name read-only, and a plugin may really implement it
            # that way — hence the guard below.
            setattr(plugin, "session_name", join_session_name(plugin.session_name, label))  # noqa: B010
            stamped_name = True
        except AttributeError:
            LOG.warning(
                "login_stamp_skipped_readonly_session_name",
                plugin=plugin.site_name,
                label=label,
            )
    if identifier is not None:
        setattr(plugin, GP_ACCOUNT_ATTR, identifier)
    try:
        yield
    finally:
        if stamped_name:
            if prior_name is _MISSING:
                plugin.__dict__.pop("session_name", None)
            else:
                setattr(plugin, "session_name", prior_name)  # noqa: B010
        if prior_attr is _MISSING:
            plugin.__dict__.pop(GP_ACCOUNT_ATTR, None)
        else:
            setattr(plugin, GP_ACCOUNT_ATTR, prior_attr)


def _warn_if_slot_changes_hands_post(
    stored: dict[str, Any] | None, incoming_identifier: str | None, session_name: str
) -> None:
    """Warn when a successful login overwrote a slot recorded for another account.

    The pure compare/emit half: ``make_login_body`` performs the ONE metadata
    fetch before the login attempt and calls this only after success, so the
    fetch/emit split is the final shape from the start. Both identifiers must
    be present and unequal; a missing identifier on either side never warns
    (legacy slots, refresh writes). Note ``get_session_metadata`` returns
    ``dict | None`` (cache.py:222) -- read with ``.get``, never ``getattr``.
    """
    if not incoming_identifier:
        return
    stored_id = stored.get("account_identifier") if stored else None
    if stored_id and stored_id != incoming_identifier:
        LOG.warning(
            "session_slot_changing_account",
            session=session_name,
            stored=stored_id,
            incoming=incoming_identifier,
        )
        gp_console.warn(
            f"Session '{session_name}' was recorded for {stored_id}; this login "
            f"replaces it as {incoming_identifier}."
        )


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
        # Validate before any prompting: a typo in --as must not cost the
        # user a password entry first.
        as_label: str = kwargs.pop("as_label", "") or ""
        if as_label:
            try:
                validate_account_label(as_label)
            except ValueError as exc:
                raise typer.BadParameter(str(exc)) from exc
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

        # The operating identity: derived from the RESOLVED credentials, so
        # the kwarg filter runs here rather than before the loop.
        identifier, derived_label = derive_account_identity(credentials, secret_keywords)
        label = as_label or derived_label
        target_name = (
            join_session_name(plugin.session_name, label) if label else plugin.session_name
        )
        login_kwargs = _accepted_login_kwargs(
            login_method,
            headless=headless_override,
            observe_mode=_observe_mode_from_ctx(ctx),
            session_name=target_name,
            account_identifier=identifier,
        )
        # The ONE metadata fetch, before the attempt; the comparison and the
        # emission happen only after a successful login. A storage hiccup
        # must not stop a login over an advisory warning.
        try:
            stored_before = get_session_metadata(target_name)
        except Exception as exc:  # noqa: BLE001 — advisory read; never blocks login
            LOG.warning(
                "session_metadata_lookup_failed",
                session=target_name,
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            stored_before = None

        try:
            with (
                Status("Logging in...", console=gp_console.err_console),
                _stamp_login_identity(plugin, label, identifier),
            ):
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

        # Advisory only, and only after a SUCCESSFUL login: the verdict above is
        # already sealed (a failure exited), so nothing here can turn a cached
        # login into "Login failed". get_active_session() reads the environment
        # (cwd, .gp-session) and can raise on a removed cwd or an unreadable
        # file; that must cost the user a hint, not the login.
        try:
            _warn_if_slot_changes_hands_post(stored_before, identifier, target_name)
            # What the CLI computed — a hand-written login is free to cache
            # under a literal name of its own, so this is not a claim about
            # what landed in the cache.
            gp_console.info(f"Session name: {target_name}")
            current = get_active_session()
            if current and current != target_name:
                gp_console.info(
                    f"This shell is pinned to {current} — "
                    f"run: gp session use {target_name} to switch"
                )
        except Exception as exc:  # noqa: BLE001 — advisory output; the login already succeeded
            LOG.warning(
                "post_login_advisory_failed",
                plugin=plugin.site_name,
                session=target_name,
                error=str(exc),
                exc_type=type(exc).__name__,
            )

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

    # --as is offered by EVERY generated login command: the label names the
    # cached session, which the CLI (not the login callable) decides, so a
    # hand-written login(credentials) honours it just as a generated one does.
    param_specs: list[PluginParamSpec] = [
        PluginParamSpec.option(
            "as_label",
            type=str,
            default="",
            help="Account label for the cached session "
            "(default: derived from the login identifier)",
            click_kwargs={"flag": "--as"},
        )
    ]
    if _supports_headless(login_callable):
        param_specs += [
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
