# The commands each step runs

The one place the commands are written, one fenced block per step. `SKILL.md`
and the other references name a block here by its step and never spell a
templated command. Placeholders: `<name>` (passed to `gp plugin new`),
`<site-name>` (the name gp runs the plugin by, `site_name` in
`gp plugin info --json`), `<entry-point>` (its `entry_point` there),
`<command>` (an agreed command name), `<session>` and `<run>` (chosen at the
end of the capture step), `<gate-command>` (each command of the project's
gate, in order), `<url>`, `<version>`, `<n>`, `<METHOD>`, and `<template>`.

Only preflight is pre-approved (`SKILL.md`, "Permissions"); every other command
asks unless the user's settings allow it. The rules offered at the end are
drawn from "Run by the skill" alone, and the tests hold each to a line there.

## Run by preflight

`preflight.sh` runs these inside the one pre-approved call; no rule is offered.

```bash
gp version --json --at-least <version> --contract info=<n> --contract endpoints=<n>
gp plugin info --json
```

## Run by the user

The skill prints one of these for the user to run and never runs it.

```bash
gp observe --no-session interactive <url>
gp observe -s <session> interactive <url>
```

## Run by the skill

The Kick the tires lines run through the site runner, which installs the
project and its main dependencies, so the plugin's entry point. The gate runs
through the gate runner, which adds the project's `dev` extra and the gate's
own tools. Each builds an environment from `pyproject.toml` on every run. Other
`gp` lines run the `gp` on PATH.

### Start

```bash
${CLAUDE_SKILL_DIR}/scripts/preflight.sh
```

### Frame

```bash
gp plugin new <name> --check-name
```

### Capture

```bash
gp session list
gp observe list
```

### Understand

```bash
gp observe digest <session> <run> --endpoints-json
```

### Scaffold

```bash
gp plugin new <name> --from-run <session> --run <run> --command "<command>=<METHOD> <template>"
gp plugin add-command <entry-point> --from-run <session> --run <run> --command "<command>=<METHOD> <template>"
gp plugin info --json
```

### Harden

The last line is the gate, each command of it in place of `<gate-command>`, in
order; `policy.PROJECT_GATE` owns the list (guide: The gate). The skill offers
an allow rule only for the gate's `gp` commands; every other command in the
gate asks each time it runs, unless the user's settings allow it.

```bash
gp observe fixtures <session> <run> --match "<METHOD> <template>"
cp 'tests/captures/<fixture>' 'tests/captures/<fixture>.meta.json' '<fixtures-dir>/'
gp plugin upgrade
uv run --no-project --with-editable '.[dev]' --with pytest --with ruff <gate-command>
```

### Kick the tires

The last two touch the live site or a credential. The skill asks in words before
the first of them runs, whatever the user's settings allow. The last line is one
read-only command from the agreed proposal.

```bash
uv run --no-project --with-editable . gp <site-name> --help
uv run --no-project --with-editable . gp <site-name> login
uv run --no-project --with-editable . gp <site-name> <command>
```

## Allow rules for a prompt-free run

The rules the skill offers, in the settings syntax. `<site-name>` is the
plugin's `site_name`. The last two cover the plugin's help and login; the live
read-only command has no rule and asks each time, since it reads the account.

```text
Bash(gp plugin info *)
Bash(gp session list *)
Bash(gp observe list *)
Bash(gp observe digest *)
Bash(gp observe fixtures *)
Bash(gp plugin new *)
Bash(gp plugin add-command *)
Bash(gp plugin upgrade *)
Bash(uv run --no-project --with-editable '.[dev]' --with pytest --with ruff gp plugin check *)
Bash(uv run --no-project --with-editable . gp <site-name> --help)
Bash(uv run --no-project --with-editable . gp <site-name> login)
```
