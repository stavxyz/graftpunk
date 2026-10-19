# Looking past the projection

The `--endpoints-json` projection is shaped for the command proposal and stays
its source. When a step needs something it does not carry, such as a cookie's
`Secure`, `HttpOnly`, `SameSite`, expiry, domain, or path, a header, a request
body, a response shape it marks `shape unavailable`, or the markup of the login
page or of the page a login lands on, look in the recording before asking the
user, and ask the user only for what the recording does not show. Everything
here falls under the Secrets rule in `SKILL.md`.

## The `--json` digest

Run the `--json` line of the Understand block of `references/commands.md` on
the chosen session and run first. It is the whole digest model: header, cookie,
token-candidate, form field, and query parameter names, observed types, and
response shapes. It holds no header, cookie, or query value, and only one kind
of body value: for an endpoint whose recorded JSON body is `{}`, `[]`, `""`,
`0`, `false`, or `null`, that value. It does print paths as recorded, though:
up to three example paths per endpoint (the query string stripped and a segment
holding an email address replaced, but an account or order id kept), each login
step's URL and redirect target, each login form's action and the page it was
on, and the page each token was seen on.

## Where the HAR is

`network.har` in the run's directory,
`~/.local/share/graftpunk/observe/<session>/<run>/`, with the session and run
ID as `gp observe list` prints them (it prints the names, not the directory).
Resolve `~` to the user's home directory: the HAR reader, Read, and Grep each
get the absolute path, which is `<har>` in `references/commands.md`. Beside the
HAR, `bodies/` holds the response bodies too large to inline, and
`page-source.html` the page that was open when the recording stopped.

## The HAR reader

Ask the HAR through the `har-facts.py` lines of the Understand block of
`references/commands.md`. The script reads the HAR as JSON and prints names,
attributes, types, and counts, never a cookie, header, query, or body value, so
answer from its output:

- `cookies`: for each cookie a response sets, every response that sets it (by
  method, host, and path, with a count) and that setting's `HttpOnly`,
  `Secure`, `SameSite`, `Path`, `Domain`, and whether it expires. It reads the
  raw `Set-Cookie` header and the entry's `response.cookies` array and says
  when they disagree: in a real recording the array lacked `httpOnly` for a
  cookie whose header carried it, so trust the header's line. It then names
  the cookies the recording only sends.
- `request` with a piece of the URL's path in place of `<url-part>`: for each
  request whose own URL holds it, the method, the URL with its query keys only,
  the request header and cookie names, and the body's field names with each
  value's type. Leave `--method <METHOD>` off to match every method.
- `response`, matched the same way: the status, the content type, the names of
  the cookies it sets, and the body's shape, JSON keys and types or an HTML
  page's title and its forms' field names and input types.

A field whose name reads as a password, secret, token, card, or key is listed
by name alone. Exit 1 means nothing matched (no request URL held the piece, or
no cookie was set or sent), and exit 2 that the HAR is missing or is not a HAR.

## Reading the HAR by hand

Read the HAR yourself only for what the reader does not cover, and narrowly;
the skill offers no allow rule for it, since it holds the login's password. The
file is JSON with a two-space indent, one key per line, and within an entry the
request's `method` and `url` come first, then its `headers` (where `Cookie` and
`Authorization` values sit), its `cookies` (with their values), its
`queryString`, and its `postData`, then the `response`.

- Find an entry with a Grep anchored on the URL key, `"url": "[^"]*<path>`, so
  a `Referer`, `Location`, or `redirectURL` line does not match, with line
  numbers and a head limit. That line holds the query string too.
- Never Read forward from a `url` line, and never read a `postData`, a `Cookie`
  or `Authorization` value, or a cookie's `value`. Treat any form or body with a
  password-like field as holding a secret, and leave it to the reader.
- A body inlined in the HAR sits on one line, and a Grep that matches it prints
  the whole line: test for a key with a Grep in count mode, never content mode.

Answer with names, attributes, shapes, and counts.
