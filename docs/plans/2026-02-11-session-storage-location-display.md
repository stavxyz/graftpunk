# Session Storage Location Display — Design

**Issue:** [#97](https://github.com/stavxyz/graftpunk/issues/97)
**Date:** 2026-02-11
**Status:** Draft

**Goal:** Show where each session is stored in `gp session list` and `gp session show`, with per-session tracking and a `--storage-backend` flag for easy switching.

**Architecture:** Extend `SessionMetadata` with two new fields (`storage_backend`, `storage_location`) persisted in each session's `metadata.json`. Each backend self-reports its identity via read-only properties. Add a `--storage-backend` flag to all `gp session` commands.

---

## Metadata Extension

Add two fields to `SessionMetadata` in `src/graftpunk/storage/base.py`:

- `storage_backend: str = ""` — backend type: `"local"`, `"s3"`, `"r2"`, `"supabase"`
- `storage_location: str = ""` — display-friendly location: `"~/.config/graftpunk/sessions"`, `"s3://bucket"`, `"r2://bucket"`, `"supabase://bucket"`

Both fields default to `""` for backward compatibility with existing sessions.

Update the module-level `metadata_to_dict()` and `dict_to_metadata()` in `src/graftpunk/storage/base.py` to include both new fields. For backward compatibility, `dict_to_metadata()` defaults both fields to `""` when missing from old `metadata.json` files. No migration needed — fields populate on next save.

**Serializer divergence:** `LocalSessionStorage` in `src/graftpunk/storage/local.py` has private `_metadata_to_dict()` (line 318) and `_dict_to_metadata()` (line 340) that duplicate the module-level functions in `base.py`. Both sets of serializers must be updated to include the new fields. Consider unifying them during implementation (the private methods could delegate to the base module functions), but this is optional — the minimum requirement is that both serialize the new fields correctly.

---

## Protocol Changes

Add two read-only properties to the `SessionStorageBackend` Protocol in `src/graftpunk/storage/base.py`:

```python
@property
def storage_backend(self) -> str: ...   # "local", "s3", "r2", "supabase"

@property
def storage_location(self) -> str: ...  # derived from constructor args
```

Since `SessionStorageBackend` is a `typing.Protocol`, adding properties does not break existing code at runtime — only static type checkers will flag missing implementations. All three concrete backends must add both properties:

| Backend | `storage_backend` | `storage_location` |
|---------|-------------------|--------------------|
| `LocalSessionStorage` (`src/graftpunk/storage/local.py`) | `"local"` | `"~/.config/graftpunk/sessions"` (abbreviated with `~`) |
| `S3SessionStorage` (`src/graftpunk/storage/s3.py`) | `"s3"` | `"s3://foo"` |
| `S3SessionStorage` with R2 endpoint | `"r2"` | `"r2://foo"` |
| `SupabaseSessionStorage` (`src/graftpunk/storage/supabase.py`) | `"supabase"` | `"supabase://bar"` |

**R2 self-identification:** `S3SessionStorage` checks if its own `endpoint_url` (a constructor argument it already stores as `self.endpoint_url`) contains `r2.cloudflarestorage.com`. This is self-identification — the backend examines its own config to determine its display name.

**Storage location is intentionally bucket-level** (e.g., `s3://my-bucket`, not `s3://my-bucket/sessions/mysite/`). This matches the user-visible configuration granularity and keeps the display concise. Per-session key paths within a bucket are an implementation detail.

### Metadata Injection on Save

Each backend's `save_session()` stamps `storage_backend` and `storage_location` into the metadata before writing `metadata.json`. Since `SessionMetadata` is a frozen dataclass, backends use `dataclasses.replace()` to create a copy with the storage fields set, then serialize via the appropriate `metadata_to_dict()` function.

Example flow in `S3SessionStorage.save_session()`:

```python
stamped = dataclasses.replace(
    metadata,
    storage_backend=self.storage_backend,
    storage_location=self.storage_location,
)
metadata_dict = metadata_to_dict(stamped)
# write metadata_dict to metadata.json
```

Note: `SupabaseSessionStorage` already imports `replace` from `dataclasses` (line 16 of `src/graftpunk/storage/supabase.py`). The other backends will need to add the import.

---

## `--storage-backend` Flag

Add a `@session_app.callback()` to `src/graftpunk/cli/session_commands.py` with a shared `--storage-backend` option:

```
gp session --storage-backend s3 list
gp session --storage-backend local clear mysite
gp session list                          # uses GRAFTPUNK_STORAGE_BACKEND default
```

The callback stores the override in `ctx.obj`. `_get_session_storage_backend()` in `src/graftpunk/cache.py` gains an optional `backend_override` parameter. Session commands pass it from `ctx.obj`; other callers (plugin commands, login) continue using the env var.

**Callback and `no_args_is_help`:** The `session_app` Typer currently has `no_args_is_help=True`. Adding a callback requires testing that bare `gp session` still shows help rather than invoking the callback with an error. Typer callbacks with `invoke_without_command=True` interact with `no_args_is_help` — the callback should check `ctx.invoked_subcommand` and bail early when no subcommand is given.

**`use` and `unset` scope:** These commands operate on the local `.gp-session` context file, not on storage backends. The `--storage-backend` flag has no effect on them. The implementation should either skip passing the override for these commands or document that the flag is ignored.

### Singleton Cache Bypass

`_get_session_storage_backend()` in `src/graftpunk/cache.py` (line 83) currently caches a single backend instance in the module-level `_session_storage_backend` global. When `backend_override` is provided, the function returns a **fresh, uncached instance** for the requested backend type — it does not store it in the global. This ensures the override is scoped to the current command without polluting the singleton used by other callers.

```python
def _get_session_storage_backend(
    backend_override: str | None = None,
) -> "SessionStorageBackend":
    if backend_override is not None:
        # Fresh instance, not cached — scoped to this call
        return _create_backend(backend_override)
    # Existing singleton logic unchanged
    ...
```

Extract the `if/elif/else` chain from the current function into a `_create_backend(backend_type)` helper, used by both the singleton path and the override path.

Valid values: `local`, `s3`, `supabase` (matching `GRAFTPUNK_STORAGE_BACKEND`). Backend credentials (bucket, endpoint, keys) remain env-var-only. The flag is purely a selector.

If `--storage-backend s3` is passed but `GRAFTPUNK_S3_BUCKET` isn't set, the existing `settings.get_storage_config()` `ValueError` is caught at the CLI layer and printed as a friendly error. Note: `get_storage_config()` is a method on `GraftpunkSettings` (line 103 of `src/graftpunk/config.py`), not a standalone function.

---

## Display Changes

### `gp session list` — Two New Columns

```
                                    Cached Sessions
┏━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┓
┃ Session   ┃ Domain            ┃ Status       ┃ Cookies ┃ Last Modified    ┃ Backend ┃ Location             ┃
┡━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━┩
│ bekentree │ www.bekentree.com │ ● active     │      19 │ 2026-02-11 23:34 │ local   │ ~/.config/graftpunk… │
│ acme      │ api.acme.com      │ ● active     │       8 │ 2026-02-10 14:12 │ r2      │ r2://gp-sessions     │
│ internal  │ dash.internal.io  │ ○ logged out │      12 │ 2026-02-09 09:00 │ s3      │ s3://my-bucket       │
└───────────┴───────────────────┴──────────────┴─────────┴──────────────────┴─────────┴──────────────────────┘
```

- **Backend**: dim styling, no-wrap
- **Location**: dim styling, local paths abbreviated with `~`
- Old sessions without fields: show `—` in both columns

**Replacing existing `path` key:** `list_sessions_with_metadata()` in `src/graftpunk/cache.py` (line 518) currently hardcodes `"path": str(settings.sessions_dir / name)` — always a local path regardless of actual backend. This field is replaced by `storage_backend` and `storage_location` from the session metadata. The `"path"` key is removed from the returned dict. Any code referencing `session["path"]` must switch to `session["storage_location"]`.

### `gp session show` — Two New Lines

```
[dim]Backend:[/dim]    r2
[dim]Location:[/dim]   r2://gp-sessions
```

For old sessions without storage fields, display `—` for both.

### `--json` Output

Both fields included naturally since they're in the metadata dict.

---

## Error Handling

1. **Old sessions without storage fields** — default to `""`, display as `—`. Populated on next save.
2. **`--storage-backend` with missing credentials** — catch `ValueError` from `settings.get_storage_config()`, print friendly error, exit 1.
3. **Backend mismatch** — metadata records where session *was* saved; current backend is what's queried. No conflict.
4. **Singleton cache** — `_get_session_storage_backend()` returns a fresh uncached instance when `backend_override` is provided, leaving the singleton untouched.

---

## Files to Modify

| File | Change |
|------|--------|
| `src/graftpunk/storage/base.py` | Add `storage_backend`/`storage_location` fields to `SessionMetadata`, properties to `SessionStorageBackend` Protocol, update `metadata_to_dict()`/`dict_to_metadata()` |
| `src/graftpunk/storage/local.py` | Implement properties, inject on save via `dataclasses.replace()`, update private `_metadata_to_dict()`/`_dict_to_metadata()` |
| `src/graftpunk/storage/s3.py` | Implement properties (R2 self-identification), inject on save via `dataclasses.replace()` |
| `src/graftpunk/storage/supabase.py` | Implement properties, inject on save via `dataclasses.replace()` |
| `src/graftpunk/cache.py` | Add `backend_override` param with singleton bypass, extract `_create_backend()` helper, replace `"path"` key with storage fields |
| `src/graftpunk/cli/session_commands.py` | Add callback with `--storage-backend`, add Backend/Location columns to list table, add fields to show panel |

## Tests

| Test File | Coverage |
|-----------|----------|
| `tests/unit/test_storage_base.py` | `dict_to_metadata()` backward compat with missing `storage_backend`/`storage_location`; `metadata_to_dict()` roundtrip with new fields |
| `tests/unit/test_storage_local.py` | `storage_backend`/`storage_location` properties; private serializer roundtrip with new fields; injection on save |
| `tests/unit/test_storage_s3.py` | Properties; R2 self-identification (`"r2"` when `endpoint_url` contains `r2.cloudflarestorage.com`); injection on save |
| `tests/unit/test_storage_supabase.py` | Properties; injection on save |
| `tests/unit/test_cache.py` | `_get_session_storage_backend(backend_override=...)` returns fresh uncached instance; `list_sessions_with_metadata()` includes storage fields instead of `"path"` |
| `tests/unit/test_session_commands.py` | `--storage-backend` flag overrides global setting; list table and show panel include new columns; `--json` output includes storage fields; callback with `no_args_is_help` still shows help |
