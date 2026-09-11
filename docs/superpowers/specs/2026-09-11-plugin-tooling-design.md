---
type: spec
---

# Plugin tooling: observe digest, raw captures, and the plugin scaffold

**Date:** 2026-09-11
**Status:** design, awaiting approval
**Series:** part B of four (B tooling, A session primitives, C docs refresh, D the plugin-development skill). Each part is its own spec, plan, and PR. This one comes first because A, C, and D all depend on the commands it adds.

## Problem

Building a new graftpunk plugin today starts from a recording and ends with a
hand-built project, and the two starting points the framework offers for the
middle are stale:

- `gp import-har` (`src/graftpunk/cli/main.py:727` (`@app.command("import-har")`))
  generates handlers whose first parameter is a raw `requests.Session`
  (`src/graftpunk/har/generator.py:91` (`    params = ["self", "session: requests.Session"]`)),
  a shape the framework has not accepted since command handlers started
  receiving a `CommandContext`. It surfaces a detected login only as a comment
  and never emits a `LoginConfig`, a `TokenConfig`, or output views. Its
  auth-flow heuristic (`src/graftpunk/har/analyzer.py:186` (`def detect_auth_flow`))
  matched 46 asset loads as a "form" login on a real 731-entry capture on
  2026-09-10.
- `examples/templates/python_template.py` (last changed 2026-02-03) predates
  multi-account sessions, header roles, `cache_login_session`, and output views.

A recording is the right primary source, but nothing makes it readable. The
capture probed on 2026-09-10 was a 36 MB `network.har` with 731 entries, of
which 183 JSON responses across 49 distinct endpoints were the useful part, plus
a handful of custom per-site request headers a plugin has to reproduce. Neither
a developer nor an agent can read that file as it stands, so each plugin has
started with ad-hoc scripts over the HAR.

What a run holds, from the code: `network.har` written by
`src/graftpunk/observe/storage.py:126` (`    def write_har(self, entries: list[dict[str, Any]]) -> None:`)
under `~/.local/share/graftpunk/observe/<session>/<run>/`
(`src/graftpunk/observe/context.py:100` (`OBSERVE_BASE_DIR = Path.home() / ".local" / "share" / "graftpunk" / "observe"`)),
with each response carrying either an inline `body` or a `_bodyFile` pointer
into `bodies/` (`src/graftpunk/observe/capture.py:122` (`        response["_bodyFile"] = f"bodies/{body_filename}"`)),
which the existing parser already resolves
(`src/graftpunk/har/parser.py:171` (`def _parse_response(response_data: dict[str, Any], base_dir: Path | None = None) -> HARResponse:`)).
An interactive run stopped with Ctrl+C also holds `page-source.html` and a
screenshot; one closed by shutting the browser holds network data only.

## Design

Three commands and one analysis module. The module turns a run into a
`RunDigest`; the commands render it, extract from it, and scaffold from it.

### The analysis module: `src/graftpunk/observe/digest.py`

```python
@dataclass(frozen=True)
class Endpoint:
    host: str
    template: str                 # "/orders/{id}/items"
    methods: tuple[str, ...]
    count: int
    statuses: tuple[int, ...]
    content_type: str             # response, primary
    query_params: dict[str, str]  # name -> observed type ("int", "str", "bool", "list")
    body_params: dict[str, str]   # JSON or form fields on POST/PUT/PATCH, name -> type
    body_kind: str                # "json", "form", "none"
    response_shape: str           # a depth-limited type sketch, see below
    custom_headers: tuple[str, ...]  # request header names not in the standard set
    examples: tuple[str, ...]     # up to 3 concrete paths behind the template

@dataclass(frozen=True)
class LoginObservation:
    order: int
    method: str
    url: str                      # query stripped
    status: int
    kind: str                     # "form_page", "credential_post", "redirect", "set_cookie", "auth_api"
    fields: tuple[str, ...]       # form field NAMES on a credential post; never values

@dataclass(frozen=True)
class LoginForm:                  # from page-source.html or any HTML response
    action: str
    method: str
    fields: dict[str, str]        # credential role -> CSS selector ("username", "password", other names verbatim)
    submit: str | None            # selector of the submit control
    hidden: tuple[str, ...]       # hidden input names (token candidates)
    source: str                   # which document it came from

@dataclass(frozen=True)
class TokenCandidate:
    kind: str                     # "header", "meta", "hidden_input", "cookie"
    name: str
    seen_on: tuple[str, ...]      # endpoint templates that sent it (headers) or pages that carried it

@dataclass(frozen=True)
class RunDigest:
    session: str
    run_id: str
    primary_host: str
    hosts: dict[str, int]         # host -> request count, all hosts
    endpoints: tuple[Endpoint, ...]
    login: tuple[LoginObservation, ...]
    login_forms: tuple[LoginForm, ...]
    tokens: tuple[TokenCandidate, ...]
    cookies: tuple[str, ...]      # names set during the run, primary host
    dropped: dict[str, int]       # why entries were left out: "static", "third_party", "error"

def digest_run(run_dir: Path, *, all_hosts: bool = False, limit: int = 60) -> RunDigest
def render_markdown(d: RunDigest) -> str
def render_json(d: RunDigest) -> str
```

Rules the module applies:

- **Parsing** reuses `parse_har_file` with the run directory as `base_dir`, so
  streamed bodies resolve. Bodies over 256 KB are sampled (first 64 KB) for
  shape only; nothing about a body is ever printed verbatim.
- **Primary host** is the host that answered the most non-static requests;
  hosts sharing its registrable domain count as primary. Other hosts are
  reported by count only unless `--all-hosts`.
- **Static and tracking exclusion** reuses the analyzer's exclusion patterns
  (the `EXCLUDE_PATTERNS` list that `_should_exclude` at
  `src/graftpunk/har/analyzer.py:269` (`def _should_exclude(url: str) -> bool:`) compiles),
  extended with a content-type rule: image, font, CSS, and JavaScript
  responses are static regardless of URL.
- **Path templates** collapse a segment to `{id}` when it is all digits, a
  UUID, 16 or more hex characters, or base64-like and 20 or more characters.
  A segment that appears with more than 8 distinct values across the run is
  also collapsed. The parameter name is the singular of the preceding segment
  when there is one (`orders/123` becomes `orders/{order_id}`), else `{id}`.
- **Types** are observed, not declared: a query value that always parses as an
  integer is `int`, `true`/`false` is `bool`, repeated keys are `list`, else
  `str`. Values are never retained.
- **Response shape** is a type sketch of the JSON body: object keys with their
  value types, arrays as `[{...}]` using the first element, depth limited to 3
  and 12 keys per object with a `...` marker. Strings, numbers, and booleans
  render as their type name only.
- **Custom headers** are request header names on primary-host calls that are
  not in a standard set (the RFC headers plus `sec-ch-*`, `sec-fetch-*`,
  `user-agent`, `referer`, `origin`, `accept*`, `content-*`, `cookie`,
  `x-requested-with`). Names are reported; values are never reported.
- **Login observations** are, in capture order: GETs of HTML pages that
  contain a password input (`form_page`), POSTs whose form or JSON body has a
  password-like field name (`credential_post`, field names listed, values
  dropped), 30x responses in the twenty entries after a credential post
  (`redirect`), responses in that window that set cookies (`set_cookie`, cookie
  names only), and calls to paths matching the analyzer's auth patterns
  (`auth_api`). No entry outside that shape is a login observation, which is
  what stops asset loads being reported as a login.
- **Login forms** are parsed from `page-source.html` and from every HTML
  response of the primary host: each `<form>` containing an `input[type=password]`
  yields a `LoginForm` with a CSS selector per input (prefer `#id`, else
  `form[action=...] input[name=...]`), the role guessed from type and name
  (`password` for the password input; `username` for `type=email`, or a name
  containing `user`, `email`, `login`, `account`; anything else keeps its
  name), the submit control's selector, and hidden input names.
- **Token candidates**: request headers whose name contains `csrf`, `xsrf`, or
  `token`; `<meta name=...>` tags in HTML bodies with the same substrings;
  hidden inputs named like `_token`, `csrf*`, `authenticity_token`; cookies
  with those substrings. Each candidate lists where it was seen so the reader
  can pair a header with its source.
- **Redaction** is by construction: the digest holds names, types, counts,
  templates, and shapes, and no header value, cookie value, query value, body
  value, or credential. The markdown renderer therefore has nothing to redact.
- **Size**: `limit` caps the endpoint list (default 60, ordered by count, JSON
  first, then other content types); the markdown for the 2026-09-10 capture
  should come out under 400 lines. The JSON form is complete regardless of
  `limit` ordering but honours the cap.

### `gp observe digest <session> [<run>] [--json] [--all-hosts] [--limit N] [--output PATH]`

Resolves the run like `gp observe show`
(`src/graftpunk/cli/main.py:277` (`@observe_app.command("show")`)): session
through `session_dirname`
(`src/graftpunk/observe/storage.py:20` (`def session_dirname(session_name: str) -> str:`)),
run defaulting to the newest. Prints the markdown digest, or JSON with
`--json`; `--output` writes to a file instead. Missing run or missing
`network.har` is a one-line error and exit 1. Markdown sections, in order:
Summary (hosts, counts, dropped), Login (observations then forms), Tokens,
Cookies, Endpoints (one block per endpoint), Other hosts.

### `gp observe fixtures <session> [<run>] --match "<METHOD> <template>" [--out DIR] [--limit N] [--allow-tracked]`

Writes captured response bodies exactly as recorded, for the developer or the
agent to derive committed test fixtures from by hand. `--match` takes the
`METHOD template` line as printed by the digest (repeatable); the template
matches the digest's own templating, and a plain path or a glob is accepted.
For each matching entry, up to `--limit` (default 5) per template, it writes
`<out>/<method>_<slug>[_<n>].<ext>` (`json`, `html`, `txt`, or the captured
extension for streamed binaries) and a sidecar `<same>.meta.json` holding the
URL with its query, the status, the content type, the request body parameter
names, and the capture timestamp, so provenance travels with the file.

`--out` defaults to `tests/captures/` under the current directory. Because the
files are unscrubbed, the command keeps them out of version control:

1. If the target is inside a git work tree, it ensures `.gitignore` at the
   repository root contains the target's relative path (adding a line, and
   saying so) before writing anything.
2. If any target path is already tracked (`git ls-files --error-unmatch`), it
   refuses and exits 1, unless `--allow-tracked` is passed.
3. Outside a git work tree it writes and prints a warning that nothing
   protects the directory.

It prints every file it wrote. It never modifies a body.

### `gp plugin new <name> [--url URL] [--from-run <session> [<run>]] [--dir PATH] [--backend nodriver|selenium] [--new]`

A current scaffold, in two modes decided by the working directory (or
`--dir`):

- **New project** when the directory is empty or has no `pyproject.toml`:
  creates `pyproject.toml` (hatchling, src layout, `[project.entry-points."graftpunk.plugins"]`
  with `<name> = "graftpunk_<name>.plugin:<Name>Plugin"`, dependency
  `graftpunk[browser]>=<the running graftpunk version>`, dev dependencies
  pytest and ruff, the ruff configuration the framework itself uses),
  `src/graftpunk_<name>/__init__.py`, `src/graftpunk_<name>/plugin.py`,
  `tests/conftest.py`, `tests/test_plugin.py`, `tests/fixtures/.gitkeep`,
  `.gitignore` (with `tests/captures/`), and `README.md`.
- **Add to a suite** when `pyproject.toml` exists and declares the
  `graftpunk.plugins` entry-point group
  (`src/graftpunk/plugins/__init__.py:176` (`PLUGINS_GROUP = "graftpunk.plugins"`)):
  creates `src/graftpunk_<name>/` and `tests/test_<name>.py`, appends the
  entry point line and the package to `[tool.hatch.build.targets.wheel]
  packages` when that table lists packages explicitly, and touches nothing
  else. `--new` forces a new project in `--dir` instead.
  A `pyproject.toml` without the entry-point group is a different project:
  the command refuses and explains both modes.

`plugin.py` is the modern template, not the 2026-02 one: a `SitePlugin` with
`site_name`, `session_name`, `help_text`, `base_url`, `backend`
(`nodriver` default), `api_version = 1`, a `login_config` block, a commented
`token_config` block, and a module docstring that opens a dated verification
log ("Verified against a real account on: (none yet)"). Command stubs call
`ctx.session.xhr(...)` (or `navigate`/`form_submit` where the digest saw a
form post), raise `PluginError` with a "run `gp <name> login`" hint on a
non-JSON 200 or a 401/403 (the pattern every existing plugin rewrites, until
part A gives it a home), and return the parsed body. `tests/conftest.py`
provides a `make_ctx(session)` helper and an autouse fixture that removes
`<NAME>_*` environment variables; `tests/test_plugin.py` checks the plugin
class instantiates and that each stub command handles a fixture from
`tests/fixtures/` (the generated test loads a fixture file the developer
copies from `tests/captures/` and invents the content of). `README.md` says
how to install editable, log in, run a command, run tests, and states the
fixtures policy (captures are never committed; fixtures copy the structure
and invent the content).

`--from-run` fills the scaffold from `digest_run`:

- `base_url` from the primary host; `site_name` from `--name`.
- `login_config`: from the first `LoginForm` whose fields include a
  `password` role: `url` is the form's page path, `steps=[LoginStep(fields=
  {role: selector}, submit=<selector>)]`, `failure` left as a commented
  placeholder the developer fills after one failed login, `success` likewise.
  When the login observations show a credential post followed by a redirect
  to a different path, that path is noted in a comment as a candidate
  `success` check. No form found: the block is emitted commented out with the
  observations listed above it.
- `token_config`: emitted when a header token candidate pairs with a meta tag
  or cookie of a matching name: `Token.from_meta_tag(name=..., header=...)` or
  `Token.from_cookie(cookie_name=..., header=...)`. Unpaired candidates are
  listed in a comment.
- one stub command per endpoint, up to 12, JSON endpoints first: path
  parameters become required arguments, query parameters become options with
  the observed type, JSON body parameters on mutating methods become options
  as well; the docstring names the run id, the call count, and the response
  shape sketch. Custom headers seen on that endpoint are set explicitly in the
  stub so the developer sees them.

`gp import-har`, `src/graftpunk/cli/import_har.py`
(`src/graftpunk/cli/import_har.py:38` (`def import_har(`)), and
`src/graftpunk/har/generator.py` are removed; `gp plugin new --from-run`
supersedes them. `har/analyzer.py` keeps only what the digest reuses (the
exclusion patterns and the auth-path patterns); `detect_auth_flow` and
`discover_api_endpoints` (`src/graftpunk/har/analyzer.py:331` (`def discover_api_endpoints(`))
are removed with their tests, since the digest replaces both with better
inputs (bodies, page source, headers). `har/parser.py` is untouched. A
developer with a standalone HAR from another tool is served by `gp observe
digest --har PATH`, which runs the same digest on a file with no run
directory (no bodies, no page source).

### Removal in a minor release

Removing a command in 1.17 rather than 2.0 is a deliberate choice: the
command emits code the framework cannot run, so nobody can be depending on
its output, and the README already sends new users to `import-har` as the
starting point, which is the wrong signal to keep shipping. The CHANGELOG
records it under Removed with the replacement command, and the README and
`docs/HOW_IT_WORKS.md` references
(`./README.md:308` (`  import-har  Import HAR file and generate a graftpunk plugin.`)) change to the new commands.

### Error handling

- Digest and fixtures never raise on a malformed entry: the parser already
  records per-entry errors, and the digest counts them under `dropped["error"]`.
- A body file referenced by the HAR but missing on disk counts as a dropped
  entry with a note; the digest still completes.
- `plugin new` never overwrites: an existing file at any target path aborts
  before anything is written, listing the conflicts. In suite mode it edits
  `pyproject.toml` by appending lines to the two tables it knows, and refuses
  when the entry point name already exists.
- Fixtures' git checks use `git` on `PATH`; if `git` is missing the command
  treats the directory as outside a work tree and warns.

### Testing

Synthetic runs built in `tmp_path` (a small `network.har` with inline bodies,
a `bodies/` file, `page-source.html`), never a recorded capture:

- digest: hosts and primary host; static and third-party exclusion; templating
  of numeric, UUID, hex, and high-cardinality segments with the singular
  parameter name; type observation for query and body params; response shape
  depth and key caps; custom header detection with values absent from output;
  login observations only in the credential-post window; login form parsing
  from page source and from an HTML response, selectors and roles; token
  candidate pairing; `limit`; JSON and markdown renderers; `--har` on a bare
  file; the markdown for a 700-entry synthetic run stays under 400 lines; a
  grep of the rendered output for any value planted in the synthetic run
  (cookie value, token value, password, email) finds nothing.
- fixtures: files and sidecars for a match; `--limit`; `.gitignore` line added
  once and not duplicated; refusal on a tracked path and `--allow-tracked`;
  warning outside a work tree; bodies byte-identical to the capture.
- plugin new: a new project imports, its plugin class instantiates and passes
  `build_plugin_config`, the entry point string resolves to the class, the
  generated `tests/` pass under pytest against a generated fixture, and `ruff
  check` and `ruff format --check` pass on the generated tree; suite mode adds
  the package and the entry point line and leaves the rest of `pyproject.toml`
  byte-identical; refusal on conflicts; `--from-run` on the synthetic run
  emits a `LoginConfig` with the expected selectors, a paired `TokenConfig`,
  and one stub per endpoint with the right parameters.
- CLI: each command through the Typer runner for the happy path and each
  refusal.

### Documentation

`README.md`: the HAR Import section becomes "From recording to plugin" with
the three commands; the CLI reference lists them. `docs/HOW_IT_WORKS.md`:
the Observability section gains digest and fixtures; the plugin section points
at `gp plugin new`. `CHANGELOG.md` `[Unreleased]`: Added (three commands, the
`--har` form), Removed (`gp import-har`, `graftpunk.har.generator`, the two
analyzer functions), with the replacement named. The full authoring guide is
part C.
