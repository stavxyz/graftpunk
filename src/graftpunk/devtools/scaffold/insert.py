"""Places one rendered command stub inside a plugin class, through ``write.py``.

The placement rule is code, and total over any class the project reader
accepts whose body does not start on the ``class`` line (refused instead,
below): immediately after the class's last ``@command``-decorated member (a
method or a command group), or, for a class with no command yet, immediately
after the class body's last statement, wherever the class sits in the module.
A module a developer has extended with helpers above or below the class is
handled rather than refused (graft skill spec, 2026-09-21). The stub is
re-indented to the class body's own indentation (tabs included) before it is
spliced in, read from the first body statement's source line. It inserts a
stub, the imports the rendered stub says it references, and nothing else.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from graftpunk.devtools.errors import DevtoolsRefusal
from graftpunk.devtools.plugin_project import PluginDefect, PluginView, require_plugin_project
from graftpunk.devtools.scaffold import policy
from graftpunk.devtools.scaffold.pysrc import (
    INDENT_STEP,
    ImportPlacementError,
    binds_name,
    joined_like,
    source_lines,
    with_import,
)
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
    project-relative fixture its test will look for, or ``None`` when
    ``gp observe fixtures`` writes no fixture for this endpoint (see
    ``render.RenderedCommand.fixture``)."""

    module: Path
    cli_name: str
    fixture: str | None


def insertion_line(plugin: PluginView, lines: Sequence[str], class_indent: str) -> int:
    """The line after which a new command goes: the last command's last statement,
    or the class body's last statement for a class with no command yet, advanced
    past any comment line that immediately follows it (no blank line between)
    indented deeper than *class_indent*, the class body's own indentation. Such a
    comment sits at the command body's own indentation, so it reads as that
    command's trailing comment, not the start of the new stub's body."""
    at = plugin.commands[-1].span.end if plugin.commands else plugin.class_span.end
    class_level = len(class_indent)
    while at < len(lines):
        line = lines[at]
        indent = len(line) - len(line.lstrip(" \t"))
        if not line.strip().startswith("#") or indent <= class_level:
            break
        at += 1
    return at


def _reindented(lines: Sequence[str], unit: str) -> list[str]:
    """*lines*, each rendered at some multiple of ``pysrc.INDENT_STEP`` spaces, with
    every multiple of that many leading spaces replaced by that many repeats of
    *unit*: the class body's own indentation, which need not be spaces."""
    if unit == " " * INDENT_STEP:
        return list(lines)
    reindented = []
    for line in lines:
        stripped = line.lstrip(" ")
        level = (len(line) - len(stripped)) // INDENT_STEP
        reindented.append(unit * level + stripped)
    return reindented


def _command_names(plugin: PluginView) -> set[str]:
    """Every name a real (non-group) command already registers, plus the reserved
    names every plugin has. policy.RESERVED_COMMAND_NAMES is also checked by
    plan_command, called below before this function; seeded here too, so this
    collision check stays correct on its own if that call order ever changes.
    Split from a group by CommandView.group, never by whether endpoint is set:
    a hand-written command can carry no endpoint= and is still a command."""
    taken: set[str] = set(policy.RESERVED_COMMAND_NAMES)
    for command in plugin.commands:
        if not command.group:
            taken.add(command.method)
            taken.add(command.cli_name)
    return taken


def _group_names(plugin: PluginView) -> set[str]:
    """Every name a command group (a decorated nested class) already registers:
    its Python identifier and its CLI name."""
    names: set[str] = set()
    for command in plugin.commands:
        if command.group:
            names.add(command.method)
            names.add(command.cli_name)
    return names


def _layout_refusal(module_path: str) -> str:
    """The one-line refusal for a class whose body shares a line with its
    header: the stub inserter only knows how to re-indent a stub to an
    existing body line's own indentation, which a header-line body has none
    of."""
    return (
        f"{module_path}: the stub does not fit this class's layout (its body starts "
        f"on the class header's line); add a command by hand."
    )


def add_command(
    root: Path, entry_point: str, d: RunDigest, selection: CommandSelection
) -> AddedCommand:
    """Add *selection*'s stub to the plugin whose entry-point name is *entry_point*,
    the one identity ``gp plugin info --json`` reports for it.

    Raises:
        CommandInsertError: The target plugin's module is defective, no entry
            point has that name, the command name is taken (as a method name, a
            CLI name, or a name the module already binds at top level), the
            plugin class's body starts on the ``class`` line, or the module's
            imports are in a shape ``with_import`` does not place into.
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
    candidates = {command.identifier, command.registered_name}
    if candidates & _group_names(plugin):
        raise CommandInsertError(
            f"{plugin.module_path}: a command group already registers {selection.name!r}."
        )
    if candidates & _command_names(plugin):
        raise CommandInsertError(
            f"{plugin.module_path} already has a command named {selection.name!r}."
        )
    if command.identifier in plugin.class_names:
        raise CommandInsertError(
            f"{plugin.module_path}: the plugin class already defines {selection.name!r}."
        )
    module = root / plugin.module_path
    original = read_original(module)
    tree = ast.parse(original)
    if binds_name(tree, command.identifier):
        raise CommandInsertError(
            f"{plugin.module_path}: the module already binds {command.identifier!r} at top level."
        )
    klass = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == plugin.class_name
    )
    if not klass.body:
        raise CommandInsertError(_layout_refusal(plugin.module_path))
    lines = source_lines(original)
    first_body_line = lines[klass.body[0].lineno - 1]
    indent_unit = first_body_line[: len(first_body_line) - len(first_body_line.lstrip(" \t"))]
    if klass.body[0].lineno == klass.lineno or not indent_unit:
        # The body shares a line with the header: either the `class` keyword's
        # own line (a one-line class), or, for a header that spans lines, the
        # line holding the closing `):` (klass.body[0].lineno != klass.lineno
        # there, so that comparison alone misses it; an empty indent unit
        # catches both shapes, since a body on its own line is always
        # indented).
        raise CommandInsertError(_layout_refusal(plugin.module_path))
    rendered = render_command(command, d)
    stub_lines = _reindented(rendered.lines, indent_unit)
    at = insertion_line(plugin, lines, indent_unit)
    text = joined_like(original, [*lines[:at], "", *stub_lines, *lines[at:]])
    first_party = view.first_party_packages
    try:
        for imported_from, name in rendered.imports:
            text = with_import(text, imported_from, name, first_party=first_party)
    except ImportPlacementError as exc:
        raise CommandInsertError(f"{plugin.module_path}: {exc}") from exc
    except SyntaxError as exc:
        raise CommandInsertError(
            f"{plugin.module_path}: does not parse after the stub is placed ({exc.msg})."
        ) from exc
    apply_changes([PlannedChange(module, text, original=original, validate=validate_python)])
    fixture = None if rendered.fixture is None else f"{plugin.fixtures_root}{rendered.fixture}"
    return AddedCommand(
        module=module,
        cli_name=command.registered_name,
        fixture=fixture,
    )
