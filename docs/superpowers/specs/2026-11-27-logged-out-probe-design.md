---
type: spec
---

# Logged-out answers per endpoint: `gp observe probe` (#376)

Paths are under `src/graftpunk/` unless they say otherwise. The repository owner decided the seven open questions of the design posted on #376 on 2026-11-27; the decisions are folded in below and listed at the end.

## Goal

A plugin author learns, from a recording, what each endpoint answers when the session is missing, so the generated plugin can tell a dead session from a live one. Today `gp plugin new` says nothing about that, and a dead session is caught only by status, by `request_json` reading a login page, or by code the author writes.

## What exists

- `gp observe` is a Typer group in `cli/observe_commands.py` with `go`, `interactive`, `digest`, `fixtures`, `list`, `show`, and `clean`. The group callback resolves a session name, or with `--no-session` takes `--session` only as the run's directory name.
- A run lives at `<observe dir>/<session slug>/<run id>/` (`observe/storage.py`): `network.har`, `bodies/`, `screenshots/`, `snapshots/`, `events.jsonl`, `console.jsonl`, and `page-source.html`. The directory is 0700 and every file 0600 (`write_private_file`). `network.har` holds request headers, the `Cookie` header included, in plaintext.
- The digest is `har/digest.py`: `DigestSource.from_run_dir` names the files it reads, and `digest()` builds a `RunDigest` of `Endpoint` rows. Each row keeps `statuses`, `content_type`, `shape`, `falsy_first_response`, and `fixture_entry_index`, the HAR index of the recording the generator uses. `har/report.py` renders text (`render_markdown`, `_endpoint_block`), full JSON (`render_json`), and the versioned projection (`endpoints_projection`, `_projected_endpoint`). Fields marked `OMIT_EMPTY` are left out when empty. The endpoints schema is 2 (`contracts.py`, `ENDPOINTS_SCHEMA`), written as 1 when no row carries a GraphQL operation.
- GraphQL detection is `har/graphql.py`. `document_operation` returns the kind of the first operation definition only; `operations_in` reads a JSON body or a batch; a persisted query sent by hash has kind `persisted` (`operation_kind`).
- Dead-session detection is runtime only, in `plugins/site_requests.py`: 401 and 403 raise `SessionRejectedError` (`error_for_status`); `request_json` also raises it for a non-JSON 2xx that `is_login_document` (`har/documents.py`) reads as a login page; `request_text` decides by status alone. `LoginConfig` (`plugins/cli_plugin.py`) has no session-check field.
- `gp plugin new` (`cli/scaffold_commands.py`) digests the run, renders stubs (`devtools/scaffold/render.py`, with `GP-FILL` notes), and prints next steps (`_print_next_steps`).

## Structure

The code splits along the package layering that exists today: `har/` is the pure layer over recorded files and imports nothing from `observe/`; `observe/` records and does I/O and depends on `har/`.

`har/logged_out.py` (new, pure) owns the vocabulary, the file's reading, and every decision that reads no network:

- `ProbeClass` and `NotProbedReason`: `Literal` types naming every class and reason. `har/digest.py`, `har/report.py`, and the scaffold import them and match no string literal of their own.
- `LoggedOut` and `LoggedOutEntry`: the file's types. `Endpoint.logged_out` is typed `LoggedOutEntry | None`.
- `classify(response, recorded) -> LoggedOutEntry`: decides the class from the response facts and the logged-in recording.
- `summarize(logged_out) -> LoggedOutSummary`: the only place the ranking and the run-level warnings live. It holds the count per class; the suggested check, which is the probed GET with the smallest recorded body whose class is `rejected` or a `login_page` with reason `login form`, else the `empty_data` GraphQL query with the fewest emptied paths, else none; and the bot-defence warning when every probed endpoint is `rejected`. The digest's `## Logged out` section, `gp plugin new`'s `_print_next_steps`, and the probe command's closing line all print its result and compute nothing themselves.
- `read_logged_out(run_dir) -> LoggedOut | None`: the file's only reader. It calls `refuse_unknown_schema("logged_out", ...)`; on `UnknownSchemaError` it returns `None` with the reason, and the digest prints one line saying the file was written by a newer graftpunk and is ignored, and renders the run as unprobed.

`observe/probe.py` (new) owns what touches the network or writes the file:

- `select(digest, har, *, recorded_values) -> ProbePlan`: the replay list and the skip list with reasons. `recorded_values` is the mapping of flagged cookie and token-candidate names to their recorded values, computed by the CLI command (which already imports `graftpunk.testing.recorded`), so `observe/` gains no dependency on `testing/`.
- `send(plan, *, session_factory) -> list[ProbeResponse]`: the only code that touches the network. Tests pass a session factory that mounts the local server.
- `write_logged_out(run_dir, entries)`: the file's only writer, with `schema` from `current_schema("logged_out")`.

`cli/observe_commands.py` gains the `probe` command, which wires these together and holds no logic of its own.

`har/paths.py` gains public `is_auth_path(path)` and `is_logout_path(path)`, moved from the private `_AUTH_URL_REGEX` and `_LOGOUT_URL_REGEX` in `har/digest.py`, which imports them; the selection and the classifier use them too.

`contracts.py` gains the surface `logged_out` with `LOGGED_OUT_SCHEMA = 1` in `Surface` and `_CURRENT`, and not in `CLI_SURFACES`: like the sidecar, it is read by graftpunk itself, never by a caller through a `gp` command.

> **Design note (2026-11-27):** this section was added in response to the design review, and its first version was revised after a second review. Each part has one owner; the pure parts (the class names, the file's types and reader, the classifier, and the ranking) sit in `har/`, so `har/` keeps importing nothing from `observe/`; the network and the writer sit in `observe/`; the auth and logout paths are public predicates in `har/paths.py`; and the file's schema number is declared in `contracts.py` with the other surfaces.

## Command

`gp observe probe [SESSION] [RUN_ID]` reads an existing run and writes one file into it. A separate command, not a flag on `go`, because recordings come from `go`, `interactive`, and `gp http --observe`, and a probe can be rerun without re-recording. It needs no browser and loads no cached session: the session argument only names the run directory. The digest stays a pure function over files and sends nothing.

Options: `--yes` (skip the confirmation), `--limit N` (default 50), `--delay SECONDS` (default 1.0), and `--dry-run` (print the plan and exit).

## Which requests are replayed

Selection starts from the run's digest: one request per endpoint row, the row's `fixture_entry_index` recording. Only hosts in the digest's primary scope are probed. A row is replayed when it is one of these:

- A GET whose path is not an auth or logout path, whose row is not `login_flow`, and whose path holds no segment from this list: `unsubscribe`, `confirm`, `verify`, `activate`, `delete`, `remove`, `cancel`, `approve`, and `reset`. A GET whose `query` parameter parses as a GraphQL document holding any mutation is skipped.
- A POST GraphQL request whose every operation is a `query`. A new helper in `har/graphql.py` returns the kind of every definition in a document, and `document_operation` is rewritten to take the first of its answer, so one parser reads the document: a document is replayed only when every non-fragment definition is `query`, and a batch only when every element is. A persisted query sent by hash is never replayed, because its kind cannot be read from the run. Nothing else that is not a GET is replayed.

A selected request is still skipped when any path segment or query value equals a value `flagged_values_of` (`har/digest.py`) records for a flagged cookie (it covers only flagged names, with `None` where the values are not all known), or a value the recording holds for a token candidate's name (`recorded_values`, `testing/recorded.py`; the digest's `TokenCandidate` keeps no value), or looks like a JWT. Every skip is listed with its reason in the plan and in the output file.

## How a request is sent

- A fresh `requests.Session` with `trust_env=False`, so `~/.netrc` adds no credentials. Each request passes `proxies=requests.utils.get_environ_proxies(url)`, which reads every `*_PROXY` variable (`ALL_PROXY` included) and `NO_PROXY`, and on macOS falls back to the system proxy settings, plus `verify=` from `REQUESTS_CA_BUNDLE` or `CURL_CA_BUNDLE` when set, so the probe reaches the site the way a plugin session (which keeps `trust_env=True`) does. The output records whether a proxy was used, never its URL.
- A cookie jar whose policy accepts no cookie, so a `Set-Cookie` from one probe is never sent on the next.
- `session.headers` is cleared (a fresh `requests.Session` otherwise sends `Accept-Encoding`, `Accept: */*`, and `Connection` from `requests.utils.default_headers()`), and the only headers set are the recorded `User-Agent`, `Accept`, and `Accept-Language`, plus `Content-Type: application/json` on a GraphQL POST. The transport adds `Host`, and `Content-Length` on a POST. `logged-out.json` lists every header name on the prepared request. No recorded `Cookie`, `Authorization`, `Proxy-Authorization`, `Referer`, `Origin`, or header from the run's custom header set is sent, whatever the recording held. A `Proxy-Authorization` that `requests` builds from a credentialed proxy URL goes to the proxy only. The recorded browser `User-Agent` is used so the probe gets the answers a plugin would get.
- A GraphQL body is the recorded body.
- Redirects are followed by hand, at most 5, and only to in-scope hosts; an off-scope target is recorded and not requested.
- Timeout 10 seconds; the body read stops at 256 KiB.
- One request at a time, `--delay` apart, up to `--limit`. A 429, or a 503 with `Retry-After`, stops the run, and the rest are recorded as `not_probed: rate_limited`.

## What is recorded

One file, `logged-out.json` in the run directory, written with `write_private_file`. It holds `schema`, read through `current_schema("logged_out")`, the time, the names of the headers sent, whether a proxy was used, and one entry per probed endpoint holding its `endpoint_key` (`har/digest.py`) and the HAR entry index of the replayed request (the row's `fixture_entry_index`). The digest joins an entry to its row by HAR index, which does not change across graftpunk versions, and falls back to `endpoint_key`; an entry that joins neither way is reported as one warning line naming the count. Each entry holds the first status; the first hop's `Location` as scheme, host, and templated path with the query dropped; the final status, media type, and body length; and a class with a one-line reason. It never holds a body, a header value, or a query value. Skipped and not-probed endpoints carry their reason.

Classes, decided in this order on the final response:

- `rejected`: 401 or 403.
- `redirect_offsite`: the chain left the scope; the target host is kept.
- `login_page`: an HTML body that `is_login_document` accepts (reason `login form`), or a chain that ended on a path `is_auth_path` matches (reason `auth path`).
- `rate_limited`, `not_found` (404), and `server_error` (5xx), recorded as they are.
- `graphql_error`: JSON whose top-level object has a non-empty `errors` array with `data` null or absent.
- `empty_data`: JSON where at least one path (depth 3, key names only) held a non-empty value in the logged-in recording and is null, `[]`, `{}`, or missing logged out, and no such path holds a non-empty value. The emptied paths are stored when their names pass `printable_name`, for example `data.viewer`.
- `html_for_non_html`: an HTML body that is not a login page, where the logged-in recording was not HTML.
- `same_as_logged_in`: JSON whose `ShapeNode` equals the logged-in recording's to depth 3, compared structurally (not through `summarize_shape`, which is display code), and that is not `empty_data`. The endpoint is public and cannot tell a dead session.
- `other`: anything left, with its media type.

The design posted on #376 had two more rules, both dropped because each guessed: an app-shell fingerprint (a hash of `<title>` and script `src` list compared across paths), and an error rule for an object holding only `error`, `message`, or `code` keys. Answers those rules caught are `other` with their media type.

A 403 from bot defence looks the same as a 403 for a missing session. When every probed endpoint is `rejected`, `summarize` carries a warning that the site may be refusing the client rather than the session, and every place that prints the summary prints it.

## How the digest states it

`DigestSource.from_run_dir` gains `logged_out: Path | None`, declared with `OMIT_EMPTY` metadata so `render_json`'s `source` object is unchanged, and `Endpoint` gains `logged_out` (`OMIT_EMPTY`), so a run without the file renders byte for byte as before, in text, `--json`, and `--endpoints-json`. `logged_out` is added to schema 1 projections (runs with no GraphQL row) as well as schema 2. Text adds one line to each probed endpoint block, for example `- logged out: 200 application/json, empty_data (data.viewer null)` or `- logged out: 302 to /login, login_page`, and a `## Logged out` section with the count per class, the suggested check, and any warning, all from `summarize`. `--json` carries the object. `--endpoints-json` carries `logged_out` on probed entries only, within the current endpoints schema: the field is optional and changes no existing field, and the one reader, the graft skill, reads the fields it knows.

## What the scaffold suggests

Each stub gets one comment from its class:

- `rejected`, or a `login_page` with reason `login form` on a `request_json` stub: no check is needed, because `request_json` already raises `SessionRejectedError`.
- A `login_page` with reason `auth path`: a `GP-FILL` saying `request_json` raises `UnexpectedResponseError` there (the page has no password form for `is_login_document` to find), and the check to write.
- `empty_data`: a `GP-FILL` with the check written out, for example `if body["data"]["viewer"] is None: raise SessionRejectedError(...)`.
- `graphql_error`: a `GP-FILL` saying a logged-out call answers with GraphQL `errors`, and the check to write.
- `html_for_non_html` on a `request_text` stub: a `GP-FILL` saying status alone will not catch a dead session there.
- `same_as_logged_in`: the endpoint is public.

`_print_next_steps` prints the suggested check from `summarize` (see Structure) and ranks nothing itself.

## Safety and privacy

Before replaying, the command prints the host, the number of requests, each method and templated path, and the skip list, then this warning, and asks y/N: the requests go to the live site from this machine; no cookies or credentials are sent; URLs and GraphQL variables are sent as recorded and may hold account ids; and the site may log them or rate-limit this address. `--dry-run` prints the same plan and sends nothing. Nothing from the responses is stored beyond the fields above. The graft skill's consent rule for live calls (`skills/graft/SKILL.md`) applies: the skill asks the user before it runs `probe`.

## Tests

A local `ThreadingHTTPServer` like `tests/unit/observe_http_site.py`, with a synthetic `network.har` for `https://myshop.example`. A test adapter mounted through the probe's session factory sends every `myshop.example` connection to the local server, so nothing leaves the machine. Routes: `/api/me` 401; `/graphql` answering a query with `{"data": {"viewer": null, "orders": []}}` and another with `{"errors": [...], "data": null}`; `/print/1` HTML where the recording held JSON; `/download/1` 403; `/account` 302 to a login form; `/api/catalog` the same JSON logged in and out; and a route that answers 429. The HAR also holds a mutation, a document with a query and a mutation, a persisted query, a DELETE, `/logout`, and a GET whose query carries the recorded cookie value.

Assertions: each class is as listed; the server log never shows the skipped requests and never shows a `Cookie`, `Authorization`, or recorded custom header; the 429 stops the run; `logged-out.json` is 0600 and holds no body text, cookie value, or proxy URL; with `HTTPS_PROXY` set the request goes through the proxy, and with the host in `NO_PROXY` it does not; a `~/.netrc` in a temporary `HOME` adds no `Authorization`; the server log shows no `Accept-Encoding` or `Connection` header the recording did not hold; a redirect to an auth path with no password form is `login_page` with reason `auth path` and its stub gets the `UnexpectedResponseError` `GP-FILL`; the digest and `gp plugin new` print the same suggested check for the same run; `har/` imports nothing from `observe/` (an import test); a `logged-out.json` whose `schema` is newer than this graftpunk's is ignored with one line; an entry whose `endpoint_key` no longer matches still joins by HAR index; `--json` for a run without the file is byte for byte unchanged; the digest text and JSON carry the new lines; a run without the file digests unchanged against the existing fixtures; and the scaffold emits the expected comments.

## Out of scope

- A declarative session check on a plugin, run by graftpunk: #407, written after the probe has run on real sites.
- Replaying anything that is not a GET or a GraphQL query.
- A browser-based logged-out probe for pages that need JavaScript.

## Decisions (repository owner, 2026-11-27, recorded on #376)

1. A separate `gp observe probe` command, not `gp observe go --logged-out-probe`.
2. GraphQL POST queries are replayed only when every operation is a `query`; never a mutation, a mixed document, or a persisted query.
3. The recorded browser `User-Agent`.
4. `logged_out` is added within the current endpoints schema.
5. Defaults: 50 requests, 1 second apart, stop at the first 429 or 503 with `Retry-After`.
6. Proxies from the environment are passed through explicitly, with `trust_env=False` kept.
7. The declarative session check is its own spec, after the probe has run on real sites (#407).

Also agreed: the two guessing rules above are dropped.
