"""Places one rendered command stub inside a plugin class, through ``write.py``.

The placement rule is code, and total over any class the project reader
accepts: immediately after the class's last ``@command``-decorated method, or,
for a class with no command yet, immediately after the class body's last
statement, wherever the class sits in the module. A module a developer has
extended with helpers above or below the class is handled rather than refused
(graft skill spec, 2026-09-21). It inserts a stub, the imports the rendered stub says it
references, and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from graftpunk.devtools.errors import DevtoolsRefusal
from graftpunk.devtools.plugin_project import PluginDefect, PluginView, require_plugin_project
from graftpunk.devtools.scaffold import policy
from graftpunk.devtools.scaffold.pysrc import ImportPlacementError, with_import
from graftpunk.devtools.scaffold.render import render_command
from graftpunk.devtools.scaffold.selection import CommandSelection, plan_command
from graftpunk.devtools.scaffold.write import (
    PlannedChange,
    apply_changes,
    read_original,
    validate_python,
)
from graftpunk.har.digest import RunDigest

__all__ = ["AddedCommand", "CommandInsertError", "add_command", "insertion_line"]


class CommandInsertError(DevtoolsRefusal, ValueError):
    """The stub cannot be added; nothing was written."""


@dataclass(frozen=True)
class AddedCommand:
    """What ``add_command`` did: the module it edited, the command's CLI name, and the
    project-relative fixture its test will look for."""

    module: Path
    cli_name: str
    fixture: str


def insertion_line(plugin: PluginView) -> int:
    """The line after which a new command goes."""
    return plugin.commands[-1].span.end if plugin.commands else plugin.class_span.end


def _taken_names(plugin: PluginView) -> set[str]:
    # policy.RESERVED_COMMAND_NAMES is also checked by plan_command, called
    # below before this function; seeded here too, so this collision check
    # stays correct on its own if that call order ever changes.
    taken: set[str] = set(policy.RESERVED_COMMAND_NAMES)
    for command in plugin.commands:
        taken.add(command.method)
        taken.add(command.cli_name)
    return taken


def add_command(
    root: Path, entry_point: str, d: RunDigest, selection: CommandSelection
) -> AddedCommand:
    """Add *selection*'s stub to the plugin whose entry-point name is *entry_point*,
    the one identity ``gp plugin info --json`` reports for it.

    Raises:
        CommandInsertError: The target plugin's module is defective, no entry
            point has that name, the command name is taken (as a method name or a
            CLI name), or the module's imports are in a shape ``with_import`` does
            not place into.
        CommandSelectionError: See :func:`plan_command`.
        NotAPluginProjectError: *root* is not a plugin project.
        PluginProjectError: See :func:`read_project`.
        ScaffoldWriteError: The write failed; the module was restored first, or
            the error names it as left changed.
    """
    view = require_plugin_project(root)
    plugin = view.plugin(entry_point)
    if isinstance(plugin, PluginDefect):
        raise CommandInsertError(plugin.message)
    if plugin is None:
        names = ", ".join(
            sorted({p.entry_point for p in view.plugins} | {d.entry_point for d in view.defects})
        )
        raise CommandInsertError(
            f"No plugin has the entry-point name {entry_point!r} in {root}; "
            f"its entry points are: {names}."
        )
    command = plan_command(d, selection)
    if {command.identifier, command.registered_name} & _taken_names(plugin):
        raise CommandInsertError(
            f"{plugin.module_path} already has a command named {selection.name!r}."
        )
    rendered = render_command(command, d)
    module = root / plugin.module_path
    original = read_original(module)
    lines = original.splitlines()
    at = insertion_line(plugin)
    text = "\n".join([*lines[:at], "", *rendered.lines, *lines[at:]]) + "\n"
    try:
        for imported_from, name in rendered.imports:
            text = with_import(text, imported_from, name)
    except ImportPlacementError as exc:
        raise CommandInsertError(f"{plugin.module_path}: {exc}") from exc
    apply_changes([PlannedChange(module, text, original=original, validate=validate_python)])
    return AddedCommand(
        module=module,
        cli_name=command.registered_name,
        fixture=f"{plugin.fixtures_root}{rendered.fixture}",
    )
