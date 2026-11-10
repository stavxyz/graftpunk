"""Output configuration for structured command responses.

Provides dataclasses for configuring how command output is filtered,
formatted, and displayed across table, CSV, and other formats.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from graftpunk import console as gp_console
from graftpunk.logging import get_logger
from graftpunk.plugins.export import table_rows

LOG = get_logger(__name__)


@dataclass(frozen=True)
class ColumnFilter:
    """Filter columns by exact name matching.

    Args:
        mode: Either "include" (keep only listed columns) or "exclude" (remove listed).
        columns: List of exact column names to include or exclude.
    """

    mode: Literal["include", "exclude"]
    columns: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.mode not in ("include", "exclude"):
            raise ValueError(f"mode must be 'include' or 'exclude', got {self.mode!r}")
        object.__setattr__(self, "columns", tuple(self.columns))


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
        if self.max_width < 0:
            raise ValueError(f"max_width must be >= 0, got {self.max_width}")
        if self.align not in ("left", "right", "center"):
            raise ValueError(f"align must be 'left', 'right', or 'center', got {self.align!r}")


@dataclass(frozen=True)
class ViewConfig:
    """Configuration for a single view/table."""

    name: str
    path: str = ""
    title: str = ""
    columns: ColumnFilter | None = None
    display: tuple[ColumnDisplayConfig, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("name must be non-empty")
        object.__setattr__(self, "display", tuple(self.display))


@dataclass(frozen=True)
class OutputConfig:
    """Complete output configuration for a command."""

    views: tuple[ViewConfig, ...] = ()
    default_view: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "views", tuple(self.views))
        # Validate unique view names
        view_names = [v.name for v in self.views]
        if len(view_names) != len(set(view_names)):
            seen = set()
            duplicates = [n for n in view_names if n in seen or seen.add(n)]
            raise ValueError(f"Duplicate view names: {duplicates}")
        # Validate default_view references existing view
        if self.default_view and self.default_view not in view_names:
            raise ValueError(f"default_view '{self.default_view}' not found in views: {view_names}")

    def get_view(self, name: str) -> ViewConfig | None:
        """Get a view by name, or None if not found."""
        for view in self.views:
            if view.name == name:
                return view
        return None

    def get_default_view(self) -> ViewConfig | None:
        """Get the default view (by default_view name or first view)."""
        if self.default_view:
            return self.get_view(self.default_view)
        return self.views[0] if self.views else None

    def filter_views(
        self,
        names: list[str],
        column_overrides: dict[str, list[str]] | None = None,
    ) -> "OutputConfig":
        """Return a new OutputConfig with only the requested views.

        Args:
            names: View names to include, in the requested order.
            column_overrides: Optional mapping of view name to column list.
                Replaces the view's ColumnFilter with an include filter.

        Returns:
            New OutputConfig with filtered views. Preserves ``default_view``
            if it appears in the filtered set.
        """
        overrides = column_overrides or {}
        views_by_name = {v.name: v for v in self.views}
        filtered: list[ViewConfig] = []
        # A name given twice keeps its first place: two copies of one view
        # would fail the unique-name check (#292).
        for name in dict.fromkeys(names):
            view = views_by_name.get(name)
            if view is None:
                LOG.warning("filter_views_unknown", name=name, available=list(views_by_name))
                continue
            if name in overrides:
                view = replace(view, columns=ColumnFilter("include", tuple(overrides[name])))
            filtered.append(view)
        filtered_names = {v.name for v in filtered}
        default = self.default_view if self.default_view in filtered_names else ""
        return OutputConfig(views=tuple(filtered), default_view=default)


def select_views(config: OutputConfig, view_args: Sequence[str]) -> OutputConfig:
    """*config* narrowed to the views ``--view`` arguments name, in the order given.

    A view named more than once is shown once with the union of the columns
    its arguments ask for, in first-seen order, and one warning names every
    repeated view (#292). An argument with no column list asks for the view's
    own columns: an include filter's columns join the union, and a view with
    no filter or an exclude filter shows every column, so no override applies.
    """
    names: list[str] = []
    repeated: list[str] = []
    merged: dict[str, list[str]] = {}
    explicit: set[str] = set()
    shows_all: set[str] = set()
    for arg in view_args:
        name, cols = parse_view_arg(arg)
        if name not in names:
            names.append(name)
        elif name not in repeated:
            repeated.append(name)
        if cols:
            explicit.add(name)
        else:
            view = config.get_view(name)
            own = view.columns if view is not None else None
            if own is None or own.mode == "exclude":
                shows_all.add(name)
                continue
            cols = list(own.columns)
        columns = merged.setdefault(name, [])
        columns.extend(c for c in cols if c not in columns)
    if repeated:
        gp_console.warn(
            f"--view given more than once for {', '.join(repeated)}: "
            "showing each view once, with the union of the columns asked for."
        )
    overrides = {
        name: cols for name, cols in merged.items() if name in explicit and name not in shows_all
    }
    return config.filter_views(names, overrides)


def parse_view_arg(arg: str) -> tuple[str, list[str]]:
    """Parse view argument string into name and column list.

    Parses 'name:col1,col2' format used in CLI --view arguments.

    Args:
        arg: View argument string (e.g., "items:id,name" or just "items").

    Returns:
        Tuple of (view_name, column_list). Column list is empty if no
        columns specified or if only view name is provided.
    """
    if ":" in arg:
        name, cols_str = arg.split(":", 1)
        cols = [c.strip() for c in cols_str.split(",") if c.strip()] if cols_str.strip() else []
        return name, cols
    return arg, []


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
    for item in data[:100]:
        if isinstance(item, dict):
            all_keys.update(item.keys())

    # Priority order for exact matches (higher index = higher priority)
    priority_order = ["title", "name", "id"]

    def score(key: str) -> tuple[int, int, str]:
        key_lower = key.lower()
        # Exact match priority (id > name > title)
        exact_priority = priority_order.index(key_lower) if key_lower in priority_order else -1

        # Category score
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

        # Return (category, exact_priority, alphabetical) for stable sorting
        return (category, exact_priority, key)

    sorted_keys = sorted(all_keys, key=score, reverse=True)
    return sorted_keys[:max_cols]


def apply_column_filter(
    data: list[dict],
    column_filter: ColumnFilter | None,
) -> list[dict]:
    """Apply column filter to list of dictionaries.

    A row that is not a dict is first the one-column row ``{"value": row}``
    (:func:`graftpunk.plugins.export.table_rows`), so the filter keeps or drops
    it like any other column, where ``row[k]`` raised.
    """
    if column_filter is None:
        return data

    result = []
    for row in table_rows(data):
        if column_filter.mode == "include":
            filtered = {k: row[k] for k in column_filter.columns if k in row}
        else:
            filtered = {k: v for k, v in row.items() if k not in column_filter.columns}
        result.append(filtered)
    return result


def extract_view_data(data: Any, path: str) -> Any:
    """Extract data from nested structure using path expression.

    When jmespath is installed, supports full JMESPath query syntax including
    array indexing and projections (e.g., "results[0].items", "data[?active]").
    Falls back to simple dot-notation path traversal if jmespath is unavailable.

    Args:
        data: The nested data structure to extract from.
        path: JMESPath expression or dot-notation path (e.g., "results.items").

    Returns:
        The extracted data, or None if path traversal fails.
    """
    if not path:
        return data

    try:
        import jmespath
        import jmespath.exceptions

        try:
            result = jmespath.search(path, data)
            if result is None:
                LOG.debug(
                    "view_data_extraction_empty",
                    path=path,
                    data_type=type(data).__name__,
                )
            return result
        except jmespath.exceptions.JMESPathError as exc:
            LOG.warning("jmespath_expression_invalid", path=path, error=str(exc))
            return None
    except ImportError:
        LOG.debug("jmespath_not_installed_using_fallback", path=path)
        result = data
        for key in path.split("."):
            if isinstance(result, dict) and key in result:
                result = result[key]
            else:
                LOG.debug(
                    "view_data_extraction_failed",
                    path=path,
                    failed_at_key=key,
                    available_keys=list(result.keys()) if isinstance(result, dict) else None,
                )
                return None
        return result
