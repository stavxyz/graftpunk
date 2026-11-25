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
- `load_session()` is public and returns a `BrowserSession`; `gp session
  export`, `gp observe --session` and `update_session_cookies` use the whole
  object. That contract stays: it needs the browser packages, as it does now.
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

## The record

The decrypted payload is UTF-8 JSON, one object:

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
  docs/HOW_IT_WORKS.md's plain-HTTP login saves). `backend_type`,
  `use_stealth` and the `selenium` block are present only for `browser`. An
  object of any other type is saved in the legacy pickle format with a warning
  naming its type, as it is saved today.
- `cookies` carries every `http.cookiejar.Cookie` constructor argument, so a
  jar rebuilt from it equals the original cookie for cookie.
- `headers` is a list of pairs, so order and case survive.
- `riders` holds exactly the attributes in `PICKLED_SESSION_RIDER_ATTRS`
  (`graftpunk.tokens`); the memory-only riders stay out, as they are out of the
  pickle today. The key names above are illustrative; the builder maps each
  attribute to one stable key and tests the mapping against that tuple, so a
  rider added later cannot be forgotten.
- A reader ignores keys it does not know and refuses a `graftpunk_session`
  version higher than it supports, with an error that names the version and
  says to upgrade graftpunk. It never reports such a session as expired.
- Format detection is by the first decrypted byte: `{` is a record, anything
  else is a legacy pickle (pickle protocol 2 and later starts with `0x80`).
- `SessionMetadata.checksum` stays the SHA-256 of the decrypted payload.

### What the record cannot hold

The selenium and plain-requests save paths also pickle `auth`, `hooks` and
`adapters` (requests' `__attrs__`), and the selenium path `webdriver_options`
and `_driver_initializer`. These can be arbitrary objects or callables. Rule:
a session whose `auth` is not `None` or a `(user, password)` pair, whose
`hooks` differ from requests' defaults, whose mounted `adapters` are not the
two default `HTTPAdapter`s with default settings, or whose `webdriver_options`
or `_driver_initializer` are not their defaults, is saved in the legacy pickle
format, with one warning naming the attribute. The legacy writer stays `dill`
(`cache.py` imports it as `pickle`), since hooks and initializers can be
lambdas. That keeps such a session
working exactly as today, at the price of keeping the legacy writer until a
major release. Which selenium sessions hit this in practice is UNVERIFIED; the
builder measures it against the repo's selenium tests and example plugins and
reports the count. (Builder's call: if `webdriver_options` proves to be plain
arguments in every case found, hold it in the record instead.)

## Reading

One function in `cache.py` turns decrypted bytes into a `SessionRecord`
(a frozen dataclass of plain values):

- From a record: parse and validate the JSON.
- From a legacy pickle: `_BrowserFreeUnpickler` as today, then lift the
  object's plain state into the same `SessionRecord`. On graftpunk-lite the
  object is a placeholder holding the pickled state; on a full install the
  unpickler imports the real class and the object is a real `BrowserSession`
  or `requests.Session`, so the lift reads the same fields from either. This is
  the only remaining use of the placeholder unpickler.

Every loader goes through it:

- `load_session_for_api`, `load_session_for_api_resolved` and
  `load_session_for_api_from_bytes` build a `GraftpunkSession` from the
  `SessionRecord`. For a record none of them imports `graftpunk.session`; for
  a legacy pickle they import it only where it is installed. Either way they
  work on graftpunk-lite. `load_session_for_api` stops calling `load_session`.
- `load_session` returns the class the session was saved as. A legacy pickle
  is always unpickled whole with the real classes, exactly as today, so no
  state a pickle holds is ever lost on load. A record of kind `browser`
  becomes a `BrowserSession` (a classmethod that sets the same state
  `__setstate__` sets; needs the browser packages and raises the install hint
  on lite, as today); kind `api` becomes a `GraftpunkSession` and kind
  `requests` a `requests.Session`, neither of which needs a browser package,
  so a plain-HTTP login's session keeps loading on lite as it does today.
- `gp session show`, `gp session list` read metadata only and do not change.

## Writing, and the staged rollout

`cache_session` takes its format from one setting,
`GRAFTPUNK_SESSION_FORMAT`, values `pickle` and `record`.

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

`update_session_cookies` and the copy and rename paths write a session back in
the format the configured setting names; under the default of release 1 that is
`pickle`, so a store is never flipped by a write-back. Under `record`:

- A session stored as a record is loaded through `SessionRecord`, updated and
  written as a record. This works on graftpunk-lite.
- A session stored as a legacy pickle is, on a full install, unpickled whole,
  updated, and saved through `cache_session`, where the rule in "What the
  record cannot hold" decides record or pickle. On graftpunk-lite the real
  classes are absent, so whether the pickle holds state a record would drop
  cannot be known: the write-back is skipped with a warning that says the
  session is in the old format and is rewritten by the next login or by a
  write-back from a full install. That is today's outcome on lite, stated
  instead of logged as a failure.

## graftpunk-lite (#245, session half)

With the loaders above, a plugin command that uses its session works on
graftpunk-lite for both formats. The existing lite guard that prints the
install hint for "a plugin command that uses its session" is removed for the
load path and kept for `login` and the other browser-driving commands. The
README's lite paragraph and `scripts/smoke_install.sh` (the lite contract
check the release workflow runs) are updated to the new contract: on lite, an
API command with a stored session runs; `login` prints the hint.

## Proving nothing breaks

This is the part the owner's condition rests on, so it is specified, not left
to the builder.

1. **Real old pickles.** A committed script,
   `scripts/session_compat.py generate --graftpunk VERSION --out DIR`, installs
   that released version from PyPI into a scratch venv and has it cache
   synthetic sessions (placeholder hosts, a test key): a nodriver session, a
   selenium session, a plain `requests.Session` and a `GraftpunkSession`, one
   with cached tokens and header roles, one with an account label, one with a
   custom adapter mounted (must stay a pickle), and one legacy flat-file
   session with no checksum. The nodriver, requests and api sessions need no
   browser. The selenium session is generated on a developer machine, once per
   version, by one headless Chrome launch with `--use-mock-keychain
   --password-store=basic` (constructing the object without a browser would
   not produce the `webdriver_options` and `_driver_initializer` a real login
   saves, which are the values in question); CI only reads the committed
   fixtures. If a release cannot be launched that way, its selenium fixture is
   omitted and the fact is recorded in the fixtures' README. Fixtures
   generated from 1.21.0 (the HTTPie cookie-policy era), 1.40.1, 1.43.1 and
   1.44.0 are committed under `tests/fixtures/session_compat/`. A test loads
   every one through every loader and compares cookies (every attribute),
   headers, roles, tokens and account against a JSON expectation written by
   the generator.
2. **Round trip.** For each fixture: load, save as a record, load again; the
   two `SessionRecord`s are equal. Same through `update_session_cookies`.
3. **Old reader, new writer.** `scripts/session_compat.py read --graftpunk
   VERSION --store DIR` runs a released version against a store written by the
   branch. A CI job runs it with 1.44.0 against a store written with the
   default (`pickle`): the session loads. A second case writes with `record`
   and asserts the documented outcome (the old release reports it expired and
   does not delete it), so that sentence in the Upgrading note is a tested
   fact.
4. **Lite.** `scripts/smoke_install.sh` gains: on a lite venv, a YAML plugin
   command with a stored session (both formats) returns its response against a
   local fake server.
5. **No code on load.** A record whose JSON carries a string that would be a
   pickle opcode sequence, and a pickle crafted to call a marker function, are
   each fed to the record reader: the first is data, the second is never
   unpickled when the first byte is `{`.

`scripts/session_compat.py` is documented in CONTRIBUTING.md (what it checks,
when to run it: before any change to the stored format or to
`BrowserSession.__getstate__`).

## Left to the builder

- Module layout (`SessionRecord` in `cache.py` or a new `session_record.py`),
  names of private helpers, and test file layout.
- The exact JSON key for each rider and the validation error wording.
- Whether `webdriver_options` can live in the record (see above).

## Open risks

- UNVERIFIED: that every attribute a selenium `BrowserSession` needs after
  `load_session` is covered by the record plus the pickle fallback rule. The
  compat fixtures are what settle it; if a selenium session cannot round-trip,
  selenium sessions stay on pickle in release 1 and the design is revisited
  before release 2.
- UNVERIFIED: how many independent consumers share a store across versions.
  The staged rollout assumes at least one does.
