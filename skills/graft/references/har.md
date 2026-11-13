# Looking past the projection

The `--endpoints-json` projection is shaped for the command proposal and stays
its source. When a step needs something it does not carry, such as a cookie's
`Secure`, `HttpOnly`, `SameSite`, expiry, domain, or path, a header, a request
body, a response shape it marks `shape unavailable`, or the markup of the login
page or of the page a login lands on, look in the recording before asking the
user, and ask the user only for what the recording does not show. Everything
here falls under the Secrets rule in `SKILL.md`.

## The `--json` digest

Run the `--json` line of the Understand block of `references/commands.md` on the
chosen session and run first. It is the whole digest model: header, cookie,
token-candidate, form field, and query parameter names, observed types, response
shapes, and in `cookie_facts` each cookie's attributes and the endpoints that
set it (no `Domain`). Any name the gate holds back is left out wherever it would
print and counted: a cookie's in `cookie_names_not_shown`, a header's in
`header_names_not_shown` (`custom_headers_not_shown` on an endpoint), a meta
tag's in `meta_names_not_shown`, a hidden input's in `hidden_names_not_shown`,
and a query or posted field's in `field_names_not_shown` (`fields_not_shown` on
a login step, `query_params_not_shown` and `body_params_not_shown` on an
endpoint); `names_not_shown` counts each held-back name once. It holds no
header, cookie, or query value, and only one kind of body value: for an endpoint
whose recorded JSON body is `{}`, `[]`, `""`, `0`, `false`, or `null`, that
value. It does print paths as recorded, though: up to three example paths per
endpoint (the query string stripped and a segment holding an email address
replaced, but an account or order id kept), each login step's URL and redirect
target, each form's action and page, and each token's page.

## Where the HAR is

`network.har` in the run's directory,
`~/.local/share/graftpunk/observe/<session>/<run>/`, with the session and run
ID as `gp observe list` prints them (it prints the names, not the directory).
Resolve `~` to the user's home directory: the HAR reader, Read, and Grep each
get the absolute path, which is `<har>` in `references/commands.md`. Beside the
HAR, `bodies/` holds every body, sent or received, that was too big for the HAR
or binary, `snapshots/` the page as it was at each credential post (the digest
reads a login form from it) and, as `<request id>.after.html`, a structure-only
read of the page taken shortly after the response to that post (unless the last
post's shows a login step, the digest keeps only the `success` suggestions it
also holds), and
`page-source.html` the page that was open when the recording stopped.

## The HAR reader

Ask the HAR through the `har-facts.py` lines of the Understand block of
`references/commands.md`. The script reads the HAR as JSON and prints no cookie,
header, query, or body value; answer from its output:

- `cookies`: for each cookie a response sets, every response that sets it (by
  method and templated URL, with a count) and that setting's `HttpOnly`,
  `Secure`, `SameSite`, `Path`, `Domain`, and `expiry` (`session`,
  `persistent`, or `past` when the response deletes it), read from the raw
  `Set-Cookie` header and the entry's `response.cookies` array, with a note
  when they disagree; `unknown` is what the recording does not settle, and
  `(not shown)` a `Path` carrying a query, a comma, a space, a percent escape,
  or the like, in
  the digest's words. A recording made by another tool or an older graftpunk
  has shown the array lacking `httpOnly` where the header carried it (cause
  unverified), so trust the header's line. It then names the cookies the
  recording only sends. With `--name '<cookie>'` it prints that one cookie's
  block, for a name you already hold; leave it off to list every cookie. It
  answers yes or no for an exact name, so a guess at a name that carries an
  account value is confirmed by the answer: ask only for names the task needs.
- `request` with a piece of the URL as the script prints it (`{id}`, `{token}`,
  or `{}` where it holds text back) for `<url-part>`: for each request whose own
  URL holds it, the method, the templated URL with its query keys, the request
  header and cookie names, and the body's field names with each value's type.
  Leave `--method <METHOD>` off to match every method. A recorded path matches
  only with `--recorded-path`, which confirms it: pass only one you hold.
- `response`, matched the same way: the status, the content type, the response
  header and set cookie names, and the body's shape, JSON keys and types or an
  HTML page's title length and its forms, each with its method, templated
  action, and controls by name (or by position) and input type.
- `operations`: each GraphQL operation on the digest's hosts, keyed as the digest
  keys it, its calls (and batched ones), kind, nested variable and response keys
  and types, then which response field fed which later variable, by name.

Names, path segments, subdomains, and content subtypes print only when they pass
a heuristic gate for field names, which also lets fixed framework names through
(CDN and bot-manager headers and cookies, ASP.NET WebForms controls, Django
formset fields, routes such as `graphql`). What fails is a placeholder (`{id}`,
`{token}`, `{}`, a scheme alone, `(method not shown)`, an unnamed blob of N
bytes, a title's length class) or a count ("N names that read as values, not
shown"). The gate stops random tokens with digits or mixed case; a plain word (a
username, a person's or place's name, a label, a handle like `alice84`) and a
letter-only token that spells like a word (10.84% of 9-letter random tokens,
5.20% at 12) can still be the account's own: never copy one into code without
the user's word. A field whose name reads as a secret is listed by name alone.
Exit 1 means nothing matched; exit 2 means the HAR is missing, is not a HAR,
nests deeper than 512 levels, or holds an entry the script could not read.

When a name a step needs is counted as reading like a value, use `--name` for a
cookie whose name you hold, and ask the user for any other name (a login field,
a form field). Never read the HAR by hand for it.

## Reading the HAR by hand

Read the HAR yourself only for what the reader does not cover, and narrowly;
the skill offers no allow rule for it, since it holds the session's cookies and
tokens and can hold the login's password. The
file is JSON with a two-space indent, one key per line, and within an entry the
request's `method` and `url` come first, then its `headers` (where `Cookie` and
`Authorization` values sit), its `cookies` (with their values), its
`queryString`, and its `postData`, then the `response`.

- Find an entry with a Grep anchored on the URL key, `"url": "[^"]*<path>`, so
  a `Referer`, `Location`, or `redirectURL` line does not match, with line
  numbers and a head limit. That line holds the query string, values and all;
  the reader's `request` line gives the keys alone.
- Never Read forward from a `url` line, and never read a `postData`, a `Cookie`
  or `Authorization` value, or a cookie's `value`. Treat any form or body with a
  password-like field as holding a secret, and leave it to the reader.
- A body inlined in the HAR sits on one line, and a Grep that matches it prints
  the whole line: test for a key with a Grep in count mode, never content mode.

Answer with names, attributes, shapes, and counts.
