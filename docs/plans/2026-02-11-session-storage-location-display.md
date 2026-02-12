# Session Storage Location Display — Design

**Goal:** Show where each session is stored in `gp session list` and `gp session show`, with per-session tracking and a `--storage-backend` flag for easy switching.

**Architecture:** Extend `SessionMetadata` with two new fields (`storage_backend`, `storage_location`) persisted in each session's `metadata.json`. Each backend self-reports its identity. Add a `--storage-backend` flag to all `gp session` commands.

---

## Metadata Extension

Add two fields to `SessionMetadata` in `storage/base.py`:

- `storage_backend: str` — backend type: `"local"`, `"s3"`, `"r2"`, `"supabase"`
- `storage_location: str` — display-friendly location: `"~/.config/graftpunk/sessions"`, `"s3://bucket"`, `"r2://bucket"`, `"supabase://bucket"`

Update `metadata_to_dict()` and `dict_to_metadata()`. For backward compatibility, `dict_to_metadata()` defaults both fields to `""` when missing from old `metadata.json` files. No migration needed — fields populate on next save.

---

## Protocol Changes

Add two read-only properties to `SessionStorageBackend`:

```python
@property
def storage_backend(self) -> str: ...   # "local", "s3", "r2", "supabase"

@property
def storage_location(self) -> str: ...  # derived from constructor args
```

Each backend implements these as simple properties:

| Backend | `storage_backend` | `storage_location` |
|---------|-------------------|--------------------|
| `LocalSessionStorage(base_dir=~/.config/graftpunk/sessions)` | `"local"` | `"~/.config/graftpunk/sessions"` (abbreviated) |
| `S3SessionStorage(bucket="foo")` | `"s3"` | `"s3://foo"` |
| `S3SessionStorage(bucket="foo", endpoint_url="...r2.cloudflarestorage.com...")` | `"r2"` | `"r2://foo"` |
| `SupabaseSessionStorage(bucket_name="bar")` | `"supabase"` | `"supabase://bar"` |

R2 identification: `S3SessionStorage` checks if its `endpoint_url` contains `r2.cloudflarestorage.com`. This is not "detection" — the backend examines its own config to determine its display name.

Each backend's `save_session()` injects `storage_backend` and `storage_location` into the metadata dict before writing `metadata.json`.

---

## `--storage-backend` Flag

Add a `@session_app.callback()` with a shared `--storage-backend` option:

```
gp session --storage-backend s3 list
gp session --storage-backend local clear mysite
gp session list                          # uses GRAFTPUNK_STORAGE_BACKEND default
```

The callback stores the override in `ctx.obj`. `_get_session_storage_backend()` in `cache.py` gains an optional `backend_override` parameter. Session commands pass it from `ctx.obj`; other callers (plugin commands, login) continue using the env var.

Valid values: `local`, `s3`, `supabase` (matching `GRAFTPUNK_STORAGE_BACKEND`). Backend credentials (bucket, endpoint, keys) remain env-var-only. The flag is purely a selector.

If `--storage-backend s3` is passed but `GRAFTPUNK_S3_BUCKET` isn't set, the existing `get_storage_config()` ValueError is caught at the CLI layer and printed as a friendly error.

---

## Display Changes

### `gp session list` — Two New Columns

```
                                    Cached Sessions
┏━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┓
┃ Session   ┃ Domain            ┃  Status  ┃ Cookies ┃ Last Modified    ┃ Backend ┃ Location             ┃
┡━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━┩
│ bekentree │ www.bekentree.com │ ● active │      19 │ 2026-02-11 23:34 │ local   │ ~/.config/graftpunk… │
│ acme      │ api.acme.com      │ ● active │       8 │ 2026-02-10 14:12 │ r2      │ r2://gp-sessions     │
│ internal  │ dash.internal.io  │ ○ out    │      12 │ 2026-02-09 09:00 │ s3      │ s3://my-bucket       │
└───────────┴───────────────────┴──────────┴─────────┴──────────────────┴─────────┴──────────────────────┘
```

- **Backend**: dim styling, no-wrap
- **Location**: dim styling, local paths abbreviated with `~`
- Old sessions without fields: show `—` in both columns

### `gp session show` — Two New Lines

```
[dim]Backend:[/dim]    r2
[dim]Location:[/dim]   r2://gp-sessions
```

### `--json` Output

Both fields included naturally since they're in the metadata dict.

---

## Error Handling

1. **Old sessions without storage fields** — default to `""`, display as `—`. Populated on next save.
2. **`--storage-backend` with missing credentials** — catch `ValueError` from `get_storage_config()`, print friendly error, exit 1.
3. **Backend mismatch** — metadata records where session *was* saved; current backend is what's queried. No conflict.
4. **Singleton cache** — `_get_session_storage_backend()` respects `backend_override` without polluting the cached singleton.

---

## Files to Modify

| File | Change |
|------|--------|
| `storage/base.py` | Add fields to `SessionMetadata`, properties to protocol, update serializers |
| `storage/local.py` | Implement properties, inject on save |
| `storage/s3.py` | Implement properties (R2 self-identification), inject on save |
| `storage/supabase.py` | Implement properties, inject on save |
| `cache.py` | Add `backend_override` param, include storage fields in list output |
| `cli/session_commands.py` | Add callback with `--storage-backend`, add columns/fields to display |

## Tests

- Each backend's `storage_backend` / `storage_location` properties
- S3 returns `"r2"` when endpoint contains `r2.cloudflarestorage.com`
- `dict_to_metadata()` backward compat with missing fields
- `--storage-backend` flag overrides global setting
- List table and show panel include new fields
- `--json` output includes storage fields
