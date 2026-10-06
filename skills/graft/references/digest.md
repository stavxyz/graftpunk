# From the digest to a command proposal

The user should never have to know the commands in advance. The source is the
projection the Understand block of `references/commands.md` prints: one entry
per endpoint with `method`, `template`, `host`, `login_flow`, `content_type`,
`shape`, `query_params`, `body_params`, and `custom_headers`, plus a `login`
summary and the recording's `primary_host`.
Read that projection only; never read the HAR or `--json`.

## The rules, in order

1. Drop every entry whose `login_flow` is true. The generator skips exactly the
   same entries, so the proposal and the scaffold agree. Trust the digest for
   the rest: static assets, trackers, and hosts outside the primary host's
   domain never reach the list. A subdomain, such as an API host, does reach
   it. Each endpoint carries its `host`, and a row on another host than
   `primary_host` is shown with that host.
2. Keep JSON endpoints, and HTML documents that carry the user's own data (a
   dashboard, an order list, a statement page). Drop navigation chrome.
3. Name each kept entry as a short verb phrase a person would type at the
   shell: `orders` for `GET /api/orders`, `order` for
   `GET /api/orders/{order_id}` (the path parameter becomes the command's
   argument), `invoice-pdf` for a document download. A list and its detail are
   two commands, never one.
4. Show one table, one row per command: name, what it returns (from `shape` and
   `content_type`), the endpoint as `<METHOD> <template>`, and the parameters
   with their types (each `query_params` and `body_params` value is a type
   label; the labels include `str`, `int`, `float`, `bool`, `object`, `mixed`,
   and `list[<element>]`, and a `mixed` or list label needs the user's decision
   when the row is proposed) (guide: Understand). When any row's `host` differs
   from `primary_host`, add a host column filled in for those rows only, so the
   user sees which commands call another host. Say which of the user's wants
   each row serves, and name any want with no row.
5. Ask one question, "keep, rename, or drop any of these?", and apply the
   answer. A want with no endpoint goes back to the capture step for that flow.

In create mode, `primary_host` (the host the digest picks as the recording's
main one) becomes the plugin's `base_url` unless the scaffold overrides it, and
it can be a CDN or an API subdomain (guide: Scaffold). When the host of the site URL the
user gave differs from `primary_host`, ask once, after the keep, rename, or drop
answer, which of the two is the plugin's base, naming both. If the user picks
the site URL's host, the scaffold step runs the `--url` line of the Scaffold
block with `https://` and that host in place of `<base-url>`: scheme and host
only, no path and no trailing slash. Enhance mode never asks this.

What request call a stub makes (`request_json` or `request_text`, and its role)
is the generator's decision; the table reports `content_type` and `shape` and
never predicts the call.

The `login` summary tells a plain form from a redirect. `auth_urls` lists only
the login's own observations, each of kind `form_page`, `credential_post`,
`redirect` (a hop of the credential post's redirect chain), or `set_cookie`. A
plain form shows a `form_page` and a `credential_post` on the primary host. A
login whose form or post is on another host points to an identity provider
(guide: Identity-provider redirects). An empty `forms` entry while a credential
post exists means no login form was recorded (a script-driven login, or a
recording that missed the form page); confirm the shape with the user. Confirm
the login shape with the user only when that summary leaves it open, and ask it
as its own question right after the keep, rename, or drop answer, before the
scaffold step.

`forms` holds each login form's `action`, its `fields` selectors by role, its
`submit` selector, `neutral_roles`, and `unresolved_roles`. Name every
unresolved role in the proposal: the generated login config carries a `GP-FILL`
for each, which the implement step fills with a selector the user reads off the
login page in their browser (guide: Login).

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
