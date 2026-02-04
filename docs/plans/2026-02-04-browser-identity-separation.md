# Plan: Fix GraftpunkSession Profile Fallback (Issue #49)

> **Issue**: [#49 — GraftpunkSession profile fallback allows python-requests User-Agent to leak to the wire](https://github.com/stavxyz/graftpunk/issues/49)
>
> **Related**: [#50 — Higher-level session API](https://github.com/stavxyz/graftpunk/issues/50) (design decisions here set the foundation)

## Design Summary

Separate browser identity headers from request-type headers. Browser identity is set once at session init and can never leak. Request-type headers come from captured profiles when available, or canonical defaults when not.

### Two Independent Axes

Every browser request has headers from two axes:

1. **Browser identity** — Who is making the request? Invariant across request types.
   - `User-Agent`, `sec-ch-ua`, `sec-ch-ua-mobile`, `sec-ch-ua-platform`

2. **Request type** — What kind of request? Varies by navigation/xhr/form.
   - `Accept`, `Sec-Fetch-Mode`, `Sec-Fetch-Site`, `Sec-Fetch-Dest`, `Sec-Fetch-User`, `X-Requested-With`, `Upgrade-Insecure-Requests`, `Content-Type` (forms)

Today both are tangled in each profile dict. This fix separates them.

### Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Where to apply browser identity | Session-level defaults at `__init__` | Impossible to leak; simplest guarantee |
| How to identify identity headers | Explicit allowlist (`_BROWSER_IDENTITY_HEADERS`) | Easy to reason about and extend |
| Fallback when profile missing | Canonical request-type header sets | Most correct requests; aligns with #50 |
| Scope | Build foundation for #50 now | Canonical header sets serve both #49 fallback and #50's `.xhr()`/`.navigate()` methods |
| `_case_insensitive_get` location | Module-level private function | Pure function, no instance state needed |

---

## Tasks

### Task 1: Add constants to `graftpunk_session.py`

**File**: `src/graftpunk/graftpunk_session.py`

Add after the imports, before the class definition:

```python
_BROWSER_IDENTITY_HEADERS: frozenset[str] = frozenset({
    "User-Agent",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
})

_CANONICAL_REQUEST_HEADERS: dict[str, dict[str, str]] = {
    "navigation": {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    },
    "xhr": {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Requested-With": "XMLHttpRequest",
    },
    "form": {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Content-Type": "application/x-www-form-urlencoded",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    },
}
```

Add the case-insensitive lookup helper:

```python
def _case_insensitive_get(mapping: dict[str, str], key: str) -> str | None:
    """Get a value from a dict with case-insensitive key matching."""
    for k, v in mapping.items():
        if k.lower() == key.lower():
            return v
    return None
```

**Commit**: `feat: add browser identity and canonical request-type header constants`

---

### Task 2: Add `_apply_browser_identity()` and update `__init__`

**File**: `src/graftpunk/graftpunk_session.py`

Update `__init__` to call `_apply_browser_identity()` **before** the snapshot:

```python
def __init__(self, header_profiles=None, **kwargs):
    super().__init__(**kwargs)
    self._gp_header_profiles = header_profiles or {}
    self.gp_default_profile = None

    # Apply browser identity headers from any captured profile.
    # This guarantees User-Agent/sec-ch-ua are session defaults
    # before any request, regardless of profile detection outcome.
    self._apply_browser_identity()

    # Snapshot AFTER identity applied — identity headers are treated
    # as defaults (not user-set) by _is_user_set_header().
    self._gp_default_session_headers = dict(self.headers)
```

Add the new method:

```python
def _apply_browser_identity(self) -> None:
    """Set browser identity headers from any available profile.

    Extracts headers from _BROWSER_IDENTITY_HEADERS that are shared
    across all profiles (same browser = same identity). Sets them as
    session-level defaults so they apply to every request.
    """
    for profile in self._gp_header_profiles.values():
        for key in _BROWSER_IDENTITY_HEADERS:
            matched = _case_insensitive_get(profile, key)
            if matched is not None and key not in self.headers:
                self.headers[key] = matched
        # All profiles share the same browser identity.
        # Stop once we have a User-Agent.
        if "User-Agent" in self.headers:
            break
```

**Commit**: `feat: extract browser identity headers at session init`

---

### Task 3: Fix `prepare_request` fallback

**File**: `src/graftpunk/graftpunk_session.py`

Replace the broken fallback (lines 123-125):

```python
# BEFORE (broken — falls back to same missing profile):
if not profile_headers:
    profile_headers = self._gp_header_profiles.get("navigation", {})

# AFTER (uses canonical request-type headers):
if not profile_headers:
    LOG.debug(
        "profile_not_captured_using_canonical",
        detected=profile_name,
        available=list(self._gp_header_profiles.keys()),
    )
    profile_headers = dict(_CANONICAL_REQUEST_HEADERS.get(profile_name, {}))
```

**Commit**: `fix: fall back to canonical request-type headers when profile missing`

---

### Task 4: Write tests

**File**: `tests/unit/test_graftpunk_session.py`

Update `SAMPLE_PROFILES` to include `sec-ch-ua` headers for identity testing.

New test classes:

**`TestBrowserIdentityGuarantee`**:
- `test_browser_ua_set_at_init` — session.headers["User-Agent"] is browser UA, not python-requests
- `test_empty_profiles_keeps_requests_default` — no profiles → no crash, requests default UA kept
- `test_single_profile_identity_extracted` — only xhr profile → identity still extracted
- `test_case_insensitive_extraction` — profile with lowercase `user-agent` → extracted correctly
- `test_sec_ch_ua_headers_extracted` — sec-ch-ua, sec-ch-ua-mobile, sec-ch-ua-platform all set
- `test_identity_treated_as_default_not_user_set` — `_is_user_set_header("User-Agent")` returns False after init
- `test_user_override_after_init_detected` — setting session.headers["User-Agent"] after init is detected as user-set

**`TestCanonicalFallback`**:
- `test_missing_navigation_uses_canonical` — only xhr+form profiles, GET request → canonical navigation headers applied
- `test_missing_xhr_uses_canonical` — only navigation+form profiles, json POST → canonical xhr headers applied
- `test_canonical_navigation_has_correct_headers` — verify Accept, Sec-Fetch-Mode, etc.
- `test_canonical_xhr_has_correct_headers` — verify Accept, X-Requested-With, etc.
- `test_captured_profile_preferred_over_canonical` — when profile exists, canonical not used
- `test_browser_ua_present_in_canonical_fallback` — even with canonical fallback, browser UA from session defaults is in prepared request

Update existing `TestFallbackBehavior.test_missing_form_profile_falls_back_to_navigation` — this test's assertion changes (now falls back to canonical form headers, not navigation profile).

**`TestCaseInsensitiveGet`**:
- `test_exact_match` — returns value for exact key
- `test_lowercase_match` — returns value for lowercase key
- `test_missing_key` — returns None

**Commit**: `test: add browser identity and canonical fallback tests`

---

### Task 5: Update documentation

**Files**:
- `CHANGELOG.md` — Add fixed entry under Unreleased
- `docs/HOW_IT_WORKS.md` — Update GraftpunkApp/Logging section to describe identity separation
- `README.md` — No changes needed (this is internal session behavior)

**Commit**: `docs: document browser identity separation and canonical fallback`

---

## What Does NOT Change

- `_detect_profile()` — heuristics are correct, untouched
- `extract_header_profiles()` in `observe/headers.py` — capture stays as-is
- `EXCLUDED_HEADERS` — orthogonal concern
- Header priority in `prepare_request` merge logic — caller > user-set > profile > defaults
- Session serialization — `_gp_header_profiles` pickled the same way
- `headers_for()` public API — returns raw captured profile unchanged

## How This Sets Up #50

After this lands, the codebase has:
- `_BROWSER_IDENTITY_HEADERS` — the identity header set
- `_CANONICAL_REQUEST_HEADERS` — canonical headers for each request type
- Browser identity guaranteed on every request via session defaults

Issue #50's `.xhr()`, `.navigate()`, `.form_submit()` methods become thin wrappers that compose canonical + captured headers, add Referer, and delegate to `self.request()`. No rework needed.
