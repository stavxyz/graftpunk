"""Session storage backends for BSC.

This package provides pluggable storage backends for session persistence:
- LocalSessionStorage: Local filesystem storage (default)
- SupabaseSessionStorage: Supabase Storage + database (cloud)

Storage backends are discovered via entry points, allowing custom backends
to be installed as separate packages.
"""

from bsc.storage.base import (
    SessionMetadata,
    SessionStorageBackend,
    parse_datetime_iso,
)
from bsc.storage.local import LocalSessionStorage

__all__ = [
    "SessionMetadata",
    "SessionStorageBackend",
    "parse_datetime_iso",
    "LocalSessionStorage",
]


# Lazy imports for optional backends
def __getattr__(name: str) -> type:
    if name == "SupabaseSessionStorage":
        from bsc.storage.supabase import SupabaseSessionStorage

        return SupabaseSessionStorage
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
