---
type: plan
validated:
  sha: f916b8ebff6e5012646088489eb581dcfa76ae5c
  date: 2026-10-05T00:23:44Z
  reviewers: [fact-check, solid-hygiene]
  findings:
    critical: 0
    important: 0
    medium: 1
    low: 1
    nitpick: 1
  net_negative_raised: 0
  net_negative_addressed: 0
  net_negative_remaining: 0
---

# The graft Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the third of the three pull requests the graft skill design orders: a Claude Code plugin marketplace inside the graftpunk repository with one skill, `graftpunk:graft`, which walks a developer through the plugin guide and runs the `gp` commands itself, plus the tests, the version-bump check, and the documentation that make it maintainable.

**Architecture:** `.claude-plugin/marketplace.json` and `.claude-plugin/plugin.json` make the repository root a plugin (`source: "./"`), and `skills/graft/` holds the skill: `SKILL.md` (frontmatter and the flow), five references read one step at a time (`commands.md`, `rules.md`, `capture.md`, `digest.md`, `harden.md`), and `scripts/preflight.sh`, the one version handshake. Everything the skill knows about a plugin project comes from the package (`gp version --json --at-least --contract`, `gp plugin info --json`, `gp observe digest --endpoints-json`, and the scaffold commands); the skill's own tests pin its prose to the guide's headings, forbid copying the guide, hold the frontmatter's pre-approval to preflight alone and every offered allow rule to a command the skill itself runs, and run preflight against real and fake `gp` executables. The tests take the guide helpers from `tests/unit/guide_harness.py` (the project-tools plan, Task 9) and import no other test module. A CI job and a `just` recipe enforce the skill's independent version.

**Permissions, decided (primary source: https://code.claude.com/docs/en/skills):** `allowed-tools` grants "Tools Claude can use without asking permission during the turn that invokes this skill. The grant clears when you send your next message." The frontmatter therefore pre-approves only preflight, the one command the invoking turn reliably runs. `references/commands.md` stays the one declared list of the commands each step runs and gains the allow rules the skill offers for a prompt-free run; the skill offers each rule once it can be filled in (`SKILL.md` owns when) and never adds one itself. The live login and the first live read are asked for in words at the kick-the-tires step, whatever the user's settings allow.

**Tech Stack:** Markdown with YAML frontmatter, bash, JSON manifests, pytest (with `pyyaml`, already a dependency), GitHub Actions, `just`.

**Spec:** `docs/superpowers/specs/2026-09-21-graft-skill-design.md` (validated 2026-09-22). Read it alongside this plan. This plan runs only after `docs/superpowers/plans/2026-09-22-graft-package-foundations.md` and `docs/superpowers/plans/2026-09-22-graft-package-project-tools.md` have merged and shipped in one graftpunk release; it consumes their CLI surfaces exactly as their Interfaces blocks state them.

**Precondition, checked by Task 3's first test:** the graftpunk installed in this checkout's environment reports a version at least `SKILL_REQUIRES_GRAFTPUNK` (`1.17.0`, the release that ships the two package pull requests). The skill must never merge ahead of the package it needs. Until graftpunk 1.17.0 is released (after #216 merges), Task 3's floor test (`test_the_installed_graftpunk_meets_the_skill_floor`) and its real-gp preflight tests (`TestPreflightWithTheRealGp`) fail, and execution stops at Task 3 Step 4 until that release is installed here.

**Note, 2026-10-06:** the floor (`SKILL_REQUIRES_GRAFTPUNK`) became `1.18.0`, and the skill's host question and its `--url <base url>` were removed when graftpunk 1.18.0 added per-endpoint hosts (see the spec's amendments of that date); the skill now passes `--url` only in create mode, when the user picks the site URL's host over `primary_host` as the base. A subdomain, such as an API host, does reach the endpoint list, each endpoint carrying its `host`, so the statement below that other hosts never reach it holds only for hosts outside the primary host's domain. The body of this plan is otherwise unchanged.

## Global Constraints

- The placement rule in `src/graftpunk/devtools/__init__.py` is untouched: this pull request adds no Python under `src/`. `graftpunk.testing` still imports nothing from `graftpunk.devtools`.
- The skill copies nothing from the guide: no run of eight or more consecutive words from `SKILL.md` or a reference appears in `docs/PLUGIN_DEVELOPMENT.md`, outside a quotation that ends with a citation of a guide heading. It cites only headings that exist. Tests enforce both.
- The skill carries no schema number except `SKILL_READS_INFO_SCHEMA` and `SKILL_READS_ENDPOINTS_SCHEMA` at the top of `preflight.sh`, and no copy of the name rule, the reserved names, the gate, or the publish checklist.
- Any change under `skills/` or `.claude-plugin/` bumps the `version` field of `.claude-plugin/plugin.json`, the one version the skill carries; `marketplace.json` has no root `version`. This pull request introduces it at `0.1.0`.
- No email in either manifest; the GitHub handle is enough.
- Placeholders only, in the skill, tests, docs, and commit messages: `myshop`, `myshop.example`, `alice@example.com`, and `example.com` and `example.net` hosts. Never a real site, vendor, account, `op://` path, or a named secret manager (the skill says `your-secret-tool`).
- No em dashes or en dashes, and no spaced double hyphen standing in for one, in any file, commit message, or plan text.
- Every commit subject is in the repository's `type(scope): subject` form.
- No Claude attribution in commits: no `Co-Authored-By`, no "Generated with" footer, no `Claude-Session` trailer.
- Tests assert behaviour, never that a mock was called.
- No test module imports from another `test_*.py` module; the guide helpers come from `tests/unit/guide_harness.py`.
- The frontmatter's `allowed-tools` pre-approves preflight and nothing else; the skill offers allow rules and never adds them; the kick-the-tires step asks in words before the first live call.
- The full gate, green at the end of every task and run in full by the last task: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
- A single test runs as `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/<file>.py::<test> -q`.

## Review Focus

1. A PATH with no `gp` on it but a system binary of the same name elsewhere (PARI/GP installs one called `gp`): the missing-`gp` test must not depend on the host's `/usr/bin`, so it builds its own PATH from a tools directory. Pinned in Task 3 (`tests/unit/test_graft_preflight.py::test_gp_missing_exits_1_with_the_install_line`).
2. `gp plugin info --json` refusing to read the directory (a malformed `pyproject.toml`): a person expects preflight to stop with gp's own message rather than print half a JSON object. Pinned in Task 3 (`test_a_project_gp_cannot_read_exits_1_with_gps_message`).
3. An offered allow rule that widens consent by accident, such as `Bash(gp *)` or `Bash(gp observe *)`: the second would let Claude run `gp observe interactive`, which the skill never runs. Pinned in Task 4 (`test_no_offered_rule_reaches_the_recorder_or_every_gp_command`).
4. A reference that quotes a guide sentence to be accurate: a blockquote line is exempt from the copy check only when it ends with a citation of a heading that exists. Pinned in Task 5 (`test_a_quotation_is_exempt_only_with_a_citation`).
5. `just skill-version` run on a branch whose base has no manifests yet (this pull request itself): a first introduction has no base version to differ from, and must pass. Pinned in Task 6 (`test_a_first_introduction_passes`).

## File Structure

| File | Responsibility |
| --- | --- |
| `.claude-plugin/marketplace.json` (new, Task 2) | The marketplace: name `graftpunk`, one plugin, `source: "./"`. |
| `.claude-plugin/plugin.json` (new, Task 2) | The plugin: name, description, version, author. |
| `skills/graft/scripts/preflight.sh` (new, Task 3) | `gp` present and new enough, the one version handshake, and `gp plugin info --json` relayed. |
| `skills/graft/SKILL.md` (new, Task 4) | Frontmatter and the flow. |
| `skills/graft/references/commands.md` (new, Task 4) | The declared commands each step runs, and the allow rules the skill offers for them; the referent of the consent tests. |
| `skills/graft/references/rules.md`, `capture.md`, `digest.md`, `harden.md` (new, Task 5) | What each step needs, citing the guide by heading and pointing to `commands.md` for every command. |
| `tests/unit/test_graft_manifests.py` (new, Task 2) | The manifests' tests. |
| `tests/unit/skill_harness.py` (new, Task 4) | The skill's files, found once for the skill's test modules: `SKILL_DIR`, `SKILL_MD`, `COMMANDS_MD`, `skill_docs`, `commands_in`, and `declared_commands`. Not a test module. |
| `tests/unit/test_graft_consent.py` (new, Task 4) | The frontmatter, the offered allow rules against the declared commands, the live-call question, and every `gp` invocation resolved against the CLI. |
| `tests/unit/test_graft_references.py` (new, Task 5) | Citations, the copy check, the references' length, the commands' one owner, and `digest.md`'s closed lists against what the digest produces. |
| `tests/unit/test_graft_preflight.py` (new, Task 3) | Preflight against the real `gp` and a fake one, and CONTRIBUTING's update procedure against preflight's (Task 7). |
| `scripts/check-skill-version.sh` (new, Task 6) | The version-bump check. |
| `.github/workflows/skill-version.yml` (new, Task 6) | Runs the check on pull requests. |
| `.github/workflows/python-quality.yml` (modify, Task 6) | Runs the Python quality jobs (the full test suite, lint, type check, typer compatibility, and the lean install) when only the skill, the guide, `scripts/`, or `CONTRIBUTING.md` changes. |
| `justfile` (modify, Task 6) | `just skill-version`. |
| `tests/unit/test_skill_version_script.py` (new, Task 6) | The check against throwaway git repositories. |
| `docs/PLUGIN_DEVELOPMENT.md`, `README.md`, `CONTRIBUTING.md`, `CHANGELOG.md` (modify, Task 7) | "With the skill", the install lines, "Releasing the skill", one Added line. |

---

### Task 1 [manual]: Probe whether Ctrl+C in Claude Code's `!` shell mode reaches `gp observe interactive`

The spec marks this UNVERIFIED: that a Ctrl+C typed while a `!` command runs in a Claude Code session reaches `gp` as SIGINT, so the recorder's handler runs and the HAR is saved. The interactive-mode documentation (https://code.claude.com/docs/en/interactive-mode) lists Ctrl+C as "Interrupt, or clear input", and its shell-mode section does not say whether Ctrl+C reaches a running `!` command. The outcome decides one wording choice in Task 5 (`capture.md`) and nothing else; every other task is independent of it. A person runs this task on a workstation with a display, because the recorder opens a real browser.

The spec's former second UNVERIFIED claim, that an unchanged version string leaves users on the content they have, needs no probe (the spec now cites the source): the plugins reference (https://code.claude.com/docs/en/plugins-reference, the `version` field) says setting `version` "pins the plugin to that version until you change it", and names three installs the field does not pin: "A plugin with a `command` source, a plugin from a marketplace hosted on claude.ai, and a plugin loaded in place from a marketplace added from a local path aren't pinned by this field." None applies to a GitHub install. The version rule in Task 6 rests on that sentence.

**Files:**
- None. The result is recorded in the pull request's test plan and in Task 5's commit message.

**Interfaces:**
- Produces: `CTRL_C_REACHES_GP`, either `yes` or `no`, which Task 5 Step 3 reads.

- [ ] **Step 1: Start a scratch session**

In an empty scratch directory with graftpunk installed (`gp version --json` prints `"contracts"`), start Claude Code:

```bash
mkdir -p /tmp/graft-probe && cd /tmp/graft-probe && claude
```

- [ ] **Step 2: Run the recorder through shell mode**

In the Claude Code input, type exactly:

```text
! gp observe --no-session interactive https://example.com/
```

Wait until the browser opens and the session shows `Recording... press Ctrl+C to stop and save`.

- [ ] **Step 3: Press Ctrl+C once, in the Claude Code window**

Record what the session shows. The recorder's own save path prints `Recording stopped. Saving capture...`.

- [ ] **Step 4: Check the disk, which is the primary source**

In a separate terminal:

```bash
gp observe list
ls -l ~/.local/share/graftpunk/observe/example/
```

Take the newest run directory `ls` printed and run `ls -l ~/.local/share/graftpunk/observe/example/<run-id>/network.har`.

Expected for `CTRL_C_REACHES_GP=yes`: the session printed `Recording stopped. Saving capture...`, the newest run under `example` holds a non-empty `network.har`, and the browser closed. Anything else (no save message, no new run, an empty or missing `network.har`, or a browser left open) is `CTRL_C_REACHES_GP=no`. If the browser was left open, close it and run `gp observe list` again to confirm no complete run was written.

- [ ] **Step 5: Record the result**

Write one line into the pull request description's test plan, with the date and the Claude Code version (`claude --version`): `Ctrl+C in ! shell mode reaches gp observe interactive: yes|no (<date>, Claude Code <version>)`. Delete the scratch run with `gp observe clean example --force`.

---

### Task 2: The marketplace and plugin manifests

**Files:**
- Create: `.claude-plugin/marketplace.json`, `.claude-plugin/plugin.json`
- Test: `tests/unit/test_graft_manifests.py`

**Interfaces:**
- Consumes: nothing.
- Consumes: `REPO_ROOT` from `tests/unit/guide_harness.py` (the project-tools plan, Task 9).
- Produces: the marketplace `graftpunk` serving plugin `graftpunk` at version `0.1.0` from `./`, so the install lines are `/plugin marketplace add stavxyz/graftpunk` and `/plugin install graftpunk@graftpunk`. `tests/unit/test_graft_manifests.py`, which holds the manifests' tests and nothing else; Tasks 4 and 5 put the skill's other tests in modules of their own.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_graft_manifests.py`:

```python
"""The graft skill's manifests (graft skill spec, 2026-09-21, "Testing").

Nothing here runs Claude Code. The skill's tests are split along its files: the
manifests here, the frontmatter and consent in tests/unit/test_graft_consent.py,
the references' citations and the copy check in
tests/unit/test_graft_references.py, and preflight in
tests/unit/test_graft_preflight.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.unit.guide_harness import REPO_ROOT

MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class TestManifests:
    def test_plugin_json_carries_the_one_version(self) -> None:
        """plugin.json's version is the one that pins an installed plugin (plugins
        reference, the version field). marketplace.json's root version is only the
        marketplace manifest's own (marketplace reference, top-level fields), and
        nothing here reads it, so neither the root nor the entry carries one."""
        assert _json(PLUGIN_MANIFEST)["version"]
        marketplace = _json(MARKETPLACE)
        (entry,) = marketplace["plugins"]
        assert "version" not in marketplace
        assert "version" not in entry

    def test_the_plugin_name_matches_the_marketplace_entry(self) -> None:
        (entry,) = _json(MARKETPLACE)["plugins"]
        assert entry["name"] == _json(PLUGIN_MANIFEST)["name"] == "graftpunk"

    def test_the_plugin_is_the_repository_root(self) -> None:
        (entry,) = _json(MARKETPLACE)["plugins"]
        assert entry["source"] == "./"

    def test_the_marketplace_is_named_for_the_install_line(self) -> None:
        assert _json(MARKETPLACE)["name"] == "graftpunk"

    def test_no_email_is_published(self) -> None:
        for path in (MARKETPLACE, PLUGIN_MANIFEST):
            assert "@" not in path.read_text(encoding="utf-8"), path
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_manifests.py -q`
Expected: FAIL (`FileNotFoundError: ... .claude-plugin/marketplace.json`).

- [ ] **Step 3: Write the manifests**

Create `.claude-plugin/marketplace.json`:

```json
{
  "name": "graftpunk",
  "owner": {"name": "stavxyz"},
  "description": "graftpunk's Claude Code skills: /graftpunk:graft creates or enhances a site plugin",
  "plugins": [
    {
      "name": "graftpunk",
      "source": "./",
      "description": "/graftpunk:graft walks the plugin developer guide: record the site, digest the recording, scaffold or extend the plugin, implement, harden, publish"
    }
  ]
}
```

The manifest carries no root `version` (the plugin's version lives in `plugin.json` alone; see the test above) and no `$schema` key. The marketplace reference says `$schema` is "Ignored at load time" (https://code.claude.com/docs/en/plugins/marketplace-reference, top-level fields), and `https://www.anthropic.com/claude-code/marketplace.schema.json` returned HTTP 404 when checked on 2026-09-22.

Create `.claude-plugin/plugin.json`:

```json
{
  "name": "graftpunk",
  "description": "/graftpunk:graft walks the plugin developer guide: record the site, digest the recording, scaffold or extend the plugin, implement, harden, publish",
  "version": "0.1.0",
  "author": {"name": "stavxyz"}
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_manifests.py -q`
Expected: PASS.

- [ ] **Step 5: Confirm the sdist stays an allowlist**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_sdist_contents.py -q`
Expected: PASS. `.claude-plugin/` and `skills/` are outside `[tool.hatch.build.targets.sdist].only-include`, so the source distribution is unchanged.

- [ ] **Step 6: Commit**

```bash
git add .claude-plugin/marketplace.json .claude-plugin/plugin.json tests/unit/test_graft_manifests.py
git commit -m "feat(skill): the repository is a Claude Code plugin marketplace serving graftpunk"
```

---

### Task 3: `preflight.sh`, the one version handshake

**Files:**
- Create: `skills/graft/scripts/preflight.sh` (executable)
- Test: `tests/unit/test_graft_preflight.py`

**Interfaces:**
- Consumes: `gp version --json --at-least VERSION --contract SURFACE=N ...` from the foundations plan, Task 5. When gp can read every value it was given, it prints one line of JSON on stdout and exits 0, or 1 when the installed version is below VERSION, or 3 when a named surface differs (one line per mismatch on stderr naming the older side). When it cannot read VERSION or a `--contract` value, it prints nothing on stdout and exits 1 with its message on stderr. It exits 2 for an option it does not know (`src/graftpunk/cli/main.py:173` (`def version`)); `gp plugin info --json` from the project-tools plan, Task 4; `REPO_ROOT` from `tests/unit/guide_harness.py` (the project-tools plan, Task 9).
- Produces: `skills/graft/scripts/preflight.sh` with `SKILL_REQUIRES_GRAFTPUNK="1.17.0"`, `SKILL_READS_INFO_SCHEMA=1`, and `SKILL_READS_ENDPOINTS_SCHEMA=1` at the top; it passes all three to gp in one `gp version` call, parses no JSON, and compares nothing itself. On success it prints `{"installation": <gp version --json>, "project": <gp plugin info --json>}` and exits 0; otherwise it prints one message on stderr and exits 1, whatever failed. Each failure has its own message: no `gp` (the install line); no `uv` (its install page, https://docs.astral.sh/uv/, since the skill runs every command that loads the plugin through `uv run`); `gp version` exited 1 (the message names all three readings, graftpunk below the floor, a floor gp cannot read, or a malformed `--contract` value, relays gp's own message, and gives the upgrade line); a contract mismatch (gp's own lines relayed and the fix for each side); `gp` could not read the project; `gp` rejected an option preflight passed; or `gp version` exited with any other status (relayed with gp's output). Task 4's `SKILL.md` reads zero against non-zero and shows the message; it runs preflight first.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_graft_preflight.py`:

```python
"""The graft skill's preflight script, against the real gp and a fake one
(graft skill spec, 2026-09-21, "Preflight").

Preflight asks gp every version question in one call and relays the answers; it
parses no JSON and compares nothing. The fake gp below answers as told and
records the arguments it was given, so these tests pin what preflight asks and
what it does with each exit status.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from packaging.version import Version

import graftpunk
from tests.unit.guide_harness import REPO_ROOT

PREFLIGHT = REPO_ROOT / "skills" / "graft" / "scripts" / "preflight.sh"
BASH = shutil.which("bash")
# The only programs preflight may use besides bash builtins, gp, and uv (which it
# only looks for). The PATH the fake-gp tests build holds nothing else, so a
# preflight that reached for a JSON or text tool would fail there; the real-gp
# tests also put the environment's own bin directory on the PATH, so they do not
# prove it.
_TOOLS = ("mktemp", "cat", "rm")
_REAL_GP_DIR = Path(sys.executable).parent
_VERSION_OK = '{"contracts": {"endpoints": 1, "info": 1}, "graftpunk": "9.9.9"}'
_INFO_EMPTY = '{"directory": "empty", "plugins": [], "schema": 1}'
_OLDER_CALLER = (
    "info: this graftpunk writes schema 2 and the caller reads 1; "
    "the caller is older than graftpunk."
)
# What gp version prints, with exit 1, for an --at-least value it cannot read.
_UNREADABLE_FLOOR = "--at-least: 'one.two' is not a version graftpunk can order."


def _constant(name: str) -> str:
    match = re.search(rf'^{name}="?([^"\n]+)"?$', PREFLIGHT.read_text(), re.M)
    assert match, f"preflight.sh declares no {name}"
    return match.group(1)


def _tools(tmp_path: Path) -> Path:
    """A directory holding only the programs preflight needs, so no host binary called
    gp can stand in for the one a test means. Preflight only checks that uv is on
    the PATH and never runs it, so a stub that exits 99 stands in for it."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in _TOOLS:
        found = shutil.which(name)
        assert found, name
        (tools / name).symlink_to(found)
    uv = tools / "uv"
    uv.write_text(f"#!{BASH}\nexit 99\n")
    uv.chmod(0o755)
    return tools


def _env(home: Path, *path_dirs: Path) -> dict[str, str]:
    return {
        "PATH": os.pathsep.join(str(d) for d in path_dirs),
        "HOME": str(home),
        "NO_COLOR": "1",
        "GRAFTPUNK_CONFIG_DIR": str(home / "gp-config"),
    }


def _preflight(cwd: Path, home: Path, *path_dirs: Path) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [BASH, str(PREFLIGHT)],
        cwd=cwd,
        env=_env(home, *path_dirs),
        capture_output=True,
        text=True,
        timeout=180,
    )


def _real_gp(cwd: Path, home: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [str(_REAL_GP_DIR / "gp"), *args],
        cwd=cwd,
        env=_env(home, _REAL_GP_DIR),
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )
    return result.stdout


def _fake_gp(
    tmp_path: Path,
    *,
    version_json: str = _VERSION_OK,
    version_exit: int = 0,
    version_message: str = "",
    info_json: str = _INFO_EMPTY,
    info_exit: int = 0,
) -> Path:
    """A gp that answers exactly the two calls preflight makes, as told, and writes the
    arguments of its version call, one per line, to version-args beside itself."""
    fake = tmp_path / "fake"
    fake.mkdir()
    script = fake / "gp"
    script.write_text(
        f"""#!{BASH}
if [ "$1" = version ]; then
  printf '%s\\n' "$@" > "{fake}/version-args"
  if [ {version_exit} -eq 2 ]; then echo "No such option: --contract" >&2; exit 2; fi
  printf '%s\\n' '{version_json}'
  if [ -n "{version_message}" ]; then printf '%s\\n' "{version_message}" >&2; fi
  exit {version_exit}
fi
if [ "$1" = plugin ] && [ "$2" = info ]; then
  if [ {info_exit} -eq 2 ]; then echo "No such option: --json" >&2; exit 2; fi
  printf '%s\\n' '{info_json}'
  exit {info_exit}
fi
exit 99
"""
    )
    script.chmod(0o755)
    return fake


@pytest.fixture()
def dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """(work directory, home, tools directory)."""
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return work, home, _tools(tmp_path)


def test_the_installed_graftpunk_meets_the_skill_floor() -> None:
    """The skill must not merge ahead of the graftpunk release it needs."""
    floor = _constant("SKILL_REQUIRES_GRAFTPUNK")
    assert Version(graftpunk.__version__) >= Version(floor), (
        f"graftpunk {graftpunk.__version__} is below the skill's floor {floor}"
    )


def test_preflight_is_executable() -> None:
    assert os.access(PREFLIGHT, os.X_OK)


@pytest.mark.skipif(BASH is None, reason="preflight is a bash script")
class TestPreflightWithTheRealGp:
    @pytest.mark.parametrize("kind", ["empty", "plugin", "foreign"])
    def test_both_answers_are_relayed_unchanged(
        self, dirs: tuple[Path, Path, Path], kind: str
    ) -> None:
        """Exit 0 also means the real gp accepted the skill's floor and both of its
        contract numbers."""
        work, home, tools = dirs
        if kind == "plugin":
            _real_gp(
                work,
                home,
                "plugin",
                "new",
                "myshop",
                "--url",
                "https://myshop.example",
                "--dir",
                str(work),
            )
        elif kind == "foreign":
            (work / "pyproject.toml").write_text('[project]\nname = "other"\n')
        result = _preflight(work, home, _REAL_GP_DIR, tools)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert set(payload) == {"installation", "project"}
        assert payload["installation"] == json.loads(_real_gp(work, home, "version", "--json"))
        assert payload["project"] == json.loads(_real_gp(work, home, "plugin", "info", "--json"))
        assert payload["project"]["directory"] == kind


@pytest.mark.skipif(BASH is None, reason="preflight is a bash script")
class TestPreflightWithAFakeGp:
    def test_it_asks_for_its_floor_and_both_contract_numbers_in_one_call(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        fake = _fake_gp(tmp_path)
        assert _preflight(work, home, fake, tools).returncode == 0
        assert (fake / "version-args").read_text().splitlines() == [
            "version",
            "--json",
            "--at-least",
            _constant("SKILL_REQUIRES_GRAFTPUNK"),
            "--contract",
            f"info={_constant('SKILL_READS_INFO_SCHEMA')}",
            "--contract",
            f"endpoints={_constant('SKILL_READS_ENDPOINTS_SCHEMA')}",
        ]

    def test_gp_missing_exits_1_with_the_install_line(self, dirs: tuple[Path, Path, Path]) -> None:
        work, home, tools = dirs
        result = _preflight(work, home, tools)
        assert result.returncode == 1
        assert "gp is not on PATH" in result.stderr
        assert "uv tool install graftpunk" in result.stderr
        assert result.stdout == ""

    def test_uv_missing_exits_1_with_its_install_page_before_asking_gp(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        """The commands that load the plugin run through uv run, so without uv the
        skill stops at preflight rather than at the harden step."""
        work, home, tools = dirs
        (tools / "uv").unlink()
        fake = _fake_gp(tmp_path)
        result = _preflight(work, home, fake, tools)
        assert result.returncode == 1
        assert "uv is not on PATH" in result.stderr
        assert "https://docs.astral.sh/uv/" in result.stderr
        assert not (fake / "version-args").exists()
        assert result.stdout == ""

    def test_gp_version_exit_1_exits_1_with_gps_message_and_the_upgrade_line(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        """gp's exit 1 with a message of its own: a floor it cannot read prints
        nothing on stdout and says so on stderr (src/graftpunk/cli/main.py, version)."""
        work, home, tools = dirs
        fake = _fake_gp(
            tmp_path, version_json="", version_exit=1, version_message=_UNREADABLE_FLOOR
        )
        result = _preflight(work, home, fake, tools)
        assert result.returncode == 1
        assert _UNREADABLE_FLOOR in result.stderr
        assert "uv tool upgrade graftpunk" in result.stderr
        for reading in ("older than", "version floor", "--contract value"):
            assert reading in result.stderr, reading
        assert result.stdout == ""

    def test_a_floor_not_met_exits_1_with_the_upgrade_line_and_the_readings(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        """gp's exit 1 with no message: the installed graftpunk is below the floor."""
        work, home, tools = dirs
        result = _preflight(work, home, _fake_gp(tmp_path, version_exit=1), tools)
        assert result.returncode == 1
        assert "uv tool upgrade graftpunk" in result.stderr
        for reading in ("older than", "version floor", "--contract value"):
            assert reading in result.stderr, reading
        assert result.stdout == ""

    def test_a_contract_mismatch_exits_1_relaying_gp_and_the_fix_for_each_side(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        fake = _fake_gp(tmp_path, version_exit=3, version_message=_OLDER_CALLER)
        result = _preflight(work, home, fake, tools)
        assert result.returncode == 1
        assert "different schemas" in result.stderr
        assert _OLDER_CALLER in result.stderr
        assert "/plugin marketplace update graftpunk" in result.stderr
        assert "Installed tab of /plugin" in result.stderr
        assert "claude plugin update graftpunk@graftpunk" in result.stderr
        # /plugin update has no session form; /plugin would open the Discover tab.
        assert "/plugin update" not in result.stderr
        assert "uv tool upgrade graftpunk" in result.stderr
        assert result.stdout == ""

    def test_a_rejected_version_option_exits_1_naming_both_readings(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        result = _preflight(work, home, _fake_gp(tmp_path, version_exit=2), tools)
        assert result.returncode == 1
        assert "older than" in result.stderr
        assert "misspelled flag" in result.stderr
        assert "No such option: --contract" in result.stderr
        assert "uv tool upgrade graftpunk" in result.stderr

    def test_a_rejected_info_option_exits_1_naming_the_call(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        result = _preflight(work, home, _fake_gp(tmp_path, info_exit=2), tools)
        assert result.returncode == 1
        assert "gp rejected an option preflight passed (gp plugin info --json)" in result.stderr
        assert "No such option: --json" in result.stderr

    def test_an_unexpected_version_status_exits_1_relaying_gps_output(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        """A gp version crash has its own message, never the unreadable-project one."""
        work, home, tools = dirs
        fake = _fake_gp(tmp_path, version_exit=70, version_message="Traceback: boom")
        result = _preflight(work, home, fake, tools)
        assert result.returncode == 1
        assert "exited 70" in result.stderr
        assert "Traceback: boom" in result.stderr
        assert "could not read the project" not in result.stderr

    def test_a_project_gp_cannot_read_exits_1_with_gps_message(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        fake = _fake_gp(tmp_path, info_json="pyproject.toml: not valid TOML", info_exit=1)
        result = _preflight(work, home, fake, tools)
        assert result.returncode == 1
        assert "could not read the project" in result.stderr
        assert "pyproject.toml: not valid TOML" in result.stderr
        assert result.stdout == ""

    def test_a_floor_that_is_met_exits_0_and_relays(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        result = _preflight(work, home, _fake_gp(tmp_path), tools)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "installation": json.loads(_VERSION_OK),
            "project": json.loads(_INFO_EMPTY),
        }
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_preflight.py -q`
Expected: FAIL (`FileNotFoundError` reading `skills/graft/scripts/preflight.sh`).

- [ ] **Step 3: Write the script**

Create `skills/graft/scripts/preflight.sh`:

```bash
#!/usr/bin/env bash
# Preflight for /graftpunk:graft. Checks that gp is installed, asks gp in one call
# whether it is new enough (--at-least) and whether it writes the payloads this
# skill reads at the schemas it reads (--contract), and relays gp plugin info
# --json. On success prints
#   {"installation": <gp version --json>, "project": <gp plugin info --json>}
# and exits 0. Otherwise prints one message on stderr, naming what failed and
# what to do, and exits 1. The failures, each with its own message:
#   gp is not on PATH
#   uv is not on PATH (the skill runs every command that loads the plugin through
#     uv run; preflight only looks for it)
#   gp version exited 1 (graftpunk older than this skill needs, or gp could not
#     read the floor or a --contract value this script passed)
#   gp reports a contract mismatch (gp's own lines name the older side)
#   gp could not read the project in this directory
#   gp rejected an option this script passed
#   gp version exited with a status this script does not expect
# It compares nothing and parses no JSON: gp answers by its exit status, and the
# JSON is relayed exactly as gp printed it.
set -u

SKILL_REQUIRES_GRAFTPUNK="1.17.0"
SKILL_READS_INFO_SCHEMA=1
SKILL_READS_ENDPOINTS_SCHEMA=1

INSTALL_LINE="uv tool install graftpunk   (or: pip install graftpunk)"
UPGRADE_LINE="uv tool upgrade graftpunk   (or: pip install --upgrade graftpunk)"
UV_INSTALL_PAGE="https://docs.astral.sh/uv/"
SKILL_UPDATE_LINE="/plugin marketplace update graftpunk, then update graftpunk on the Installed tab of /plugin (or run: claude plugin update graftpunk@graftpunk)"

if ! command -v gp >/dev/null 2>&1; then
  printf 'graftpunk is not installed: gp is not on PATH.\nInstall it: %s\n' "$INSTALL_LINE" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  printf 'uv is not on PATH. The skill runs every command that loads the plugin through uv run.\n' >&2
  printf 'Install it: %s\n' "$UV_INSTALL_PAGE" >&2
  exit 1
fi

errfile="$(mktemp)"
trap 'rm -f "$errfile"' EXIT

# Exit 2 from gp means an option it does not know, and nothing else: gp reports
# an unreadable value with exit 1 and a message of its own.
option_rejected() {
  printf 'gp rejected an option preflight passed (%s).\n' "$1" >&2
  printf 'Either the installed graftpunk is older than %s, or this skill passed a misspelled flag.\n' \
    "$SKILL_REQUIRES_GRAFTPUNK" >&2
  printf 'Upgrade first: %s\nIf it still fails after upgrading, the skill has a bug; report this message.\n' \
    "$UPGRADE_LINE" >&2
  cat "$errfile" >&2
  exit 1
}

installation="$(gp version --json --at-least "$SKILL_REQUIRES_GRAFTPUNK" \
  --contract "info=$SKILL_READS_INFO_SCHEMA" \
  --contract "endpoints=$SKILL_READS_ENDPOINTS_SCHEMA" 2>"$errfile")"
status=$?
case "$status" in
  0) ;;
  1)
    printf 'gp version refused (exit 1): the installed graftpunk is older than %s, or gp\n' \
      "$SKILL_REQUIRES_GRAFTPUNK" >&2
    printf 'could not read the version floor or a --contract value this skill passed.\n' >&2
    printf "gp's message, when it gave one, says which:\n" >&2
    cat "$errfile" >&2
    printf 'When graftpunk is older, upgrade it: %s\n' "$UPGRADE_LINE" >&2
    exit 1
    ;;
  2) option_rejected "gp version --json --at-least --contract" ;;
  3)
    printf 'graftpunk and this skill read a payload at different schemas:\n' >&2
    cat "$errfile" >&2
    printf 'When graftpunk is the older side, upgrade it: %s\n' "$UPGRADE_LINE" >&2
    printf 'When this skill is the older side, update it: %s\n' "$SKILL_UPDATE_LINE" >&2
    exit 1
    ;;
  *)
    printf 'gp version exited %s, which preflight does not expect:\n%s\n' "$status" "$installation" >&2
    cat "$errfile" >&2
    exit 1
    ;;
esac

project="$(gp plugin info --json 2>"$errfile")"
status=$?
case "$status" in
  0) ;;
  2) option_rejected "gp plugin info --json" ;;
  *)
    printf 'gp could not read the project in this directory:\n%s\n' "$project" >&2
    cat "$errfile" >&2
    exit 1
    ;;
esac

printf '{"installation": %s, "project": %s}\n' "$installation" "$project"
```

Then make it executable:

```bash
chmod +x skills/graft/scripts/preflight.sh
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_preflight.py -q`
Expected: PASS. If `test_the_installed_graftpunk_meets_the_skill_floor` fails, stop: the release the precondition names has not shipped into this environment (`uv sync` after pulling `main`), and nothing else in this plan should proceed.

- [ ] **Step 5: Commit**

```bash
git add skills/graft/scripts/preflight.sh tests/unit/test_graft_preflight.py
git commit -m "feat(skill): preflight asks gp every version question in one call and relays the project"
```

---

### Task 4: `SKILL.md`, `commands.md`, and the consent tests

**Files:**
- Create: `skills/graft/SKILL.md`, `skills/graft/references/commands.md`
- Create: `tests/unit/skill_harness.py` (the skill's files, found once for the skill's test modules)
- Test: `tests/unit/test_graft_consent.py`

**Interfaces:**
- Consumes: `preflight.sh` (Task 3); `REPO_ROOT`, `check_invocation`, `gp_invocations`, `blocks`, and `section` from `tests/unit/guide_harness.py` (the project-tools plan, Task 9, which moved them out of the guide's test module and added `section`, the one rule for where a markdown section ends); `PROJECT_GATE` from `src/graftpunk/devtools/scaffold/policy.py:178` (`PROJECT_GATE: Final`), the gate's one owner.
- Produces: the skill, invoked as `/graftpunk:graft [plugin-name] [site-url]`, whose frontmatter pre-approves preflight alone; `references/commands.md` with the declared commands per step and the offered allow rules; `tests/unit/skill_harness.py`, a non-test module (pytest does not collect it) with `SKILL_DIR`, `SKILL_MD`, `COMMANDS_MD`, `skill_docs() -> list[Path]`, `commands_in(text: str) -> list[str]`, and `declared_commands() -> list[str]`, which Task 5's tests use; `tests/unit/test_graft_consent.py`, the frontmatter and consent tests.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/skill_harness.py`:

```python
"""The graft skill's files, found once for the skill's test modules
(tests/unit/test_graft_consent.py and tests/unit/test_graft_references.py), so no
test module imports another. Nothing here is a test.
"""

from __future__ import annotations

from pathlib import Path

from tests.unit.guide_harness import REPO_ROOT, blocks

SKILL_DIR = REPO_ROOT / "skills" / "graft"
SKILL_MD = SKILL_DIR / "SKILL.md"
COMMANDS_MD = SKILL_DIR / "references" / "commands.md"


def skill_docs() -> list[Path]:
    """SKILL.md and every reference: the files the skill's prose tests read."""
    return [SKILL_MD, *sorted((SKILL_DIR / "references").glob("*.md"))]


def commands_in(text: str) -> list[str]:
    """Every non-blank line of every fenced bash block in *text*."""
    return [
        line.strip()
        for _start, body in blocks(text, "bash")
        for line in body.splitlines()
        if line.strip()
    ]


def declared_commands() -> list[str]:
    """Every line of every fenced bash block in commands.md: the commands the steps
    run, whoever runs them."""
    return commands_in(COMMANDS_MD.read_text(encoding="utf-8"))
```

Create `tests/unit/test_graft_consent.py`:

```python
"""The graft skill's frontmatter and consent (graft skill spec, 2026-09-21,
"Testing"): the one pre-approved command, and every allow rule the skill offers
held to the commands commands.md declares."""

from __future__ import annotations

import os
import re
import shlex
from typing import Any

import pytest
import yaml

from graftpunk.devtools.scaffold.policy import PROJECT_GATE
from tests.unit.guide_harness import blocks, check_invocation, gp_invocations, section
from tests.unit.skill_harness import (
    COMMANDS_MD,
    SKILL_DIR,
    SKILL_MD,
    commands_in,
    skill_docs,
)

_SKILL_DIR_VAR = "${CLAUDE_SKILL_DIR}/"
# The whole pre-approved list. Claude Code keeps an allowed-tools grant only for
# the turn that invokes the skill (https://code.claude.com/docs/en/skills), and
# preflight is the one command that turn reliably runs.
_PREFLIGHT_ENTRY = "${CLAUDE_SKILL_DIR}/scripts/preflight.sh *"
# The two uv runners. Each builds an environment from the project's pyproject.toml
# on every run and writes no lockfile or .venv into it. The site runner, for the
# Kick the tires lines, installs the project and its main dependencies, so the
# plugin's entry point, and nothing else. The gate runner, for the Harden gate
# line, adds the project's dev extra (uv warns and continues when there is none)
# and the gate's own tools.
_SITE_RUNNER = "uv run --no-project --with-editable ."
_GATE_RUNNER = "uv run --no-project --with-editable '.[dev]' --with pytest --with ruff"
# The Harden line that runs the gate, one PROJECT_GATE command at a time.
_GATE_SLOT = "<gate-command>"
# The offered rules scoped to the plugin being built, one per declared Kick the
# tires line: the skill puts the plugin's site_name in place of <site-name> when
# it offers them. The live read-only command gets no rule and asks each time.
_SITE_RULES = [f"{_SITE_RUNNER} gp <site-name> --help", f"{_SITE_RUNNER} gp <site-name> login"]
_LIVE_READ_ONLY = f"{_SITE_RUNNER} gp <site-name> <command>"
# What the kick-the-tires step asks before the first live call, whatever the
# user's settings allow: the consent point for the live site and the login.
_LIVE_CALL_QUESTION = "run a live login and one read-only command now?"
# What commands.md says about the gate's commands an offered rule does not cover.
_GATE_RULE_SENTENCE = (
    "The skill offers an allow rule only for the gate's `gp` commands; every other "
    "command in the gate asks each time it runs, unless the user's settings allow it."
)


def _frontmatter() -> dict[str, Any]:
    text = SKILL_MD.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n", 2)[1])


def _allowed() -> list[str]:
    return re.findall(r"Bash\(([^)]*)\)", _frontmatter()["allowed-tools"])


def _commands_section(heading: str) -> str:
    """commands.md's section under *heading*, by the harness's one section rule."""
    return section(COMMANDS_MD.read_text(encoding="utf-8"), heading)


def _skill_run_commands() -> list[str]:
    """The commands under "Run by the skill", the gate's line expanded to each
    PROJECT_GATE command in its place: the only ones an offered rule may cover."""
    return [
        command
        for line in commands_in(_commands_section("## Run by the skill"))
        for command in (
            [line.replace(_GATE_SLOT, gate) for gate in PROJECT_GATE]
            if _GATE_SLOT in line
            else [line]
        )
    ]


def _preflight_commands() -> list[str]:
    return commands_in(_commands_section("## Run by preflight"))


def _offered_rules() -> list[str]:
    """The patterns of the allow rules commands.md offers: its fenced text block under
    "Allow rules for a prompt-free run", one Bash(<pattern>) rule per line."""
    rules_section = _commands_section("## Allow rules for a prompt-free run")
    (_start, body), *_rest = blocks(rules_section, "text")
    rules = [line.strip() for line in body.splitlines() if line.strip()]
    assert rules and all(re.fullmatch(r"Bash\([^()]+\)", rule) for rule in rules), rules
    return [rule.removeprefix("Bash(").removesuffix(")") for rule in rules]


def _matches(pattern: str, command: str) -> bool:
    """Claude Code's Bash permission rule: a trailing " *" matches the prefix with or
    without arguments; anything else matches exactly
    (https://code.claude.com/docs/en/permissions, Wildcard patterns).

    Valid only under the restriction test_every_rule_is_a_scoped_gp_rule enforces
    (a "*" only as the trailing " *"); it models no other wildcard form. Re-check
    it against that page whenever the page changes."""
    if pattern.endswith(" *"):
        prefix = pattern[:-2]
        return command == prefix or command.startswith(prefix + " ")
    return command == pattern


class TestFrontmatter:
    def test_it_parses_and_names_the_skill(self) -> None:
        frontmatter = _frontmatter()
        assert frontmatter["name"] == "graft"
        assert "disable-model-invocation" not in frontmatter

    def test_the_description_names_both_modes(self) -> None:
        description = _frontmatter()["description"]
        assert "Create a graftpunk site plugin" in description
        assert "add commands to an existing one" in description

    def test_allowed_tools_is_exactly_the_preflight_entry(self) -> None:
        """Pinned: anything more would read as consent the grant does not give past
        the invoking turn."""
        assert _allowed() == [_PREFLIGHT_ENTRY]

    def test_every_path_in_allowed_tools_exists(self) -> None:
        paths = [p for p in _allowed() if p.startswith(_SKILL_DIR_VAR)]
        assert paths
        for pattern in paths:
            target = SKILL_DIR / pattern.removeprefix(_SKILL_DIR_VAR).removesuffix(" *")
            assert target.is_file() and os.access(target, os.X_OK), pattern


class TestOfferedAllowRules:
    def test_every_offered_rule_matches_a_command_the_skill_runs(self) -> None:
        """Containment: a rule for a command the skill no longer runs itself fails."""
        declared = _skill_run_commands()
        for pattern in _offered_rules():
            assert any(_matches(pattern, command) for command in declared), (
                f"Bash({pattern}) matches no command under Run by the skill"
            )

    def test_no_rule_is_offered_for_what_only_preflight_runs(self) -> None:
        """Preflight runs its gp calls inside the one pre-approved call, so a rule
        for them would widen consent for nothing."""
        preflight_only = set(_preflight_commands()) - set(_skill_run_commands())
        assert preflight_only
        for command in preflight_only:
            assert not any(_matches(p, command) for p in _offered_rules()), command

    def test_every_rule_is_a_scoped_gp_rule(self) -> None:
        """Never Bash(*), never a bare Bash(gp *), never a command other than gp,
        whether gp runs from PATH or through a uv runner."""
        for pattern in _offered_rules():
            command = pattern
            for runner in (_SITE_RUNNER, _GATE_RUNNER):
                command = command.removeprefix(f"{runner} ")
            assert command.startswith("gp "), pattern
            assert command not in ("*", "gp *"), pattern
            assert "*" not in pattern.removesuffix(" *"), pattern

    def test_no_offered_rule_reaches_the_recorder_or_every_gp_command(self) -> None:
        """The other direction of containment: no rule matches a command outside
        the declared list."""
        for probe in (
            "gp observe --no-session interactive <url>",
            "gp observe -s <session> interactive <url>",
            "gp anything",
            "gp <site-name> delete-everything",
            "uv run anything",
            f"{_SITE_RUNNER} pytest",
            f"{_GATE_RUNNER} pytest",
            f"{_SITE_RUNNER} gp anything",
            f"{_GATE_RUNNER} gp anything",
            f"{_SITE_RUNNER} gp <site-name> delete-everything",
            f"{_GATE_RUNNER} gp <site-name> delete-everything",
        ):
            assert not any(_matches(pattern, probe) for pattern in _offered_rules()), probe

    def test_the_plugin_rules_are_the_declared_help_and_login_lines(self) -> None:
        rules = _offered_rules()
        assert [rule for rule in rules if "<site-name>" in rule] == _SITE_RULES
        assert set(_SITE_RULES) <= set(_skill_run_commands())

    def test_the_live_read_only_command_asks_each_time(self) -> None:
        """It reads the user's account: declared, and covered by no offered rule."""
        assert _LIVE_READ_ONLY in _skill_run_commands()
        assert not any(_matches(pattern, _LIVE_READ_ONLY) for pattern in _offered_rules())

    def test_the_gates_gp_commands_have_rules_and_commands_md_says_the_rest_do_not(
        self,
    ) -> None:
        """The gate is policy.PROJECT_GATE's to list, and runs through the gate
        runner. Every gp command in it is covered by an offered rule; an
        offered rule is only ever a gp rule (test_every_rule_is_a_scoped_gp_rule),
        so commands.md says in words that the gate's other commands ask each time."""
        rules = _offered_rules()
        gate_gp = [f"{_GATE_RUNNER} {c}" for c in PROJECT_GATE if c.startswith("gp ")]
        assert gate_gp
        for command in gate_gp:
            assert any(_matches(pattern, command) for pattern in rules), command
        commands_md = " ".join(COMMANDS_MD.read_text(encoding="utf-8").split())
        assert _GATE_RULE_SENTENCE in commands_md


def test_the_kick_the_tires_lines_and_the_gate_run_through_their_runners() -> None:
    """The kick-the-tires lines need the plugin's entry point installed, which the gp
    on PATH does not have; the gate needs the project's dev dependencies and its
    own tools as well."""
    kick = commands_in(_commands_section("### Kick the tires"))
    harden = commands_in(_commands_section("### Harden"))
    assert kick and f"{_GATE_RUNNER} {_GATE_SLOT}" in harden
    for line in kick:
        assert line.startswith(f"{_SITE_RUNNER} gp "), line


def test_the_gate_runner_installs_every_program_the_gate_runs() -> None:
    """PROJECT_GATE owns the gate's commands; the declared Harden line's --with list
    must install each program one of them runs, gp aside (the project's graftpunk
    dependency installs it). A new tool in PROJECT_GATE fails here, not on users."""
    harden = commands_in(_commands_section("### Harden"))
    (gate_line,) = [line for line in harden if _GATE_SLOT in line]
    words = shlex.split(gate_line)
    installed = {words[i + 1] for i, word in enumerate(words[:-1]) if word == "--with"}
    for command in PROJECT_GATE:
        program = command.split()[0]
        assert program == "gp" or program in installed, command


def test_the_live_step_asks_before_the_first_live_call() -> None:
    """Found by the step's bold name, up to the next numbered item or heading, so
    adding or removing a step does not move it."""
    text = SKILL_MD.read_text(encoding="utf-8")
    rest = text[text.index("**Kick the tires**") :]
    ends = [
        m.start()
        for m in (re.search(r"^\d+\. \*\*", rest, re.M), re.search(r"^## ", rest, re.M))
        if m
    ]
    step = rest[: min(ends)] if ends else rest
    assert _LIVE_CALL_QUESTION in " ".join(step.split())


SKILL_INVOCATIONS = [
    (doc.name, line_no, invocation)
    for doc in skill_docs()
    for line_no, invocation in gp_invocations(doc.read_text(encoding="utf-8"))
]


def test_the_skill_has_invocations_to_check() -> None:
    assert SKILL_INVOCATIONS


@pytest.mark.parametrize(
    ("doc", "line_no", "invocation"), SKILL_INVOCATIONS, ids=lambda v: str(v)[:60]
)
def test_every_gp_invocation_names_a_real_command_and_options(
    doc: str, line_no: int, invocation: str
) -> None:
    check_invocation(invocation, f"{doc}:{line_no}")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_consent.py -q`
Expected: collection error, `FileNotFoundError` reading `skills/graft/SKILL.md`.

- [ ] **Step 3: Write `SKILL.md`**

Create `skills/graft/SKILL.md`:

````markdown
---
name: graft
description: Create a graftpunk site plugin from a browser recording, or add commands to an existing one. Use when asked to build, scaffold, or extend a graftpunk plugin for a site, or to add a command to a plugin.
argument-hint: "[plugin-name] [site-url]"
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/preflight.sh *)
---

# graft: build or extend a graftpunk site plugin

You are taking a developer through `docs/PLUGIN_DEVELOPMENT.md` in the graftpunk
repository, called "the guide" below. The guide is the reference and this skill
is a route through it: when the two disagree, the guide wins. Two modes share one
flow. Create mode starts from an empty directory and ends with a new plugin
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

Run `${CLAUDE_SKILL_DIR}/scripts/preflight.sh` first, on every invocation. If it
exits non-zero, show its message verbatim and stop. On exit 0 it prints one JSON
object, `{"installation": ..., "project": ...}`, and `project.directory` picks the
mode: `empty` is create mode, `plugin` is enhance mode. For `foreign`, stop and
say: "this directory holds a project that is not a graftpunk plugin; run the
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
suite), take the entry whose `entry_point` is the plugin name given above; when
none was given or none matches, ask which.

## How to run the steps

Begin every turn by naming the step you are on, in one line, by name and never
by number: "Step: scaffold. Done: ...; next: ...". Ask one question at a time.
When a `gp` command fails, show its output verbatim, find the cause in the guide
section the step cites, fix it, and run the step again once. Never skip a step,
and never summarise a failure away. Read a reference only at the step that names
it.

## The steps

1. **Frame** (guide: Frame). Collect the plugin name and check it with the
   command in the Frame block of `references/commands.md`, which writes
   nothing: exit 0 means the
   name is usable, and on exit 1 show the refusal and ask for another. Collect
   the site URL, and what the user wants to do on the site in plain words ("see
   my orders and download invoices" is enough). Do not ask for command names or
   endpoints; the digest supplies both. The login shape and the backend are
   decided later from the digest, and confirmed with the user only when the
   digest is ambiguous.
2. **Capture** (guide: Capture). Read `references/capture.md`, hand the
   recording to the user exactly as it says, and choose the session and run as
   its "After the recording" section says. You never run the recorder yourself.
   Every later step reads that session and that run.
3. **Understand** (guide: Understand). Run the command in the Understand block
   of `references/commands.md` on the chosen session and run, then read
   `references/digest.md` and build the proposal it describes. The user keeps,
   renames, or drops rows in one answer.
4. **Scaffold** (guide: Scaffold). Run the `gp plugin new` line of the Scaffold
   block in `references/commands.md`, with the chosen session and run and one
   `--command` per row the user kept. The generator writes only those stubs,
   under those names, each with its endpoint declared. Edit nothing it wrote during this step. Then run
   `gp plugin info --json` and confirm every agreed command is listed with the
   endpoint it was agreed for.
5. **Implement** (guide: Implement). For each stub, fill in the request, name
   the parameters, decide the return shape, and replace every `GP-FILL` marker.
   Raise `CommandError` or `PluginError` on failure. Read `references/rules.md`
   and read the guide section behind any rule the work touches.
6. **Harden** (guide: Harden). Read `references/harden.md` and follow it: one
   fixture and one test per command, then the project's gate (guide: The gate),
   acting on gp's output as that file says. The gate must pass before the next
   step.
7. **Kick the tires** (guide: Check the CLI surface you shipped) (guide: Login).
   Before the first live call, ask in words, whatever the user's settings
   allow: "run a live login and one read-only command now?" Their answer is
   the consent for the live site and the login; an allow rule does not stand in
   for it. On yes, run the help line of the Kick the tires block in
   `references/commands.md` and confirm every agreed command name is listed,
   then its login line, then its read-only command line with one read-only
   command from the agreed proposal, against the live site while the user
   watches. Credentials come from the
   environment or the workstation env file, which the user fills in: print the
   `gp config set` lines with placeholder values, never real ones. If the login
   fails, diagnose it against the guide's Login section, adjust the plugin's
   `LoginConfig`, and try once more.
8. **Publish checklist** (guide: Before you publish). Read that section of the
   guide. Its first item, the gate, already holds after the harden step. Walk
   the rest as the guide lists them, fix what you can, and stop with the items
   left for the user.

## Where enhance mode differs

The steps above are written for create mode. Enhance mode runs the same steps
with these differences. This list is the index of what enhance mode changes;
the references hold the details.

- Frame collects only the new thing the user wants to do. The plugin's
  `entry_point` (the name `gp plugin add-command` takes), its `site_name` (the
  name `gp` runs it by), and its `base_url` come from `project.plugins`.
- Capture picks the recorder line by whether the plugin has a session, as
  `references/capture.md` says.
- Understand leaves out every endpoint an existing command declares, and prints
  each one it left out beside that command's name and declared endpoint, so a
  declaration that went stale after a hand edit shows up. Every remaining row
  is marked new. Each existing command reported with `endpoint: null` gets a
  row of its own reading "existing command, endpoint not declared", so the user
  can say whether a proposed row duplicates it. Never drop or propose over an
  undeclared command silently.
- Scaffold does not run `gp plugin new`. For each agreed command it runs the
  `gp plugin add-command` line of the Scaffold block in `references/commands.md`,
  with the chosen session and run, which adds one stub in the generated shape
  and writes no test. Act on gp's output as `references/harden.md` says ("The
  gate"); that file also says what the harden step does for each added command.
- The publish checklist is limited to the items the new commands touch.

## Secrets

Never ask for a password, never write a credential anywhere, and never print
what `gp config get --resolve` returns. Never read a value that came off the
account: not a cookie or token value, not a HAR body, not a capture. From a
recording, read the `--endpoints-json` projection and nothing else. When the
user pastes a secret into the conversation, say where it belongs
(`gp config set NAME '$(your-secret-tool read ...)'`) and do not use it.
````

The argument paragraph reads correctly whether or not Claude Code substitutes the placeholders. The skills documentation (https://code.claude.com/docs/en/skills, fetched 2026-09-23) says `$0` is "the first argument" (0-based) and "An indexed placeholder with no corresponding argument, such as `$2` when only one argument was passed, stays in the content unchanged." Whether a placeholder is substituted everywhere it appears in the file is unverified (the page does not say so in as many words), so the paragraph is written to be safe if it is: it names a placeholder only where it wants the value, and describes a missing one in words ("a dollar sign followed by a digit") rather than by writing one.

- [ ] **Step 4: Write `commands.md`**

Create `skills/graft/references/commands.md`:

````markdown
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

The last line is the project's gate, one unit, run whole: each of its commands
in place of `<gate-command>`, in order. `policy.PROJECT_GATE` owns those
commands and the guide's section on it lists them (guide: The gate). The skill
offers an allow rule only for the gate's `gp` commands; every other command in
the gate asks each time it runs, unless the user's settings allow it.

```bash
gp observe fixtures <session> <run> --match "<METHOD> <template>"
gp plugin upgrade
uv run --no-project --with-editable '.[dev]' --with pytest --with ruff <gate-command>
```

### Kick the tires

These touch the live site or a credential. The skill asks in words before the
first of them runs, whatever the user's settings allow. The last line is one
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
````

`gp plugin add-command` takes `--run` as `gp plugin new` does (`src/graftpunk/cli/scaffold_project_commands.py:106` (`"--run"`)), so both scaffold lines read the run chosen at the end of the capture step, never whichever run is newest when they run; the walker test resolves both lines against the CLI.

**Which `gp` loads the plugin, and what each runner installs (probed 2026-10-04 with uv 0.12.18, in a scratch directory holding `UV_CACHE_DIR`, `UV_TOOL_DIR`, and `UV_TOOL_BIN_DIR`, against a project from `gp plugin new myshop --url https://myshop.example`).** A `gp` installed with `uv tool install <graftpunk checkout>` does not list `myshop` in `gp --help`, since the plugin's entry point is not installed beside it. In the project directory, the site runner `uv run --no-project --with-editable . gp --help` lists `myshop`. The site runner installs the project and its main dependencies and nothing else: `pytest --version` through it fails with `No such file or directory (os error 2)`, so a change to the gate's tools never changes the Kick the tires lines or their allow rules. The gate runner `uv run --no-project --with-editable '.[dev]' --with pytest --with ruff` installs the project with its main and `dev` dependencies, plus pytest and ruff: with `six` added to the `dev` extra only, `python -c 'import six'` succeeded through the gate runner (six 1.17.0) and failed through the site runner (`ModuleNotFoundError: No module named 'six'`). With the `dev` extra deleted from `pyproject.toml`, the gate runner printed a warning that the package `graftpunk-myshop @ file:///...` does not have an extra named `dev` and still ran `pytest --version` (pytest 9.1.1), `ruff --version` (ruff 0.16.10), and a `gp --help` that lists `myshop`, so enhance mode works on a project with no `dev` extra (only `gp plugin new` writes one). Afterwards the directory held no `uv.lock` and no `.venv/`, so `harden.md` has no reinstall step. Both runners read `pyproject.toml` on every run: in an earlier probe the same day, after a dependency was added there, the next run imported it, and after graftpunk's floor was raised to `>=99.0.0`, the next run failed to resolve. Each run resolves every dependency to the newest release the project's requirements allow, not to the floor and not to the project's `uv.lock`: with `six>=1.10.0` among the main dependencies and a `uv.lock` from `uv lock --resolution lowest-direct` pinning six 1.10.0, the gate runner installed six 1.17.0, PyPI's newest release. The gate therefore judges the plugin against the newest versions the project allows, which may be newer than its lock, and `harden.md` says so as a trade-off. It also needs graftpunk 1.17.0 (this plan's precondition), the first release that ships `gp plugin check` and `graftpunk.testing`, to be published: in the earlier probe the scaffold's `graftpunk[browser]>=1.16.0` resolved PyPI's 1.16.0, its newest release that day, which has neither, and `ruff check .` passed while `pytest` failed on the missing `graftpunk.testing` and `gp plugin check` was an unknown command. Another earlier probe the same day, through `uv run --project .` with the checkout's graftpunk overlaid (`--with <graftpunk checkout>`), had `pytest` pass and `gp plugin check` run and report the fresh scaffold's `GP-FILL` markers, as the guide says it does.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_consent.py -q`
Expected: PASS. The walker test resolves every `gp` invocation in `SKILL.md` and `commands.md` against the installed CLI, the interactive recorder and the `gp version --contract` handshake included.

- [ ] **Step 6: Commit**

```bash
git add skills/graft/SKILL.md skills/graft/references/commands.md tests/unit/skill_harness.py tests/unit/test_graft_consent.py
git commit -m "feat(skill): SKILL.md pre-approves preflight alone, and commands.md declares the commands and the allow rules offered for them"
```

---

### Task 5: The references, and the tests that pin them to the guide

**Files:**
- Create: `skills/graft/references/rules.md`, `capture.md`, `digest.md`, `harden.md`
- Test: `tests/unit/test_graft_references.py`

The slug helpers the citation tests use are `slug` and `slugs_of`, both defined in `tests/unit/guide_harness.py` (`def slug`, `def slugs_of`). The project-tools plan's Task 9 moved `_slugs_of` there from `tests/unit/test_plugin_development_guide.py` and extracted `slug` from it, so this task edits no guide test.

**Interfaces:**
- Consumes: `CTRL_C_REACHES_GP` (Task 1); `SKILL_DIR`, `SKILL_MD`, `COMMANDS_MD`, `skill_docs`, and `declared_commands` from `tests/unit/skill_harness.py` (Task 4); `GUIDE`, `GUIDE_TEXT`, `REPO_ROOT`, `gp_invocations`, `outside_fences`, `section`, `slug`, and `slugs_of` from `tests/unit/guide_harness.py` (the project-tools plan, Task 9); `DigestSource` from `src/graftpunk/har/digest.py:202` (`class DigestSource`), `digest` from `src/graftpunk/har/digest.py:1430` (`def digest`), `endpoints_projection` from `src/graftpunk/har/report.py:222` (`def endpoints_projection`), and `LoginForm` from `src/graftpunk/har/documents.py:65` (`class LoginForm`). Fence handling and the section rule have one owner each, in the harness: `_prose` starts from `outside_fences`, and `_without_section` is derived from `section`.
- Produces: the four references `SKILL.md` names, and `tests/unit/test_graft_references.py`, the citation, copy, command-ownership, and digest-list tests. The copy detector (`_words`, `_runs`, `_prose`) and its self-tests' probe (`_probe`, which takes a sentence from `GUIDE_TEXT` at test time) stay in that module, their one user. No reference restates what a `gp` command prints: `harden.md` ("The gate") tells the reader to show gp's output and act on every instruction in it, and the other files point there, so gp's output stays the one owner of the follow-up steps. `capture.md` and `harden.md` carry no fenced command blocks: they point to the step's block in `commands.md`, the one owner of the commands.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_graft_references.py`:

```python
"""The graft skill's references against the guide they cite, and the commands
against commands.md, their one owner (graft skill spec, 2026-09-21, "Testing")."""

from __future__ import annotations

import dataclasses
import json
import re
import shlex
from pathlib import Path
from typing import Any

import pytest

from graftpunk.har.digest import DigestSource, digest
from graftpunk.har.documents import LoginForm
from graftpunk.har.report import endpoints_projection
from tests.unit.guide_harness import (
    GUIDE,
    GUIDE_TEXT,
    REPO_ROOT,
    gp_invocations,
    outside_fences,
    section,
    slug,
    slugs_of,
)
from tests.unit.skill_harness import (
    COMMANDS_MD,
    SKILL_DIR,
    SKILL_MD,
    declared_commands,
    skill_docs,
)

_CITATION_RE = re.compile(r"\(guide: ([^)]+)\)")
_QUOTE_CITATION_RE = re.compile(r"\(guide: ([^)]+)\)\s*$")
# "Cite, do not copy", enforced at a stated threshold.
_COPY_RUN = 8
_MAX_REFERENCE_LINES = 120


def _words(text: str) -> list[str]:
    """Lowercased words with punctuation stripped."""
    return re.sub(r"[^\w\s]", " ", text.lower()).split()


def _runs(words: list[str]) -> set[tuple[str, ...]]:
    return {tuple(words[i : i + _COPY_RUN]) for i in range(len(words) - _COPY_RUN + 1)}


def _prose(text: str) -> str:
    """*text* outside fenced blocks and quotations, with its citations removed: what the
    copy check compares. A quotation is a blockquote line ending with a citation.
    Fences are the harness's to find (``outside_fences``)."""
    kept = [
        line
        for line in outside_fences(text).splitlines()
        if not (line.startswith("> ") and _QUOTE_CITATION_RE.search(line))
    ]
    return _CITATION_RE.sub(" ", "\n".join(kept))


def _without_section(text: str, heading: str) -> str:
    """*text* without the section under the line *heading*, where the section ends by
    the harness's one rule (``section``); *text* itself when it has no such line."""
    lines = text.splitlines()
    if heading not in lines:
        return text
    start = lines.index(heading)
    end = start + len(section(text, heading).splitlines())
    return "\n".join(lines[:start] + lines[end:])


# The guide's "With the skill" section describes this skill, and holds only its
# install and invocation facts, which the skill may state in the same words;
# every other section is the guide's to state and the skill's to cite.
GUIDE_RUNS = _runs(_words(_without_section(GUIDE_TEXT, "## With the skill")))

# The section the copy detector's self-tests take their probe sentence from.
_PROBE_HEADING = "## Capture"


def _probe() -> tuple[str, str]:
    """The first prose sentence of the guide's _PROBE_HEADING section, and that
    section's title, read from GUIDE_TEXT at test time so a guide edit cannot leave
    the self-tests probing a sentence the guide no longer holds."""
    paragraphs = outside_fences(section(GUIDE_TEXT, _PROBE_HEADING)).split("\n\n")[1:]
    paragraph = " ".join(next(p for p in paragraphs if p.strip()).split())
    sentence = re.split(r"(?<=[.:])\s", paragraph)[0]
    assert len(_words(sentence)) >= _COPY_RUN, sentence
    return sentence, _PROBE_HEADING.lstrip("# ")


class TestCitations:
    @pytest.mark.parametrize("doc", skill_docs(), ids=lambda p: p.name)
    def test_every_cited_heading_exists(self, doc: Path) -> None:
        slugs = slugs_of(GUIDE)
        for title in _CITATION_RE.findall(doc.read_text(encoding="utf-8")):
            assert slug(title) in slugs, f"{doc.name} cites a heading the guide lacks: {title!r}"

    def test_every_step_cites_a_heading(self) -> None:
        steps = [
            line for line in SKILL_MD.read_text().splitlines() if re.match(r"^\d+\. \*\*", line)
        ]
        assert steps
        assert all(_CITATION_RE.search(step) for step in steps)

    def test_every_rule_names_a_heading(self) -> None:
        rules: list[str] = []
        for line in (SKILL_DIR / "references" / "rules.md").read_text().splitlines():
            if line.startswith("- "):
                rules.append(line)
            elif rules and line.startswith("  "):
                rules[-1] += " " + line.strip()
        assert rules
        slugs = slugs_of(GUIDE)
        for rule in rules:
            (title,) = _CITATION_RE.findall(rule)
            assert slug(title) in slugs, rule


class TestNothingIsCopied:
    @pytest.mark.parametrize("doc", skill_docs(), ids=lambda p: p.name)
    def test_no_run_of_eight_words_from_the_guide(self, doc: Path) -> None:
        copied = sorted(
            " ".join(run) for run in _runs(_words(_prose(doc.read_text()))) & GUIDE_RUNS
        )
        assert copied == [], f"{doc.name} copies the guide: {copied[:3]}"

    def test_the_detector_catches_a_copied_sentence(self) -> None:
        sentence, _title = _probe()
        assert _runs(_words(_prose(sentence))) & GUIDE_RUNS

    def test_a_quotation_is_exempt_only_with_a_citation(self) -> None:
        sentence, title = _probe()
        assert not _runs(_words(_prose(f"> {sentence} (guide: {title})"))) & GUIDE_RUNS
        assert _runs(_words(_prose(f"> {sentence}"))) & GUIDE_RUNS

    @pytest.mark.parametrize("doc", skill_docs(), ids=lambda p: p.name)
    def test_every_quotation_cites_a_heading_that_exists(self, doc: Path) -> None:
        slugs = slugs_of(GUIDE)
        for line in doc.read_text().splitlines():
            if line.startswith("> "):
                match = _QUOTE_CITATION_RE.search(line)
                assert match and slug(match.group(1)) in slugs, line


@pytest.mark.parametrize(
    "name", ["rules.md", "capture.md", "digest.md", "harden.md", "commands.md"]
)
def test_each_reference_exists_and_stays_short(name: str) -> None:
    lines = (SKILL_DIR / "references" / name).read_text().splitlines()
    assert len(lines) < _MAX_REFERENCE_LINES


# gp invocations the skill docs spell that Claude does not run from commands.md:
# lines printed for the user to fill in, one named only to say its output is never
# printed, and a worked example with real values. The one exemption list, so a new
# exemption shows in review as a change to this test.
_NOT_RUN_FROM_COMMANDS_MD = (
    "gp config set",  # printed for the user with placeholder values (SKILL.md, Secrets)
    "gp config get --resolve",  # named only to say its output is never printed
    "gp plugin new myshop --from-run myshop",  # digest.md's worked example
)


def _token_pattern(token: str) -> re.Pattern[str]:
    """A commands.md token as a pattern: each ``<placeholder>`` in it stands for any
    non-empty value."""
    return re.compile(".+".join(re.escape(part) for part in re.split(r"<[^<>]+>", token)))


def _is_declared(invocation: str, declared: list[str]) -> bool:
    """The one matcher: whether *invocation* is a commands.md line, with its
    placeholders filled or not, or the leading words of one (prose that names "the
    `gp plugin new` line" points at it). Leading words stop before the first option,
    so a partial command with options of its own must match a line in full."""
    words = shlex.split(invocation)
    for line in declared:
        tokens = shlex.split(line)
        if len(words) > len(tokens):
            continue
        if not all(_token_pattern(t).fullmatch(w) for t, w in zip(tokens, words, strict=False)):
            continue
        if len(words) == len(tokens) or not any(w.startswith("-") for w in words):
            return True
    return False


@pytest.mark.parametrize("doc", skill_docs(), ids=lambda p: p.name)
def test_commands_md_owns_every_gp_command_the_skill_spells(doc: Path) -> None:
    """Containment for the commands themselves: every gp invocation a skill doc
    spells, backticked or fenced, is a commands.md line or the leading words of one,
    or a named exemption. Outside commands.md a placeholder is never spelled: the
    doc names the commands.md block instead, so each templated command has one
    spelling. A command added to a step without adding it to commands.md fails here."""
    declared = declared_commands()
    for line_no, invocation in gp_invocations(doc.read_text(encoding="utf-8")):
        if invocation.startswith(_NOT_RUN_FROM_COMMANDS_MD):
            continue
        where = f"{doc.name}:{line_no}: {invocation}"
        assert _is_declared(invocation, declared), f"{where} is not declared in commands.md"
        if doc != COMMANDS_MD:
            assert "<" not in invocation, f"{where} spells a command commands.md owns"


# digest.md's closed lists, each compared with what the digest produces. The
# projection's type labels are not a closed list there: graftpunk.har.digest
# builds them across several functions and keeps no constant naming them all, so
# digest.md says which labels the list includes, and no test here pins them.
_DIGEST_MD = SKILL_DIR / "references" / "digest.md"


def _listed(opening: str, closing: str) -> set[str]:
    """The backticked names in digest.md between *opening* and the next *closing*,
    whitespace collapsed first: one of its closed lists."""
    flat = " ".join(_DIGEST_MD.read_text(encoding="utf-8").split())
    start = flat.index(opening) + len(opening)
    return set(re.findall(r"`([^`]+)`", flat[start : flat.index(closing, start)]))


def _projection() -> dict[str, Any]:
    """endpoints_projection on the repository's sample recording, with one login form
    added, since the sample records none."""
    result = digest(DigestSource.from_har(REPO_ROOT / "tests" / "fixtures" / "sample.har"))
    form = LoginForm(
        action="/session",
        method="POST",
        fields={"username": "#username", "password": "#password"},
        submit=None,
        hidden=(),
        source="https://myshop.example/login",
    )
    return endpoints_projection(dataclasses.replace(result, login_forms=(form,)))


def _har_entry(
    method: str,
    url: str,
    *,
    status: int = 200,
    body: str = "",
    headers: tuple[tuple[str, str], ...] = (),
    post: str | None = None,
) -> dict[str, Any]:
    response_headers = [("Content-Type", "text/html"), *headers]
    entry: dict[str, Any] = {
        "startedDateTime": "2026-09-10T10:00:00.000Z",
        "time": 5,
        "request": {"method": method, "url": url, "headers": [], "cookies": [], "queryString": []},
        "response": {
            "status": status,
            "statusText": "",
            "headers": [{"name": n, "value": v} for n, v in response_headers],
            "cookies": [],
            "content": {"mimeType": "text/html", "text": body, "size": len(body)},
        },
    }
    if post is not None:
        entry["request"]["postData"] = {"mimeType": "application/json", "text": post}
    return entry


def _login_projection(tmp_path: Path) -> dict[str, Any]:
    """endpoints_projection of a recorded login: the form page, the credential post
    answering a redirect, one redirect hop, and the landing page setting a cookie."""
    form = (
        '<form action="/session" method="post"><input type="email" name="email">'
        '<input type="password" name="password"></form>'
    )
    credentials = json.dumps({"email": "alice@example.com", "password": "x"})
    entries = [
        _har_entry("GET", "https://myshop.example/signin", body=form),
        _har_entry(
            "POST",
            "https://myshop.example/session",
            status=302,
            headers=(("Location", "/sso/callback"),),
            post=credentials,
        ),
        _har_entry(
            "GET",
            "https://myshop.example/sso/callback",
            status=302,
            headers=(("Location", "/dashboard"),),
        ),
        _har_entry(
            "GET",
            "https://myshop.example/dashboard",
            body="<p>orders</p>",
            headers=(("Set-Cookie", "s=v; Path=/"),),
        ),
    ]
    har = tmp_path / "login.har"
    har.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))
    return endpoints_projection(digest(DigestSource.from_har(har)))


class TestDigestListsMatchTheirSource:
    def test_the_auth_url_kinds_are_the_ones_a_login_produces(self, tmp_path: Path) -> None:
        """Derived from behaviour: a recorded login that walks a form page, a
        credential post, a redirect hop, and a landing that sets a cookie, digested,
        and the kinds its auth_urls hold. That recording produces every login kind,
        so digest.md's list must equal them."""
        produced = {o["kind"] for o in _login_projection(tmp_path)["login"]["auth_urls"]}
        assert produced
        assert _listed("each of kind", ". A plain form") == produced

    def test_the_endpoint_fields_are_the_projections(self) -> None:
        (endpoint, *_rest) = _projection()["endpoints"]
        assert _listed("one entry per endpoint with", ", plus a") == set(endpoint)

    def test_the_login_summary_keys_are_the_projections(self) -> None:
        text = _DIGEST_MD.read_text(encoding="utf-8")
        for key in _projection()["login"]:
            assert f"`{key}`" in text, key

    def test_the_form_keys_are_the_projections(self) -> None:
        (form,) = _projection()["login"]["forms"]
        assert _listed("`forms` holds each login form's", ". Name every") == set(form)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_references.py tests/unit/test_plugin_development_guide.py -q`
Expected: FAIL (`test_each_reference_exists_and_stays_short[rules.md]` and its siblings: `FileNotFoundError`). The guide tests still pass.

- [ ] **Step 3: Write `rules.md`**

Create `skills/graft/references/rules.md`:

```markdown
# House rules

One line per rule, each naming the guide heading that states it. When a rule
applies to the work in front of you, read the cited section before acting; this
file is an index, not the rule text.

- The plugin registers through its package's entry point, never through the
  plugins directory. (guide: Register through an entry point, not through the plugins directory)
- A capture is a credential: it never enters git and is never pasted anywhere.
  (guide: Capture)
- A fixture keeps the structure of a capture and invents every value in it.
  (guide: Deriving a fixture from a capture)
- A parser that finds no container raises and names what it looked for; only a
  container that is present and empty returns an empty list.
  (guide: Parsers do not return a confident empty list)
- The login's `failure` text is the site's exact wording, taken from a real
  failed attempt. (guide: Getting the signals right)
- Secrets reach the plugin from the environment or the workstation env file,
  and from nowhere else. (guide: Secrets and configuration)
- A secret is looked up by what it is, never by the label it happens to carry.
  (guide: Resolve a secret by what it is, not by what it is labelled)
- Commands raise the exception that tells the user what went wrong.
  (guide: What they raise, and what the user sees)
- Tests never see the developer's own site variables.
  (guide: Keep the developer's own environment out of the tests)
```

- [ ] **Step 4: Write `capture.md`, choosing the hand-off by Task 1's result**

Create `skills/graft/references/capture.md` with the content below, replacing the line `HAND-OFF` with the paragraph for the recorded `CTRL_C_REACHES_GP`.

For `CTRL_C_REACHES_GP=yes`:

```markdown
Tell the user to run that line in this Claude Code session, typed after the
`!` prefix and a space. Then they log in,
work through every item on the list below, and press Ctrl+C, which ends the
recorder and saves what it captured. Wait until they say it is done.
```

For `CTRL_C_REACHES_GP=no`:

```markdown
Tell the user to run that line in a separate terminal window, not with the
`!` prefix in this session: ending the recorder takes a Ctrl+C that reaches it,
and this session's shell mode does not pass one through. In that terminal they
log in, work through every item on the list below, and press Ctrl+C, which ends
the recorder and saves what it captured. Then they come back here and say it is
done.
```

The file:

````markdown
# Handing the recording to the user

The recording needs a person at the keyboard: logging in, clicking through
pages, and ending the recorder. The skill prints the command, says what to do,
and waits. It never runs the recorder itself.

## What to print

In enhance mode, run `gp session list` first. Print the recorder line from the
"Run by the user" block of `references/commands.md`, with the site's URL in
place of `<url>`: in enhance mode, the `-s <session>` line when that list shows
a session for the plugin, so the recording starts already logged in; otherwise,
and always in create mode, the `--no-session` line.

HAND-OFF

## What to exercise

Turn each want the user named in the frame step into something to click, and
list those for them before they start. Useful prompts:

- Log in the ordinary way, even when you expect the session to be remembered.
- For each list you want as a command: open it, go to the next page, and apply
  one filter or sort you would use from the shell.
- For each detail you want: open two different items, so the digest can tell
  the fixed part of the path from the identifier.
- For each download: start it once and let it finish.
- Skip everything else. Unrelated pages add endpoints to read and nothing to use.

## After the recording

This is where the recording is chosen, once. When the user says they are done,
run `gp observe list` and take the newest run under the name the recording was
stored under: after the `--no-session` line, the name graftpunk inferred from
the host; after the `-s <session>` line, that session's stored name as
`gp observe list` shows it. Say the session name and the run ID back; the user
may name another run instead. Every later step reads that session and that run, never
whichever run is newest by then. A want that the digest later shows no endpoint
for gets a second, narrower recording aimed at that one flow, and that
recording is chosen the same way.
````

The two recorder lines store their runs under different names. `--no-session` stores under the name inferred from the URL, and `-s` under the session's own name (`src/graftpunk/cli/observe_commands.py:629` (`def _resolve_observe_context`)), passed through `session_dirname` on the way to disk (`src/graftpunk/observe/storage.py:20` (`def session_dirname`)), so a session `myshop@alice` lists as `myshop-alice`. `gp observe list` prints those directory names, which is why "After the recording" reads the name from its output rather than from the session as typed.

- [ ] **Step 5: Write `digest.md`**

Create `skills/graft/references/digest.md`:

````markdown
# From the digest to a command proposal

The user should never have to know the commands in advance. The source is the
projection the Understand block of `references/commands.md` prints: one entry
per endpoint with `method`, `template`, `login_flow`, `content_type`, `shape`,
`query_params`, `body_params`, and `custom_headers`, plus a `login` summary.
Read that projection only; never read the HAR, a capture, or `--json`.

## The rules, in order

1. Drop every entry whose `login_flow` is true. The generator skips exactly the
   same entries, so the proposal and the scaffold agree. Trust the digest for
   the rest: static assets, trackers, and other hosts never reach the list.
2. Keep JSON endpoints, and HTML documents that carry the user's own data (a
   dashboard, an order list, a statement page). Drop navigation chrome.
3. Name each kept entry as a short verb phrase a person would type at the
   shell: `orders` for `GET /api/orders`, `order` for `GET /api/orders/{order_id}`
   (the path parameter becomes the command's argument), `invoice-pdf` for a
   document download. A list and its detail are two commands, never one.
4. Show one table, one row per command: name, what it returns (from `shape` and
   `content_type`), the endpoint as `<METHOD> <template>`, and the parameters
   with their types (each `query_params` and `body_params` value is a type
   label; the labels include `str`, `int`, `float`, `bool`, `object`, `mixed`,
   and `list[<element>]`, and a `mixed` or list label needs the user's decision
   when the row is proposed) (guide: Understand). Say which of the user's wants each row serves, and name any want with no row.
5. Ask one question, "keep, rename, or drop any of these?", and apply the
   answer. A want with no endpoint goes back to the capture step for that flow.

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
the login shape with the user only when that summary leaves it open.

`forms` holds each login form's `action`, its `fields` selectors by role, its
`submit` selector, `neutral_roles`, and `unresolved_roles`. Name every
unresolved role in the proposal: the generated login config carries a
`GP-FILL` for each, which the implement step fills from the page with the
user's confirmation (guide: Login).

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
````

- [ ] **Step 6: Write `harden.md`**

Create `skills/graft/references/harden.md`:

````markdown
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

In enhance mode `gp plugin add-command` writes no test. For each command it
added, write one in the shape of the tests the project already has: the same
context over the plugin's fixtures directory, one call, and assertions on the
returned shape (guide: Test against fixtures, not against the site).

## The gate

Read the guide's section on the gate (guide: The gate) and run every command it
lists, through the runner the Harden block of `references/commands.md` gives
it, until all of them pass. This section is where
the skill's handling of gp's output lives, for the scaffold step and this one:
show the user gp's output and act on every instruction in it, then run the
gate again. Both `gp plugin add-command` and `gp plugin upgrade` may ask for
the project to be installed again or for its graftpunk requirement to be
raised, and each finding of the gate's plugin check carries the advice to
follow.

That runner installs the project with its main and `dev` dependencies and the
gate's own tools, and builds the environment from `pyproject.toml` on every
run, so there is nothing to install: when gp asks for the project to be
installed again, run the gate again. Each run resolves to the newest versions
the project's requirements allow, not to the versions in the project's
`uv.lock` if it has one. That is a trade-off: the gate may judge the plugin
against newer versions than the project's lock pins, so when a failure points
at a dependency's version, say that to the user. Never install anything into the `gp` on
the user's PATH. The cases to expect, in plain words:

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
````

"A fixture per command" names no gp output label: the generated test spells no fixture path, and `FixtureSession` finds a fixture by the capture's own file name (the name `gp observe fixtures` writes) and answers 404 when none matches (`src/graftpunk/testing/__init__.py:71` (`class FixtureSession`)), so the copy keeps the capture's name.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_references.py tests/unit/test_plugin_development_guide.py -q`
Expected: PASS. If `test_no_run_of_eight_words_from_the_guide` names a run, reword that sentence of the reference in your own words (do not edit the guide to make it pass), and run again.

- [ ] **Step 8: Commit**

The message names the hand-off Task 1 chose (`in-session` for `yes`, `separate terminal` for `no`):

```bash
git add skills/graft/references/rules.md skills/graft/references/capture.md skills/graft/references/digest.md skills/graft/references/harden.md tests/unit/test_graft_references.py
git commit -m "feat(skill): the step references, cited by heading and checked against copying the guide (capture hand-off: in-session)"
```

---

### Task 6: The version-bump check, its workflow, and `just skill-version`

**Files:**
- Create: `scripts/check-skill-version.sh` (executable), `.github/workflows/skill-version.yml`
- Modify: `justfile` (new recipe after `test-unit`), `.github/workflows/python-quality.yml:25` (`python:`) (the paths filter list, which ends at `- 'uv.lock'` on line 31)
- Test: `tests/unit/test_skill_version_script.py`

**Interfaces:**
- Consumes: the two manifests (Task 2); `REPO_ROOT` from `tests/unit/guide_harness.py` (the project-tools plan, Task 9), so the repository root has one definition across the test modules.
- Produces: `scripts/check-skill-version.sh BASE`, which compares HEAD against the merge base of BASE and HEAD: exit 0 when nothing under `skills/` or `.claude-plugin/` changed between the merge base and HEAD (`git diff --name-only BASE...HEAD`), or when `plugin.json`'s version differs from the merge base's (or the merge base has no `plugin.json`); exit 1 otherwise, naming the next patch version. A base branch that moved on after the branch point never reads as this branch's downgrade. `just skill-version [BASE]`, defaulting to `origin/main`. Task 7's `CONTRIBUTING.md` section describes both.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_skill_version_script.py`:

```python
"""scripts/check-skill-version.sh against throwaway git repositories
(graft skill spec, 2026-09-21, "Versioning and release")."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.guide_harness import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "check-skill-version.sh"
BASH = shutil.which("bash")
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(BASH is None or GIT is None, reason="needs bash and git")


def _git_env(home: Path) -> dict[str, str]:
    """No user or system git config: no hooks path, no signing, a fixed identity."""
    return {
        **os.environ,
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "alice",
        "GIT_AUTHOR_EMAIL": "alice@example.com",
        "GIT_COMMITTER_NAME": "alice",
        "GIT_COMMITTER_EMAIL": "alice@example.com",
    }


class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.env = _git_env(root.parent)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        assert GIT is not None
        return subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [GIT, *args], cwd=self.root, env=self.env, capture_output=True, text=True, check=True
        ).stdout.strip()

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def manifests(self, version: str) -> None:
        """The two manifests as Task 2 writes them: the version in plugin.json alone."""
        self.write(
            ".claude-plugin/marketplace.json",
            json.dumps({"name": "graftpunk", "plugins": [{"name": "graftpunk"}]}, indent=2),
        )
        self.write(
            ".claude-plugin/plugin.json",
            json.dumps({"name": "graftpunk", "version": version}, indent=2),
        )

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def check(self, base: str) -> subprocess.CompletedProcess[str]:
        assert BASH is not None
        return subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [BASH, str(SCRIPT), base], cwd=self.root, env=self.env, capture_output=True, text=True
        )


@pytest.fixture()
def repo(tmp_path: Path) -> _Repo:
    root = tmp_path / "repo"
    root.mkdir()
    return _Repo(root)


def test_a_skill_change_without_a_bump_fails_and_names_the_next_patch(repo: _Repo) -> None:
    repo.manifests("0.1.0")
    repo.write("skills/graft/SKILL.md", "first\n")
    base = repo.commit("base")
    repo.write("skills/graft/SKILL.md", "second\n")
    repo.commit("change without a bump")
    result = repo.check(base)
    assert result.returncode == 1
    assert "0.1.1" in result.stdout + result.stderr


def test_a_skill_change_with_a_bump_passes(repo: _Repo) -> None:
    repo.manifests("0.1.0")
    repo.write("skills/graft/SKILL.md", "first\n")
    base = repo.commit("base")
    repo.write("skills/graft/SKILL.md", "second\n")
    repo.manifests("0.1.1")
    repo.commit("change with a bump")
    assert repo.check(base).returncode == 0


def test_a_change_elsewhere_needs_no_bump(repo: _Repo) -> None:
    repo.manifests("0.1.0")
    base = repo.commit("base")
    repo.write("src/module.py", "x = 1\n")
    repo.commit("unrelated")
    assert repo.check(base).returncode == 0


def test_a_first_introduction_passes(repo: _Repo) -> None:
    repo.write("README.md", "before the skill\n")
    base = repo.commit("base")
    repo.manifests("0.1.0")
    repo.write("skills/graft/SKILL.md", "first\n")
    repo.commit("introduce the skill")
    assert repo.check(base).returncode == 0


def test_a_base_that_moved_on_after_the_branch_point_is_not_this_branchs_change(
    repo: _Repo,
) -> None:
    """The base bumped the skill after the branch point and the branch never touched
    skills/: compared against the merge base, the branch changed nothing there, and
    the base's newer version is not reported as this branch's downgrade."""
    repo.manifests("0.1.0")
    repo.write("skills/graft/SKILL.md", "first\n")
    repo.commit("branch point")
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("src/module.py", "x = 1\n")
    repo.commit("unrelated, on the branch")
    repo.git("checkout", "-q", "main")
    repo.write("skills/graft/SKILL.md", "second\n")
    repo.manifests("0.1.1")
    base = repo.commit("the base bumps the skill")
    repo.git("checkout", "-q", "feature")
    result = repo.check(base)
    assert result.returncode == 0, result.stderr
    assert "no bump needed" in result.stdout
    assert "0.1.1" not in result.stdout + result.stderr


def test_the_script_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_skill_version_script.py -q`
Expected: FAIL (the script does not exist; `bash` exits 127).

- [ ] **Step 3: Write the script**

Create `scripts/check-skill-version.sh`:

```bash
#!/usr/bin/env bash
# check-skill-version.sh BASE
#
# When this branch's changes since it left BASE touch skills/ or .claude-plugin/,
# the version field of .claude-plugin/plugin.json must differ from the merge
# base's. That version pins an installed plugin: Claude Code keeps a GitHub install
# on that string, so a skill change without a bump never reaches people who
# already installed it. Comparing against the merge base, not BASE's tip, keeps
# a base branch that moved on after the branch point out of this branch's result.
# Run by .github/workflows/skill-version.yml on pull requests, and by hand as
# `just skill-version`.
set -euo pipefail

base="${1:?usage: scripts/check-skill-version.sh <base commit>}"
plugin=".claude-plugin/plugin.json"

version_of() {
  python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])'
}

# Separate steps, so a bad BASE fails here under set -e instead of reading as
# "no change".
merge_base="$(git merge-base "$base" HEAD)"
all_changed="$(git diff --name-only "$base"...HEAD)"
changed="$(printf '%s\n' "$all_changed" | grep -E '^(skills|\.claude-plugin)/' || true)"
if [ -z "$changed" ]; then
  echo "skill-version: nothing under skills/ or .claude-plugin/ changed; no bump needed."
  exit 0
fi

head_plugin="$(version_of < "$plugin")"
base_json="$(git show "$merge_base:$plugin" 2>/dev/null || true)"
if [ -z "$base_json" ]; then
  echo "skill-version: $plugin is new at $head_plugin."
  exit 0
fi
base_version="$(printf '%s' "$base_json" | version_of)"
if [ "$head_plugin" = "$base_version" ]; then
  next="$(python3 -c 'import sys; p = sys.argv[1].split("."); p[-1] = str(int(p[-1]) + 1); print(".".join(p))' "$base_version")"
  echo "skill-version: skills/ or .claude-plugin/ changed, but the version is still $base_version. Bump $plugin to $next." >&2
  exit 1
fi
echo "skill-version: $base_version -> $head_plugin"
```

Make it executable:

```bash
chmod +x scripts/check-skill-version.sh
```

- [ ] **Step 4: Add the workflow, the recipe, and the quality workflow's paths**

Create `.github/workflows/skill-version.yml`:

```yaml
name: Skill version

on:
  pull_request:
    branches: [main]

jobs:
  filter-paths:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: read
    outputs:
      skill_changed: ${{ steps.filter.outputs.skill }}
    steps:
      - uses: actions/checkout@v4
      - uses: dorny/paths-filter@v3
        id: filter
        with:
          filters: |
            skill:
              - 'skills/**'
              - '.claude-plugin/**'

  skill-version:
    needs: filter-paths
    if: needs.filter-paths.outputs.skill_changed == 'true'
    name: Skill version bumped
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: The plugin version bumped
        env:
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
        run: scripts/check-skill-version.sh "$BASE_SHA"
```

The workflow runs on every pull request to `main` and follows `.github/workflows/python-quality.yml`'s pattern (`.github/workflows/python-quality.yml:21` (`dorny/paths-filter@v3`)): a `filter-paths` job and a conditional job. The workflow itself has no `paths:` filter, so its jobs exist on every pull request, and the check job is skipped when nothing under `skills/` or `.claude-plugin/` changed.

In `justfile`, add after the `test-unit` recipe:

```just
# Check the skill's version bump against a base commit (CI runs the same check)
skill-version BASE="origin/main":
    scripts/check-skill-version.sh {{BASE}}
```

In `.github/workflows/python-quality.yml`, add these five lines to the `python:` filter list (after `- 'uv.lock'`), so the tests that pin a non-Python file run when only that file changes: the skill's tests, the guide's tests, the version script's tests, and the test that holds `CONTRIBUTING.md`'s update procedure to preflight's:

```yaml
              - 'skills/**'
              - '.claude-plugin/**'
              - 'docs/PLUGIN_DEVELOPMENT.md'
              - 'scripts/**'
              - 'CONTRIBUTING.md'
```

- [ ] **Step 5: Run the tests and the recipe to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_skill_version_script.py -q`
Expected: PASS.

Run: `just skill-version main`
Expected: exit 0 and `skill-version: .claude-plugin/plugin.json is new at 0.1.0.` (this branch introduces the manifests).

- [ ] **Step 6: Commit**

```bash
git add scripts/check-skill-version.sh .github/workflows/skill-version.yml .github/workflows/python-quality.yml justfile tests/unit/test_skill_version_script.py
git commit -m "ci(skill): a skill change must bump the plugin version, checked against the merge base"
```

---

### Task 7: The guide, README, CONTRIBUTING, CHANGELOG, the full gate, and the dry run

**Files:**
- Modify: `docs/PLUGIN_DEVELOPMENT.md` (new section "With the skill" between "The six steps at a glance" and "Frame")
- Modify: `README.md` (the Plugins section, after the paragraph that links the guide)
- Modify: `CONTRIBUTING.md` (new section "Releasing the skill" after "Pull Request Process")
- Modify: `CHANGELOG.md` (`[Unreleased]` Added)
- Modify: `tests/unit/test_graft_preflight.py` (one test: CONTRIBUTING gives the same skill update as preflight)

**Interfaces:**
- Consumes: everything above.
- Produces: the documentation the spec's "Documentation changes" names for the skill pull request.

- [ ] **Step 1: Add "With the skill" to the guide**

In `docs/PLUGIN_DEVELOPMENT.md`, insert before `## Frame`:

```markdown
## With the skill

graftpunk's repository is also a Claude Code plugin marketplace with one skill.
Install it with `/plugin marketplace add stavxyz/graftpunk` and
`/plugin install graftpunk@graftpunk`. Run
`/graftpunk:graft myshop https://myshop.example/` in an empty directory to create
a plugin, or `/graftpunk:graft` inside a plugin project to add commands to it.
The skill needs graftpunk 1.17.0 or later and `uv` on your PATH.
```

The section holds install and invocation facts and nothing else: the skill's copy check leaves it out of the guide text it compares against, so anything else written here would escape that check.

- [ ] **Step 2: Point the README at "With the skill"**

In `README.md`, "## Plugins", after the paragraph that ends "and tests.", insert one paragraph that links the guide's section instead of repeating its install commands:

```markdown
In Claude Code, the `/graftpunk:graft` skill walks that guide with you and runs the `gp` commands itself; **[With the skill](docs/PLUGIN_DEVELOPMENT.md#with-the-skill)** says how to install and run it.
```

- [ ] **Step 3: Write the failing test that ties CONTRIBUTING to preflight's update line**

The update procedure lives in `preflight.sh`'s `SKILL_UPDATE_LINE` and in CONTRIBUTING, and nowhere else. Append to `tests/unit/test_graft_preflight.py`:

```python
def test_contributing_gives_the_same_skill_update_as_preflight() -> None:
    """CONTRIBUTING's "Releasing the skill" and preflight's SKILL_UPDATE_LINE tell a
    user the same update: the same commands and the same Installed-tab wording."""
    line = _constant("SKILL_UPDATE_LINE")
    contributing = " ".join(
        (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8").replace("`", "").split()
    )
    for phrase in (
        "/plugin marketplace update graftpunk",
        "update graftpunk on the Installed tab of /plugin",
        "claude plugin update graftpunk@graftpunk",
    ):
        assert phrase in line, phrase
        assert phrase in contributing, phrase
```

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_preflight.py::test_contributing_gives_the_same_skill_update_as_preflight -q`
Expected: FAIL (`AssertionError: /plugin marketplace update graftpunk`; CONTRIBUTING has no such section yet).

- [ ] **Step 4: Add "Releasing the skill" to CONTRIBUTING**

In `CONTRIBUTING.md`, insert before `## Reporting Issues`:

```markdown
## Releasing the skill

The Claude Code skill under `skills/` is versioned apart from the graftpunk
package. Any change under `skills/` or `.claude-plugin/` needs a patch bump of
the `version` field in `.claude-plugin/plugin.json`; a new skill or a changed
invocation contract is a minor bump. That field pins an installed plugin:
setting it "pins the plugin to that version until you change it"
([plugins reference](https://code.claude.com/docs/en/plugins-reference), the
`version` field), so people who installed the skill from GitHub receive a
change only when it moves. Three kinds of install are not pinned by it: a
plugin with a `command` source, a plugin from a marketplace hosted on
claude.ai, and a plugin loaded in place from a marketplace added from a local
path. `.claude-plugin/marketplace.json` carries no root `version`: that field
is the marketplace manifest's own version
([marketplace reference](https://code.claude.com/docs/en/plugins/marketplace-reference),
top-level fields), and nothing here needs it.

The `Skill version` workflow checks this on every pull request that touches
those paths, comparing against the commit the branch started from (the merge
base). Run the same check before pushing with `just skill-version`, which
compares against `origin/main`, or name another base with
`just skill-version <commit>`. After a merge, users update with
`/plugin marketplace update graftpunk`, then update graftpunk on the Installed
tab of `/plugin` (or run `claude plugin update graftpunk@graftpunk` in a shell).
`skills/graft/scripts/preflight.sh` prints the same procedure when the skill is
older than the installed graftpunk, and a test holds the two to the same words.
```

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_preflight.py::test_contributing_gives_the_same_skill_update_as_preflight -q`
Expected: PASS.

- [ ] **Step 5: Add the CHANGELOG line**

Append to `CHANGELOG.md` under `[Unreleased]` / `### Added`. The 1.17.0 release this plan waits for moves the package's lines out of `[Unreleased]`, so the section may be empty now: if `[Unreleased]` has no `### Added` heading, create one directly under `[Unreleased]` and put the line there.

```markdown
- **The `/graftpunk:graft` Claude Code skill.** The repository is now a Claude Code plugin marketplace: `/plugin marketplace add stavxyz/graftpunk`, then `/plugin install graftpunk@graftpunk`. `/graftpunk:graft myshop https://myshop.example/` in an empty directory creates a plugin by walking `docs/PLUGIN_DEVELOPMENT.md` (frame, capture, understand, scaffold, implement, harden, a live check, and the publish checklist), running the `gp` commands itself (each subject to your permission settings; the skill offers allow rules for a prompt-free run) and handing you the browser recording and the live login; `/graftpunk:graft` inside a plugin project adds commands to it. The skill is versioned apart from the package (0.1.0) and needs graftpunk 1.17.0 or later and `uv`.
```

- [ ] **Step 6: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: every test passes (the skill's copy check now also runs against the new guide section), ruff reports no findings and no files to reformat, ty reports no errors.

- [ ] **Step 7: Scan the diff for banned punctuation and names**

Run: `git diff origin/main...HEAD | perl -CSD -ne 'print "$.: $_" if /^\+.*([\x{2013}\x{2014}]| \x2d\x2d )/'`
Expected: no output.

- [ ] **Step 8: Commit**

```bash
git add docs/PLUGIN_DEVELOPMENT.md README.md CONTRIBUTING.md CHANGELOG.md tests/unit/test_graft_preflight.py
git commit -m "docs(skill): install and release notes for the graft skill"
```

- [ ] **Step 9 [manual]: The dry run, recorded on the pull request**

With the branch's skill installed from a local path (`/plugin marketplace add <path to this worktree>`, then `/plugin install graftpunk@graftpunk`), run in an empty scratch directory. A local-directory marketplace loads the plugin in place, so the version does not pin this install.

1. `/graftpunk:graft myshop https://example.com/`. Where no live capture is available, stand in for the capture step with this repository's synthetic recording, filed as a run the scaffold step can read: `mkdir -p ~/.local/share/graftpunk/observe/example/dry-run && cp tests/fixtures/sample.har ~/.local/share/graftpunk/observe/example/dry-run/network.har`, then tell the skill the recording is done. Carry the flow through understand, scaffold, implement, and harden until the project's gate is green; skip the live check and say so in the notes.
2. In the resulting project, `/graftpunk:graft` with no arguments, adding one command.

Paste into the pull request's test plan every `gp` command the skill ran and the output of each gate run, and note any step where the skill deviated from `SKILL.md`. This is the one check no unit test can make. Remove the stand-in run afterwards with `gp observe clean example --force`.
