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
src/graftpunk/plugins/cli_plugin.py            @command(endpoint=...) stored on the command metadata
src/graftpunk/har/digest.py                    login_flow on every endpoint (moved from the generator)
src/graftpunk/har/naming.py                    parse_endpoint: the one parser for "<METHOD> <template>"
src/graftpunk/devtools/plugin_project.py       the one reader: resolve and classify the directory's plugin project, one structural view
src/graftpunk/devtools/scaffold/project.py     PROJECT_GATE, PROJECT_REQUIREMENTS, and the fixtures-root rule, beside the other project policy
src/graftpunk/devtools/scaffold/render.py      a single-command render entry point; the declared endpoint and typed params on every stub
src/graftpunk/devtools/scaffold/insert.py      the mutator: place a rendered stub after the plugin class's last command
src/graftpunk/devtools/scaffold/upgrade.py     the migrator behind gp plugin upgrade, applying PROJECT_REQUIREMENTS
src/graftpunk/devtools/plugin_check.py         the lint behind gp plugin check
src/graftpunk/devtools/captures.py             the sidecar writer, importing the schema from its owner
src/graftpunk/cli/scaffold_commands.py         thin entry points for generation and mutation: gp plugin new --command, add-command
src/graftpunk/cli/plugin_project_commands.py   thin entry points for inspection, migration, and linting: gp plugin info, upgrade, check
src/graftpunk/cli/main.py                      gp version --json
src/graftpunk/testing/sidecar.py               the sidecar's one owner: schema, keys, loader (pytest-free, plugin-facing)
src/graftpunk/testing/plugin.py                fixtures_are_sanitised, wired into the generated conftest (pytest side)
src/graftpunk/cli/observe_commands.py          gp observe fixtures writes the sidecar through the devtools writer
```

`skills/` sits at the repository root because a plugin whose `source` is `./` loads `skills/<name>/SKILL.md` from the root; the official marketplace documentation states that `source: "./"` means the plugin is the marketplace root and that skills load from `skills/<name>/SKILL.md`, and the `stavxyz/skills` repository is a working example of exactly this root layout. The Python package under `src/` is unaffected: `skills/` is not a Python package and is excluded from the sdist by the existing allowlist.

## The manifests

`.claude-plugin/marketplace.json`:

```json
{
  "$schema": "https://anthropic.com/claude-code/marketplace.schema.json",
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

The additions to graftpunk itself, in one pull request that merges before the skill's, so the skill never carries knowledge the package owns. Each reports a fact, renders through the existing generator, migrates a project on request, or checks an invariant; none orchestrates.

**One owner for "what plugin project is this directory".** A new module, `src/graftpunk/devtools/plugin_project.py`, resolves the working directory to a plugin project (`pyproject.toml`, the `[project.entry-points."graftpunk.plugins"]` table, the module path), classifies it (`create`, `enhance`, or `foreign`), and reads the module with `ast`, never importing it, so a broken plugin still gets an answer. It returns one structural view that every consumer works from: the plugin class with its source span (there must be exactly one `SitePlugin` subclass; the refusal message for anything else is this module's), its `site_name` and `base_url`, each `@command`-decorated method with its span, its decorator keywords (the `endpoint` literal if present), and the method literal and the literal prefix of the path its `ctx.request_json` or `ctx.request_text` call passes, the location of every `GP-FILL` marker in the module, and the project's resolved fixtures root (see below). That is the whole set of facts the info command, the mutator, the lint, and the sanitisation check need, so none of them parses on its own; the rule is "one read, four consumers", and the view grows here when a consumer needs more, though a fourth body-level need is the point to reconsider the view rather than extend it. The CLI layer (`src/graftpunk/cli/scaffold_commands.py`, whose docstring says "argument handling only") stays that way: thin entry points calling into `devtools`.

**The digest names the login flow's endpoints.** `_login_flow_endpoints`, which the generator computes today in `render.py` to skip the login flow when it renders stubs, moves into the digest model: each endpoint in `gp observe digest --json` carries a `login_flow` boolean, and the generator reads it instead of recomputing. The skill's proposal table filters on the same field, so the proposal and the scaffold cannot disagree about which endpoints are the login flow.

**`gp version --json`.** Prints the installed version as data, `{"graftpunk": "<X.Y.Z>"}`, beside the existing human panel. Preflight compares that value against its floor; an installed `gp` too old to have the option fails the option parse, which is itself the "too old" signal, so preflight never reads rendered text.

**A declared endpoint on every generated command.** `@command` gains an optional `endpoint: str | None = None` keyword, stored on `CommandMetadata` and never consulted at runtime or shown in help. The generator writes it on every stub it renders, `@command(help=..., endpoint="GET /api/orders/{order_id}")`, so the endpoint a command implements is a declaration the generator owns, never a deduction from the request call (which the generator renders as an f-string for a parameterised path, so a call-site reader would fail exactly where it matters). A hand-written command may carry the keyword or not. This is a deliberate exception to the rule in `devtools/__init__.py` that tooling lives in `devtools` and the top-level API is what a plugin uses: the field is tooling provenance on a runtime object, documented as such in the decorator's docstring, and the alternative (a second, devtools-owned decorator on every generated command) would cost every plugin author more than one ignored keyword does. The exception is recorded here so the rule stays a rule.

**`gp plugin info --json`.** The facts the scaffold tooling can answer about this invocation without leaving the process or the directory, as one JSON object: a `schema: 1` field, `mode`, `reserved` (the top-level command names from `reserved_cli_names()`, which the skill needs before the capture step), and for `enhance` the `module` path, `site_name`, `base_url`, and `commands`, a list of `{name, endpoint}` where `endpoint` is the declared literal or `null` for a command that carries none (the two states never need a third field to tell apart). Within a schema version fields are added and never renamed or removed, and a test pins the field set per schema version so a rename fails the suite. The subject rule is stated so it can be applied: a fact is admitted when it is in-process, deterministic, and needed by the flow before the capture step; session facts fail it (the session store is pluggable and may be a remote credentialed backend) and belong to `gp session list`, which the skill calls itself; the installed version has its own machine surface (`gp version --json`). `--json` is the only output form (the command exists for a machine; a human reads `gp plugin new`'s output and the module). Exit 0 in every mode; the mode is data, not an error.

**One encoding for "this endpoint, under this name", with one parser.** Both scaffold commands take `--command "<name>=<METHOD> <template>"`: the name first, then `=`, then the endpoint as the digest prints it. A command name cannot contain `=`, so the split is unambiguous, and the skill, the tests, and the docs carry one shape. The `<METHOD> <template>` half already has a private validator in `observe_commands.py` (`_validate_match_patterns`, which exists because a value that splits wrong matches nothing and looks like an empty result rather than a typo); that parser and its refusal move to `src/graftpunk/har/naming.py`, which already owns the method-and-template stem, as `parse_endpoint`, and all four consumers (`gp observe fixtures --match`, `gp plugin new --command`, `gp plugin add-command --command`, and the skill's own composition of the string from the digest) go through it. The `--command` wrapper owns only the outer split on the first `=`.

**`gp plugin new --command "<name>=<METHOD> <template>"`, repeatable.** With `--from-run`, selects which digest endpoints become stubs and names them, so the names agreed in the proposal step reach the generator instead of being applied to generated code afterwards, and the fixture path rule stays entirely inside the package. Explicit selection is not subject to the twelve-stub cap that applies to the unselected case (`_MAX_SCAFFOLD_ENDPOINTS` in `render.py`). Without `--command` the behaviour is unchanged (every eligible endpoint, generated names, the cap).

**`gp plugin add-command <plugin> --from-run <session> --command "<name>=<METHOD> <template>"`.** Renders one command stub for the named endpoint of the session's newest run (`--run` pins an older one) in exactly the shape `gp plugin new` writes, through a single-command entry point in `render.py` that `gp plugin new` also uses for each of its stubs, and a mutator in `src/graftpunk/devtools/scaffold/insert.py` places it inside the plugin class immediately after the class's last `@command`-decorated method, wherever the class sits in the module, using the spans the shared reader returns. The placement policy is therefore code, not a docstring convention, and a module a developer has extended with helpers above or below the class is handled rather than refused; the only structural requirement is the reader's (exactly one `SitePlugin` subclass), and a module that fails it is refused with the reader's reason. Refuses when the command name exists, when the endpoint is not in the digest, and when the digest marks the endpoint `login_flow`. It inserts a stub and does nothing else. Prints the fixture path the command's test will look for (the rule `gp plugin new` prints as its `Next:` line and `FixtureSession` uses at read time; for a suite member that is under `tests/fixtures/<plugin>/`, per `fixtures_root`).

**`gp plugin upgrade`.** Brings an existing project up to the current generated shape, on purpose and by name. What that shape requires is declared once, `PROJECT_REQUIREMENTS` in `devtools/scaffold/project.py` beside `PROJECT_GATE`: each requirement names the file, the line or key it must contain, and how to add it (today one entry, the `fixtures_are_sanitised` wiring in `tests/conftest.py`). The renderer emits every requirement when it generates a project, `upgrade` applies the ones a project lacks, and `gp plugin check` reports the ones it lacks; all three read the declaration and none spells a requirement itself, and a test pins the three consumers to it. A future requirement (a gate entry, a second conftest fixture, a `pyproject.toml` key) is one new entry. `upgrade` prints what it changed and changes nothing a project already has; the skill runs it when `check` asks for it.

**Typed parameters rendered by the generator.** Where a stub parameter has a type in the digest, the generator emits the explicit `PluginParamSpec` entry itself, so the compensation for #208 (introspected options arrive as strings under the future-annotations import) lives in `render.py` and is one edit to remove when #208 lands; the skill says nothing about it.

**A sidecar with one owner, safe to commit by construction.** `gp observe fixtures` writes a `.meta.json` sidecar beside each capture; today it carries `url` (the captured request URL, query string included), `captured_at`, `status`, `content_type`, and `body_params`, as a dict literal inside the CLI command, and `FixtureSession` reads two of those keys by name. The format gets one owner, `src/graftpunk/testing/sidecar.py`, on the pytest-free side of `graftpunk.testing`: it declares the schema (`schema: 1`, with the keys `status`, `content_type`, `body_params`, `capture_sha256`, and `flagged_names`) and the loader, and it is the only place the key set is spelled. It sits in the plugin-facing package because `FixtureSession`, which a plugin's tests use, reads sidecars; the writer, which only `gp observe fixtures` runs, lives in `src/graftpunk/devtools/captures.py` and imports the schema from the owner, so the placement rule in `devtools/__init__.py` (plugin-facing API at the top level, tooling in `devtools`) holds on both sides. Every field in it may be committed: `url` and `captured_at` are gone (the HAR and the digest hold them), `capture_sha256` is the hash of the capture body, and `flagged_names` is the cookie and token names the digest recorded for that host. `gp observe fixtures` writes through the owner, `FixtureSession` reads through it, and the sidecar travels with the fixture: the guide's derivation recipe gains one line saying to copy both files into the fixtures root. Nothing reads `url` or `captured_at` from a sidecar today, and `gp observe fixtures` has not shipped in a release, so the change is free.

**The fixtures root has one rule, answerable from disk.** `fixtures_root` today takes a `ScaffoldSpec`, which exists only during generation. The rule (`tests/fixtures/` for a standalone project, `tests/fixtures/<plugin>/` for a suite member) moves to `devtools/scaffold/project.py` as a function of two facts, whether the project is a suite and the plugin's module name, and both callers supply them: the generator from its spec, and the reader from the project on disk, whose structural view carries the resolved root. `add-command` prints it from the view; the sanitisation check and the lint scan it from the view. One rule, two inputs, no inline restatement.

**`fixtures_are_sanitised`, a test the generated suite runs.** `src/graftpunk/testing/plugin.py`, the pytest side of the existing split, gains a check that the generated `tests/conftest.py` carries (a `PROJECT_REQUIREMENTS` entry) beside `site_env_scrubber`. It resolves the fixtures root through the reader and fails if that directory does not exist (the generator creates it with a `.gitkeep`, so a missing root means a resolution bug, and a resolution bug must never pass as "nothing to check"). For every fixture under it that has a sidecar: the sidecar is loaded through the owner, which refuses a missing or unknown `schema` and holds the keys to that version's set (so an old-format or hand-edited sidecar cannot smuggle a URL in, and a later version can add a field without failing sidecars committed under an earlier one); the fixture's hash must differ from `capture_sha256`; and none of `flagged_names` may appear in the fixture body or the sidecar. A fixture without a sidecar is skipped here with a reason that names it, and reported by `gp plugin check`, which runs in the same gate, so the omission fails the gate rather than dissolving into a skip. The check therefore runs in any clone and on the user's CI, with no capture tree present. Its limits are stated in the guide and in the check's docstring: it catches an unchanged copy, a leaked flagged name, a sidecar outside its declared format, and (through the lint) a fixture with no sidecar; it does not judge whether invented content is invented well.

**`gp plugin check`, a lint in `src/graftpunk/devtools/plugin_check.py`.** Works from the shared reader's view and reports, listing each finding: a remaining `GP-FILL` marker; a module without exactly one `SitePlugin` subclass; a command whose declared `endpoint` method differs from the method literal its request call passes, or whose declared path does not start with the literal prefix the call passes (the weak consistency check the declaration admits, since the path may be an f-string); a `PROJECT_REQUIREMENTS` entry the project lacks, which `gp plugin upgrade` fixes; and a fixture under the fixtures root with no sidecar. It is a lint, not a test, so a freshly scaffolded project's test suite still means what test suites mean, and it never edits.

**The project gate and the project's requirements have one owner each.** The list of commands a generated project's gate runs is one constant, `PROJECT_GATE` (`pytest`, `ruff check .`, `ruff format --check .`, `gp plugin check`), in `src/graftpunk/devtools/scaffold/project.py`, where project-level policy already lives, beside `PROJECT_REQUIREMENTS`; the renderer quotes it into the generated README's checks block like every other reader of it, the guide's "The gate" section quotes that block, its CI workflow example runs the same list, and its "Before you publish" checklist says "the gate is green" and keeps only the items the gate cannot check (nothing about markers or individual commands), and tests pin all four to the constant (the way `tests/unit/test_project_description.py` pins the README's CLI block to `gp --help`) and pin the constant's `gp` entries to the live CLI through the walker `tests/unit/test_plugin_development_guide.py` already has, so a renamed command fails the suite instead of shipping a stale gate. The skill's `harden.md` points at the guide's section and runs whatever it lists; the skill never enumerates the gate.

The plan orders the work as two pull requests: the package changes with their tests, docs (including the guide's "The gate" section and its recipe line), and changelog line (a minor release of graftpunk, since the skill's `SKILL_REQUIRES_GRAFTPUNK` floor names it), then the marketplace and the skill. `plugin_app`'s help string becomes "Scaffold, extend, inspect, and check a graftpunk plugin project". The CLI layer splits now, while the entry points are being written: `src/graftpunk/cli/scaffold_commands.py` keeps generation and mutation (`new`, `add-command`) and a new `src/graftpunk/cli/plugin_project_commands.py` holds inspection, migration, and linting (`info`, `upgrade`, `check`), both registering on the same `plugin_app`, each "argument handling only".

## The skill

### Frontmatter

```yaml
---
name: graft
description: Create a graftpunk site plugin from a browser recording, or add commands to an existing one. Use when asked to build, scaffold, or extend a graftpunk plugin for a site, or to add a command to a plugin.
argument-hint: "[plugin-name] [site-url]"
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/preflight.sh *) Bash(gp plugin info *) Bash(gp session list) Bash(gp observe list) Bash(gp observe digest *) Bash(gp observe fixtures *) Bash(gp plugin new *) Bash(gp plugin add-command *) Bash(gp plugin upgrade *) Bash(gp plugin check *)
---
```

`disable-model-invocation` stays at its default (false), so Claude can invoke the skill from a plain request. The pre-approved list is declared, not derived: it is the boundary, and the rule for what belongs on it is stated in `SKILL.md` above the list (the commands the eight steps run unattended that touch neither the live site nor a credential). The skill's test (see Testing) enforces the rule in the direction that cannot widen consent on its own: every entry must be a command the steps run (a stale entry fails), and the two exclusions must stay absent; a newly documented command is not pulled onto the list by the test, it needs a deliberate edit to the declaration. Writing inside the project tree is not an exclusion axis: producing the project is the skill's job, and every write these commands make is to files the user asked the skill to produce (`gp observe fixtures` writes under `tests/captures/`, `gp plugin new`, `add-command`, and `upgrade` write the project). `gp <plugin> login` and any command against the live site are therefore absent and go through the normal permission prompt, which is the user's go-ahead for the live check; `gp observe show` is absent because no step runs it. A later "add `gp x` to avoid a prompt" edit is a two-file change that shows in the diff as a test change.

### Modes

The mode comes from the working directory, and the package decides it: preflight runs `gp plugin info --json` (see Package changes, delivered first) and relays its answer.

- `mode: create` (no `pyproject.toml`): scaffold into the working directory.
- `mode: enhance` (a `pyproject.toml` with a `[project.entry-points."graftpunk.plugins"]` table): the answer carries the plugin module path, `site_name`, `base_url`, and every existing command with its declared endpoint, or `endpoint: null` for a command that has none.
- `mode: foreign` (a `pyproject.toml` without that table): stop with "this directory holds a project that is not a graftpunk plugin; run the skill in an empty directory or in the plugin's project".

Arguments: `$0` is the plugin name and `$1` the site URL in create mode; both are ignored with a note in enhance mode, where the plugin name comes from the package's answer. A create-mode invocation with no arguments asks for the name and the URL, one question each.

### The flow

Each step names the guide heading it follows. The skill reads a reference file only at the step that needs it, and reads the guide section itself only when the reference says to.

1. **Frame** (guide: Frame). Create mode collects the plugin name (validated against the name rule the guide states; a reserved name is refused with the list preflight printed), the site URL, and what the user wants to do on the site, in plain words: "see my orders and download invoices" is enough. It does not ask for command names or endpoints; those come from the digest. Enhance mode collects only the new thing the user wants to do. The login shape and the backend are decided by the skill from the digest later, and confirmed with the user only when the digest is ambiguous (an identity-provider redirect, or no login at all).
2. **Capture** (guide: Capture). The skill prints the exact command for the user to run in the session with the `!` prefix, `! gp observe --no-session interactive <url>`, tells the user to log in, exercise every flow they named, and stop with Ctrl+C, and then waits. UNVERIFIED: that a Ctrl+C typed during shell mode (https://code.claude.com/docs/en/interactive-mode) reaches `gp` as SIGINT so the HAR is saved; the interactive-mode documentation describes Ctrl+C only as interrupting the running operation. The plan's first task probes this on the workstation; if it does not hold, the hand-off tells the user to run the recorder in a separate terminal instead. When the user says the recording is done, the skill runs `gp observe list`, takes the newest run for the inferred session name, and repeats the name back. It never runs the interactive command itself.
3. **Understand** (guide: Understand). The skill runs `gp observe digest <session name> --json`, reads the model, and proposes the commands: a table with one row per candidate (name, what it returns, the endpoint behind it, the parameters seen), mapped to the wants the user stated, with any want that has no endpoint named as needing another recording. The rules that build the table live in `digest.md` and are stated once, in "The command proposal, in detail" below. The user keeps, renames, or drops rows in one answer.
4. **Scaffold** (guide: Scaffold). `gp plugin new <name> --from-run <session name> --command "<agreed name>=<METHOD> <template>"` once per row the user kept (the newest run for that session; `--run <run id>` pins an older one). The generator writes only those stubs, under those names, with the request call it decides for each endpoint and the endpoint declared on each. The skill edits nothing the generator wrote in this step.
5. **Implement** (guide: Implement). For each stub: fill the request, name the parameters, decide the return shape, and replace every `GP-FILL` marker. Errors raise `CommandError` or `PluginError`.
6. **Harden** (guide: Harden). `gp observe fixtures <session name> --match "<METHOD> <template>"` for each command; each captured file and its sidecar are copied into the project's fixtures root (the guide's recipe, extended to name the sidecar) and the fixture's content is invented with its structure kept. The generated project's own suite proves the copy was changed and leaks no flagged name (`fixtures_are_sanitised`, under Package changes), one test per command with `fixture_context`, then the project's gate as the guide's "The gate" section lists it (the package owns the list; it includes `gp plugin check`, which reports a remaining `GP-FILL` marker, so step 5 cannot be skipped). When `gp plugin check` reports missing project wiring, the skill runs `gp plugin upgrade` and reruns the gate. The gate has to pass before the next step.
7. **Kick the tires** (guide: Check the CLI surface you shipped, and Login). With the user's go-ahead, the skill runs `gp <name> --help` and checks every agreed command name is there, then `gp <name> login` and one read-only command against the live site while the user watches. Credentials come from the environment or the workstation env file, which the user sets up (the skill prints the `gp config set` lines with placeholders and never the values). A login failure is diagnosed against the guide's Login section (the failure text, the success signals, the timeout) and the plugin's `LoginConfig` adjusted; the check reruns once.
8. **Publish checklist** (guide: Before you publish). The skill reads the guide's checklist section, whose first item is "the gate is green" (already true after step 6) and whose remaining items are the ones no check can decide (a real account was tried, the README says what the plugin does, the entry point is registered in the plugin's own distribution), walks those, fixes what it can, and stops with the remaining items listed for the user.

The skill restates the step it is on at the start of every turn (one line: "Step 4 of 8: scaffold. Done: ...; next: ...") and asks one question at a time.

### Where enhance mode differs

The eight steps above are written for create mode. Enhance mode runs the same steps with exactly these differences, and this list is the only place they are stated, so a change to enhance mode is one edit here:

- Step 1 collects only the new thing the user wants to do; the plugin name, `site_name`, and `base_url` come from `gp plugin info`.
- Step 2 runs `gp session list` and records with `-s <session name>` when it lists a session for the plugin, and with `--no-session` otherwise.
- Step 3 excludes the endpoints existing commands declare (`gp plugin info` reports each command's `endpoint`), marks every remaining row as new, and, for each existing command reported with `endpoint: null`, adds a row of its own that says "existing command, endpoint not declared" so the user can say whether a proposed row duplicates it; the skill never silently drops or silently proposes over an undeclared command.
- Step 4 does not run `gp plugin new`. For each agreed command it runs `gp plugin add-command <name> --from-run <session name> --command "<agreed name>=<METHOD> <template>"`, which renders one stub in the generated shape into the plugin class and prints the fixture path its test will look for.
- Step 8 is limited to the checklist items the new commands touch.

### The command proposal, in detail

The user should never need to know the commands in advance. The digest is the source: every first-party, non-static endpoint the recording saw, with its method, templated path, query and body parameters and their observed types, response shape, custom header names, and `login_flow` flag. The markdown form prints the first `--limit` endpoints (default 60) and says how many it held back, so the skill passes `--json` and reads the whole model. The shape of the request call a command makes (`request_json` or `request_text`, and its role) is the generator's decision, made when it renders the stub; the proposal table reports what the digest says about an endpoint (its content type and shape) and never restates that rule. The skill turns the model into a proposal by these rules, in order:

- Drop every endpoint the digest marks `login_flow` (the same flag the generator skips when it renders stubs, so the table and the scaffold agree), and trust the digest for the rest: static assets, analytics and tracking hosts, and anything on a host other than the primary one never reach the endpoint list at all.
- Keep JSON endpoints and HTML documents that carry the user's own data (a dashboard, an order list, a statement page); drop navigation chrome.
- Name each kept endpoint as a verb phrase a person would type: `orders` for `GET /api/orders`, `order` for `GET /api/orders/{order_id}` (the path parameter becomes the command's argument), `invoice-pdf` for a document download. Collapse a list-and-detail pair into two commands, never one.
- Present the proposal as a table with one row per command: name, what it returns (from the shape), endpoint, parameters. Say which of the user's stated wants each row serves, and name any want with no endpoint behind it.
- Ask one question: "keep, rename, or drop any of these?" and apply the answer. A want with no endpoint gets the capture step again for that flow, appended to the same run name.

In enhance mode the same rules apply to the new recording, minus the endpoints the existing commands already use.

### The references

Each reference is short (under 120 lines) and holds only what the skill needs at the step that reads it; anything the guide already says is cited by heading and read from the guide at that point, never copied. `rules.md` is an inventory: one line per house rule, each naming the guide heading that states it (entry point registration, never commit a capture, fixtures invent content, parsers never return a confident empty list, exact failure text, secrets from the environment only, resolve a secret by what it is), and the skill reads the cited section when a rule applies. `capture.md` holds the hand-off text and the "what to exercise" prompts, which exist nowhere else. `digest.md` holds the command proposal rules (their single home; the section below is the design that produced them) and a worked example on the guide's synthetic digest; its login-flow rule is "drop what the digest marks `login_flow`", the same flag the generator reads, so the reference restates no package logic. `harden.md` holds the fixture derivation recipe and the test shape, and for the gate and the publish checklist points at the guide's "The gate" and "Before you publish" and tells the skill to read them.

### Preflight

`skills/graft/scripts/preflight.sh`, bash, needing only `gp` on the path. It owns exactly what must be decided before anything else runs and nothing it would have to interpret: it checks that `gp` is present and new enough, runs `gp plugin info --json`, prints that JSON unchanged, and exits:

- `0` with the JSON on stdout (the `mode`, and for `enhance` the project facts, are the package's answer; preflight adds nothing and interprets nothing, so mode policy has one owner, the skill).
- `2` when `gp` is missing, with the install line (`uv tool install graftpunk` or `pip install graftpunk`).
- `3` when the version `gp version --json` reports is below the floor the skill was written for (a constant at the top of the script, `SKILL_REQUIRES_GRAFTPUNK`, set to the release that ships the package changes; the name is distinct from `scaffold_commands.py`'s `_graftpunk_version_floor()`, which is the version a scaffolded project depends on), with the upgrade line. `gp` has no `--version` option and `gp version` prints a Rich panel for people; preflight reads the `graftpunk` value out of the JSON and compares it to the floor as three integers (major, minor, patch, split on dots, compared in order, so `1.10.0` is above `1.9.0`; a pre-release suffix is dropped before the split), and an installed `gp` too old to accept `--json` fails the option parse, which preflight reports as the same exit `3`. Preflight never reads rendered text.

The skill runs preflight first on every invocation and stops on any non-zero exit, printing the script's message verbatim; on `0` it reads `mode` and applies the Modes section, including the stop message for `foreign`.

## Errors and secrets

Every `gp` command's failure is shown to the user verbatim, the cause diagnosed against the guide, and the step retried once after the fix. No step is skipped, and no failure is summarised away. The skill never asks for a password, never writes a credential anywhere, never reads a capture's cookies, tokens, or HAR body, and never prints a `gp config get --resolve` result; it passes run names and match templates to `gp` and reads only the digest, the fixtures directory, and the generated project. When the user pastes a secret into the conversation, the skill says where it belongs (`gp config set NAME '$(your-secret-tool read ...)'`) and does not use it.

## Documentation changes

In the package pull request: `docs/PLUGIN_DEVELOPMENT.md`'s "The gate" section and its CI workflow example take the gate list the package owns (four commands); "Before you publish" reduces to "the gate is green" plus the items no check can decide; "Deriving a fixture from a capture" gains the line that copies the sidecar with the fixture; and the guide's sidecar example loses `url` and `captured_at` and gains `schema`, `capture_sha256`, and `flagged_names`. In the skill pull request: `docs/PLUGIN_DEVELOPMENT.md` gains a section "With the skill" after "The six steps at a glance", three sentences on installing and invoking the skill and one saying the skill follows this guide, so the guide is the reference when the two disagree; `README.md`'s Plugins section gains the two install lines; `CONTRIBUTING.md` gains "Releasing the skill": the version rule, the two files to bump, the CI check, and `just skill-version` for running it by hand. `CHANGELOG.md` `[Unreleased]` Added gets one line per pull request.

## Versioning and release

The skill's version is independent of the graftpunk package version. Any change under `skills/` requires a patch bump of both `version` fields (`.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`), because Claude Code caches an installed plugin under a version-stamped path, `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`, and a new version string means a new cache directory (https://code.claude.com/docs/en/plugins-reference). UNVERIFIED: that an unchanged version string leaves stale content in place. The documentation does not state it; `stavxyz/skills` RELEASING.md reports the cache "isn't reliably invalidated when the version is unchanged" and cites claude-code issues #46081, #14061, and #17361. Minor bumps are for a new skill or a changed invocation contract.

One guard, in CI. `.github/workflows/skill-version.yml` runs on pull requests: when the diff against the base commit touches `skills/` or `.claude-plugin/`, the two version fields must be equal to each other and different from the base's; otherwise the job fails and names the next patch. The check is `scripts/check-skill-version.sh <base sha>`, also runnable by hand and as `just skill-version` before pushing. No git hook: `core.hooksPath` is repository-wide and exclusive, and a local backstop for a check that already blocks the merge is not worth that commitment.

After a merge, users update with `/plugin marketplace update graftpunk` and `/plugin update`.

## Testing

`tests/unit/test_graft_skill.py`, in the existing unit suite so the normal gate runs it:

- Every guide heading cited in `SKILL.md` and the four references exists in `docs/PLUGIN_DEVELOPMENT.md` (reusing the slug helper from `tests/unit/test_plugin_development_guide.py`, which already skips fenced blocks).
- The references copy nothing from the guide: no run of eight or more consecutive words from a reference appears in the guide (words compared lowercased with punctuation stripped, so the "cite, do not copy" rule is enforced at a stated threshold, not hoped), and every rule in `rules.md` names a heading that exists.
- Every `gp` invocation in `SKILL.md` and the references resolves to a real command with real options, through the same walker the guide test uses (the interactive command included: it is written for the user to run, and it still has to parse).
- `SKILL.md` frontmatter parses, `name` is `graft`, the description names both modes, every path in `allowed-tools` exists, and every `allowed-tools` entry is a command the eight steps run (the test collects the `gp` invocations in `SKILL.md` and the references with the guide test's collector and asserts containment, so a stale entry fails), while `gp <plugin> login` and every command the flow runs against the live site are asserted absent. The test never requires an entry, so documenting a new command cannot widen the pre-approved list by itself.
- The two manifest `version` fields are equal, and `plugin.json`'s `name` matches the marketplace's plugin entry.
- Preflight, run on scratch trees, exits `0` and prints the JSON of `gp plugin info` unchanged for an empty directory, for a project written by `gp plugin new`, and for a directory with a foreign `pyproject.toml` (whose JSON says `mode: foreign`); exits `3` when `PATH` holds a fake `gp` whose `version --json` reports a version below `SKILL_REQUIRES_GRAFTPUNK` and when the fake `gp` rejects `--json`; and passes a fake `gp` reporting exactly the floor and one reporting a higher minor with a lower patch (both sides of a ten-boundary, `1.9.0` against a floor of `1.10.0` and the reverse), so the integer comparison is exercised where string comparison would go wrong.
- `scripts/check-skill-version.sh` fails on a `skills/` change without a bump and passes with one (run against two throwaway git commits in a temporary repository).

The package changes carry their own unit tests beside the code they extend: `@command(endpoint=...)` stored on the metadata and absent from help; the digest's `login_flow` flag set on exactly the endpoints `_login_flow_endpoints` used to compute, and the generator skipping those; `plugin_project` resolving the three directory states, refusing a module without exactly one `SitePlugin` subclass, and returning the structural view (spans, decorator keywords, request method literal and path prefix, marker locations) for a generated plugin (every command with an endpoint) and a hand-written one with an undeclared command (`endpoint: null`); `gp plugin info --json` carrying `schema: 1` and exactly the pinned field set per schema version (a frozen list in the test, so a rename fails); `gp version --json` printing the installed version as data; `gp plugin new --command` writing only the selected stubs under the given names, ignoring the twelve-stub cap for explicit selections, and refusing a name that collides, a malformed `--command` value, or an endpoint the digest lacks; `gp plugin add-command` placing one stub after the plugin class's last command in a module written by `gp plugin new` and in one with helpers below the class, refusing a duplicate name and a `login_flow` endpoint, touching nothing but the module, and printing the fixture path for both a project and a suite member; `gp plugin upgrade` adding the conftest wiring to a project that lacks it and changing nothing in one that has it; the generator emitting explicit `PluginParamSpec` entries for typed parameters; the sidecar owner writing exactly its schema's keys and refusing to load a sidecar with a missing or unknown `schema`; `fixtures_are_sanitised` failing on an unchanged copy, on a leaked flagged name in the body or the sidecar, and on a sidecar of unknown schema, passing on an invented fixture, and skipping a fixture with no sidecar with a reason that names it; `gp plugin check` reporting a remaining marker, a declared method that differs from the request's method literal, a declared path that does not start with the call's literal prefix, a module without exactly one plugin class, and missing conftest wiring, and passing on a clean project; `PROJECT_GATE` matching the generated README's checks block, the guide's "The gate" block, the guide's CI example, and the guide's publish checklist's first item, and each of its `gp` entries resolving through the CLI walker; `PROJECT_REQUIREMENTS` pinned to what the renderer emits, what `upgrade` applies, and what `check` reports (one entry added in the test makes all three follow); the fixtures-root rule giving the same answer from a spec and from the project it generated, for a standalone project and a suite member, and the sanitisation check failing when the root is missing; `gp plugin check` reporting a fixture with no sidecar; `parse_endpoint` accepting the digest's printed form and refusing a value with no space, a lowercase method, or an empty template, and every consumer (`gp observe fixtures --match`, both `--command` options) routing through it so the same malformed value is refused identically by all three.

A full dry run of the skill (create mode against a recording of a real site, or `gp observe digest --har tests/fixtures/sample.har` where a live capture is not available, then enhance mode adding one command) is a manual test-plan item on the pull request, recorded with the commands run and their output.

## Rejected approaches

A thin skill that only tells the model to read the guide: no duplication, but every run reloads 1,100 lines, the per-step checklists that make a run reliable are missing, and nothing pins the skill to the guide's headings. A `gp plugin dev` pipeline command that runs digest, scaffold, and fixtures in one go: fewer model steps, but it moves orchestration into the CLI as a new product surface with a release dependency, and enhance mode needs judgment a pipeline cannot supply. Two skills (`new-plugin` and `enhance-plugin`): the flows share six of eight steps, so they would drift.

## Out of scope

The introspector's handling of string and optional annotations (#208; the skill works around it with explicit `PluginParamSpec` entries). Naming a first capture and `gp plugin new --new` (#210; the skill uses the inferred name). The README follow-ups in #211. A `gp` pipeline that chains digest, scaffold, and fixtures into one command (orchestration stays in the skill).

## Review provenance

This design went through validate four times on 2026-09-21 (a fact-check reviewer and a design reviewer in parallel, then a design re-review after each redesign), and every net-negative finding was addressed rather than accepted. The decisions whose reasoning still constrains the implementation are stated inline where they apply: orchestration stays in the skill and the package only reports facts, renders, migrates, or checks (Goals and non-goals); the `@command(endpoint=...)` keyword is a stated exception to the devtools placement rule; the sidecar loader lives with `FixtureSession` and its writer in `devtools`. The rest of the history is the shape of the corrections, recorded once here so an implementer does not have to sift it: the first draft parsed plugin projects and rendered stubs outside the package, and left sanitisation to the model, so those became package features; the second draft deduced endpoints from call sites, compared fixtures against a never-committed capture tree, threaded mode branches through every step, and copied guide text into the references, so endpoints became declarations, the sidecar became the check's committed input, the flow became mode-neutral with one divergence list, and the references became pointers; the third draft left the sidecar format spelled in three files, scraped a version out of a Rich panel, let `add-command` migrate the conftest, and gave the reader too little for the mutator and the lint, so the sidecar got an owner and a version, `gp version --json` exists, `gp plugin upgrade` migrates, and the reader returns one structural view; the fourth draft spelled the project's requirements in three modules, resolved the fixtures root only from a generation spec, derived the consent list from the documents, and left the publish checklist as a fourth spelling of the gate, so `PROJECT_REQUIREMENTS` and the fixtures-root rule got one owner each, the consent test enforces containment, and the checklist quotes the gate. Two platform claims remain marked UNVERIFIED in the text and are probed by the plan's first task.
