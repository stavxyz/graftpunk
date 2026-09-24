"""``gp plugin info --json``: the payload, built from the project reader's view.

Kept apart from :mod:`graftpunk.devtools.plugin_project`, which stays a
structural view that knows no output format (graft skill spec, 2026-09-21).
"""

from __future__ import annotations

from collections.abc import Sequence

from graftpunk.contracts import current_schema
from graftpunk.devtools.errors import DevtoolsRefusal
from graftpunk.devtools.plugin_project import PluginDefect, ProjectView

__all__ = ["PluginDefectRefusal", "info_payload"]


class PluginDefectRefusal(DevtoolsRefusal):
    """``gp plugin info`` refuses while any plugin module is defective, rather than
    describe a project with that plugin left out. The message is one line per
    defect: ``entry point '<name>': <the reader's message>``."""

    def __init__(self, defects: Sequence[PluginDefect]) -> None:
        self.defects = tuple(defects)
        super().__init__(
            "\n".join(f"entry point {d.entry_point!r}: {d.message}" for d in self.defects)
        )


def info_payload(view: ProjectView) -> dict[str, object]:
    """``gp plugin info --json``: facts about the directory, and none about the install.

    Within a schema version fields are added and never renamed or removed; the
    number comes from :mod:`graftpunk.contracts`. A plugin's ``entry_point`` is its
    one identity, the name ``gp plugin add-command`` takes; a command's ``name`` is
    the one the CLI registers (:attr:`CommandView.cli_name`).

    Raises:
        PluginDefectRefusal: A plugin module is defective. The payload is never
            built without that plugin, so every caller gets the refusal.
    """
    if view.defects:
        raise PluginDefectRefusal(view.defects)
    return {
        "schema": current_schema("info"),
        "directory": view.directory,
        "plugins": [
            {
                "entry_point": plugin.entry_point,
                "module": plugin.module_path,
                "site_name": plugin.site_name,
                "base_url": plugin.base_url,
                "commands": [
                    {
                        "name": command.cli_name,
                        "endpoint": command.endpoint,
                    }
                    for command in plugin.commands
                ],
            }
            for plugin in view.plugins
        ],
    }
