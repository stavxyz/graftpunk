"""Tests for output configuration dataclasses."""

import pytest

from graftpunk.plugins.cli_plugin import CommandResult
from graftpunk.plugins.formatters import format_output
from graftpunk.plugins.output_config import (
    ColumnDisplayConfig,
    ColumnFilter,
    OutputConfig,
    ViewConfig,
    apply_column_filter,
    auto_detect_columns,
    extract_view_data,
    parse_view_arg,
    select_views,
)


class TestColumnFilter:
    def test_include_mode(self) -> None:
        cf = ColumnFilter(mode="include", columns=["id", "name"])
        assert cf.mode == "include"
        assert cf.columns == ("id", "name")

    def test_exclude_mode(self) -> None:
        cf = ColumnFilter(mode="exclude", columns=["description"])
        assert cf.mode == "exclude"
        assert cf.columns == ("description",)

    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="mode must be"):
            ColumnFilter(mode="invalid", columns=["id"])

    def test_empty_columns_allowed(self) -> None:
        cf = ColumnFilter(mode="include", columns=[])
        assert cf.columns == ()

    def test_accepts_list_input(self) -> None:
        cf = ColumnFilter(mode="include", columns=["id", "name"])
        assert cf.columns == ("id", "name")


class TestColumnDisplayConfig:
    def test_minimal_config(self) -> None:
        cfg = ColumnDisplayConfig(name="id")
        assert cfg.name == "id"
        assert cfg.header == ""
        assert cfg.max_width == 0
        assert cfg.align == "left"

    def test_full_config(self) -> None:
        cfg = ColumnDisplayConfig(
            name="price",
            header="Price ($)",
            max_width=10,
            align="right",
        )
        assert cfg.name == "price"
        assert cfg.header == "Price ($)"
        assert cfg.max_width == 10
        assert cfg.align == "right"

    def test_invalid_align_rejected(self) -> None:
        with pytest.raises(ValueError, match="align must be"):
            ColumnDisplayConfig(name="id", align="middle")

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="name must be non-empty"):
            ColumnDisplayConfig(name="")

    def test_negative_max_width_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_width must be >= 0"):
            ColumnDisplayConfig(name="id", max_width=-1)


class TestViewConfig:
    def test_minimal_view(self) -> None:
        view = ViewConfig(name="items")
        assert view.name == "items"
        assert view.path == ""
        assert view.title == ""
        assert view.columns is None
        assert view.display == ()

    def test_full_view(self) -> None:
        view = ViewConfig(
            name="items",
            path="results.items",
            title="Product Items",
            columns=ColumnFilter("include", ["id", "name"]),
            display=[ColumnDisplayConfig(name="id", header="ID")],
        )
        assert view.name == "items"
        assert view.path == "results.items"
        assert view.title == "Product Items"
        assert view.columns is not None
        assert view.columns.mode == "include"
        assert len(view.display) == 1

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="name must be non-empty"):
            ViewConfig(name="")


class TestOutputConfig:
    def test_empty_config(self) -> None:
        cfg = OutputConfig()
        assert cfg.views == ()
        assert cfg.default_view == ""

    def test_single_view(self) -> None:
        cfg = OutputConfig(
            views=[ViewConfig(name="items", columns=ColumnFilter("include", ["id"]))],
            default_view="items",
        )
        assert len(cfg.views) == 1
        assert cfg.default_view == "items"

    def test_multiple_views(self) -> None:
        cfg = OutputConfig(
            views=[
                ViewConfig(name="items"),
                ViewConfig(name="facets"),
            ],
            default_view="items",
        )
        assert len(cfg.views) == 2

    def test_duplicate_view_names_rejected(self) -> None:
        with pytest.raises(ValueError, match="Duplicate view names"):
            OutputConfig(
                views=[
                    ViewConfig(name="items"),
                    ViewConfig(name="items"),
                ],
            )

    def test_invalid_default_view_rejected(self) -> None:
        with pytest.raises(ValueError, match="default_view 'missing' not found"):
            OutputConfig(
                views=[ViewConfig(name="items")],
                default_view="missing",
            )

    def test_get_view_by_name(self) -> None:
        cfg = OutputConfig(
            views=[
                ViewConfig(name="items", path="results.items"),
                ViewConfig(name="facets", path="results.facets"),
            ],
        )
        view = cfg.get_view("facets")
        assert view is not None
        assert view.path == "results.facets"

    def test_get_view_not_found(self) -> None:
        cfg = OutputConfig(views=[ViewConfig(name="items")])
        assert cfg.get_view("missing") is None

    def test_get_default_view_explicit(self) -> None:
        cfg = OutputConfig(
            views=[
                ViewConfig(name="items"),
                ViewConfig(name="facets"),
            ],
            default_view="facets",
        )
        view = cfg.get_default_view()
        assert view is not None
        assert view.name == "facets"

    def test_get_default_view_implicit(self) -> None:
        cfg = OutputConfig(
            views=[
                ViewConfig(name="first"),
                ViewConfig(name="second"),
            ],
        )
        view = cfg.get_default_view()
        assert view is not None
        assert view.name == "first"

    def test_get_default_view_empty(self) -> None:
        cfg = OutputConfig()
        assert cfg.get_default_view() is None


class TestFilterViews:
    def test_filter_by_name(self) -> None:
        cfg = OutputConfig(
            views=[
                ViewConfig(name="items", title="Items"),
                ViewConfig(name="page", title="Page Info"),
                ViewConfig(name="facets", title="Facets"),
            ],
        )
        filtered = cfg.filter_views(["items", "facets"])
        assert len(filtered.views) == 2
        assert filtered.views[0].name == "items"
        assert filtered.views[1].name == "facets"

    def test_filter_preserves_order_of_request(self) -> None:
        cfg = OutputConfig(
            views=[
                ViewConfig(name="items", title="Items"),
                ViewConfig(name="page", title="Page Info"),
            ],
        )
        filtered = cfg.filter_views(["page", "items"])
        assert filtered.views[0].name == "page"
        assert filtered.views[1].name == "items"

    def test_filter_unknown_names_skipped(self) -> None:
        cfg = OutputConfig(views=[ViewConfig(name="items", title="Items")])
        filtered = cfg.filter_views(["items", "nonexistent"])
        assert len(filtered.views) == 1
        assert filtered.views[0].name == "items"

    def test_filter_with_column_overrides(self) -> None:
        cfg = OutputConfig(
            views=[
                ViewConfig(
                    name="items",
                    columns=ColumnFilter("include", ["id", "name", "desc"]),
                ),
            ],
        )
        overrides = {"items": ["id", "name"]}
        filtered = cfg.filter_views(["items"], column_overrides=overrides)
        assert filtered.views[0].columns is not None
        assert filtered.views[0].columns.columns == ("id", "name")
        assert filtered.views[0].columns.mode == "include"

    def test_filter_empty_names_returns_empty(self) -> None:
        cfg = OutputConfig(views=[ViewConfig(name="items", title="Items")])
        filtered = cfg.filter_views([])
        assert len(filtered.views) == 0

    def test_filter_returns_new_instance(self) -> None:
        cfg = OutputConfig(
            views=[ViewConfig(name="items"), ViewConfig(name="page")],
        )
        filtered = cfg.filter_views(["items"])
        assert len(cfg.views) == 2  # Original unchanged
        assert len(filtered.views) == 1

    def test_filter_with_column_overrides_preserves_other_fields(self) -> None:
        cfg = OutputConfig(
            views=[
                ViewConfig(
                    name="items",
                    path="results.items",
                    title="Product Items",
                    columns=ColumnFilter("include", ["id", "name", "desc"]),
                    display=[ColumnDisplayConfig(name="id", header="ID")],
                ),
            ],
        )
        overrides = {"items": ["id", "name"]}
        filtered = cfg.filter_views(["items"], column_overrides=overrides)
        view = filtered.views[0]
        assert view.path == "results.items"
        assert view.title == "Product Items"
        assert len(view.display) == 1
        assert view.display[0].header == "ID"

    def test_filter_preserves_default_view_when_in_filtered_set(self) -> None:
        cfg = OutputConfig(
            views=[
                ViewConfig(name="items"),
                ViewConfig(name="page"),
                ViewConfig(name="facets"),
            ],
            default_view="page",
        )
        filtered = cfg.filter_views(["items", "page"])
        assert filtered.default_view == "page"

    def test_filter_drops_default_view_when_not_in_filtered_set(self) -> None:
        cfg = OutputConfig(
            views=[
                ViewConfig(name="items"),
                ViewConfig(name="page"),
            ],
            default_view="page",
        )
        filtered = cfg.filter_views(["items"])
        assert filtered.default_view == ""


class TestParseViewArg:
    def test_name_and_columns(self) -> None:
        name, cols = parse_view_arg("items:id,name,brand")
        assert name == "items"
        assert cols == ["id", "name", "brand"]

    def test_name_only(self) -> None:
        name, cols = parse_view_arg("items")
        assert name == "items"
        assert cols == []

    def test_columns_with_spaces(self) -> None:
        name, cols = parse_view_arg("items:id, name, brand")
        assert name == "items"
        assert cols == ["id", "name", "brand"]

    def test_empty_columns(self) -> None:
        name, cols = parse_view_arg("items:")
        assert name == "items"
        assert cols == []


class TestAutoDetectColumns:
    def test_empty_list(self) -> None:
        assert auto_detect_columns([]) == []

    def test_prioritizes_id_name_title(self) -> None:
        data = [{"description": "long", "id": "1", "name": "foo", "other": "x"}]
        cols = auto_detect_columns(data, max_cols=3)
        assert cols[:2] == ["id", "name"]

    def test_deprioritizes_description(self) -> None:
        data = [{"id": "1", "name": "foo", "description": "long text", "status": "ok"}]
        cols = auto_detect_columns(data, max_cols=3)
        assert "description" not in cols

    def test_limits_to_max_cols(self) -> None:
        data = [{"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}]
        cols = auto_detect_columns(data, max_cols=3)
        assert len(cols) == 3

    def test_samples_first_100_items(self) -> None:
        data = [{"common": i} for i in range(150)]
        data[0]["rare"] = "special"
        cols = auto_detect_columns(data)
        assert "common" in cols
        assert "rare" in cols

    def test_ignores_non_dict_items(self) -> None:
        data = [{"id": 1}, "not a dict", {"name": "foo"}]  # type: ignore[list-item]
        cols = auto_detect_columns(data)
        assert "id" in cols
        assert "name" in cols


class TestApplyColumnFilter:
    def test_include_mode(self) -> None:
        data = [{"id": 1, "name": "foo", "desc": "long"}]
        cf = ColumnFilter("include", ["id", "name"])
        result = apply_column_filter(data, cf)
        assert result == [{"id": 1, "name": "foo"}]

    def test_exclude_mode(self) -> None:
        data = [{"id": 1, "name": "foo", "desc": "long"}]
        cf = ColumnFilter("exclude", ["desc"])
        result = apply_column_filter(data, cf)
        assert result == [{"id": 1, "name": "foo"}]

    def test_preserves_order(self) -> None:
        data = [{"c": 3, "b": 2, "a": 1}]
        cf = ColumnFilter("include", ["a", "b"])
        result = apply_column_filter(data, cf)
        assert list(result[0].keys()) == ["a", "b"]

    def test_missing_columns_ignored(self) -> None:
        data = [{"id": 1, "name": "foo"}]
        cf = ColumnFilter("include", ["id", "missing"])
        result = apply_column_filter(data, cf)
        assert result == [{"id": 1}]

    def test_empty_filter_returns_original(self) -> None:
        data = [{"id": 1, "name": "foo"}]
        result = apply_column_filter(data, None)
        assert result == data


class TestExtractViewData:
    def test_empty_path_returns_root(self) -> None:
        data = {"items": [1, 2, 3]}
        result = extract_view_data(data, "")
        assert result == data

    def test_simple_path(self) -> None:
        data = {"results": {"items": [1, 2, 3]}}
        result = extract_view_data(data, "results.items")
        assert result == [1, 2, 3]

    def test_nested_path(self) -> None:
        data = {"a": {"b": {"c": "value"}}}
        result = extract_view_data(data, "a.b.c")
        assert result == "value"

    def test_missing_path_returns_none(self) -> None:
        data = {"items": [1, 2, 3]}
        result = extract_view_data(data, "missing.path")
        assert result is None

    def test_jmespath_array_access(self) -> None:
        """Test JMESPath-style array access when jmespath is installed."""
        pytest.importorskip("jmespath")
        data = {"results": [{"name": "first"}, {"name": "second"}]}
        result = extract_view_data(data, "results[0].name")
        assert result == "first"

    def test_jmespath_filter_expression(self) -> None:
        """Test JMESPath filter expressions when jmespath is installed."""
        pytest.importorskip("jmespath")
        data = {"items": [{"id": 1, "active": True}, {"id": 2, "active": False}]}
        result = extract_view_data(data, "items[?active]")
        assert result == [{"id": 1, "active": True}]


class TestRepeatedViewArgs:
    """#292: --view items:a --view items:b raised ValueError: Duplicate view
    names after the command's request had run."""

    def _cfg(self) -> OutputConfig:
        return OutputConfig(
            views=[
                ViewConfig(name="items", title="Items"),
                ViewConfig(name="picked", columns=ColumnFilter("include", ("id", "sku"))),
                ViewConfig(name="page", title="Page"),
            ],
        )

    def test_repeats_merge_as_the_union_in_first_seen_order(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = select_views(self._cfg(), ["items:a,b", "page", "items:c,a"])
        assert [v.name for v in out.views] == ["items", "page"]
        assert out.views[0].columns == ColumnFilter("include", ("a", "b", "c"))
        assert out.views[1].columns is None
        err = capsys.readouterr().err
        assert err.count("--view given more than once") == 1
        assert "items" in err

    def test_one_warning_names_every_repeated_view(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        select_views(self._cfg(), ["items:a", "page", "items:b", "page", "items:c"])
        err = capsys.readouterr().err
        assert err.count("--view given more than once") == 1
        assert "items, page" in err

    def test_no_repeat_no_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        out = select_views(self._cfg(), ["items:a", "page"])
        assert out.views[0].columns == ColumnFilter("include", ("a",))
        assert capsys.readouterr().err == ""

    def test_a_bare_repeat_of_an_unfiltered_view_shows_every_column(self) -> None:
        out = select_views(self._cfg(), ["items", "items:a"])
        assert [v.name for v in out.views] == ["items"]
        assert out.views[0].columns is None

    def test_a_bare_repeat_of_an_include_view_joins_its_own_columns(self) -> None:
        out = select_views(self._cfg(), ["picked:name", "picked"])
        assert out.views[0].columns == ColumnFilter("include", ("name", "id", "sku"))

    def test_a_view_only_ever_named_bare_keeps_its_own_filter(self) -> None:
        out = select_views(self._cfg(), ["picked", "picked"])
        assert out.views[0].columns == ColumnFilter("include", ("id", "sku"))

    def test_filter_views_keeps_the_first_of_a_repeated_name(self) -> None:
        out = self._cfg().filter_views(["page", "items", "page"])
        assert [v.name for v in out.views] == ["page", "items"]

    def test_command_result_export_merges_repeated_views(self) -> None:
        result = CommandResult(
            data={"items": [{"a": 1, "b": 2, "c": 3}]},
            output_config=OutputConfig(views=[ViewConfig(name="items", path="items")]),
        )
        text = result.export(format="csv", views=("items:a", "items:c"))
        assert isinstance(text, str)
        assert text.splitlines()[0] == "a,c"

    def test_format_output_merges_repeated_views(self, capsys: pytest.CaptureFixture[str]) -> None:
        import io

        from rich.console import Console

        buf = io.StringIO()
        result = CommandResult(
            data={"items": [{"a": 1, "b": 2, "c": 3}]},
            output_config=OutputConfig(views=[ViewConfig(name="items", path="items")]),
        )
        format_output(
            result, "csv", Console(file=buf), view_args=("items:b", "items:a"), user_explicit=True
        )
        assert buf.getvalue().splitlines()[0] == "b,a"
        assert capsys.readouterr().err.count("--view given more than once") == 1
