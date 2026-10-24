"""Login command generation for plugins.

Creates auto-generated 'login' CLI commands from plugin login() methods
or declarative LoginConfig.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.status import Status

from graftpunk import _distribution, workstation_env
from graftpunk import console as gp_console
from graftpunk.cache import get_session_metadata, list_sessions
from graftpunk.cli.errors import exit_missing_browser_stack
from graftpunk.logging import get_logger
from graftpunk.observe import OBSERVE_BASE_DIR
from graftpunk.plugins.cli_plugin import (
    CLIPluginProtocol,
    LoginConfig,
    has_declarative_login,
)
from graftpunk.plugins.login_engine import generate_login_method
from graftpunk.session_context import SESSION_FILE_NAME, get_active_session
from graftpunk.session_identity import (
    derive_account_identity,
    join_session_name,
    split_session_name,
    validate_account_label,
)
from graftpunk.session_migration import (
    LegacySlotMigration,
    earlier_name,
    finish_superseded_slot,
    hidden_name,
    is_legacy_secret_slot,
    migrate_legacy_account_slot,
    plugin_secret_field,
    records_legacy_secret,
    replace_legacy_record,
)
from graftpunk.session_scope import operating_session

LOG = get_logger(__name__)

# Field-name keywords that mark a credential as secret. Shared policy:
# make_login_body masks prompts with it, and gp config's set guardrail
# (cli/config_commands.py) warns on literal values for such names.
SECRET_KEYWORDS = frozenset({"password", "secret", "token", "key"})


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


def legacy_secret_field_of(plugin: CLIPluginProtocol) -> str | None:
    """The secret field graftpunk 1.24.0 named *plugin*'s sessions after, if any (#293).

    Worked out from the login field names (:func:`resolve_login_fields`).
    None when 1.24.0 labelled them from an identifier field, or from none.
    """
    return plugin_secret_field(resolve_login_fields(plugin), SECRET_KEYWORDS)


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


def _warn_if_slot_changes_hands_post(
    stored: dict[str, Any] | None, incoming_identifier: str | None, session_name: str
) -> None:
    """Warn when a successful login overwrote a slot recorded for another account.

    The pure compare/emit half: ``make_login_body`` performs the one
    pre-attempt metadata fetch before the login attempt and calls this only
    after success, so the fetch/emit split is the final shape from the start.
    Both identifiers must
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


def credential_envvar(plugin: CLIPluginProtocol, field_name: str) -> str:
    """The environment variable a login credential field is read from.

    ``username_envvar`` / ``password_envvar`` on the plugin name their own
    variable; every other field (and those two when unset) is
    ``{SITE_PREFIX}_{FIELD}``.
    """
    overrides = {
        "username": getattr(plugin, "username_envvar", ""),
        "password": getattr(plugin, "password_envvar", ""),
    }
    override = overrides.get(field_name)
    if override:
        return override
    site_prefix = plugin.site_name.upper().replace("-", "_").replace(" ", "_")
    return f"{site_prefix}_{field_name.upper()}"


def known_credentials(plugin: CLIPluginProtocol) -> dict[str, str]:
    """The plugin's login credentials gp can read without running anything.

    For each login field (:func:`resolve_login_fields`), the value its
    environment variable holds, else the workstation env file's static value
    for that name; a field with neither is left out. A ``$(...)`` command
    value is not run: outside a login it could ask for a password or a
    biometric approval, so a credential kept that way is not known here. Used
    to scrub a plugin command's observe run as a login run is scrubbed
    (:meth:`graftpunk.observe.run.Redaction.from_credentials`).
    """
    env_file = workstation_env.load()
    values: dict[str, str] = {}
    for field_name in resolve_login_fields(plugin):
        name = credential_envvar(plugin, field_name)
        value = os.environ.get(name)
        if not value:
            entry = env_file.get(name)
            if entry is not None and entry.kind == workstation_env.STATIC:
                value = entry.raw_value
        if value:
            values[field_name] = value
    return values


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

    # A login generated from login_config drives a browser; a hand-written
    # login() may be plain requests, so only the generated one is checked.
    generated = login_method(plugin) is None and has_declarative_login(plugin)

    def body(ctx: typer.Context, **kwargs: Any) -> None:
        if generated:
            # Before any prompt or workstation env lookup: a $(...) credential
            # command must not run, nor a password be typed, for a login that
            # cannot start.
            absent = _distribution.absent_browser_module(getattr(plugin, "backend", ""))
            if absent is not None:
                exit_missing_browser_stack(ctx.command_path, absent)
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

        for field_name in fields:
            is_secret = any(kw in field_name.lower() for kw in secret_keywords)
            envvar = credential_envvar(plugin, field_name)

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
        identifier, derived_label = derive_account_identity(credentials)
        label = as_label or derived_label
        target_name = (
            join_session_name(plugin.session_name, label) if label else plugin.session_name
        )
        migration = _migrate_legacy_slot(plugin, credentials, target_name, identifier)
        login_kwargs = _accepted_login_kwargs(
            login_method,
            headless=headless_override,
            observe_mode=_observe_mode_from_ctx(ctx),
            session_name=target_name,
            account_identifier=identifier,
        )
        # The one pre-attempt metadata fetch; the comparison and the
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
                operating_session(target_name, identifier),
            ):
                result = login_method(credentials, **login_kwargs)
                # An awaitable back means an async login, whatever shape the callable
                # had (an async def, a sync wrapper or callable object around one, or
                # a custom awaitable), so it is run here rather than read as an
                # unexpected return.
                if inspect.isawaitable(result):
                    # Suppress asyncio "Loop ... is closed" warning that fires when
                    # asyncio.run() closes the event loop while nodriver's subprocess
                    # handlers are still pending. Suppression covers the entire
                    # asyncio.run() call because the warning fires during shutdown,
                    # which is inseparable from the run() call itself.
                    from graftpunk.logging import suppress_asyncio_noise

                    with suppress_asyncio_noise():
                        result = asyncio.run(_awaited(result))

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
            module = _distribution.missing_browser_module(exc)
            if module is not None:
                exit_missing_browser_stack(ctx.command_path, module)
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
        # login into "Login failed". Each advisory gets its own try, so a
        # storage hiccup in one costs that hint alone.
        landed = False
        legacy_record = _holds_legacy_record(plugin, target_name, stored_before)
        try:
            # A 1.24.0 record of a secret is replaced below; naming it in a
            # "recorded for" warning would print the secret (#293).
            if not legacy_record:
                _warn_if_slot_changes_hands_post(stored_before, identifier, target_name)
            # What the CLI computed, not what landed: compare the slot across
            # the login, since the one pre-attempt fetch already captured it.
            # Existence alone would not answer it: a hand-written login caching
            # under self.session_name by hand now writes the bare base (#174),
            # and if the target slot happened to exist already from an earlier
            # login, an existence test would stay silent while the user keeps
            # using a stale session. A pre-attempt fetch that failed leaves
            # stored_before None; a slot that exists afterwards is then taken as
            # landed, because nothing here can prove otherwise.
            gp_console.info(f"Session name: {target_name}")
            stored_after = get_session_metadata(target_name)
            landed = stored_after is not None and (
                stored_before is None
                or stored_after.get("modified_at") != stored_before.get("modified_at")
            )
            if not landed:
                LOG.warning(
                    "login_cached_outside_target_slot",
                    plugin=plugin.site_name,
                    target=target_name,
                )
                gp_console.warn(
                    f"Nothing was written to '{target_name}' by this login. If it "
                    "caches by hand, call cache_login_session(self, session) or "
                    "accept session_name and account_identifier keyword arguments."
                )
        except Exception as exc:  # noqa: BLE001 (advisory output: the login already succeeded)
            LOG.warning(
                "post_login_advisory_failed",
                stage="target_slot_check",
                plugin=plugin.site_name,
                session=target_name,
                error=str(exc),
                exc_type=type(exc).__name__,
            )

        if migration is not None and migration.action == "superseded":
            migration = _finish_superseded(plugin, migration, landed=landed)
        if legacy_record:
            _replace_legacy_record(plugin, target_name, identifier)

        # get_active_session() reads the environment (cwd, .gp-session) and can
        # raise on a removed cwd or an unreadable file.
        try:
            current = get_active_session()
            if current and current != target_name:
                current = _shown_pin(plugin, current, migration)
                gp_console.info(
                    f"This shell is pinned to {current} — "
                    f"run: gp session use {target_name} to switch"
                )
        except Exception as exc:  # noqa: BLE001 (advisory output: never blocks a login)
            LOG.warning(
                "post_login_advisory_failed",
                stage="ambient_pin_hint",
                plugin=plugin.site_name,
                session=target_name,
                error=str(exc),
                exc_type=type(exc).__name__,
            )

    return body


def _migrate_legacy_slot(
    plugin: CLIPluginProtocol,
    credentials: dict[str, str],
    target_name: str,
    identifier: str | None,
) -> LegacySlotMigration | None:
    """Deal with the slot graftpunk 1.24.0 named after a secret for these credentials (#293).

    Runs before the login attempt (see
    :func:`graftpunk.session_migration.migrate_legacy_account_slot`); a stale
    slot that must wait for the login is finished by :func:`_finish_superseded`.
    Advisory: a failure is logged and the login goes on. The old name is never
    printed or logged, since it holds the secret.
    """
    try:
        migration = migrate_legacy_account_slot(
            plugin.session_name,
            credentials,
            SECRET_KEYWORDS,
            target_name=target_name,
            identifier=identifier,
            observe_base_dir=OBSERVE_BASE_DIR,
            environ=os.environ,
            cwd=Path.cwd(),
            existing_names=list_sessions,
        )
    except Exception as exc:  # noqa: BLE001 (a migration hiccup must not block a login)
        LOG.warning(
            "legacy_account_slot_migration_failed",
            plugin=plugin.site_name,
            session=target_name,
            exc_type=type(exc).__name__,
        )
        return None
    if migration is not None:
        _report_before_login(migration)
    return migration


def _finish_superseded(
    plugin: CLIPluginProtocol, migration: LegacySlotMigration, *, landed: bool
) -> LegacySlotMigration:
    """Delete a superseded stale slot once the login has written its slot (#293). Advisory."""
    try:
        finished = finish_superseded_slot(
            migration, landed=landed, observe_base_dir=OBSERVE_BASE_DIR, cwd=Path.cwd()
        )
    except Exception as exc:  # noqa: BLE001 (advisory: the login already succeeded)
        LOG.warning(
            "legacy_account_slot_migration_failed",
            plugin=plugin.site_name,
            session=migration.new_name,
            exc_type=type(exc).__name__,
        )
        return migration
    _report_slot_outcome(finished)
    return finished


def _report_before_login(migration: LegacySlotMigration) -> None:
    """What the pre-login step did, naming the field and never the old name."""
    target_name = migration.new_name
    source = f"the {migration.source_field} field, which is not an account identifier"
    if migration.action == "superseded":
        if migration.problem:
            gp_console.warn(migration.problem)
    else:
        _report_slot_outcome(migration)
    if migration.leftovers:
        base = split_session_name(target_name)[0]
        one = migration.leftovers == 1
        count = "Another session" if one else f"{migration.leftovers} sessions"
        gp_console.warn(
            f"{count} of {base} cached by graftpunk 1.24.0 or earlier "
            f"{'is' if one else 'are'} named after {source}, and gp cannot tell which "
            f"account {'it belongs' if one else 'each belongs'} to, so it leaves "
            f"{'it' if one else 'them'} alone. To remove one you no longer use, find its "
            "name with: gp session list, then run: gp session clear <name>"
        )


def _holds_legacy_record(
    plugin: CLIPluginProtocol, target_name: str, stored: dict[str, Any] | None
) -> bool:
    """Whether *target_name* records a secret 1.24.0 took for the account (#293).

    A check that fails answers True: it only withholds a warning that would
    otherwise print the recorded value, and the replacement after the login
    checks again.
    """
    try:
        return (
            legacy_secret_field_of(plugin) is not None
            and records_legacy_secret(stored)
            and not is_legacy_secret_slot(target_name, stored)
        )
    except Exception as exc:  # noqa: BLE001 (advisory: never blocks a login)
        LOG.warning("legacy_record_check_failed", exc_type=type(exc).__name__)
        return True


def _replace_legacy_record(
    plugin: CLIPluginProtocol, target_name: str, identifier: str | None
) -> None:
    """After a successful login: make sure *target_name* no longer records a 1.24.0 secret."""
    try:
        secret_field = legacy_secret_field_of(plugin)
        replace_legacy_record(target_name, identifier, secret_field=secret_field)
        metadata = get_session_metadata(target_name)
    except Exception as exc:  # noqa: BLE001 (advisory: the login already succeeded)
        LOG.warning(
            "legacy_account_record_not_replaced",
            session=target_name,
            exc_type=type(exc).__name__,
        )
        gp_console.warn(
            f"Could not replace the account recorded on '{target_name}' by an earlier "
            "graftpunk; the next successful login tries again."
        )
        return
    if records_legacy_secret(metadata):
        return
    gp_console.info(
        f"Replaced the account recorded on '{target_name}': it came from the "
        f"{secret_field} field, which is not an account identifier."
    )


def _report_slot_outcome(migration: LegacySlotMigration) -> None:
    """What happened to the stale slot, if anything, and the pins that named it."""
    target_name = migration.new_name
    why = (
        f"its name came from the {migration.source_field} field, which is not an account identifier"
    )
    if migration.action == "renamed":
        gp_console.info(f"Renamed this account's earlier session to '{target_name}': {why}.")
    elif migration.action == "deleted":
        gp_console.info(f"Removed this account's earlier session: {why}.")
    elif migration.action == "kept" and migration.problem:
        gp_console.warn(migration.problem)
        return
    else:
        return
    if migration.pin_updated:
        gp_console.info(f"Updated {SESSION_FILE_NAME} here to {target_name}.")
    if migration.env_pinned:
        gp_console.warn(f"GRAFTPUNK_SESSION names the old session; set it to {target_name}.")
    gp_console.info(
        f"If another directory pins the old session, run there: gp session use {target_name}"
    )


def _shown_pin(
    plugin: CLIPluginProtocol, current: str, migration: LegacySlotMigration | None
) -> str:
    """*current* as the pin hint may print it: a name 1.24.0 took from a secret is masked."""
    base = plugin.session_name
    if migration is not None and current == migration.stale_name:
        return earlier_name(base)
    if split_session_name(current)[0] != base or split_session_name(current)[1] is None:
        return current
    if legacy_secret_field_of(plugin) is None:
        return current
    with hidden_name(current):
        metadata = get_session_metadata(current)
    return earlier_name(base) if is_legacy_secret_slot(current, metadata) else current


async def _awaited(awaitable: Any) -> Any:
    """*awaitable*'s result: ``asyncio.run`` takes only a coroutine, and a login may
    return any awaitable."""
    return await awaitable


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
