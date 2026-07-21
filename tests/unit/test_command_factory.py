"""Tests for the Typer-native command factory (construction only)."""

from __future__ import annotations

import inspect
import tempfile

import pytest
import typer
from typer.testing import CliRunner

from graftpunk.cli.command_factory import map_param_spec, synthesize_command_fn
from graftpunk.exceptions import PluginError
from graftpunk.plugins.cli_plugin import PluginParamSpec


class TestMapParamSpec:
    def test_str_option_with_default(self) -> None:
        spec = PluginParamSpec.option("county", type=str, default="all", help="County filter")
        param, ann = map_param_spec("surety", "search", spec)
        assert param.name == "county"
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert ann is str
        info = param.default
        assert isinstance(info, typer.models.OptionInfo)
        assert info.default == "all"
        assert info.param_decls == ("--county",)
        assert info.help == "County filter"

    def test_int_option(self) -> None:
        spec = PluginParamSpec.option("limit", type=int, default=50)
        param, ann = map_param_spec("surety", "search", spec)
        assert ann is int
        assert param.default.default == 50

    def test_required_option_maps_to_ellipsis_default(self) -> None:
        spec = PluginParamSpec.option("docno", type=str, required=True)
        param, ann = map_param_spec("surety", "document", spec)
        assert param.default.default is ...

    def test_underscore_name_becomes_hyphen_flag(self) -> None:
        spec = PluginParamSpec.option("filed_from", type=str, default="")
        param, _ = map_param_spec("surety", "search", spec)
        assert param.default.param_decls == ("--filed-from",)

    def test_bool_default_false_is_bare_flag(self) -> None:
        # PluginParamSpec.option auto-sets is_flag=True for bool+default False
        spec = PluginParamSpec.option("no_wait", type=bool, default=False)
        param, ann = map_param_spec("shopkeep", "run", spec)
        assert ann is bool
        assert param.default.default is False
        # explicit positive-only decl -> bare --no-wait flag, no --no-no-wait pair
        assert param.default.param_decls == ("--no-wait",)

    def test_envvar_and_show_default_pass_through(self) -> None:
        spec = PluginParamSpec.option(
            "token", type=str, default="", click_kwargs={"envvar": "MY_TOKEN"}
        )
        param, _ = map_param_spec("p", "c", spec)
        assert param.default.envvar == "MY_TOKEN"
        spec2 = PluginParamSpec.option(
            "count", type=int, default=3, click_kwargs={"show_default": True}
        )
        param2, _ = map_param_spec("p", "c", spec2)
        assert param2.default.show_default is True

    def test_required_argument(self) -> None:
        spec = PluginParamSpec.argument("export_id", type=str)  # required=True default
        param, ann = map_param_spec("shopkeep", "get", spec)
        assert param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert isinstance(param.default, typer.models.ArgumentInfo)
        assert param.default.default is ...
        assert ann is str

    def test_variadic_argument_nargs_minus_one(self) -> None:
        spec = PluginParamSpec.argument(
            "files", type=str, required=False, click_kwargs={"nargs": -1}
        )
        param, ann = map_param_spec("p", "c", spec)
        assert ann == list[str]
        assert param.default.default is None  # optional variadic

    def test_unsupported_option_key_raises_plugin_error(self) -> None:
        spec = PluginParamSpec("weird", is_option=True, click_kwargs={"callback": print})
        with pytest.raises(PluginError, match=r"plugin 'p'.*command 'c'.*param 'weird'.*callback"):
            map_param_spec("p", "c", spec)

    def test_unsupported_argument_key_raises_plugin_error(self) -> None:
        spec = PluginParamSpec("path", is_option=False, click_kwargs={"metavar": "X"})
        with pytest.raises(PluginError, match="metavar"):
            map_param_spec("p", "c", spec)

    def test_non_flag_bool_rejected_loudly(self) -> None:
        # click could express a value-taking bool option (is_flag=False); Typer
        # cannot without a flag pair -- must fail loudly, never drift silently.
        spec = PluginParamSpec(
            "toggle", is_option=True, click_kwargs={"type": bool, "is_flag": False}
        )
        with pytest.raises(PluginError, match="is_flag"):
            map_param_spec("p", "c", spec)

    def test_bool_without_is_flag_rejected_loudly(self) -> None:
        # .option()'s smart default only sets is_flag=True for default=False;
        # a direct-constructed spec with default=True bypasses that heuristic
        # and would otherwise silently map to a one-way flag that can never
        # express False. Must fail loudly, same as is_flag=False.
        spec = PluginParamSpec(
            "enabled", is_option=True, click_kwargs={"type": bool, "default": True}
        )
        with pytest.raises(PluginError, match="bool"):
            map_param_spec("p", "c", spec)

    def test_nargs_two_rejected_loudly(self) -> None:
        # The RFC's closed contract only promises nargs=-1 (variadic); any
        # other nargs value maps to a scalar argument at runtime -- a silent
        # usage-error trap. Must fail loudly at registration instead.
        spec = PluginParamSpec(
            "pair",
            is_option=False,
            click_kwargs={"type": str, "required": False, "nargs": 2},
        )
        with pytest.raises(PluginError, match="nargs"):
            map_param_spec("p", "c", spec)

    def test_variadic_with_default_rejected_loudly(self) -> None:
        # nargs=-1 + a non-None default is unsupported: Typer variadic
        # arguments cannot express a non-empty default, so the default would
        # be silently dropped. Must fail loudly instead.
        spec = PluginParamSpec(
            "files",
            is_option=False,
            click_kwargs={"type": str, "required": False, "nargs": -1, "default": ["a"]},
        )
        with pytest.raises(PluginError, match="nargs=-1"):
            map_param_spec("p", "c", spec)

    def test_required_with_default_raises_plugin_error(self) -> None:
        # The .option()/.argument() constructors reject this combo; a directly
        # constructed spec must hit the same fail-loud wall at the mapper.
        spec = PluginParamSpec(
            "docno", is_option=True, click_kwargs={"type": str, "required": True, "default": "x"}
        )
        with pytest.raises(PluginError, match="required.*cannot also declare a default"):
            map_param_spec("p", "c", spec)


def _capture_body(captured: dict):
    def body(ctx: typer.Context, **kwargs) -> None:
        captured["ctx"] = ctx
        captured["kwargs"] = kwargs
        source = ctx.get_parameter_source("format")
        # name-compare: version-proof across external/vendored ParameterSource enums
        captured["format_explicit"] = source is not None and source.name == "COMMANDLINE"

    return body


def _app_with(fn, name="cmd"):
    app = typer.Typer()
    app.command(name=name)(fn)
    return app


class TestSynthesizeCommandFn:
    def test_declared_default_applied_when_flag_absent(self) -> None:
        # THE regression that motivated the RFC: absent option must get its
        # declared default (typer>=0.26 dropped externally-built defaults).
        captured: dict = {}
        fn = synthesize_command_fn(
            name="search",
            param_specs=[PluginParamSpec.option("county", type=str, default="all")],
            body=_capture_body(captured),
        )
        result = CliRunner().invoke(_app_with(fn, "search"), [])
        assert result.exit_code == 0, result.output
        assert captured["kwargs"]["county"] == "all"

    def test_explicit_value_wins_and_format_detection(self) -> None:
        captured: dict = {}
        fn = synthesize_command_fn(
            name="search",
            param_specs=[PluginParamSpec.option("county", type=str, default="all")],
            body=_capture_body(captured),
        )
        app = _app_with(fn, "search")
        r1 = CliRunner().invoke(app, ["--county", "travis"])
        assert r1.exit_code == 0, r1.output
        assert captured["kwargs"]["county"] == "travis"
        assert captured["format_explicit"] is False  # --format not passed
        r2 = CliRunner().invoke(app, ["--format", "csv"])
        assert r2.exit_code == 0, r2.output
        assert captured["kwargs"]["format"] == "csv"
        assert captured["format_explicit"] is True

    def test_builtin_options_present_with_defaults(self) -> None:
        captured: dict = {}
        fn = synthesize_command_fn(name="c", param_specs=[], body=_capture_body(captured))
        result = CliRunner().invoke(_app_with(fn), [])
        assert result.exit_code == 0, result.output
        assert captured["kwargs"]["format"] == "json"
        assert captured["kwargs"]["view"] == []
        assert captured["kwargs"]["output"] == ""

    def test_short_flags_f_and_o(self) -> None:
        captured: dict = {}
        fn = synthesize_command_fn(name="c", param_specs=[], body=_capture_body(captured))
        with tempfile.NamedTemporaryFile(suffix=".csv") as tmp:
            result = CliRunner().invoke(_app_with(fn), ["-f", "csv", "-o", tmp.name])
            assert result.exit_code == 0, result.output
            assert captured["kwargs"]["format"] == "csv"
            assert captured["kwargs"]["output"] == tmp.name

    def test_no_builtins_mode_for_login(self) -> None:
        captured: dict = {}
        fn = synthesize_command_fn(
            name="login",
            param_specs=[],
            body=_capture_body(captured),
            include_builtin_options=False,
        )
        result = CliRunner().invoke(_app_with(fn, "login"), [])
        assert result.exit_code == 0, result.output
        assert "format" not in captured["kwargs"]

    def test_required_option_missing_errors_exit_2(self) -> None:
        fn = synthesize_command_fn(
            name="document",
            param_specs=[PluginParamSpec.option("docno", type=str, required=True)],
            body=_capture_body({}),
        )
        result = CliRunner().invoke(_app_with(fn, "document"), [])
        assert result.exit_code == 2

    def test_bool_flag_and_variadic_argument(self) -> None:
        captured: dict = {}
        fn = synthesize_command_fn(
            name="run",
            param_specs=[
                PluginParamSpec.argument(
                    "files", type=str, required=False, click_kwargs={"nargs": -1}
                ),
                PluginParamSpec.option("no_wait", type=bool, default=False),
            ],
            body=_capture_body(captured),
        )
        app = _app_with(fn, "run")
        result = CliRunner().invoke(app, ["a.csv", "b.csv", "--no-wait"])
        assert result.exit_code == 0, result.output
        assert list(captured["kwargs"]["files"]) == ["a.csv", "b.csv"]
        assert captured["kwargs"]["no_wait"] is True

    def test_reserved_param_name_ctx_rejected(self) -> None:
        # A plugin param literally named `ctx` collides with the always-
        # injected Context param and would hit inspect.Signature's duplicate
        # -param ValueError; must fail loudly with a clear message instead.
        with pytest.raises(PluginError, match="reserved"):
            synthesize_command_fn(
                name="c",
                param_specs=[PluginParamSpec.option("ctx", type=str, default="x")],
                body=_capture_body({}),
            )

    def test_reserved_param_name_format_rejected(self) -> None:
        # `format`/`view`/`output` are injected as built-ins when
        # include_builtin_options is True (the default); a plugin param
        # sharing one of those names must fail loudly, not collide silently.
        with pytest.raises(PluginError, match="reserved"):
            synthesize_command_fn(
                name="c",
                param_specs=[PluginParamSpec.option("format", type=str, default="x")],
                body=_capture_body({}),
            )

    def test_reserved_param_name_allowed_without_builtins(self) -> None:
        # `format` is only reserved because the built-in options are
        # injected; with include_builtin_options=False (e.g. login commands)
        # it's a perfectly normal plugin param name.
        captured: dict = {}
        fn = synthesize_command_fn(
            name="c",
            param_specs=[PluginParamSpec.option("format", type=str, default="x")],
            body=_capture_body(captured),
            include_builtin_options=False,
        )
        result = CliRunner().invoke(_app_with(fn), [])
        assert result.exit_code == 0, result.output
        assert captured["kwargs"]["format"] == "x"

    def test_ctx_is_injected_click_context(self) -> None:
        captured: dict = {}
        fn = synthesize_command_fn(name="c", param_specs=[], body=_capture_body(captured))
        CliRunner().invoke(_app_with(fn), [])
        # The injected ctx is the RUNNING Click's Context (external click on
        # typer<0.26, vendored typer._click on >=0.26). typer.Context is an
        # annotation MARKER only -- isinstance(ctx, typer.Context) is False on
        # BOTH generations. Assert the behavioral contract instead.
        ctx = captured["ctx"]
        assert type(ctx).__name__ == "Context"
        assert hasattr(ctx, "get_parameter_source")
        assert hasattr(ctx, "find_root")
