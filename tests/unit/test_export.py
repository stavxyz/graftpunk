from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from graftpunk.plugins.export import (
    flatten_dict,
    get_downloads_dir,
    json_to_csv,
    json_to_pdf,
    ordered_keys,
)


class TestFlattenDict:
    def test_flat_dict_unchanged(self) -> None:
        d = {"a": 1, "b": "hello", "c": 3.14}
        result = flatten_dict(d)
        assert result == {"a": 1, "b": "hello", "c": 3.14}

    def test_nested_dict_uses_dot_notation(self) -> None:
        d = {"a": {"b": 1, "c": 2}}
        result = flatten_dict(d)
        assert result == {"a.b": 1, "a.c": 2}

    def test_deeply_nested(self) -> None:
        d = {"a": {"b": {"c": {"d": 42}}}}
        result = flatten_dict(d)
        assert result == {"a.b.c.d": 42}

    def test_custom_separator(self) -> None:
        d = {"a": {"b": 1}}
        result = flatten_dict(d, sep="_")
        assert result == {"a_b": 1}

    def test_custom_parent_key(self) -> None:
        d = {"b": 1}
        result = flatten_dict(d, parent_key="a")
        assert result == {"a.b": 1}

    def test_none_values_preserved(self) -> None:
        d = {"a": None, "b": {"c": None}}
        result = flatten_dict(d)
        assert result == {"a": None, "b.c": None}

    def test_empty_dict_returns_empty(self) -> None:
        assert flatten_dict({}) == {}

    def test_list_of_primitives_joined(self) -> None:
        d = {"tags": ["red", "green", "blue"]}
        result = flatten_dict(d)
        assert result == {"tags": "red, green, blue"}

    def test_list_of_dicts_becomes_json_string(self) -> None:
        d = {"items": [{"id": 1}, {"id": 2}]}
        result = flatten_dict(d)
        assert result["items"] == json.dumps([{"id": 1}, {"id": 2}])

    def test_empty_list_becomes_empty_string(self) -> None:
        d = {"items": []}
        result = flatten_dict(d)
        assert result == {"items": ""}

    def test_bool_values_preserved(self) -> None:
        d = {"active": True, "deleted": False}
        result = flatten_dict(d)
        assert result == {"active": True, "deleted": False}

    def test_mixed_nested_and_flat(self) -> None:
        d = {
            "name": "test",
            "meta": {"version": 1, "tags": ["a", "b"]},
            "active": True,
        }
        result = flatten_dict(d)
        assert result == {
            "name": "test",
            "meta.version": 1,
            "meta.tags": "a, b",
            "active": True,
        }


class TestOrderedKeys:
    def test_single_row(self) -> None:
        rows = [{"a": 1, "b": 2}]
        assert ordered_keys(rows) == ["a", "b"]

    def test_superset_of_keys(self) -> None:
        rows = [{"a": 1, "b": 2}, {"b": 3, "c": 4}]
        assert ordered_keys(rows) == ["a", "b", "c"]

    def test_preserves_first_appearance_order(self) -> None:
        rows = [{"z": 1}, {"a": 2}, {"m": 3}]
        assert ordered_keys(rows) == ["z", "a", "m"]

    def test_empty_rows(self) -> None:
        assert ordered_keys([]) == []

    def test_empty_dicts(self) -> None:
        assert ordered_keys([{}, {}]) == []


class TestJsonToCsv:
    def test_basic_csv(self, tmp_path: Path) -> None:
        data = [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]
        out = tmp_path / "out.csv"
        path, count = json_to_csv(data, out)
        assert count == 2
        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 2
        assert rows[0]["name"] == "Alice"
        assert rows[1]["age"] == "25"

    def test_nested_dicts_flattened(self, tmp_path: Path) -> None:
        data = [{"user": {"name": "Alice", "age": 30}}]
        out = tmp_path / "out.csv"
        path, count = json_to_csv(data, out)
        assert count == 1
        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert "user.name" in rows[0]
        assert rows[0]["user.name"] == "Alice"

    def test_flatten_disabled(self, tmp_path: Path) -> None:
        data = [{"user": {"name": "Alice"}}]
        out = tmp_path / "out.csv"
        path, count = json_to_csv(data, out, flatten=False)
        assert count == 1
        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        # Without flattening, nested dict is written as-is (string repr)
        assert "user" in rows[0]

    def test_empty_data(self, tmp_path: Path) -> None:
        out = tmp_path / "out.csv"
        path, count = json_to_csv([], out)
        assert count == 0
        assert Path(path).exists()
        assert Path(path).read_text() == ""

    def test_superset_columns_from_all_rows(self, tmp_path: Path) -> None:
        data = [{"a": 1, "b": 2}, {"b": 3, "c": 4}]
        out = tmp_path / "out.csv"
        path, _ = json_to_csv(data, out)
        with open(path) as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            rows = list(reader)
        assert fieldnames is not None
        assert "a" in fieldnames
        assert "b" in fieldnames
        assert "c" in fieldnames
        # First row has no "c", should be empty
        assert rows[0]["c"] == ""
        # Second row has no "a", should be empty
        assert rows[1]["a"] == ""

    def test_string_path_accepted(self, tmp_path: Path) -> None:
        data = [{"x": 1}]
        out = str(tmp_path / "out.csv")
        path, count = json_to_csv(data, out)
        assert count == 1
        assert Path(path).exists()

    def test_returns_absolute_path(self, tmp_path: Path) -> None:
        data = [{"x": 1}]
        out = tmp_path / "out.csv"
        path, _ = json_to_csv(data, out)
        assert Path(path).is_absolute()


class TestJsonToPdf:
    def test_basic_pdf(self, tmp_path: Path) -> None:
        data = [{"name": "Alice", "age": 30}]
        out = tmp_path / "out.pdf"
        path, pages = json_to_pdf(data, out)
        assert pages >= 1
        content = Path(path).read_bytes()
        assert content[:5] == b"%PDF-"

    def test_metadata_header_section(self, tmp_path: Path) -> None:
        data = [{"name": "Alice"}]
        out = tmp_path / "out.pdf"
        path, _ = json_to_pdf(data, out, title="Test Report", metadata={"Account": "12345"})
        assert Path(path).exists()
        # PDF was created successfully with metadata -- we verify the file
        # is a valid PDF and non-empty (content verification is visual)
        content = Path(path).read_bytes()
        assert len(content) > 100

    def test_empty_data(self, tmp_path: Path) -> None:
        out = tmp_path / "out.pdf"
        path, pages = json_to_pdf([], out)
        assert pages >= 1
        content = Path(path).read_bytes()
        assert content[:5] == b"%PDF-"

    def test_string_path_accepted(self, tmp_path: Path) -> None:
        data = [{"x": 1}]
        out = str(tmp_path / "out.pdf")
        path, pages = json_to_pdf(data, out)
        assert pages >= 1
        assert Path(path).exists()

    def test_returns_absolute_path(self, tmp_path: Path) -> None:
        data = [{"x": 1}]
        out = tmp_path / "out.pdf"
        path, _ = json_to_pdf(data, out)
        assert Path(path).is_absolute()

    def test_nested_dicts_flattened_in_table(self, tmp_path: Path) -> None:
        data = [{"user": {"name": "Alice", "age": 30}}]
        out = tmp_path / "out.pdf"
        path, pages = json_to_pdf(data, out)
        assert pages >= 1
        assert Path(path).exists()

    def test_flatten_disabled(self, tmp_path: Path) -> None:
        data = [{"user": {"name": "Alice", "age": 30}}]
        out = tmp_path / "out.pdf"
        path, pages = json_to_pdf(data, out, flatten=False)
        assert pages >= 1
        assert Path(path).exists()

    def test_many_rows_creates_multiple_pages(self, tmp_path: Path) -> None:
        data = [{"id": i, "value": f"item-{i}"} for i in range(100)]
        out = tmp_path / "out.pdf"
        path, pages = json_to_pdf(data, out)
        assert pages > 1


class TestGetDownloadsDir:
    """Tests for the get_downloads_dir utility function."""

    def test_default_directory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default download directory is ./gp-downloads/ resolved to absolute."""
        monkeypatch.delenv("GP_DOWNLOADS_DIR", raising=False)
        result = get_downloads_dir()
        assert result == Path("gp-downloads").resolve()
        assert result.is_absolute()

    def test_env_var_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """GP_DOWNLOADS_DIR env var overrides default."""
        custom_dir = tmp_path / "my-downloads"
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(custom_dir))
        result = get_downloads_dir()
        assert result == custom_dir

    def test_creates_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Download directory is created if it doesn't exist."""
        download_dir = tmp_path / "downloads"
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(download_dir))
        result = get_downloads_dir()
        assert result == download_dir
        assert result.is_dir()
