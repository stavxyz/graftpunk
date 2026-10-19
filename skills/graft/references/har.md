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
The Read and Grep tools take an absolute path, so resolve `~` to the user's
home directory before using either. Beside the HAR, `bodies/` holds the
response bodies too large to inline, and `page-source.html` the page that was
open when the recording stopped.

## Where the answers sit

The HAR is JSON written with a two-space indent, one key per line. Its
`log.entries[]` each hold a `request` and a `response`:

- `request.url` is the full URL, query string included: find an entry by it.
- `response.cookies[]` holds one object per `Set-Cookie` line: `name`, then
  `value`, then `httpOnly`, `secure`, `path`, `domain`, and `expires` when the
  server set them. `SameSite` and `Max-Age` have no field there and appear only
  in the raw `Set-Cookie` entry of `response.headers[]`. A flag missing from
  the array does not show it is off, since the array often leaves out a flag
  the header carries: confirm in the header with the count-mode match below,
  `HttpOnly` or `Secure` in place of `SameSite=Lax`.
- `request.postData` holds a request body as `mimeType` and `text`.
- `response.content.text` holds a response body, or `response.content._bodyFile`
  names its file under `bodies/`.
- `request.headers[]` and `response.headers[]` hold `name` and `value` pairs.

## Reading narrowly

Never read the whole HAR. Grep it with a narrow pattern, the URL path or
`"name": "<cookie>"`, with line numbers and a head limit, then Read with an
offset and a limit of a few lines around a match. For a cookie's attributes,
start the Read two lines below its `name` line, past its `value`. A body
inlined in the HAR sits on one line, often a very long one: Grep for a key
inside it rather than reading the line. To learn a cookie's `SameSite` without
printing its header line, Grep in count mode, case-insensitive, for
`("|\\n)<cookie>=[^\\]*;\s*SameSite=Lax`, then `Strict`, then `None`: a header's
`Set-Cookie` lines are joined by an escaped newline, so the pattern starts at
that cookie's own name and stops at the next line.

Never read the credential post's `request.postData`, which holds the password
the user typed (the `login` summary's `credential_post` names that request),
and never read the value of a request's `Cookie` or `Authorization` header.
Read a cookie's attributes, not its value. Answer with names, attributes,
shapes, and counts.

No allow rule is offered for reading the HAR, since it holds the login's
password.
