# The graft skill: a Claude Code skill that creates or enhances a graftpunk site plugin

Date: 2026-09-21. Status: approved design, awaiting the implementation plan.

## Summary

graftpunk gains a Claude Code plugin marketplace inside its own repository and one skill in it, `graftpunk:graft`, which walks a developer through the six steps of `docs/PLUGIN_DEVELOPMENT.md` (frame, capture, understand, scaffold, implement, harden) and runs the `gp` commands itself, pausing only where a person has to act: the browser recording and the live login check. The same skill enhances an existing plugin: run inside a plugin project, it records the new flow, proposes commands from the digest, adds the stubs, fixtures, and tests, and runs the project's gate.

Users install it with two commands and see it under `/skills`:

```text
/plugin marketplace add stavxyz/graftpunk
/plugin install graftpunk@graftpunk
```

Invocation: `/graftpunk:graft myshop https://myshop.example/` creates a plugin; `/graftpunk:graft` with no arguments, run inside a plugin project, enhances it. Claude also invokes it on its own when asked to create a graftpunk plugin for a site or to add a command to one.

## Goals and non-goals

Goals: a developer with a site login and no graftpunk knowledge gets a working, tested, publishable plugin package by following the skill, without reading the guide first; the skill copies nothing from the guide and cites only headings that exist, and tests enforce both; everything the skill needs to know or render about a plugin project comes from the package, which is the one tested owner of that format; the skill is versioned and released independently of the graftpunk package.

Non-goals: driving the browser recording from the skill (Claude Code cannot press Ctrl+C in an interactive command); a `gp` pipeline command that bundles digest, scaffold, and fixtures (orchestration stays in the skill, see the rejected approaches; the package changes below report facts and render one stub, which is not orchestration); handling secrets in any form.

## Repository layout

```text
.claude-plugin/marketplace.json     the marketplace: name graftpunk, one plugin, source ./
.claude-plugin/plugin.json          the Claude Code plugin: name graftpunk, version 0.1.0
skills/graft/SKILL.md               the skill: frontmatter and the flow
skills/graft/references/rules.md    the house rules as a checklist, each citing a guide heading
skills/graft/references/capture.md  the capture hand-off: the command, what to exercise, how to stop
skills/graft/references/digest.md   how to read a digest and turn endpoints into a command proposal
skills/graft/references/harden.md   fixtures, tests, the gate, the publish checklist
skills/graft/scripts/preflight.sh   gp present and new enough; relays gp plugin info --json
tests/unit/test_graft_skill.py      the skill's own tests (see Testing)
.github/workflows/skill-version.yml the version-bump check on pull requests
scripts/check-skill-version.sh      the check itself, also run by just skill-version
docs/PLUGIN_DEVELOPMENT.md          gains one short section, "With the skill"
README.md                           gains the two install lines under Plugins
CONTRIBUTING.md                     gains "Releasing the skill" (the version rule)
```

And in the package, delivered in a pull request before the skill (see Package changes, delivered first):

```text
src/graftpunk/plugins/cli_plugin.py       @command(endpoint=...) stored on the command metadata
src/graftpunk/cli/scaffold_commands.py    gp plugin info, gp plugin new --command, gp plugin add-command, gp plugin check
src/graftpunk/devtools/scaffold/render.py a single-command render entry point; the declared endpoint and typed params on every stub; the placement invariant
src/graftpunk/devtools/captures.py        capture_sha256 and flagged_names in the fixture sidecar
src/graftpunk/testing/                    fixtures_are_sanitised, wired into the generated conftest
```

`skills/` sits at the repository root because a plugin whose `source` is `./` loads `skills/<name>/SKILL.md` from the root; the official marketplace documentation states that `source: "./"` means the plugin is the marketplace root and that skills load from `skills/<name>/SKILL.md`, and the `stavxyz/skills` repository is a working example of exactly this root layout. The Python package under `src/` is unaffected: `skills/` is not a Python package and is excluded from the sdist by the existing allowlist.

## The manifests

`.claude-plugin/marketplace.json`:

```json
{
  "$schema": "https://code.claude.com/schemas/marketplace.json",
  "name": "graftpunk",
  "owner": {"name": "stavxyz"},
  "description": "graftpunk's Claude Code skills: /graftpunk:graft creates or enhances a site plugin",
  "version": "0.1.0",
  "plugins": [
    {
      "name": "graftpunk",
      "source": "./",
      "description": "/graftpunk:graft walks the plugin developer guide: record the site, digest the recording, scaffold or extend the plugin, implement, harden, publish"
    }
  ]
}
```

`.claude-plugin/plugin.json` carries the same name, description, version, and author. The two `version` fields move together; nothing else in either file changes per release. No email in `owner` or `author` (the repository is public; the GitHub handle is enough).

## Package changes, delivered first

Six small additions to graftpunk itself, in one pull request that merges before the skill's, so the skill never carries knowledge the package owns. Each reports a fact, renders through the existing generator, or checks an invariant; none orchestrates.

**A declared endpoint on every generated command.** `@command` gains an optional `endpoint: str | None = None` keyword, stored on the command's metadata and shown in `gp <plugin> --help` as nothing (it is provenance, not help text). The generator writes it on every stub it renders, `@command(help=..., endpoint="GET /api/orders/{order_id}")`, so the endpoint a command implements is a declaration the generator owns, never a deduction from the request call (which the generator renders as an f-string for a parameterised path, so a call-site reader would fail exactly where it matters). A hand-written command may carry the keyword or not.

**`gp plugin info --json`.** The working directory's plugin project, as one JSON object with a `schema: 1` field and the rule that fields are added and never renamed or removed within a schema version, so a skill released independently of the package can rely on it. Fields: `mode` (`create`, `enhance`, or `foreign`), `reserved` (the top-level command names from `reserved_cli_names()`), and for `enhance` the `module` path, `site_name`, `base_url`, `session_cached` (whether the session store holds a session for that plugin), and `commands`, a list of `{name, endpoint, declared}` where `endpoint` is the `@command(endpoint=...)` literal and `declared` is `false` (with `endpoint: null`) for a command that carries none, which is a distinct, reported state rather than a failed read. The module is read with `ast`, never imported, so a broken plugin still gets an answer; the reader lives beside the generator that writes the shape, and reads only declarations. Installation facts (`gp --version`) are not in the payload; the version floor has its own owner in preflight. Without `--json` it prints the same facts as a table. Exit 0 in every mode; the mode is data, not an error.

**`gp plugin new --command "<METHOD> <template>=<name>"`, repeatable.** With `--from-run`, selects which digest endpoints become stubs and names them, so the names agreed in the proposal step reach the generator instead of being applied to generated code afterwards, and the fixture path rule stays entirely inside the package. Without `--command` the behaviour is unchanged (every eligible endpoint, generated names).

**`gp plugin add-command <plugin> --from-run <session> --endpoint "<METHOD> <template>" --command <name>`.** Renders one command stub for the named endpoint of the session's newest run (`--run` pins an older one) in exactly the shape `gp plugin new` writes, through a single-command entry point in `render.py` that `gp plugin new` also uses for each of its stubs, and inserts it into the plugin class of the module the working directory's `pyproject.toml` names, immediately after the class's last method. That placement is a stated invariant of the generated module (the plugin class is the module's last statement and its commands are its last members), written once in `render.py`'s module docstring and relied on by both the generator and the mutator; a module that no longer satisfies it (the class is not last, or cannot be parsed) is refused with the reason. Refuses when the command name exists, when the endpoint is not in the digest, and when the digest attributes the endpoint to the login flow. Prints the fixture path the command's test will look for (the rule `gp plugin new` prints as its `Next:` line and `FixtureSession` uses at read time).

**Typed parameters rendered by the generator.** Where a stub parameter has a type in the digest, the generator emits the explicit `PluginParamSpec` entry itself, so the compensation for #208 (introspected options arrive as strings under the future-annotations import) lives in `render.py` and is one edit to remove when #208 lands; the skill says nothing about it.

**`fixtures_are_sanitised`, a test the generated suite runs.** `gp observe fixtures` already writes a `.meta.json` sidecar beside each capture; it gains two fields, `capture_sha256` (the hash of the capture body) and `flagged_names` (the cookie and token names the digest recorded for that host). The sidecar travels with the fixture: the guide's recipe copies both files into `tests/fixtures/`, and `FixtureSession` already reads the sidecar. `graftpunk.testing` gains a pytest check that `gp plugin new` wires into the generated `tests/conftest.py` beside `site_env_scrubber`: for every fixture under `tests/fixtures/` that has a sidecar, the fixture's hash must differ from `capture_sha256` and none of `flagged_names` may appear in the fixture body; a fixture without a sidecar was not derived from a capture and is skipped, with the skip reason naming it. The check therefore runs in any clone and on the user's CI, with no capture tree present. Its limits are stated in the guide and in the check's docstring: it catches an unchanged copy and a leaked flagged name; it does not judge whether invented content is invented well.

**`gp plugin check`, a lint.** Fails while any `GP-FILL` marker remains in the plugin module, listing each. It is a lint, not a test, so a freshly scaffolded project's test suite still means what test suites mean; the project's gate becomes four commands (`pytest`, `ruff check .`, `ruff format --check .`, `gp plugin check`), and the skill runs the same four.

The plan orders the work as two pull requests: the package changes with their tests, docs, and changelog line (a minor release of graftpunk, since the skill's `SKILL_REQUIRES_GRAFTPUNK` floor names it), then the marketplace and the skill. `plugin_app`'s help string becomes "Scaffold, extend, inspect, and check a graftpunk plugin project".

> **Design note (2026-09-21):** this section exists because the design reviewer flagged, as net-negative, the first draft's out-of-package parser of plugin projects, its hand-written command stub, and its unenforced sanitisation rule. All three are now package features with tests, and the skill calls them.

> **Design note (2026-09-21, re-review):** the re-review flagged two net-negatives in the first redesign: the command-to-endpoint mapping was deduced from request call-site literals (which the generator renders as f-strings for parameterised paths, so the mapping would have been `null` exactly where enhance mode needs it), and the sanitisation check compared fixtures against a capture tree the house rules say never to commit, so it would have passed silently in every clone. The endpoint is now a declaration the generator writes and the reader reads; the sanitisation inputs travel with the fixture in its committed sidecar. The re-review's advisories are also folded in here: the payload is defined by subject with a schema version, insertion is a stated invariant rather than "append", the agreed command names reach the generator through `--command`, the #208 compensation moved into the generator, and the marker check became a lint.

## The skill

### Frontmatter

```yaml
---
name: graft
description: Create a graftpunk site plugin from a browser recording, or add commands to an existing one. Use when asked to build, scaffold, or extend a graftpunk plugin for a site, or to add a command to a plugin.
argument-hint: "[plugin-name] [site-url]"
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/preflight.sh *) Bash(gp plugin info *) Bash(gp observe list) Bash(gp observe show *) Bash(gp observe digest *) Bash(gp observe fixtures *) Bash(gp plugin new *) Bash(gp plugin add-command *)
---
```

`disable-model-invocation` stays at its default (false), so Claude can invoke the skill from a plain request. The pre-approved tools are the read-only and file-writing `gp` commands the flow runs unattended, and the list is also a consent boundary: `gp <plugin> login` and any command against the live site are deliberately absent, so they go through the normal permission prompt, which is the user's go-ahead for the live check. `SKILL.md` carries a one-line comment above the list saying so, and the skill's test pins the exact set (see Testing), so a later "add `gp x` to avoid a prompt" edit is a two-file change that shows in the diff as a test change. (`gp session list` is not needed: `gp plugin info` reports whether a session is cached.)

### Modes

The mode comes from the working directory, and the package decides it: preflight runs `gp plugin info --json` (see Package changes, delivered first) and relays its answer.

- `mode: create` (no `pyproject.toml`): scaffold into the working directory.
- `mode: enhance` (a `pyproject.toml` with a `[project.entry-points."graftpunk.plugins"]` table): the answer carries the plugin module path, `site_name`, `base_url`, and every existing command with the method and path template of the request it makes.
- `mode: foreign` (a `pyproject.toml` without that table): stop with "this directory holds a project that is not a graftpunk plugin; run the skill in an empty directory or in the plugin's project".

Arguments: `$0` is the plugin name and `$1` the site URL in create mode; both are ignored with a note in enhance mode, where the plugin name comes from the package's answer. A create-mode invocation with no arguments asks for the name and the URL, one question each.

> **Design note (2026-09-21):** the first draft had preflight parse `pyproject.toml` and the plugin module itself, in bash and `python3` under `skills/`, and re-derive the reserved command list. The design reviewer flagged that as a second, untested reader of a format the package owns, which a future layout change would break silently. The package now answers every question about a plugin project through one read-only command, and preflight only relays it.

### The flow

Each step names the guide heading it follows. The skill reads a reference file only at the step that needs it, and reads the guide section itself only when the reference says to.

1. **Frame** (guide: Frame). Create mode collects the plugin name (validated against the name rule the guide states; a reserved name is refused with the list preflight printed), the site URL, and what the user wants to do on the site, in plain words: "see my orders and download invoices" is enough. It does not ask for command names or endpoints; those come from the digest. Enhance mode collects only the new thing the user wants to do. The login shape and the backend are decided by the skill from the digest later, and confirmed with the user only when the digest is ambiguous (an identity-provider redirect, or no login at all).
2. **Capture** (guide: Capture). The skill prints the exact command for the user to run in the session with the `!` prefix, `! gp observe --no-session interactive <url>`, tells the user to log in, exercise every flow they named, and stop with Ctrl+C, and then waits. UNVERIFIED: that a Ctrl+C typed during shell mode (https://code.claude.com/docs/en/interactive-mode) reaches `gp` as SIGINT so the HAR is saved; the interactive-mode documentation describes Ctrl+C only as interrupting the running operation. The plan's first task probes this on the workstation; if it does not hold, the hand-off tells the user to run the recorder in a separate terminal instead. When the user says the recording is done, the skill runs `gp observe list`, takes the newest run for the inferred session name, and repeats the name back. It never runs the interactive command itself.
3. **Understand** (guide: Understand). The skill runs `gp observe digest <session name> --json`, reads the model, and proposes the commands: a table with one row per candidate (name, what it returns, the endpoint behind it, the parameters seen), mapped to the wants the user stated, with any want that has no endpoint named as needing another recording. The rules that build the table live in `digest.md` and are stated once, in "The command proposal, in detail" below. The user keeps, renames, or drops rows in one answer.
4. **Scaffold** (guide: Scaffold). `gp plugin new <name> --from-run <session name> --command "<METHOD> <template>=<agreed name>"` once per row the user kept (the newest run for that session; `--run <run id>` pins an older one). The generator writes only those stubs, under those names, with the request call it always writes (`ctx.request_json` with `role="xhr"` for a JSON endpoint, `ctx.request_text` with `role="navigation"` for a document) and the endpoint declared on each. The skill edits nothing the generator wrote in this step.
5. **Implement** (guide: Implement). For each stub: fill the request, name the parameters, decide the return shape, and replace every `GP-FILL` marker. Errors raise `CommandError` or `PluginError`.
6. **Harden** (guide: Harden). `gp observe fixtures <session name> --match "<METHOD> <template>"` for each command; each captured file and its sidecar are copied into `tests/fixtures/` and the fixture's content is invented with its structure kept (the guide's recipe). The generated project's own suite proves the copy was changed and leaks no flagged name (`fixtures_are_sanitised`, under Package changes), one test per command with `fixture_context`, then the project's gate: `pytest`, `ruff check .`, `ruff format --check .`, and `gp plugin check`, which fails while any `GP-FILL` marker remains, so step 5 cannot be skipped. The gate has to pass before the next step.
7. **Kick the tires** (guide: Check the CLI surface you shipped, and Login). With the user's go-ahead, the skill runs `gp <name> --help` and checks every agreed command name is there, then `gp <name> login` and one read-only command against the live site while the user watches. Credentials come from the environment or the workstation env file, which the user sets up (the skill prints the `gp config set` lines with placeholders and never the values). A login failure is diagnosed against the guide's Login section (the failure text, the success signals, the timeout) and the plugin's `LoginConfig` adjusted; the check reruns once.
8. **Publish checklist** (guide: Before you publish). The skill reads the guide's checklist section and walks it item by item, fixes what it can, and stops with the remaining items listed for the user.

The skill restates the step it is on at the start of every turn (one line: "Step 4 of 8: scaffold. Done: ...; next: ...") and asks one question at a time.

### Where enhance mode differs

The eight steps above are written for create mode. Enhance mode runs the same steps with exactly these differences, and this list is the only place they are stated, so a change to enhance mode is one edit here:

- Step 1 collects only the new thing the user wants to do; the plugin name, `site_name`, and `base_url` come from `gp plugin info`.
- Step 2 records with `-s <session name>` when `gp plugin info` reports a cached session, and with `--no-session` otherwise.
- Step 3 excludes the endpoints existing commands declare (`gp plugin info` reports each command's `endpoint`), marks every remaining row as new, and, for each existing command reported with `declared: false`, adds a row of its own that says "existing command, endpoint not declared" so the user can say whether a proposed row duplicates it; the skill never silently drops or silently proposes over an undeclared command.
- Step 4 does not run `gp plugin new`. For each agreed command it runs `gp plugin add-command <name> --from-run <session name> --endpoint "<METHOD> <template>" --command <agreed name>`, which renders one stub in the generated shape into the plugin class and prints the fixture path its test will look for.
- Step 8 is limited to the checklist items the new commands touch.

> **Design note (2026-09-21):** the first draft threaded "create mode does X, enhance mode does Y" through six of the eight steps, wrote the enhance-mode stub by hand in prose (a second, untested copy of the generator's template), matched existing commands by parsing their `ctx.request_*` call sites, and left the capture sanitisation rule and the `GP-FILL` rule to the model's diligence. The design reviewer flagged the hand-written stub, the call-site matching, and the unenforced sanitisation as net-negative and the rest as advisory. The steps are now mode-neutral with one divergence list; the stub, the command-to-request mapping, the sanitisation check, and the marker check all live in the package, where they are tested, and the skill calls them.

### The command proposal, in detail

The user should never need to know the commands in advance. The digest is the source: every first-party, non-static endpoint the recording saw, with its method, templated path, query and body parameters and their observed types, response shape, and custom header names. The markdown form prints the first `--limit` endpoints (default 60) and says how many it held back, so the skill passes `--json` and reads the whole model; the role a command's request needs is decided from the endpoint's content type (JSON becomes `request_json` with `role="xhr"`, anything else `request_text` with `role="navigation"`), not read off the digest. The skill turns the model into a proposal by these rules, in order:

- Drop the login flow's own endpoints (the digest lists them under `## Endpoints` like any other; cross-reference `## Login`, whose observations name each auth URL, to identify them), and trust the digest for the rest: static assets, analytics and tracking hosts, and anything on a host other than the primary one never reach the endpoint list at all.
- Keep JSON endpoints and HTML documents that carry the user's own data (a dashboard, an order list, a statement page); drop navigation chrome.
- Name each kept endpoint as a verb phrase a person would type: `orders` for `GET /api/orders`, `order` for `GET /api/orders/{order_id}` (the path parameter becomes the command's argument), `invoice-pdf` for a document download. Collapse a list-and-detail pair into two commands, never one.
- Present the proposal as a table with one row per command: name, what it returns (from the shape), endpoint, parameters. Say which of the user's stated wants each row serves, and name any want with no endpoint behind it.
- Ask one question: "keep, rename, or drop any of these?" and apply the answer. A want with no endpoint gets the capture step again for that flow, appended to the same run name.

In enhance mode the same rules apply to the new recording, minus the endpoints the existing commands already use.

### The references

Each reference is short (under 120 lines) and holds only what the skill needs at the step that reads it; anything the guide already says is cited by heading and read from the guide at that point, never copied. `rules.md` is an inventory: one line per house rule, each naming the guide heading that states it (entry point registration, never commit a capture, fixtures invent content, parsers never return a confident empty list, exact failure text, secrets from the environment only, resolve a secret by what it is), and the skill reads the cited section when a rule applies. `capture.md` holds the hand-off text and the "what to exercise" prompts, which exist nowhere else. `digest.md` holds the command proposal rules (their single home; the section below is the design that produced them) and a worked example on the guide's synthetic digest. `harden.md` holds the fixture derivation recipe and the test shape, and for the gate and the publish checklist points at the guide's "The gate" and "Before you publish" and tells the skill to read them.

> **Design note (2026-09-21):** the first draft copied the publish checklist and the house rules into the references verbatim with a heading citation as the only guard, and stated the proposal rules in three places (this section, the flow's step 3, and `digest.md`). The design reviewer noted that a citation pins a heading, not the content, so copies drift silently. The references now carry only inventories and pointers for material the guide owns, copy nothing verbatim, and the proposal rules have one home.

### Preflight

`skills/graft/scripts/preflight.sh`, bash, needing only `gp` on the path. It owns exactly what must be decided before anything else runs and nothing it would have to interpret: it checks that `gp` is present and new enough, runs `gp plugin info --json`, prints that JSON unchanged, and exits:

- `0` with the JSON on stdout (the `mode`, and for `enhance` the project facts, are the package's answer; preflight adds nothing and interprets nothing, so mode policy has one owner, the skill).
- `2` when `gp` is missing, with the install line (`uv tool install graftpunk` or `pip install graftpunk`).
- `3` when `gp --version` is below the floor the skill was written for (a constant at the top of the script, `SKILL_REQUIRES_GRAFTPUNK`, set to the release that ships the package changes; the name is distinct from `scaffold_commands.py`'s `_graftpunk_version_floor()`, which is the version a scaffolded project depends on), with the upgrade line. The comparison is on the version string, never on help output.

The skill runs preflight first on every invocation and stops on any non-zero exit, printing the script's message verbatim; on `0` it reads `mode` and applies the Modes section, including the stop message for `foreign`.

> **Design note (2026-09-21):** the first draft's contract was a `KEY=value` block computed by the script (mode, module path, site name, commands, reserved names) and a capability check that scraped `--help` output. The design reviewer flagged the computed block as a published interface over a duplicate parser and the help scraping as a presentation detail used as a contract. The contract is now the package's own JSON plus a version floor.

## Errors and secrets

Every `gp` command's failure is shown to the user verbatim, the cause diagnosed against the guide, and the step retried once after the fix. No step is skipped, and no failure is summarised away. The skill never asks for a password, never writes a credential anywhere, never reads a capture's cookies, tokens, or HAR body, and never prints a `gp config get --resolve` result; it passes run names and match templates to `gp` and reads only the digest, the fixtures directory, and the generated project. When the user pastes a secret into the conversation, the skill says where it belongs (`gp config set NAME '$(your-secret-tool read ...)'`) and does not use it.

## Documentation changes

`docs/PLUGIN_DEVELOPMENT.md` gains a section "With the skill" after "The six steps at a glance": three sentences on installing and invoking the skill, and one saying the skill follows this guide, so the guide is the reference when the two disagree. `README.md`'s Plugins section gains the two install lines. `CONTRIBUTING.md` gains "Releasing the skill": the version rule, the two files to bump, the CI check, and the optional local hook (`git config core.hooksPath .githooks`). `CHANGELOG.md` `[Unreleased]` Added gets one line.

## Versioning and release

The skill's version is independent of the graftpunk package version. Any change under `skills/` requires a patch bump of both `version` fields (`.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`), because Claude Code caches an installed plugin under a version-stamped path, `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`, and a new version string means a new cache directory (https://code.claude.com/docs/en/plugins-reference). UNVERIFIED: that an unchanged version string leaves stale content in place. The documentation does not state it; `stavxyz/skills` RELEASING.md reports the cache "isn't reliably invalidated when the version is unchanged" and cites claude-code issues #46081, #14061, and #17361. Minor bumps are for a new skill or a changed invocation contract.

One guard, in CI. `.github/workflows/skill-version.yml` runs on pull requests: when the diff against the base commit touches `skills/` or `.claude-plugin/`, the two version fields must be equal to each other and different from the base's; otherwise the job fails and names the next patch. The check is `scripts/check-skill-version.sh <base sha>`, also runnable by hand and as `just skill-version` before pushing. No git hook: `core.hooksPath` is repository-wide and exclusive, and a local backstop for a check that already blocks the merge is not worth that commitment.

After a merge, users update with `/plugin marketplace update graftpunk` and `/plugin update`.

> **Design note (2026-09-21):** the first draft mirrored the `stavxyz/skills` opt-in pre-push hook beside the CI check. The design reviewer noted the hook adds a repository-wide convention for a check CI already enforces; the CI job and a `just` recipe replace it.

## Testing

`tests/unit/test_graft_skill.py`, in the existing unit suite so the normal gate runs it:

- Every guide heading cited in `SKILL.md` and the four references exists in `docs/PLUGIN_DEVELOPMENT.md` (reusing the slug helper from `tests/unit/test_plugin_development_guide.py`, which already skips fenced blocks).
- The references copy nothing from the guide: no line of a reference longer than a short phrase appears verbatim in the guide (so the "cite, do not copy" rule is enforced, not hoped), and every rule in `rules.md` names a heading that exists.
- Every `gp` invocation in `SKILL.md` and the references resolves to a real command with real options, through the same walker the guide test uses (the interactive command included: it is written for the user to run, and it still has to parse).
- `SKILL.md` frontmatter parses, `name` is `graft`, the description names both modes, every path in `allowed-tools` exists, and the `allowed-tools` entries equal a list written in the test, which also asserts that `gp <plugin> login` and every non-`gp` command stay absent (the consent boundary, pinned).
- The two manifest `version` fields are equal, and `plugin.json`'s `name` matches the marketplace's plugin entry.
- Preflight, run on scratch trees, exits `0` and prints the JSON of `gp plugin info` unchanged for an empty directory, for a project written by `gp plugin new`, and for a directory with a foreign `pyproject.toml` (whose JSON says `mode: foreign`), and exits `3` when `PATH` holds a fake `gp` whose `--version` is below `SKILL_REQUIRES_GRAFTPUNK`.
- `scripts/check-skill-version.sh` fails on a `skills/` change without a bump and passes with one (run against two throwaway git commits in a temporary repository).

The package changes carry their own unit tests beside the code they extend: `@command(endpoint=...)` stored on the metadata and absent from help; `gp plugin info --json` on the three directory states, on a generated plugin (every command `declared: true`), and on a hand-written plugin with one undeclared command (`declared: false`, `endpoint: null`), plus the `schema` field; `gp plugin new --command` writing only the selected stubs under the given names and refusing a name that collides or an endpoint the digest lacks; `gp plugin add-command` inserting one stub after the plugin class's last method in a module written by `gp plugin new`, refusing a duplicate name, a login-flow endpoint, and a module whose class is not last, and printing the fixture path; the generator emitting explicit `PluginParamSpec` entries for typed parameters; `gp observe fixtures` writing `capture_sha256` and `flagged_names` into the sidecar; `fixtures_are_sanitised` failing on an unchanged copy and on a leaked flagged name, passing on an invented one, and skipping a fixture with no sidecar with a reason that names it; `gp plugin check` failing while a marker remains and passing when none does.

> **Design note (2026-09-21):** the design reviewer noted that the first draft's tests proved citations resolve, not that the skill and the guide agree, while the goals section claimed a test proves the two never disagree. The references now copy nothing, so agreement reduces to two mechanical checks (no verbatim copies; every cited heading exists), and both are tests. The goal is stated in those terms.

A full dry run of the skill (create mode against a recording of a real site, or `gp observe digest --har tests/fixtures/sample.har` where a live capture is not available, then enhance mode adding one command) is a manual test-plan item on the pull request, recorded with the commands run and their output.

## Rejected approaches

A thin skill that only tells the model to read the guide: no duplication, but every run reloads 1,100 lines, the per-step checklists that make a run reliable are missing, and nothing pins the skill to the guide's headings. A `gp plugin dev` pipeline command that runs digest, scaffold, and fixtures in one go: fewer model steps, but it moves orchestration into the CLI as a new product surface with a release dependency, and enhance mode needs judgment a pipeline cannot supply. Two skills (`new-plugin` and `enhance-plugin`): the flows share six of eight steps, so they would drift.

## Out of scope

The introspector's handling of string and optional annotations (#208; the skill works around it with explicit `PluginParamSpec` entries). Naming a first capture and `gp plugin new --new` (#210; the skill uses the inferred name). The README follow-ups in #211. A `gp` pipeline that chains digest, scaffold, and fixtures into one command (orchestration stays in the skill).

> **Design note (2026-09-21):** the first draft deferred "a generator that adds a command to an existing module" behind "if this comes up often" while shipping a prose copy of the generator's template. The design reviewer flagged the deferral as making that duplication permanent, since enhance mode is half the skill's purpose. The single-command generator is in scope as `gp plugin add-command` (see Package changes, delivered first).
