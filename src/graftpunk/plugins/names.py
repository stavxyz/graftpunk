"""The names a site plugin answers to: its ``site_name`` and its ``aliases``.

:func:`plugin_names` is the one resolver. The CLI's registration, the session
resolution behind it, :class:`graftpunk.client.GraftpunkClient` (through
:func:`graftpunk.plugins.get_plugin`), and ``gp plugin check`` all build their
name table with it, over every plugin discovered (the CLI included, whether or
not it then registers each one), so an alias resolves to the same plugin, or to
the same refusal, everywhere (#374).

An alias is another name for the same plugin object. It is never a second
plugin, never its own session slot, and never the source of a credential
variable name: every consumer resolves the alias to the plugin and then reads the
plugin's own ``site_name`` and ``session_name``. A plugin's aliases are the ones
its own class declares; like the rest of its configuration, they are not
inherited from a parent plugin class.

Canonical names and core commands always win. An alias is dropped, with an
:class:`AliasConflict` saying why, when it names a core command, another
plugin's ``site_name`` or ``session_name`` (the session base ``gp session`` and
``gp http`` resolve a plugin name to), an alias another plugin also declares
(dropped from both, so the outcome never depends on discovery order), or the
plugin's own ``site_name``; when the plugin lists it twice; or when it is not a
name the CLI can register. An alias may equal the plugin's own ``session_name``.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from typing import Any, Final, Generic, Protocol, TypeVar

from graftpunk.exceptions import PluginError

__all__ = [
    "CORE_COMMAND_NAMES",
    "AliasConflict",
    "PluginNames",
    "conflict_log_fields",
    "declared_aliases",
    "plugin_names",
    "validate_aliases",
]

CORE_COMMAND_NAMES: Final[frozenset[str]] = frozenset(
    {"config", "http", "keepalive", "observe", "plugin", "plugins", "session", "version"}
)
"""The top-level ``gp`` commands. The CLI reserves the set it derives from its own
app at attach time; a test holds this constant equal to that set, so a consumer
without the app (``GraftpunkClient``, ``gp plugin check``) refuses the same
aliases the CLI does."""

ALIAS_RE: Final = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
"""A name the CLI can register as a command: the shape ``gp plugin new`` requires
of a plugin name, without the limits that exist for the generated project's
distribution name (``gp plugin check`` applies those too)."""


class _Named(Protocol):
    @property
    def site_name(self) -> str: ...


P = TypeVar("P", bound=_Named)


@dataclass(frozen=True)
class AliasConflict:
    """An alias that :func:`plugin_names` dropped: the plugin that declared it,
    the alias, and why (a clause that completes "alias '<alias>' ...")."""

    site_name: str
    alias: str
    reason: str

    def __str__(self) -> str:
        if not self.alias:
            # The whole declared value was refused; reason is the validator's
            # message, which names the plugin and the fix.
            return f"{self.reason} The plugin loads without aliases."
        return f"plugin '{self.site_name}': alias '{self.alias}' {self.reason}; it is ignored."


@dataclass(frozen=True)
class PluginNames(Generic[P]):
    """Every name that resolves, and every alias that was dropped.

    ``by_name`` maps each ``site_name`` and each accepted alias to its plugin.
    ``aliases`` maps each ``site_name`` to its accepted aliases, in declared
    order (an empty tuple when it has none)."""

    by_name: dict[str, P] = field(default_factory=dict)
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    conflicts: tuple[AliasConflict, ...] = ()

    def resolve(self, name: str) -> P | None:
        """The plugin *name* (a ``site_name`` or an accepted alias) names, or ``None``."""
        return self.by_name.get(name)


def conflict_log_fields(conflict: AliasConflict) -> dict[str, str]:
    """The fields a ``plugin_alias_ignored`` log line carries for *conflict*: no
    ``alias`` field when the whole declared value was refused."""
    fields = {"plugin": conflict.site_name, "reason": conflict.reason}
    if conflict.alias:
        fields["alias"] = conflict.alias
    return fields


def validate_aliases(value: Any, site_name: str) -> tuple[str, ...]:
    """*value*, a plugin's declared ``aliases``, as a tuple of strings.

    The one check of the value's type, for every kind of plugin: a ``SitePlugin``
    and a YAML plugin through ``build_plugin_config``, and a plugin written
    against ``CLIPluginProtocol`` through ``discover_all_plugins``, which refuses
    each the same way, with this message. ``None`` is no aliases, and a list (YAML
    has no tuple) becomes a tuple. A bare string is refused rather than read as
    one alias, so a missing trailing comma (``aliases = ("shop")``) is reported.

    Raises:
        PluginError: *value* is not a list or tuple of strings.
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(a, str) for a in value):
        raise PluginError(
            f"Plugin '{site_name}': 'aliases' must be a tuple (or a YAML list) of "
            f"strings, got {value!r}. One alias is written ('shop',)."
        )
    return tuple(value)


def declared_aliases(plugin: Any) -> tuple[str, ...]:
    """The aliases *plugin* declares: an empty tuple when it declares none,
    including a plugin written against ``CLIPluginProtocol`` before ``aliases``
    existed, and one whose ``aliases`` is a method (a command of that name).

    Raises:
        PluginError: The declared value is not a list or tuple of strings
            (:func:`validate_aliases`), or a ``SitePlugin`` or YAML plugin whose
            configuration already refused it.
    """
    refused = getattr(plugin, "_aliases_error", "")
    if isinstance(refused, str) and refused:
        raise PluginError(refused)
    # A SitePlugin or YAML plugin's config holds what its own class declared,
    # validated; the attribute may be a value gp leaves alone (an inherited dict).
    config = getattr(type(plugin), "_plugin_config", None)
    value = getattr(plugin, "aliases", ())
    if config is not None and isinstance(getattr(config, "aliases", None), tuple):
        computed = (
            not callable(value)
            and isinstance(value, (tuple, list))
            and all(isinstance(name, str) for name in value)
            and tuple(value) != config.aliases
        )
        if computed:
            raise PluginError(
                f"Plugin '{plugin.site_name}': 'aliases' is read only from an "
                'assignment in the class body (aliases = ("shop",)), not from a '
                "property or an attribute set in __init__."
            )
        return config.aliases
    if callable(value):
        return ()
    return validate_aliases(value, plugin.site_name)


def plugin_names(
    plugins: Iterable[P], reserved: Collection[str] = CORE_COMMAND_NAMES
) -> PluginNames[P]:
    """Build the name table for *plugins*, against the *reserved* top-level names.

    A ``site_name`` maps to the first plugin that has it (refusing a duplicate
    ``site_name`` is the caller's business, as before aliases existed). Each
    alias is checked as the module docstring says. A plugin whose ``aliases``
    cannot be read or is not a list or tuple of strings gets none, and one
    conflict (with an empty ``alias``) says why; the plugin itself, and its
    ``site_name`` and ``session_name``, stay in the table.
    """
    ordered = list(plugins)
    by_name: dict[str, P] = {}
    for plugin in ordered:
        by_name.setdefault(plugin.site_name, plugin)
    # Every site_name and session base, with the plugins that hold it: the names
    # an alias may not take from another plugin.
    holders: dict[str, list[str]] = {}
    for plugin in ordered:
        for name in {plugin.site_name, _session_base(plugin)} - {""}:
            holders.setdefault(name, []).append(plugin.site_name)

    conflicts: list[AliasConflict] = []
    claims: dict[str, list[P]] = {}
    candidates: dict[str, list[str]] = {}
    for plugin in ordered:
        own = plugin.site_name
        try:
            declared = declared_aliases(plugin)
        except PluginError as exc:
            conflicts.append(AliasConflict(own, "", str(exc)))
            declared = ()
        except Exception as exc:  # noqa: BLE001 - plugin boundary: a property may raise anything
            reason = f"Plugin '{own}': reading 'aliases' raised {exc!r}."
            conflicts.append(AliasConflict(own, "", reason))
            declared = ()
        seen: set[str] = set()
        kept: list[str] = []
        for alias in declared:
            reason = _refusal(alias, plugin, seen, reserved, holders)
            seen.add(alias)
            if reason is not None:
                conflicts.append(AliasConflict(own, alias, reason))
                continue
            kept.append(alias)
            claims.setdefault(alias, []).append(plugin)
        candidates[own] = kept

    shared = {alias: owners for alias, owners in claims.items() if len(owners) > 1}
    for alias, owners in shared.items():
        names = [owner.site_name for owner in owners]
        for owner in owners:
            others = ", ".join(f"'{n}'" for n in names if n != owner.site_name)
            conflicts.append(AliasConflict(owner.site_name, alias, f"is also an alias of {others}"))

    aliases: dict[str, tuple[str, ...]] = {}
    for plugin in ordered:
        own = plugin.site_name
        accepted = tuple(a for a in candidates.get(own, ()) if a not in shared)
        aliases.setdefault(own, accepted)
        if by_name.get(own) is plugin:
            for alias in accepted:
                by_name[alias] = plugin
    return PluginNames(by_name=by_name, aliases=aliases, conflicts=tuple(conflicts))


def _session_base(plugin: Any) -> str:
    """*plugin*'s ``session_name``, or ``""`` when it has none or reading it raises
    (the CLI reports that plugin when it reaches it; the table does not)."""
    try:
        value = getattr(plugin, "session_name", "")
    except Exception:  # noqa: BLE001 - plugin boundary: a property may raise anything
        return ""
    return value if isinstance(value, str) else ""


def _refusal(
    alias: str,
    plugin: _Named,
    seen: set[str],
    reserved: Collection[str],
    holders: dict[str, list[str]],
) -> str | None:
    """Why *alias* cannot be one of *plugin*'s names, or ``None`` when it can."""
    own = plugin.site_name
    if alias == own:
        return "is the plugin's own site_name"
    if alias in seen:
        return "is listed twice"
    if not ALIAS_RE.fullmatch(alias):
        return (
            "is not a name the CLI can register: it must start with a letter and "
            "contain only letters, digits, hyphens, and underscores"
        )
    if alias in reserved:
        return "is a core gp command"
    others = [name for name in holders.get(alias, ()) if name != own]
    if not others:
        return None
    if alias in others:
        return f"is the site_name of plugin '{alias}'"
    return f"is the session name of plugin '{others[0]}'"
