# Session Storage Location Display — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Show where each session is stored in `gp session list` and `gp session show`, with per-session tracking and a `--storage-backend` flag.

**Architecture:** Extend `SessionMetadata` with `storage_backend`/`storage_location` fields. Each backend self-reports identity via properties. Backends stamp fields on save via `dataclasses.replace()`. Add `--storage-backend` CLI flag via Typer callback. Replace hardcoded `"path"` key with storage fields.

**Tech Stack:** Python, Typer, Rich, pytest, dataclasses

**Design Doc:** `docs/plans/2026-02-11-session-storage-location-display.md`

---

### Task 1: Extend SessionMetadata and base serializers

**Files:**
- Modify: `src/graftpunk/storage/base.py:45-124`
- Test: `tests/unit/test_storage_base.py`

**Step 1: Write failing tests** — Add `TestStorageFields` class to test file:
- `test_metadata_defaults_storage_fields_to_empty` — construct `SessionMetadata` without new fields, assert both `== ""`
- `test_metadata_accepts_storage_fields` — construct with `storage_backend="s3"`, `storage_location="s3://bucket"`
- `test_metadata_to_dict_includes_storage_fields` — assert dict has both keys
- `test_dict_to_metadata_missing_storage_fields_defaults` — pass dict without new keys, assert defaults to `""`
- `test_metadata_to_dict_roundtrip_with_storage_fields` — roundtrip preserves values

**Step 2: Run** `uv run pytest tests/unit/test_storage_base.py::TestStorageFields -v` — expect FAIL

**Step 3: Implement** in `src/graftpunk/storage/base.py`:
1. Add fields to `SessionMetadata` after `status`: `storage_backend: str = ""` and `storage_location: str = ""`
2. Update docstring Attributes section
3. Add to `metadata_to_dict()` return dict: `"storage_backend"`, `"storage_location"`
4. Add to `dict_to_metadata()` constructor: `storage_backend=data.get("storage_backend", "")`, same for location

**Step 4: Run** `uv run pytest tests/unit/test_storage_base.py -v` — expect PASS

**Step 5: Commit** `feat: add storage_backend/storage_location to SessionMetadata`

---

### Task 2: Add Protocol properties and update LocalSessionStorage

**Files:**
- Modify: `src/graftpunk/storage/base.py:127-228` (Protocol)
- Modify: `src/graftpunk/storage/local.py`
- Test: `tests/unit/test_storage_local.py`

**Step 1: Write failing tests** — Add `TestLocalStorageIdentity` class (add `from pathlib import Path` to imports):
- `test_storage_backend_is_local` — assert `== "local"`
- `test_storage_location_uses_tilde` — create with `Path.home() / ".config/graftpunk/sessions"`, assert starts with `~`
- `test_storage_location_non_home_path` — create with `tmp_path`, assert `== str(tmp_path)`
- `test_save_stamps_storage_fields_in_metadata` — save, read `metadata.json`, assert fields present
- `test_private_serializers_include_storage_fields` — call `_metadata_to_dict` on stamped metadata
- `test_private_deserializer_defaults_missing_fields` — call `_dict_to_metadata` on old dict

**Step 2: Run** `uv run pytest tests/unit/test_storage_local.py::TestLocalStorageIdentity -v` — expect FAIL

**Step 3: Implement:**

In `src/graftpunk/storage/base.py`, add to `SessionStorageBackend` Protocol (after `update_session_metadata`):
```python
    @property
    def storage_backend(self) -> str:
        """Backend type identifier (e.g., "local", "s3", "r2", "supabase")."""
        ...

    @property
    def storage_location(self) -> str:
        """Display-friendly storage location (e.g., "s3://bucket", "~/.config/...")."""
        ...
```

In `src/graftpunk/storage/local.py`:
1. Add `from dataclasses import replace` to imports
2. Add properties after `__init__`:
```python
    @property
    def storage_backend(self) -> str:
        return "local"

    @property
    def storage_location(self) -> str:
        try:
            return str("~" / self.base_dir.relative_to(Path.home()))
        except ValueError:
            return str(self.base_dir)
```
3. In `save_session()`, before `self._metadata_to_dict(metadata)`: `metadata = replace(metadata, storage_backend=self.storage_backend, storage_location=self.storage_location)`
4. Add `storage_backend`/`storage_location` to `_metadata_to_dict()` and `_dict_to_metadata()`

**Step 4: Run** `uv run pytest tests/unit/test_storage_local.py tests/unit/test_storage_base.py -v` — expect PASS

**Step 5: Commit** `feat: add storage identity properties to Protocol and LocalSessionStorage`

---

### Task 3: S3SessionStorage identity (with R2 self-identification)

**Files:**
- Modify: `src/graftpunk/storage/s3.py`
- Test: `tests/unit/test_storage_s3.py`

**Step 1: Write failing tests** — Add `TestS3StorageIdentity`:
- `test_s3_backend_without_r2_endpoint` — no endpoint_url → `"s3"`, `"s3://my-bucket"`
- `test_r2_backend_detected_from_endpoint` — endpoint with `r2.cloudflarestorage.com` → `"r2"`, `"r2://gp-sessions"`
- `test_non_r2_custom_endpoint` — MinIO endpoint → `"s3"`
- `test_save_stamps_storage_fields` — save, inspect `put_object` call for metadata JSON, assert fields

**Step 2: Run** `uv run pytest tests/unit/test_storage_s3.py::TestS3StorageIdentity -v` — expect FAIL

**Step 3: Implement** in `src/graftpunk/storage/s3.py`:
1. Add `from dataclasses import replace` to imports
2. Add properties:
```python
    @property
    def storage_backend(self) -> str:
        if self.endpoint_url and "r2.cloudflarestorage.com" in self.endpoint_url:
            return "r2"
        return "s3"

    @property
    def storage_location(self) -> str:
        return f"{self.storage_backend}://{self.bucket}"
```
3. In `save_session()`, before `metadata_json = json.dumps(metadata_to_dict(metadata), ...)`:
```python
        stamped = replace(metadata, storage_backend=self.storage_backend, storage_location=self.storage_location)
        metadata_json = json.dumps(metadata_to_dict(stamped), indent=2)
```

**Step 4: Run** `uv run pytest tests/unit/test_storage_s3.py -v` — expect PASS

**Step 5: Commit** `feat: add storage identity to S3SessionStorage with R2 self-identification`

---

### Task 4: SupabaseSessionStorage identity

**Files:**
- Modify: `src/graftpunk/storage/supabase.py`
- Test: `tests/unit/test_storage_supabase.py`

**Step 1: Write failing tests** — Add `TestSupabaseStorageIdentity`:
- `test_storage_backend_is_supabase` — assert `== "supabase"`
- `test_storage_location_uses_bucket_name` — assert `== "supabase://sessions"`
- `test_custom_bucket_name_in_location` — custom bucket → `"supabase://custom-bucket"`
- `test_save_stamps_storage_fields` — save, inspect upload call body JSON

**Step 2: Run** `uv run pytest tests/unit/test_storage_supabase.py::TestSupabaseStorageIdentity -v` — expect FAIL

**Step 3: Implement** in `src/graftpunk/storage/supabase.py` (`replace` already imported):
1. Add properties after `__init__`:
```python
    @property
    def storage_backend(self) -> str:
        return "supabase"

    @property
    def storage_location(self) -> str:
        return f"supabase://{self.bucket_name}"
```
2. In `_do_save()`, before metadata serialization: stamp with `replace(metadata, ...)`

**Step 4: Run** `uv run pytest tests/unit/test_storage_supabase.py -v` — expect PASS

**Step 5: Commit** `feat: add storage identity to SupabaseSessionStorage`

---

### Task 5: Update cache.py — backend_override, storage fields

**Files:**
- Modify: `src/graftpunk/cache.py:83-125, 179-208, 488-522`
- Test: `tests/unit/test_cache.py`

**Step 1: Write failing tests** — Add `TestBackendOverride` and `TestListSessionsStorageFields`:
- `test_override_returns_fresh_instance` — mock settings, call with/without override, assert different objects
- `test_override_does_not_pollute_singleton` — override doesn't change subsequent default calls
- `test_list_includes_storage_fields` — save via LocalSessionStorage, assert `storage_backend`/`storage_location` in results
- `test_list_no_path_key` — assert `"path"` not in results

Add `from pathlib import Path` and `from graftpunk.storage.base import SessionMetadata` to imports.

**Step 2: Run** — expect FAIL

**Step 3: Implement** in `src/graftpunk/cache.py`:
1. Extract `_create_backend(backend_type)` helper from `_get_session_storage_backend()`. Pass `backend_type` to `settings.get_storage_config(backend_type=...)`.
2. Add `backend_override: str | None = None` param to `_get_session_storage_backend()`. If set, return `_create_backend(backend_override)` without caching.
3. Update `get_session_metadata()` dict to include `storage_backend`/`storage_location`.
4. Update `list_sessions_with_metadata()`: replace `"path"` key with `storage_backend`/`storage_location`. Remove unused `settings` variable. Update docstring.
5. Add `backend_override` param to public functions: `list_sessions_with_metadata()`, `get_session_metadata()`, `clear_session_cache()`. Pass through to `_get_session_storage_backend()`.

**Step 4: Run** `uv run pytest tests/unit/test_cache.py -v` — expect PASS

**Step 5: Commit** `feat: add backend_override to cache, include storage fields in metadata dicts`

---

### Task 6: Add Backend/Location columns to session list and show

**Files:**
- Modify: `src/graftpunk/cli/session_commands.py`
- Create: `tests/unit/test_session_commands.py`

**Step 1: Write failing tests** in new `tests/unit/test_session_commands.py`:

`TestSessionListDisplay` (mock `list_sessions_with_metadata`):
- `test_list_shows_backend_column` — assert "Backend" and backend value in output
- `test_list_shows_location_column` — assert "Location" and URI in output
- `test_list_missing_storage_fields_shows_dash` — empty fields → em-dash in output
- `test_list_json_includes_storage_fields` — `--json` output has both keys

`TestSessionShowDisplay` (mock `get_session_metadata` and `resolve_session_name`):
- `test_show_includes_backend_and_location` — assert values in panel output
- `test_show_empty_storage_fields_shows_dash` — empty → dash
- `test_show_json_includes_storage_fields` — `--json` has both keys

**Step 2: Run** — expect FAIL

**Step 3: Implement** in `src/graftpunk/cli/session_commands.py`:
1. Add two table columns after "Last Modified": `Backend` (dim, no_wrap) and `Location` (dim)
2. Add values to `table.add_row()`: `session.get("storage_backend") or "[dim]—[/dim]"`, same for location
3. In `show()`, add backend/location lines to info string after Expires

**Step 4: Run** `uv run pytest tests/unit/test_session_commands.py -v` — expect PASS

**Step 5: Commit** `feat: add Backend/Location columns to session list and show`

---

### Task 7: Add --storage-backend flag via Typer callback

**Files:**
- Modify: `src/graftpunk/cli/session_commands.py`
- Modify: `tests/unit/test_session_commands.py`

**Step 1: Write failing tests** — Add `TestStorageBackendFlag`:
- `test_bare_session_shows_help` — `invoke(session_app, [])` → exit 0, shows usage
- `test_flag_passes_override` — `["--storage-backend", "s3", "list"]` accepted without error
- `test_flag_with_missing_credentials_shows_error` — mock raises ValueError, assert friendly error

**Step 2: Run** — expect FAIL

**Step 3: Implement:**
1. Add `@session_app.callback(invoke_without_command=True)` with `--storage-backend` option. Store in `ctx.obj["storage_backend"]`. Handle no-subcommand case (print help, exit 0).
2. Remove `no_args_is_help=True` from `session_app` Typer constructor (callback handles it).
3. Add `ctx: typer.Context` to `session_list`, `show`, `session_clear`, `export`. Extract `backend_override` from `ctx.obj`. Wrap calls in `try/except ValueError`.
4. `session_use` and `session_unset` don't need the override — they operate on local `.gp-session` file.

**Step 4: Run** `uv run pytest tests/unit/test_session_commands.py -v` — expect PASS

**Step 5: Commit** `feat: add --storage-backend flag to session commands`

---

### Task 8: Full test suite, quality checks, CHANGELOG

**Step 1: Run** `uv run pytest tests/ -v` — expect ALL PASS

**Step 2: Run** `uvx ruff check . && uvx ruff format --check . && uvx ty check src/` — expect clean

**Step 3: Fix any issues found**

**Step 4: Update CHANGELOG** — Under `## [Unreleased]` → `### Added`:
```markdown
- **Session Storage Location Display**: `gp session list` and `gp session show` display where each session is stored (#97)
  - Two new columns: Backend (`local`, `s3`, `r2`, `supabase`) and Location (`~/.config/...`, `s3://bucket`, etc.)
  - Per-session tracking via `storage_backend`/`storage_location` in `metadata.json`
  - `--storage-backend` flag on all `gp session` commands for querying specific backends
  - S3 backend self-identifies as `r2` when endpoint is Cloudflare R2
  - Backward compatible: old sessions display `—` until next save
```

**Step 5: Commit** `docs: add session storage location display to CHANGELOG`

**Step 6: Run** `uv run pytest tests/ -v` — final verification, expect ALL PASS
