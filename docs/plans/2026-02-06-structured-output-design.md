# Structured Output System Design

**Date:** 2026-02-06
**Status:** Draft
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

```python
from dataclasses import dataclass, field
from typing import Literal

@dataclass
class ColumnFilter:
    """Filter columns by include or exclude patterns."""
    mode: Literal["include", "exclude"]
    columns: list[str]

@dataclass
class ColumnDisplayConfig:
    """Display configuration for a specific column."""
    name: str
    header: str = ""  # Display header (defaults to name)
    max_width: int = 0  # 0 = no limit
    align: Literal["left", "right", "center"] = "left"

@dataclass
class ViewConfig:
    """Configuration for a single view/table."""
    name: str
    path: str = ""  # JMESPath to extract data (empty = root)
    title: str = ""  # Display title (optional)
    columns: ColumnFilter | None = None
    display: list[ColumnDisplayConfig] = field(default_factory=list)

@dataclass
class OutputConfig:
    """Complete output configuration for a command."""
    views: list[ViewConfig] = field(default_factory=list)
    default_view: str = ""  # Which view to show when none specified
```

### Key Design Decisions

- **ColumnFilter** uses explicit `mode` to avoid ambiguity between include/exclude
- **ViewConfig.path** uses JMESPath for extracting nested data
- **OutputConfig** supports multiple views but always has a sensible default
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
    """Select best columns for display."""
    if not data:
        return []

    all_keys = set()
    for item in data[:100]:  # Sample first 100
        all_keys.update(item.keys())

    def score(key: str) -> tuple[int, str]:
        """Higher score = show first."""
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
