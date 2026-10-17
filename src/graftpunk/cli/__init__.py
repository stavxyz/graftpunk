"""graftpunk CLI package.

This package provides the command-line interface for graftpunk using Typer.

``app`` is imported on first access: importing ``graftpunk.cli.main`` is
starting the ``gp`` program (it configures structlog and registers plugins),
which importing a submodule such as ``graftpunk.cli.plugin_commands`` must not do.
"""

from typing import Any

__all__ = ["app"]


def __getattr__(name: str) -> Any:
    if name == "app":
        from graftpunk.cli.main import app

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), "app"])
