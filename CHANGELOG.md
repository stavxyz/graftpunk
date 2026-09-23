# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Orphaned Chrome cleanup** ([#96](https://github.com/stavxyz/graftpunk/issues/96)). When the Python process that launched a `nodriver` browser dies without running its cleanup (SIGKILL, an OOM kill, a closed terminal, a crash), Chrome survives and keeps its debug port and its memory. All three launch sites (the `NoDriverBackend`, `gp observe`, and browser token extraction) now pass `--graftpunk-owner-pid=<pid>` in `browser_args`, which Chrome ignores and `ps` reports, and every launch first ends the browsers whose owner is gone: SIGTERM, then SIGKILL after a three second grace period. A narrower rule covers browsers launched before the marker existed (no marker, a nodriver temp profile, and a parent of pid 1). A browser without a debugging port, and any browser whose owner is still alive, is never touched. The pass also deletes nodriver temp profiles that no live browser names and that are more than an hour old. POSIX only: on Windows nothing is scanned and nothing is signalled.
- **SIGTERM and SIGHUP end the browser** ([#96](https://github.com/stavxyz/graftpunk/issues/96)). A live browser registers a handle with the new `graftpunk.signals` module, whose handlers send one SIGTERM per registered browser, restore the default disposition, and re-raise the signal, so the exit status stays the conventional 128 + signum and a supervisor still sees the real cause. The handler does nothing else, since it runs while the process is dying; the temp profile of a browser ended that way is collected by the next launch's sweep. A signal slot a host program already handles is left alone. Importing graftpunk installs nothing, and neither does a `gp` command that opens no browser: the CLI sets `graftpunk.signals.auto_install`, and `graftpunk.browser_launch.arm_termination_handlers()` arms the handlers on the main thread at the first browser launch, before the sweep runs. A library consumer opts in with `graftpunk.signals.install_termination_cleanup()`.
- **`GRAFTPUNK_KEEP_ORPHANED_CHROME`** ([#96](https://github.com/stavxyz/graftpunk/issues/96)). Set it to `1` to skip the start-time cleanup entirely. It does not touch the SIGTERM and SIGHUP handlers, which still end a registered browser on those signals.
- **`gp observe digest`** and **`gp observe digest --har PATH`**. Reads a HAR (a run, or a bare file from another tool) into a `RunDigest`: hosts, endpoints (method, templated path, parameter names and types, response shape, custom header names), login observations and forms, token candidates, and cookie names. Redacted by construction: no header, cookie, query, or body value is ever retained, and every URL it keeps (an example path, a login observation, a form action or the page it came from, where a token was seen) is scheme, host, and path only, with no query string, fragment, userinfo, or `;params` in any segment, and a path segment that holds an email address (percent-encoded or not) masked as its placeholder; such a segment also templates like an id (`/users/{user_id}`). A path segment fails closed (`graftpunk.har.paths.looks_dynamic`): one that holds a digit stays literal only when every part (split on `_ . - ~ $`) holds no digit, is a spelled word with a digit run of at most two after it (`address2`, `ec2`), a version of at most two digits (`v2`), or a lone digit run of at most two that is the segment's only part holding a digit (`/page/2`, `step-2`), so a date (`03-14-87` included), a card, phone, or national id number in digit groups, an address such as `10.0.0.1`, a long number, and a short random token all template; a letters-only segment and a word with one or two digits after it (`smith42`) stay literal, the path rule's known limits. A name is dropped only on strong evidence (`graftpunk.har.paths.holds_an_id`): it, or a part of it, is an email, a run of six or more digits, hex of twelve or more characters mixing digits and letters (a UUID included), `0x` and twelve or more hex digits, three or more all-digit parts totalling seven or more digits (`123-45-6789`; a whole date is a name), a phone number with its area code in parentheses (`(555)123-4567`), a prefixed id with a tail of twelve or more characters mixing upper case, lower case, and digits, at the start or at any `_`/`-` part (`cus_NffrFeUfNV2Hib`, `otp_cus_NffrFeUfNV2Hib`), or a base64-like token of twenty-four or more characters with at least five letter/digit switches; a path segment it reads as an id templates too. A short random token used as a field name (`kqzpwmab47`) is kept, the name rule's known limit. Every entry of a key-position id table is caught, each name sub-rule is the only catch of one entry, and the names read as ids are held under ceilings on a regression corpus, the round-7 corpus of public SDK names the thresholds were set against, and a fresh corpus written after (none of any when the rule last changed); a response object whose keys are ids as a group (three or more alphanumeric keys of one length of twelve or more, each mixing letters and digits and not reading as a word with a short number, and not one name numbered, such as `addressLine1` to `addressLine3`) has every key replaced by `{key}`; the path rule's miss rate on random tokens of each shape is held under a ceiling. Every name position goes through it (query, JSON body, and form keys; response keys; request header names; cookie and token names; a login form's element ids, input names, and hidden input names): a query, body, or form key or a request header name that holds one is dropped and counted per endpoint (`Endpoint.query_keys_dropped_as_ids`, `body_keys_dropped_as_ids`, `header_names_dropped_as_ids`, apart from a key dropped for not being a field name at all: `query_keys_dropped_as_non_names`, `body_keys_dropped_as_non_names`), which the generated stub states in one `GP-FILL` comment per cause; a response key that holds one becomes `{key}` in the shape; a cookie or token name that holds one is left out in any form and counted (`RunDigest.cookie_names_dropped_as_ids`, `token_names_dropped_as_ids`; the generated plugin notes dropped token candidates in one `GP-FILL` comment); and a hidden login input whose name holds one is dropped and counted (`LoginForm.hidden_names_dropped_as_ids`). A login form's roles follow HTML semantics around its password input (the one marked `current-password`, else the first): the username is the input `autocomplete="username"` names, else the one `autocomplete="email"` names nearest before the password, else the hinted text-like input nearest before it, else the nearest text-like input, else an input after it named exactly `username`, `email`, `login`, or `user`; the submit is the first submit control after the password (an image input counts, and a control whose `form` attribute names the form belongs to it); other text-like inputs between them are roles by name; a checkbox, radio, file, image, reset, range, or hidden input is never a field role; and a registration form (no `current-password`, and a `new-password` or a confirmation password) is left out, a lone form with one password marked `new-password` excepted. A control outside the form its `form` attribute names is selected by its id or by `tag[form="id"]` with its name or type when that is unique on the page, and a name selector is used only when no other input of the form shares the name. A username the form has no input for is listed in `LoginForm.absent_roles` with its own `GP-FILL`, a neutral role says whether its input had no name (`LoginForm.nameless_roles`), an action that is only a fragment is scoped to match, and the same form recorded on several pages is listed once. An input is selected by its id, else its name, else its type (`input:not([type])` for a typeless input), never by an id or name that holds an account value; such a name, or none, gets a neutral role key (`field_1`, never another input's name) and a `GP-FILL` saying to rename it; a selector by type is used only when it picks one input of the form and is never printed without the form scope; and a role left without a selector is listed in `LoginForm.unresolved_roles` with a `GP-FILL` naming it (`LoginForm.neutral_roles`, `unresolved_roles`, `unscoped_fields`, `unscoped_submit`). A POST to where a login form a GET served posts (the same host and path, resolved against the unmasked page and compared before email masking) is the credential post whatever its password field is named, and a form in a POST's own response never marks that POST; a page carrying a login form is the form page (and `login_flow`) only when a credential post follows it in the login window, and the form a credential post went to is listed first. A field name may hold a `$` (OData `$filter`, WebForms `ctl00$Main$txtSearch`), and a form body with some unreadable keys stays a form with those keys counted. A parameter's type is `str`, `int`, `float`, `bool`, `object` (a JSON object), `mixed` (JSON values no one type sends), or `list[<element>]` (a repeated query or form key, or a JSON array; the element is one of those, `list`, or `unknown` when only empty arrays were seen). A query or form value is typed only when the typed value is sent back spelled exactly as recorded (`07030` stays `str`, and so does `True`; `bool` is the lowercase `true` or `false`), and a query or form parameter the endpoint's requests type differently is `str`. A JSON body field keeps its JSON type: `null` is not an observation, `int` and `float` together are `float`, two arrays merge their element types the same way (`list[int]` with `list[float]` gives `list[float]`, other element types give `list[mixed]`), and any other disagreement is `mixed`. `graftpunk.har` gains the model (`DigestSource`, `RunDigest`, `Endpoint`, `ShapeNode`, `LoginObservation`, `LoginForm`, `TokenCandidate`, `digest`).
- **`gp observe fixtures`**. Writes captured response bodies exactly as recorded, named and matched against `gp observe digest`'s own templates (a family the digest collapsed into one endpoint, such as `GET /products/{product_id}`, included), for deriving committed test fixtures by hand. Captures never enter git: the target directory is added to `.gitignore` automatically, and a tracked path refuses without `--allow-tracked`. Each capture gets a `<file>.meta.json` sidecar written to be committed beside the fixture derived from it: a `schema` number, the status, the content type, the body parameter names (a JSON body's keys are held to the same "reads as a field name" rule a form body's keys already were: a key that does not read as a field name, such as an email address or a key starting with a digit, or that holds an id, is dropped), the sha256 of the captured body as a 64-character lowercase hex digest, and every cookie name any response in the recording set (a static response and an out-of-scope host included, since a `--match` glob can write a capture from either), plus the token candidate names the run's digest recorded, a name that holds an id left out in any form and counted in `redacted_names`; never a header, cookie, query, or body value, the URL, or the capture time. A `--match` pattern that matched nothing is named in the output, a repeated capture of one template gets a `#1`, `#2` suffix, two templates that would share a fixture stem (`/a_b` and `/a/b`, whatever their extensions) are refused before anything is written, and a `.gitignore` that is a directory is refused as one. `graftpunk.testing.FixtureSession` reads it through `graftpunk.testing.sidecar`, which refuses a sidecar with a missing or unknown `schema`, a key outside its version, a value of the wrong type or sign, or a malformed `capture_sha256` (and `Sidecar` refuses the same at construction) by raising `graftpunk.testing.sidecar.SidecarError`, a `graftpunk.exceptions.GraftpunkError` rather than a `ValueError`, so a plugin's `except ValueError` around a fixture-backed request does not swallow it.
- **`CommandContext.request_json`** and **`request_text`**. The role, rejection, and body-shape policy every generated (and hand-written) command needs, backed by the new `graftpunk.plugins.site_requests.SiteRequests`. New `SessionRejectedError` and `UnexpectedResponseError` in `graftpunk.exceptions`.
- **`graftpunk.testing`**. `make_context`, `FixtureSession`, `fixture_context` (pytest-free), and `graftpunk.testing.plugin.site_env_scrubber` (a pytest fixture), for testing a plugin's commands without a network.
- **`LoginConfig.success_url`, `LoginConfig.timeout`, and `LoginConfig.settle`**, in Python and as `login.success_url`, `login.timeout`, and `login.settle` in YAML. `success_url` is a glob matched against the whole browser URL after the last login step (`https://app.example.com/*`, `*/dashboard*`), usable on its own or alongside `success`, in which case both have to hold. `timeout` (default `30.0`) is how long the engine waits for the success or failure signal, and `settle` (default `1.0`) is the pause between the document finishing loading and the cookie capture. `gp plugin new --from-run` fills in `success_url` from the URL the recorded login redirected to.
- **`docs/PLUGIN_DEVELOPMENT.md`**, the plugin developer guide. One document from naming a plugin to publishing it: what a plugin is and why it registers through an entry point rather than the plugins directory, recording a site with `gp observe interactive`, reading the recording with `gp observe digest`, scaffolding with `gp plugin new`, filling the stubs with `ctx.request_json`/`request_text`, `LoginConfig` and the post-submit signals, credentials through the workstation env file, and tests against `graftpunk.testing` fixtures. The plugin sections of `README.md`, `docs/HOW_IT_WORKS.md`, and `examples/README.md` now point into it.
- **`gp plugin new <name> [--url URL] [--from-run SESSION] [--run RUN_ID] [--dir PATH] [--backend nodriver|selenium] [--new]`**. Scaffolds a new plugin project, or adds one to an existing suite, optionally filled in from a `gp observe digest` run: `login_config`, `token_config`, and up to twelve command stubs. The generated `login_config` sets `url` to the page the login form was on (a `GP-FILL` comment when that page's path holds an id, or when the form came from a saved page source), unscopes its selectors when the form's action holds an id, and templates an id in the landing path of `success_url` as `*` (a landing path of ids only sets no `success_url`); an id here is a path segment the digest's lexical rule reads as one, so an id in a shape it does not read as one is not caught. `--run` names a specific run instead of the session's newest one and requires `--from-run`. A plugin name starts with a letter, uses letters, digits, hyphens, and underscores, and is at most 40 characters; the command refuses anything else, and refuses a name that collides with a reserved top-level CLI command name (`plugin`, `plugins`, `session`, `http`, `config`, `keepalive`, `observe`, and any other top-level command registered by the time plugins attach). A hyphenated name maps to an importable package: `gp plugin new my-shop` writes `src/graftpunk_my_shop/plugin.py` and `tests/test_my_shop.py`, registers the entry point `my-shop = "graftpunk_my_shop.plugin:MyShopPlugin"`, and the plugin's own CLI command stays `gp my-shop`. A generated project passes its own `ruff check` and `ruff format --check` as written. Every file the command writes goes through one write step that refuses before touching anything when a target conflicts, validates each generated Python and TOML file's grammar before the first byte is written, and restores every file it had already changed if a later write fails, naming any path it could not restore. Adding a plugin to an existing suite edits that suite's `pyproject.toml` and `.gitignore` without disturbing a byte outside the change, CRLF line endings included (the line added to a CRLF `.gitignore`, here and by `gp observe fixtures`, ends in CRLF too); a `pyproject.toml` that mixes CRLF and LF line endings is refused with a message to normalise it, a file the edit would touch that is read-only is refused before anything is written, and a `pyproject.toml` or `.gitignore` that is not UTF-8 text is refused by name.
- **Machine surfaces for tools that drive `gp`** (the groundwork for the `/graftpunk:graft` Claude Code skill). `gp observe digest --endpoints-json` prints a versioned projection of the digest (method, template, `login_flow`, content type, shape summary, parameter names and types, custom header names, and the login's auth URLs and form selectors, the URL paths, form actions included, templated as endpoints are and each form action cut before its query string and `;params`, and a form whose action templating changed printed with each selector without its form scope, since the scope spells the literal action: an id selector, or a name selector no other input on the recorded page shares (`input[name="username"]`), any other being left out and named in the form's `unresolved_roles`; each form carries `action`, `fields`, `submit`, `neutral_roles`, and `unresolved_roles`; no cookie name, token candidate, example path, or body). `gp version --json` prints the installed version and the schema number of each payload a caller reads; `--at-least VERSION` exits 0 or 1 by version order (`packaging` is now a dependency), and `--contract SURFACE=N` (repeatable) exits 3, naming the older side, unless graftpunk writes that payload at that schema. `gp plugin new NAME --check-name` checks a name and writes nothing. Every stub `gp plugin new --from-run` renders from the digest declares the endpoint it calls as `@command(endpoint="GET /api/orders")`, a new keyword stored on the command, never consulted at runtime, and never shown in help. A stub's `int` or `float` parameter carries an explicit `PluginParamSpec` entry so its type survives; a `bool` parameter's entry makes it a flag with a negative (`click_kwargs={"is_flag": True, "flag": "--archived/--no-archived"}`, the `flag` key `command_factory` already read, now documented on `PluginParamSpec`), reaching the handler as `True`, `False`, or `None` when neither is given, which `ctx.request_json` sends as `archived=true`, `archived=false`, or nothing (related: [#208](https://github.com/stavxyz/graftpunk/issues/208), which this only works around for generated stubs, not fixes; a hand-written command's `int | None` annotation can still be introspected as `str`). A stub's request body carries only the fields the caller gave, and a body the recording posted as a form is sent as form data (`data=`) rather than JSON. A list parameter is a repeatable option (the new `multiple` key of `PluginParamSpec`'s `click_kwargs`), sent as repeated query or form keys or as a JSON array; a JSON body field no option can send as recorded (an object, a `mixed` value, or an array of objects, booleans, arrays, mixed elements, or only empty arrays) is left out of the stub with a `GP-FILL` comment naming it; a body field typed apart from a query parameter of the same name gets its own `--body-<name>` option; and a site parameter named for a built-in option (`format`, `output`, `session`, `view`) or `help` gets a suffixed option (`--format-2`). A generated command's name is a Python identifier (`import` gives `import_`, a leading digit gains `n_`), never a `SitePlugin` attribute (`setup` gives `setup_2`), and its test never repeats a fixed test's name; a stub percent-encodes each path value (`urllib.parse.quote` imported as `_quote_path`) before it goes into the URL, and two endpoints sharing a fixture stem get one generated test and a `GP-FILL` for the other. `--match` values now need the method in capitals, as the digest prints it, and a template that starts with `/` or `*` and holds no whitespace; anything else is refused instead of matching nothing.

### Changed

- **The post-submit wait is a bounded poll for the configured signal, not a fixed three seconds.** After the last login step both backends now check the page every half second until `LoginConfig.timeout` (30 seconds by default): the `failure` text ends the wait as a failure and the success signal (the `success` element, the `success_url` glob, or both when both are set) ends it as a success. A `Too Many Requests` page ends it as a failure on every pass that reads the page text, which is every pass when `failure` is set, every pass once a `success_url` matches, and in every configuration the last pass before the timeout, so a login that gives up can still name the limiter rather than its own signal. On success the engine waits for the document to finish loading, pauses for `LoginConfig.settle`, and then captures cookies as before. What you see on the first run after upgrading: a login that used to fail because the site was still redirecting when the three seconds ran out now succeeds, and a login whose signal never appears now fails after `timeout` seconds instead of three, with a warning naming the selector or URL pattern that never appeared and the URL the page ended on. A login with neither `success` nor `success_url` configured keeps the three-second window it always had, and now spends it watching for the `failure` text rather than sleeping through it: an error the site renders a second or two after the submit is caught instead of missed, and a clean page still takes the same verdict, with the same warning when nothing validates the login at all. A plugin that wants the old three-second give-up back sets `timeout=3.0`. A page that could not be read at all after the submit (the browser closed, or the tab went away mid-redirect) now fails the login with a `login_page_unreadable` warning instead of raising the read error out of the login. On the selenium backend a `success` selector the browser cannot parse now fails the login at once with a `PluginError` naming the selector, instead of waiting out the timeout. Plugins need no change.
- **An installed plugin whose name collides with a top-level `gp` command is skipped, and the rest still load.** On the first `gp` run after upgrading, a plugin whose `site_name` is a top-level command name (`plugin`, `plugins`, `session`, `http`, `config`, `keepalive`, `observe`, and any other registered by the time plugins attach) stops loading: `gp` prints a warning naming it, and every other installed plugin is registered as before. To get it back, rename it: change its `site_name` and the name on its `graftpunk.plugins` entry point, then reinstall. Its commands move with the name (`gp http items` becomes `gp myshop items`).

### Removed

- **`gp import-har`**, replaced by `gp observe digest` and `gp plugin new` (see the README's "From recording to plugin"). It generated handlers taking a raw `requests.Session`, a shape the framework stopped accepting once command handlers started receiving a `CommandContext`.
- **`graftpunk.har.analyzer`** and **`graftpunk.har.generator`**, and the exports they backed on `graftpunk.har`: `detect_auth_flow`, `discover_api_endpoints`, `generate_plugin_code`, `extract_domain`, `APIEndpoint`, `AuthFlow`, `AuthStep`. Superseded by `graftpunk.har.digest`.
- **`examples/templates/python_template.py`**, replaced by `gp plugin new`. `examples/templates/yaml_template.yaml` is unaffected.

### Fixed

- **The `nodriver` temp profile no longer leaks on every stop** ([#96](https://github.com/stavxyz/graftpunk/issues/96)). nodriver creates its profile with `tempfile.mkdtemp(prefix="uc_")` and deletes it only from an `atexit` handler that iterates its registry of live browsers. graftpunk removes its browser from that registry on stop, to keep that handler's output off stdout, so nothing deleted the directory and each session left one behind under the temp directory. All three stop paths (the `NoDriverBackend`, `gp observe`, and browser token extraction) now delete it. A custom `profile_dir` is never touched. In the `NoDriverBackend`, a start attempt that fails after Chrome is already up releases that browser and its profile too, rather than leaving one of each behind per attempt ([#192](https://github.com/stavxyz/graftpunk/issues/192)).

## [1.16.0] - 2026-09-07

This is a minor release. It carries two documented changes for plugin authors and one new behaviour inside command handlers.

### Upgrading

1. CLI installs do not upgrade themselves when a project raises its graftpunk dependency floor. Run `uv tool upgrade graftpunk`, `pipx upgrade graftpunk`, `pip install -U graftpunk`, or `pip install -U 'graftpunk[browser]'` when the browser extra is in use (extras must be repeated on every upgrade).
2. **Inside a hand-written `login()`, `self.session_name` is the bare base.** A login that caches through `browser_session()` or `browser_session_sync()` needs nothing. One that calls `cache_session(session, self.session_name)` by hand now writes the bare slot; call `cache_login_session(self, session)` instead (exported from `graftpunk.plugins`), or accept `session_name` and `account_identifier` keyword arguments. After a login that did not write its target slot, the CLI says so and names that migration. Listed under Changed below.
3. **`CLIPluginProtocol.get_session` takes an optional `session_name`.** A type checker flags a structural implementer that still declares the zero-argument form; add `session_name: str | None = None`. `SitePlugin` subclasses and `isinstance` checks are unaffected. Listed under Changed below.
4. **`self.get_session()` inside a command handler loads the account the invocation resolved.** With one cached account nothing changes; with several, it follows the pin instead of raising `AmbiguousSessionError`. Listed under Added below.

### Added

- **The operating session scope** ([#174](https://github.com/stavxyz/graftpunk/issues/174)). A new internal `graftpunk.session_scope` module holds the session slot one dispatch is about in a `contextvars.ContextVar`, and owns `resolve_load_target(base, explicit)`, the one place the explicit-then-scope-then-bare precedence lives. The shared execution pipeline sets the scope around every command handler from the operating name the dispatcher already resolved, and the login command sets it around the login callable, so both the CLI and `GraftpunkClient` behave the same way without either of them managing the scope itself.
- **`SitePlugin.get_session(session_name=None)`** ([#174](https://github.com/stavxyz/graftpunk/issues/174)). `get_session()` now follows one chain: an explicit `session_name`, then the operating session scope, then the plugin's bare base name. Called from a command handler it loads the slot the invocation is pinned to, so `self.get_session()` under `--session myshop@bob` returns bob's session where it used to resolve `myshop` on its own terms and raise `AmbiguousSessionError` with two accounts cached. An explicit name wins over both tiers: a labelled name loads exactly, a bare name resolves. An explicit name is honoured even when `requires_session` is False, since only the no-argument call short-circuits to a plain session. Outside a command, nothing sets a scope and the behaviour is unchanged. `cache_login_session` is now exported from `graftpunk.plugins` beside `SitePlugin`.

### Changed

- **`CLIPluginProtocol.get_session` gained an optional `session_name` parameter** ([#174](https://github.com/stavxyz/graftpunk/issues/174)). A plugin that subclasses `SitePlugin` is unaffected. A plugin that satisfies the protocol structurally, with its own `get_session(self) -> requests.Session`, no longer matches it: a type checker flags it; `isinstance` is unaffected, because the protocol is `runtime_checkable` and checks attribute presence only. Add `session_name: str | None = None` to that signature. Nothing calls the protocol method with an argument, so an implementation that accepts and ignores it is enough.

- **The login identity stamp is retired, and a hand-written `login()` now reads the bare base name** ([#174](https://github.com/stavxyz/graftpunk/issues/174)). `gp <site> login --as bob` used to temporarily overwrite `plugin.session_name` and an account attribute on the plugin instance for the length of one login flow. It now sets the operating session scope instead and never mutates the instance. **Behaviour change:** inside your own `login()`, `self.session_name` always reads the class attribute (`myshop`), where during a login it used to read `myshop@bob`. A `login()` that caches through `browser_session()`, `browser_session_sync()`, or `cache_login_session(self, session)` is unaffected and still lands on `myshop@bob` with the identifier recorded. A `login()` that calls `cache_session(session, self.session_name)` by hand now writes the bare `myshop` slot; a successful login that writes nothing to the slot the CLI named now says so and names the fix, rather than reporting a session name that nothing was cached under. The check runs for a bare target too, so a login that caches nothing at all is reported either way. The migration is one line: call `cache_login_session(self, session)` (exported from `graftpunk.plugins`), or declare `session_name` and `account_identifier` keyword arguments on `login()`, which the CLI already passes to any login that accepts them. Generated declarative logins are unaffected: they receive both values explicitly.

- `CommandContext._session_name` is renamed to `_operating_session_name`, to say plainly that it holds the slot a load resolved to rather than the caller's raw pin ([#178](https://github.com/stavxyz/graftpunk/issues/178)). A read-only `_session_name` property remains on `CommandContext` as a compatibility alias returning the same value, so a command handler still reading the old name keeps working.

### Fixed

- `update_session_cookies(session, base)` follows the slot the session was loaded from when `base` is that slot's base name and no slot is cached under `base` itself, so `SitePlugin.get_session()` and `load_session_for_api(base)` callers that persist with the bare name no longer drop the refresh ([#174](https://github.com/stavxyz/graftpunk/issues/174)). A slot cached under the name given always wins, so an existing bare `base` slot is still written. The slot name rides on the session object in memory and is not pickled; both the loaders and `cache_session` set it, so the last load or save wins. Explicit labelled names and slots from another base stay literal.
- `gp session export` and `gp session use` now honor `--storage-backend` when resolving the session name, and `gp session export` also honors it when loading the session, instead of resolving (and, for export, loading) against the default backend regardless of the flag ([#178](https://github.com/stavxyz/graftpunk/issues/178)).
- Session storage backends' "not found" logs on a plain miss are now DEBUG instead of WARNING, since a miss on the way to a hit (for example `load_session_for_api("myshop")` resolving to `myshop@alice`) is a normal query result. A real not-found still raises `SessionNotFoundError`, and the API loader records it at INFO ([#178](https://github.com/stavxyz/graftpunk/issues/178)).
- `gp http --session <name>` no longer lists cached sessions twice when `<name>` is a registered site name (or alias) with nothing cached. It lists once on the miss path ([#178](https://github.com/stavxyz/graftpunk/issues/178)).

### Security

- Loading a local session now tightens an existing `metadata.json` or `session.pickle` file's permissions to 0600 when they are wider than that, and re-saving one does the same ([#178](https://github.com/stavxyz/graftpunk/issues/178)).

## [1.15.1] - 2026-09-07

### Fixed

- Every CLI surface that takes a session name (the `gp session` commands, `gp observe --session`, and `gp http --session`) now operates on the exact cached slot when one exists under that name, and only resolves accounts when it does not ([#186](https://github.com/stavxyz/graftpunk/pull/186)). Previously a registered site name was always resolved, so with a legacy bare slot next to a labelled one the documented `gp session clear <base>` step exited with the ambiguity list and removed nothing. An unpinned plugin command is a deliberate exception and keeps refusing when several sessions share a base, since that surface writes the session back on every command and refusing forces the one-time migration described below.

## [1.15.0] - 2026-09-06

This is a minor release: it adds features and carries two documented behaviour changes, each with a one-line escape hatch (the reserved `session` option name, and kebab-cased command names).

### Upgrading

1. CLI installs do not upgrade themselves when a project raises its graftpunk dependency floor. Run `uv tool upgrade graftpunk`, `pipx upgrade graftpunk`, `pip install -U graftpunk`, or `pip install -U 'graftpunk[browser]'` when the browser extra is in use (extras must be repeated on every upgrade).
2. Library consumers should wrap `execute()` calls and catch `AmbiguousSessionError` next to `SessionNotFoundError`. Construction no longer raises for session state.
3. Anything that writes a session back should load with `load_session_for_api_resolved` and use the returned name.
4. Plugins that declare their own `session` option must rename it.
5. **Stale bare-name sessions after the first labelled login.** The first `gp <site> login` after upgrading writes a new `base@label` slot and leaves the legacy bare `base` slot in place. From that point, an unpinned command refuses with `AmbiguousSessionError` naming both candidates, and library code calling `load_session_for_api("base")` keeps loading the stale bare slot, because an exact name match wins over resolution. Upgrading alone does not touch cached sessions; this first appears on the first re-login after upgrading. The remedy is a one-time manual step: run `gp session clear base` once after that first login to remove the stale slot, or pin the session explicitly with `--session base@label`, `GRAFTPUNK_SESSION`, or `session=` in code. On 1.15.0 itself, `gp session clear base` exits with the ambiguity list and removes nothing. Pin the session with `--session base@label` or `GRAFTPUNK_SESSION` until 1.15.1 is installed (the fix is the `[1.15.1]` entry above), then run `gp session clear base` once.
   - Fix any code that still calls the low-level `load_session("base")` (importable as `graftpunk.load_session` or `graftpunk.cache.load_session`) before removing the stale slot. That function is literal and raises `SessionNotFoundError` once the bare slot is cleared and only the labelled slot remains. Use `load_session_for_api` (or `load_session_for_api_resolved` when writing the session back), or pin the labelled name. The swap does not return the cached session object itself. The `requests.Session` it returns carries the cookies, headers, header roles, cached tokens, and the account identifier. It does not carry CSRF tokens, since CSRF tokens are per-request and are never persisted. A caller that needs the cached session object itself should pin the labelled name.
6. **CLI command spelling.** `@command` now kebab-cases function command names: `gp <site> by_parcel` becomes `gp <site> by-parcel`. Pin the old spelling with `@command(name="by_parcel")`, or update the invocation to the hyphenated form.
7. **Invalid session names fail earlier.** A session name that cannot be valid (`"MyShop@Alice"`, `"../../x"`) now raises `ValueError` where it used to reach storage and surface as `SessionNotFoundError`. Error handlers that name only `SessionNotFoundError` should add `ValueError`.

### Added

- **Typed token-extraction exceptions** ([#131](https://github.com/stavxyz/graftpunk/issues/131)). A new `TokenExtractionError` carries two subtypes: `SessionInvalidatedError` for a required cookie that is gone (re-login) and `TokenPatternMismatchError` for a header or pattern that no longer matches (fix the Token config). All three are exported from `graftpunk`. Browser-mode failures raise the base class, since the cause cannot be distinguished from an empty result. All three also subclass `ValueError` permanently, so existing `except ValueError` code keeps working. `gp` now tells you whether to re-login or fix the config.
- **`--format` help lists a plugin's own formats** ([#116](https://github.com/stavxyz/graftpunk/issues/116)). A plugin that registers `format_overrides` (for example `html`) now sees them in every plugin command's `--format` help: `Output format (built-in: json, table, raw, csv, xlsx, pdf; plugin: html)`.
- **Multi-account sessions** ([#151](https://github.com/stavxyz/graftpunk/issues/151)). Sessions are keyed by plugin and account (`myshop@alice`). `gp <site> login` derives the account label from the login identifier (`--as` overrides it) and records the identifier in metadata, so `gp session list` shows it. Commands resolve one operating session per invocation, checked in this order: `--session`, then `GRAFTPUNK_SESSION`, then `.gp-session`, then the sole cached session. They refuse with `AmbiguousSessionError` when several are cached and none is picked, and the load-resolved name keys every write-back. The env and file tiers are base-scoped: a pin for another plugin is ignored, and resolution continues for the plugin you are running. An explicit `--session` always wins. The label derives only from identifier-shaped fields (username, email, login, identifier, user); a one-time code, PIN, passphrase or security answer is never turned into a session label, and when nothing qualifies, the session keeps its bare name (`--as` still names it). `GraftpunkClient` gains `session=` and ignores ambient shell state by design. Logging in over a slot recorded for a different account warns. Existing bare-name sessions keep working; no migration is required.

### Changed

- **`GraftpunkClient` resolves its session on first use; construction no longer resolves it** ([#181](https://github.com/stavxyz/graftpunk/issues/181)). Construction performs no session-storage I/O and never raises for session *state* reasons. `AmbiguousSessionError` (several accounts cached, unpinned) and `SessionNotFoundError` now surface from the first `execute()` call, so catch `AmbiguousSessionError` next to `SessionNotFoundError`. Lazy resolution moves the error into the call that `execute()` already wraps: a wrapper that only names `SessionNotFoundError` still lets ambiguity through, so add one `except` clause for `AmbiguousSessionError`; construction itself does not need to change. "Pick one" is the correct instruction: logging in again adds a third slot, and that does not resolve the ambiguity. Plugin discovery still runs at construction (`PluginError` is unchanged). An illegal pin string (`session="MyShop@Alice"`, `session="../../x"`) still raises `ValueError` there, because that check is pure policy with no I/O, so a typo fails where it was typed. Pin with `GraftpunkClient("myshop", session="myshop@alice")` to skip resolution entirely. A **labelled** pin lists nowhere at all, hit or miss, on both the client and the CLI (plugin commands and `gp http`: `--session myshop@alice`, `GRAFTPUNK_SESSION`); the bare-base fallback matches on the base, which a labelled miss can never reach, so resolving one would only pay for a useless listing. A **bare** pin on the client and on plugin commands resolves at load time and lists once on the miss path, exactly as before; `gp http` resolves a registered plugin's base name before the load, as before, so a full miss there lists twice. One consequence to know about: a command that overrides `requires_session=True` on a `requires_session=False` plugin now takes the same *unpinned* semantics as everything else. With a legacy bare `myshop` cached alongside `myshop@alice`, this now raises `AmbiguousSessionError`; it used to take the bare slot outright. Pin the client (`session="myshop"`) to keep the old behaviour.

- **`load_session_for_api(name)` and `SitePlugin.get_session()` treat a bare name as a base name** ([#182](https://github.com/stavxyz/graftpunk/issues/182), [#151](https://github.com/stavxyz/graftpunk/issues/151)). When nothing is cached under that name exactly, they resolve a bare base name to its single cached account, or raise `AmbiguousSessionError` naming the candidates; the slot actually loaded is the one written back to. A slot cached under the bare name itself still wins outright, with no listing and no ambiguity. Library consumers holding a bare name keep working after labelled logins, but the exception surface changed: code catching `SessionNotFoundError` should also catch `AmbiguousSessionError`. A name that cannot be a session name at all (`"MyShop@Alice"`, `"../../x"`) now raises `ValueError` before anything is loaded or listed, where it used to reach storage and come back as `SessionNotFoundError`. New `load_session_for_api_resolved(name, *, resolve=True)` returns `(session, loaded_name)`. **Use it instead of `load_session_for_api` whenever you write the session back**, since `update_session_cookies(session, name)` uses its name literally and a bare name would refresh a slot that does not exist. `resolve=False` on either function is exact-only, for callers that already resolved.

- **`session` is now a reserved plugin option name** ([#151](https://github.com/stavxyz/graftpunk/issues/151)). Every generated plugin command carries the built-in `--session` for multi-account resolution; the synthesized `login` command does not, since it derives the account label from the login identifier instead. **Breaking for plugins** that declared their own `session` parameter on a command: registration now fails with `PluginError` ("reserved parameter name"). Rename the plugin parameter.

- **`@command` kebab-cases function command names by default and accepts `name=`** ([#147](https://github.com/stavxyz/graftpunk/issues/147)). A method `by_parcel` is now `gp <site> by-parcel`, matching what groups already did; auto-discovered group methods follow the same rule. **Behaviour change for the CLI (minor version bump)**: a multi-word function command that was typed with underscores is now hyphenated. Pin the old spelling with `@command(name="by_parcel")` if you need it. `GraftpunkClient` is unaffected: `client.by_parcel()`, `execute("by_parcel")` and `execute("by-parcel")` all resolve. Two Python names that kebab-case to the same CLI name now raise `PluginError` in the client, as they already did in the CLI.

- **`ty` type-checking is blocking in CI again, pinned to 0.0.75** ([#130](https://github.com/stavxyz/graftpunk/issues/130)). The same pinned command runs in CI (`.github/workflows/python-quality.yml`), `just lint`, and `CONTRIBUTING.md`. The nodriver "false positives" were ty failing to read `nodriver/cdp/network.py` at all, because the generated file carries a Latin-1 byte. A partial stub under `typings/` (not shipped in the wheel or sdist) covers that one module, and the twelve nodriver `type: ignore` comments that had been papering over the unreadable CDP modules are gone; an unused `type: ignore` is now itself an error. The remaining diagnostics were real typing gaps and are fixed in code.

### Fixed

- **Status messages treat names as data** ([#166](https://github.com/stavxyz/graftpunk/issues/166)). Session names, domains, storage backends, config keys, HAR paths and error text are escaped before Rich renders them, so a value containing a bracket sequence no longer raises `MarkupError` or disappears from `gp session list/show/export/clear/use`, `gp keepalive`, `gp config` and `gp import-har` output, including the generated-code preview of `--dry-run`.

## [1.14.0] - 2026-08-26

Minor rather than patch: every entry is a fix, but three of them change observable
defaults that a consumer may have been relying on. **Changed behaviour:** as a
library, graftpunk now logs to stderr at WARNING when structlog is unconfigured
(previously stdout, unfiltered); CSV written to stdout uses `\n` line endings
(previously `\r\n`); `gp version` puts the project description in the panel body
rather than the title.

### Fixed

- **The sdist ships only the package** — `tests/`, `docs/`, `examples/`, CI config and scratch directories no longer go to PyPI; the source distribution is an explicit allowlist (`src/graftpunk`, `README.md`, `LICENSE`, `CHANGELOG.md`, `pyproject.toml`).
- **Raw, CSV and JSON output bypass Rich** ([#145](https://github.com/stavxyz/graftpunk/issues/145), [#144](https://github.com/stavxyz/graftpunk/issues/144)) — data payloads were printed through Rich's markup parser and word-wrapper: an item name containing `[/LB]` crashed `--format raw` with a MarkupError, `--output` files carried ANSI codes under FORCE_COLOR and had rows over 200 columns broken across lines, and `gp session list/show --json` was invalid JSON at narrow terminal widths. Payloads are now written byte-for-byte (UTF-8) to the file or to stdout; JSON is syntax-highlighted only on an interactive terminal and never wrapped; table cells, headers, `gp` status lines (`Saved: …`) and `observe` names are treated as data rather than markup; table files and `CommandResult.export()` render without colour. CSV output now uses `\n` line endings on every platform.
- **Library use no longer logs to stdout** ([#163](https://github.com/stavxyz/graftpunk/issues/163)) — importing graftpunk without the `gp` CLI left structlog unconfigured (stdout, no level filter), so a stray import-time debug line could corrupt a consumer's machine-readable output. graftpunk now applies a stderr/WARNING default when structlog is not already configured, and nothing logs at import time.

### Changed

- **README, PyPI description and `gp --help` describe the project as it is** ([#161](https://github.com/stavxyz/graftpunk/pull/161)) — authenticated browser sessions captured once and replayed over plain HTTP (stealth login, encrypted at rest, pluggable storage) — instead of the "Turn any website into an API" tagline. Inside the package the text has one owner, `graftpunk.DESCRIPTION` / `graftpunk.LONG_DESCRIPTION`, which the banner, the `gp version` panel and the docstrings derive from. The framing sections now name the mechanism (browser login once; session, header fingerprint and tokens captured and encrypted; replayed over plain HTTP from Python or a generated CLI). `pyproject.toml` keywords gain `har`, `cdp`, `nodriver`, `csrf`. No behaviour change; a new test pins the description across every surface.

## [1.13.1] - 2026-08-25

### Fixed

- **Observe HAR now carries `Set-Cookie` and the raw `Cookie` header** ([#157](https://github.com/stavxyz/graftpunk/issues/157)). Chromium delivers only filtered headers on `Network.responseReceived`; both capture backends now subscribe to the `*ExtraInfo` events and merge the raw wire headers into each entry, parsed into HAR `response.cookies` / `request.cookies`. On an observed login the `POST → 302` hop shows the session cookie it minted. Event order is not assumed (CDP does not guarantee it): headers are buffered and correlated by request id, and a redirect hop keeps its own `Set-Cookie` (a late one is matched to the hop by status code; a hop whose ExtraInfo never arrives is counted in the `extra_info_unmatched` drain log rather than given its successor's). Raw request `Cookie` headers depend on nodriver delivering `requestWillBeSentExtraInfo`: measured at the handler on a real login, 0.48.1 delivered 1 of 11 events and 0.50.3 delivered 9 of 11 (intermediate versions untested); `Set-Cookie` worked on both.
- **Console-log timestamps are seconds since epoch** ([#158](https://github.com/stavxyz/graftpunk/issues/158)). CDP's `Runtime.Timestamp` is milliseconds; `console.jsonl` mixed that with a `time.time()` seconds fallback. Both are normalised to seconds.
- **Observe HAR now records every hop of a redirect chain** ([#153](https://github.com/stavxyz/graftpunk/issues/153)). CDP reuses the request id across redirects and both capture backends kept only the final request, so a login `POST` that answered with a `302` was absent from the HAR entirely — the one request an observed login exists to show. Each hop is now its own HAR 1.2 entry with the redirect's status and headers and a `redirectURL` link to the next hop; redirect hops are skipped by body fetching.
- **`NoDriverBackend.delete_all_cookies()` raised `TypeError` on every nodriver version** ([#152](https://github.com/stavxyz/graftpunk/issues/152)). It passed the method name as a string to `tab.send()`, which expects the CDP generator; the `TypeError` escaped the best-effort `except` instead of returning `False`. Now sends `Network.clearBrowserCookies` properly and is covered by a test that executes the coroutine against a nodriver-faithful `send()`. The method is also now best-effort for **every** failure: CDP-level errors (nodriver's `ProtocolException`, a plain `Exception` subclass) previously escaped the transport-only `except` tuple and propagated; they now log a warning and return `False`, matching the documented contract. The same transport-only `except` was on `current_url`, `page_title`, `page_source`, `get_cookies`, `get_user_agent` and `set_cookies`; each now honours its documented fallback for every failure.

## [1.13.0] - 2026-08-24

### Added

- **`LoginConfig.headless` and `gp <plugin> login --headless` / `--headful`** ([#148](https://github.com/stavxyz/graftpunk/issues/148)). Declarative login no longer hardcodes a visible browser window: set `headless: true` on sites that need no CAPTCHA/2FA, and override either way per invocation. The flags are offered only on declarative logins.
- **`--observe=full` now covers `gp <plugin> login`** ([#148](https://github.com/stavxyz/graftpunk/issues/148)). An observed login records a run under the plugin's session name — screenshot, page source, HAR with bodies and console logs on nodriver; HAR, console logs and an error screenshot on selenium — whether or not the login succeeds, and prints the run directory. Submitted credentials are scrubbed from the HAR before it is written (`graftpunk.observe.run.redact_har_entries`); session cookies are not, so treat the run as sensitive.

### Fixed

- **Declarative login typed into a detached DOM node and blamed the credentials** ([#148](https://github.com/stavxyz/graftpunk/issues/148)). On sites that re-render the login form after load, the element handle could be stale by the time keys were sent; the browser then refused to submit the empty required field and the CLI reported "Check your credentials". The nodriver engine now re-selects, clears, types, waits for the renderer, and reads the value back from the live DOM by selector, retrying up to 3 times and failing with an error that names the field. The CLI message is cause-neutral, a `Too Many Requests` page is reported as rate limiting rather than a login failure, and the failure-text / success-element warnings say what was measured.
- **Response-body capture on nodriver >= 0.50.1** ([#146](https://github.com/stavxyz/graftpunk/issues/146)). nodriver 0.50 renamed `Connection.send(..., _is_update)` to `_attach` and merges unknown kwargs into the CDP message, so the eager body fetch put `_is_update: true` on the wire and Chrome rejected every `Network.getResponseBody` with `-32600`; capture silently reverted to the eviction-limited late path. The eager fetch now passes `_is_update=True` only when the installed `send()` declares it (nodriver <= 0.48) and nothing otherwise. Measured on 0.50.3 with `gp --observe=full observe go https://www.python.org/`: 0 × `-32600`, 23 of 32 HAR entries with bodies.

## [1.12.0] - 2026-08-01

### Added

- **Workstation env file + `gp config` family** ([#142](https://github.com/stavxyz/graftpunk/pull/142)). Persist per-machine environment for `gp` in `~/.config/graftpunk/env`: static values (e.g. `GRAFTPUNK_BROWSER_EXECUTABLE_PATH`, a store name) inject at process bootstrap; `$(…)` command values (e.g. `$(op read "op://…")`) resolve lazily at the moment something actually needs them — login credential resolution, first access of an allowlisted settings field (via a bounded `LazySettings` proxy), or YAML plugin `${VAR}` header expansion. `gp --help` and unrelated commands never execute a command value. One precedence rule, owned by a single `lookup()`: real environment → workstation file (an empty-string value counts as unset at both tiers). Managed with git-config-style verbs: `gp config` (settings panel, unchanged), `show`, `path`, `list`, `get [--resolve]`, `set`, `unset`, `edit`. The file is created and kept `0600`; command strings live on disk, secrets never do; resolved values are memoized per-process only. New modules `graftpunk.paths` and `graftpunk.workstation_env`; library consumers get identical bootstrap behavior through `get_settings()` without importing the CLI. Design doc: `docs/rfcs/2026-07-28-workstation-env.md`.

### Fixed

- An unreadable or non-UTF-8 workstation env file degrades to "no file" with a warning instead of crashing every `gp` invocation; `gp config edit` handles multi-word `$EDITOR`/`$VISUAL` values (`code --wait`, `emacsclient -nw`) and re-asserts `0600` after replace-on-write editors.

## [1.11.0] - 2026-07-29

### Changed

- **BREAKING (install): browser automation moved to the `[browser]` extra.** `requestium`, `selenium`, `webdriver-manager`, `undetected-chromedriver`, `selenium-stealth`, `nodriver`, and `httpie` are no longer base dependencies. The base install is now lean and WASM-friendly, so `graftpunk` resolves under Pyodide / Cloudflare Python Workers, where the browser stack has no installable wheels (see [#121](https://github.com/stavxyz/graftpunk/issues/121)).

  **Migration — if you use live login, stealth driving, or any `gp <site> login` flow, install the extra:**

  ```bash
  pip install 'graftpunk[browser]'
  ```

  The `[nodriver]` and `[all]` extras and the `dev` dependency group already pull `graftpunk[browser]`, so those workflows are unchanged. A plain `pip install graftpunk` still gives you the full CLI and cached-session API replay (`load_session_for_api`, `load_session_for_api_from_bytes`); only launching a browser needs the extra. Accessing `graftpunk.BrowserSession` or `graftpunk.create_stealth_driver` without it now raises an `ImportError` naming the extra to install.

  Side benefit: base installs no longer pull `httpie`, taking its permanently-ignored PYSEC-2023-242 advisory out of the default dependency surface.

- `decrypt_data()` now raises `EncryptionError` (not a bare `ValueError`) when handed a malformed Fernet key — the likeliest failure for callers passing `key=` from a secret store.

### Added

- **`load_session_for_api_from_bytes(encrypted, *, key=None)`** — build a browser-free API session directly from encrypted session bytes, for callers that already hold the blob (e.g. a Cloudflare Worker reading it through an R2 binding) and cannot go through a storage backend or the browser stack. Decrypts, deserializes browser-free, and returns a `GraftpunkSession` with cookies, headers, header roles, and cached tokens. Distinguishes "cannot decrypt" (`EncryptionError` — fix the key) from "session unusable" (`SessionExpiredError` — log in again).
- Browser-only symbols (`BrowserSession`, `create_stealth_driver`) are now loaded lazily (PEP 562), so `import graftpunk` succeeds with no browser stack present.

## [1.10.0] - 2026-07-21

### Fixed

- **`gp <site>` plugin commands broken under typer>=0.26 (vendored Click)** — plugin subcommands failed to mount ("No such command") and, when reached, crashed during argument parsing or silently dropped option defaults. Root cause: graftpunk hand-built *external* `click.Option`/`click.Argument`/`click.Group` objects and handed them to Typer's runtime, which since typer 0.26 parses with its own vendored Click — a cross-implementation `Parameter`↔`Context` contract that does not hold. The CLI plugin layer is now **Typer-native**: a signature-synthesizing factory declares every parameter via `typer.Option`/`typer.Argument` so Typer builds them with its own Click; plugin groups are nested `typer.Typer` sub-apps mounted with `add_typer`. Works on typer 0.21 through latest (CI now runs a typer version matrix). See `docs/rfcs/2026-07-19-typer-native-plugin-commands.md`.

### Changed

- `typer` floor raised to `>=0.21` (the oldest CI-tested version); no upper bound.
- `PluginParamSpec.click_kwargs` is now a documented, closed contract (type/required/default/help/is_flag/show_default/envvar for options; type/required/default/nargs for arguments) interpreted into Typer-native parameters; unsupported keys raise `PluginError` at registration instead of being splatted into `click.Option`, including bool options that aren't declared as flags (`is_flag=True`) and `nargs` values other than `1`/`-1` (or `-1` combined with a `default`). Command-level `CommandSpec.click_kwargs` get the same closed, fail-loud treatment (help/short_help/hidden/deprecated/epilog).
- A group-segment name colliding with an existing command is now a registration-time `PluginError` (previously a `command_group_conflict` warning that silently mangled the group).
- Plugin command/`--help`/usage-error rendering now comes from Typer's standard pipeline (minor cosmetic differences; documented interface — names, options, arguments, types, defaults, behavior — unchanged).

## [1.9.1] - 2026-07-17

### Added

- **Automated PyPI release via GitHub Actions Trusted Publishing** — `.github/workflows/release.yml` publishes to PyPI over OIDC (no stored token) when a `vX.Y.Z` tag is pushed: it runs the tests, verifies the tag matches `pyproject.toml`, builds, publishes, and creates the GitHub release. A `workflow_dispatch` (`ref: vX.Y.Z`) re-runs the pipeline against an existing tag. Requires a one-time PyPI trusted-publisher config (see README "Releasing").

### Changed

- **`just release` now only validates + pushes the tag** — build, PyPI upload, and GitHub-release creation moved to CI (above). This removes the local PyPI credential requirement and runs the build/publish gate on a CI-pinned Python instead of the local interpreter. `just publish` remains as a manual token-based fallback.

### Fixed

- **`graftpunk.__version__` no longer drifts from the packaged version** — it is now derived from the installed package metadata (`importlib.metadata.version`) instead of a hardcoded literal in `__init__.py`. The 1.9.0 release shipped `__version__ == "1.8.2"` because that release was bumped by hand and the `__init__.py` literal was missed; `pyproject.toml` is now the single source of truth, so this class of mismatch is unrepresentable. `just bump` no longer edits `__init__.py`.

## [1.9.0] - 2026-07-16

### Added

- **`GRAFTPUNK_BROWSER_EXECUTABLE_PATH` setting** — point the nodriver login browser at a specific Chrome/Chromium binary (e.g. Chrome-for-Testing) on machines/CI without a system Chrome install. `BrowserSession` forwards it to the nodriver backend's `browser_executable_path` option. Refs #132.

### Fixed

- **Login engine couldn't navigate to an absolute `login_config.url` (login host != API `base_url`)** — the login URL was built as `f"{base_url}{login_url}"`, assuming `login_config.url` is a path to append. That produced a malformed URL for a plugin whose login form is on a different host than its API `base_url`. A shared `_resolve_url` helper now uses an absolute URL as-is (any scheme) and joins a path onto `base_url` otherwise — applied to the login URL in both the nodriver and selenium generators (including the timeout-error message), and to token-extraction page URLs, which had the same latent bug. Closes #132.

## [1.8.2] - 2026-05-05

### Fixed

- **Chrome subprocesses leaked as zombies under live python parents** — `NoDriverBackend._stop_async()` now awaits the Chrome subprocess after `browser.stop()` so the kernel can collect its exit status. Without this, nodriver's `Browser.stop()` sends SIGTERM but never `await`s `proc.wait()`, leaving Chrome as a `<defunct>` entry under the parent until that parent exits. In a docker container with `init: true`, docker-init can't help because the parent is alive — init only reaps processes whose parent has died. The fix adds a module-level `_reap_browser_process()` helper with SIGTERM→SIGKILL escalation (3s, then 1s) and a final warning log on kernel-level wedge. The reap runs in a `try/finally` so it always executes even when `browser.stop()` itself raises. Closes #127. See also #96 (complementary — covers abnormal-exit orphans).

## [1.8.1] - 2026-03-04

### Fixed

- **Intermittent nodriver startup failure in observe mode and token extraction** — `nodriver_start()` in `tokens.py` and `_setup_observe_session()` in `cli/main.py` now retry on "Failed to connect to browser" with back-off, matching the existing pattern in `NoDriverBackend._start_async()`. Also passes `sandbox=False` and `--test-type` browser args consistently across all nodriver launch sites.
- **`ty` type-check errors in encryption vault parsing** — added explicit `dict[str, str]` type narrowing for Supabase RPC `result.data` so `ty` can resolve the `.get()` overload

### Changed

- **Test suite runs 13x faster** (120s → 9s) via two improvements:
  - Patched login engine timing constants (`_ELEMENT_WAIT_TIMEOUT`, `_POST_SUBMIT_DELAY`, `_ELEMENT_RETRY_INTERVAL`) in tests via a shared `_fast_login_timings` fixture — eliminates 30s real-clock deadline loops and 3s real sleeps
  - Added `pytest-xdist` for parallel test execution (`-n auto` default in `addopts`)
- `_select_with_retry()` now uses `None` sentinel defaults resolved at call time (instead of definition time) so `monkeypatch.setattr` on module constants takes effect in tests

## [1.8.0] - 2026-02-19

### Added

- **`CommandResult.export()` Method**: Format and export command output programmatically without importing CLI internals (#112)
  - `result.export("json")` → `str` (formatted JSON text)
  - `result.export("pdf")` → `bytes` (raw PDF bytes)
  - `result.export("csv", "/tmp/data.csv")` → `Path` (file written)
  - Supports all format types: json, table, raw, csv, xlsx, pdf
  - View and column filtering via `views` parameter: `result.export("csv", views=("items:name,price",))`
  - Uses the same 3-level formatter hierarchy as the CLI (core → plugin-wide → per-command)
- **`binary` property on `OutputFormatter` protocol**: Formatters declare whether they produce binary output (xlsx, pdf) or text (json, table, raw, csv)

### Changed

- `execute_plugin_command()` accepts optional `plugin_formatters` keyword argument and threads them onto the returned `CommandResult`
- `GraftpunkClient._execute_command()` populates `_plugin_formatters` from `plugin.format_overrides` during result normalization

## [1.7.1] - 2026-02-18

### Fixed

- **Intermittent nodriver browser startup failure** — `_start_async()` now retries `uc.start()` up to 3 times with back-off when Chrome's CDP port isn't ready within nodriver's ~2.75s timeout (common on macOS under load)
- Sync `start()` now catches the bare `Exception` that nodriver raises on connection failure, wrapping it as `BrowserError` instead of letting it propagate unwrapped

## [1.7.0] - 2026-02-17

### Added

- **Export Utilities Module** (`graftpunk.export`): Shared helpers for data export
  - `flatten_dict()` — recursively flatten nested dicts with dot-delimited keys
  - `json_to_csv()` — convert JSON data to CSV files with ordered columns
  - `json_to_pdf()` — convert JSON data to PDF with optional vendor header and logo
  - `get_downloads_dir()` moved here from formatters and exported publicly

- **PDF Formatter**: Built-in `PdfFormatter` registered as `--format pdf`
  - Renders command output as styled PDF documents
  - Supports vendor headers with logo, company name, and document title
  - Falls back to JSON format when `fpdf2` is not installed

- **`--output`/`-o` Framework Flag**: Direct file output for any plugin command
  - Text formatters (json, table, raw, csv) capture output and write to the specified file path
  - File formatters (xlsx, pdf) use the provided path instead of auto-generating one in `gp-downloads/`
  - Added to all plugin commands automatically via the framework

- **Three-Level Format Override System**: Plugin-customizable output formatting
  - Core formatters (built-in + entry points) as the base layer
  - Plugin-wide overrides via `SitePlugin.format_overrides` class variable
  - Per-command overrides via `CommandResult.format_overrides` field
  - `--format` accepts any string, allowing plugins to register custom format names not known to core

### Changed

- `get_downloads_dir()` moved from `graftpunk.formatters` to `graftpunk.export` (re-exported for backwards compatibility)
- Extracted `_write_to_file` and `_resolve_output_filepath` DRY helpers, eliminating duplicated file-output boilerplate across 6 formatters
- `format_overrides` typed as `dict[str, OutputFormatter]` on `CommandResult`, `SitePlugin`, and `format_output` (was `dict[str, Any]`)

### Fixed

- No-op `max(y, y+2)` in PDF export corrected to unconditional `y+2`
- Missing logo path in PDF export now logs a warning instead of silently skipping
- Storage config tests no longer leak `GRAFTPUNK_S3_*` and `GRAFTPUNK_STORAGE_BACKEND` env vars from the shell

## [1.6.1] - 2026-02-13

### Fixed

- **Column filter applied to single-dict views** — `_resolve_view_data` now applies `ColumnFilter` to single-dict data, preventing excluded keys from rendering as JSON blobs
- **Required CLI params now enforced** — `PluginParamSpec.option()` and `.argument()` no longer pass `default=None` to Click when `required=True`, so Click properly raises `MissingParameter` (#61)
- **Unknown format_type raises ValueError** — `format_output()` now raises instead of silently falling back to JSON, making typos in `format_hint` visible to plugin authors (#58)
- **X-CSRF-TOKEN excluded from header profiles and sessions** — Ephemeral WAF sensor blobs no longer leak into XHR header roles or API sessions (#66)

## [1.6.0] - 2026-02-13

### Added

- **Multi-View Rendering**: Commands can define multiple named views on their response data, rendered as separate sections (#80)
  - **TableFormatter**: Multiple views render as Rich tables with titled Rule section headers; single views render clean without headers
  - **XlsxFormatter** (new): Writes `.xlsx` files with one worksheet per view, bold headers, and auto-sized columns. Files saved to `GP_DOWNLOADS_DIR` (default: `./gp-downloads/`)
  - **CsvFormatter**: Warns when multiple views exist and renders the default view; suggests `--view` to select
  - **`--view` CLI option**: Select specific views and columns — `--view items`, `--view items:id,name`. Repeatable for multiple views
  - **`OutputConfig.filter_views()`**: Filter views by name with optional per-view column overrides
  - **`_resolve_view_data()` helper**: Shared extraction+filtering for consistent behavior across all formatters

### Changed

- `OutputConfig.views`, `ColumnFilter.columns`, and `ViewConfig.display` fields changed from `list` to `tuple` on frozen dataclasses, preventing post-construction mutation
- `get_downloads_dir()` now resolves paths to absolute via `Path.resolve()` for deterministic behavior regardless of working directory
- `CommandResult.format_hint` Literal type now includes `"xlsx"`

## [1.5.0] - 2026-02-11

### Fixed

- **`--format` flag now overrides `format_hint`**: When a user explicitly passes `--format`/`-f` on the command line, the plugin's `CommandResult.format_hint` is ignored so the user's choice always wins (#94)

### Added

- **Session Storage Location Display**: `gp session list` and `gp session show` display where each session is stored (#97)
  - Two new columns: Backend (`local`, `s3`, `r2`, `supabase`) and Location (`~/.config/...`, `s3://bucket`, etc.)
  - Per-session tracking via `storage_backend`/`storage_location` in `metadata.json`
  - `--storage-backend` flag on `gp session list`, `show`, and `clear` for querying specific backends
  - S3 backend self-identifies as `r2` when endpoint is Cloudflare R2
  - Backward compatible: old sessions display `—` until next save

- **HTTP Request Header Roles** (`--role`): Set browser header roles on `gp http` commands (#92)
  - Built-in roles: `navigation`, `xhr`, `form` — registered via `register_role()`. CLI accepts `navigate` as shorthand for `navigation`
  - Plugin-defined custom roles: plugins can declare a `header_roles` dict with arbitrary names
  - `--role <name>` dispatches via `request_with_role()` for any role name
  - Replaces manual multi-header overrides with a single flag

## [1.4.0] - 2026-02-08

### Added

- **First-Class Python API** (`GraftpunkClient`): Programmatic access to plugin commands (#90)
  - `GraftpunkClient` — stateful, context-manager-friendly client that wraps a single plugin
  - Attribute-based dispatch: `client.invoice.list(status="OPEN")`
  - String dispatch: `client.execute("invoice", "list", status="OPEN")`
  - Lazy session loading, token injection, 403 retry, and session persistence — same pipeline as the CLI
  - Exported from top-level package: `from graftpunk import GraftpunkClient`

- **Shared Plugin Discovery API**: `discover_all_plugins()` and `get_plugin()` in `graftpunk.plugins`
  - Unified plugin lookup across entry points, YAML files, and Python files
  - Cached discovery with `lru_cache` (call `discover_all_plugins.cache_clear()` to force refresh)
  - Used by both the CLI and `GraftpunkClient`

- **Shared Execution Core**: `execute_plugin_command()` in `graftpunk.client`
  - Handles retry/rate-limit and `CommandResult` normalization
  - CLI callback delegates to this function instead of maintaining its own execution logic

### Changed

- CLI plugin registration now delegates to `discover_all_plugins()` instead of calling individual discovery functions directly
- Retry and rate-limit logic unified into `_run_handler_with_limits()` — single implementation shared by both the CLI and Python API paths
- `close()` on `GraftpunkClient` wraps session persistence in try-except so failures don't prevent plugin teardown

## [1.3.0] - 2026-02-07

### Added

- **S3-Compatible Storage Backend**: Session persistence on S3-compatible object storage
  - Supports Cloudflare R2 (zero egress fees), AWS S3, MinIO, and any S3-compatible service
  - Retry logic with exponential backoff and jitter for transient failures
  - Region='auto' handling for Cloudflare R2
  - Install with: `pip install graftpunk[s3]`

- **Structured Output System**: OutputConfig for declarative table/CSV formatting
  - `OutputConfig` dataclass with named views, column definitions, and default view selection
  - OutputConfig support in YAML plugins via `output:` block
  - `output_config` field on `CommandResult` for plugin-controlled formatting

- **Multi-Step Login Support**: Identifier-first authentication flows (#77)
  - `LoginStep` dataclass for defining individual steps in a login flow
  - `LoginConfig.steps` list replaces flat fields for multi-step scenarios
  - Nodriver and Selenium engines both support multi-step flows
  - YAML `login.steps:` block with the same capabilities

- **Resilient Element Selection**: Retry and wait_for in login engine (#67)
  - `wait_for` field on `LoginConfig` for post-login element waiting
  - `_select_with_retry` deadline-based retry helper for nodriver's `tab.select()`
  - Handles `ProtocolException` during page transitions

- **`--no-session` Flag**: Run `observe` and `http` commands without a pre-existing session (#54, #56)

- **First-Class CSV Output Formatter**: Dedicated `CsvFormatter` with fallback handling (#57)

- **Click Kwargs Passthrough**: Fine-grained plugin parameter control via `click_kwargs` on `CommandSpec` (#72)

- **Interactive Observe Mode**: Record browser sessions interactively with `gp observe interactive`
  - Opens authenticated browser at a URL, records all network traffic while you click around
  - Press Ctrl+C to stop and save HAR files, screenshots, page source, and console logs
  - Also available as `gp observe go --interactive` (`-i`) flag on existing command
  - Captures response bodies eagerly via CDP `LoadingFinished` events (prevents buffer eviction)

- **EAFP Token Injection**: Optimistic token injection with 403 retry
  - Cached tokens are injected even when expired — if the server rejects with 403, tokens are refreshed and the request retried once
  - Tokens extracted during login are persisted through session serialization (pickle roundtrip)
  - `update_session_cookies()` preserves the token cache when saving session changes

- **Token Polling with Retry**: Robust token extraction from dynamic pages
  - `_poll_for_tokens()` checks page content up to 6 times (0.5s intervals) for token patterns
  - Handles bot challenges (e.g., Akamai) and lazy-rendered pages without wasting time on fast ones
  - Checks content first, sleeps only between retries (no unconditional initial delay)

- **Login-Time Token Extraction**: Extract tokens during login without a separate browser launch
  - Nodriver and Selenium login engines extract tokens from the already-open browser session
  - Cookie-based and page-based token sources both supported during login
  - `_build_token_cache()` shared helper eliminates duplication between backends

- **Eager CDP Body Fetching**: Response bodies captured before Chrome evicts them
  - `NodriverCaptureBackend` listens for `Network.LoadingFinished` CDP events
  - Bodies fetched immediately via `Network.getResponseBody` and streamed to disk
  - Async capture with `start_capture_async()` / `stop_capture_async()` for interactive mode

- **Network Debug Flag**: `--network-debug` CLI flag for wire-level HTTP tracing
  - Enables `HTTPConnection.debuglevel = 1` for raw HTTP traffic on stderr
  - Sets `urllib3`, `httpx`, and `httpcore` loggers to DEBUG level
  - Independent of `-v`/`-vv` verbosity — can be combined with any log level

- **Bot-Detection Cookie Filtering**: Skip WAF tracking cookies when injecting into browsers
  - Akamai cookies (`bm_*`, `ak_bmsc`, `_abck`) filtered by default in `inject_cookies_to_nodriver()`
  - Prevents `ERR_HTTP2_PROTOCOL_ERROR` caused by stale bot-classification state
  - Opt-out via `skip_bot_cookies=False` parameter
  - Extensible to Cloudflare, Imperva, PerimeterX, and DataDome WAFs

- **Plugin Interface v1**: Full command framework for building CLI tools on top of authenticated sessions
  - `SitePlugin` base class with `@command` decorator for defining CLI commands
  - `CommandContext` dataclass injected into handlers with session, plugin metadata, and observability
  - `CommandSpec` with per-command `timeout`, `max_retries`, and `rate_limit` enforcement
  - `CommandResult` with `format_hint` for plugin-controlled output formatting
  - `CommandError` exception for user-facing error messages without tracebacks
  - `CLIPluginProtocol` runtime-checkable structural typing contract
  - `api_version` field for forward-compatible plugin interface negotiation
  - Command groups with `parent=` nesting via `@command` decorator on classes
  - `setup()` / `teardown()` lifecycle hooks
  - Async handler auto-detection (with deprecation warning for v1)

- **Declarative Login Engine**: Define login flows with CSS selectors instead of writing automation code
  - `LoginConfig` frozen dataclass: `url`, `fields`, `submit`, `failure`, `success`
  - Auto-generates `gp <plugin> login` CLI command from declarative config
  - Generates sync (Selenium) or async (NoDriver) login functions automatically
  - Supports flat class attributes (`login_url`, `login_fields`, etc.) for ergonomics
  - YAML `login:` block with the same capabilities
  - Customizable credential environment variable names per plugin
  - Interactive prompts with masked input for password fields

- **Browser Header Replay**: Requests look like they came from Chrome, not Python
  - `GraftpunkSession` subclass of `requests.Session` with browser header roles
  - Captures real browser headers during login via CDP network events
  - Classifies headers into navigation, XHR, and form roles
  - `load_session_for_api()` returns `GraftpunkSession` with browser header roles
  - Brotli support (`Accept-Encoding: gzip, deflate, br`)
  - **Request-type methods** (#50): `xhr()`, `navigate()`, `form_submit()` for explicit role control
    - Each method applies the correct captured (or registered fallback) headers for that request type
    - `referer` kwarg resolves paths against `gp_base_url` (e.g., `referer="/invoice/list"`)
    - Caller-supplied headers override role headers
    - Eliminates boilerplate: plugins no longer need to build request headers manually

- **Token and CSRF Support**: Declarative token extraction and auto-injection
  - `Token` and `TokenConfig` types for declarative token definitions
  - Extract tokens from cookies, response headers, or page content (regex)
  - Auto-inject tokens into request headers before each command
  - Auto-retry with fresh tokens on 403 responses
  - YAML `tokens:` block for declarative configuration

- **Ad-hoc HTTP Requests** (`gp http`): Make authenticated requests without writing a plugin
  - Supports all HTTP methods: `get`, `post`, `put`, `patch`, `delete`, `head`, `options`
  - Uses `GraftpunkSession` with full browser header replay

- **Observability System**: Capture browser activity for debugging and auditing
  - `ObservabilityContext` with `mark()`, `screenshot()`, `log()` methods
  - `gp observe go` — open authenticated browser and capture network traffic
  - Full network capture with request/response bodies and console logs
  - HAR file generation with disk-streamed body support
  - Screenshot capture (Selenium backend)
  - `NoOpObservabilityContext` for zero-overhead when disabled
  - `gp observe list/show/clean` for managing captured data
  - `--observe full` flag on all commands

- **Session Management Redesign**: All session commands under `gp session` subgroup
  - `gp session list` / `show` / `clear` / `export` (moved from top-level)
  - `gp session use <name>` / `gp session unset` — active session context
  - Session name validation (no dots allowed)
  - Plugin site_name resolution as alias in session commands
  - Session persistence after commands (`saves_session` flag, `update_session_cookies()`)

- **Plugin Discovery Improvements**
  - Python file auto-discovery from `~/.config/graftpunk/plugins/*.py`
  - Plugin collision detection (fail-fast on duplicate `site_name`)
  - `site_name` auto-inference from `base_url` domain or YAML filename
  - Partial success: valid plugins load even when others fail
  - Unified error collection across all discovery sources

- **Example Plugins and Templates**
  - `httpbin.yaml` — YAML plugin for httpbin.org (no auth, demonstrates all YAML features)
  - `quotes.py` — Python/Selenium plugin with declarative login (test site)
  - `hackernews.py` — Python/NoDriver plugin with declarative login (real site)
  - `yaml_template.yaml` and `python_template.py` starter templates

- **CLI Improvements**
  - `GraftpunkApp` custom Typer subclass with plugin group registration
  - Rich help formatting for all plugin commands (`TyperCommand` / `TyperGroup`)
  - Default log verbosity reduced to WARNING; `-v` (info), `-vv` (debug) flags
  - `GRAFTPUNK_LOG_FORMAT` env var and `--log-format` CLI flag
  - Clean error output for unknown commands
  - `gp_console` module for centralized Rich terminal output with Status spinners
  - Auto-introspection of Python plugin method parameters for CLI argument generation

### Changed

- **Supabase storage backend** refactored to pure file-based storage. No longer uses `session_cache` database table. Users with existing Supabase sessions will need to re-login.
- All storage backends now use the same file-pair pattern: `{session_name}/session.pickle` + `metadata.json`
- `LoginConfig` restructured to use `steps` list for multi-step login flows
- `format_output` writes to stdout instead of stderr (#60)
- Example plugins updated for steps-based LoginConfig API
- `PluginConfig` is now a frozen dataclass constructed via `build_plugin_config()` factory
- Login configuration extracted into `LoginConfig` frozen dataclass (replaces 5 flat fields)
- `get_commands()` returns `list[CommandSpec]` instead of `dict`
- `requires_session` flag replaces `session_name=""` hack for sessionless commands
- All metadata types are frozen dataclasses (`CommandMetadata`, `PluginParamSpec`, discovery errors)
- `BrowserSession` supports context manager protocol (sync and async)
- `inject_cookies_to_nodriver()` returns `tuple[int, int]` (injected, skipped) instead of `int`; callers can now see how many cookies were filtered
- `inject_cookies_to_nodriver()` logs a warning when all cookies are filtered (indicates the session may not work)
- `GraftpunkSession.__init__` now accepts `base_url` keyword argument for Referer path resolution
- `_detect_role()` classifies non-GET/POST methods as XHR (was: navigation); registered role headers used as fallback when a captured role is missing
- Chrome sandbox disabled by default for NoDriver; `--no-sandbox` warning suppressed
- Auto-detect Chrome version for matching ChromeDriver

### Fixed

- Graceful observe shutdown on Ctrl+C and browser close (#69)
- CDP eager body fetch failures (#64)
- Session headers contaminating GET requests (#65)
- `load_session_for_api` overwrites browser UA with python-requests default (#52)
- Silent failures in S3 storage replaced with explicit `StorageError` exceptions
- **Browser identity header leak** (#49): `GraftpunkSession` now separates browser identity headers (User-Agent, sec-ch-ua, Accept-Language, Accept-Encoding, etc.) from request-type headers (Accept, Sec-Fetch-*). Identity headers are set as session defaults at init, preventing `python-requests` User-Agent from ever reaching the wire when roles exist. When a detected role wasn't captured during login, registered role headers are used as fallback instead of silently applying no headers. `_detect_role()` now correctly classifies DELETE/PUT/PATCH/HEAD/OPTIONS as XHR per HTML spec §4.10.18.6 (forms only support GET and POST).
- Nested plugin subcommand groups (e.g. `gp bek invoice`) now use `TyperGroup` instead of plain `click.Group`, so `--help` output gets the same rich formatting as top-level commands

## [1.2.1] - 2026-01-28

### Changed

- **NoDriver now included by default**: `pip install graftpunk` includes both Selenium and NoDriver backends
- The `[nodriver]` extra is kept for backwards compatibility but is now a no-op

## [1.2.0] - 2026-01-27

### Added

- **Browser Abstraction Layer**: Pluggable browser backend architecture
  - `BrowserBackend` Protocol defining the browser automation interface
  - `SeleniumBackend` wrapping existing stealth stack (undetected-chromedriver + selenium-stealth)
  - `NoDriverBackend` for CDP-direct automation without WebDriver binary detection
  - Backend factory with `get_backend()`, `list_backends()`, `register_backend()`
  - `Cookie` TypedDict for type-safe cookie handling across backends

- **NoDriver Integration**: CDP-direct browser automation
  - Eliminates WebDriver binary detection vector
  - Async-to-sync bridging for consistent API
  - Better anti-detection for enterprise-protected sites

### Changed

- `BrowserSession` now accepts `backend` parameter ("selenium", "nodriver", or "legacy")
- Exported `BrowserBackend`, `get_backend`, `list_backends`, `register_backend` from main package

## [1.1.0] - 2026-01-25

### Added

- **HAR File Import** (`gp import-har`): Generate plugins from browser network captures
  - Parse HAR files exported from browser dev tools
  - Detect authentication flows (login forms, OAuth, redirects, session cookies)
  - Discover API endpoints from captured JSON responses
  - Generate plugins in Python or YAML format
  - Dry-run mode for previewing output without writing files

- **YAML Plugin System**: Declarative command definitions without Python
  - Define site commands in simple YAML files
  - Automatic parameter handling with type validation
  - Environment variable expansion for secrets
  - JMESPath support for response extraction
  - Drop-in plugin discovery from `~/.config/graftpunk/plugins/`

- **Python Plugin System**: Extensible command architecture
  - `@command` decorator for custom command logic
  - `SitePlugin` base class with session integration
  - Dynamic command registration at CLI startup
  - Output formatting (`--format json|table|raw`) on all plugin commands

### Changed

- Extracted keepalive subcommands to dedicated module for cleaner architecture
- Improved plugin discovery with structured error reporting

## [1.0.0] - 2026-01-23

### Added

- Initial release of graftpunk as a standalone package
- Encrypted session persistence with Fernet (AES-128-CBC + HMAC-SHA256)
- Stealth browser automation with undetected-chromedriver and selenium-stealth
- Pluggable storage backends: local filesystem, Supabase, S3
- Session keepalive daemon with customizable handlers
- Plugin architecture via Python entry points
- MFA support: TOTP generation, reCAPTCHA detection, magic link extraction
- CLI interface for session management
- Full type annotations with py.typed marker

### Storage Backends

- **Local**: File-based storage with configurable directory
- **Supabase**: Cloud storage with Vault integration for key management
- **S3**: AWS S3 bucket storage

### Security

- Fernet encryption for all session data
- SHA-256 checksum validation before deserialization
- 0600 permissions on local key files
- Supabase Vault integration for cloud key storage
