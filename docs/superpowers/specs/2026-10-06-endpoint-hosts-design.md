---
type: spec
---

# Per-endpoint hosts: a generated command calls the host its endpoint was recorded on

**Issues:** #214 (the generator), #225 (the projection). **Date:** 2026-10-06.

## The problem

The digest records each endpoint's host (`Endpoint.host`, spelled by `graftpunk.har.paths.normal_host`: lower case, no default port). Two surfaces drop it:

- `gp plugin new` and `gp plugin add-command` render every stub's request as a path, which `SiteRequests` joins to the plugin's `base_url` (`urljoin`, `src/graftpunk/plugins/site_requests.py`). `base_url` defaults to `https://{primary_host}`, and `_primary_host` prefers a host that served an HTML document. A site whose pages are on `myshop.example` and whose JSON is on `api.myshop.example` therefore gets commands that call `https://myshop.example/api/orders`. The fixture tests pass, because `FixtureSession` matches by path alone, and the first live call fails. Reproduced on 1.17.0 with a two-host HAR.
- `gp observe digest --endpoints-json` (schema `endpoints` 1) carries `primary_host` once and no host per endpoint, so a tool driving `gp` cannot see the split. The graft skill works around it by asking the user, on every run, whether the data comes from `primary_host`.

## The change

**One rule decides where a command's request goes, and it lives in the package.** A new function in `src/graftpunk/devtools/scaffold/render.py` (or `policy.py`, beside the other generation rules), `request_target(endpoint_host: str, base_url: str) -> str | None`, returns `None` when the endpoint's host is `base_url`'s host (compared with `normal_host` on both sides) and otherwise the origin to prefix: `base_url`'s scheme, `://`, and the endpoint's host. Both writers call it: `gp plugin new` with the base URL it writes (`--url` or `https://{primary_host}`), and `gp plugin add-command` with the target plugin's `base_url` as the project reader reports it (`PluginView.base_url` in `src/graftpunk/devtools/plugin_project.py`, read from a string literal in the class body). When the reader has no `base_url` for the plugin (a hand-written plugin that sets none, or sets it from an expression), `add-command` writes an absolute `https://` URL for every endpoint, since nothing relative would resolve.

**Amended 2026-10-06:** `request_target` lives in `render.py`, takes `base_url: str | None`, and returns a `RequestTarget` record (the origin to prefix or `None`, the endpoint's host, and `base_url`'s host or `None`) instead of `str | None`, so the stub and the writers' line read one result; one predicate, `_on_base_host`, decides whether a host is `base_url`'s for both the stub's request and the login page's `url`; `gp plugin new` refuses a `--url` that is not an `http://` or `https://` URL with a valid host and port, so only `add-command` meets a plugin with no readable `base_url`; and the line is worded in `graftpunk.cli` (`print_other_host`), reading `<name> calls <host>; the plugin sets no base_url gp can read as a URL, so its request is an absolute URL` when there is no base host.

**A stub for an endpoint on another host requests an absolute URL.** `"https://api.myshop.example/api/orders/" + order_id` instead of `"/api/orders/" + order_id`, built by the same path templating (`_templated_url`) with the origin in front. `urljoin` passes an absolute URL through unchanged, so no runtime change is needed. The `endpoint=` declaration stays `"METHOD template"` (path only): it identifies the endpoint for tooling, and the host lives in the request it declares. The generated test is unchanged: `FixtureSession` matches the path, so the fixture is found.

**The projection carries each endpoint's host.** `endpoints_projection` adds `"host": endpoint.host` to each endpoint. This is additive within schema `endpoints` 1 (`src/graftpunk/contracts.py`: within one schema version, fields are added and never renamed or removed), so no version bump, and `primary_host` stays.

**The writers say when a command calls another host.** `gp plugin new` and `gp plugin add-command` print one line per such command, `<name> calls <host>, not <base host>; its request is an absolute URL`, so a developer sees the split without reading the module. It is information, not a `Next:` step.

## Known limits, stated rather than fixed here

- The digest keys endpoints by `(method, template)`, so one template recorded on two hosts is one endpoint carrying the first host seen. Splitting it is a digest-keying change with consequences for fixture names and declarations; out of scope.
- The digest does not record a scheme; another host gets `base_url`'s scheme. A site serving its API over plain `http` while its pages use `https` is not handled; none has been seen.
- A stub that calls another host still sends its role's headers. The `xhr` role sends `Sec-Fetch-Site: same-origin` and no `Origin` (`src/graftpunk/graftpunk_session.py`, around lines 111-118), which a browser would not send cross-site. Out of scope here.

## Also in this change (noted on #225)

- The `--all-hosts` help text in `src/graftpunk/cli/observe_commands.py` says "not just the primary one"; the default keeps the primary host's whole domain. It reads "not just the primary host's domain".
- The login `url` `GP-FILL` comment in `render.py` says "the path of the login page"; it adds "or its full URL when it is not on base_url's host".

## The skill, after the release

`SKILL_REQUIRES_GRAFTPUNK` rises to the release that ships this. The understand step drops the host question: the proposal shows a `host` column only when some row's host differs from `primary_host`, and the scaffold passes no `--url`. Enhance mode drops its `base_url` question, since `add-command` now targets each endpoint's host. The plugin version bumps.

## Testing

- `request_target`: same host (with and without a default port spelled), another host, a `base_url` with a path, no `base_url`.
- A two-host recording end to end (#214's acceptance): digest, `gp plugin new`, then the generated command called through `SiteRequests` with a recording session asserts the URL on the wire is the API host's, and the generated test passes against its fixture.
- `add-command` into a plugin whose `base_url` is the page host, for an endpoint on the API host; and into a plugin with no readable `base_url`.
- The projection: every endpoint has `host`, equal to `Endpoint.host`; the projection's schema stays 1.
- The writers' information line, for one other-host command and for none.
