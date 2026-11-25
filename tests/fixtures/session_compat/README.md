# Saved sessions written by released versions

Every file here is test-only and holds synthetic data: placeholder hosts
(`myshop.example`), placeholder cookie, header and token values, and the
account label `alice`. `key` is a Fernet key generated for these fixtures. It
protects nothing but this synthetic data and must never be used for anything
else.

## `<version>/`

One directory per released graftpunk version (1.21.0, 1.40.1, 1.43.1 and
1.44.0), written by that release from PyPI:

```bash
python scripts/session_compat.py generate --graftpunk 1.44.0 --out tests/fixtures/session_compat/1.44.0
```

For each case there is:

- `<case>.enc`: the encrypted payload the release stored (a dill pickle).
- `<case>.metadata.json`: the `metadata.json` the release wrote beside it. Its
  `storage_location` is rewritten to `~/.config/graftpunk/sessions`, because
  the real value is the absolute path of the machine that ran the generator.
  Expiry is a century after generation, so the fixtures do not expire.
- `<case>.expected.json`: what the session held as the release pickled it
  (from its own `__getstate__`): every cookie's constructor arguments, the
  headers as saved, header roles, cached tokens, account, and the class's
  qualified name.

The cases:

| Case | Saved object |
| --- | --- |
| `nodriver` | a nodriver `BrowserSession` (built with `__new__` and `__setstate__`) |
| `requests` | a plain `requests.Session` |
| `api` | a `GraftpunkSession` |
| `tokens` | nodriver, with two cached tokens, header roles and role origins |
| `account` | nodriver, cached as `myshop@alice` with account identifier `alice` |
| `adapter` | `requests.Session` with `HTTPAdapter(max_retries=3)` on `https://` |
| `selenium` | a selenium `BrowserSession`, built without a browser (below) |
| `subclass` | a subclass of `GraftpunkSession` defined in `__main__`, which dill embeds by value |
| `flatfile` | the `nodriver` payload in the flat-file layout, with no metadata |

The selenium cases are synthetic: the generator builds the object with
`BrowserSession.__new__` and `requestium.Session.__init__`, which starts no
driver, so the pickle has requestium's own initializer and empty webdriver
options. They are not a record of what a real selenium login saves. No
release writes the flat-file layout (`<name>.session.pickle` with no
`metadata.json`, which `LocalSessionStorage._load_legacy_session` reads), so
the generator places the `nodriver` payload there itself.

These files describe frozen releases and are never rewritten; the generator
refuses an `--out` that already holds files. What a case should do today (its
kind, whether it can be stored as a record) is declared in
`tests/unit/session_compat_cases.py`. Add a release by generating a new
directory and adding its version there.

## `legacy/`

`browserfree_session.enc` and `browserfree_session.key` guard the browser-free
load path (`load_session_for_api_from_bytes`), read by
`tests/unit/test_browserfree_session_load.py`, `test_cookies.py`,
`test_lite_cli_hints.py` and `scripts/smoke_install.sh`. The payload is a
`BrowserSession` with `User=dummyuser` and `Password=dummypass` cookies,
encrypted with its own throwaway key.

The pickle names `graftpunk.session.BrowserSession` by reference (module and
qualified name), not by value. Only a by-reference pickle makes the
browser-free unpickler resolve that class, so only it exercises the path
taken when the browser stack is absent. It is not one of the compat cases:
its token cache holds a string, not a `CachedToken`. It is frozen; the script
that built it is in the history as
`git show 5755dbe4:scripts/gen_browserfree_fixture.py`.
