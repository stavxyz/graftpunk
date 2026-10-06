# Fixtures, tests, the gate, and the checklist

## A fixture per command

For each command, write its capture out of the recording with the
`gp observe fixtures` line from the Harden block of `references/commands.md`,
the command's endpoint in place of `<METHOD> <template>`. Copy each capture gp
writes, with its sidecar, into the fixtures directory the generated tests read
(their `FIXTURES_DIR`), keeping its file name: a test finds its fixture by that
name, and a request with no fixture under it answers 404. When gp writes
numbered copies of one endpoint, copy the one you want onto the plain name.
Then edit the copy's response, inventing every value while keeping its
structure (guide: Deriving a fixture from a capture). Leave the copied sidecar
alone.

When gp says it can write no fixture for a command's endpoint, write a
fixture and its sidecar by hand, with invented values in the shape the site
returns (guide: Test against fixtures, not against the site).

The generated suite checks every fixture on every run and names each problem it
finds; act on each line it prints. A passing check does not mean the invented
values are good ones; read the fixture once more before moving on.

## A test per command

Each generated test builds a context with `fixture_context` over the plugin's
fixtures directory and calls the command. Replace its `GP-FILL` assertion with
one on the shape the command returns: the keys a caller relies on, and the
values you invented. Add one error-path test where a command can fail, by
copying a fixture and setting its sidecar's `status` to an error.

In enhance mode `gp plugin add-command` writes no test. Its `Next:` line either
names the fixture the command's test should read, or says `gp observe fixtures`
writes no fixture for that endpoint, in which case write the fixture by hand as
above. For each command it added, write a test in the shape of the tests the
project already has: the same context over the plugin's fixtures directory, one
call, and assertions on the returned shape (guide: Test against fixtures, not
against the site).

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
  fixture by hand, as above.
- uv warns that the project has no `dev` extra: the project defines none, the
  gate's own tools are installed anyway, and nothing needs doing; do not add
  an extra to the user's project.

## Before you publish

Read the guide's publish checklist (guide: Before you publish) and walk its
items in order. Keep no copy of that list here or in the conversation; the
guide is the one place it lives.
