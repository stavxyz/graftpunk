# Test fixtures

## `browserfree_session.enc` + `browserfree_session.key`

The **A4 guard** for the browser-free session-load path
(`load_session_for_api_from_bytes`), used by
`tests/unit/test_browserfree_session_load.py`.

**Both files are test-only and contain no real credentials.** The payload is a
dummy `BrowserSession` with `User=dummyuser` / `Password=dummypass` cookies. The
`.key` is a throwaway Fernet key generated solely to encrypt that dummy payload —
it decrypts nothing else and must never be reused for anything.

### Why the fixture exists

The pickle inside is a real `graftpunk.session.BrowserSession`, pickled **by
reference** (module + qualname), not by value. That distinction is the whole
point: only a by-reference pickle forces `_BrowserFreeUnpickler.find_class` to
resolve `graftpunk.session.BrowserSession`, and therefore only a by-reference
pickle exercises the stub fallback when the browser stack is absent. A
pickled-by-value payload would carry its own class definition and reconstruct
directly, and the test would pass without ever touching the code it guards.

### Regenerating

```bash
# Requires the browser stack, since it imports graftpunk.session:
pip install 'graftpunk[browser]'   # or: uv sync --group dev
python scripts/gen_browserfree_fixture.py
```

Regenerate when `BrowserSession.__getstate__`/`__setstate__` or its pickled
attribute set changes. See the module docstring in
`scripts/gen_browserfree_fixture.py` for the details it has to preserve.
