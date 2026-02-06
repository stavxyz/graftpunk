# Structured Output System Design

**Date:** 2026-02-06
**Status:** Implemented (PR #78)
**Authors:** stavxyz, Claude

## 1. Problem Statement & Goals

### Problem

When commands return complex nested JSON data, the table formatter produces unusable output:

- **Too many columns**: 30+ columns from flat object with nested structures
- **Long text values**: Description fields truncate poorly or wrap badly
- **Nested objects**: JSON-dumped into cells (unreadable)
- **No column control**: Users can't select which fields matter to them
- **Multiple data views**: Single response may contain items, pagination, facets - all need different treatment

### Goals

1. **Plugin authors** can declare which columns to show by default
2. **Users** can override column selection at runtime via CLI flags
3. **Multiple views** from a single command (items table, pagination summary, facets)
4. **Format-agnostic** config works for table, CSV, JSON, XLSX
5. **Sensible defaults** via auto-detection when no config exists
6. **Principle of least astonishment** - CLI overrides surgically modify, not wholesale replace

### Non-Goals

- Full JMESPath query language at CLI (too complex)
- Interactive column selection (out of scope)
- Custom cell renderers beyond truncation/formatting

---

## 2. Data Model

All dataclasses are frozen (immutable) with comprehensive validation in `__post_init__`.

```python
from dataclasses import dataclass, field
from typing import Literal

@dataclass(frozen=True)
class ColumnFilter:
    """Filter columns by exact name matching.

    Args:
        mode: Either "include" (keep only listed columns) or "exclude" (remove listed).
        columns: List of exact column names to include or exclude.
    """
    mode: Literal["include", "exclude"]
    columns: list[str]
    # Validation: mode must be "include" or "exclude"

@dataclass(frozen=True)
class ColumnDisplayConfig:
    """Display configuration for a specific column."""
    name: str           # Required, must be non-empty
    header: str = ""    # Display header (defaults to name)
    max_width: int = 0  # 0 = no limit, must be >= 0
    align: Literal["left", "right", "center"] = "left"

@dataclass(frozen=True)
class ViewConfig:
    """Configuration for a single view/table."""
    name: str                    # Required, must be non-empty
    path: str = ""               # JMESPath to extract data (empty = root)
    title: str = ""              # Display title (optional)
    columns: ColumnFilter | None = None
    display: list[ColumnDisplayConfig] = field(default_factory=list)

@dataclass(frozen=True)
class OutputConfig:
    """Complete output configuration for a command."""
    views: list[ViewConfig] = field(default_factory=list)
    default_view: str = ""  # Must reference existing view name if set

    def get_view(self, name: str) -> ViewConfig | None:
        """Get a view by name, or None if not found."""
        ...

    def get_default_view(self) -> ViewConfig | None:
        """Get the default view (by default_view name or first view)."""
        ...
    # Validation: view names must be unique, default_view must exist
```

### Key Design Decisions

- **Frozen dataclasses** - All types are immutable for safety and caching
- **ColumnFilter** uses explicit `mode` to avoid ambiguity between include/exclude
- **ViewConfig.path** uses JMESPath for extracting nested data (with dot-notation fallback)
- **OutputConfig** supports multiple views with `get_default_view()` helper
- **Validation** catches configuration errors early with clear error messages
- **display** configs are optional - columns auto-detect width/alignment if not specified

---

## 3. CLI Interface

### New Global Flags

```
--view NAME:COL1,COL2,...   Render view NAME with specified columns (repeatable)
--columns COL1,COL2,...     Filter columns for default view (shorthand)
--exclude COL1,COL2,...     Exclude columns from output
--raw                       Bypass all filtering, show complete data
```

### Examples

```bash
# Single view with column selection
gp unfi item search oil --view items:id,name,brand,price

# Multiple views
gp unfi item search oil --view items:id,name --view facets:name,count

# Column exclusion
gp unfi item search oil --columns id,name,brand --exclude description

# Shorthand for default view
gp unfi item search oil --columns id,name,brand

# Bypass all filtering
gp unfi item search oil --raw
```

### Parsing `--view`

```python
def parse_view_arg(arg: str) -> tuple[str, list[str]]:
    """Parse 'name:col1,col2' into (name, [col1, col2])."""
    if ":" in arg:
        name, cols = arg.split(":", 1)
        return name, [c.strip() for c in cols.split(",")]
    return arg, []  # View name only, use configured columns
```

### Click Implementation

```python
@click.option(
    "--view",
    multiple=True,
    help="Render view with columns (NAME:COL1,COL2,...)",
)
@click.option(
    "--columns",
    help="Filter columns for default view (COL1,COL2,...)",
)
@click.option(
    "--exclude",
    help="Exclude columns from output (COL1,COL2,...)",
)
@click.option(
    "--raw",
    is_flag=True,
    help="Bypass all output filtering",
)
```

---

## 4. Formatter Protocol & Behavior

### Resolution Order

```
--raw > --view/--columns/--exclude > OutputConfig > auto-detection
```

1. **--raw**: Skip all filtering, pass data directly to formatter
2. **CLI flags**: Override or extend OutputConfig
3. **OutputConfig**: Plugin-defined defaults from CommandResult
4. **Auto-detection**: Smart defaults when nothing is configured

### CommandResult Extension

```python
@dataclass
class CommandResult:
    data: Any
    metadata: dict = field(default_factory=dict)
    format_hint: str = "json"
    output_config: OutputConfig | None = None  # NEW
```

### Formatter Changes

```python
class OutputFormatter(Protocol):
    def format(
        self,
        data: Any,
        output_config: OutputConfig | None = None,  # NEW
    ) -> str: ...
```

### Multi-View Rendering

When OutputConfig has multiple views:

1. For each view in `output_config.views`:
   - Extract data using `view.path` (JMESPath)
   - Apply column filtering
   - Render table/section
2. Combine with view titles as separators:

```
## Items (48 results)
id    name              brand
---   ----              -----
123   Olive Oil         ...

## Facets
name       count
----       -----
Organic    12
```

---

## 5. Auto-Detection Heuristics

When no OutputConfig is provided and no CLI flags given:

### For list[dict] Data

1. **Identify columns**: All unique keys across items
2. **Score columns** by usefulness:
   - `id`, `name`, `title` → high priority (identifiers)
   - `created_at`, `updated_at` → medium priority (timestamps)
   - `description`, `content`, `body` → low priority (long text)
   - Nested objects/arrays → lowest priority
3. **Select top N columns** (default: 8)
4. **Truncate long values** (default: 50 chars)

### For dict Data

1. Render as key-value pairs (current behavior)
2. Skip nested objects (show "[object]")

### Heuristic Implementation

```python
def auto_detect_columns(data: list[dict], max_cols: int = 8) -> list[str]:
    """Select best columns for display using heuristics.

    Prioritizes columns in this order:
    1. Identity columns: id, name, title (exact matches, id highest)
    2. ID/name-related columns containing "id" or "name"
    3. Date columns: created_at, updated_at, date
    4. Other columns (alphabetically)
    5. Content columns (deprioritized): description, content, body, text

    Args:
        data: List of dictionaries to analyze (samples first 100 items).
        max_cols: Maximum number of columns to return (default: 8).

    Returns:
        List of column names ordered by priority, up to max_cols.
    """
    if not data:
        return []

    all_keys: set[str] = set()
    for item in data[:100]:  # Sample first 100
        if isinstance(item, dict):
            all_keys.update(item.keys())

    # Priority order for exact matches: id > name > title
    priority_order = ["title", "name", "id"]

    def score(key: str) -> tuple[int, int, str]:
        key_lower = key.lower()
        exact_priority = priority_order.index(key_lower) if key_lower in priority_order else -1

        if key_lower in ("id", "name", "title"):
            category = 3
        elif "id" in key_lower or "name" in key_lower:
            category = 2
        elif key_lower in ("created_at", "updated_at", "date"):
            category = 1
        elif key_lower in ("description", "content", "body", "text"):
            category = -1
        else:
            category = 0

        return (category, exact_priority, key)

    sorted_keys = sorted(all_keys, key=score, reverse=True)
    return sorted_keys[:max_cols]
```

---

## 6. Plugin Author Experience

### Declaring Output Config

```python
from graftpunk.plugins import OutputConfig, ViewConfig, ColumnFilter

@command(
    help="Search product catalog",
    params=[...],
)
def search(self, ctx: CommandContext, query: str, items_only: bool = False):
    result = ctx.session.get(...).json()

    if items_only:
        return result["results"]["items"]

    # Return with output configuration
    return CommandResult(
        data=result,
        output_config=OutputConfig(
            views=[
                ViewConfig(
                    name="items",
                    path="results.items",
                    columns=ColumnFilter("include", ["id", "name", "brand", "price"]),
                ),
                ViewConfig(
                    name="pagination",
                    path="results",
                    columns=ColumnFilter("include", ["page", "totalPages", "totalItems"]),
                ),
            ],
            default_view="items",
        ),
    )
```

### Simple Case (Single View)

```python
return CommandResult(
    data=items,
    output_config=OutputConfig(
        views=[
            ViewConfig(
                name="default",
                columns=ColumnFilter("include", ["id", "name", "status"]),
            ),
        ],
    ),
)
```

### No Config (Auto-Detection)

```python
# Just return data - auto-detection kicks in
return items
```

---

## 6b. CLI Override Behavior

CLI flags **surgically modify** the OutputConfig rather than replacing it entirely.

### Merge Rules

| CLI Flag | Behavior |
|----------|----------|
| `--raw` | Bypass OutputConfig entirely |
| `--view name:cols` | Override columns for that view only |
| `--view name` (no cols) | Use view's configured columns |
| `--columns cols` | Override default view's columns |
| `--exclude cols` | Remove from any view's column list |

### Example: Plugin Config + CLI Override

Plugin defines:
```python
OutputConfig(views=[
    ViewConfig(name="items", columns=ColumnFilter("include", ["id", "name", "brand", "price", "stock"])),
    ViewConfig(name="facets", columns=ColumnFilter("include", ["name", "count"])),
])
```

User runs:
```bash
gp unfi item search oil --view items:id,name --exclude stock
```

Result:
- `items` view shows only: `id`, `name` (CLI override)
- `facets` view unchanged (not specified)
- `--exclude stock` has no effect since `stock` isn't in the CLI-specified columns

User runs:
```bash
gp unfi item search oil --exclude price
```

Result:
- `items` view shows: `id`, `name`, `brand`, `stock` (price excluded)
- `facets` view unchanged

---

## 7. Dependencies & File Structure

### New Files

```
src/graftpunk/plugins/
├── output_config.py      # OutputConfig, ViewConfig, ColumnFilter, ColumnDisplayConfig
│                         # + parsing logic, merge logic, auto-detection
├── formatters.py         # Updated to accept OutputConfig
└── cli_plugin.py         # CommandResult gains output_config field
```

### Dependencies

- **jmespath**: For path extraction (`pip install jmespath`)
- No other new dependencies

### Migration Path

1. **Phase 1**: Add OutputConfig support, auto-detection fallback
   - All existing plugins work unchanged
   - New plugins can opt-in to OutputConfig

2. **Phase 2**: Add CLI flags (`--view`, `--columns`, `--exclude`, `--raw`)
   - Users can override any command's output

3. **Phase 3**: Update built-in formatters
   - Table, CSV, JSON formatters honor OutputConfig
   - XLSX if/when added

### Backwards Compatibility

- Commands returning plain data continue to work (auto-detection)
- Commands returning CommandResult without output_config continue to work
- No breaking changes to existing plugin interface

---

## Appendix: Full Example

### Before (Current)

```bash
$ gp unfi item search oil -f table
# 30+ columns, JSON blobs in cells, unusable
```

### After (With OutputConfig)

```bash
$ gp unfi item search oil -f table

## Items (48 results)
id       name                    brand        price
------   ----------------------  -----------  ------
523801   Extra Virgin Olive Oil  California   $12.99
523802   Organic Olive Oil       Spectrum     $14.49
...

## Pagination
page  totalPages  totalItems
----  ----------  ----------
1     4           48
```

### After (With CLI Override)

```bash
$ gp unfi item search oil -f table --view items:id,name --exclude brand

## Items (48 results)
id       name
------   ----------------------
523801   Extra Virgin Olive Oil
523802   Organic Olive Oil
...
```

---

## Implementation Status

### Phase 1: Core OutputConfig (✅ Implemented in PR #78)

- [x] `ColumnFilter`, `ColumnDisplayConfig`, `ViewConfig`, `OutputConfig` dataclasses
- [x] Frozen/immutable with comprehensive `__post_init__` validation
- [x] `default_view` validation (must reference existing view)
- [x] View name uniqueness validation
- [x] `get_view()` and `get_default_view()` helper methods
- [x] `parse_view_arg()`, `auto_detect_columns()`, `apply_column_filter()`, `extract_view_data()`
- [x] JMESPath support with dot-notation fallback
- [x] Logging for debugging path extraction failures
- [x] `CommandResult.output_config` field
- [x] `TableFormatter` and `CsvFormatter` apply OutputConfig
- [x] YAML plugin `output_config` block support
- [x] 55+ tests with comprehensive edge case coverage

### Phase 2: CLI Flags (🔜 Future)

- [ ] `--view NAME:COL1,COL2,...` flag
- [ ] `--columns COL1,COL2,...` flag
- [ ] `--exclude COL1,COL2,...` flag
- [ ] `--raw` flag to bypass all filtering
- [ ] CLI override merge logic

### Phase 3: Auto-Detection Integration (🔜 Future)

- [ ] `auto_detect_columns()` fallback when no OutputConfig provided
- [ ] ColumnDisplayConfig width/alignment applied in formatters
- [ ] Multi-view rendering with section headers
