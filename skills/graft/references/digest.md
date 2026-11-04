# From the digest to a command proposal

The user should never have to know the commands in advance. The source is the
projection the `--endpoints-json` line of the Understand block of
`references/commands.md` prints: one entry per endpoint with `method`,
`template`, `host`, `login_flow`, `content_type`, `shape`, `query_params`,
`body_params`, `body_not_captured`, and `custom_headers`, plus a `login`
summary and the recording's `primary_host`. A row's `custom_headers_not_shown`
counts header names held back as values: ask the user for them. A row with
`body_not_captured` set has a request body gp could not read field names from,
so its `body_params` are empty or partial: say so in the row rather than
proposing a command with no inputs. The markdown digest's line says why: `body
not text` names the binary body's file in `bodies/` (the user decides what the
command sends); `body left out` means the capture had nowhere to put it, and a
new recording made the way `references/commands.md` records one keeps it; `body
not captured` means the recording holds no copy. Build the proposal from the
projection; `references/har.md` says where to look for what it leaves out.

A row with `operation` is one GraphQL operation (`(anonymous)` holds the unnamed
ones): its `body_params` are the variables, and its endpoint is `<METHOD>
<template> (<operation>)`, the whole key `--command` takes. Propose a command per
operation the user wants, named from it (`orders` for `OrdersByCustomer`); the
HAR reader's `operations` line gives its calls, its kind (a mutation changes data), and its shapes.

## The base host

Each row's `host` is compared with the base host, the host of the plugin's
`base_url`: the generator writes a relative path for an endpoint on it and an
absolute URL for any other. In enhance mode it is the host of the plugin's
`base_url` in `project.plugins` (when null or hostless, every row shows its
host). In create mode it is `primary_host` (the digest's pick, which can be a
CDN or an API subdomain; guide: Scaffold) unless the user picks another: when
the host of the site URL the user gave differs, ask once, before the table,
which of the two is the base, naming both. If the user picks the site URL's,
the scaffold step runs the `--url` line of the Scaffold block with its scheme,
host and any port as `<base-url>`, with no path and no trailing slash.

## The rules, in order

1. Drop every entry whose `login_flow` is true. The generator skips exactly the
   same entries, so the proposal and the scaffold agree. Trust the digest for
   the rest: static assets, trackers, and hosts outside the primary host's
   domain never reach the list. A subdomain, such as an API host, does reach
   it. Each endpoint carries its `host`, and a row on another host than the
   base host is shown with that host.
2. Keep JSON endpoints, and HTML documents that carry the user's own data (a
   dashboard, an order list). A row's `inline_json` (`target`, `shape`) is the
   data a page's script holds, often the results. Drop navigation chrome.
3. Name each kept entry as a short shell verb phrase: `orders` for
   `GET /api/orders`, `order` for `GET /api/orders/{order_id}` (the parameter
   becomes the argument), `invoice-pdf` for a download; a list and its detail
   are two commands. A `family_members` row offers its members as one command:
   an option, not the default. Sibling pages and one-envelope JSON form them too.
4. Show one table, one row per command: name, what it returns (from `shape` and
   `content_type`), the endpoint as `<METHOD> <template>`, and the parameters
   with their types (each `query_params` and `body_params` value is a type
   label; the labels include `str`, `int`, `float`, `bool`, `object`, `mixed`,
   and `list[<element>]`, and a `mixed` or list label needs the user's decision
   when the row is proposed) (guide: Understand). When any row's `host` differs
   from the base host, add a host column filled in for those rows only, so the
   user sees which commands call another host. Say which of the user's wants
   each row serves, and name any want with no row.
5. Ask one question, "keep, rename, or drop any of these?", and apply the
   answer. A want with no endpoint goes back to the capture step for that flow.

The stub's request call is the generator's decision; the table reports
`content_type` and `shape`, never the call.

The `login` summary tells a plain form from a redirect. `auth_urls` lists only
the login's own observations, each of kind `form_page`, `credential_post`,
`redirect` (a hop of the credential post's redirect chain), or `set_cookie`. A
plain form shows a `form_page` and a `credential_post` on the primary host; a
form or post on another host points to an identity provider (guide:
Identity-provider redirects). `forms` also lists a modal login outside any
`<form>`, from a recorded page or the run's `snapshots/`. For a form with an
`opener_candidate`, ask "did clicking <it> open the login?": on yes, strip the
leading `# ` from its commented click step and `wait_for` lines and delete the
click's `GP-FILL` lines; on no, for a `tag:nth-of-type` path the user cannot
place, or for a `GP-FILL` that says a click may open the form, ask which control
opens it and add a click-only `LoginStep`. No `forms` beside a credential post
means no login form was recorded. Confirm the login shape only when the summary
leaves it open, alone, after the keep, rename, or drop answer, before scaffolding.

`forms` holds each login form's `action`, `fields` selectors by role, `submit`
selector, `neutral_roles`, `unresolved_roles`, and `opener_candidate` (the click
the recording suggests opened the form, or null). Name every unresolved role in the
proposal: the generated login config carries a `GP-FILL` for each, which the
implement step fills from the login page's field names the HAR reader prints
(`references/har.md`), or else with a selector the user reads off the login page
(guide: Login). `success_candidates` and `failure_candidates` hold suggestions.

## A worked example

On the guide's example recording of `myshop`, the projection would hold, among
others (the guide elides the three non-JSON endpoints; the three rows below are
the ones its Login section implies):

| method | template | login_flow | shape |
| --- | --- | --- | --- |
| GET | /login | true | non-JSON |
| POST | /session | true | non-JSON |
| GET | /api/orders | false | object{orders, page, total} |
| GET | /api/orders/{order_id} | false | object{id, items, placed_on, total} |
| GET | /dashboard | false | non-JSON |

For "see my orders", the proposal is:

| command | returns | endpoint | parameters |
| --- | --- | --- | --- |
| orders | a page of orders with a total | GET /api/orders | archived: bool, page: int, per_page: int |
| order | one order with its items | GET /api/orders/{order_id} | order_id (path) |

`/login` and `/session` are gone by rule 1, and `/dashboard` is left out as
chrome unless the user asked for something only it shows. The two kept rows
reach the scaffold step as:

```bash
gp plugin new myshop --from-run myshop --run 20260901-101500-4242 --command "orders=GET /api/orders" --command "order=GET /api/orders/{order_id}"
```
