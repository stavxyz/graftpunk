# Fixtures, tests, the gate, and the checklist

## A fixture per command

In enhance mode, first run the gate's plugin check and act on what it reports,
so the fixtures directory and the suite's fixture check exist before anything is
copied. For each command, write its capture out of the recording with the
`gp observe fixtures` line from the Harden block of `references/commands.md`,
the command's endpoint in place of `<METHOD> <template>`. Captures land in the
git-ignored `tests/captures/` and hold the account's own data: read them as this
step needs, and put no value from one into anything committed or shared (a
fixture, a test, code, a comment, a docstring, a commit message, or a pull
request). Copy the capture gp writes under the plain name (not a numbered one)
and its sidecar into the fixtures directory the generated tests read (their
`FIXTURES_DIR`) with the `cp` line of the Harden block, keeping both file names:
a test finds its fixture by that name, and a request with no fixture under it
answers 404. Copy those two files by name, never with a glob or a whole
directory, which would bring numbered captures along.

Then rewrite each copy with invented values, keeping its structure: the same
keys, nesting, and types, a few list items rather than every one, and values of
the same kind (an id stays an id-shaped string, a date a date) that belong to no
real account (guide: Deriving a fixture from a capture). For an HTML capture,
keep the markup and the classes and attributes a parser selects on, and invent
the text and the attribute values that carry data. A token value in a capture is
replaced like any other value. Then run the fixture-leaks line of the Harden
block on the capture, the fixture, and its sidecar. It prints each captured
value, and each piece of one (a run of three or more digits, a word holding an
`@`, a digit, or a capital letter, both halves of an email address, and a pair
of words with one of those in it), still in the fixture, escaped and
percent-encoded copies included (a single word in lower case only inside an
identifier or an address), and the names the sidecar lists. It matches text, so
read the fixture once more for what it cannot see: a word in lower case on its
own, a number written another way (`12345` as `12,345`), digits split across
fields, and a copy re-encoded, such as base64. When it exits 2 (a capture that
is not text, such as a PDF), it compared nothing: replace the copied file
wholesale with invented values, read the plugin module by eye for anything from
that capture, and tell the user. Rewrite each value it prints unless it holds no
account data and the command branches or selects on it (a status, a currency
code, a class name), or the site shows it to every account (a heading, a button
label), or it is part of a format (a year, a time-zone suffix). When every value
it still prints is one of those, the fixture is done; tell the user which values
you kept and why. A sidecar is committed: when one of its names holds an account
value, tell the user and do not commit that sidecar as it is
(guide: Test against fixtures, not against the site). Run the same line once
more with the plugin module in place of the fixture and with no sidecar, and
rewrite any captured value it prints there: a docstring and a comment hold
invented values only. Then run the gate's `pytest` line through the runner in
the Harden block: the generated suite checks every fixture on every run and
names each one that is still byte for byte its capture or holds a flagged cookie
or token name; rewrite any it names. Count the check as run only when that
`pytest` output carries its `fixtures_are_sanitised:` line; without it the
project lacks the wiring, so run the gate's plugin check and act on what it
reports first.

When gp says it can write no fixture for a command's endpoint, write a fixture
and its sidecar yourself, with invented values in the shape the projection gives
(guide: Test against fixtures, not against the site). When that shape reads
`shape unavailable` too (a body too large to sample), ask the user what the
response looks like.

## A test per command

Each generated test builds a context with `fixture_context` over the plugin's
fixtures directory and calls the command. Replace its `GP-FILL` assertion with
one on the shape the command returns: the keys a caller relies on, and the
values the fixture holds. Add one error-path test where a command can fail, over
a fixtures directory of its own holding a copy of the fixture whose sidecar
`status` is an error, since a fixture is found by its name. In either mode, once
a command's test is written, run the fixture-leaks line with its capture and the
test module in place of the fixture, with no sidecar, and rewrite any captured
value it prints: an assertion holds the fixture's invented values only. When it
exits 2, read the test module by eye instead.

In enhance mode `gp plugin add-command` writes no test; act on its `Next:` line
as "The gate" below says. For each command it added, derive the fixture as above
and write a test in the shape of the tests the project already has: the same
context over the plugin's fixtures directory, one call, and assertions on the
returned shape (guide: Test against fixtures, not against the site).

## The gate

Read the guide's section on the gate (guide: The gate) and run every command it
lists, through the runner the Harden block of `references/commands.md` gives
it, until all of them pass. This section is where the skill's handling of gp's
output lives, for the scaffold step and this one: show the user gp's output and
act on every instruction in it, then run the gate again. Both
`gp plugin add-command` and `gp plugin upgrade` may ask for the project to be
installed again or for its graftpunk requirement to be raised, and each finding
of the gate's plugin check carries the advice to follow.

That runner installs the project with its main and `dev` dependencies and the
gate's own tools, and builds the environment from `pyproject.toml` on every
run, so there is nothing to install: when gp asks for the project to be
installed again, run the gate again. Each run resolves to the newest versions
the project's requirements allow, not to the versions in the project's
`uv.lock` if it has one. That is a trade-off: the gate may judge the plugin
against newer versions than the project's lock pins, so when a failure points
at a dependency's version, say that to the user. Never install anything into
the `gp` on the user's PATH. The cases to expect, in plain words:

- graftpunk's requirement in the project was raised: run the gate again, which
  picks up the new requirement.
- the requirement cannot be raised for you: raise it by hand, then run the
  gate again.
- the fixtures tree or some project wiring is missing: gp says to run
  `gp plugin upgrade`; run it, then the whole gate.
- an endpoint for which `gp observe fixtures` writes no fixture: write the
  fixture yourself from the projection's shape, as above.
- the project's dependencies are dynamic: gp names what has to require the
  newer graftpunk; tell the user, since the fix is outside `pyproject.toml`.
- uv warns that the project has no `dev` extra: the project defines none, the
  gate's own tools are installed anyway, and nothing needs doing; do not add
  an extra to the user's project.

## Before you publish

Walk the guide's publish checklist in order (guide: Before you publish), and
keep no copy of it here or in the conversation.
