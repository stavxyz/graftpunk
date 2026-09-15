---
type: spec
validated:
  sha: f9fe96238ebf4c13bff82327d5b45da3602fa9a6
  date: 2026-09-11T19:29:24Z
  reviewers: [fact-check, solid-hygiene]
  findings:
    critical: 1
    important: 1
    medium: 12
    low: 6
    nitpick: 0
  net_negative_raised: 4
  net_negative_addressed: 4
  net_negative_remaining: 0
---

# Plugin tooling: observe digest, raw captures, and the plugin scaffold

**Date:** 2026-09-11
**Status:** approved 2026-09-11 (validated over three rounds against f9fe962; rebased onto 3b3b661 the same day)
**Series:** part B of four (B tooling, A session primitives, C docs refresh, D the plugin-development skill). Each part is its own spec, plan, and PR. This one comes first because A, C, and D all depend on the commands it adds.

## Problem

Building a new graftpunk plugin today starts from a recording and ends with a
hand-built project, and the two starting points the framework offers for the
middle are stale:

- `gp import-har` (`src/graftpunk/cli/main.py:794` (`@app.command("import-har")`))
  generates handlers whose first parameter is a raw `requests.Session`
  (`src/graftpunk/har/generator.py:91` (`    params = ["self", "session: requests.Session"]`)),
  a shape the framework has not accepted since command handlers started
  receiving a `CommandContext`. It surfaces a detected login only as a comment
  and never emits a `LoginConfig`, a `TokenConfig`, or output views. Its
  auth-flow heuristic (`src/graftpunk/har/analyzer.py:186` (`def detect_auth_flow`))
  built a 46-step "form" login out of a real 731-entry capture probed on
  2026-09-10 (24 of those steps are static assets and 36 classify as
  `unknown`), because path matching against `AUTH_URL_PATTERNS` and
  `POST_LOGIN_PATTERNS` (`src/graftpunk/har/analyzer.py:22` (`AUTH_URL_PATTERNS = [`)
  and `src/graftpunk/har/analyzer.py:42` (`POST_LOGIN_PATTERNS = [`)) applies no
  static-asset exclusion.
- `examples/templates/python_template.py` (last changed 2026-02-03) predates
  multi-account sessions, header roles, `cache_login_session`, and output views.

A recording is the right primary source, but nothing makes it readable. The
capture probed on 2026-09-10 was a 36 MB `network.har` with 731 entries, of
which roughly 190 JSON responses (189 with an `application/json` content type,
179 of them status 200) across 49 distinct endpoints were the useful part, plus
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

Three commands, one analysis package, and the framework helpers the scaffold
calls into. The analysis turns a HAR (with whatever a run adds to it) into a
`RunDigest`; the commands render it, extract from it, and scaffold from it.

### A rule for everything the scaffold emits

Generated code lands in other people's repositories, where nothing graftpunk
ships later can change it. So the scaffold may emit only two kinds of line:
facts about the site (URLs, selectors, parameter names, header names) and
calls into graftpunk's public API. It never emits graftpunk's own logic. Where
the framework lacks the primitive a generated file needs, this part adds the
primitive first; it does not paste a copy.

> **Design note (2026-09-11):** the first draft had the generated command
> stubs carry their own expired-session detection (the pattern every existing
> plugin rewrites) "until part A gives it a home". The validation review
> flagged that as the paradigm case of a shortcut that cannot be undone: a
> copy in a user's repository is permanent. The rule above is the response,
> and it is why the request helpers and the testing package below are in
> part B rather than part A.

### Where new modules live

Two placement rules, stated once so adjacent decisions stop contradicting
each other. Public API that a plugin imports at runtime or in its own tests
lives at the top level of the package (`graftpunk.testing`, the request
helpers on `CommandContext`). Tooling that only the `gp` CLI runs, and that
a plugin never imports, lives under `graftpunk.devtools` (the scaffold, the
capture-directory helper). `graftpunk.har` is analysis and is importable by
both.

### The analysis package: `graftpunk.har`

`graftpunk.har` becomes "everything that understands a HAR file": the parser
it already holds, plus the digest. `src/graftpunk/har/analyzer.py` and
`src/graftpunk/har/generator.py` are deleted, along with
`tests/unit/test_har_analyzer.py` and `tests/unit/test_har_generator.py`; the
exclusion patterns and the auth-path patterns the digest keeps move into the
digest module. The observe package keeps what it knows today, capturing and
storing runs, and its CLI command only resolves a run directory into a
`DigestSource`.

> **Design note (2026-09-11):** the first draft placed the digest in
> `observe/` and kept `har/analyzer.py` alive to hold two pattern lists for
> it. The review called that a split owner for one responsibility. The digest
> reasons about HAR entries, not about how a run is stored, so it belongs
> beside the parser; the run directory is a thin resolution step at the CLI.
> That is also what makes the bare-file form below fall out naturally.

The package gains four modules, each with one job:

- `har/paths.py`: path templating, pure functions. `template_path(path) ->
  tuple[str, dict[str, str]]` collapses a segment to a parameter when it is
  all digits, a UUID, 16 or more hex characters, or base64-like and 20 or
  more characters; the parameter name is the singular of the preceding
  segment when there is one (`orders/123` becomes `orders/{order_id}`), else
  `{id}`. The digest also collapses a segment that appears with more than 8
  distinct values across a run.
- `har/naming.py`: the one file-naming rule that capture, fixture, and test
  share. `capture_filename(method, path, content_type) -> str` gives
  `<method>_<slug>.<ext>` where the slug is the templated path with
  parameters kept as `{name}`, and `<ext>` is `json`, `html`, `txt`, or the
  captured extension for a binary. It depends on `paths.py` and nothing
  else, and the fixtures command, `graftpunk.testing`, and the scaffold all
  import it from here.
- `har/documents.py`: HTML extraction, on the standard library `html.parser`
  and limited to forms, inputs, and meta tags. `extract_login_forms(html,
  source) -> tuple[LoginForm, ...]` and `extract_token_candidates(html, source)
  -> tuple[TokenCandidate, ...]`. Its inputs are HTML text (a page source or
  an HTML response body), so it does not know about HAR entries.
- `har/digest.py`: the model and the digest over entries, calling the three
  above. `har/report.py` holds the two renderers.

> **Design note (2026-09-11):** the rerun review asked for the naming rule,
> the HTML extraction, and the templating to leave the digest module so it
> stays "entries to model". Four modules with one job each is the result.

> **Design note (2026-09-12):** the high-cardinality rule above stated a
> count and said nothing about what the values at that position have to look
> like, so nine word-like sibling routes (`/api/orders`, `/api/products`,
> ...) counted as one family and collapsed into a single `/api/{api_id}`,
> costing the digest eight endpoints and the scaffold eight stubs. The rule
> now has two halves: more than `_HIGH_CARDINALITY_THRESHOLD` distinct values
> at the position, *and* more than `_DYNAMIC_MAJORITY` (half) of those values
> eligible. Eligibility is `paths.looks_dynamic`, the same predicate
> `template_path` collapses on, relaxed to also accept any segment carrying a
> digit, so a slug family (`/products/red-widget-2024`) still collapses while
> a family of plain words does not.

> **Design note (2026-09-12, polish round 1):** "more than 8 distinct values
> across a run" is now scoped to the family that qualified, not to every
> template of the same segment count. A family is the set of templates that
> agree on every segment but one, and a qualifying family carries its own key
> (the tuple of its other segments) alongside the position. Only a template
> belonging to that family, whose own segment at that position is eligible,
> is re-templated. Without the scoping, one slug family under `/products/`
> turned every two-segment sibling into a parameter: `/account/profile` and
> `/account/settings` merged into `/account/{account_id}`, and `/api/health`
> became `/api/{api_id}`.

`src/graftpunk/har/digest.py`:

```python
BodyKind = Literal["json", "form", "none"]
ObservationKind = Literal["form_page", "credential_post", "redirect", "set_cookie", "auth_api"]
TokenKind = Literal["header", "meta", "hidden_input", "cookie"]
DropReason = Literal["static", "third_party", "error", "other_scheme"]

@dataclass(frozen=True)
class DigestSource:
    har_path: Path
    bodies_dir: Path | None = None        # a run's bodies/ directory, when present
    page_source: Path | None = None       # a run's page-source.html, when present
    session: str | None = None            # run identity, when the source is a run
    run_id: str | None = None

    @classmethod
    def from_run_dir(cls, run_dir: Path, *, session: str, run_id: str) -> "DigestSource": ...
    @classmethod
    def from_har(cls, har_path: Path) -> "DigestSource": ...

@dataclass(frozen=True)
class ShapeNode:
    kind: str                             # "object", "array", "string", "number", "boolean", "null"
    children: dict[str, "ShapeNode"] | None = None   # objects: key -> node, at most 12
    item: "ShapeNode | None" = None       # arrays: the first element's node
    truncated: bool = False               # keys beyond 12 or depth beyond 3 were dropped

@dataclass(frozen=True)
class Endpoint:
    host: str
    template: str                 # "/orders/{order_id}/items"
    methods: tuple[str, ...]
    count: int
    statuses: tuple[int, ...]
    content_type: str             # response, primary
    query_params: dict[str, str]  # name -> observed type ("int", "str", "bool", "list")
    body_params: dict[str, str]   # JSON or form fields on POST/PUT/PATCH, name -> type
    body_kind: BodyKind
    shape: ShapeNode | None       # None for non-JSON responses
    custom_headers: tuple[str, ...]  # request header names not in the standard set
    examples: tuple[str, ...]     # up to 3 concrete paths behind the template

@dataclass(frozen=True)
class LoginObservation:
    order: int
    method: str
    url: str                      # query stripped
    status: int
    kind: ObservationKind
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
    kind: TokenKind
    name: str
    seen_on: tuple[str, ...]      # endpoint templates that sent it (headers) or pages that carried it

@dataclass(frozen=True)
class RunDigest:
    source: DigestSource
    primary_host: str
    hosts: dict[str, int]         # host -> request count, all hosts
    endpoints: tuple[Endpoint, ...]   # everything found, ordered by count then template
    login: tuple[LoginObservation, ...]
    login_forms: tuple[LoginForm, ...]
    tokens: tuple[TokenCandidate, ...]
    cookies: tuple[str, ...]      # names set during the run, primary host
    dropped: dict[DropReason, int]

def digest(source: DigestSource, *, all_hosts: bool = False) -> RunDigest
```

`src/graftpunk/har/report.py` holds `render_markdown(d, *, limit=60)` and
`render_json(d)`. The digest produces everything it found; each consumer
applies its own budget: the markdown renderer takes `limit` and orders JSON
endpoints first, the JSON renderer is complete, and the scaffold takes its
own top twelve. The renderers are importable as `graftpunk.har.report` and are
not exported from the package top level, so presentation can change without
touching the package's compatibility surface.

> **Design note (2026-09-11):** the explicit `DigestSource` replaces a
> `digest_run(run_dir)` signature the review found storage-shaped. A later
> pass moved the endpoint cap out of `digest()` (it was a display budget
> applied while building the model, so the JSON form was silently truncated),
> turned the shape into typed `ShapeNode`s instead of a dict with an in-band
> flag, and made the closed string sets `Literal` types.

The public surface of `graftpunk.har` after this change is the parser
(`HAREntry`, `HARParseResult`, `HARRequest`, `HARResponse`, `ParseError`,
`parse_har_file`) and the digest model (`DigestSource`, `RunDigest`,
`Endpoint`, `ShapeNode`, `LoginObservation`, `LoginForm`, `TokenCandidate`,
`digest`). `detect_auth_flow`, `discover_api_endpoints`,
`generate_plugin_code`, `extract_domain`, `APIEndpoint`, `AuthFlow`, and
`AuthStep` leave `__all__` (`src/graftpunk/har/__init__.py:34` (`__all__ = [`))
and the module docstring's usage example changes with them. `graftpunk.har`
stays importable with the base install: no parser dependency is added.

Rules the digest applies:

- **Parsing** reuses `parse_har_file`, which derives its body base directory
  from the HAR file's own parent
  (`src/graftpunk/har/parser.py:336` (`    base_dir = filepath.parent`)), so a
  run's `network.har` resolves streamed bodies with no extra argument, and a
  bare HAR from another tool simply has none to resolve. Bodies over 256 KB are
  sampled (first 64 KB) for shape only; nothing about a body is ever printed
  verbatim.

> **Design note (2026-09-12, polish round 1):** the sampling above is gone. A
> fixed-size prefix of a JSON body is never itself valid JSON, so parsing one
> could only ever fail, and every body over the threshold reported
> `shape: non-JSON` (and the generated docstring said `Shape: non-JSON.`),
> which is a false claim about the endpoint. The body is now parsed whole; it
> is already in memory. The 256 KB constant stays as the line above which a
> parse failure is reported as `SHAPE_UNAVAILABLE` ("shape unavailable: body
> over the sampling threshold") rather than as non-JSON, since a capture
> routinely truncates a body that large. The 64 KB constant is removed.
> `ShapeNode.kind` gains `"unavailable"` to carry that third state, distinct
> from `shape=None` (the endpoint returns no JSON at all); the scaffold omits
> the shape line entirely for it.
- **Primary host** is the host that answered the most non-static requests;
  hosts sharing its registrable domain count as primary. Other hosts are
  reported by count only unless `--all-hosts`.

> **Design note (2026-09-12, polish round 1):** "registrable domain" was
> implemented as the primary host's last two labels, which needs a public
> suffix list to be correct. Without one, a site under a two-label public
> suffix (a country-code second-level domain such as `co.uk`) made every host
> sharing that suffix a first party. Scope is now the primary host's parent
> domain, and the host itself when it has two labels or fewer:
> `shop.team.example.com` scopes to `team.example.com`, `www.example.com` to
> `example.com`. A host is in scope when it equals that root or is a
> subdomain of it. No public suffix list is involved. The residual the
> no-list rule accepts: a primary host of the form `name.<two-label public
> suffix>` (`mybank.co.uk`) has three labels, so it scopes to the bare suffix
> and every host under that suffix counts as first party for that capture.
> Sites normally record from a `www` or `app` subdomain, which scopes
> correctly; a bare apex under such a suffix is the one shape this rule gets
> wrong.

> **Design note (2026-09-12, polish round 2):** "most non-static requests" is
> no longer the whole election. A host that served an HTML document is
> preferred over one that did not, and the count decides within each group. A
> page-driven site answers its own pages and serves everything else as assets,
> so it can be out-counted on non-static requests by a third-party telemetry
> endpoint answering a handful of beacons: on a real recording the site served
> three documents and an error-reporting host four beacons, which made the
> beacon host primary and put the whole site out of scope. A capture with no
> HTML in it (an API-only run) falls back to the count alone, and a tie still
> keeps the first host seen.
- **Static and tracking exclusion** reuses the analyzer's exclusion patterns
  (the `EXCLUDE_PATTERNS` list at
  `src/graftpunk/har/analyzer.py:58` (`EXCLUDE_PATTERNS = [`), compiled once
  into `EXCLUDE_REGEX` at `src/graftpunk/har/analyzer.py:73` (`EXCLUDE_REGEX = re.compile("|".join(EXCLUDE_PATTERNS), re.IGNORECASE)`)),
  which move into the digest module, extended with a content-type rule:
  image, font, CSS, and JavaScript responses are static regardless of URL.

> **Design note (2026-09-12, polish round 1):** the analyzer's list is no
> longer reused verbatim. It was one alternation searched over the whole URL,
> so every pattern matched anywhere: `/api/analytics/summary` on the primary
> host was dropped as a tracker, and `cdn.`, `static.`, `assets.` matched any
> path spelling them. Exclusion is now three rules against three parts of the
> URL. The asset extensions match the end of the path. The tracker and asset
> host names (`google-analytics`, `googletagmanager`, `facebook.com`,
> `analytics`, `tracking`, `cdn.`, `static.`, `assets.`, `fonts.`) match the
> netloc only, since each names a host rather than a path. `pixel` and
> `beacon` match a whole path segment, so `/pixel` is dropped and
> `/pixelate-image` is not.

> **Design note (2026-09-12, polish round 2):** the content-type half of the
> rule no longer names four families in prose. A real recording served
> `/vendor/custom.<32 hex>._hs` as `text/hyperscript`: no listed extension, no
> listed type, so it became an endpoint, a command stub, and a generated test.
> A response is static when its main type is `image`, `font`, `audio`, or
> `video` (`_STATIC_MAIN_TYPES`), or when its full type with parameters
> stripped is in `_STATIC_CONTENT_TYPES` (`text/css`, `text/javascript`,
> `application/javascript`, `application/x-javascript`,
> `application/ecmascript`, `text/hyperscript`, `application/wasm`,
> `application/font-woff`, `application/font-woff2`,
> `application/vnd.ms-fontobject`, `image/svg+xml`). The extension and host
> rules are unchanged, and still catch an asset whose response declared no
> content type at all.

> **Design note (2026-09-12, polish round 2):** `DropReason` gains
> `other_scheme`. A capture taken before the first navigation holds the
> browser's own new-tab page (`chrome://`, `chrome-untrusted://`, and a
> `data:` URL), whose netloc is not a host: those entries were counted as
> hosts and listed under "Other hosts". An entry whose scheme is neither
> `http` nor `https` is now dropped ahead of every other rule, so it reaches
> neither the host counts nor the endpoint list.
- **Types** are observed, not declared: a query value that always parses as an
  integer is `int`, `true`/`false` is `bool`, repeated keys are `list`, else
  `str`. Values are never retained.
- **Response shape** is a `ShapeNode` tree of the JSON body: objects map keys
  to child nodes, arrays carry their first element's node, depth is limited to
  3 and objects to 12 keys with `truncated` set beyond that. Strings, numbers,
  and booleans are recorded as their kind only.
- **Custom headers** are request header names on primary-host calls that are
  not in a standard set (the RFC headers plus `sec-ch-*`, `sec-fetch-*`,
  `user-agent`, `referer`, `origin`, `accept*`, `content-*`, `cookie`,
  `x-requested-with`). Names are reported; values are never reported.
- **Login observations** are, in capture order: GETs of HTML pages that
  contain a password input (`form_page`), POSTs whose form or JSON body has a
  password-like field name (`credential_post`, field names listed, values
  dropped), 30x responses in the twenty entries after a credential post
  (`redirect`), responses in that window that set cookies (`set_cookie`, cookie
  names only), and calls to paths matching the auth-path patterns (`auth_api`).
  No entry outside that shape is a login observation, which is what stops
  asset loads being reported as a login.
- **Login forms** come from `documents.py` over `page-source.html` and every
  HTML response of the primary host: each `<form>` containing an
  `input[type=password]` yields a `LoginForm` with a CSS selector per input
  (prefer `#id`, else `form[action=...] input[name=...]`), the role guessed
  from type and name (`password` for the password input; `username` for
  `type=email`, or a name containing `user`, `email`, `login`, `account`;
  anything else keeps its name), the submit control's selector, and hidden
  input names.
- **Token candidates**: request headers whose name contains `csrf`, `xsrf`, or
  `token`; `<meta name=...>` tags in HTML bodies with the same substrings;
  hidden inputs named like `_token`, `csrf*`, `authenticity_token`; cookies
  with those substrings. Each candidate lists where it was seen so the reader
  can pair a header with its source.
- **Redaction** is by construction: the digest holds names, types, counts,
  templates, and shapes, and no header value, cookie value, query value, body
  value, or credential. The markdown renderer therefore has nothing to redact.
- **Size**: the markdown renderer's `limit` (default 60) caps the endpoint
  list; the markdown for the 2026-09-10 capture should come out under 400
  lines. Every threshold in this section (256 KB, 8 distinct values,
  16 hex characters, 20 characters, 20 entries, depth 3, 12 keys, 60, 400) is
  a named constant in the module that applies it, so the tests assert the
  names rather than repeat the literals.

### The framework helpers the scaffold calls

**Request helpers.** A new module `src/graftpunk/plugins/site_requests.py`
owns the request policy as `SiteRequests(session, plugin_name, base_url)`
with `json(method, url, *, role="xhr", **kwargs) -> Any` and `text(method,
url, *, role="navigation", **kwargs) -> str`. `CommandContext` gains two thin
delegating methods, `request_json` and `request_text`, built from its own
session, plugin name, and base URL, so the context stays an identity carrier
and the policy has its own file and tests. The policy:

- A relative `url` is joined onto `base_url`.
- `role` is one of the session's registered header roles, `xhr`, `navigation`,
  or `form` (the names `GraftpunkSession` registers; the helper methods on the
  session are `xhr`, `navigate`, and `form_submit`). A session that has no
  role support (a plain `requests.Session`) gets the request without role
  headers and one `session_roles_unavailable` warning per session, so the
  degradation is visible at runtime and not only in prose. The context's
  field stays typed `requests.Session`; `make_context` builds a
  `GraftpunkSession` by default.
- A 401 or 403 raises `SessionRejectedError`. Any other 4xx or 5xx raises
  `CommandError` whose `user_message` names the method, path, and status.
- `json()` on a 2xx whose body is not JSON raises `UnexpectedResponseError`
  naming the content type it got, unless the body is a login document (it
  contains an `input[type=password]`, judged by `har/documents.py`), in which
  case it raises `SessionRejectedError`, since that is the HTML login page a
  stale session gets back with a 200. Otherwise it returns the parsed body.
- `text()` returns the body of any 2xx as text and detects rejection by
  status only, so an HTML endpoint is never mistaken for an expired session.
- Neither helper returns a partial result; both raise typed errors only.

> **Design note (2026-09-12, polish round 2):** the helpers gained one more
> rule, before the request goes out. A `params` or `data` mapping has `True`
> and `False` replaced by `"true"` and `"false"`, and every key whose value is
> `None` removed. A real recording sent `keywordSearch=false`; the digest typed
> it `bool`, so the stub declares `keyword_search: bool | None = None` and
> passes it in `params=`, and `requests` serialises a Python bool with `str()`,
> which would have sent `keywordSearch=True`. The scaffold rule forbids fixing
> that in generated code (a generated line is a site fact or a call into the
> public API, never logic), so the framework helper owns it. The `None` rule is
> the other half of the same stub shape: `None` is how a stub says the caller
> did not ask for the parameter, and sending it would add a value the site
> never saw.

`SessionRejectedError` and `UnexpectedResponseError` are new `CommandError`
subclasses in `src/graftpunk/exceptions.py`; the CLI already renders a
`CommandError` through its `user_message` as one clean line
(`src/graftpunk/cli/plugin_runtime.py:278` (`    except CommandError as exc:`)).
`SessionRejectedError`'s message reads "The site rejected the cached session
(<status> on <method> <path>). Run: gp <plugin> login". The three
session-related errors now in the codebase own distinct conditions:
`SessionExpiredError` means the cached blob itself is unusable (the cache
raises it); `SessionInvalidatedError` means token extraction discovered the
site wants a fresh login (the token layer raises it); `SessionRejectedError`
means a command's request was refused. The last two are the re-login signals
and both render the login hint; folding them into one hierarchy is out of
scope here and left to part A.

> **Design note (2026-09-11):** the re-review found that a stub generated for
> an HTML endpoint would call the JSON-only helper and report every successful
> call as an expired session, permanently, in the user's repository. The
> owner chose a second helper for raw responses over limiting stubs to JSON
> endpoints. The rerun review then asked for the policy to leave the context
> dataclass, for the role degradation to be observable, for the "not JSON"
> signal to stop meaning "expired" unless the body is a login page, and for
> the relation to the existing session errors to be stated; all four are
> above. This is the "session expired" primitive part A was going to add;
> part A keeps the poll-until login step.

**`graftpunk.testing`**, a package split by module so its pytest boundary is
legible from the import paths:

- `graftpunk/testing/__init__.py` is pytest-free and unconditional:
  `make_context(session=None, *, plugin_name="test", command_name="test",
  base_url="", config=None) -> CommandContext` (a `GraftpunkSession` when no
  session is given), `FixtureSession`, and the sugar `fixture_context(dir,
  **kw)`, which is `make_context(session=FixtureSession(dir), **kw)` and
  nothing more. `FixtureSession` is a `GraftpunkSession` subclass that never
  opens a socket: each request is answered from the file in `dir` named by
  `har/naming.py` for that method and path; its status and content type come
  from the sidecar `<name>.meta.json` beside it when present (the same sidecar
  `gp observe fixtures` writes, so a fixture can express a 403 or an HTML 200
  as easily as a JSON 200), else 200 and the type implied by the extension; no
  matching file answers 404.
- `graftpunk/testing/plugin.py` holds everything that imports pytest:
  `site_env_scrubber(prefix) -> fixture`, which returns an autouse fixture
  object that removes every environment variable starting with `prefix` for
  each test and restores them afterwards. A generated `conftest.py` is two
  declarations, the import of `site_env_scrubber` and
  `scrub_site_env = site_env_scrubber("<NAME>_")`, and no code.

> **Design note (2026-09-12):** the generated `conftest.py` also listed
> `graftpunk.testing.plugin` in `pytest_plugins`, which made pytest try to
> rewrite assertions in a module the import on the line above had already
> loaded: every generated project printed a `PytestAssertRewriteWarning` on
> its first run. The module defines no hooks or fixtures of its own, so the
> `pytest_plugins` declaration bought nothing; the import and the assignment
> are the whole file now.

> **Design note (2026-09-11):** the first draft generated a `make_ctx`
> helper and an env-scrubbing fixture into every project's `conftest.py`.
> Under the rule above those are framework logic, so they live here and the
> generated file declares them. The re-review then found the generated tests
> had no owned network stand-in; `FixtureSession` is the answer. The rerun
> review found the pytest boundary described two incompatible ways and a
> conditional export surface; splitting by module removed the guard, and the
> sidecar gives downstream tests the rejection path too.

### `gp observe digest <session> [<run>] [--json] [--all-hosts] [--limit N] [--output PATH]` and `gp observe digest --har PATH`

Resolves the run like `gp observe show`
(`src/graftpunk/cli/observe_commands.py:388` (`@observe_app.command("show")`)): session
through `session_dirname`
(`src/graftpunk/observe/storage.py:20` (`def session_dirname(session_name: str) -> str:`)),
run defaulting to the newest, then builds `DigestSource.from_run_dir` and
calls `digest`. `--har PATH` builds `DigestSource.from_har` instead and takes
no session or run; giving both is an error. Prints the markdown digest, or
JSON with `--json`; `--output` writes to a file instead; `--limit` reaches the
markdown renderer only. Missing run, missing `network.har`, or a missing file
is a one-line error and exit 1. Markdown sections, in order: Summary (source,
hosts, counts, dropped), Login (observations then forms), Tokens, Cookies,
Endpoints (one block per endpoint), Other hosts.

The two new observe commands live in a new `src/graftpunk/cli/observe_commands.py`,
which also takes ownership now of the run-resolution helper (`resolve_run(session,
run) -> Path`) that `gp observe show` and `gp observe clean` use, so the
interim split grows no import back into `main.py`. The module exposes
`register(observe_app: typer.Typer) -> None`, the idiom
`register_plugin_commands` already uses, and `main.py` calls it right after
it constructs the sub-app; the other command modules are attached as
module-level sub-apps, and this one differs only because the sub-app it
extends already exists in `main.py`. The five existing observe commands stay
in `main.py` for this part, to keep its diff to the new commands now that the
`gp observe interactive` launch path has just been reworked by PR #195
(merged 2026-09-11, closing #96). Part C moves them into
`observe_commands.py`.

> **Design note (2026-09-11):** the review noted the new commands had no
> named module and that `main.py` already holds the whole observe sub-app.
> New code goes in its own module; the rerun asked for the interim split to
> have a named owner (part C), for the shared run resolution to move now,
> and for the precedent of `register()` to be named correctly.

> **Design note (2026-09-15):** `observe clean` does not resolve a run; the
> sentence overstated the helper's reach. It removes a session directory (or
> the whole base directory) and never looked a run up. `gp observe show` is
> the only other caller of `resolve_run` in the CLI. Part C also retired
> `register()`: `observe_commands.py` now owns `observe_app` and attaches all
> seven commands at import time, and `main.py` just adds the sub-app.

### `gp observe fixtures <session> [<run>] --match "<METHOD> <template>" [--out DIR] [--limit N] [--allow-tracked]`

Writes captured response bodies exactly as recorded, for the developer or the
agent to derive committed test fixtures from by hand. `--match` takes the
`METHOD template` line as printed by the digest (repeatable); the template
matches the digest's own templating, and a plain path or a glob is accepted.
For each matching entry, up to `--limit` (default 5) per template, it writes
the file `har/naming.py` names (with `_<n>` before the extension from the
second match on) and a sidecar `<same>.meta.json` holding the URL with its
query, the status, the content type, the request body parameter names, and
the capture timestamp, so provenance travels with the file and a fixture
derived from it keeps its status and type.

> **Design note (2026-09-12):** a `--match` value is validated up front:
> partitioning `METHOD template` on a blank template or an unrecognized
> method refuses with a red line and exits 1, rather than silently matching
> nothing and leaving a typo indistinguishable from an empty run. An entry
> whose captured body is not text (the parser returns no body for a binary
> content type, an image or a font among them) is skipped with a dim line
> naming the content type, and skipping it never consumes a `--limit` slot,
> so a run of binary and text responses interleaved still yields `--limit`
> text fixtures per template.

The "captures never enter git" rule has one owner, `src/graftpunk/devtools/captures.py`:
`CAPTURES_DIR = "tests/captures"`, `ensure_ignored(repo_root, relative) ->
bool` (adds the line to the root `.gitignore` when absent, returns whether it
added it), and `is_tracked(path) -> bool` (`git ls-files --error-unmatch`).
The fixtures command, the scaffold's generated `.gitignore`, and the
scaffold's suite mode all use it, so the default directory and the ignore
line cannot disagree.

`--out` defaults to `CAPTURES_DIR` under the current directory. Because the
files are unscrubbed:

1. If the target is inside a git work tree, `ensure_ignored` runs first and
   the command says when it added the line.
2. If any target path is tracked, the command refuses and exits 1, unless
   `--allow-tracked` is passed.
3. Outside a git work tree it writes and prints a warning that nothing
   protects the directory; if `git` is not on `PATH` it behaves the same way.

It prints every file it wrote. It never modifies a body.

> **Design note (2026-09-11):** the review found the invariant encoded three
> times in the first draft (here, in the generated `.gitignore`, and absent
> from suite mode). `captures.py` is the single owner now, suite mode runs the
> same check, and the rerun's placement rule puts it under `devtools` beside
> the scaffold rather than in the runtime package root.

### `gp plugin new <name> [--url URL] [--from-run <session> [<run>]] [--dir PATH] [--backend nodriver|selenium] [--new]`

Generation lives in `src/graftpunk/devtools/scaffold/`: `render.py` turns a
`ScaffoldSpec` (name, mode, backend, base URL, and an optional `RunDigest`)
into a mapping of relative path to file content; `project.py` decides the
mode, checks for conflicts, and writes; `pyproject_edit.py` is the one seam
that edits an existing `pyproject.toml`. The CLI command in
`src/graftpunk/cli/scaffold_commands.py` registers the `plugin` group and
does argument handling only.

Top-level CLI names are reserved against plugin registration in
`register_plugin_commands` (`src/graftpunk/cli/plugin_commands.py`), which
already refuses a name collision between plugins: at attach time it derives
the reserved set from the app's registered top-level commands and groups
(`plugin`, `plugins`, `session`, `http`, `config`, `keepalive`, `observe`,
and whatever is added later) and refuses a plugin whose `site_name` is in
it, with a message naming the clash. `gp plugins` (the list) is unchanged.

> **Design note (2026-09-11):** the review asked for a named owner of
> generation since `har/generator.py` is deleted, and noted `gp plugin new`
> beside `gp plugins`. The package above is the owner; the list command is
> not renamed. The rerun found a single hardcoded reserved name in the
> registry; the reserved set is now derived from the CLI at attach time.

Two modes, decided by the working directory (or `--dir`):

- **New project** when the directory is empty or has no `pyproject.toml`:
  `pyproject.toml` (hatchling, src layout, `[project.entry-points."graftpunk.plugins"]`
  with `<name> = "graftpunk_<name>.plugin:<Name>Plugin"`, a dependency floor
  `graftpunk[browser]>=<major.minor.0 of the running graftpunk>` (a
  pre-release or local checkout floors at its base release), dev
  dependencies pytest and ruff, and a minimal ruff block: `line-length = 100`
  and `select = ["E", "F", "I", "UP", "B"]`), `src/graftpunk_<name>/__init__.py`,
  `src/graftpunk_<name>/plugin.py`, `tests/conftest.py`, `tests/test_plugin.py`,
  `tests/fixtures/.gitkeep`, `.gitignore` (with the `CAPTURES_DIR` line from
  `captures.py`), and `README.md`.
- **Add to a suite** when `pyproject.toml` exists and declares the
  `graftpunk.plugins` entry-point group
  (`src/graftpunk/plugins/__init__.py:176` (`PLUGINS_GROUP = "graftpunk.plugins"`)):
  creates `src/graftpunk_<name>/` and `tests/test_<name>.py`, adds the entry
  point line and, when `[tool.hatch.build.targets.wheel]` lists `packages`
  explicitly, the package; runs `ensure_ignored` for `CAPTURES_DIR` and
  reports if it added the line; touches nothing else. `--new` forces a new
  project in `--dir` instead. A `pyproject.toml` without the entry-point group
  is a different project: the command refuses and explains both modes.

> **Design note (2026-09-12, polish round 1):** a suite member owns
> `tests/fixtures/<module>/` rather than sharing `tests/fixtures/` with its
> siblings, and its generated `FIXTURES_DIR` points there. Fixtures are named
> for an endpoint's method and templated path, so two plugins in one suite with
> a `GET /orders` between them would claim the same file. Each add emits
> `tests/fixtures/<module>/.gitkeep`, and `write_scaffold` drops any `.gitkeep`
> whose directory already exists: that file exists only to put an empty
> directory under version control, and refusing on one the previous add created
> made every second plugin in a suite impossible to create. `render()` stays
> pure; `project.py` makes that filesystem decision.

`pyproject_edit.py` reads the file with `tomllib` to find the two tables and
writes textually, and it edits exactly two shapes: a `[project.entry-points."graftpunk.plugins"]`
table (a new `name = "..."` line appended to that table) and a multi-line
or inline `packages = [...]` array under `[tool.hatch.build.targets.wheel]`.
Any other shape (an `include` list instead of `packages`, an entry-point group
spelled another way, a table it cannot locate textually) makes it refuse with
the shape it saw and the two lines to add by hand. Duplicate entry point names
refuse too. A wrong edit to someone's build configuration is worse than a
refusal with instructions.

`plugin.py` is the modern template, not the 2026-02 one: a `SitePlugin` with
`site_name`, `session_name`, `help_text`, `base_url`, `backend` set
explicitly (`nodriver` by default; the framework's own default is `selenium`),
`api_version = 1`, a `login_config` block, a commented `token_config` block,
and a module docstring that opens a dated verification log ("Verified against
a real account on: (none yet)"). Command stubs call `ctx.request_json(...)`
for endpoints whose primary content type is JSON and `ctx.request_text(...)`
for the rest, with the role the digest saw (`xhr` for JSON calls,
`navigation` for HTML pages, `form` where it saw a form post), and return the
result; they contain no error handling of their own. Everything the generator
could not determine is marked with one grep-able marker, `# GP-FILL: <what to
fill in>`, on the line it belongs to; no other comment style is used for that
purpose. `tests/conftest.py` is the `site_env_scrubber` import plus
`scrub_site_env = site_env_scrubber("<NAME>_")`; `tests/test_plugin.py` checks
the plugin class instantiates and, for each stub command, calls it through
`fixture_context(FIXTURES_DIR)` against a fixture file in `tests/fixtures/`
named by `har/naming.py` for that endpoint (the developer copies the capture
of the same name and its sidecar from `tests/captures/` and invents the
content) and asserts on the returned data. `README.md` says how to install
editable, log in, run a command, run tests, and states the fixtures policy
(captures are never committed; fixtures copy the structure and invent the
content).

> **Design note (2026-09-11):** the ruff block is a described minimal
> configuration rather than a copy of the framework's, so generated projects
> are the same regardless of which graftpunk version generated them.

`--from-run` fills the scaffold from the digest:

- `base_url` from the primary host; `site_name` from `--name`.
- `login_config`: from the first `LoginForm` whose fields include a
  `password` role: `url` is the form's page path, `steps=[LoginStep(fields=
  {role: selector}, submit=<selector>)]`, `failure` and `success` left as
  `GP-FILL` lines the developer completes after one failed login. When the
  login observations show a credential post followed by a redirect to a
  different path, that path is noted beside `success` as the candidate. No
  form found: the block is emitted commented out with the observations
  listed above it.
- `token_config`: emitted when a header token candidate pairs with a meta tag
  or cookie of a matching name: `Token.from_meta_tag(name=..., header=...)` or
  `Token.from_cookie(cookie_name=..., header=...)`. Unpaired candidates are
  listed on a `GP-FILL` line.
- one stub command per endpoint, up to 12, JSON endpoints first and then the
  rest (each calling the helper for its content type): path parameters become
  required arguments, query parameters become options with the observed type,
  JSON body parameters on mutating methods become options as well; the
  docstring names the run id, the call count, and the shape drawn from the
  `ShapeNode` tree at the scaffold's own depth. Custom headers seen on that
  endpoint are passed explicitly in the stub's call so the developer sees
  them.

> **Design note (2026-09-12, polish round 1):** "one stub command per
> endpoint" excludes the login flow. An endpoint that appears in
> `RunDigest.login` with kind `form_page` or `credential_post`, matched on
> method and templated path, gets no stub and no generated test: `login_config`
> above already owns it. A run holding the login form GET `/login` and the
> credential POST `/login` rendered `login` and `login_2` stubs calling
> `request_text`, which are wrong for the developer and whose generated tests
> could only fail. The digest's own endpoint list is unchanged; only the
> scaffold skips them.

YAML plugins remain supported at runtime, and this part generates Python
only; a `--format yaml` scaffold is out of scope here and part C decides
whether to add it. `examples/templates/python_template.py` is deleted, since
`gp plugin new` is the Python starting point now, and the line naming it in
`examples/README.md:102` (`for Python plugins`) is
replaced with a pointer to `gp plugin new`; `examples/templates/yaml_template.yaml`
stays as the YAML one.

`gp import-har`, `src/graftpunk/cli/import_har.py`
(`src/graftpunk/cli/import_har.py:38` (`def import_har(`)), `src/graftpunk/har/generator.py`,
and `src/graftpunk/har/analyzer.py` are removed. Their tests go with them:
`tests/unit/test_import_har.py`, `tests/unit/test_har_generator.py`, and
`tests/unit/test_har_analyzer.py` are deleted, the `TestImportHarCommand`
class in `tests/unit/test_cli.py` (`tests/unit/test_cli.py:637` (`class TestImportHarCommand:`))
is deleted, and the `("graftpunk.cli.import_har", "console")` entry is removed
from `_CONSOLE_LOCATIONS` in `tests/unit/conftest.py`
(`tests/unit/conftest.py:20` (`    ("graftpunk.cli.import_har", "console"),`)),
where an autouse fixture imports every listed module for every unit test.
`har/parser.py` is untouched.

### Removal in a minor release

Removing a command in 1.17 rather than 2.0 is a deliberate choice: the
command emits code the framework cannot run, so nobody can be depending on
its output, and the README still documents `import-har` as the way to
generate a plugin from a capture, which is the wrong signal to keep
shipping. The same change drops `detect_auth_flow`, `discover_api_endpoints`,
`generate_plugin_code`, and `extract_domain` from the public `graftpunk.har`
exports, along with the now-unused `APIEndpoint`, `AuthFlow`, and `AuthStep`
(`src/graftpunk/har/__init__.py:34` (`__all__ = [`)), and the CHANGELOG records
that under Removed as a documented API break naming the import path. The
README references (`./README.md:308` (`  import-har  Import HAR file and generate a graftpunk plugin.`)
and the `### HAR Import` section at `./README.md:379` (`### HAR Import`))
change to the new commands; `docs/HOW_IT_WORKS.md` has no `import-har`
reference to remove, only the additions described under Documentation below.

### Error handling

- Digest and fixtures never raise on a malformed entry: the parser already
  records per-entry errors, and the digest counts them under `dropped["error"]`.
- A body file referenced by the HAR but missing on disk counts as a dropped
  entry with a note; the digest still completes.
- `plugin new` never overwrites: an existing file at any target path aborts
  before anything is written, listing the conflicts. `pyproject_edit.py`
  refuses rather than guesses, as above.
- `request_json` and `request_text` raise typed errors only; neither returns
  a partial result.

### Testing

Synthetic runs built in `tmp_path` (a small `network.har` with inline bodies,
a `bodies/` file, `page-source.html`), never a recorded capture:

- paths and naming: templating of numeric, UUID, hex, base64-like, and
  high-cardinality segments with the singular parameter name; the capture
  filename for each method, path, and content type; both are pure and are
  tested without a HAR.
- documents: login form parsing from a page source and from an HTML body,
  selectors and roles, submit control, hidden inputs; meta-tag and hidden-input
  token candidates; the login-document predicate the request policy uses.
- digest: hosts and primary host; static and third-party exclusion; type
  observation for query and body params; `ShapeNode` depth and key caps with
  `truncated`; custom header detection with values absent from output; login
  observations only in the credential-post window; token candidate pairing;
  `DigestSource.from_har` on a bare file; every endpoint found is present in
  the model regardless of count.
- report: markdown `limit` and ordering; the JSON form complete; the markdown
  for a 700-entry synthetic run stays under 400 lines; a grep of the rendered
  output for any value planted in the synthetic run (cookie value, token
  value, password, email) finds nothing.
- `SiteRequests` and the context delegates: relative and absolute URLs; each
  role; a plain `requests.Session` takes the no-role path and logs the warning
  once; 401 and 403 raise `SessionRejectedError` whose message names the
  plugin's login command; a 2xx HTML login page raises it from `json()`; a
  2xx of another unexpected type raises `UnexpectedResponseError` naming the
  type; `text()` returns any 2xx body; other statuses raise `CommandError`
  with the status; through the CLI runner, each error renders as one line.
- `graftpunk.testing`: `make_context` builds a valid `CommandContext` with a
  `GraftpunkSession`; `FixtureSession` answers a request from the matching
  file, honours a sidecar's status and content type, and 404s an unmatched
  request; `fixture_context` is the documented sugar; `site_env_scrubber`
  removes only the prefixed variables and restores them; a subprocess that
  imports `graftpunk.testing` with pytest hidden from the import system still
  exposes `make_context` and `FixtureSession`.
- fixtures: files and sidecars for a match; `--limit`; `.gitignore` line added
  once and not duplicated; refusal on a tracked path and `--allow-tracked`;
  warning outside a work tree; bodies byte-identical to the capture.
- `captures.py`: `ensure_ignored` idempotent; `is_tracked` on a tracked and
  an untracked path in a temporary repository.
- scaffold: a new project imports, its plugin class instantiates and passes
  `build_plugin_config`, the entry point string resolves to the class, the
  generated `tests/` pass under pytest against a generated fixture and
  sidecar, and `ruff check` and `ruff format --check` pass on the generated
  tree; suite mode adds the package and the entry point line and leaves the
  rest of `pyproject.toml` byte-identical; `pyproject_edit.py` edits both
  known shapes and refuses each listed unknown shape with the message naming
  it; refusal on conflicts; `--from-run` on the synthetic run emits a
  `LoginConfig` with the expected selectors, a paired `TokenConfig`, and one
  stub per endpoint with the right helper and parameters; a plugin whose
  `site_name` is any reserved CLI name is refused at registration.
- CLI: each command through the Typer runner for the happy path and each
  refusal, including `digest` given both a run and `--har`.
- The three deleted test modules and the deleted `TestImportHarCommand`
  class are the only tests removed; the suite is green at every commit.

### Documentation

`README.md`: the HAR Import section becomes "From recording to plugin" with
the three commands; the CLI reference lists them. `examples/README.md`
points at `gp plugin new` where it named the Python template.
`docs/HOW_IT_WORKS.md`: the Observability section gains digest and fixtures;
the plugin section points at `gp plugin new` and documents
`ctx.request_json`, `ctx.request_text`, and `graftpunk.testing`.
`CHANGELOG.md`: the existing `[Unreleased]` section (PR #195 opened it) gains Added
(three commands, the `--har` form, `CommandContext.request_json` and
`request_text`, `SessionRejectedError` and `UnexpectedResponseError`,
`graftpunk.testing`, the reserved CLI names) and Removed (`gp import-har`,
`graftpunk.har.generator`, `graftpunk.har.analyzer`, the named
`graftpunk.har` exports, the Python template), with the replacement named.
The full authoring guide is part C.
