---
type: spec
---

# Sessions stored as plain data: design

Date: 2026-11-24. Status: draft, awaiting the owner's approval. Tracks #245.

## Intent and why

graftpunk saves a logged-in session by pickling the live `BrowserSession`
object (`src/graftpunk/cache.py`, `cache_session`: `pickle.dumps(session)`).
`BrowserSession` subclasses `requestium.Session`
(`src/graftpunk/session.py`), so reading a saved session back means importing
that class, and with it requestium and selenium, even when the reader only
wants cookies and headers and starts no browser.

What follows from that today:

- A browser-free install (graftpunk-lite) cannot run a plugin command that
  uses its saved session (#245).
- A second loader, `load_session_for_api_from_bytes`, works around it with
  `_BrowserFreeUnpickler`, which replaces every class it cannot import with an
  empty placeholder. Its own docstring says it is not a security boundary.
- Loading a session executes whatever the pickle says to construct. Only the
  Fernet key keeps a forged blob out.

The owner's reading, which this design adopts: needing browser packages to
read cookies is a composition defect, and the fix belongs in the stored format.
The owner's condition: nothing that works today may break.

## Goals

1. A saved session is a versioned plain-data record. Reading one imports no
   browser package and constructs no class named by the stored bytes.
2. Every session saved by any earlier release still loads, with the same
   cookies, headers, header roles, cached tokens and account identity.
3. A plugin command that uses its saved session runs on graftpunk-lite without
   `[browser]`. Saving refreshed cookies and tokens back from lite works for a
   store that writes records; under release 1's default (`pickle`) it stays as
   it is today (the write-back fails and is logged), because a pickle an older
   release can read cannot be written without the browser classes.
4. No release reads a store written by a newer release and fails silently or
   destroys the session.

## Non-goals

- How a plugin is installed beside graftpunk-lite (plugins declare
  `graftpunk[browser]`). That is the other half of #245 and gets its own short
  design after this ships; it does not depend on any choice made here.
- Logging in without a browser.
- Changing the encryption (Fernet), the key sources, the storage backends'
  interfaces, session naming, or `SessionMetadata`.
- Making `GraftpunkSession` thread-safe.

## Anti-goals

- A flag-day: a release after which older sessions need a new login.
- A format an older graftpunk reads as "expired" and clears. On a shared
  bucket that would log out every host.
- Dropping session state the record cannot hold without saying so.

## Constraints

- Mixed versions share stores. A consumer pins graftpunk below a cap and
  upgrades on its own schedule, and its environments may read the same bucket
  (whether they do is UNVERIFIED). An older release sees only "bytes that
  decrypt and then fail to unpickle", which `load_session` reports as
  `SessionExpiredError`.
- `load_session()` is public and returns the cached object (a `BrowserSession`
  for a browser login); `gp session export` and `gp observe --session` call it,
  and `update_session_cookies` and `copy_session` use the whole object through
  `_load_session_with_metadata`. That contract stays: for a browser session it
  needs the browser packages, as it does now.
- The `_warn_login_page_unreadable` contract and everything else in the login
  engine are untouched.
- Public repo: placeholders only in tests and docs.

## Approaches considered

1. **A plain-data record (chosen by the owner, 2026-11-24).** Versioned JSON
   holding cookies, headers, riders and backend settings, encrypted as today.
   Removes the browser import, the placeholder unpickler for new sessions, and
   code execution on load. Costs a format migration and a staged rollout.
2. **Make the placeholder unpickler the default loader everywhere.** Small, but
   keeps the pickle, the placeholders and code-on-load, and
   `update_session_cookies` would re-save placeholder objects.
3. **Restructure `BrowserSession` so its module imports no browser package
   until used.** The class inherits from `requestium.Session`, so this is a
   rewrite of that class, and the stored bytes stay tied to it.

## One declaration of what a session stores

Every class graftpunk saves already reduces itself to a plain state dict:
`BrowserSession.__getstate__` (nodriver and selenium branches), and requests'
own `__getstate__` for `GraftpunkSession` and a plain `requests.Session`
(its `__attrs__` list). `__setstate__` on each class restores from that dict.
The pickle is that dict plus the class name. The record is that dict as JSON.
Nothing else in this design lists session fields by hand.

A new module, `graftpunk/session_record.py`, owns the format. It is a leaf:
a unit test imports it in a fresh interpreter and asserts that the only
`graftpunk` modules loaded are on a short allowlist (`graftpunk.session_record`,
`graftpunk.session_identity`, `graftpunk.cookies`, `graftpunk.exceptions`,
`graftpunk.logging`, and what those import today) and that no `selenium`,
`requestium`, `nodriver` or `httpie` module is loaded. The rider registry
(`SESSION_RIDER_ATTRS`, `SESSION_RIDER_DEFAULTS`, the memory-only set and
`PICKLED_SESSION_RIDER_ATTRS`) moves from `graftpunk.tokens`, which imports
the browser-launch helpers, to `graftpunk.session_identity`, which already
holds `GP_ACCOUNT_ATTR` and `GP_SESSION_NAME_ATTR`; `tokens` re-exports the
names so existing imports keep working. `cache.py` stays the storage and
orchestration layer and calls into the new module, which holds:

- **The field declaration.** One table. Each row names a state key as
  `__getstate__` writes it (`_backend_type`, `_use_stealth`, `session_name`,
  `current_url`, `headers`, `cookies`, `params`, `verify`, `proxies`, `cert`,
  `max_redirects`, `trust_env`, `stream`, `webdriver_path`, `default_timeout`,
  `_last_requests_url`, and each name in `PICKLED_SESSION_RIDER_ATTRS`), the
  record key it is stored under, and its codec (plain
  JSON value, header pairs, cookie list, cached-token map). A second set of
  rows marks the pickle-only keys (`auth`, `hooks`, `adapters`,
  `webdriver_options`, `_driver_initializer`, `_driver`) with the test that
  says the value is at its default.
- **`state_to_record(kind, state)`**: the only builder. It walks the state
  dict against the table. A key the table stores is encoded. A pickle-only key
  at its default is dropped. A pickle-only key away from its default, or any
  key the table does not know, makes the session unrecordable: the function
  returns the reason and the caller writes a legacy pickle with one warning
  naming the key. Unknown state therefore fails toward keeping it.
- **`record_to_state(record)`**: the inverse, producing the state dict
  `__setstate__` already accepts.
- **The legacy codec**, `state_from_pickle(decrypted)`: reads the state dict
  out of a pickle any release wrote. It unpickles with the three session
  classes always replaced by a state-capturing placeholder (the existing
  `_Stub`, whose `__setstate__` stores the dict), whether or not the real
  classes are importable, so it sees one shape on every install and never
  reads an attribute through a property. (A real selenium `BrowserSession`
  raises `BrowserError` from `.session_name` when it has no driver; a real
  nodriver one keeps the name as `_session_name`; the state dict has
  `session_name` in every case.) Classes inside the state that cannot be
  imported are stubbed as today. This code owns the historical key names and
  is otherwise frozen.

- **The kind map**, in both directions, keyed by fully qualified class name
  (`graftpunk.session.BrowserSession`, `graftpunk.graftpunk_session.
  GraftpunkSession`, `requests.sessions.Session`) and compared against
  `type(session).__module__` and `__qualname__`, so classifying a session
  imports nothing. The class itself is imported only by `load_session` for a
  record of kind `browser`.
- **`StoredSession`**, a small typed view of a state dict: `kind`, `cookies`
  (a jar), `headers`, the riders, and the backend settings, with
  `__getstate__()` returning the state dict. It exposes the attributes the
  write-backs touch on a real session (`cookies`, each rider attribute, and
  `_session_name`) under the same names. `record_to_state` and
  `state_from_pickle` results are wrapped in it; the raw dict crosses a module
  boundary only at `__setstate__`.
- **`encode_session(session)`**: the only writer. Session (a real object or a
  `StoredSession`) in, payload bytes out. The format setting, the kind lookup,
  `state_to_record`, the fall back to the legacy pickle and its one warning
  all live inside it. Release 2's default flip and the 2.0 removal of the
  legacy writer are each one edit, here.

A test builds a fully populated live session of each kind, calls its
`__getstate__()`, and fails if any key is neither stored nor pickle-only in
the table. A field added to `BrowserSession.__getstate__` later cannot reach a
release without a row.

## The record

The decrypted payload is UTF-8 JSON, one object. Key names are illustrative;
the table fixes them:

```json
{
  "graftpunk_session": 1,
  "kind": "browser",
  "backend_type": "nodriver",
  "use_stealth": true,
  "session_name": "myshop",
  "current_url": "https://myshop.example/account",
  "headers": [["User-Agent", "..."], ["Accept", "*/*"]],
  "cookie_policy": "browser",
  "cookies": [
    {"version": 0, "name": "sid", "value": "...", "port": null,
     "port_specified": false, "domain": ".myshop.example",
     "domain_specified": true, "domain_initial_dot": true, "path": "/",
     "path_specified": true, "secure": true, "expires": 1790000000,
     "discard": false, "comment": null, "comment_url": null,
     "rest": {"HttpOnly": null}, "rfc2109": false}
  ],
  "riders": {
    "cached_tokens": {"csrf": {"name": "csrf", "value": "...",
                                "extracted_at": 1763900000.0, "ttl": 3600.0}},
    "header_roles": {"xhr": {"X-Requested-With": "XMLHttpRequest"}},
    "header_role_origins": {"xhr": "https://myshop.example"},
    "account": "alice"
  },
  "requests": {"params": {}, "verify": true, "proxies": {}, "cert": null,
               "max_redirects": 30, "trust_env": true, "stream": false},
  "selenium": {"webdriver_path": null, "default_timeout": 5,
               "last_requests_url": null}
}
```

- `kind` says which class was saved, because `cache_session` is public and
  takes more than a `BrowserSession`: `browser` (`BrowserSession`), `api`
  (`GraftpunkSession`) and `requests` (a plain `requests.Session`, which
  docs/HOW_IT_WORKS.md's plain-HTTP login saves). The match is on the exact
  type through the kind map above, never `isinstance`: a subclass of
  any of the three, and an object of any other type, is saved as a legacy
  pickle with a warning naming its type, so it loads back as the class it was.
- `cookies` carries every `http.cookiejar.Cookie` constructor argument, so a
  jar rebuilt from it equals the original cookie for cookie. `cookie_policy`
  is `browser` (graftpunk's `BrowserCookiePolicy`) or `default` (the standard
  library's policy with default settings); a jar with any other policy makes
  the session unrecordable, by the rule above.
- `headers` is a list of pairs, so order and case survive.
- `riders` holds the state keys in `PICKLED_SESSION_RIDER_ATTRS`; the
  memory-only riders stay out, as they are out of the pickle today. An `api`
  or `requests` session's state has none (requests' `__attrs__` carries no
  riders), so its record has none. That is today's behaviour kept on purpose:
  a saved `GraftpunkSession` loses its header roles and token cache in the
  pickle too, and `update_session_cookies` restores only the account, from
  the metadata.
- A reader ignores keys it does not know and refuses a `graftpunk_session`
  version higher than it supports, with an error that names the version and
  says to upgrade graftpunk. It never reports such a session as expired.
- Format detection is by the first decrypted byte: `{` is a record, anything
  else is a legacy pickle (pickle protocol 2 and later starts with `0x80`).
- `SessionMetadata.checksum` stays the SHA-256 of the decrypted payload.

### What the record cannot hold

`auth`, `hooks` and `adapters` (requests' `__attrs__`, so present for all
three kinds), and for selenium `webdriver_options` and `_driver_initializer`,
can be arbitrary objects or callables. They are the pickle-only rows of the
table: `auth` other than `None` or a `(user, password)` pair, `hooks` other
than requests' defaults, `adapters` other than the two default `HTTPAdapter`s
with default settings, and `webdriver_options` or `_driver_initializer` away
from their defaults each keep the session in the legacy pickle format, with
one warning naming the key. The legacy writer stays `dill` (`cache.py` imports
it as `pickle`), since hooks and initializers can be lambdas. Such a session
works exactly as today, at the price of keeping the legacy writer until a
major release. Which selenium sessions hit this in practice is UNVERIFIED; the
builder measures it against the compat fixtures and reports the count.
(Builder's call: if `webdriver_options` proves to be plain arguments in every
case found, give it a stored row instead.)

## Reading

Decrypted bytes become a state dict in one place: `record_to_state` for a
record, `state_from_pickle` for a legacy pickle. Both loaders start there.

- **API loaders.** `load_session_for_api`, `load_session_for_api_resolved` and
  `load_session_for_api_from_bytes` build a `GraftpunkSession` from a
  `StoredSession` through one builder: the existing `_api_session_from_session`
  (`cache.py`), which already depends only on the `SessionLike` protocol the
  view satisfies. It stays the only owner of the
  header filtering (requests' defaults, ephemeral and client-set headers,
  `replayable_roles`) and of applying the browser cookie policy. None of the
  three imports `graftpunk.session`, for either format, so they work on
  graftpunk-lite. `load_session_for_api` stops calling `load_session`.
- **`load_session`** returns the object that was saved. A legacy pickle is
  unpickled whole with the real classes, exactly as today, so no state a
  pickle holds is ever lost on load. A record is restored through the class's
  own `__setstate__`: `obj = cls.__new__(cls); obj.__setstate__(
  record_to_state(record))`, with `cls` chosen by `kind`. There is no second
  copy of the restore logic, and no header filtering here: `load_session`
  hands back what was saved, as it does today. Kind `browser` needs the
  browser packages and raises the install hint on lite, as today; kinds `api`
  and `requests` need none, so a plain-HTTP login's session keeps loading on
  lite.
- `gp session show`, `gp session list` read metadata only and do not change.

## Writing, and the staged rollout

The format comes from one setting, `GRAFTPUNK_SESSION_FORMAT`, values
`pickle` and `record`, read in one place: `encode_session`. `cache_session`
and `copy_session` both call it for the payload (today `copy_session` repeats
`cache_session`'s pickle, checksum and encrypt lines; that duplication is
removed, not grown).

- **Release 1 (1.45.0): reads both, writes `pickle` by default.** Nothing an
  older reader sees changes. Setting `record` opts a store in. This release is
  the floor: every environment that shares a store must run it or later before
  any of them writes records.
- **Release 2 (a later minor, not before the owner says the known consumers
  are on release 1 or later): the default becomes `record`.** `pickle` stays
  selectable. The Upgrading note leads with the one sentence that matters:
  a graftpunk older than 1.45.0 reading the same store will report these
  sessions as expired.
- The legacy writer and reader are removed no earlier than 2.0.

### Write-backs

`update_session_cookies` and `copy_session` (which `session_migration` uses to
rename a slot) each load a stored session, change it, and save it. Each keeps
exactly one mutation, written against an object, as today: cookies, the rider
attributes, `_session_name`, and the account. What changes is only which
object the loaded bytes become:

- A record, or a legacy pickle `state_to_record` accepts: a `StoredSession`,
  on every install. The mutation runs on it, `encode_session` writes it, and
  `cache_session` reads its metadata from it as from any session. This works
  on graftpunk-lite.
- A legacy pickle `state_to_record` refuses (a pickle-only key away from its
  default, or a class inside the state that could not be imported): the real
  object, unpickled whole, as today, and written back as a pickle. This needs
  the real classes.

Under release 1's default (`pickle`) a `StoredSession` cannot be written as a
pickle an older release can read, so both write-backs take the second path for
every session, which is today's code path unchanged.

Where the real classes are needed and absent (graftpunk-lite), the two paths
keep their own contracts:

- `update_session_cookies` is best-effort by contract. It skips the
  write-back with a warning that names the reason and says the next login or a
  write-back from a full install rewrites the session.
- `copy_session` promises a verified copy or an exception, and
  `session_migration` deletes the source on success. It raises, as it raises
  `ImportError` on lite today, and the source slot is kept.

## graftpunk-lite (#245, session half)

With the loaders above, a plugin command that uses its session works on
graftpunk-lite for both formats. The install hint on a session load comes
today from two handlers that convert a missing browser module: plugin commands
(`src/graftpunk/cli/plugin_runtime.py`) and `gp http` with a cached session
(`src/graftpunk/cli/http_commands.py`). Neither fires any longer for a record
or a legacy pickle the API loaders can read. The hint stays for `login`,
`gp observe` and `gp session export`. Both README passages that describe the
lite contract (the lite paragraph and the `load_session_for_api` note) and
`scripts/smoke_install.sh` (the lite contract check the release workflow
runs) are updated: on lite, an API command or `gp http` with a stored session
runs; `login` prints the hint.

## Proving nothing breaks

This is the part the owner's condition rests on, so it is specified, not left
to the builder.

1. **Real old pickles.** A committed script,
   `scripts/session_compat.py generate --graftpunk VERSION --out DIR`, installs
   that released version from PyPI into a scratch venv and has it cache
   synthetic sessions (placeholder hosts, a test key): a nodriver session, a
   selenium session, a plain `requests.Session` and a `GraftpunkSession`, one
   with cached tokens and header roles, one with an account label, one with a
   custom adapter mounted and one that is a subclass of `GraftpunkSession`
   (both must stay pickles and load back as what they were). No listed release
   writes the legacy flat-file layout, so the generator writes that fixture
   itself: the encrypted pickle of a released version's session placed at
   `<name>.session.pickle` with no `metadata.json`, the layout
   `LocalSessionStorage._load_legacy_session` reads. The nodriver, requests
   and api sessions need no browser. The selenium session is generated on a
   developer machine, once per version, by one headless Chrome launch with
   `--use-mock-keychain --password-store=basic` (constructing the object
   without a browser would not produce the `webdriver_options` and
   `_driver_initializer` a real login saves, which are the values in
   question); CI only reads the committed fixtures. If a release cannot be
   launched that way, its selenium fixture is omitted and the fact is recorded
   in the fixtures' README. Fixtures generated from 1.21.0 (the HTTPie
   cookie-policy era), 1.40.1, 1.43.1 and 1.44.0 are committed under
   `tests/fixtures/session_compat/`. A test loads every one through every
   loader and compares cookies (every attribute), headers, roles, tokens and
   account against a JSON expectation written by the generator. The script
   absorbs `scripts/gen_browserfree_fixture.py`: that script's fixture
   (`tests/fixtures/browserfree_session.enc`) becomes one of the compat
   fixtures, and its entries in `tests/fixtures/README.md` and CONTRIBUTING.md
   point at the new script.
2. **Round trip.** For each recordable fixture: state dict from the pickle,
   `state_to_record`, `record_to_state`; the two state dicts are equal. Then
   load, save as a record, load again through `load_session` and through the
   API loaders, and compare as in item 1. Same through
   `update_session_cookies` and `copy_session`.
3. **Old reader, new writer.** `scripts/session_compat.py read --graftpunk
   VERSION --store DIR` runs a released version against a store written by the
   branch. A CI job runs it with 1.44.0 against a store written with the
   default (`pickle`): the session loads. A second case writes with `record`
   and asserts the documented outcome (the old release reports it expired and
   does not delete it), so that sentence in the Upgrading note is a tested
   fact.
4. **Lite.** `scripts/smoke_install.sh` gains: on a lite venv, a YAML plugin
   command with a stored session (both formats) returns its response against a
   local fake server; with `GRAFTPUNK_SESSION_FORMAT=record`, a command that
   refreshes a cookie writes it back and the next run reads it; and
   `copy_session` on a pickle it cannot record raises and leaves the source.
5. **No code on load.** A record whose JSON carries a string that would be a
   pickle opcode sequence, and a pickle crafted to call a marker function, are
   each fed to the record reader: the first is data, the second is never
   unpickled when the first byte is `{`.
6. **The declaration holds.** The two tests named above: every key a live
   session's `__getstate__()` emits has a row, and importing
   `graftpunk.session_record` loads no browser module.

`scripts/session_compat.py` is documented in CONTRIBUTING.md (what it checks,
when to run it: before any change to the stored format or to
`BrowserSession.__getstate__`).

## Left to the builder

- Names of private helpers and test file layout.
- The exact JSON key for each row and the validation error wording.
- Whether `webdriver_options` gets a stored row (see above).

## Open risks

- UNVERIFIED: that every attribute a selenium `BrowserSession` needs after
  `load_session` is covered by the record plus the pickle fallback rule. The
  compat fixtures are what settle it; if a selenium session cannot round-trip,
  selenium sessions stay on pickle in release 1 and the design is revisited
  before release 2.
- UNVERIFIED: whether a selenium `BrowserSession` restored from a pickle can
  be pickled again today. `__getstate__` reads `self.session_name`, which
  reaches the driver when `_session_name` is absent, and a restored session
  has no driver. If it cannot, today's selenium write-back already fails and
  is only logged; the compat fixtures show it, and the fix (restore
  `_session_name` in `__setstate__`) ships with release 1. The same
  `__getstate__` reads `self.driver.current_url`; whether that tries to start
  a driver on a restored session is part of the same check, since
  `encode_session` and the completeness test call `__getstate__()` directly.
- UNVERIFIED: how many independent consumers share a store across versions.
  The staged rollout assumes at least one does.
