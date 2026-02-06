# Structured Output System Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement configurable table/CSV output formatting so plugin authors can declare which columns to show and users can override at runtime.

**Architecture:** New `output_config.py` module with dataclasses (OutputConfig, ViewConfig, ColumnFilter), extend CommandResult to carry OutputConfig, update formatters to apply column filtering and multi-view rendering.

**Tech Stack:** Python 3.11+, jmespath (optional dependency), rich, dataclasses, click

---

## Task 1: Add jmespath to optional dependencies

**Files:**
- Modify: `pyproject.toml:57-59`

**Step 1: Verify current state**

Run: `grep -A2 "jmespath" pyproject.toml`
Expected: Shows existing jmespath optional dependency

**Step 2: Confirm jmespath is already in optional dependencies**

The jmespath dependency already exists in pyproject.toml lines 57-59. No changes needed.

**Step 3: Commit (skip - no changes)**

No commit needed - dependency already exists.

---

## Task 2: Create output_config.py with dataclasses

**Files:**
- Create: `src/graftpunk/plugins/output_config.py`
- Test: `tests/unit/test_output_config.py`

**Step 1: Write the failing test for ColumnFilter**

```python
"""Tests for output configuration dataclasses."""

import pytest

from graftpunk.plugins.output_config import ColumnFilter


class TestColumnFilter:
    def test_include_mode(self) -> None:
        cf = ColumnFilter(mode="include", columns=["id", "name"])
        assert cf.mode == "include"
        assert cf.columns == ["id", "name"]

    def test_exclude_mode(self) -> None:
        cf = ColumnFilter(mode="exclude", columns=["description"])
        assert cf.mode == "exclude"
        assert cf.columns == ["description"]

    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="mode must be"):
            ColumnFilter(mode="invalid", columns=["id"])

    def test_empty_columns_allowed(self) -> None:
        cf = ColumnFilter(mode="include", columns=[])
        assert cf.columns == []
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_output_config.py::TestColumnFilter -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'graftpunk.plugins.output_config'"

**Step 3: Write minimal implementation**

Create `src/graftpunk/plugins/output_config.py`:

```python
"""Output configuration for structured command responses.

Provides dataclasses for configuring how command output is filtered,
formatted, and displayed across table, CSV, and other formats.
"""

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class ColumnFilter:
    """Filter columns by include or exclude patterns."""

    mode: Literal["include", "exclude"]
    columns: list[str]

    def __post_init__(self) -> None:
        if self.mode not in ("include", "exclude"):
            raise ValueError(f"mode must be 'include' or 'exclude', got {self.mode!r}")
        # Defensive copy
        object.__setattr__(self, "columns", list(self.columns))
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_output_config.py::TestColumnFilter -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/output_config.py tests/unit/test_output_config.py
git commit -m "feat(output): add ColumnFilter dataclass for column filtering"
```

---

## Task 3: Add ColumnDisplayConfig dataclass

**Files:**
- Modify: `src/graftpunk/plugins/output_config.py`
- Modify: `tests/unit/test_output_config.py`

**Step 1: Write the failing test**

Add to `tests/unit/test_output_config.py`:

```python
from graftpunk.plugins.output_config import ColumnDisplayConfig


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
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_output_config.py::TestColumnDisplayConfig -v`
Expected: FAIL with "ImportError: cannot import name 'ColumnDisplayConfig'"

**Step 3: Write minimal implementation**

Add to `src/graftpunk/plugins/output_config.py`:

```python
@dataclass(frozen=True)
class ColumnDisplayConfig:
    """Display configuration for a specific column."""

    name: str
    header: str = ""
    max_width: int = 0
    align: Literal["left", "right", "center"] = "left"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("name must be non-empty")
        if self.align not in ("left", "right", "center"):
            raise ValueError(f"align must be 'left', 'right', or 'center', got {self.align!r}")
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_output_config.py::TestColumnDisplayConfig -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/output_config.py tests/unit/test_output_config.py
git commit -m "feat(output): add ColumnDisplayConfig for column display settings"
```

---

## Task 4: Add ViewConfig dataclass

**Files:**
- Modify: `src/graftpunk/plugins/output_config.py`
- Modify: `tests/unit/test_output_config.py`

**Step 1: Write the failing test**

Add to `tests/unit/test_output_config.py`:

```python
from graftpunk.plugins.output_config import ViewConfig


class TestViewConfig:
    def test_minimal_view(self) -> None:
        view = ViewConfig(name="items")
        assert view.name == "items"
        assert view.path == ""
        assert view.title == ""
        assert view.columns is None
        assert view.display == []

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
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_output_config.py::TestViewConfig -v`
Expected: FAIL with "ImportError: cannot import name 'ViewConfig'"

**Step 3: Write minimal implementation**

Add to `src/graftpunk/plugins/output_config.py`:

```python
@dataclass(frozen=True)
class ViewConfig:
    """Configuration for a single view/table."""

    name: str
    path: str = ""
    title: str = ""
    columns: ColumnFilter | None = None
    display: list[ColumnDisplayConfig] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("name must be non-empty")
        # Defensive copy
        object.__setattr__(self, "display", list(self.display))
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_output_config.py::TestViewConfig -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/output_config.py tests/unit/test_output_config.py
git commit -m "feat(output): add ViewConfig for single view configuration"
```

---

## Task 5: Add OutputConfig dataclass

**Files:**
- Modify: `src/graftpunk/plugins/output_config.py`
- Modify: `tests/unit/test_output_config.py`

**Step 1: Write the failing test**

Add to `tests/unit/test_output_config.py`:

```python
from graftpunk.plugins.output_config import OutputConfig


class TestOutputConfig:
    def test_empty_config(self) -> None:
        cfg = OutputConfig()
        assert cfg.views == []
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
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_output_config.py::TestOutputConfig -v`
Expected: FAIL with "ImportError: cannot import name 'OutputConfig'"

**Step 3: Write minimal implementation**

Add to `src/graftpunk/plugins/output_config.py`:

```python
@dataclass(frozen=True)
class OutputConfig:
    """Complete output configuration for a command."""

    views: list[ViewConfig] = field(default_factory=list)
    default_view: str = ""

    def __post_init__(self) -> None:
        # Defensive copy
        object.__setattr__(self, "views", list(self.views))
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_output_config.py::TestOutputConfig -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/output_config.py tests/unit/test_output_config.py
git commit -m "feat(output): add OutputConfig for complete output configuration"
```

---

## Task 6: Add parse_view_arg helper function

**Files:**
- Modify: `src/graftpunk/plugins/output_config.py`
- Modify: `tests/unit/test_output_config.py`

**Step 1: Write the failing test**

Add to `tests/unit/test_output_config.py`:

```python
from graftpunk.plugins.output_config import parse_view_arg


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
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_output_config.py::TestParseViewArg -v`
Expected: FAIL with "ImportError: cannot import name 'parse_view_arg'"

**Step 3: Write minimal implementation**

Add to `src/graftpunk/plugins/output_config.py`:

```python
def parse_view_arg(arg: str) -> tuple[str, list[str]]:
    """Parse 'name:col1,col2' into (name, [col1, col2]).

    Args:
        arg: View argument in format "name" or "name:col1,col2,..."

    Returns:
        Tuple of (view_name, column_list). Column list is empty if no columns specified.
    """
    if ":" in arg:
        name, cols_str = arg.split(":", 1)
        if cols_str.strip():
            cols = [c.strip() for c in cols_str.split(",") if c.strip()]
        else:
            cols = []
        return name, cols
    return arg, []
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_output_config.py::TestParseViewArg -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/output_config.py tests/unit/test_output_config.py
git commit -m "feat(output): add parse_view_arg for CLI --view parsing"
```

---

## Task 7: Add auto_detect_columns heuristic

**Files:**
- Modify: `src/graftpunk/plugins/output_config.py`
- Modify: `tests/unit/test_output_config.py`

**Step 1: Write the failing test**

Add to `tests/unit/test_output_config.py`:

```python
from graftpunk.plugins.output_config import auto_detect_columns


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
        # Keys only in items 101+ should still be found (via sampling)
        data = [{"common": i} for i in range(150)]
        data[0]["rare"] = "special"
        cols = auto_detect_columns(data)
        assert "common" in cols
        assert "rare" in cols
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_output_config.py::TestAutoDetectColumns -v`
Expected: FAIL with "ImportError: cannot import name 'auto_detect_columns'"

**Step 3: Write minimal implementation**

Add to `src/graftpunk/plugins/output_config.py`:

```python
def auto_detect_columns(data: list[dict], max_cols: int = 8) -> list[str]:
    """Select best columns for display using heuristics.

    Prioritizes identifier columns (id, name, title), deprioritizes
    long text fields (description, content, body), and limits to max_cols.

    Args:
        data: List of dictionaries to analyze.
        max_cols: Maximum number of columns to return.

    Returns:
        List of column names in priority order.
    """
    if not data:
        return []

    # Sample first 100 items for performance
    all_keys: set[str] = set()
    for item in data[:100]:
        if isinstance(item, dict):
            all_keys.update(item.keys())

    def score(key: str) -> tuple[int, str]:
        """Higher score = show first. Secondary sort by key name."""
        key_lower = key.lower()
        if key_lower in ("id", "name", "title"):
            return (3, key)
        if "id" in key_lower or "name" in key_lower:
            return (2, key)
        if key_lower in ("created_at", "updated_at", "date"):
            return (1, key)
        if key_lower in ("description", "content", "body", "text"):
            return (-1, key)
        return (0, key)

    sorted_keys = sorted(all_keys, key=score, reverse=True)
    return sorted_keys[:max_cols]
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_output_config.py::TestAutoDetectColumns -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/output_config.py tests/unit/test_output_config.py
git commit -m "feat(output): add auto_detect_columns heuristic for smart defaults"
```

---

## Task 8: Add apply_column_filter helper

**Files:**
- Modify: `src/graftpunk/plugins/output_config.py`
- Modify: `tests/unit/test_output_config.py`

**Step 1: Write the failing test**

Add to `tests/unit/test_output_config.py`:

```python
from graftpunk.plugins.output_config import apply_column_filter


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
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_output_config.py::TestApplyColumnFilter -v`
Expected: FAIL with "ImportError: cannot import name 'apply_column_filter'"

**Step 3: Write minimal implementation**

Add to `src/graftpunk/plugins/output_config.py`:

```python
def apply_column_filter(
    data: list[dict],
    column_filter: ColumnFilter | None,
) -> list[dict]:
    """Apply column filter to list of dictionaries.

    Args:
        data: List of dictionaries to filter.
        column_filter: Column filter to apply. None means no filtering.

    Returns:
        Filtered list of dictionaries with only requested columns.
    """
    if column_filter is None:
        return data

    result = []
    for row in data:
        if column_filter.mode == "include":
            filtered = {k: row[k] for k in column_filter.columns if k in row}
        else:  # exclude
            filtered = {k: v for k, v in row.items() if k not in column_filter.columns}
        result.append(filtered)
    return result
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_output_config.py::TestApplyColumnFilter -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/output_config.py tests/unit/test_output_config.py
git commit -m "feat(output): add apply_column_filter for filtering dict columns"
```

---

## Task 9: Add extract_view_data with JMESPath

**Files:**
- Modify: `src/graftpunk/plugins/output_config.py`
- Modify: `tests/unit/test_output_config.py`

**Step 1: Write the failing test**

Add to `tests/unit/test_output_config.py`:

```python
from graftpunk.plugins.output_config import extract_view_data


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
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_output_config.py::TestExtractViewData -v`
Expected: FAIL with "ImportError: cannot import name 'extract_view_data'"

**Step 3: Write minimal implementation**

Add to `src/graftpunk/plugins/output_config.py`:

```python
def extract_view_data(data: dict, path: str) -> object:
    """Extract data from nested dict using dot-notation path.

    Uses jmespath if available, otherwise falls back to simple dot-notation.

    Args:
        data: Source dictionary.
        path: Dot-notation path (e.g., "results.items").

    Returns:
        Extracted data or None if path not found.
    """
    if not path:
        return data

    try:
        import jmespath

        return jmespath.search(path, data)
    except ImportError:
        # Fallback to simple dot-notation
        result = data
        for key in path.split("."):
            if isinstance(result, dict) and key in result:
                result = result[key]
            else:
                return None
        return result
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_output_config.py::TestExtractViewData -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/output_config.py tests/unit/test_output_config.py
git commit -m "feat(output): add extract_view_data with JMESPath/dot-notation fallback"
```

---

## Task 10: Export types from plugins __init__.py

**Files:**
- Modify: `src/graftpunk/plugins/__init__.py`

**Step 1: Read current exports**

Run: `head -50 /Users/stavxyz/src/graftpunk/src/graftpunk/plugins/__init__.py`

**Step 2: Add output_config exports**

Add to `src/graftpunk/plugins/__init__.py` imports:

```python
from graftpunk.plugins.output_config import (
    ColumnDisplayConfig,
    ColumnFilter,
    OutputConfig,
    ViewConfig,
    apply_column_filter,
    auto_detect_columns,
    extract_view_data,
    parse_view_arg,
)
```

And add to `__all__`:

```python
    "ColumnDisplayConfig",
    "ColumnFilter",
    "OutputConfig",
    "ViewConfig",
    "apply_column_filter",
    "auto_detect_columns",
    "extract_view_data",
    "parse_view_arg",
```

**Step 3: Verify import works**

Run: `cd /Users/stavxyz/src/graftpunk && python -c "from graftpunk.plugins import OutputConfig, ViewConfig, ColumnFilter; print('OK')"`
Expected: OK

**Step 4: Commit**

```bash
git add src/graftpunk/plugins/__init__.py
git commit -m "feat(output): export OutputConfig types from plugins module"
```

---

## Task 11: Add output_config field to CommandResult

**Files:**
- Modify: `src/graftpunk/plugins/cli_plugin.py:245-256`
- Modify: `tests/unit/test_cli_plugin.py` (add test)

**Step 1: Write the failing test**

Add to existing test file or create new test:

```python
def test_command_result_accepts_output_config() -> None:
    from graftpunk.plugins import OutputConfig, ViewConfig, ColumnFilter
    from graftpunk.plugins.cli_plugin import CommandResult

    cfg = OutputConfig(
        views=[ViewConfig(name="items", columns=ColumnFilter("include", ["id"]))],
    )
    result = CommandResult(data={"items": []}, output_config=cfg)
    assert result.output_config is not None
    assert len(result.output_config.views) == 1


def test_command_result_output_config_defaults_none() -> None:
    from graftpunk.plugins.cli_plugin import CommandResult

    result = CommandResult(data={"items": []})
    assert result.output_config is None
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_cli_plugin.py -k "output_config" -v`
Expected: FAIL with "TypeError: __init__() got an unexpected keyword argument 'output_config'"

**Step 3: Modify CommandResult dataclass**

In `src/graftpunk/plugins/cli_plugin.py`, modify the CommandResult class:

```python
from graftpunk.plugins.output_config import OutputConfig  # Add at top with other imports

@dataclass(frozen=True)
class CommandResult:
    """Structured return type for command handlers."""

    data: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    format_hint: Literal["json", "table", "raw", "csv"] | None = None
    output_config: OutputConfig | None = None  # NEW FIELD
```

Note: Use TYPE_CHECKING guard for the import to avoid circular imports.

**Step 4: Run test to verify it passes**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_cli_plugin.py -k "output_config" -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/cli_plugin.py tests/unit/test_cli_plugin.py
git commit -m "feat(output): add output_config field to CommandResult"
```

---

## Task 12: Update TableFormatter to use OutputConfig

**Files:**
- Modify: `src/graftpunk/plugins/formatters.py:57-90`
- Modify: `tests/unit/test_formatters.py`

**Step 1: Write the failing test**

Add to `tests/unit/test_formatters.py`:

```python
from graftpunk.plugins import ColumnFilter, OutputConfig, ViewConfig


class TestTableFormatterWithOutputConfig:
    def test_applies_column_filter(self) -> None:
        console = MagicMock(spec=Console)
        data = [{"id": 1, "name": "foo", "desc": "long"}]
        cfg = OutputConfig(
            views=[ViewConfig(name="default", columns=ColumnFilter("include", ["id", "name"]))],
        )
        TableFormatter().format(data, console, output_config=cfg)
        console.print.assert_called_once()
        table = console.print.call_args[0][0]
        assert isinstance(table, Table)
        assert len(table.columns) == 2

    def test_no_config_uses_all_columns(self) -> None:
        console = MagicMock(spec=Console)
        data = [{"id": 1, "name": "foo", "desc": "long"}]
        TableFormatter().format(data, console, output_config=None)
        console.print.assert_called_once()
        table = console.print.call_args[0][0]
        assert len(table.columns) == 3
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_formatters.py::TestTableFormatterWithOutputConfig -v`
Expected: FAIL with "TypeError: format() got an unexpected keyword argument 'output_config'"

**Step 3: Update TableFormatter.format signature and logic**

Modify `src/graftpunk/plugins/formatters.py`:

```python
from graftpunk.plugins.output_config import OutputConfig, apply_column_filter, extract_view_data

class TableFormatter:
    """Output as a rich table (for lists of dicts or single dicts)."""

    name = "table"

    def format(
        self,
        data: Any,
        console: Console,
        output_config: OutputConfig | None = None,
    ) -> None:
        # Apply output config if provided
        if output_config and output_config.views:
            view = output_config.views[0]  # Use first view for now
            if view.path:
                data = extract_view_data(data, view.path)
            if isinstance(data, list) and data and isinstance(data[0], dict):
                data = apply_column_filter(data, view.columns)

        if isinstance(data, list) and data and isinstance(data[0], dict):
            headers = list(data[0].keys())
            table = Table(header_style="bold cyan", border_style="dim")
            for header in headers:
                table.add_column(header)
            for row in data:
                table.add_row(*[str(row.get(h, "")) for h in headers])
            console.print(table)
        elif isinstance(data, dict):
            # ... existing dict handling
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_formatters.py::TestTableFormatterWithOutputConfig -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/formatters.py tests/unit/test_formatters.py
git commit -m "feat(output): TableFormatter applies OutputConfig column filtering"
```

---

## Task 13: Update OutputFormatter protocol

**Files:**
- Modify: `src/graftpunk/plugins/formatters.py:24-39`

**Step 1: Update the Protocol**

Modify `OutputFormatter` protocol to include optional output_config:

```python
@runtime_checkable
class OutputFormatter(Protocol):
    """Protocol for custom output formatters."""

    @property
    def name(self) -> str:
        """Formatter name used in --format flag."""
        ...

    def format(
        self,
        data: Any,
        console: Console,
        output_config: OutputConfig | None = None,
    ) -> None:
        """Format and print data to the console."""
        ...
```

**Step 2: Update all formatters**

Update JsonFormatter, RawFormatter, and CsvFormatter to accept output_config parameter:

```python
class JsonFormatter:
    name = "json"

    def format(
        self,
        data: Any,
        console: Console,
        output_config: OutputConfig | None = None,
    ) -> None:
        # JSON formatter ignores output_config - shows full data
        json_str = json.dumps(data, indent=2, default=str)
        console.print(JSON(json_str))
```

**Step 3: Verify all tests pass**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_formatters.py -v`
Expected: All PASS

**Step 4: Commit**

```bash
git add src/graftpunk/plugins/formatters.py
git commit -m "feat(output): update OutputFormatter protocol with output_config param"
```

---

## Task 14: Update format_output to pass OutputConfig

**Files:**
- Modify: `src/graftpunk/plugins/formatters.py:200-225`
- Modify: `tests/unit/test_formatters.py`

**Step 1: Write the failing test**

Add to `tests/unit/test_formatters.py`:

```python
class TestFormatOutputWithOutputConfig:
    def test_extracts_output_config_from_command_result(self) -> None:
        console = MagicMock(spec=Console)
        cfg = OutputConfig(
            views=[ViewConfig(name="default", columns=ColumnFilter("include", ["id"]))],
        )
        result = CommandResult(
            data=[{"id": 1, "name": "foo"}],
            output_config=cfg,
        )
        format_output(result, "table", console)
        console.print.assert_called_once()
        table = console.print.call_args[0][0]
        assert len(table.columns) == 1  # Only "id"
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_formatters.py::TestFormatOutputWithOutputConfig -v`
Expected: FAIL (columns not filtered)

**Step 3: Update format_output function**

Modify `format_output` in `src/graftpunk/plugins/formatters.py`:

```python
def format_output(data: Any, format_type: str, console: Console) -> None:
    """Format and print command output."""
    formatters = discover_formatters()
    formatter = formatters.get(format_type)
    if formatter is None:
        LOG.warning("unknown_format", format=format_type)
        formatter = formatters["json"]

    output_config = None

    # Unwrap CommandResult
    if isinstance(data, CommandResult):
        output_config = data.output_config  # Extract config
        if data.format_hint and data.format_hint in formatters and format_type == "json":
            formatter = formatters[data.format_hint]
        data = data.data

    formatter.format(data, console, output_config=output_config)
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_formatters.py::TestFormatOutputWithOutputConfig -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/formatters.py tests/unit/test_formatters.py
git commit -m "feat(output): format_output extracts and passes OutputConfig"
```

---

## Task 15: Update CsvFormatter to use OutputConfig

**Files:**
- Modify: `src/graftpunk/plugins/formatters.py:104-152`
- Modify: `tests/unit/test_formatters.py`

**Step 1: Write the failing test**

Add to `tests/unit/test_formatters.py`:

```python
class TestCsvFormatterWithOutputConfig:
    def test_applies_column_filter(self) -> None:
        console = MagicMock(spec=Console)
        data = [{"id": 1, "name": "foo", "desc": "long"}]
        cfg = OutputConfig(
            views=[ViewConfig(name="default", columns=ColumnFilter("include", ["id", "name"]))],
        )
        CsvFormatter().format(data, console, output_config=cfg)
        rows = _parse_csv_output(console)
        assert rows[0] == ["id", "name"]  # Only included columns
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_formatters.py::TestCsvFormatterWithOutputConfig -v`
Expected: FAIL (all columns present)

**Step 3: Update CsvFormatter**

Modify CsvFormatter.format to apply column filtering:

```python
class CsvFormatter:
    name = "csv"

    def format(
        self,
        data: Any,
        console: Console,
        output_config: OutputConfig | None = None,
    ) -> None:
        if isinstance(data, str):
            console.print(data)
            return
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            RawFormatter().format(data, console)
            return
        if not data:
            return
        if not all(isinstance(item, dict) for item in data):
            RawFormatter().format(data, console)
            return

        # Apply output config if provided
        if output_config and output_config.views:
            view = output_config.views[0]
            if view.path:
                data = extract_view_data(data, view.path)
                if not isinstance(data, list):
                    data = [data] if data else []
            data = apply_column_filter(data, view.columns)

        if not data:
            return

        # ... rest of CSV logic unchanged
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_formatters.py::TestCsvFormatterWithOutputConfig -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/formatters.py tests/unit/test_formatters.py
git commit -m "feat(output): CsvFormatter applies OutputConfig column filtering"
```

---

## Task 16: Run full test suite

**Files:**
- No changes

**Step 1: Run all formatter tests**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/unit/test_formatters.py tests/unit/test_output_config.py -v`
Expected: All PASS

**Step 2: Run full test suite**

Run: `cd /Users/stavxyz/src/graftpunk && python -m pytest tests/ -v`
Expected: All PASS

**Step 3: Run linter**

Run: `cd /Users/stavxyz/src/graftpunk && ruff check src/graftpunk/plugins/output_config.py src/graftpunk/plugins/formatters.py`
Expected: No errors

**Step 4: Commit (if any fixes needed)**

Only commit if there were linting fixes.

---

## Task 17: Create integration test with UNFI plugin

**Files:**
- Modify: `/Users/stavxyz/src/graftpunk-plugins-2/src/graftpunk_unfi/plugin.py`
- Modify: `/Users/stavxyz/src/graftpunk-plugins-2/tests/test_unfi_plugin.py`

**Step 1: Update UNFI search command to use OutputConfig**

Add OutputConfig to the search command return:

```python
from graftpunk.plugins import CommandResult, OutputConfig, ViewConfig, ColumnFilter

def search(self, ctx, query, customer_number="", dept=0, page=1, size=48, items_only=False):
    # ... existing code ...
    result = resp.json()

    if items_only:
        return result.get("results", {}).get("items", [])

    return CommandResult(
        data=result,
        output_config=OutputConfig(
            views=[
                ViewConfig(
                    name="items",
                    path="results.items",
                    columns=ColumnFilter("include", [
                        "itemNumber", "itemDescription", "brandDescription",
                        "packSize", "totalPrice",
                    ]),
                ),
            ],
            default_view="items",
        ),
    )
```

**Step 2: Test the plugin**

Run: `cd /Users/stavxyz/src/graftpunk-plugins-2 && python -m pytest tests/ -v`
Expected: All PASS

**Step 3: Commit**

```bash
cd /Users/stavxyz/src/graftpunk-plugins-2
git add src/graftpunk_unfi/plugin.py
git commit -m "feat: add OutputConfig to UNFI item search for clean table output"
```

---

## Summary

This plan implements the Structured Output System in 17 tasks:

1. **Tasks 1-9**: Create output_config.py with all dataclasses and helper functions
2. **Task 10**: Export types from plugins module
3. **Task 11**: Extend CommandResult with output_config field
4. **Tasks 12-15**: Update formatters to use OutputConfig
5. **Task 16**: Validate with full test suite
6. **Task 17**: Integration test with UNFI plugin

Each task follows TDD: write failing test, implement, verify pass, commit.
