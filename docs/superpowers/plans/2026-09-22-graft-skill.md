# The graft Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the third of the three pull requests the graft skill design orders: a Claude Code plugin marketplace inside the graftpunk repository with one skill, `graftpunk:graft`, which walks a developer through the plugin guide and runs the `gp` commands itself, plus the tests, the version-bump check, and the documentation that make it maintainable.

**Architecture:** `.claude-plugin/marketplace.json` and `.claude-plugin/plugin.json` make the repository root a plugin (`source: "./"`), and `skills/graft/` holds the skill: `SKILL.md` (frontmatter and the flow), five references read one step at a time (`commands.md`, `rules.md`, `capture.md`, `digest.md`, `harden.md`), and `scripts/preflight.sh`, the one version handshake. Everything the skill knows about a plugin project comes from the package (`gp version --json --at-least --contract`, `gp plugin info --json`, `gp observe digest --endpoints-json`, and the scaffold commands); the skill's own tests pin its prose to the guide's headings, forbid copying the guide, hold the frontmatter's pre-approval to preflight alone and every offered allow rule to a command the skill itself runs, and run preflight against real and fake `gp` executables. The tests take the guide helpers from `tests/unit/guide_harness.py` (the project-tools plan, Task 9) and import no other test module. A CI job and a `just` recipe enforce the skill's independent version.

**Permissions, decided (primary source: https://code.claude.com/docs/en/skills):** `allowed-tools` grants "Tools Claude can use without asking permission during the turn that invokes this skill. The grant clears when you send your next message." The frontmatter therefore pre-approves only preflight, the one command the invoking turn reliably runs. `references/commands.md` stays the one declared list of the commands each step runs and gains the allow rules the skill offers for a prompt-free run; the skill offers them in its first message and never adds them itself. The live login and the first live read are asked for in words at the kick-the-tires step, whatever the user's settings allow.

**Tech Stack:** Markdown with YAML frontmatter, bash, JSON manifests, pytest (with `pyyaml`, already a dependency), GitHub Actions, `just`.

**Spec:** `docs/superpowers/specs/2026-09-21-graft-skill-design.md` (validated 2026-09-22). Read it alongside this plan. This plan runs only after `docs/superpowers/plans/2026-09-22-graft-package-foundations.md` and `docs/superpowers/plans/2026-09-22-graft-package-project-tools.md` have merged and shipped in one graftpunk release; it consumes their CLI surfaces exactly as their Interfaces blocks state them.

**Precondition, checked by Task 3's first test:** the graftpunk installed in this checkout's environment reports a version at least `SKILL_REQUIRES_GRAFTPUNK` (`1.17.0`, the release that ships the two package pull requests). The skill must never merge ahead of the package it needs.

## Global Constraints

- The placement rule in `src/graftpunk/devtools/__init__.py` is untouched: this pull request adds no Python under `src/`. `graftpunk.testing` still imports nothing from `graftpunk.devtools`.
- The skill copies nothing from the guide: no run of eight or more consecutive words from `SKILL.md` or a reference appears in `docs/PLUGIN_DEVELOPMENT.md`, outside a quotation that ends with a citation of a guide heading. It cites only headings that exist. Tests enforce both.
- The skill carries no schema number except `SKILL_READS_INFO_SCHEMA` and `SKILL_READS_ENDPOINTS_SCHEMA` at the top of `preflight.sh`, and no copy of the name rule, the reserved names, the gate, or the publish checklist.
- Any change under `skills/` or `.claude-plugin/` bumps the `version` field of both manifests, equal to each other. This pull request introduces them at `0.1.0`.
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

1. A PATH with no `gp` on it but a system binary of the same name elsewhere (PARI/GP installs one called `gp`): the missing-`gp` test must not depend on the host's `/usr/bin`, so it builds its own PATH from a tools directory. Pinned in Task 3 (`tests/unit/test_graft_preflight.py::test_gp_missing_exits_2_with_the_install_line`).
2. `gp plugin info --json` refusing to read the directory (a malformed `pyproject.toml`): a person expects preflight to stop with gp's own message rather than print half a JSON object. Pinned in Task 3 (`test_a_project_gp_cannot_read_exits_4_with_gps_message`).
3. An offered allow rule that widens consent by accident, such as `Bash(gp *)` or `Bash(gp observe *)`: the second would let Claude run `gp observe interactive`, which the skill never runs. Pinned in Task 4 (`test_no_offered_rule_reaches_the_recorder_or_every_gp_command`).
4. A reference that quotes a guide sentence to be accurate: a blockquote line is exempt from the copy check only when it ends with a citation of a heading that exists. Pinned in Task 5 (`test_a_quotation_is_exempt_only_with_a_citation`).
5. `just skill-version` run on a branch whose base has no manifests yet (this pull request itself): a first introduction has no base version to differ from, and must pass when the two new versions agree. Pinned in Task 6 (`test_a_first_introduction_passes_when_the_versions_agree`).

## File Structure

| File | Responsibility |
| --- | --- |
| `.claude-plugin/marketplace.json` (new, Task 2) | The marketplace: name `graftpunk`, one plugin, `source: "./"`. |
| `.claude-plugin/plugin.json` (new, Task 2) | The plugin: name, description, version, author. |
| `skills/graft/scripts/preflight.sh` (new, Task 3) | `gp` present and new enough, the one version handshake, and `gp plugin info --json` relayed. |
| `skills/graft/SKILL.md` (new, Task 4) | Frontmatter and the flow. |
| `skills/graft/references/commands.md` (new, Task 4) | The declared commands each step runs, and the allow rules the skill offers for them; the referent of the consent tests. |
| `skills/graft/references/rules.md`, `capture.md`, `digest.md`, `harden.md` (new, Task 5) | What each step needs, citing the guide by heading and pointing to `commands.md` for every command. |
| `tests/unit/test_graft_skill.py` (new, Tasks 2, 4, 5) | The skill's own tests: manifests, frontmatter and consent, citations, and the copy check. |
| `tests/unit/test_graft_preflight.py` (new, Task 3) | Preflight against the real `gp` and a fake one. |
| `scripts/check-skill-version.sh` (new, Task 6) | The version-bump check. |
| `.github/workflows/skill-version.yml` (new, Task 6) | Runs the check on pull requests. |
| `.github/workflows/python-quality.yml` (modify, Task 6) | Runs the unit suite when only the skill or the guide changes. |
| `justfile` (modify, Task 6) | `just skill-version`. |
| `tests/unit/test_skill_version_script.py` (new, Task 6) | The check against throwaway git repositories. |
| `docs/PLUGIN_DEVELOPMENT.md`, `README.md`, `CONTRIBUTING.md`, `CHANGELOG.md` (modify, Task 7) | "With the skill", the install lines, "Releasing the skill", one Added line. |

---

### Task 1 [manual]: Probe whether Ctrl+C in Claude Code's `!` shell mode reaches `gp observe interactive`

The spec marks this UNVERIFIED: that a Ctrl+C typed while a `!` command runs in a Claude Code session reaches `gp` as SIGINT, so the recorder's handler runs and the HAR is saved. The interactive-mode documentation (https://code.claude.com/docs/en/interactive-mode) lists Ctrl+C as "Interrupt, or clear input", and its shell-mode section does not say whether Ctrl+C reaches a running `!` command. The outcome decides one wording choice in Task 5 (`capture.md`) and nothing else; every other task is independent of it. A person runs this task on a workstation with a display, because the recorder opens a real browser.

The spec's former second UNVERIFIED claim, that an unchanged version string leaves users on the content they have, needs no probe (the spec now cites the source): the plugins reference (https://code.claude.com/docs/en/plugins-reference, the `version` field) says setting a version "pins the plugin to that version string, so users only receive updates when you bump it" (the sentence goes on to exempt a `command` source and a plugin loaded in place from a local-directory marketplace; neither applies to a GitHub install). The version rule in Task 6 rests on that sentence.

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
- Test: `tests/unit/test_graft_skill.py`

**Interfaces:**
- Consumes: nothing.
- Consumes: `REPO_ROOT` from `tests/unit/guide_harness.py` (the project-tools plan, Task 9).
- Produces: the marketplace `graftpunk` serving plugin `graftpunk` at version `0.1.0` from `./`, so the install lines are `/plugin marketplace add stavxyz/graftpunk` and `/plugin install graftpunk@graftpunk`. `tests/unit/test_graft_skill.py` with `SKILL_DIR`, `MARKETPLACE`, `PLUGIN_MANIFEST`, and `_json`, which Tasks 4 and 5 extend.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_graft_skill.py`:

```python
"""The graft skill's own tests (graft skill spec, 2026-09-21, "Testing").

Nothing here runs Claude Code. The skill is checked as files: its manifests, its
frontmatter's one pre-approved command and the allow rules it offers against
the declared list, and its prose against the guide it cites. Preflight has its
own module, tests/unit/test_graft_preflight.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.unit.guide_harness import REPO_ROOT

MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"
SKILL_DIR = REPO_ROOT / "skills" / "graft"


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class TestManifests:
    def test_the_two_versions_are_equal(self) -> None:
        """plugin.json's version is the one that pins an installed plugin (plugins
        reference, the version field); marketplace.json's root version is the
        marketplace manifest's own (plugin marketplaces, root fields). This
        repository keeps them in lockstep so one number names a release."""
        assert _json(MARKETPLACE)["version"] == _json(PLUGIN_MANIFEST)["version"]

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

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_skill.py -q`
Expected: FAIL (`FileNotFoundError: ... .claude-plugin/marketplace.json`).

- [ ] **Step 3: Write the manifests**

Create `.claude-plugin/marketplace.json`:

```json
{
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

The manifest carries no `$schema` key. The marketplace documentation says of `$schema` that "Claude Code ignores this field at load time" (https://code.claude.com/docs/en/plugin-marketplaces), and `https://www.anthropic.com/claude-code/marketplace.schema.json` returned HTTP 404 when checked on 2026-09-22.

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

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_skill.py -q`
Expected: PASS.

- [ ] **Step 5: Confirm the sdist stays an allowlist**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_sdist_contents.py -q`
Expected: PASS. `.claude-plugin/` and `skills/` are outside `[tool.hatch.build.targets.sdist].only-include`, so the source distribution is unchanged.

- [ ] **Step 6: Commit**

```bash
git add .claude-plugin/marketplace.json .claude-plugin/plugin.json tests/unit/test_graft_skill.py
git commit -m "feat(skill): the repository is a Claude Code plugin marketplace serving graftpunk"
```

---

### Task 3: `preflight.sh`, the one version handshake

**Files:**
- Create: `skills/graft/scripts/preflight.sh` (executable)
- Test: `tests/unit/test_graft_preflight.py`

**Interfaces:**
- Consumes: `gp version --json --at-least VERSION --contract SURFACE=N ...` from the foundations plan, Task 5 (prints one line of JSON first, then exits 0; 1 when the installed version is below VERSION, VERSION is unreadable, or a `--contract` value is malformed, with gp's message on stderr for the latter two; 3 when a named surface differs, one line per mismatch on stderr naming the older side; 2 for an option gp does not know); `gp plugin info --json` from the project-tools plan, Task 4; `REPO_ROOT` from `tests/unit/guide_harness.py` (the project-tools plan, Task 9).
- Produces: `skills/graft/scripts/preflight.sh` with `SKILL_REQUIRES_GRAFTPUNK="1.17.0"`, `SKILL_READS_INFO_SCHEMA=1`, and `SKILL_READS_ENDPOINTS_SCHEMA=1` at the top; it passes all three to gp in one `gp version` call, parses no JSON, and compares nothing itself. On success it prints `{"installation": <gp version --json>, "project": <gp plugin info --json>}` and exits 0; otherwise one message on stderr and exit 2 (no `gp`), 3 (`gp version` exited 1, whose message names all three readings: graftpunk below the floor, a floor gp cannot read, or a malformed `--contract` value; or gp reports a contract mismatch, with gp's own lines relayed and the fix for each side), 4 (`gp` could not read the project), 5 (`gp` rejected an option preflight passed), or 6 (`gp version` exited with any other status, relayed with gp's output). Task 4's `SKILL.md` runs it first.

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
# The only programs preflight may use besides bash builtins and gp. The PATH the
# fake-gp tests build holds nothing else, so a preflight that reached for a JSON
# or text tool would fail there; the real-gp tests also put the environment's own
# bin directory on the PATH, so they do not prove it.
_TOOLS = ("mktemp", "cat", "rm")
_REAL_GP_DIR = Path(sys.executable).parent
_VERSION_OK = '{"contracts": {"endpoints": 1, "info": 1}, "graftpunk": "9.9.9"}'
_INFO_EMPTY = '{"directory": "empty", "plugins": [], "schema": 1}'
_OLDER_CALLER = (
    "info: this graftpunk writes schema 2 and the caller reads 1; "
    "the caller is older than graftpunk."
)


def _constant(name: str) -> str:
    match = re.search(rf'^{name}="?([^"\n]+)"?$', PREFLIGHT.read_text(), re.M)
    assert match, f"preflight.sh declares no {name}"
    return match.group(1)


def _tools(tmp_path: Path) -> Path:
    """A directory holding only the programs preflight needs, so no host binary called
    gp can stand in for the one a test means."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in _TOOLS:
        found = shutil.which(name)
        assert found, name
        (tools / name).symlink_to(found)
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

    def test_gp_missing_exits_2_with_the_install_line(self, dirs: tuple[Path, Path, Path]) -> None:
        work, home, tools = dirs
        result = _preflight(work, home, tools)
        assert result.returncode == 2
        assert "uv tool install graftpunk" in result.stderr
        assert result.stdout == ""

    def test_a_floor_not_met_exits_3_with_gps_message_and_the_upgrade_line(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        fake = _fake_gp(tmp_path, version_exit=1, version_message="--at-least: below the floor")
        result = _preflight(work, home, fake, tools)
        assert result.returncode == 3
        assert "--at-least: below the floor" in result.stderr
        assert "uv tool upgrade graftpunk" in result.stderr
        for reading in ("older than", "version floor", "--contract value"):
            assert reading in result.stderr, reading

    def test_a_contract_mismatch_exits_3_relaying_gp_and_the_fix_for_each_side(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        fake = _fake_gp(tmp_path, version_exit=3, version_message=_OLDER_CALLER)
        result = _preflight(work, home, fake, tools)
        assert result.returncode == 3
        assert _OLDER_CALLER in result.stderr
        assert "/plugin marketplace update graftpunk" in result.stderr
        assert "uv tool upgrade graftpunk" in result.stderr
        assert result.stdout == ""

    def test_a_rejected_version_option_exits_5_naming_both_readings(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        result = _preflight(work, home, _fake_gp(tmp_path, version_exit=2), tools)
        assert result.returncode == 5
        assert "older than" in result.stderr
        assert "misspelled flag" in result.stderr
        assert "No such option: --contract" in result.stderr
        assert "uv tool upgrade graftpunk" in result.stderr

    def test_a_rejected_info_option_exits_5(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        result = _preflight(work, home, _fake_gp(tmp_path, info_exit=2), tools)
        assert result.returncode == 5

    def test_an_unexpected_version_status_exits_6_not_4(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        """Exit 4 means the project could not be read; a gp version crash is another
        failure and gets its own code."""
        work, home, tools = dirs
        fake = _fake_gp(tmp_path, version_exit=70, version_message="Traceback: boom")
        result = _preflight(work, home, fake, tools)
        assert result.returncode == 6
        assert "exited 70" in result.stderr
        assert "Traceback: boom" in result.stderr

    def test_a_project_gp_cannot_read_exits_4_with_gps_message(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        fake = _fake_gp(tmp_path, info_json="pyproject.toml: not valid TOML", info_exit=1)
        result = _preflight(work, home, fake, tools)
        assert result.returncode == 4
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
# and exits 0. Otherwise prints one message on stderr and exits:
#   2  gp is not on PATH
#   3  gp version exited 1 (graftpunk older than this skill needs, or gp could
#      not read the floor or a --contract value this script passed), or gp
#      reports a contract mismatch (gp's own lines name the older side)
#   4  gp could not read the project in this directory
#   5  gp rejected an option this script passed
#   6  gp version exited with a status this script does not expect
# It compares nothing and parses no JSON: gp answers by its exit status, and the
# JSON is relayed exactly as gp printed it.
set -u

SKILL_REQUIRES_GRAFTPUNK="1.17.0"
SKILL_READS_INFO_SCHEMA=1
SKILL_READS_ENDPOINTS_SCHEMA=1

INSTALL_LINE="uv tool install graftpunk   (or: pip install graftpunk)"
UPGRADE_LINE="uv tool upgrade graftpunk   (or: pip install --upgrade graftpunk)"
SKILL_UPDATE_LINE="/plugin marketplace update graftpunk, then /plugin update graftpunk@graftpunk"

if ! command -v gp >/dev/null 2>&1; then
  printf 'graftpunk is not installed: gp is not on PATH.\nInstall it: %s\n' "$INSTALL_LINE" >&2
  exit 2
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
  exit 5
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
    exit 3
    ;;
  2) option_rejected "gp version --json --at-least --contract" ;;
  3)
    printf 'graftpunk and this skill read a payload at different schemas:\n' >&2
    cat "$errfile" >&2
    printf 'When graftpunk is the older side, upgrade it: %s\n' "$UPGRADE_LINE" >&2
    printf 'When this skill is the older side, update it: %s\n' "$SKILL_UPDATE_LINE" >&2
    exit 3
    ;;
  *)
    printf 'gp version exited %s, which preflight does not expect:\n%s\n' "$status" "$installation" >&2
    cat "$errfile" >&2
    exit 6
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
    exit 4
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
- Test: `tests/unit/test_graft_skill.py`

**Interfaces:**
- Consumes: `preflight.sh` (Task 3); `check_invocation`, `gp_invocations`, and `blocks` from `tests/unit/guide_harness.py` (the project-tools plan, Task 9, which moved them out of the guide's test module).
- Produces: the skill, invoked as `/graftpunk:graft [plugin-name] [site-url]`, whose frontmatter pre-approves preflight alone; `references/commands.md` with the declared commands per step and the offered allow rules; `_skill_docs()`, `COMMANDS_MD`, and `_declared_commands()` in the test module, which Task 5's tests use.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_graft_skill.py`, add `import os`, `import re`, `import shlex`, `import pytest`, and `import yaml` to the imports, extend the harness import to `from tests.unit.guide_harness import REPO_ROOT, blocks, check_invocation, gp_invocations`, and append:

```python
SKILL_MD = SKILL_DIR / "SKILL.md"
COMMANDS_MD = SKILL_DIR / "references" / "commands.md"
_SKILL_DIR_VAR = "${CLAUDE_SKILL_DIR}/"
# The whole pre-approved list. Claude Code keeps an allowed-tools grant only for
# the turn that invokes the skill (https://code.claude.com/docs/en/skills), and
# preflight is the one command that turn reliably runs.
_PREFLIGHT_ENTRY = "${CLAUDE_SKILL_DIR}/scripts/preflight.sh *"
# The one offered rule scoped to the plugin being built: the skill puts the
# plugin's name in place of <name> when it offers the rules.
_PLUGIN_RULE = "gp <name> *"
# What the kick-the-tires step asks before the first live call, whatever the
# user's settings allow: the consent point for the live site and the login.
_LIVE_CALL_QUESTION = "run a live login and one read-only command now?"


def _skill_docs() -> list[Path]:
    return [SKILL_MD, *sorted((SKILL_DIR / "references").glob("*.md"))]


def _frontmatter() -> dict[str, Any]:
    text = SKILL_MD.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n", 2)[1])


def _allowed() -> list[str]:
    return re.findall(r"Bash\(([^)]*)\)", _frontmatter()["allowed-tools"])


def _commands_in(text: str) -> list[str]:
    return [
        line.strip()
        for _start, body in blocks(text, "bash")
        for line in body.splitlines()
        if line.strip()
    ]


def _section(heading: str) -> str:
    """commands.md from the line *heading* to the next heading of the same level."""
    text = COMMANDS_MD.read_text(encoding="utf-8")
    start = text.index(f"\n{heading}\n")
    level = heading.split(" ", 1)[0] + " "
    end = text.find(f"\n{level}", start + 1)
    return text[start : end if end != -1 else len(text)]


def _declared_commands() -> list[str]:
    """Every line of every fenced bash block in commands.md: the commands the steps run,
    whoever runs them."""
    return _commands_in(COMMANDS_MD.read_text(encoding="utf-8"))


def _skill_run_commands() -> list[str]:
    """The commands under "Run by the skill": the only ones an offered rule may cover."""
    return _commands_in(_section("## Run by the skill"))


def _preflight_commands() -> list[str]:
    return _commands_in(_section("## Run by preflight"))


def _offered_rules() -> list[str]:
    """The patterns of the allow rules commands.md offers: its fenced text block under
    "Allow rules for a prompt-free run", one Bash(<pattern>) rule per line."""
    text = COMMANDS_MD.read_text(encoding="utf-8")
    section = text[text.index("## Allow rules for a prompt-free run") :]
    (_start, body), *_rest = blocks(section, "text")
    rules = [line.strip() for line in body.splitlines() if line.strip()]
    assert rules and all(re.fullmatch(r"Bash\([^()]+\)", rule) for rule in rules), rules
    return [rule.removeprefix("Bash(").removesuffix(")") for rule in rules]


def _matches(pattern: str, command: str) -> bool:
    """Claude Code's Bash permission rule: a trailing " *" matches the prefix with or
    without arguments; anything else matches exactly
    (https://code.claude.com/docs/en/permissions, wildcard rules).

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
        """Never Bash(*), never a bare Bash(gp *), never a command other than gp."""
        for pattern in _offered_rules():
            assert pattern.startswith("gp "), pattern
            assert pattern not in ("*", "gp *"), pattern
            assert "*" not in pattern.removesuffix(" *"), pattern

    def test_no_offered_rule_reaches_the_recorder_or_every_gp_command(self) -> None:
        for probe in (
            "gp observe --no-session interactive <url>",
            "gp observe -s <session> interactive <url>",
            "gp anything",
        ):
            assert not any(_matches(pattern, probe) for pattern in _offered_rules()), probe

    def test_the_plugin_rule_is_offered_and_scoped_to_the_plugin(self) -> None:
        rules = _offered_rules()
        assert _PLUGIN_RULE in rules
        assert [rule for rule in rules if rule.startswith("gp <")] == [_PLUGIN_RULE]


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
    for doc in _skill_docs()
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
    tokens = shlex.split(invocation)[1:]
    if tokens and tokens[0].startswith("<"):
        pytest.skip(f"{invocation} addresses the plugin's own command group")
    check_invocation(invocation, f"{doc}:{line_no}")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_skill.py -q`
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

`references/commands.md` lists every command the steps run and, under "Allow
rules for a prompt-free run", the permission rules this skill offers for them.
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

Your first message after preflight offers the allow rules: show the lines under
"Allow rules for a prompt-free run" in `references/commands.md`, with `<name>`
replaced by the plugin's name once you know it, and say that the user can add
them to this project's settings with `/permissions` for a run without prompts.
Say that the skill works either way: without the rules, each command asks first.

In create mode, `$0` is the plugin name and `$1` the site URL. Ask for whichever
is missing, one question at a time. In enhance mode, ignore both and say so in
one line; the plugin comes from `project.plugins`. With one entry, take it. With
several (a suite), take the entry whose `site_name` is `$0` when there is one,
and otherwise ask which.

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
2. **Capture** (guide: Capture). Read `references/capture.md` and hand the
   recording to the user exactly as it says. You never run the recorder
   yourself. When the user says the recording is done, run `gp observe list`,
   take the newest run under the inferred name, and say that name back.
3. **Understand** (guide: Understand). Run the command in the Understand block
   of `references/commands.md`, then read
   `references/digest.md` and build the proposal it describes. The user keeps,
   renames, or drops rows in one answer.
4. **Scaffold** (guide: Scaffold). Run the `gp plugin new` line of the Scaffold
   block in `references/commands.md`, with one `--command` per row the user kept
   (its `--run` form pins an older run). The generator writes only those stubs, under those names, each
   with its endpoint declared. Edit nothing it wrote during this step. Then run
   `gp plugin info --json` and confirm every agreed command is listed with the
   endpoint it was agreed for.
5. **Implement** (guide: Implement). For each stub, fill in the request, name
   the parameters, decide the return shape, and replace every `GP-FILL` marker.
   Raise `CommandError` or `PluginError` on failure. Read `references/rules.md`
   and read the guide section behind any rule the work touches.
6. **Harden** (guide: Harden). Read `references/harden.md` and follow it: one
   fixture and one test per command, then the project's gate. When
   `gp plugin check` reports missing project wiring, run `gp plugin upgrade`
   and run the gate again. The gate must pass before the next step.
7. **Kick the tires** (guide: Check the CLI surface you shipped) (guide: Login).
   Before the first live call, ask in words, whatever the user's settings
   allow: "run a live login and one read-only command now?" Their answer is
   the consent for the live site and the login; an allow rule does not stand in
   for it. On yes, run the help line of the Kick the tires block in
   `references/commands.md` and confirm every agreed command name is listed,
   then its login line and one read-only command against the live site while
   the user watches. Credentials come from the
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
with these differences, and this list is the only place they are stated:

- Frame collects only the new thing the user wants to do. The plugin name,
  `site_name`, and `base_url` come from `project.plugins`.
- Capture first runs `gp session list`. When it lists a session for the
  plugin, the recording uses `-s <session>`; otherwise `--no-session`.
- Understand leaves out every endpoint an existing command declares, and prints
  each one it left out beside that command's name and declared endpoint, so a
  declaration that went stale after a hand edit shows up. Every remaining row
  is marked new. Each existing command reported with `endpoint: null` gets a
  row of its own reading "existing command, endpoint not declared", so the user
  can say whether a proposed row duplicates it. Never drop or propose over an
  undeclared command silently.
- Scaffold does not run `gp plugin new`. For each agreed command it runs the
  `gp plugin add-command` line of the Scaffold block in `references/commands.md`,
  which adds one stub in the generated shape and prints the fixture path its
  test will look for.
- The publish checklist is limited to the items the new commands touch.

## Secrets

Never ask for a password, never write a credential anywhere, and never print
what `gp config get --resolve` returns. Never read a value that came off the
account: not a cookie or token value, not a HAR body, not a capture. From a
recording, read the `--endpoints-json` projection and nothing else. When the
user pastes a secret into the conversation, say where it belongs
(`gp config set NAME '$(your-secret-tool read ...)'`) and do not use it.
````

- [ ] **Step 4: Write `commands.md`**

Create `skills/graft/references/commands.md`:

````markdown
# The commands each step runs

The declared list of every command a step runs, one fenced block per step.
`SKILL.md` and the other references point to a block here by its step's name
and never spell a templated command themselves; this file is the one place the
commands are written. The placeholders are `<name>` (the plugin), `<command>`
(a command's name, as agreed in the proposal), `<session>` (the recording's
name), `<run-id>`, `<url>`, `<version>`, `<n>`, `<METHOD>`, and `<template>`.

Only preflight is pre-approved (`SKILL.md`, "Permissions"). Every other command
the skill runs asks the user's permission unless their settings allow it. The
rules at the end of this file are the ones the skill offers for that, drawn
from "Run by the skill" alone, and the skill's tests hold each of them to a
command listed there.

## Run by preflight

`preflight.sh` runs these itself, inside the one pre-approved call; the skill
never runs them directly, so no rule is offered for them.

```bash
gp version --json --at-least <version> --contract info=<n> --contract endpoints=<n>
gp plugin info --json
```

## Run by the user

The recorder needs a person at the keyboard. The skill prints one of these for
the user to run, and never runs it.

```bash
gp observe --no-session interactive <url>
gp observe -s <session> interactive <url>
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
gp observe digest <session> --endpoints-json
```

### Scaffold

The last line reads back the commands the step added.

```bash
gp plugin new <name> --from-run <session> --command "<command>=<METHOD> <template>"
gp plugin new <name> --from-run <session> --run <run-id> --command "<command>=<METHOD> <template>"
gp plugin add-command <name> --from-run <session> --command "<command>=<METHOD> <template>"
gp plugin info --json
```

### Harden

```bash
gp observe fixtures <session> --match "<METHOD> <template>"
gp plugin check
gp plugin upgrade
```

The project's gate is whatever the guide's "The gate" section lists; it includes
`gp plugin check`.

### Kick the tires

These touch the live site or a credential. The skill asks in words before the
first of them runs, whatever the user's settings allow.

```bash
gp <name> --help
gp <name> login
```

## Allow rules for a prompt-free run

The permission rules the skill offers in its first message, one per line in the
settings syntax, with `<name>` replaced by the plugin's name. The user adds them
to the project's settings with `/permissions`; the skill never adds them. The
last rule covers the plugin's own commands, its login included.

```text
Bash(gp plugin info *)
Bash(gp session list *)
Bash(gp observe list *)
Bash(gp observe digest *)
Bash(gp observe fixtures *)
Bash(gp plugin new *)
Bash(gp plugin add-command *)
Bash(gp plugin upgrade *)
Bash(gp plugin check *)
Bash(gp <name> *)
```
````

- [ ] **Step 5: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_skill.py -q`
Expected: PASS. The walker test resolves every `gp` invocation in `SKILL.md` and `commands.md` against the installed CLI, the interactive recorder and the `gp version --contract` handshake included.

- [ ] **Step 6: Commit**

```bash
git add skills/graft/SKILL.md skills/graft/references/commands.md tests/unit/test_graft_skill.py
git commit -m "feat(skill): SKILL.md pre-approves preflight alone, and commands.md declares the commands and the allow rules offered for them"
```

---

### Task 5: The references, and the tests that pin them to the guide

**Files:**
- Create: `skills/graft/references/rules.md`, `capture.md`, `digest.md`, `harden.md`
- Test: `tests/unit/test_graft_skill.py`

The slug helpers the citation tests use are `slug` and `slugs_of` in `tests/unit/guide_harness.py`. The project-tools plan's Task 9 moved `_slugs_of` there from `tests/unit/test_plugin_development_guide.py:317-337` and extracted `slug` from it, so this task edits no guide test.

**Interfaces:**
- Consumes: `CTRL_C_REACHES_GP` (Task 1); `_skill_docs`, `COMMANDS_MD`, and `_declared_commands` (Task 4); `GUIDE`, `GUIDE_TEXT`, `blocks`, `slug`, and `slugs_of` from `tests/unit/guide_harness.py` (the project-tools plan, Task 9).
- Produces: the four references `SKILL.md` names. The copy detector (`_words`, `_runs`, `_prose`) stays in `tests/unit/test_graft_skill.py`, its one user. `capture.md` and `harden.md` carry no fenced command blocks: they point to the step's block in `commands.md`, the one owner of the commands.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_graft_skill.py`, extend the harness import with `GUIDE`, `GUIDE_TEXT`, `slug`, and `slugs_of`, and append:

```python
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
    copy check compares. A quotation is a blockquote line ending with a citation."""
    kept: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or (line.startswith("> ") and _QUOTE_CITATION_RE.search(line)):
            continue
        kept.append(line)
    return _CITATION_RE.sub(" ", "\n".join(kept))


def _without_section(text: str, heading: str) -> str:
    """*text* without the section under the line *heading*, up to the next heading of
    the same level; *text* itself when it has no such line."""
    lines = text.splitlines()
    if heading not in lines:
        return text
    start = lines.index(heading)
    level = heading.split(" ", 1)[0] + " "
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith(level)), len(lines))
    return "\n".join(lines[:start] + lines[end:])


# The guide's "With the skill" section describes this skill, and holds only its
# install and invocation facts, which the skill may state in the same words;
# every other section is the guide's to state and the skill's to cite.
GUIDE_RUNS = _runs(_words(_without_section(GUIDE_TEXT, "## With the skill")))


class TestCitations:
    @pytest.mark.parametrize("doc", _skill_docs(), ids=lambda p: p.name)
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
    @pytest.mark.parametrize("doc", _skill_docs(), ids=lambda p: p.name)
    def test_no_run_of_eight_words_from_the_guide(self, doc: Path) -> None:
        copied = sorted(
            " ".join(run) for run in _runs(_words(_prose(doc.read_text()))) & GUIDE_RUNS
        )
        assert copied == [], f"{doc.name} copies the guide: {copied[:3]}"

    def test_the_detector_catches_a_copied_sentence(self) -> None:
        sentence = "The recording is only as good as what you clicked."
        assert sentence in GUIDE_TEXT
        assert _runs(_words(_prose(sentence))) & GUIDE_RUNS

    def test_a_quotation_is_exempt_only_with_a_citation(self) -> None:
        sentence = "The recording is only as good as what you clicked."
        assert not _runs(_words(_prose(f"> {sentence} (guide: Capture)"))) & GUIDE_RUNS
        assert _runs(_words(_prose(f"> {sentence}"))) & GUIDE_RUNS

    @pytest.mark.parametrize("doc", _skill_docs(), ids=lambda p: p.name)
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


_TEMPLATED_GP_SPAN_RE = re.compile(r"`!? ?(gp [^`]*<[^`]*)`")


def _outside_fences(text: str) -> str:
    kept: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            kept.append(line)
    return "\n".join(kept)


@pytest.mark.parametrize("doc", _skill_docs(), ids=lambda p: p.name)
def test_no_templated_command_is_spelled_in_prose(doc: Path) -> None:
    """One owner for the commands, in prose as in fenced blocks: a backticked gp
    command with a <placeholder> outside commands.md is a second spelling, so the
    prose names the commands.md block instead."""
    if doc == COMMANDS_MD:
        pytest.skip("commands.md is the declaration")
    spelled = _TEMPLATED_GP_SPAN_RE.findall(_outside_fences(doc.read_text(encoding="utf-8")))
    assert spelled == [], f"{doc.name} spells a command commands.md owns: {spelled}"


@pytest.mark.parametrize("doc", _skill_docs(), ids=lambda p: p.name)
def test_a_templated_command_in_a_fenced_block_is_declared_in_commands_md(doc: Path) -> None:
    """One owner for the commands: a reference points to commands.md instead of
    repeating a command, so a templated gp line in any other file's fenced block
    must be one commands.md declares. A worked example with real values (no
    <placeholder>) is not a declaration and is exempt."""
    if doc == COMMANDS_MD:
        pytest.skip("commands.md is the declaration")
    declared = set(_declared_commands())
    for _start, body in blocks(doc.read_text(encoding="utf-8"), "bash"):
        for line in body.splitlines():
            command = line.strip()
            if command.startswith("gp ") and "<" in command:
                assert command in declared, f"{doc.name} repeats an undeclared command: {command}"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_skill.py tests/unit/test_plugin_development_guide.py -q`
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

Print the recorder line from the "Run by the user" block of `references/commands.md`,
with the site's URL in place of `<url>`: the `--no-session` line in create mode,
and in enhance mode when `gp session list` shows no session for the plugin; the
`-s <session>` line in enhance mode when a session exists, so the recording
starts already logged in.

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

When the user says they are done, run `gp observe list`, take the newest run
under the name graftpunk inferred from the host, and repeat that name back
before the understand step uses it. A want that the digest later shows no
endpoint for gets a second, narrower recording aimed at that one flow.
````

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
   `content_type`), the endpoint as `<METHOD> <template>`, and the parameters.
   Say which of the user's wants each row serves, and name any want with no row.
5. Ask one question, "keep, rename, or drop any of these?", and apply the
   answer. A want with no endpoint goes back to the capture step for that flow.

What request call a stub makes (`request_json` or `request_text`, and its role)
is the generator's decision; the table reports `content_type` and `shape` and
never predicts the call.

The `login` summary tells a plain form from a redirect: `forms` holds the
field selectors a plain form needs, and `auth_urls` entries of kind `redirect`,
or on another host, point to an identity provider. Confirm the login shape with
the user only when that summary leaves it open.

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
| orders | a page of orders with a total | GET /api/orders | archived, page, per_page |
| order | one order with its items | GET /api/orders/{order_id} | order_id |

`/login` and `/session` are gone by rule 1, and `/dashboard` is left out as
chrome unless the user asked for something only it shows. The two kept rows
reach the scaffold step as:

```bash
gp plugin new myshop --from-run myshop --command "orders=GET /api/orders" --command "order=GET /api/orders/{order_id}"
```
````

- [ ] **Step 6: Write `harden.md`**

Create `skills/graft/references/harden.md`:

````markdown
# Fixtures, tests, the gate, and the checklist

## A fixture per command

For each command, write its capture out of the recording with the
`gp observe fixtures` line from the Harden block of `references/commands.md`,
the command's endpoint in place of `<METHOD> <template>`. That writes the
response and its `.meta.json` sidecar under `tests/captures/`, which is
gitignored. Copy both files, under the same names, into the fixtures directory
the command's test reads: `gp plugin new` listed it under its `Next:` line, and
`gp plugin add-command` printed it after adding the stub. Then edit the
copied response and invent every value in it while keeping its structure, as
the guide's recipe says (guide: Deriving a fixture from a capture). Leave the
copied sidecar alone.

The generated suite checks the result on every run: a fixture with no sidecar,
a fixture that is still byte for byte its capture, or a flagged cookie or token
name left in a fixture fails the suite. A passing check does not mean the
invented values are good ones; read the fixture once more before moving on.

## A test per command

Each generated test builds a context with `fixture_context` over the plugin's
fixtures directory and calls the command. Replace its `GP-FILL` assertion with
one on the shape the command returns: the keys a caller relies on, and the
values you invented. Add one error-path test where a command can fail, by
copying a fixture and setting its sidecar's `status` to an error.

## The gate

Read the guide's section on the gate (guide: The gate) and run every command it
lists, in the project's directory, until all of them pass. One of them is
`gp plugin check`; when it says the project is missing wiring, run
`gp plugin upgrade` and then the whole gate again.

## Before you publish

Read the guide's publish checklist (guide: Before you publish) and walk its
items in order. Keep no copy of that list here or in the conversation; the
guide is the one place it lives.
````

- [ ] **Step 7: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_skill.py tests/unit/test_plugin_development_guide.py -q`
Expected: PASS. If `test_no_run_of_eight_words_from_the_guide` names a run, reword that sentence of the reference in your own words (do not edit the guide to make it pass), and run again.

- [ ] **Step 8: Commit**

The message names the hand-off Task 1 chose (`in-session` for `yes`, `separate terminal` for `no`):

```bash
git add skills/graft/references/rules.md skills/graft/references/capture.md skills/graft/references/digest.md skills/graft/references/harden.md tests/unit/test_graft_skill.py
git commit -m "feat(skill): the step references, cited by heading and checked against copying the guide (capture hand-off: in-session)"
```

---

### Task 6: The version-bump check, its workflow, and `just skill-version`

**Files:**
- Create: `scripts/check-skill-version.sh` (executable), `.github/workflows/skill-version.yml`
- Modify: `justfile` (new recipe after `test-unit`), `.github/workflows/python-quality.yml:25-31` (paths filter)
- Test: `tests/unit/test_skill_version_script.py`

**Interfaces:**
- Consumes: the two manifests (Task 2); `REPO_ROOT` from `tests/unit/guide_harness.py` (the project-tools plan, Task 9), so the repository root has one definition across the test modules.
- Produces: `scripts/check-skill-version.sh BASE`, exit 0 when nothing under `skills/` or `.claude-plugin/` changed between BASE and HEAD, or when both manifest versions are equal and differ from BASE's (or BASE has no manifest); exit 1 otherwise, naming the next patch version. `just skill-version [BASE]`, defaulting to `origin/main`. Task 7's `CONTRIBUTING.md` section describes both.

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

    def manifests(self, marketplace: str, plugin: str) -> None:
        self.write(
            ".claude-plugin/marketplace.json",
            json.dumps({"name": "graftpunk", "version": marketplace}, indent=2),
        )
        self.write(
            ".claude-plugin/plugin.json",
            json.dumps({"name": "graftpunk", "version": plugin}, indent=2),
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
    repo.manifests("0.1.0", "0.1.0")
    repo.write("skills/graft/SKILL.md", "first\n")
    base = repo.commit("base")
    repo.write("skills/graft/SKILL.md", "second\n")
    repo.commit("change without a bump")
    result = repo.check(base)
    assert result.returncode == 1
    assert "0.1.1" in result.stdout + result.stderr


def test_a_skill_change_with_both_bumped_passes(repo: _Repo) -> None:
    repo.manifests("0.1.0", "0.1.0")
    repo.write("skills/graft/SKILL.md", "first\n")
    base = repo.commit("base")
    repo.write("skills/graft/SKILL.md", "second\n")
    repo.manifests("0.1.1", "0.1.1")
    repo.commit("change with a bump")
    assert repo.check(base).returncode == 0


def test_versions_that_disagree_fail(repo: _Repo) -> None:
    repo.manifests("0.1.0", "0.1.0")
    base = repo.commit("base")
    repo.manifests("0.1.1", "0.1.0")
    repo.commit("half a bump")
    result = repo.check(base)
    assert result.returncode == 1
    assert "differ" in result.stdout + result.stderr


def test_a_change_elsewhere_needs_no_bump(repo: _Repo) -> None:
    repo.manifests("0.1.0", "0.1.0")
    base = repo.commit("base")
    repo.write("src/module.py", "x = 1\n")
    repo.commit("unrelated")
    assert repo.check(base).returncode == 0


def test_a_first_introduction_passes_when_the_versions_agree(repo: _Repo) -> None:
    repo.write("README.md", "before the skill\n")
    base = repo.commit("base")
    repo.manifests("0.1.0", "0.1.0")
    repo.write("skills/graft/SKILL.md", "first\n")
    repo.commit("introduce the skill")
    assert repo.check(base).returncode == 0


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
# When the diff from BASE to HEAD touches skills/ or .claude-plugin/, the version
# field of .claude-plugin/plugin.json and .claude-plugin/marketplace.json must be
# equal to each other and different from BASE's. plugin.json's version is the one
# that pins an installed plugin: Claude Code keeps a GitHub install on that string,
# so a skill change without a bump never reaches people who already installed it
# (a local-directory marketplace loads the plugin in place and is not pinned).
# marketplace.json's root version is the marketplace manifest's own, and moves in
# lockstep so one number names a release. Run by .github/workflows/skill-version.yml
# on pull requests, and by hand as `just skill-version`.
set -euo pipefail

base="${1:?usage: scripts/check-skill-version.sh <base commit>}"
marketplace=".claude-plugin/marketplace.json"
plugin=".claude-plugin/plugin.json"

version_of() {
  python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])'
}

# Two steps, so a bad BASE fails here under set -e instead of reading as "no change".
all_changed="$(git diff --name-only "$base" HEAD)"
changed="$(printf '%s\n' "$all_changed" | grep -E '^(skills|\.claude-plugin)/' || true)"
if [ -z "$changed" ]; then
  echo "skill-version: nothing under skills/ or .claude-plugin/ changed; no bump needed."
  exit 0
fi

head_marketplace="$(version_of < "$marketplace")"
head_plugin="$(version_of < "$plugin")"
if [ "$head_marketplace" != "$head_plugin" ]; then
  echo "skill-version: the two versions differ: $marketplace has $head_marketplace, $plugin has $head_plugin. Set both to the same version." >&2
  exit 1
fi

base_json="$(git show "$base:$plugin" 2>/dev/null || true)"
if [ -z "$base_json" ]; then
  echo "skill-version: $plugin is new at $head_plugin."
  exit 0
fi
base_version="$(printf '%s' "$base_json" | version_of)"
if [ "$head_plugin" = "$base_version" ]; then
  next="$(python3 -c 'import sys; p = sys.argv[1].split("."); p[-1] = str(int(p[-1]) + 1); print(".".join(p))' "$base_version")"
  echo "skill-version: skills/ or .claude-plugin/ changed, but the version is still $base_version. Bump both manifests to $next." >&2
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
    paths:
      - "skills/**"
      - ".claude-plugin/**"

jobs:
  skill-version:
    name: Skill version bumped
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Both manifest versions bumped together
        env:
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
        run: scripts/check-skill-version.sh "$BASE_SHA"
```

In `justfile`, add after the `test-unit` recipe:

```just
# Check the skill's version bump against a base commit (CI runs the same check)
skill-version BASE="origin/main":
    scripts/check-skill-version.sh {{BASE}}
```

In `.github/workflows/python-quality.yml`, add these three lines to the `python:` filter list (after `- 'uv.lock'`), so the skill's tests and the guide's tests run when only the skill or the guide changes:

```yaml
              - 'skills/**'
              - '.claude-plugin/**'
              - 'docs/PLUGIN_DEVELOPMENT.md'
```

- [ ] **Step 5: Run the tests and the recipe to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_skill_version_script.py -q`
Expected: PASS.

Run: `just skill-version main`
Expected: exit 0 and `skill-version: .claude-plugin/plugin.json is new at 0.1.0.` (this branch introduces the manifests).

- [ ] **Step 6: Commit**

```bash
git add scripts/check-skill-version.sh .github/workflows/skill-version.yml .github/workflows/python-quality.yml justfile tests/unit/test_skill_version_script.py
git commit -m "ci(skill): a skill change must bump both manifest versions together"
```

---

### Task 7: The guide, README, CONTRIBUTING, CHANGELOG, the full gate, and the dry run

**Files:**
- Modify: `docs/PLUGIN_DEVELOPMENT.md` (new section "With the skill" between "The six steps at a glance" and "Frame")
- Modify: `README.md` (the Plugins section, after the paragraph that links the guide)
- Modify: `CONTRIBUTING.md` (new section "Releasing the skill" after "Pull Request Process")
- Modify: `CHANGELOG.md` (`[Unreleased]` Added)

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
```

The section holds install and invocation facts and nothing else: the skill's copy check leaves it out of the guide text it compares against, so anything else written here would escape that check.

- [ ] **Step 2: Add the install lines to the README**

In `README.md`, "## Plugins", after the paragraph that ends "and tests.", insert:

````markdown
In Claude Code, the `/graftpunk:graft` skill walks that guide with you and runs the `gp` commands itself. Install it with:

```text
/plugin marketplace add stavxyz/graftpunk
/plugin install graftpunk@graftpunk
```
````

- [ ] **Step 3: Add "Releasing the skill" to CONTRIBUTING**

In `CONTRIBUTING.md`, insert before `## Reporting Issues`:

```markdown
## Releasing the skill

The Claude Code skill under `skills/` is versioned apart from the graftpunk
package. Any change under `skills/` or `.claude-plugin/` needs a patch bump of
the `version` field in both `.claude-plugin/plugin.json` and
`.claude-plugin/marketplace.json`, and the two stay equal; a new skill or a
changed invocation contract is a minor bump. The version in `plugin.json` is the
one that pins an installed plugin: Claude Code keeps a GitHub install on that
string, so people who installed the skill receive a change only when it moves
([plugins reference](https://code.claude.com/docs/en/plugins-reference), the
`version` field; a plugin loaded in place from a local-directory marketplace is
not pinned). The root `version` in `marketplace.json` is the marketplace
manifest's own
([plugin marketplaces](https://code.claude.com/docs/en/plugin-marketplaces),
root fields); it moves with the plugin's so one number names a release.

The `Skill version` workflow checks this on every pull request that touches
those paths. Run the same check before pushing with `just skill-version`, which
compares against `origin/main`, or name another base with
`just skill-version <commit>`. After a merge, users update with
`/plugin marketplace update graftpunk` and `/plugin update graftpunk@graftpunk`.
```

- [ ] **Step 4: Add the CHANGELOG line**

Append to `CHANGELOG.md` under `[Unreleased]` / `### Added`:

```markdown
- **The `/graftpunk:graft` Claude Code skill.** The repository is now a Claude Code plugin marketplace: `/plugin marketplace add stavxyz/graftpunk`, then `/plugin install graftpunk@graftpunk`. `/graftpunk:graft myshop https://myshop.example/` in an empty directory creates a plugin by walking `docs/PLUGIN_DEVELOPMENT.md` (frame, capture, understand, scaffold, implement, harden, a live check, and the publish checklist), running the `gp` commands itself (each subject to your permission settings; the skill offers allow rules for a prompt-free run) and handing you the browser recording and the live login; `/graftpunk:graft` inside a plugin project adds commands to it. The skill is versioned apart from the package (0.1.0) and needs graftpunk 1.17.0 or later.
```

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: every test passes (the skill's copy check now also runs against the new guide section), ruff reports no findings and no files to reformat, ty reports no errors.

- [ ] **Step 6: Scan the diff for banned punctuation and names**

Run: `git diff main | perl -CSD -ne 'print "$.: $_" if /^\+.*([\x{2013}\x{2014}]| \x2d\x2d )/'`
Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add docs/PLUGIN_DEVELOPMENT.md README.md CONTRIBUTING.md CHANGELOG.md
git commit -m "docs(skill): install and release notes for the graft skill"
```

- [ ] **Step 8 [manual]: The dry run, recorded on the pull request**

With the branch's skill installed from a local path (`/plugin marketplace add <path to this worktree>`, then `/plugin install graftpunk@graftpunk`), run in an empty scratch directory. A local-directory marketplace loads the plugin in place, so the version does not pin this install.

1. `/graftpunk:graft myshop https://example.com/`. Where no live capture is available, stand in for the capture step with this repository's synthetic recording, filed as a run the scaffold step can read: `mkdir -p ~/.local/share/graftpunk/observe/example/dry-run && cp tests/fixtures/sample.har ~/.local/share/graftpunk/observe/example/dry-run/network.har`, then tell the skill the recording is done. Carry the flow through understand, scaffold, implement, and harden until the project's gate is green; skip the live check and say so in the notes.
2. In the resulting project, `/graftpunk:graft` with no arguments, adding one command.

Paste into the pull request's test plan every `gp` command the skill ran and the output of each gate run, and note any step where the skill deviated from `SKILL.md`. This is the one check no unit test can make. Remove the stand-in run afterwards with `gp observe clean example --force`.
