---
name: graft
description: Create a graftpunk site plugin from a browser recording, or add commands to an existing one. Use when asked to build, scaffold, or extend a graftpunk plugin for a site, or to add a command to a plugin.
argument-hint: "[plugin-name] [site-url]"
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/preflight.sh *)
---

# graft: build or extend a graftpunk site plugin

You are taking a developer through the graftpunk plugin guide, at
`${CLAUDE_PLUGIN_ROOT}/docs/PLUGIN_DEVELOPMENT.md` (this plugin ships it beside
the skill), called "the guide" below. The guide is the reference and this skill
is a route through it: when the two disagree, the guide wins. Two modes share
one flow. Create mode starts from an empty directory and ends with a new plugin
project. Enhance mode starts inside an existing plugin project and adds commands
to it.

## Permissions

The frontmatter pre-approves preflight and nothing else. Claude Code keeps an
`allowed-tools` grant only for the turn that invokes a skill ("The grant clears
when you send your next message", https://code.claude.com/docs/en/skills), and
preflight is the one command that turn reliably runs. Every other command asks
the user's permission each time it runs, unless their settings already allow it.

`references/commands.md` lists every `gp` command the steps run and the
preflight call, names the project's gate as one unit, and, under "Allow rules
for a prompt-free run", holds the permission rules this skill offers for them.
Offer those rules; never add a rule to a settings file yourself.

The live login and the first command against the live site are asked for in
words at the kick-the-tires step, whatever the user's settings allow. That
question, not a permission rule, is the consent for the live site and for a
credential.

## Start with preflight

The skill's scripts are in `${CLAUDE_SKILL_DIR}/scripts/`. Claude Code fills
in that directory in this file only, so where a reference file writes the
variable `CLAUDE_SKILL_DIR` (after a dollar sign, in braces), put this skill's
directory, `${CLAUDE_SKILL_DIR}`, in its place.

Run `${CLAUDE_SKILL_DIR}/scripts/preflight.sh` first, on every invocation. If it
exits non-zero, show its message verbatim and stop. On exit 0 it prints one JSON
object, `{"installation": ..., "project": ...}`, and `project.directory` picks
the mode: `empty` is create mode, `plugin` is enhance mode. For `foreign`, stop
and say: "this directory holds a project that is not a graftpunk plugin; run the
skill in an empty directory or in the plugin's project".

Your first message after preflight offers the allow rules under "Allow rules
for a prompt-free run" in `references/commands.md` that hold no `<site-name>`,
and says that the user can add them to this project's settings with
`/permissions` for a run without prompts. Say that the skill works either way:
without the rules, each command asks first. Offer the rules that hold
`<site-name>` once the plugin's name is fixed, with that name in its place: in
create mode at the end of the frame step, since the name `gp plugin new` takes
becomes the plugin's `site_name`; in enhance mode once the plugin is chosen,
from its `site_name` in `project.plugins`.

This invocation's arguments: plugin name `$0`, site URL `$1`. Claude Code puts
each argument given in its place, and a position with no argument keeps its
placeholder as written, a dollar sign followed by a digit. A value that reads
that way, or is empty, was not given. In create mode, ask for each one not
given, one question at a time. In enhance mode the plugin comes from
`project.plugins`, and any argument other than a suite member's name is
ignored, which you say in one line. With one entry, take it. With several (a
suite), take the entry whose `entry_point` or `site_name` is the plugin name
given above; when none was given or none matches, ask which.

## How to run the steps

Begin every turn by naming the step you are on, in one line, by name and never
by number: "Step: scaffold. Done: ...; next: ...". Ask one question at a time.
When a `gp` command fails, show its output verbatim, find the cause in the guide
section the step cites, fix it, and run the step again once. If it fails again,
show that output too and stop for the user. Never skip a step, and never
summarise a failure away. Read a reference only at the step that names it. Never
fetch a page of the site yourself, at any step: what the site shows comes from
the user, read off it in their own browser, or from the live calls they agree to
at the kick-the-tires step.

## The steps

1. **Frame** (guide: Frame). Collect the plugin name and check it with the
   command in the Frame block of `references/commands.md`, which writes nothing:
   exit 0 means the name is usable, and on exit 1 show the refusal and ask for
   another. Collect the site URL, and what the user wants to do on the site in
   plain words ("see my orders and download invoices" is enough). Do not ask for
   command names or endpoints; the digest supplies both. The login shape is
   decided later from the digest, and confirmed with the user only when the
   digest is ambiguous.
2. **Capture** (guide: Capture). Read `references/capture.md`, hand the
   recording to the user exactly as it says, and choose the session and run as
   its "After the recording" section says. You never run the recorder yourself.
   Every later step reads that session and that run.
3. **Understand** (guide: Understand). Run the command in the Understand block
   of `references/commands.md` on the chosen session and run, then read
   `references/digest.md` and build the proposal it describes. When the host of
   the site URL the user gave differs from the projection's `primary_host`,
   first ask once which of the two is the plugin's base, as that file says
   ("The base host"). A row whose `host` differs from the base host shows its
   host, so the user sees which commands call another host; the generator
   targets each endpoint's own host. The user keeps, renames, or drops rows in
   one answer.
4. **Scaffold** (guide: Scaffold). Run the `gp plugin new` line of the Scaffold
   block in `references/commands.md` without `--url`, or the one with it when
   the user chose the site URL's host at the understand step, with the chosen
   session and run and one `--command` per row the user kept. The generator
   writes only those stubs, under those names, each with its endpoint declared,
   and prints a `<command> calls <host>` line for each one on another host;
   relay those lines as `references/harden.md` says ("The gate"). Edit nothing
   it wrote during this step. Then run `gp plugin info --json` and confirm
   every agreed command is listed with the endpoint it was agreed for.
5. **Implement** (guide: Implement). For each stub, fill in the request, name
   the parameters, decide the return shape, and replace every `GP-FILL` marker.
   Raise `CommandError` or `PluginError` on failure. Read `references/rules.md`
   and read the guide section behind any rule the work touches. The login's
   markers need what only the user can see, so ask for each one the generated
   config marks, one question at a time, and wait for each answer: the exact
   sentence the site shows after a wrong password (they try one in their own
   browser), a CSS selector of an element on the page a successful login lands
   on, a selector for any login field or submit button the digest left
   unresolved, and the login page's path, or its full URL when it is not on the
   base URL's host. Ask for the URL a login lands on only when `success` is
   still unset; once `success` is set, drop a `success_url` marker instead
   (guide: Getting the signals right).
6. **Harden** (guide: Harden). Read `references/harden.md` and follow it: one
   fixture and one test per command, then the project's gate (guide: The gate),
   acting on gp's output as that file says. The gate must pass before the next
   step.
7. **Kick the tires** (guide: Check the CLI surface you shipped) (guide: Login).
   Before the first live call, ask in words, whatever the user's settings allow:
   "run a live login and one read-only command now?" Their answer is the consent
   for the live site and the login; an allow rule does not stand in for it. On
   yes, set up the credentials before anything runs: the login reads each field
   of the plugin's `LoginConfig` from an environment variable named for the
   `site_name` in capitals (hyphens and spaces become underscores), an
   underscore, and the field name in capitals (a plugin's `username_envvar` or
   `password_envvar` names that field's variable instead), and prompts on the
   terminal for one it cannot find, which in this session aborts the login.
   Print the `gp config set` line of the "Run by the user" block in
   `references/commands.md` once per field, with a placeholder value, never a
   real one, and wait until the user says they have run them. Then run the help
   line of the Kick the tires block and confirm every agreed command name is
   listed, then its login line, then its read-only command line with one
   read-only command from the agreed proposal, against the live site while the
   user watches. If the login fails, diagnose it against the guide's Login
   section, adjust the plugin's `LoginConfig`, and try once more; if it fails
   again, show the output and stop for the user.
8. **Publish checklist** (guide: Before you publish). Read that section of the
   guide. Its first item, the gate, already holds after the harden step. Walk
   the rest as the guide lists them and fix what you can. An item that needs
   another live call, such as confirming the failure text with a wrong password,
   or a fact only the user has, such as the account identifier to grep for, is
   theirs: list it and do not run it. Stop with the items left for the user.

## Where enhance mode differs

The steps above are written for create mode. Enhance mode runs the same steps
with these differences. This list is the index of what enhance mode changes;
the references hold the details.

- Frame collects only the new thing the user wants to do. The plugin's
  `entry_point` (what you pass to `gp plugin add-command`) and its `site_name`
  (the name `gp` runs it by) come from `project.plugins`. Nothing is asked about
  its `base_url`, since `gp plugin add-command` targets each endpoint's own
  host.
- Capture picks the recorder line by whether the plugin has a session, as
  `references/capture.md` says.
- Understand leaves out every endpoint an existing command declares, and prints
  each one it left out beside that command's name and declared endpoint, so a
  declaration that went stale after a hand edit shows up. Every remaining row
  is marked new. Each existing command reported with `endpoint: null` gets a
  row of its own reading "existing command, endpoint not declared", so the user
  can say whether a proposed row duplicates it. Never drop or propose over an
  undeclared command silently. The base host is the host of the chosen
  plugin's `base_url` in `project.plugins`, the host `gp plugin add-command`
  compares with, and the question about the plugin's base is not asked
  (`references/digest.md`, "The base host").
- Scaffold does not run `gp plugin new`. For each agreed command it runs the
  `gp plugin add-command` line of the Scaffold block in
  `references/commands.md`, with the chosen session and run, which adds one stub
  in the generated shape and writes no test. Act on gp's output as
  `references/harden.md` says ("The gate"); that file also says what the harden
  step does for each added command.
- The publish checklist is limited to the items the new commands touch.

## Secrets

Never ask for a password, and never write a credential anywhere. Never read a
HAR, a cookie, or what `gp config get --resolve` returns; a token value inside a
capture is replaced like any other captured value. From a recording, read the
`--endpoints-json` projection, and at the harden step the captures
`gp observe fixtures` writes into the git-ignored `tests/captures/`: they hold
the account's own data and stay on this workstation, so no value from one goes
into anything that is committed or shared, such as a fixture, a test, code, a
comment, a docstring, a commit message, or a pull request. The output of the
live read-only command the user agreed to at the kick-the-tires step is the
same: show it to them, confirm it returned data, and use no value from it. When
the user pastes a secret into the conversation, say it belongs in the
workstation env file through the `gp config set` line of the "Run by the user"
block in `references/commands.md`, with a `$(your-secret-tool read ...)` value,
and do not use it.