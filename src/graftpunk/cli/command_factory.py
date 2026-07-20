"""Typer-native construction of plugin CLI commands.

Construction ONLY: mapping ``PluginParamSpec`` -> Typer parameter declarations
and synthesizing registerable command functions. The run-time execution
pipeline lives in ``cli/plugin_runtime.py`` (see the RFC: the factory is
body-parameterized; construction and execution are separate responsibilities).

Why signature synthesis: ``typer.utils.get_params_from_function`` reads
``inspect.signature(func, eval_str=True)``, which honors a ``__signature__``
override -- so a synthesized signature fully controls the params Typer builds,
and Typer builds them with *its own* Click (vendored on typer>=0.26, external
before). No externally-built ``click.Option``/``click.Argument`` ever enters
Typer's runtime, which is the root fix for the vendored-Click breakage.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import typer

from graftpunk.exceptions import PluginError
from graftpunk.plugins.cli_plugin import PluginParamSpec

# The supported click_kwargs surface (the RFC's documented contract).
# Anything outside these sets fails loudly at registration -- no silent drift.
OPTION_KEYS = frozenset(
    {"type", "required", "default", "help", "is_flag", "show_default", "envvar"}
)
ARGUMENT_KEYS = frozenset({"type", "required", "default", "nargs"})

# body(ctx, **parsed_kwargs) -- the factory wraps a body it is GIVEN.
CommandBody = Callable[..., None]


def _reject_unsupported(
    plugin_name: str, command_name: str, spec: PluginParamSpec, allowed: frozenset[str]
) -> None:
    unsupported = set(spec.click_kwargs) - allowed
    if unsupported:
        kind = "option" if spec.is_option else "argument"
        raise PluginError(
            f"plugin '{plugin_name}', command '{command_name}', param '{spec.name}': "
            f"unsupported click_kwargs key(s) for {kind}: {', '.join(sorted(unsupported))}. "
            f"Supported: {', '.join(sorted(allowed))}. "
            "Extend graftpunk.cli.command_factory if a real plugin needs more."
        )


def map_param_spec(
    plugin_name: str, command_name: str, spec: PluginParamSpec
) -> tuple[inspect.Parameter, type]:
    """Map one ``PluginParamSpec`` to an ``inspect.Parameter`` Typer understands.

    Returns ``(parameter, annotation)``. The parameter's ``default`` is a
    ``typer.Option``/``typer.Argument`` info object (default-value declaration
    style) and its ``annotation`` is the plain base type.
    """
    kw = dict(spec.click_kwargs)
    base_type: type = kw.get("type", str)
    required: bool = bool(kw.get("required", not spec.is_option))
    default: Any = kw.get("default")

    if spec.is_option:
        _reject_unsupported(plugin_name, command_name, spec, OPTION_KEYS)
        if base_type is bool and kw.get("is_flag") is False:
            raise PluginError(
                f"plugin '{plugin_name}', command '{command_name}', param '{spec.name}': "
                "is_flag=False (a value-taking bool option) has no Typer-native "
                "equivalent; use a str/int option or a plain flag."
            )
        # Explicit positive-only decl: bare flag for bools (no --no-* pair),
        # and immune to typer-version differences in derived flag names.
        flag = f"--{spec.name.replace('_', '-')}"
        info = typer.Option(
            ... if required else default,
            flag,
            help=kw.get("help") or None,
            show_default=kw.get("show_default", False),
            envvar=kw.get("envvar") or None,
        )
        annotation: type = base_type
        param = inspect.Parameter(
            spec.name,
            inspect.Parameter.KEYWORD_ONLY,
            default=info,
            annotation=annotation,
        )
        return param, annotation

    _reject_unsupported(plugin_name, command_name, spec, ARGUMENT_KEYS)
    if kw.get("nargs") == -1:
        annotation = list[base_type]  # type: ignore[valid-type]
        info = typer.Argument(None if not required else ...)
    else:
        annotation = base_type
        info = typer.Argument(... if required else default)
    param = inspect.Parameter(
        spec.name,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        default=info,
        annotation=annotation,
    )
    return param, annotation
