# Writing a graftpunk plugin

This guide takes one site from a blank directory to an installed, tested plugin.
It is a how-to. For the reference description of each subsystem, see
[How graftpunk Works](HOW_IT_WORKS.md) and the [Reference](#reference) section at
the end.

Every example uses placeholder names: the site is `myshop` at
`https://myshop.example`, the account is `alice@example.com`.

## What a plugin is

A plugin is a Python package that registers one class on the `graftpunk.plugins`
entry-point group. The class subclasses `SitePlugin` and declares what the site
is and what you want to do with it:

```python
from graftpunk.plugins import CommandContext, SitePlugin, command


class MyshopPlugin(SitePlugin):
    site_name = "myshop"
    session_name = "myshop"
    help_text = "Commands for myshop"
    base_url = "https://myshop.example"
    backend = "nodriver"
    api_version = 1

    @command(help="List recent orders")
    def orders(self, ctx: CommandContext) -> dict:
        return ctx.request_json("GET", "/api/orders")
```

The attributes that matter:

- `site_name` is the CLI command group: this plugin is `gp myshop`.
- `session_name` is the base name of the cached session. It defaults to
  `site_name` and is a base, not a storage key: one login per account produces a
  slot per identifier: `myshop@alice-example-com` beside `myshop@bob-example-com`.
- `base_url` is what a relative URL in `ctx.request_json` resolves against.
- `backend` is `"selenium"` or `"nodriver"`. The `SitePlugin` class default is
  `"selenium"`; `gp plugin new` writes `"nodriver"`.
- `api_version` is `1`.
- `requires_session` is `True`. Set it to `False` for a plugin whose commands
  need no cached session.
- `login_config` and `token_config` are optional, and are covered in
  [Login](#login) and in [How graftpunk Works](HOW_IT_WORKS.md#token-and-csrf-support).

Registration happens in the plugin's own `pyproject.toml`:

```toml
[project.entry-points."graftpunk.plugins"]
myshop = "graftpunk_myshop.plugin:MyshopPlugin"
```

### Register through an entry point, not through the plugins directory

graftpunk finds plugins from three sources. The first is the entry-point group
above. The other two are a file loader that imports every `*.yaml` and `*.yml`,
and every `*.py` whose name does not start with an underscore, under
`~/.config/graftpunk/plugins/` (or under `$GRAFTPUNK_CONFIG_DIR/plugins/` when
that variable is set).

Use the entry point. Build the plugin as its own distribution and install it
editable into that project's virtual environment:

```bash
uv pip install -e .   # or: pip install -e .
```

The file loader is for a throwaway script that imports nothing but graftpunk.
It has two failure modes that cost real time:

- A loose or symlinked file is imported into whichever interpreter is running
  `gp`. If the file imports a package that interpreter does not have, every
  `gp` invocation in every other environment prints the import error and a
  traceback, including `gp --help`. The other plugins still load, so the
  breakage is noise rather than an outage, and it follows you everywhere.
- A symlink pointing into a working checkout serves whatever is on that branch
  right now. You get stale or half-finished code with no indication that it is
  not what you installed.

After installing, check which file is actually being imported:

```bash
python -c "import graftpunk_myshop; print(graftpunk_myshop.__file__)"
```

The path it prints must be the project you are editing. If it points into a
`site-packages` copy, the editable install did not take and your edits are not
the code being run.

## The six steps at a glance

1. [Frame](#frame): pick the name, the commands, the account, and the login shape.
2. [Capture](#capture): record a real browser session against the site.
3. [Understand](#understand): read the recording into a digest of endpoints, login, and tokens.
4. [Scaffold](#scaffold): generate a project filled in from that digest.
5. [Implement](#implement): fill the stubs and make the commands do the real work.
6. [Harden](#harden): tests against fixtures, a green gate, and a check before you publish.

Steps 2 and 3 repeat. One recording rarely covers every flow, and a second
recording aimed at one flow is the fastest way to answer a question the first
digest left open.

## Frame

Decide the following before you record anything.

**The name.** It starts with a letter, uses letters, digits, hyphens, and
underscores, and is at most 40 characters. It cannot be a reserved top-level
`gp` command name (`plugin`, `plugins`, `session`, `http`, `config`,
`keepalive`, `observe`, `version`, and anything else registered by the time
plugins attach). A hyphenated name maps to an importable package: `my-shop` becomes the
package `graftpunk_my_shop`, the class `MyShopPlugin`, the entry-point key
`my-shop`, and the CLI command `gp my-shop`.

**The commands.** Write down what you want to get out of the site, in the words
you would use at the shell: `gp myshop orders`, `gp myshop order --order-id
1001`. This list decides which flows you need to exercise while recording.

**The account.** One account per session slot. If the site distinguishes
accounts you care about, plan on logging in as each of them; graftpunk caches
them side by side as `myshop@alice-example-com` and `myshop@bob-example-com`.
`gp myshop login --as alice` names the slot explicitly when the derived label is
not the one you want, which is how you get a short label such as
`myshop@alice`.

**The login shape.** Five shapes come up:

- A plain form: one page, a username field, a password field, a submit button.
  Declarative `LoginConfig` handles it.
- An identifier-first form: the username page submits, then the password page
  appears. Two `LoginStep` entries handle it.
- An identity-provider redirect: submitting sends the browser through one or
  more other hosts before it lands back on the site. Declarative login handles
  it, but see [Login](#login) for `success_url` and `timeout`.
- MFA or a CAPTCHA: the flow needs a human. Leave the browser window visible
  (`headless=False`, which is the default) and let the person solve it.
- No login at all: a site whose data needs no session. Set `requires_session =
  False` on the plugin, leave `login_config` unset, and skip [Login](#login)
  entirely.

**The backend.** Use `nodriver` unless you have a reason not to; it drives
Chrome over the DevTools Protocol with no WebDriver in the picture, which is
what sites with aggressive bot protection look for. Choose `selenium` when you
are writing a login by hand against a synchronous WebDriver API, or when you
need `driver.get_log()` style diagnostics. Both are described in
[How graftpunk Works](HOW_IT_WORKS.md#browser-backends).

## Capture

Record a real browser session against the site:

```bash
gp observe --no-session interactive https://myshop.example/
```

`--session` and `--no-session` belong to the `observe` group, so they go before
the subcommand, and they cannot be used together. `--no-session` opens the
browser with no cached cookies, which is what you want the first time: you are
recording the login as well as the flows behind it, and there is no cached
session to name yet. A `--no-session` recording is filed under the name
graftpunk infers from the host, the second-to-last label: `myshop` for
`myshop.example` or `www.myshop.example`, `example` for `myshop.example.com`. `gp
observe list` prints it. For the host this guide uses, that name is `myshop`, so
the commands below work as written; for a host whose inferred name differs,
substitute the one `gp observe list` printed. Once the plugin exists and `gp
myshop login` has cached a session, `gp observe -s myshop interactive ...`
records with that session's cookies and files the run under `myshop`.

A browser opens at the URL. Log in. Then exercise every flow you listed in
[Frame](#frame): open each page, page through a list, apply a filter, download
an export. The recording is only as good as what you clicked. Press Ctrl+C in
the terminal to stop and save.

One recording is usually enough. Take several when the flows are genuinely
separate, or when a first digest leaves a question you can answer by recording
one flow on its own with nothing else in the way.

Runs live under `~/.local/share/graftpunk/observe/<name>/<run id>/`, where
`<name>` is the session when you named one and the name inferred from the host
otherwise. A run holds `network.har` plus whatever else that run captured:
`bodies/` for response bodies too large to inline in the HAR,
`page-source.html`, `screenshots/`, `console.jsonl`, `events.jsonl`, and
`metadata.json`.

```bash
gp observe list           # every session and its runs
gp observe show myshop    # the newest run for this name
```

A capture holds cookies, tokens, session identifiers, and whatever account data
the pages showed. Treat one as a credential. Never commit one, and never paste
one into an issue. `gp observe fixtures`, the command that derives test files
from a run, is built around that rule: it adds its target directory to
`.gitignore`, refuses to write onto a path git already tracks unless you pass
`--allow-tracked`, and warns when it is writing somewhere no work tree protects.

## Understand

Read the run into a digest:

```bash
gp observe digest myshop
```

It takes the recording's name and, optionally, a run id; without one it reads
the newest run. `--har PATH` digests a bare HAR file from any tool instead of a
run. `--json` prints the complete model rather than the markdown summary,
`--all-hosts` models every host instead of just the primary one, `--limit N`
raises the cap on how many endpoints the markdown form lists (60 by default),
and `--output PATH` writes to a file.

The digest is redacted by construction. It records header names, cookie names,
form field names, query parameter names and observed types, and response
shapes. It never retains a header value, a cookie value, a query value, or a
body value. Path segments are the exception: the example paths under each
endpoint are real, so a digest of a site whose URLs carry account or document
identifiers is not safe to paste anywhere a capture would not be.

Here is the output from a recording of `myshop`, with three non-JSON endpoint
blocks elided:

```text
# Observe digest

## Summary
- source: `myshop/20260915-100000-1`
- primary host: `myshop.example`
- hosts: 2, endpoints: 5
- dropped: static=2, third_party=0, other_scheme=0, error=0

## Login
1. GET https://myshop.example/login [200] form_page
2. POST https://myshop.example/session [302] credential_post (email, password) -> /dashboard

- form at `/session` (POST), source: https://myshop.example/login
    - password: `#password`
    - username: `#email`

## Tokens
- header `X-Csrf-Token`, seen on: GET /api/orders, GET /api/orders/{order_id}

## Cookies
myshop_session

## Endpoints
### GET /api/orders

- host: `myshop.example`, count: 2, statuses: [200]
- content type: `application/json`
- query params: archived: bool, page: int, per_page: int
- custom headers: X-Csrf-Token
- shape: object{orders, page, total}
- examples: /api/orders

### GET /api/orders/{order_id}

- host: `myshop.example`, count: 2, statuses: [200]
- content type: `application/json`
- custom headers: X-Csrf-Token
- shape: object{id, items, placed_on, total}
- examples: /api/orders/1001, /api/orders/1002

## Other hosts
- `analytics.example.net`: 1 request(s)
```

How to read it:

**Summary** names the run, the host the digest decided the recording was
against, and the counts. The `dropped` line is the honest part: `static` counts
assets and trackers, `third_party` counts requests to hosts outside the primary
host's domain, `other_scheme` counts non-HTTP entries such as the browser's own
new-tab page, and `error` counts entries the parser could not read. A large
`third_party` number on a site you know is single-host means the primary host
was decided wrong; rerun with `--all-hosts` and look again.

**Login** is the credential post and the entries around it, in order, with field
names but never field values, plus the CSS selectors of any login form found in
the captured HTML. This is what fills in `LoginConfig`. The `-> /dashboard` on a
redirecting credential post is where the login landed, and is what
`success_url` is derived from.

**Tokens** lists header, meta-tag, and cookie candidates whose names look like
tokens, and the endpoints each was seen on. A candidate is a name, not a value,
and not yet a decision: see [How graftpunk Works](HOW_IT_WORKS.md#token-and-csrf-support)
for turning one into a `Token`.

**Cookies** lists the cookie names the recording saw on the primary host, names
only.

**Endpoints** is one block per method and templated path. Path segments that
look like opaque identifiers collapse into named parameters, so five requests
for five order ids become one `GET /api/orders/{order_id}` with a count of five.
Each block carries the observed statuses, the content type, the query and body
parameter names with the types the recording showed, the names of any
non-standard request headers, a summary of the JSON response shape, and up to
three real example paths.

What the digest cannot tell you:

- What a parameter means. `archived: bool` says the site sent `archived=false`,
  not what archiving does or what `true` returns.
- How pagination terminates. You can see `page` and `per_page`; whether the
  last page is an empty list or a 404 is something you have to try.
- Which endpoint you actually want. A page load fans out into a dozen requests,
  and the one carrying the data you saw on screen is not always the one with the
  highest count.
- Anything about a flow you did not exercise. An endpoint that is not in the
  recording is not in the digest.

The fastest way to answer any of these is another recording aimed at one thing.
Record a session where you do nothing but page to the end of the order list, and
the digest of that run answers the pagination question with no other traffic in
the way.

## Scaffold

Generate the project from the run:

```bash
gp plugin new myshop --from-run myshop
```

The two `myshop`s are different arguments that happen to coincide here. The
first is the plugin name you chose in [Frame](#frame); the second is the
recording's name from `gp observe list`.

The options:

- `--from-run SESSION` fills the scaffold from that recording's newest run.
- `--run RUN_ID` picks a specific run instead of the newest. It requires
  `--from-run`.
- `--url URL` sets `base_url`. Without `--from-run` it is the only source of
  one. With `--from-run`, `base_url` comes from the digest's primary host, and
  an explicit `--url` overrides it (use that when the recording's busiest host
  is a CDN or an API subdomain you do not want as the base).
- `--dir PATH` is the target directory (the working directory by default).
- `--backend nodriver|selenium` sets the generated `backend` attribute
  (`nodriver` by default).
- `--new` skips the suite check on `--dir`, so the command writes a new project
  instead of adding to the suite it finds there. Nothing is overwritten either
  way, so a `--dir` that already holds a `pyproject.toml` is refused for the
  conflict: point `--dir` at a directory that has none.

### The two modes

With no `pyproject.toml` in `--dir`, it writes a new project:

```text
.gitignore
README.md
pyproject.toml
src/graftpunk_myshop/__init__.py
src/graftpunk_myshop/plugin.py
tests/conftest.py
tests/fixtures/.gitkeep
tests/test_plugin.py
```

With a `pyproject.toml` that already declares the `graftpunk.plugins`
entry-point group, it adds a plugin to that suite instead, writing only the new
files and editing the existing `pyproject.toml`:

```text
src/graftpunk_otherstore/__init__.py
src/graftpunk_otherstore/plugin.py
tests/fixtures/otherstore/.gitkeep
tests/test_otherstore.py
```

The edit adds the entry point and the wheel package:

```toml
[project.entry-points."graftpunk.plugins"]
myshop = "graftpunk_myshop.plugin:MyshopPlugin"
otherstore = "graftpunk_otherstore.plugin:OtherstorePlugin"

[tool.hatch.build.targets.wheel]
packages = ["src/graftpunk_myshop", "src/graftpunk_otherstore"]
```

Suite mode gives each plugin its own `tests/fixtures/<name>/` directory, because
fixture filenames are derived from endpoint paths and two plugins in one suite
can share a path. It does not rewrite `tests/conftest.py`, which still
scrubs only the first plugin's environment prefix; add a line for the new one
(see [Secrets and configuration](#secrets-and-configuration)).

A directory holding a `pyproject.toml` that is not a plugin suite is refused
with a message saying so. Nothing is overwritten, ever: if any target path
exists, the command refuses and writes nothing at all.

### What gets filled in

From the digest above, `src/graftpunk_myshop/plugin.py` comes out as this, with
the `api_orders_by_order_id` and `dashboard` stubs elided:

```python
"""myshop plugin.

Verified against a real account on: (none yet)
"""

from __future__ import annotations

from graftpunk.plugins import (
    CommandContext,
    LoginConfig,
    LoginStep,
    PluginParamSpec,
    SitePlugin,
    command,
)


class MyshopPlugin(SitePlugin):
    """Commands for https://myshop.example."""

    site_name = "myshop"
    session_name = "myshop"
    help_text = "Commands for myshop"
    base_url = "https://myshop.example"
    backend = "nodriver"
    api_version = 1

    login_config = LoginConfig(
        steps=[
            LoginStep(
                fields={
                    "password": "#password",
                    "username": "#email",
                },
                submit="#sign-in",
            ),
        ],
        url="/session",
        failure="GP-FILL: text on the page indicating login failure",
        # GP-FILL: success, a CSS selector for an element that is on the page this login lands on
        #   and not on the login form itself.
        success_url="*/dashboard*",
    )

    # token_config = TokenConfig(tokens=[Token.from_meta_tag(name="...", header="...")])
    # GP-FILL: unpaired token candidate: header 'X-Csrf-Token'

    @command(
        help="GP-FILL: describe api_orders",
        params=[
            PluginParamSpec.option("archived", type=bool, click_kwargs={"is_flag": True}),
            PluginParamSpec.option("page", type=int),
            PluginParamSpec.option("per_page", type=int),
        ],
        endpoint="GET /api/orders",
    )
    def api_orders(
        self,
        ctx: CommandContext,
        archived: bool | None = None,
        page: int | None = None,
        per_page: int | None = None,
    ) -> dict:
        """
        GET /api/orders: seen 2 time(s) in run myshop/20260915-100000-1.

        Shape: object{orders, page, total}.
        """
        # This request is the endpoint= declared on @command above: change both together.
        return ctx.request_json(
            "GET",
            "/api/orders",
            role="xhr",
            params={
                "archived": archived,
                "page": page,
                "per_page": per_page,
            },
            headers={
                "X-Csrf-Token": "GP-FILL",
            },
        )
```

What came from the digest: `base_url` from the primary host; the `LoginStep`
selectors and the form's action URL from the captured login page, the submit
selector too, which the digest's markdown form does not print; `success_url`
from the redirect the credential post answered with; one command stub per
endpoint, the login flow's own endpoints excluded, JSON endpoints first, up to
twelve, each with the observed query parameters as typed keyword arguments, an
explicit `params=` list whenever one of them is an `int` or a `bool` (see
[CLI parameter types](#cli-parameter-types)), the observed custom headers, and
the endpoint it calls declared as `endpoint=` on its decorator; a docstring
recording the method, the path, how many times it was seen, which run it came
from, and the response shape.

Everything the digest could not decide carries a `GP-FILL` marker: the failure
text (nobody recorded a failed login), the success selector, the help text for
each command, and the value of any custom header. The command names themselves
carry no marker, because the generator derives them from the endpoint path and
they are usually wrong for a human to type: see [Check the CLI surface you
shipped](#check-the-cli-surface-you-shipped). A token candidate that could not
be paired with a source is left as a commented `GP-FILL` line rather than a
guess. Search for `GP-FILL` and you have your to-do list.

The `endpoint=` keyword is a declaration, not a check: nothing compares it with
the request below it, and it is never used when the command runs. Tooling that
reads the plugin's source takes the endpoint from it, so when you change the
request, change the declaration with it.

The command also prints the fixtures the generated tests will look for:

```text
Next: the endpoint tests fail until these fixtures exist:
  tests/fixtures/get_api_orders.json
  tests/fixtures/get_api_orders_{order_id}.json
  tests/fixtures/get_dashboard.html
Derive each one from a capture of the same name: gp observe fixtures --help
```

Until those files exist, the generated suite fails. That is deliberate: a
generated test that passed against nothing would be worse than one that fails.

Every line the generator emits is either a fact about the site (a URL, a
selector, a parameter name, a header name) or a call into graftpunk's public
API. None of graftpunk's own logic is copied into your project, so there is
nothing in a generated file that has to stay in step with a graftpunk release.
Edit any of it freely; rename the commands, merge two stubs, delete the ones you
do not want.

## Implement

### Making requests

Command handlers call two `CommandContext` methods rather than touching
`ctx.session` directly:

```text
ctx.request_json(method, url, *, role="xhr", **kwargs)          -> parsed JSON
ctx.request_text(method, url, *, role="navigation", **kwargs)   -> the body as text
```

Both take a URL relative to `base_url` or an absolute one, and pass everything
else through to `requests`. The `role` decides which set of browser headers the
request carries: `"xhr"` for a site's own JSON endpoints, `"navigation"` for a
page a browser would load, `"form"` for a form submission. Those three are the
built-ins, and a role name can be any string a plugin has registered with
`graftpunk.register_role(name, headers)`.

Both normalise a `params` or `data` mapping before sending it:

- `True` and `False` go out as `true` and `false`. Python's own `str(True)`
  produces `True`, which a site that recorded `archived=false` does not
  recognise.
- A key whose value is `None` is dropped from the request entirely. This is what
  makes a generated stub's `archived: bool | None = None` mean "leave this
  parameter out unless the caller asked for it", rather than sending an empty
  value the site never saw.

### What they raise, and what the user sees

- `SessionRejectedError` on a 401 or 403 from either method, and, from
  `request_json` only, on a 2xx whose body is a login page (a stale session's
  tell). Its message names the status, the method and path, and the command to
  run: `Run: gp myshop login`.
- `UnexpectedResponseError` on a 2xx that is neither JSON nor a login page, or
  that declares JSON and does not parse as JSON (a truncated response).
- `CommandError` on any other 4xx or 5xx.

`SessionRejectedError` and `UnexpectedResponseError` are `CommandError`
subclasses, and the CLI prints a `CommandError`'s message as one error line and
exits 1, with no traceback. A `PluginError` prints
as `Plugin error: <message>` and exits 1. Anything else is a crash: the CLI logs
a full traceback and prints `Command failed: <message>`.

So raise `CommandError` for a failure the user can act on, and `PluginError` for
a broken plugin or configuration. Never raise a bare `ValueError` from a
handler; it reaches the user as a crash report for what is usually a bad
argument.

```python
from graftpunk.exceptions import CommandError
from graftpunk.plugins import CommandContext, command


@command(help="One order by id")
def order(self, ctx: CommandContext, order_id: str) -> dict:
    if not order_id.isdigit():
        raise CommandError(f"Order id must be numeric, got {order_id!r}")
    return ctx.request_json("GET", f"/api/orders/{order_id}")
```

### CLI parameter types

A handler's parameters become CLI options automatically, but the type is carried
through only when the introspector is handed a real type object: a bare `int`,
`float`, `bool`, or `str`. The plugin module that `gp plugin new` writes starts with
`from __future__ import annotations`, which makes every annotation in the module
a string, so in a generated plugin every option arrives as a string, a bare
`page: int = 1` included. A union such as `int | None` arrives as a string with
or without that import. That is harmless when the value goes straight into
`params` (the site reads it as text anyway), and wrong as soon as you do
arithmetic on it. To get a real type, declare it explicitly: an explicit
`params=` list replaces introspection entirely, so it works in a generated
module as written. `gp plugin new` writes that explicit list itself for every
stub with an `int` or `bool` parameter. It writes a `bool` parameter as a flag
(`click_kwargs={"is_flag": True}`), because a `bool` option that is not a flag
is refused when the command is registered; with the flag left off, the handler
receives `None`.

```python
from graftpunk.plugins import CommandContext, PluginParamSpec, command


@command(
    help="List orders",
    params=[
        PluginParamSpec.option("page", type=int, default=1, help="Page number"),
        PluginParamSpec.option("archived", type=bool, default=False, help="Include archived"),
    ],
)
def orders(self, ctx: CommandContext, page: int = 1, archived: bool = False) -> dict:
    return ctx.request_json("GET", "/api/orders", params={"page": page, "archived": archived})
```

`PluginParamSpec.option` makes a `--flag`; `PluginParamSpec.argument` makes a
positional argument. A `bool` option with `default=False` becomes a real flag.
The introspector's handling of string and optional annotations is tracked in
issue #208.

### One filter, several spellings

Sites rename the same concept from endpoint to endpoint: the order list takes
`archived`, the export takes `include_archived`, the search takes `arch`. Do not
push that onto the user. Keep one name in your command signature and map it at
the call:

```python
_ARCHIVED_PARAM = {
    "/api/orders": "archived",
    "/api/orders/export": "include_archived",
    "/api/search": "arch",
}


def _archived_params(path: str, archived: bool) -> dict[str, object]:
    return {_ARCHIVED_PARAM[path]: archived}
```

A map like this is site knowledge, so it belongs in the plugin and it belongs in
a comment saying which recording it came from.

### When the JSON endpoint is bot-protected

Some sites serve their JSON endpoints behind protection that a replayed session
cannot get past, while the server-rendered page that shows the same data comes
back fine. Fetch the page and parse it:

```python
from graftpunk.plugins import CommandContext, command


@command(help="Orders, from the rendered page")
def orders(self, ctx: CommandContext) -> dict:
    html = ctx.request_text("GET", "/orders")
    return {"orders": parse_orders(html)}
```

`request_text` decides rejection by status alone, so a real HTML 2xx body is
never mistaken for an expired session. Parsing is then your problem; see the
rule about empty lists in [Harden](#harden), which exists mostly for parsers
like this one.

### Producing files

When a command's job is to produce a file the user will feed to something else,
match the layout the site's own export produces: the same columns, in the same
order, with the same headers. Somebody downstream has a spreadsheet or a script
built on that layout, and a plugin that invents a tidier one is a plugin that
has to be un-invented later. The digest's record of the export endpoint tells
you what the real export looked like.

### Keep a lab notebook

`gp plugin new` puts a line at the top of the module for the date:

```python
"""myshop plugin.

Verified against a real account on: (none yet)
"""
```

Keep it current, and write underneath it what you actually confirmed against a
live account and what turned out to be wrong:

```python
"""myshop plugin.

Verified against a real account on: 2026-09-15.

- /api/orders paginates with page and per_page; per_page above 100 is clamped
  to 100 with no error.
- archived=true returns archived orders ONLY, not archived plus current, which
  is the opposite of what the digest's parameter name suggested.
- /api/orders/export ignores the date filters entirely and always returns the
  last 90 days.
"""
```

Six months later this is the difference between a five minute fix and another
recording session. Use absolute dates.

## Login

A declarative login is a `LoginConfig` holding one or more `LoginStep` entries,
executed in order by the login engine. `gp myshop login` is generated from it.

```python
from graftpunk.plugins import LoginConfig, LoginStep

login_config = LoginConfig(
    steps=[
        LoginStep(
            fields={"username": "#email", "password": "#password"},
            submit="#sign-in",
        ),
    ],
    url="/session",
    failure="Your email or password was incorrect.",
    success="#account-menu",
    success_url="*/dashboard*",
    timeout=30.0,
    settle=1.0,
    headless=False,
)
```

`LoginStep` fields:

- `fields` maps a credential name to the CSS selector of its input. The
  credential names are what [Secrets and configuration](#secrets-and-configuration)
  turns into environment variable names.
- `submit` is the CSS selector of the button to click after filling the fields.
- `wait_for` is a CSS selector to wait for before the step runs. Use it on a
  second step whose page appears only after the first step submits.
- `delay` is a pause in seconds after the submit click.

A step needs at least one of `fields` or `submit`, so a click-only step (a
"Continue" button with nothing to type) is just `LoginStep(submit="#continue")`.

`LoginConfig` fields:

- `steps`: the steps, in order. Required and non-empty.
- `url`: the login page, relative to `base_url`, or absolute when the login form
  lives on another host. Empty means use `base_url` itself.
- `failure`: text the site shows on a failed login.
- `success`: a CSS selector for an element that proves the login worked.
- `success_url`: a glob matched against the whole browser URL after the last
  step.
- `wait_for`: a CSS selector to wait for before any step runs.
- `headless`: run the login browser with no window. Defaults to `False`.
- `timeout`: seconds to wait after the last step for a success or failure
  signal. Defaults to `30.0`. Unused when no success signal is configured.
- `settle`: seconds to wait after the success signal and after the document
  finishes loading, before cookies are captured. Defaults to `1.0`. Unused when
  no success signal is configured.

### Getting the signals right

**`failure` must be the exact text the site shows.** The engine matches it
case-insensitively as a substring of the page, and nothing else. A paraphrase
never matches, so a failed login looks like a successful one, and graftpunk
caches a session that is not logged in. Every command then fails with a
rejection until somebody works out why. Log in once with a deliberately wrong
password and copy the sentence off the screen.

**`success` and `success_url` must not match the login page.** A success signal
that is also true before the login proves nothing, and the post-submit poll
will report success on its first pass and cache a pre-login session. Pick a
selector for something that exists only once you are in: the account menu, a
sign-out link, a greeting. For `success_url`, pick a glob narrow enough to
exclude the login URL. `*/dashboard*` is a good signal; `*myshop.example*`
is not.

A URL signal counts only once the URL differs from the one the last submit was
clicked from, which is a second guard against the same mistake. Set both
`success` and `success_url` and both have to hold.

**`timeout` is the whole post-submit budget.** After the last step, the engine
checks the page every half second until the timeout: the failure text ends the
wait as a failure, the configured success signal ends it as a success. A page
reading `Too Many Requests` ends the wait as a failure on any pass that read the
page text, and the last pass before a timeout always reads it, so a login that
gives up against a rate limiter names the limiter rather than your selector,
unless that pass also found the `success` element, which is the one thing that
outranks the marker. The
budget also covers the wait for the document to finish loading, so a signal that
arrives right at the deadline is followed by no readiness wait at all. Raise
`timeout` for a login that goes through a slow identity-provider redirect chain.
Lower it to `3.0` if you want a login to give up quickly.

**A plugin with neither `success` nor `success_url` gets a three second window
rather than a poll.** There is no signal to wait for, so the engine watches for
the `failure` text for three seconds and then takes the login at its word. That
catches an error the site renders a moment after the submit, and it is much
weaker than a real success signal. Configure one. On that path `timeout` and
`settle` are not used at all: setting either changes nothing until a success
signal exists.

### Headless, MFA, and CAPTCHA

`headless` defaults to `False`, a visible window, because a login that needs a
human cannot be completed without one. Set `headless=True` only for a site that
needs neither an MFA prompt nor a CAPTCHA. Either way, `gp myshop login
--headless` and `gp myshop login --headful` override the setting for one
invocation. A login that normally runs headless is much easier to debug with
`--headful`.

### Identity-provider redirects

When submitting the form sends the browser to another host and back, declarative
login still works: the steps run on the form you configured, and the redirect
chain is just part of the post-submit wait. Two things matter. Set `url` to an
absolute URL when the form itself is on the provider's host rather than yours.
Set `success_url` to a glob that matches where the chain lands, not where it
passes through, and raise `timeout` above the default if the chain is slow.

### The session cache knows only what you typed

Sessions are cached per machine (with the default local storage backend, under
the graftpunk config directory) and shared by every project on that machine. The
account label on a slot is the identifier in the credentials you submitted,
slugified: `alice@example.com` gives `myshop@alice-example-com`. graftpunk never
asks the site which account is actually signed in.

That is fine until it is not. If the site can land you on a different account
than the credentials suggest (a shared login, an account switcher, an SSO tenant
that resolves elsewhere), the slot name is a claim nobody checked. When it
matters, write a command that asserts the identity against a live page and run
it after logging in:

```python
from graftpunk.plugins import CommandContext, command


@command(help="Who the cached session is logged in as")
def whoami(self, ctx: CommandContext) -> dict:
    profile = ctx.request_json("GET", "/api/profile")
    return {"email": profile["email"]}
```

## Secrets and configuration

Credentials come from environment variables. Nothing secret goes in the
repository, ever.

For each field name in a `LoginStep`, the login command looks for
`<SITE_NAME>_<FIELD_NAME>`. The site name is uppercased with hyphens and spaces
turned into underscores; the field name is only uppercased, so keep field names
to letters, digits, and underscores or the derived variable name will be one no
shell and no env file can set. A plugin named `myshop` with fields `username`
and `password` reads `MYSHOP_USERNAME` and `MYSHOP_PASSWORD`. A plugin named `my-shop` reads
`MY_SHOP_USERNAME`. Setting `username_envvar` or `password_envvar` on the plugin
overrides the name for that field. If nothing supplies a value, the command
prompts for it, masked for anything that looks like a secret.

### The workstation env file

Exporting credentials in a shell profile puts them in a plaintext file that
every process on the machine inherits. graftpunk's alternative is a per-machine
env file at `~/.config/graftpunk/env`, created 0600, managed with `gp config`:

```bash
gp config path                       # print the file's path
gp config list                       # every entry, raw (commands not evaluated)
gp config set MYSHOP_USERNAME alice@example.com
gp config get MYSHOP_USERNAME
gp config unset MYSHOP_USERNAME
gp config edit                       # open it in $VISUAL, else $EDITOR, else vi
```

Entries take two forms. `NAME=value` is a static: it is injected into the
environment at startup. `NAME=$(command)` is a command entry: the command runs
through `/bin/sh` at the moment something looks the value up, and its output is
the value. Nothing is stored but the command text.

That second form is how a secret manager's CLI supplies a credential without the
credential ever sitting in a file:

```bash
gp config set MYSHOP_PASSWORD '$(your-secret-tool read myshop/password)'
```

Single-quote it, or your shell evaluates the command at `set` time and stores
the output, which is exactly what you were avoiding. `gp config list` shows a
command entry as `[command]` and never runs it, and `gp config get NAME
--resolve` runs it and prints the result. A static entry has no such protection:
`gp config list` and a plain `gp config get NAME` both print its value verbatim,
which is the other reason to prefer the command form for anything secret.

A command entry runs only when a value is needed: a login, the first
access of an allowlisted setting, or a YAML plugin's `${VAR}` header expansion.
`gp --help` never triggers it, so having several sites configured this way does
not mean an approval prompt every time you use the CLI.

Precedence for a credential, highest first: the real environment, then the
workstation env file, then an interactive prompt. graftpunk's own `GRAFTPUNK_*`
settings have one more tier: real environment, workstation env file, a `.env` in
the working directory, then the field's default. An environment variable set to
the empty string counts as unset, so a stray `export MYSHOP_PASSWORD=` does not
shadow the file.

The full design is in
[docs/rfcs/2026-07-28-workstation-env.md](rfcs/2026-07-28-workstation-env.md).

### Resolve a secret by what it is, not by what it is labelled

When a secret manager holds the credential, select the field by its purpose or
its type, not by the label somebody typed when they saved the item. A site's own
registration form can put the answer to a security question in the field
labelled "PIN", and a vendor's importer will happily preserve that. Reading by
label gives you a value that looks right and fails on every login. Read by the
item's password field, its username field, or an explicitly typed field, and
verify once by logging in.

### Keep the developer's own environment out of the tests

A generated `tests/conftest.py` is an import and an assignment:

```python
from graftpunk.testing.plugin import site_env_scrubber

scrub_site_env = site_env_scrubber("MYSHOP_")
```

`site_env_scrubber(prefix)` returns an autouse pytest fixture that removes every
environment variable starting with `prefix` for the duration of each test and
restores them afterwards. Assigning it to a module-level name is what registers
it. Without it, a developer who has `MYSHOP_PASSWORD` in their environment gets
a green suite on code that fails for everybody else. Adding a plugin to an
existing suite does not update this file: add a line for the new prefix
yourself.

## Harden

### Test against fixtures, not against the site

`graftpunk.testing` answers a command's requests from files on disk:

```python
from pathlib import Path

from graftpunk.testing import fixture_context

from graftpunk_myshop.plugin import MyshopPlugin

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_orders_lists_every_order() -> None:
    ctx = fixture_context(
        FIXTURES_DIR,
        plugin_name="myshop",
        base_url="https://myshop.example",
    )
    result = MyshopPlugin().api_orders(ctx)
    assert [order["id"] for order in result["orders"]] == ["1001", "1002"]
```

`fixture_context(dir, **kwargs)` is `make_context(session=FixtureSession(dir),
**kwargs)`. `FixtureSession` is a session that never opens a socket: it answers
each request from the file in the fixtures directory named for that method and
templated path, the same name `gp observe fixtures` writes. `GET /api/orders`
reads `get_api_orders.json`; `GET /api/orders/1001` reads
`get_api_orders_{order_id}.json`. No matching file answers 404.

A `<filename>.meta.json` sidecar beside a fixture supplies its status and
content type. `gp observe fixtures` writes one for every capture to commit
beside the fixture. It holds no URL, no time, and no header, cookie, or query
value: only the status, the content type, the hash of the captured body, the
request's body parameter names, every cookie name the recording set, and the
token names the digest found. A body key that does not read as a field name,
such as an email address or a key starting with a digit, is dropped from
`body_params`; a data-shaped key that does read as one (`sess_a8f3c9e2`) is
kept, so read the list before you commit it.

```json
{
  "body_params": [],
  "capture_sha256": "4f6c1e0a9d2b7c3e8f5a1d6b0c9e2f7a3b8d4c1e6f0a5b9c2d7e3f8a1b6c0d4e",
  "content_type": "application/json",
  "flagged_names": [
    "X-Csrf-Token",
    "myshop_session"
  ],
  "schema": 1,
  "status": 200
}
```

Without a sidecar the status is 200 and the content type is guessed from the
extension. A sidecar is how you test an error path: copy a fixture together with
its sidecar, set the copied sidecar's `status` to 403, and assert that the
command raises `SessionRejectedError`.

### Deriving a fixture from a capture

```bash
gp observe fixtures myshop --match "GET /api/orders" --match "GET /api/orders/{order_id}"
```

The first argument is the recording's name. `--match` takes a `"METHOD
template"` pair, is required, is repeatable, and accepts a glob in the template.
The template is the one `gp observe digest` prints, a collapsed family included:
a dozen product pages the digest shows as `GET /products/{product_id}` are
matched by that template and written as `get_products_{product_id}.json`.
`--out PATH` chooses where to write (`./tests/captures` by
default), `--limit N` caps how many files are written per matched template (5 by
default), and `--allow-tracked` overrides the refusal to write onto a
git-tracked path. Repeated captures of one template get `_1`, `_2` suffixes;
`FixtureSession` looks only at the base name, so those extras are there for you
to read, not for a test to load.

Then do the work by hand. **A fixture copies the real structure and invents the
content. No captured page is committed.** Open the capture, keep the shape of
the response, and replace every real value: order ids, names, addresses,
amounts, tokens, ids in URLs. `tests/captures/` is gitignored and stays that
way; `tests/fixtures/` is committed and contains nothing that came off a real
account.

### Parsers do not return a confident empty list

The worst bug in a scraper is the one that reports success. A site changes a
container class, the parser finds nothing, the command prints an empty list, and
the user believes they have no orders.

Split the two cases. A missing container is a broken parser and must raise,
naming what it looked for. A container that is present and holds nothing is a
real empty result and returns an empty list:

```python
from graftpunk.exceptions import CommandError


def orders_from(payload: dict) -> list[dict]:
    if "orders" not in payload:
        raise CommandError(
            "No 'orders' key in the response from /api/orders. The site's shape "
            "changed; re-record and check the command."
        )
    return payload["orders"]
```

`{"orders": []}` returns `[]`. `{"error": "..."}` raises and says which key was
missing and where to look.

An HTML parser follows the same rule with the container it searches for: no
`table#orders` on the page raises and names the selector it could not find,
while a `table#orders` with no rows in it returns an empty list.

### The gate

Run all of it before every commit:

```bash
pytest
ruff check .
ruff format --check .
```

Add a type checker. A generated project passes `ruff check` and `ruff format
--check` as written, so a red gate on a fresh scaffold is something you
introduced.

A minimal CI workflow to start from, running the same gate:

```yaml
name: checks

on: [push, pull_request]

jobs:
  checks:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[dev]"
      - run: pytest
      - run: ruff check .
      - run: ruff format --check .
```

Nothing in CI logs into the site: the tests run against committed fixtures, and
the environment scrubber keeps any stray credential out of them.

### Check the CLI surface you shipped

Command names are the kebab-cased Python names, and whatever that produces is
what gets registered. A generated `api_orders_by_order_id` becomes `gp myshop
api-orders-by-order-id`, which is not a name anybody wants to type. Rename the
method, or pin a name:

```python
from graftpunk.plugins import CommandContext, command


@command(help="One order by id", name="order")
def api_orders_by_order_id(self, ctx: CommandContext, order_id: str) -> dict:
    return ctx.request_json("GET", f"/api/orders/{order_id}")
```

Then look at what you built:

```bash
gp myshop --help
gp myshop orders --help
```

Runs of capitals are not split (`getHTTPStatus` becomes `get-httpstatus`), so
`name=` is the fix there too.

### Before you publish

- [ ] No `GP-FILL` marker is left anywhere in the project.
- [ ] `gp myshop --help` lists the commands under the names you meant.
- [ ] `failure` is the site's exact wording, confirmed with a wrong password.
- [ ] `success` or `success_url` is set, and neither matches the login page.
- [ ] No credential, cookie, token, account number, or personal data is in any
      committed file. Grep for the account identifier you logged in with.
- [ ] `tests/captures/` is gitignored and no capture is tracked.
- [ ] Every fixture is invented content in a real structure.
- [ ] Every parser raises on a missing container rather than returning `[]`.
- [ ] `pytest`, `ruff check .`, and `ruff format --check .` are green.
- [ ] The module docstring records the date you verified the plugin against a
      real account, and what turned out to be wrong.

## Reference

The reference descriptions of each subsystem live in
[How graftpunk Works](HOW_IT_WORKS.md):

- [Plugin System](HOW_IT_WORKS.md#plugin-system): plugin types, discovery, configuration, and the `CommandContext` request helpers.
- [Login System](HOW_IT_WORKS.md#login-system): the declarative engine, multi-step and click-only steps, custom login methods, and diagnosing a failed login.
- [Token and CSRF Support](HOW_IT_WORKS.md#token-and-csrf-support): `Token`, `TokenConfig`, extraction sources, injection, and failure modes.
- [Session Management](HOW_IT_WORKS.md#session-management): caching, multi-account sessions, loading, browser header replay, and storage backends.
- [Observability](HOW_IT_WORKS.md#observability): capture modes, the observability context, and where runs are stored.
- [Error Handling for Plugin Authors](HOW_IT_WORKS.md#error-handling-for-plugin-authors): which exception to raise when.
- [Command Groups](HOW_IT_WORKS.md#command-groups): nesting subcommands with `@command` on a class.
- [Core Types Reference](HOW_IT_WORKS.md#core-types-reference): every public type and function in one list.

Also:

- [examples/](../examples/README.md): working YAML and Python plugins you can run.
- [docs/rfcs/2026-07-28-workstation-env.md](rfcs/2026-07-28-workstation-env.md): the workstation env file's design.
- [README](../README.md): what graftpunk is, installation, and the CLI reference.
