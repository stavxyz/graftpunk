"""Tests for the Typer-native command factory (construction only)."""

from __future__ import annotations

import inspect

import pytest
import typer

from graftpunk.cli.command_factory import map_param_spec
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
