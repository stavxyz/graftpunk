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
   session stored as a record; for one stored as a pickle it stays as it is
   today (the write-back is skipped and logged), because a pickle cannot be
   written back without the browser classes. A session becomes a record at its
   next login on a host that sets `record`.
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

Two new leaf modules, both browser-free:

- `graftpunk/session_riders.py` holds session state that rides on a session:
  the rider registry (`SESSION_RIDER_ATTRS`, `SESSION_RIDER_DEFAULTS`, the
  memory-only set and `PICKLED_SESSION_RIDER_ATTRS`), the rider attribute name
  constants defined in `graftpunk.tokens` today (`_CACHE_ATTR`,
  `_CSRF_TOKENS_ATTR`, `_CSRF_ORIGIN_ATTR`, `_HEADER_ROLES_ATTR` and
  `_HEADER_ROLE_ORIGINS_ATTR`), and the `CachedToken` class, which the
  cached-token codec has to construct. They move out of `graftpunk.tokens`,
  which imports the browser-launch helpers the format must not depend on;
  `tokens` re-exports every name. The registry also names
  `GP_SESSION_NAME_ATTR` and `GP_ACCOUNT_ATTR`, which stay where they are, in
  `graftpunk.session_identity` (it imports only `slugify` and
  `graftpunk.exceptions`); `session_riders` imports those two from it and
  nothing else from graftpunk. `session_identity` is not changed. graftpunk's
  own modules (`session.py`, `cache.py`, `graftpunk_session.py` and the rest)
  import the moved names from `session_riders`. The `tokens` re-export then
  has one reason to exist: programs outside graftpunk that import from it, and
  the pickled path of `CachedToken` below. A comment on it says it is part of
  the stored format, and the class-path test is its guard.
- `graftpunk/session_record.py` holds the codec, described below. It imports
  from graftpunk only `graftpunk.session_riders`, `graftpunk.cookies`,
  `graftpunk.exceptions` and `graftpunk.logging`.

**Class paths inside a pickle are part of the stored format.** A pickle names
each class by module and qualified name, so moving a class changes what
release 1's default (`pickle`) writes, and an older release that lacks the new
path fails to unpickle it: its `load_session` raises `ImportError` with a
misleading browser-install hint, and the session is unreadable there. That is
the anti-goal, reached through the path this design calls unchanged. Therefore the qualified names of
every class a legacy pickle can contain are frozen: `graftpunk.session.
BrowserSession`, `graftpunk.graftpunk_session.GraftpunkSession`,
`graftpunk.tokens.CachedToken`, and the cookie policy, which graftpunk
pickles as a by-name call of `http.cookiejar.DefaultCookiePolicy`
(`BrowserCookiePolicy.__reduce__` in `cookies.py`), never as a graftpunk
class. `CachedToken` is defined in `session_riders` with `__module__` set to
`"graftpunk.tokens"`, where the re-export makes it resolvable, so the pickled
path does not change. A test pickles a session holding cached tokens with the
legacy writer and asserts the bytes name exactly the frozen paths and no
`session_riders` path; the old-reader case in "Proving nothing breaks" item 3
uses a session with cached tokens for the same reason.

A unit test checks both leaf modules two ways. First, it parses each module's
own import statements and asserts that the graftpunk modules it names are on
its allowlist above (an allowlist over `sys.modules` cannot work, because
importing any submodule first runs `graftpunk/__init__.py`, which loads
`graftpunk.cache`, `graftpunk.client`, `graftpunk.tokens` and more). Second, it
imports each in a fresh interpreter and asserts that no `selenium`,
`requestium`, `nodriver` or `httpie` module is loaded.

`session_record` is the codec only: what the stored bytes mean. It never
imports a session class, constructs a library object to compare with, or
changes a live session. The rollout policy (which format to write, which
object to load for a write-back, how a live session is renamed) lives in
`cache.py`, the orchestration layer, in the functions described under
"Writing". The codec holds:

- **`classify(decrypted)`**: the one entry point for stored bytes. It returns
  either a record (parsed, validated and version-checked) or "legacy pickle"
  with the raw bytes. The first-byte test, the JSON parse and the refusal of a
  newer `graftpunk_session` version live here and nowhere else; every reader
  (`load_session`, the API loaders, `load_for_write_back`) calls it first and
  only chooses what to build from the result.

- **The field declaration.** One table. Each row names a state key as
  `__getstate__` writes it (`_backend_type`, `_use_stealth`, `session_name`,
  `current_url`, `headers`, `cookies`, `params`, `verify`, `proxies`, `cert`,
  `max_redirects`, `trust_env`, `stream`, `webdriver_path`, `default_timeout`,
  `_last_requests_url`, and each name in `PICKLED_SESSION_RIDER_ATTRS`), the
  record key it is stored under, and its codec (plain
  JSON value, header pairs, cookie list, cached-token map). A second set of
  rows marks the pickle-only values (`auth`, `hooks`, `adapters`,
  `webdriver_options`, `_driver_initializer`, `_driver`, and the cookie jar's
  policy, which rides inside `cookies`), each with a
  structural predicate on the value that says it is at its default (see "What
  the record cannot hold"). Each row also names the kinds it applies to, so
  the per-kind key set is read from the table and stated nowhere else.
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
  reads an attribute through a property. (A selenium `BrowserSession` that
  never had `_session_name` set raises `BrowserError` from `.session_name`
  when it has no driver; a nodriver one keeps the name as `_session_name`; the
  state dict has `session_name` in every case.) Classes inside the state that
  cannot be imported are stubbed as today. It absorbs `_BrowserFreeUnpickler`
  from `cache.py`, so one placeholder unpickler remains. This code owns the
  historical key names and is otherwise frozen.

- **The kind map**, in both directions, keyed by fully qualified class name
  (`graftpunk.session.BrowserSession`, `graftpunk.graftpunk_session.
  GraftpunkSession`, `requests.sessions.Session`) and compared against
  `type(session).__module__` and `__qualname__`, so classifying a session
  imports nothing. The class itself is imported only by `load_session` for a
  record of kind `browser`.
- **`StoredSession`**, a small typed view of a state dict, and the protocol
  it satisfies, `StoredSessionLike`: `SessionLike` (`cookies`, `headers`) plus
  what `_extract_session_metadata` and the two write-backs read or set:
  `session_name`, `current_url`, and each rider attribute. Its
  `__getstate__()` emits the keys the table lists for its kind; it carries no
  per-kind rule of its own. It also holds the record's unknown top-level keys,
  untouched, and is the only thing that does (see the extension rule under
  "The record"). A `StoredSession` is only ever built from a record or, for
  the API loaders, from a legacy pickle's state; it is only ever written as a
  record. A test builds a view
  and the real object from the same record and asserts
  `_extract_session_metadata` returns the same for both and the two
  `__getstate__()` results have the same keys. `record_to_state` and
  `state_from_pickle` results are wrapped in the view; the raw dict crosses a
  module boundary only at `__setstate__`.

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
  jar rebuilt from it equals the original cookie for cookie. The jar's policy
  is not stored: it is given on load, as today, by the kind (`browser` and
  `api` get graftpunk's browser cookie policy, which every loader already
  applies after unpickling; `requests` gets the standard library's). A pickle
  never names a graftpunk policy class either: HEAD pickles the policy as the
  standard library's, and 1.21.0 pickled HTTPie's. The jar's policy has its
  own pickle-only row in the table, so this rule is read from the same place
  as the others: a policy that is a program's own subclass, or carries
  non-default settings (the arguments `BrowserCookiePolicy.__reduce__`
  passes), keeps the session a pickle.
- `headers` is a list of pairs, so order and case survive.
- `riders` holds the state keys in `PICKLED_SESSION_RIDER_ATTRS`; the
  memory-only riders stay out, as they are out of the pickle today. An `api`
  or `requests` session's state has none (requests' `__attrs__` carries no
  riders), so its record has none. That is today's behaviour kept on purpose:
  a saved `GraftpunkSession` loses its header roles and token cache in the
  pickle too, and `update_session_cookies` restores only the account, from
  the metadata.
- **The extension rule, one rule for every later change.** A reader refuses
  a `graftpunk_session` version higher than it supports, with an error that
  names the version and says to upgrade graftpunk; it never reports such a
  session as expired, and a write-back never writes over it. A release may add
  a top-level key without raising the version only if all three hold for an
  older release that does not know the key: it can ignore the key when
  reading; it can keep the key unchanged while rewriting cookies and tokens
  beside it; and losing the key on a fresh save is harmless. Anything else
  raises the version. Unknown keys are kept in exactly one place, the
  write-back path: a write-back reads a record into a `StoredSession`, which
  holds them, and writes them back unchanged. A fresh save (a login, or a
  program that calls `load_session` and then `cache_session`) writes a new
  record with only the keys the writer knows; live session objects carry no
  storage-format state. A test writes back a record holding an extra top-level
  key through `update_session_cookies` and `copy_session` and asserts the key
  survives both; another asserts a version-2 record is refused by all three
  readers and left byte for byte unchanged by both write-backs.
- Format detection is by the first decrypted byte: `{` is a record, anything
  else is a legacy pickle (pickle protocol 2 and later starts with `0x80`).
  `classify` owns it.
- `SessionMetadata.checksum` stays the SHA-256 of the decrypted payload.

### What the record cannot hold

`auth`, `hooks` and `adapters` (requests' `__attrs__`, so present for all
three kinds), and for selenium `webdriver_options`, `_driver_initializer` and
`_driver`, can be arbitrary objects or callables. They are the pickle-only
rows of the table. Each row's "at its default" test is a structural predicate
on the value itself. The codec constructs nothing to compare with: two fresh
`requests.Session()` objects' `adapters` do not compare equal (`HTTPAdapter`
has no `__eq__`), a `functools.partial` never equals another, and building a
selenium `BrowserSession` launches Chrome. The predicates:

- `auth`: `None`. (A `(user, password)` pair of strings is not pickle-only: it
  has a stored row.)
- `hooks`: a mapping whose only key is `response` and whose value is empty.
- `adapters`: exactly the mount prefixes `https://` and `http://`, each an
  instance of exactly `requests.adapters.HTTPAdapter` (by type name, not
  `isinstance`) whose `max_retries` is the zero-retry default and whose pool
  sizes are requests' `DEFAULT_POOLSIZE`, the one library constant the codec
  reads, from `requests.adapters`, a base dependency.
- `_driver`: `None`.
- `webdriver_options`: an empty mapping.
- `_driver_initializer`: a `functools.partial` whose `func.__name__` is
  `_start_chrome_browser` and whose keywords are only `headless`.

A value that fails its predicate, or that cannot be inspected (a placeholder
for a class that could not be imported, or an attribute read that raises),
keeps the session in the legacy pickle format, with one warning naming the
key. The legacy writer stays `dill` (`cache.py` imports it as `pickle`), since
hooks and initializers can be lambdas. Such a session works exactly as today,
at the price of keeping the legacy writer.

A test pins each predicate against the installed libraries, so a requests or
requestium release that changes a default fails CI instead of silently turning
every session unrecordable: the state of a fresh `requests.Session()` and a
fresh `GraftpunkSession` passes every predicate; where requestium is
installed, a fresh `requestium.Session()` (which starts no browser until its
driver is first used) has an empty `webdriver_options` and a
`_driver_initializer` that passes the predicate, and
`requestium.Session._start_chrome_browser` exists under that name; and each
committed selenium fixture passes too. Whether a selenium pickle's `_driver_initializer` (a partial of
a bound method of the session being unpickled) survives the state-capturing
unpickle on graftpunk-lite is UNVERIFIED; if it does not, selenium pickles are
unrecordable on lite and recordable on a full install, which the rule above
already handles. The compat test prints the unrecordable count per fixture.
(Builder's call: if `webdriver_options` proves to be plain arguments in every
case found, give it a stored row instead.)

## Reading

Every reader calls `classify(decrypted)` first. From its result, decrypted
bytes become a state dict in one place: `record_to_state` for a record,
`state_from_pickle` for a legacy pickle.

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

The format of a fresh save comes from one setting, a `GraftpunkSettings`
field, `session_format` (`GRAFTPUNK_SESSION_FORMAT`, values `pickle` and
`record`). A write-back never consults it: it writes the format it read. Two
functions in `cache.py` are the policy layer:

- **`encode_session(session, keep=None)`**: the only writer, and the only
  reader of the setting. Session in, payload bytes out.
  - `keep=None` is a fresh save (`cache_session` from a login or a program).
    The setting decides: under `record` it calls the codec's `state_to_record`
    and falls back to the legacy pickle with its one warning; under `pickle`
    it writes the legacy pickle.
  - `keep="record"` or `keep="pickle"` is a write-back, which passes the
    format `classify` reported for the bytes it read. A `StoredSession` is
    always written as a record and a real object read from a pickle is always
    written as a pickle; `encode_session` raises `TypeError` for any other
    pairing, so the rule does not rest on two functions agreeing.

  `cache_session` and `copy_session` both call it for the payload (today
  `copy_session` repeats `cache_session`'s pickle, checksum and encrypt lines;
  that duplication is removed, not grown).
- **`load_for_write_back(decrypted)`**: the only place that decides which
  object stored bytes become before they are changed and re-saved. It reads no
  setting and makes no recordability check; it decides from the bytes alone.
  It calls `classify` first, so a record of a newer version is refused before
  any change is made. A record becomes a `StoredSession`. A legacy pickle
  becomes the real object, unpickled whole, as today; when the real classes
  are absent it raises `RealSessionClassUnavailable`, a subclass of
  `ImportError` carrying the reason. It returns the session together with the
  format it read, which the caller hands to `encode_session` as `keep`.

So a store moves from pickles to records only through fresh saves on hosts
that set `record`; sessions are re-saved at every login, so that is one login
cycle per session. No write-back ever converts a session, in either
direction: a host on the default setting that refreshes a cookie in a record
writes a record, with its unknown keys intact, and a host set to `record` that
refreshes a cookie in a pickle writes a pickle.

Release 2's default flip is one edit, to the setting's default, and any later
change to the legacy writer is confined to `encode_session`; the codec does
not change for either.

- **Release 1 (1.45.0): reads both, writes `pickle` by default.** Nothing an
  older reader sees changes. Setting `record` opts a writer in: that host's
  fresh saves become records. This release is
  the floor: every environment that shares a store must run it or later before
  any of them writes records.
- **Release 2 (a later minor, not before the owner says the known consumers
  are on release 1 or later): the default becomes `record`.** `pickle` stays
  selectable. The Upgrading note leads with the one sentence that matters:
  a graftpunk older than 1.45.0 reading the same store will report these
  sessions as expired.
- The legacy reader and writer stay. Subclasses, other types, and sessions
  with pickle-only state have no other home, so removing the legacy writer is
  not planned here; it needs its own decision once the compat fixtures show
  how many real sessions are unrecordable and what they would become.

### Write-backs

`update_session_cookies` and `copy_session` (which `session_migration` uses to
rename a slot) each load a stored session, change it, and save it. Neither
reads the format setting or asks whether the bytes are recordable. Each does
three things:

1. `session, fmt = load_for_write_back(decrypted)`.
2. Its one mutation, written against `StoredSessionLike`, as today.
   `update_session_cookies` updates the cookies, the token cache and the
   account. `copy_session` sets the account and the slot name; it sets the
   name through the public `session_name` attribute, which `BrowserSession`
   (its property setter) and `StoredSession` both have, in place of today's
   `hasattr(session, "_session_name")` probe. A kind whose state holds no name
   does not gain one: only the table's keys for that kind are written.
3. Save through `encode_session(session, keep=fmt)`
   (`update_session_cookies` by way of `cache_session`, which also reads the
   metadata from the session).

Which path a write-back takes, for reference:

- The stored bytes are a record: a `StoredSession`, written back as a record.
  No real class is needed, so this works on graftpunk-lite.
- The stored bytes are a legacy pickle: the real object, unpickled whole, and
  written back as a pickle. This is today's code path unchanged, and it needs
  the real classes.

The only difference between the two callers is what they do with
`RealSessionClassUnavailable`, and that difference is their existing
contracts:

- `update_session_cookies` is best-effort. It catches the error and skips the
  write-back with a warning that gives the reason and says the next login or a
  write-back from a full install rewrites the session.
- `copy_session` promises a verified copy or an exception. It lets the error
  propagate (an `ImportError`, as on lite today). `session_migration` catches
  it, keeps the source slot and marks it superseded, and
  `finish_superseded_slot` deletes that slot only after the login has written
  the new one, so a failed copy never loses the session before its replacement
  exists.

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
   default (`pickle`), for a session holding cached tokens and header roles:
   the session loads with its tokens. A second case writes with `record`
   and asserts the documented outcome (the old release reports it expired and
   does not delete it), so that sentence in the Upgrading note is a tested
   fact.
4. **Lite.** `scripts/smoke_install.sh` gains: on a lite venv, a YAML plugin
   command with a stored session (both formats) returns its response against a
   local fake server; a command that refreshes a cookie in a session stored
   as a record writes it back as a record, with no setting, and the next run
   reads it; the same refresh on a session stored as a pickle is skipped with
   the warning; and `copy_session` on a stored pickle raises and leaves the
   source.
4a. **Write-backs keep the format.** On a full install, for each setting
   value: a write-back of a stored record yields a record and a write-back of
   a stored pickle yields a pickle, through both `update_session_cookies` and
   `copy_session`; and `encode_session` raises `TypeError` for a
   `StoredSession` with `keep="pickle"`.
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
- A selenium `BrowserSession` restored from a pickle can be pickled again
  today, by reading the code: requests' `__setstate__` sets each state key
  with `setattr`, so `session_name` goes through the property setter and
  restores `_session_name`; and `self.driver` is `BrowserSession`'s override,
  which raises `BrowserError` instead of starting a driver, an error
  `__getstate__` catches. The compat fixtures confirm it by running it; no
  `__setstate__` change is planned.
- UNVERIFIED: how many independent consumers share a store across versions.
  The staged rollout assumes at least one does.
