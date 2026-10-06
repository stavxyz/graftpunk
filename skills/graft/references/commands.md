# The commands each step runs

The one place the commands are written, one block per step; `SKILL.md` and the
references name a block by its step and never spell a templated command.
Placeholders: `<name>` (the plugin's name), `<site-name>` and `<entry-point>`
(`site_name` and `entry_point` in `gp plugin info --json`), `<command>` (an
agreed command), `<session>` and `<run>` (chosen at capture), `<gate-command>`
(each gate command, in order), `<fixture>` (the plain-named capture
`gp observe fixtures` wrote), `<fixtures-dir>` (the tests' `FIXTURES_DIR`),
`<variable>` and `<value>` (a credential's variable, and a placeholder or a
`$(...)` kept inside the single quotes, which double quotes would let the shell
run at once), `<url>`, `<version>`, `<n>`, `<METHOD>`, and `<template>`.

Only preflight is pre-approved (`SKILL.md`, "Permissions"). The rules offered
at the end come from "Run by the skill" alone; the tests hold each to a line.

## Run by preflight

`preflight.sh` runs these inside the one pre-approved call; no rule is offered.

```bash
gp version --json --at-least <version> --contract info=<n> --contract endpoints=<n>
gp plugin info --json
```

## Run by the user

The skill prints these for the user to run, and never runs them.

```bash
gp observe --no-session interactive <url>
gp observe -s <session> interactive <url>
gp config set <variable> '<value>'
```

## Run by the skill

Kick the tires runs through the site runner (the project and its main
dependencies, so its entry point), and the gate through the gate runner (adding
the `dev` extra and the gate's tools); both build from `pyproject.toml` on every
run. Other `gp` lines run the `gp` on PATH.

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
gp plugin new <name> --from-run <session> --run <run> --url <url> --command "<command>=<METHOD> <template>"
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

The login and command lines touch the live site; the skill asks in words before
either runs, whatever the settings allow. `<command>` is an agreed read-only one.

```bash
uv run --no-project --with-editable . gp <site-name> --help
uv run --no-project --with-editable . gp <site-name> login
uv run --no-project --with-editable . gp <site-name> <command>
```

## Allow rules for a prompt-free run

The rules the skill offers, in the settings syntax. The last two cover the
plugin's help and login; the live read-only command has no rule and asks each
time, since it reads the account.

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
