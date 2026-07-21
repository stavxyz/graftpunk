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

import functools
import inspect
from collections.abc import Callable, Sequence
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

    # Fail loud: required and default are mutually exclusive
    if required and default is not None:
        kind = "option" if spec.is_option else "argument"
        raise PluginError(
            f"plugin '{plugin_name}', command '{command_name}', param '{spec.name}': "
            f"a required {kind} cannot also declare a default "
            f"(required=True with default={default!r})."
        )

    if spec.is_option:
        _reject_unsupported(plugin_name, command_name, spec, OPTION_KEYS)
        if base_type is bool and not kw.get("is_flag"):
            raise PluginError(
                f"plugin '{plugin_name}', command '{command_name}', param '{spec.name}': "
                "bool options must be flags (is_flag=True) -- "
                "PluginParamSpec.option() auto-sets this for type=bool, default=False, "
                "but any other bool default (e.g. default=True) must set "
                "click_kwargs={'is_flag': True} explicitly. A value-taking bool option "
                "(is_flag=False or unset) has no Typer-native equivalent; use a str/int "
                "option or a plain flag."
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
    nargs = kw.get("nargs")
    if nargs not in (None, 1, -1):
        raise PluginError(
            f"plugin '{plugin_name}', command '{command_name}', param '{spec.name}': "
            f"nargs={nargs!r} has no Typer-native equivalent; the RFC's closed "
            "contract only supports nargs=-1 (variadic) or the default "
            "(a single value)."
        )
    if nargs == -1 and default is not None:
        raise PluginError(
            f"plugin '{plugin_name}', command '{command_name}', param '{spec.name}': "
            f"nargs=-1 (variadic) combined with default={default!r} is unsupported -- "
            "Typer variadic arguments cannot express a non-empty default; omit default "
            "(an absent variadic argument collects as an empty list)."
        )
    if nargs == -1:
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


# The built-in option contract: names + defaults, in ONE place. The factory
# declares these options from this mapping and the runtime (plugin_runtime)
# pops them against the same mapping -- a rename or default change cannot
# silently diverge between construction and execution.
BUILTIN_OPTIONS: dict[str, Any] = {"format": "json", "view": (), "output": ""}


@functools.lru_cache(maxsize=1)
def _available_formats() -> str:
    """Comma-joined formatter names for the ``--format`` help text.

    Cached: entry points don't change within a process, and this is
    recomputed once per synthesized command otherwise.
    """
    from graftpunk.plugins.formatters import discover_formatters

    return ", ".join(discover_formatters().keys())


def _builtin_option_params() -> list[tuple[inspect.Parameter, type]]:
    available = _available_formats()
    fmt = inspect.Parameter(
        "format",
        inspect.Parameter.KEYWORD_ONLY,
        default=typer.Option(
            BUILTIN_OPTIONS["format"],
            "--format",
            "-f",
            help=f"Output format (built-in: {available})",
        ),
        annotation=str,
    )
    # The `view` option's default is `None` (not a concrete tuple or list) because `None`
    # is the Typer-level sentinel for "option not passed by the user". A concrete `()` or `[]`
    # default would be indistinguishable from "user passed nothing" across Typer versions.
    # The synthesized `_command` body (in synthesize_command_fn) normalizes `None` -> `[]`
    # before forwarding.
    # The runtime (plugin_runtime) pops with BUILTIN_OPTIONS["view"] (empty tuple) as its default,
    # so all three spellings (`None`, `()`, `[]`) mean "no --view given".
    # BUILTIN_OPTIONS["view"] is the single authoritative contract for the option name + default.
    view = inspect.Parameter(
        "view",
        inspect.Parameter.KEYWORD_ONLY,
        default=typer.Option(
            None, "--view", help="Select view(s) to render; repeatable (NAME or NAME:COL1,COL2,...)"
        ),
        annotation=list[str],
    )
    out = inspect.Parameter(
        "output",
        inspect.Parameter.KEYWORD_ONLY,
        default=typer.Option(
            BUILTIN_OPTIONS["output"],
            "--output",
            "-o",
            help="Write output to file instead of stdout",
        ),
        annotation=str,
    )
    return [(fmt, str), (view, list[str]), (out, str)]


def synthesize_command_fn(
    *,
    name: str,
    param_specs: Sequence[PluginParamSpec],
    body: CommandBody,
    plugin_name: str = "",
    include_builtin_options: bool = True,
    help_text: str | None = None,
) -> Callable[..., None]:
    """Synthesize a function Typer can register as a command.

    The function's ``__signature__``/``__annotations__`` declare
    ``ctx: typer.Context`` plus one parameter per spec (positional arguments
    first, then options), plus the ``--format/--view/--output`` built-ins when
    ``include_builtin_options``. Typer introspects the synthesized signature
    and builds every parameter with its own Click. At call time the function
    forwards ``body(ctx, **parsed_kwargs)`` (built-ins included; the body pops
    them). ``view`` is normalized to ``[]`` when absent.
    """
    positional = [s for s in param_specs if not s.is_option]
    options = [s for s in param_specs if s.is_option]

    # Reserved names: `ctx` is always injected; the built-in options are
    # injected when include_builtin_options. A plugin param sharing one of
    # these names hits inspect.Signature's duplicate-param ValueError --
    # fail loud with a clear message instead of leaking that internal error.
    reserved = {"ctx"} | (set(BUILTIN_OPTIONS) if include_builtin_options else set())
    for spec in [*positional, *options]:
        if spec.name in reserved:
            raise PluginError(
                f"plugin '{plugin_name}', command '{name}', param '{spec.name}': "
                f"reserved parameter name (reserved: {', '.join(sorted(reserved))}); "
                "rename the plugin parameter."
            )

    params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}

    ctx_param = inspect.Parameter(
        "ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=typer.Context
    )
    params.append(ctx_param)
    annotations["ctx"] = typer.Context

    for spec in [*positional, *options]:
        param, annotation = map_param_spec(plugin_name, name, spec)
        params.append(param)
        annotations[spec.name] = annotation

    if include_builtin_options:
        for param, annotation in _builtin_option_params():
            params.append(param)
            annotations[param.name] = annotation

    def _command(**kwargs: Any) -> None:
        ctx = kwargs.pop("ctx")
        if "view" in kwargs and kwargs["view"] is None:
            kwargs["view"] = []
        return body(ctx, **kwargs)

    _command.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    _command.__annotations__ = annotations
    _command.__name__ = name.replace("-", "_")
    if help_text:
        _command.__doc__ = help_text
    return _command
