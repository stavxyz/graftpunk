# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Typed token-extraction exceptions** ([#131](https://github.com/stavxyz/graftpunk/issues/131)) — `TokenExtractionError` with subtypes `SessionInvalidatedError` (a required cookie is gone; re-login) and `TokenPatternMismatchError` (the header or pattern no longer matches; fix the Token config), exported from `graftpunk`. Browser-mode failures raise the base class, since the cause is not distinguishable from an empty result. All three also subclass `ValueError` (permanently), so existing `except ValueError` code keeps working; `gp` now tells you whether to re-login or fix the config.
- **`--format` help lists a plugin's own formats** ([#116](https://github.com/stavxyz/graftpunk/issues/116)) — a plugin that registers `format_overrides` (e.g. `html`) now sees them in every command's `--format` help: `Output format (built-in: …; plugin: html)`.
- **Multi-account sessions** ([#151](https://github.com/stavxyz/graftpunk/issues/151)) — sessions are keyed by plugin and account (`myshop@alice`); `gp <site> login` derives the account label from the login identifier (`--as` overrides) and records the identifier in metadata (`gp session list` shows it). Commands resolve one operating session per invocation (`--session` > `GRAFTPUNK_SESSION` > `.gp-session` > sole cached session) and refuse with `AmbiguousSessionError` when several are cached and none is picked; the load-resolved name keys every write-back. The env and file tiers are base-scoped — a pin for another plugin is ignored, and resolution continues for the plugin you are running — while an explicit `--session` always wins. The label derives only from identifier-shaped fields (username/email/login); a one-time code, PIN, passphrase or security answer is never turned into a session label, and when nothing qualifies the session keeps its bare name (`--as` names it). `GraftpunkClient` gains `session=` and ignores ambient shell state by design. Logging in over a slot recorded for a different account warns. Existing bare-name sessions keep working; no migration.

### Changed

- **`GraftpunkClient` resolves its session on first use, not at construction** ([#181](https://github.com/stavxyz/graftpunk/issues/181)) — construction performs no session-storage I/O and never raises for session *state* reasons; `AmbiguousSessionError` (several accounts cached, unpinned) and `SessionNotFoundError` surface from the first `execute()` — catch `AmbiguousSessionError` next to `SessionNotFoundError`. Lazy resolution makes the error *catchable*, not handled: a wrapper that only names `SessionNotFoundError` still lets ambiguity through, but now from inside the call it already wraps, so the fix is one `except` clause rather than restructuring construction. "Pick one" is also the right instruction — logging in again would add a third slot, not resolve the ambiguity. Plugin discovery still runs at construction (`PluginError` is unchanged), and an illegal pin string (`session="MyShop@Alice"`, `session="../../x"`) still raises `ValueError` there — pure policy, no I/O, so a typo fails where it was typed. Pin with `GraftpunkClient("myshop", session="myshop@alice")` to skip resolution entirely: a **labelled** pin now lists nowhere at all, hit or miss, on both the client and the CLI (plugin commands and `gp http`: `--session myshop@alice`, `GRAFTPUNK_SESSION`) — the bare-base fallback matches on the base, which a labelled miss can never reach, so resolving one could only pay for a useless listing. A **bare** pin still resolves at load time, listing once on the miss path, exactly as before. One consequence to know about: a command that overrides `requires_session=True` on a `requires_session=False` plugin now takes the same *unpinned* semantics as everything else — with a legacy bare `myshop` cached alongside `myshop@alice` it raises `AmbiguousSessionError` where it used to take the bare slot outright. Pin the client (`session="myshop"`) to keep the old behaviour.

- **`load_session_for_api(name)` and `SitePlugin.get_session()` treat a bare name as a base name** ([#182](https://github.com/stavxyz/graftpunk/issues/182), [#151](https://github.com/stavxyz/graftpunk/issues/151)) — they resolve a bare base name to its single cached account when nothing is cached under that name exactly — or raise `AmbiguousSessionError` naming the candidates; the slot actually loaded is the one written back to. A slot cached under the bare name itself still wins outright (no listing, no ambiguity). Library consumers holding a bare name keep working after labelled logins, but the exception surface changed: code catching `SessionNotFoundError` should also catch `AmbiguousSessionError`, and a name that cannot be a session name at all (`"MyShop@Alice"`, `"../../x"`) now raises `ValueError` before anything is loaded or listed, where it used to reach storage and come back as `SessionNotFoundError`. New `load_session_for_api_resolved(name, *, resolve=True)` returns `(session, loaded_name)` — **use it instead of `load_session_for_api` whenever you write the session back**, since `update_session_cookies(session, name)` uses its name literally and a bare name would refresh a slot that does not exist. `resolve=False` on either function is exact-only, for callers that already resolved.

- **`session` is now a reserved plugin option name** ([#151](https://github.com/stavxyz/graftpunk/issues/151)) — every generated command carries the built-in `--session` for multi-account resolution. **Breaking for plugins** that declared their own `session` parameter on a command: registration now fails with `PluginError` ("reserved parameter name") — rename the plugin parameter.

- **`@command` kebab-cases function command names by default and accepts `name=`** ([#147](https://github.com/stavxyz/graftpunk/issues/147)) — a method `by_parcel` is now `gp <site> by-parcel`, matching what groups already did; auto-discovered group methods follow the same rule. **Behaviour change for the CLI (minor version bump)** — a multi-word function command that was typed with underscores is now hyphenated; pin the old spelling with `@command(name="by_parcel")` if you need it. `GraftpunkClient` is unaffected: `client.by_parcel()`, `execute("by_parcel")` and `execute("by-parcel")` all resolve. Two Python names that kebab-case to the same CLI name now raise `PluginError` in the client as they already did in the CLI.

- **`ty` type-checking is blocking in CI again, pinned to 0.0.75** ([#130](https://github.com/stavxyz/graftpunk/issues/130)) — the same pinned command runs in `just lint` and `CONTRIBUTING.md`. The nodriver "false positives" were ty failing to read `nodriver/cdp/network.py` at all (the generated file carries a Latin-1 byte), so a partial stub under `typings/` (not shipped in the wheel or sdist) covers that one module and the twelve `type: ignore` comments that had been papering over it are gone; an unused `type: ignore` is now itself an error. The remaining diagnostics were real typing gaps and are fixed in code.

### Fixed

- **Status messages treat names as data** ([#166](https://github.com/stavxyz/graftpunk/issues/166)) — session names, domains, storage backends, config keys, HAR paths and error text are escaped before Rich renders them, so a value containing a bracket sequence no longer raises `MarkupError` or disappears from `gp session list/show/export/clear/use`, `gp keepalive`, `gp config` and `gp import-har` output (including the generated-code preview of `--dry-run`).

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
