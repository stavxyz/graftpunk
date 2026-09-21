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

Goals: a developer with a site login and no graftpunk knowledge gets a working, tested, publishable plugin package by following the skill, without reading the guide first; the skill's steps and the guide never disagree, and a test proves it; the skill is versioned and released independently of the graftpunk package.

Non-goals: driving the browser recording from the skill (Claude Code cannot press Ctrl+C in an interactive command); a `gp` pipeline command that bundles digest, scaffold, and fixtures (orchestration stays in the skill, see the rejected approaches); handling secrets in any form.

## Repository layout

```text
.claude-plugin/marketplace.json     the marketplace: name graftpunk, one plugin, source ./
.claude-plugin/plugin.json          the Claude Code plugin: name graftpunk, version 0.1.0
skills/graft/SKILL.md               the skill: frontmatter and the flow
skills/graft/references/rules.md    the house rules as a checklist, each citing a guide heading
skills/graft/references/capture.md  the capture hand-off: the command, what to exercise, how to stop
skills/graft/references/digest.md   how to read a digest and turn endpoints into a command proposal
skills/graft/references/harden.md   fixtures, tests, the gate, the publish checklist
skills/graft/scripts/preflight.sh   gp present and new enough; mode detection; existing commands
tests/unit/test_graft_skill.py      the skill's own tests (see Testing)
.github/workflows/skill-version.yml the version-bump check on pull requests
.githooks/pre-push                  the same check locally, opt-in
docs/PLUGIN_DEVELOPMENT.md          gains one short section, "With the skill"
README.md                           gains the two install lines under Plugins
CONTRIBUTING.md                     gains "Releasing the skill" (the version rule)
```

`skills/` sits at the repository root because a plugin whose `source` is `./` loads `skills/<name>/SKILL.md` from the root; the official marketplace documentation and the `stavxyz/skills` repository both use this layout. The Python package under `src/` is unaffected: `skills/` is not a Python package and is excluded from the sdist by the existing allowlist.

## The manifests

`.claude-plugin/marketplace.json`:

```json
{
  "$schema": "https://anthropic.com/claude-code/marketplace.schema.json",
  "name": "graftpunk",
  "owner": {"name": "stavxyz"},
  "metadata": {
    "description": "graftpunk's Claude Code skills: /graftpunk:graft creates or enhances a site plugin",
    "version": "0.1.0"
  },
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

## The skill

### Frontmatter

```yaml
---
name: graft
description: Create a graftpunk site plugin from a browser recording, or add commands to an existing one. Use when asked to build, scaffold, or extend a graftpunk plugin for a site, or to add a command to a plugin.
argument-hint: "[plugin-name] [site-url]"
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/preflight.sh *) Bash(gp observe list) Bash(gp observe show *) Bash(gp observe digest *) Bash(gp observe fixtures *) Bash(gp plugin new *)
---
```

`disable-model-invocation` stays at its default (false), so Claude can invoke the skill from a plain request. The pre-approved tools are the read-only and file-writing `gp` commands the flow runs unattended; `gp <plugin> login` and any command against the live site are not pre-approved, so they go through the normal permission prompt, which is the user's go-ahead for the live check.

### Modes

Preflight decides the mode from the working directory:

- No `pyproject.toml`: create mode, scaffold into the working directory.
- A `pyproject.toml` with a `[project.entry-points."graftpunk.plugins"]` table: enhance mode. Preflight prints the plugin module path, the plugin's `site_name`, `base_url`, and the names of its existing commands (read from the module with `ast`, never imported, so a broken plugin does not break preflight).
- A `pyproject.toml` without that table: stop with "this directory holds a project that is not a graftpunk plugin; run the skill in an empty directory or in the plugin's project".

Arguments: `$0` is the plugin name and `$1` the site URL in create mode; both are ignored with a note in enhance mode, where the plugin name comes from preflight. A create-mode invocation with no arguments asks for the name and the URL, one question each.

### The flow

Each step names the guide heading it follows. The skill reads a reference file only at the step that needs it, and reads the guide section itself only when the reference says to.

1. **Frame** (guide: Frame). Create mode collects the plugin name (validated against the name rule the guide states; a reserved name is refused with the list preflight printed), the site URL, and what the user wants to do on the site, in plain words: "see my orders and download invoices" is enough. It does not ask for command names or endpoints; those come from the digest. Enhance mode collects only the new thing the user wants to do. The login shape and the backend are decided by the skill from the digest later, and confirmed with the user only when the digest is ambiguous (an identity-provider redirect, or no login at all).
2. **Capture** (guide: Capture). The skill prints the exact command for the user to run in the session with the `!` prefix, `! gp observe --no-session interactive <url>` in create mode and `! gp observe -s <name> interactive <url>` in enhance mode when a session is cached (the skill checks with `gp session list`; without one it uses `--no-session`), tells the user to log in, exercise every flow they named, and stop with Ctrl+C, and then waits. When the user says the recording is done, the skill runs `gp observe list`, takes the newest run for the inferred name, and repeats the name back. It never runs the interactive command itself.
3. **Understand** (guide: Understand). The skill runs `gp observe digest <name>` and reads the summary, login observations, token candidates, and endpoints. This is where commands are proposed. The skill ranks the endpoints (JSON endpoints first, then documents that carry the user's data; login-flow endpoints, static assets, analytics, and third-party hosts dropped) and presents a table: proposed command name, what it returns, the endpoint behind it, and the parameters seen. The user keeps, renames, drops, or asks for one the digest lacks; a missing one means another recording of that flow, and the skill says so rather than guessing. In enhance mode the table excludes endpoints already covered by existing commands (matched by method and templated path against the module's `request_json` and `request_text` calls, which preflight lists) and marks the rest as new.
4. **Scaffold** (guide: Scaffold). Create mode runs `gp plugin new <name> --from-run <run name>`, then renames the generated stubs to the names agreed in step 3 (the generator derives names from paths, which the guide says are usually wrong for a person to type) and deletes stubs the user dropped. Enhance mode does not scaffold: nothing adds a command to an existing module, so the skill writes each new stub by hand in the generated shape (the `@command` decorator with help text, a `GP-FILL` marker for anything undecided, `ctx.request_json` or `ctx.request_text` with the role, path, and parameters from the digest), appends it to the plugin module, and adds the matching fixture path to the module's `Next:` list if the project keeps one.
5. **Implement** (guide: Implement). For each stub: fill the request, name the parameters, decide the return shape, and replace every `GP-FILL` marker. Parameters get explicit `PluginParamSpec` entries when they need a type, because the generated module's future-annotations import makes introspected options strings (guide: CLI parameter types; issue #208). Errors raise `CommandError` or `PluginError`.
6. **Harden** (guide: Harden). `gp observe fixtures <name> --match "<METHOD> <template>"` for each command, the captured files sanitised by hand into `tests/fixtures/` (structure kept, content invented; the skill shows the diff of what it changed and never copies a capture unchanged), one test per command with `fixture_context`, then the project's gate: `pytest`, `ruff check .`, `ruff format --check .`. The gate has to pass before the next step.
7. **Kick the tires** (guide: Check the CLI surface you shipped, and Login). With the user's go-ahead, the skill runs `gp <name> --help` and checks every agreed command name is there, then `gp <name> login` and one read-only command against the live site while the user watches. Credentials come from the environment or the workstation env file, which the user sets up (the skill prints the `gp config set` lines with placeholders and never the values). A login failure is diagnosed against the guide's Login section (the failure text, the success signals, the timeout) and the plugin's `LoginConfig` adjusted; the check reruns once.
8. **Publish checklist** (guide: Before you publish). The skill walks the guide's checklist item by item, fixes what it can, and stops with the remaining items listed for the user. In enhance mode the checklist is limited to the items the new commands touch.

The skill restates the step it is on at the start of every turn (one line: "Step 4 of 8: scaffold. Done: ...; next: ...") and asks one question at a time.

### The command proposal, in detail

The user should never need to know the commands in advance. The digest is the source: every endpoint the recording saw, with its method, templated path, parameters and their types, response shape, and role. The skill turns that into a proposal by these rules, in order:

- Drop the login flow's own endpoints, static assets, analytics and tracking hosts, and anything on a host other than the primary one (the digest already marks these; the skill does not second-guess it).
- Keep JSON endpoints and HTML documents that carry the user's own data (a dashboard, an order list, a statement page); drop navigation chrome.
- Name each kept endpoint as a verb phrase a person would type: `orders` for `GET /api/orders`, `order` for `GET /api/orders/{order_id}` (the path parameter becomes the command's argument), `invoice-pdf` for a document download. Collapse a list-and-detail pair into two commands, never one.
- Present the proposal as a table with one row per command: name, what it returns (from the shape), endpoint, parameters. Say which of the user's stated wants each row serves, and name any want with no endpoint behind it.
- Ask one question: "keep, rename, or drop any of these?" and apply the answer. A want with no endpoint gets the capture step again for that flow, appended to the same run name.

In enhance mode the same rules apply to the new recording, minus the endpoints the existing commands already use.

### The references

Each reference is short (under 120 lines) and every rule in it cites the guide heading it comes from, so the test can prove the citations resolve. `rules.md` holds the house rules as a checklist (entry point registration, never commit a capture, fixtures invent content, parsers never return a confident empty list, exact failure text, secrets from the environment only, resolve a secret by what it is). `capture.md` holds the hand-off text and the "what to exercise" prompts. `digest.md` holds the proposal rules above and a worked example on the guide's synthetic digest. `harden.md` holds the fixture derivation recipe, the test shape, the gate, and the publish checklist verbatim from the guide's "Before you publish".

### Preflight

`skills/graft/scripts/preflight.sh`, bash, no dependencies beyond `python3` and `gp` on the path. It prints `KEY=value` lines and exits:

- `0` with `MODE=create` or `MODE=enhance` plus, for enhance, `PLUGIN_MODULE=<path>`, `SITE_NAME=<name>`, `BASE_URL=<url>`, `COMMANDS=<comma-separated names>`, `SESSION_CACHED=yes|no`; for both, `GP_VERSION=<x.y.z>` and `RESERVED=<comma-separated top-level command names>`.
- `2` when `gp` is missing, with the install line (`uv tool install graftpunk` or `pip install graftpunk`).
- `3` when the installed `gp` lacks a command the skill needs (`gp observe digest --help` or `gp plugin new --help` without `--from-run` fails), with the upgrade line.
- `4` when the working directory holds a project that is not a graftpunk plugin.

The skill runs preflight first on every invocation and stops on any non-zero exit, printing the script's message verbatim.

## Errors and secrets

Every `gp` command's failure is shown to the user verbatim, the cause diagnosed against the guide, and the step retried once after the fix. No step is skipped, and no failure is summarised away. The skill never asks for a password, never writes a credential anywhere, never reads a capture's cookies, tokens, or HAR body, and never prints a `gp config get --resolve` result; it passes run names and match templates to `gp` and reads only the digest, the fixtures directory, and the generated project. When the user pastes a secret into the conversation, the skill says where it belongs (`gp config set NAME '$(your-secret-tool read ...)'`) and does not use it.

## Documentation changes

`docs/PLUGIN_DEVELOPMENT.md` gains a section "With the skill" after "The six steps at a glance": three sentences on installing and invoking the skill, and one saying the skill follows this guide, so the guide is the reference when the two disagree. `README.md`'s Plugins section gains the two install lines. `CONTRIBUTING.md` gains "Releasing the skill": the version rule, the two files to bump, the CI check, and the optional local hook (`git config core.hooksPath .githooks`). `CHANGELOG.md` `[Unreleased]` Added gets one line.

## Versioning and release

The skill's version is independent of the graftpunk package version. Any change under `skills/` requires a patch bump of both `version` fields (`.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`), because Claude Code caches an installed plugin under a version-stamped path and only refetches when the version string changes (the official plugin documentation states this, and `stavxyz/skills` documents the same rule). Minor bumps are for a new skill or a changed invocation contract.

Two guards. `.github/workflows/skill-version.yml` runs on pull requests: when the diff against the base commit touches `skills/` or `.claude-plugin/`, the two version fields must be equal to each other and different from the base's; otherwise the job fails and names the next patch. `.githooks/pre-push` does the same locally against `origin/main`, opt-in through `core.hooksPath`, mirroring the `stavxyz/skills` hook.

After a merge, users update with `/plugin marketplace update graftpunk` and `/plugin update`.

## Testing

`tests/unit/test_graft_skill.py`, in the existing unit suite so the normal gate runs it:

- Every guide heading cited in `SKILL.md` and the four references exists in `docs/PLUGIN_DEVELOPMENT.md` (reusing the slug helper from `tests/unit/test_plugin_development_guide.py`, which already skips fenced blocks).
- Every `gp` invocation in `SKILL.md` and the references resolves to a real command with real options, through the same walker the guide test uses (the interactive command included: it is written for the user to run, and it still has to parse).
- `SKILL.md` frontmatter parses, `name` is `graft`, the description names both modes, and every path in `allowed-tools` exists.
- The two manifest `version` fields are equal, and `plugin.json`'s `name` matches the marketplace's plugin entry.
- Preflight, run on scratch trees, exits `0` with `MODE=create` on an empty directory, `0` with `MODE=enhance` and the right `COMMANDS` on a project written by `gp plugin new`, and `4` on a directory with a foreign `pyproject.toml`; and exits `3` when `PATH` holds a fake `gp` whose `plugin new --help` lacks `--from-run`.
- The CI version check's script, factored into `scripts/check-skill-version.sh` so the workflow and the hook share it, fails on a `skills/` change without a bump and passes with one (run against two throwaway git commits in a temporary repository).

A full dry run of the skill (create mode against the guide's synthetic recording, then enhance mode adding one command) is a manual test-plan item on the pull request, recorded with the commands run and their output.

## Rejected approaches

A thin skill that only tells the model to read the guide: no duplication, but every run reloads 1,100 lines, the per-step checklists that make a run reliable are missing, and nothing pins the skill to the guide's headings. A `gp plugin dev` pipeline command that runs digest, scaffold, and fixtures in one go: fewer model steps, but it moves orchestration into the CLI as a new product surface with a release dependency, and enhance mode needs judgment a pipeline cannot supply. Two skills (`new-plugin` and `enhance-plugin`): the flows share six of eight steps, so they would drift.

## Out of scope

The introspector's handling of string and optional annotations (#208; the skill works around it with explicit `PluginParamSpec` entries). Naming a first capture and `gp plugin new --new` (#210; the skill uses the inferred name). The README follow-ups in #211. A generator that adds a command to an existing module (enhance mode writes the stub by hand; if this comes up often, it becomes a `gp plugin add-command` proposal on its own).
