# The commands each step runs

Every command is written here, one block per step; `SKILL.md` and the references
name a block by its step and never spell a templated command. Placeholders:
`<name>` (the plugin's name), `<site-name>` and `<entry-point>` (`site_name` and
`entry_point` in `gp plugin info --json`), `<command>` (an agreed command),
`<session>` and `<run>` (chosen at capture), `<gate-command>` (each, in order),
`<fixture>` (the plain-named capture `gp observe fixtures` wrote),
`<fixtures-dir>` (the tests' `FIXTURES_DIR`), `<variable>` and `<value>`
(`SKILL.md`, step 7), `<har>` (the run's HAR, by absolute path), `<url-part>`
(a piece of a request URL's path), `<url>`, `<base-url>`, `<version>`, `<n>`,
`<METHOD>`, and `<template>`. Only preflight is pre-approved; the rules at the
end cover "Run by the skill" alone.

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
gp observe digest <session> <run> --json
uv run --no-project python -I ${CLAUDE_SKILL_DIR}/scripts/har-facts.py cookies '<har>'
uv run --no-project python -I ${CLAUDE_SKILL_DIR}/scripts/har-facts.py request '<har>' '<url-part>' --method <METHOD>
uv run --no-project python -I ${CLAUDE_SKILL_DIR}/scripts/har-facts.py response '<har>' '<url-part>' --method <METHOD>
```

### Scaffold

```bash
gp plugin new <name> --from-run <session> --run <run> --command "<command>=<METHOD> <template>"
gp plugin new <name> --from-run <session> --run <run> --url <base-url> --command "<command>=<METHOD> <template>"
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
uv run --no-project python -I ${CLAUDE_SKILL_DIR}/scripts/fixture-leaks.py 'tests/captures/<fixture>' '<fixtures-dir>/<fixture>' '<fixtures-dir>/<fixture>.meta.json'
gp plugin upgrade
uv run --no-project --with-editable '.[dev]' --with pytest --with ruff <gate-command>
```

### Kick the tires

The login and command lines touch the live site; ask first, as `SKILL.md` says.

```bash
uv run --no-project --with-editable . gp <site-name> --help
uv run --no-project --with-editable . gp <site-name> login
uv run --no-project --with-editable . gp <site-name> <command>
```

## Allow rules for a prompt-free run

The rules the skill offers, all `gp` but its two read-only scripts, the leak
check and the HAR reader, which prints no value; a file write still asks. The
last two cover the plugin's help and login. None is offered for the live
read-only command, which reads the account, or for reading a HAR by hand.

```text
Bash(gp plugin info *)
Bash(gp session list *)
Bash(gp observe list *)
Bash(gp observe digest *)
Bash(gp observe fixtures *)
Bash(gp plugin new *)
Bash(gp plugin add-command *)
Bash(gp plugin upgrade *)
Bash(uv run --no-project python -I ${CLAUDE_SKILL_DIR}/scripts/fixture-leaks.py *)
Bash(uv run --no-project python -I ${CLAUDE_SKILL_DIR}/scripts/har-facts.py *)
Bash(uv run --no-project --with-editable '.[dev]' --with pytest --with ruff gp plugin check *)
Bash(uv run --no-project --with-editable . gp <site-name> --help)
Bash(uv run --no-project --with-editable . gp <site-name> login)
```
