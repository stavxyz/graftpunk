# The graft Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the third of the three pull requests the graft skill design orders: a Claude Code plugin marketplace inside the graftpunk repository with one skill, `graftpunk:graft`, which walks a developer through the plugin guide and runs the `gp` commands itself, plus the tests, the version-bump check, and the documentation that make it maintainable.

**Architecture:** `.claude-plugin/marketplace.json` and `.claude-plugin/plugin.json` make the repository root a plugin (`source: "./"`), and `skills/graft/` holds the skill: `SKILL.md` (frontmatter and the flow), five references read one step at a time (`commands.md`, `rules.md`, `capture.md`, `digest.md`, `harden.md`), and `scripts/preflight.sh`, the one version handshake. Everything the skill knows about a plugin project comes from the package (`gp version --json`, `gp plugin info --json`, `gp observe digest --endpoints-json`, and the scaffold commands); the skill's own tests pin its prose to the guide's headings, forbid copying the guide, bind the pre-approved command list to a declared file, and run preflight against real and fake `gp` executables. A CI job and a `just` recipe enforce the skill's independent version.

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
- The full gate, green at the end of every task and run in full by the last task: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
- A single test runs as `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/<file>.py::<test> -q`.

## Review Focus

1. A PATH with no `gp` on it but a system binary of the same name elsewhere (PARI/GP installs one called `gp`): the missing-`gp` test must not depend on the host's `/usr/bin`, so it builds its own PATH from a tools directory. Pinned in Task 3 (`test_gp_missing_exits_2_with_the_install_line`).
2. `gp plugin info --json` refusing to read the directory (a malformed `pyproject.toml`): a person expects preflight to stop with gp's own message rather than print half a JSON object. Pinned in Task 3 (`test_a_project_gp_cannot_read_exits_4_with_gps_message`).
3. A frontmatter entry that widens consent by accident, such as `Bash(gp *)` or `Bash(gp observe *)`: the second would pre-approve `gp observe interactive`, which the skill never runs. Pinned in Task 4 (`test_no_entry_reaches_the_recorder_or_every_gp_command`).
4. A reference that quotes a guide sentence to be accurate: a blockquote line is exempt from the copy check only when it ends with a citation of a heading that exists. Pinned in Task 5 (`test_a_quotation_is_exempt_only_with_a_citation`).
5. `just skill-version` run on a branch whose base has no manifests yet (this pull request itself): a first introduction has no base version to differ from, and must pass when the two new versions agree. Pinned in Task 6 (`test_a_first_introduction_passes_when_the_versions_agree`).

## File Structure

| File | Responsibility |
| --- | --- |
| `.claude-plugin/marketplace.json` (new, Task 2) | The marketplace: name `graftpunk`, one plugin, `source: "./"`. |
| `.claude-plugin/plugin.json` (new, Task 2) | The plugin: name, description, version, author. |
| `skills/graft/scripts/preflight.sh` (new, Task 3) | `gp` present and new enough, the one version handshake, and `gp plugin info --json` relayed. |
| `skills/graft/SKILL.md` (new, Task 4) | Frontmatter and the flow. |
| `skills/graft/references/commands.md` (new, Task 4) | The declared commands each step runs; the referent of the consent test. |
| `skills/graft/references/rules.md`, `capture.md`, `digest.md`, `harden.md` (new, Task 5) | What each step needs, citing the guide by heading. |
| `tests/unit/test_graft_skill.py` (new, Tasks 2 to 5) | The skill's own tests. |
| `tests/unit/test_plugin_development_guide.py` (modify, Task 5) | `_slug` extracted from `_slugs_of` for reuse. |
| `scripts/check-skill-version.sh` (new, Task 6) | The version-bump check. |
| `.github/workflows/skill-version.yml` (new, Task 6) | Runs the check on pull requests. |
| `.github/workflows/python-quality.yml` (modify, Task 6) | Runs the unit suite when only the skill or the guide changes. |
| `justfile` (modify, Task 6) | `just skill-version`. |
| `tests/unit/test_skill_version_script.py` (new, Task 6) | The check against throwaway git repositories. |
| `docs/PLUGIN_DEVELOPMENT.md`, `README.md`, `CONTRIBUTING.md`, `CHANGELOG.md` (modify, Task 7) | "With the skill", the install lines, "Releasing the skill", one Added line. |

---

### Task 1 [manual]: Probe whether Ctrl+C in Claude Code's `!` shell mode reaches `gp observe interactive`

The spec marks this UNVERIFIED: that a Ctrl+C typed while a `!` command runs in a Claude Code session reaches `gp` as SIGINT, so the recorder's handler runs and the HAR is saved. The interactive-mode documentation (https://code.claude.com/docs/en/interactive-mode) describes Ctrl+C only as interrupting the running operation. The outcome decides one wording choice in Task 5 (`capture.md`) and nothing else; every other task is independent of it. A person runs this task on a workstation with a display, because the recorder opens a real browser.

The spec's second UNVERIFIED platform claim, that an unchanged version string can leave stale plugin files in place, needs no probe: the plugins reference (https://code.claude.com/docs/en/plugins-reference, the `version` field) says setting a version "pins the plugin to that version string, so users only receive updates when you bump it". The version rule in Task 6 rests on that sentence.

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
- Produces: the marketplace `graftpunk` serving plugin `graftpunk` at version `0.1.0` from `./`, so the install lines are `/plugin marketplace add stavxyz/graftpunk` and `/plugin install graftpunk@graftpunk`. `tests/unit/test_graft_skill.py` with `SKILL_DIR`, `MARKETPLACE`, `PLUGIN_MANIFEST`, and `_json`, which Tasks 3 to 5 extend.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_graft_skill.py`:

```python
"""The graft skill's own tests (graft skill spec, 2026-09-21, "Testing").

Nothing here runs Claude Code. The skill is checked as files: its manifests, its
preflight script against real and fake gp executables, its frontmatter's
pre-approved commands against the declared list, and its prose against the
guide it cites.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.unit.test_plugin_development_guide import REPO_ROOT

MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"
SKILL_DIR = REPO_ROOT / "skills" / "graft"


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class TestManifests:
    def test_the_two_versions_are_equal(self) -> None:
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
- Test: `tests/unit/test_graft_skill.py`

**Interfaces:**
- Consumes: `gp version --json --at-least VERSION` (one-line JSON, sorted keys; exits 0, 1, or 2) from the foundations plan, Task 5; `gp plugin info --json` from the project-tools plan, Task 4; `contracts` holding `info` and `endpoints`.
- Produces: `skills/graft/scripts/preflight.sh` with `SKILL_REQUIRES_GRAFTPUNK="1.17.0"`, `SKILL_READS_INFO_SCHEMA=1`, and `SKILL_READS_ENDPOINTS_SCHEMA=1` at the top; on success it prints `{"installation": <gp version --json>, "project": <gp plugin info --json>}` and exits 0; otherwise one message on stderr and exit 2 (no `gp`), 3 (graftpunk too old, or a contracts number differs, naming which side is older), 4 (`gp` could not read the project), or 5 (`gp` rejected an option preflight passed). Task 4's `SKILL.md` runs it first.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_graft_skill.py`, add `import os`, `import re`, `import shutil`, `import subprocess`, `import sys`, `import pytest`, and `from packaging.version import Version`, and `import graftpunk` to the imports, and append:

```python
PREFLIGHT = SKILL_DIR / "scripts" / "preflight.sh"
BASH = shutil.which("bash")
# The programs preflight uses besides bash builtins and gp.
_TOOLS = ("sed", "mktemp", "cat", "rm")
_REAL_GP_DIR = Path(sys.executable).parent
_VERSION_OK = '{"contracts": {"endpoints": 1, "info": 1}, "graftpunk": "9.9.9"}'
_INFO_EMPTY = '{"directory": "empty", "plugins": [], "schema": 1}'


def _floor() -> str:
    match = re.search(r'^SKILL_REQUIRES_GRAFTPUNK="([^"]+)"$', PREFLIGHT.read_text(), re.M)
    assert match, "preflight.sh declares no SKILL_REQUIRES_GRAFTPUNK"
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
    info_json: str = _INFO_EMPTY,
    info_exit: int = 0,
) -> Path:
    """A gp that answers exactly the two calls preflight makes, as told."""
    fake = tmp_path / "fake"
    fake.mkdir()
    script = fake / "gp"
    script.write_text(
        f"""#!{BASH}
if [ "$1" = version ]; then
  if [ {version_exit} -eq 2 ]; then echo "No such option: --at-least" >&2; exit 2; fi
  printf '%s\\n' '{version_json}'
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
    assert Version(graftpunk.__version__) >= Version(_floor()), (
        f"graftpunk {graftpunk.__version__} is below the skill's floor {_floor()}"
    )


def test_preflight_is_executable() -> None:
    assert os.access(PREFLIGHT, os.X_OK)


@pytest.mark.skipif(BASH is None, reason="preflight is a bash script")
class TestPreflightWithTheRealGp:
    @pytest.mark.parametrize("kind", ["empty", "plugin", "foreign"])
    def test_both_answers_are_relayed_unchanged(
        self, dirs: tuple[Path, Path, Path], kind: str
    ) -> None:
        work, home, tools = dirs
        if kind == "plugin":
            _real_gp(work, home, "plugin", "new", "myshop", "--url", "https://myshop.example", "--dir", str(work))
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
    def test_gp_missing_exits_2_with_the_install_line(
        self, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        result = _preflight(work, home, tools)
        assert result.returncode == 2
        assert "uv tool install graftpunk" in result.stderr
        assert result.stdout == ""

    def test_a_floor_not_met_exits_3_with_the_upgrade_line(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        result = _preflight(work, home, _fake_gp(tmp_path, version_exit=1), tools)
        assert result.returncode == 3
        assert "uv tool upgrade graftpunk" in result.stderr

    def test_a_newer_contract_means_the_skill_is_older(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        newer = '{"contracts": {"endpoints": 1, "info": 2}, "graftpunk": "9.9.9"}'
        result = _preflight(work, home, _fake_gp(tmp_path, version_json=newer), tools)
        assert result.returncode == 3
        assert "this skill is older" in result.stderr
        assert "/plugin marketplace update graftpunk" in result.stderr

    @pytest.mark.parametrize(
        "installation",
        [
            '{"contracts": {"endpoints": 0, "info": 1}, "graftpunk": "9.9.9"}',
            '{"contracts": {"info": 1}, "graftpunk": "9.9.9"}',
        ],
    )
    def test_an_older_or_missing_contract_means_graftpunk_is_older(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path], installation: str
    ) -> None:
        work, home, tools = dirs
        result = _preflight(work, home, _fake_gp(tmp_path, version_json=installation), tools)
        assert result.returncode == 3
        assert "graftpunk is older than this skill" in result.stderr
        assert "uv tool upgrade graftpunk" in result.stderr

    def test_a_rejected_version_option_exits_5_naming_both_readings(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        result = _preflight(work, home, _fake_gp(tmp_path, version_exit=2), tools)
        assert result.returncode == 5
        assert "older than" in result.stderr
        assert "misspelled flag" in result.stderr
        assert "uv tool upgrade graftpunk" in result.stderr

    def test_a_rejected_info_option_exits_5(
        self, tmp_path: Path, dirs: tuple[Path, Path, Path]
    ) -> None:
        work, home, tools = dirs
        result = _preflight(work, home, _fake_gp(tmp_path, info_exit=2), tools)
        assert result.returncode == 5

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

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_skill.py -q`
Expected: FAIL (`FileNotFoundError` reading `skills/graft/scripts/preflight.sh`).

- [ ] **Step 3: Write the script**

Create `skills/graft/scripts/preflight.sh`:

```bash
#!/usr/bin/env bash
# Preflight for /graftpunk:graft. Checks that gp is installed and new enough,
# makes the skill's one version handshake against gp version --json, and relays
# gp plugin info --json. On success prints
#   {"installation": <gp version --json>, "project": <gp plugin info --json>}
# and exits 0. Otherwise prints one message on stderr and exits:
#   2  gp is not on PATH
#   3  graftpunk is older than this skill needs, or a contract number differs
#   4  gp could not read the project in this directory
#   5  gp rejected an option this script passed
# It never orders versions itself (gp answers --at-least) and compares schema
# numbers only for equality.
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

option_rejected() {
  printf 'gp rejected an option preflight passed (%s).\n' "$1" >&2
  printf 'Either the installed graftpunk is older than %s, or this skill passed a misspelled flag.\n' \
    "$SKILL_REQUIRES_GRAFTPUNK" >&2
  printf 'Upgrade first: %s\nIf it still fails after upgrading, the skill has a bug; report this message.\n' \
    "$UPGRADE_LINE" >&2
  cat "$errfile" >&2
  exit 5
}

installation="$(gp version --json --at-least "$SKILL_REQUIRES_GRAFTPUNK" 2>"$errfile")"
status=$?
case "$status" in
  0) ;;
  1)
    printf 'graftpunk is older than this skill needs (%s or later).\nUpgrade: %s\n' \
      "$SKILL_REQUIRES_GRAFTPUNK" "$UPGRADE_LINE" >&2
    exit 3
    ;;
  2) option_rejected "gp version --json --at-least" ;;
  *)
    printf 'gp version failed (exit %s):\n' "$status" >&2
    cat "$errfile" >&2
    exit 4
    ;;
esac

# The number gp version --json reports under "contracts" for surface $1, or
# nothing. gp prints that object on one line with sorted keys.
contract() {
  printf '%s' "$installation" | sed -n "s/.*\"$1\": \([0-9][0-9]*\).*/\1/p"
}

check_contract() {
  found="$(contract "$1")"
  if [ -z "$found" ] || [ "$found" -lt "$2" ]; then
    printf 'graftpunk is older than this skill: it reports %s schema %s, and this skill reads %s.\nUpgrade: %s\n' \
      "$1" "${found:-none}" "$2" "$UPGRADE_LINE" >&2
    exit 3
  fi
  if [ "$found" -gt "$2" ]; then
    printf 'this skill is older than graftpunk: graftpunk reports %s schema %s, and this skill reads %s.\nUpdate the skill: %s\n' \
      "$1" "$found" "$2" "$SKILL_UPDATE_LINE" >&2
    exit 3
  fi
}

check_contract info "$SKILL_READS_INFO_SCHEMA"
check_contract endpoints "$SKILL_READS_ENDPOINTS_SCHEMA"

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

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_skill.py -q`
Expected: PASS. If `test_the_installed_graftpunk_meets_the_skill_floor` fails, stop: the release the precondition names has not shipped into this environment (`uv sync` after pulling `main`), and nothing else in this plan should proceed.

- [ ] **Step 5: Commit**

```bash
git add skills/graft/scripts/preflight.sh tests/unit/test_graft_skill.py
git commit -m "feat(skill): preflight makes the one version handshake and relays the project"
```

---

### Task 4: `SKILL.md`, `commands.md`, and the consent test

**Files:**
- Create: `skills/graft/SKILL.md`, `skills/graft/references/commands.md`
- Test: `tests/unit/test_graft_skill.py`

**Interfaces:**
- Consumes: `preflight.sh` (Task 3); the CLI walker `_check_invocation`, `_gp_invocations`, and `_blocks` (existing, `tests/unit/test_plugin_development_guide.py:62`, `:97`, `:205`).
- Produces: the skill, invoked as `/graftpunk:graft [plugin-name] [site-url]`; `SKILL_DOCS` (every skill markdown file) and `_skill_docs()` in the test module, which Task 5's tests parametrize over.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_graft_skill.py`, add `import shlex` and `import yaml` to the imports, extend the guide-test import to `from tests.unit.test_plugin_development_guide import REPO_ROOT, _blocks, _check_invocation, _gp_invocations`, and append:

```python
SKILL_MD = SKILL_DIR / "SKILL.md"
COMMANDS_MD = SKILL_DIR / "references" / "commands.md"
_SKILL_DIR_VAR = "${CLAUDE_SKILL_DIR}/"
# The hand-maintained deny side: the login and the live-site commands stay off
# the pre-approved list, so their permission prompt is the user's go-ahead.
_DENIED = ("gp myshop login", "gp myshop orders", "gp myshop order --order-id 1001")


def _skill_docs() -> list[Path]:
    return [SKILL_MD, *sorted((SKILL_DIR / "references").glob("*.md"))]


def _frontmatter() -> dict[str, Any]:
    text = SKILL_MD.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n", 2)[1])


def _allowed() -> list[str]:
    return re.findall(r"Bash\(([^)]*)\)", _frontmatter()["allowed-tools"])


def _declared_commands() -> list[str]:
    """Every line of every fenced bash block in commands.md: the one declared referent."""
    return [
        line.strip()
        for _start, body in _blocks(COMMANDS_MD.read_text(encoding="utf-8"), "bash")
        for line in body.splitlines()
        if line.strip()
    ]


def _matches(pattern: str, command: str) -> bool:
    """Claude Code's Bash permission rule: a trailing " *" matches the prefix with or
    without arguments; anything else matches exactly
    (https://code.claude.com/docs/en/permissions, wildcard rules)."""
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

    def test_every_path_in_allowed_tools_exists(self) -> None:
        paths = [p for p in _allowed() if p.startswith(_SKILL_DIR_VAR)]
        assert paths
        for pattern in paths:
            target = SKILL_DIR / pattern.removeprefix(_SKILL_DIR_VAR).removesuffix(" *")
            assert target.is_file() and os.access(target, os.X_OK), pattern

    def test_every_entry_matches_a_declared_command(self) -> None:
        """Containment, in the direction that cannot widen consent on its own: a stale
        entry fails, and declaring a new command pulls nothing onto the list."""
        declared = _declared_commands()
        for pattern in _allowed():
            assert any(_matches(pattern, command) for command in declared), (
                f"{pattern} matches no command in references/commands.md"
            )

    @pytest.mark.parametrize("command", _DENIED)
    def test_the_login_and_the_live_site_stay_off_the_list(self, command: str) -> None:
        assert not any(_matches(pattern, command) for pattern in _allowed())

    def test_no_entry_reaches_the_recorder_or_every_gp_command(self) -> None:
        for probe in ("gp observe --no-session interactive https://myshop.example/", "gp anything"):
            assert not any(_matches(pattern, probe) for pattern in _allowed()), probe


SKILL_INVOCATIONS = [
    (doc.name, line_no, invocation)
    for doc in _skill_docs()
    for line_no, invocation in _gp_invocations(doc.read_text(encoding="utf-8"))
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
    _check_invocation(invocation, f"{doc}:{line_no}")
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
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/preflight.sh *) Bash(gp plugin info *) Bash(gp session list) Bash(gp observe list) Bash(gp observe digest *) Bash(gp observe fixtures *) Bash(gp plugin new *) Bash(gp plugin add-command *) Bash(gp plugin upgrade *) Bash(gp plugin check *)
---

# graft: build or extend a graftpunk site plugin

You are taking a developer through `docs/PLUGIN_DEVELOPMENT.md` in the graftpunk
repository, called "the guide" below. The guide is the reference and this skill
is a route through it: when the two disagree, the guide wins. Two modes share one
flow. Create mode starts from an empty directory and ends with a new plugin
project. Enhance mode starts inside an existing plugin project and adds commands
to it.

## What runs without a prompt

The `allowed-tools` list above is a boundary, declared by hand. A command belongs
on it when a step runs it unattended and it touches neither the live site nor a
credential. `gp <plugin> login` and every command that talks to the live site are
kept off it on purpose, so their permission prompt is the user's go-ahead for the
live check; the skill's test asserts that pair stays absent. Every command the
steps run is declared in `references/commands.md`, and each entry above has to
match one of them.

## Start with preflight

Run `${CLAUDE_SKILL_DIR}/scripts/preflight.sh` first, on every invocation. If it
exits non-zero, show its message verbatim and stop. On exit 0 it prints one JSON
object, `{"installation": ..., "project": ...}`, and `project.directory` picks the
mode: `empty` is create mode, `plugin` is enhance mode. For `foreign`, stop and
say: "this directory holds a project that is not a graftpunk plugin; run the
skill in an empty directory or in the plugin's project".

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

1. **Frame** (guide: Frame). Collect the plugin name and check it with
   `gp plugin new <name> --check-name`, which writes nothing: exit 0 means the
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
3. **Understand** (guide: Understand). Run
   `gp observe digest <session> --endpoints-json`, then read
   `references/digest.md` and build the proposal it describes. The user keeps,
   renames, or drops rows in one answer.
4. **Scaffold** (guide: Scaffold). Run
   `gp plugin new <name> --from-run <session> --command "<name>=<METHOD> <template>"`
   with one `--command` per row the user kept (add `--run <run-id>` to pin an
   older run). The generator writes only those stubs, under those names, each
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
   Ask for the go-ahead, then run `gp <name> --help` and confirm every agreed
   command name is listed. Then run `gp <name> login` and one read-only command
   against the live site while the user watches. Credentials come from the
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
- Scaffold does not run `gp plugin new`. For each agreed command it runs
  `gp plugin add-command <plugin> --from-run <session> --command "<name>=<METHOD> <template>"`,
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

The declared list of every command the steps run, one fenced block per step.
`SKILL.md` and the other references describe these commands in prose; this file
is the one place they are listed, and the skill's test checks every
`allowed-tools` entry in the frontmatter against these blocks. Adding a command
here does not pre-approve it: that takes an edit to the frontmatter too. The
placeholders are `<name>` (the plugin), `<session>` (the recording's name),
`<run-id>`, `<url>`, `<METHOD>`, and `<template>`.

## Preflight

Preflight runs `gp version --json` and `gp plugin info --json` itself. The
skill runs `gp plugin info --json` again after the scaffold step, to read back
the commands it added.

```bash
${CLAUDE_SKILL_DIR}/scripts/preflight.sh
gp plugin info --json
```

## Frame

```bash
gp plugin new <name> --check-name
```

## Capture

The user runs the recorder; the skill never does.

```bash
gp observe --no-session interactive <url>
gp observe -s <session> interactive <url>
```

The skill runs these:

```bash
gp session list
gp observe list
```

## Understand

```bash
gp observe digest <session> --endpoints-json
```

## Scaffold

```bash
gp plugin new <name> --from-run <session> --command "<name>=<METHOD> <template>"
gp plugin new <name> --from-run <session> --run <run-id> --command "<name>=<METHOD> <template>"
gp plugin add-command <name> --from-run <session> --command "<name>=<METHOD> <template>"
```

## Harden

```bash
gp observe fixtures <session> --match "<METHOD> <template>"
gp plugin check
gp plugin upgrade
```

The project's gate is whatever the guide's "The gate" section lists; it includes
`gp plugin check`.

## Kick the tires

These touch the live site or a credential, so they are never pre-approved.

```bash
gp <name> --help
gp <name> login
```
````

- [ ] **Step 5: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_skill.py -q`
Expected: PASS. The walker test resolves every `gp` invocation in `SKILL.md` and `commands.md` against the installed CLI, the interactive recorder included.

- [ ] **Step 6: Commit**

```bash
git add skills/graft/SKILL.md skills/graft/references/commands.md tests/unit/test_graft_skill.py
git commit -m "feat(skill): SKILL.md and the declared commands its pre-approved list is tested against"
```

---

### Task 5: The references, and the tests that pin them to the guide

**Files:**
- Create: `skills/graft/references/rules.md`, `capture.md`, `digest.md`, `harden.md`
- Modify: `tests/unit/test_plugin_development_guide.py:305-323` (`_slug` extracted from `_slugs_of`)
- Test: `tests/unit/test_graft_skill.py`

**Interfaces:**
- Consumes: `CTRL_C_REACHES_GP` (Task 1); `_skill_docs` (Task 4); `GUIDE`, `GUIDE_TEXT`, `_slugs_of` (existing), and `_slug` (this task) from the guide test.
- Produces: the four references `SKILL.md` names; `_slug(title: str) -> str` in `tests/unit/test_plugin_development_guide.py`.

- [ ] **Step 1: Extract the slug helper**

In `tests/unit/test_plugin_development_guide.py`, add above `_slugs_of`:

```python
def _slug(title: str) -> str:
    """The GitHub anchor for a heading whose text is *title*."""
    title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title).replace("`", "")
    slug = re.sub(r"[^\w\s-]", "", title.strip().lower())
    return re.sub(r"\s+", "-", slug)
```

and replace the three lines in `_slugs_of` that compute `title`, `slug`, and `slugs.add(...)` with `slugs.add(_slug(heading.group(2)))`.

- [ ] **Step 2: Write the failing tests**

In `tests/unit/test_graft_skill.py`, extend the guide-test import with `GUIDE`, `GUIDE_TEXT`, `_slug`, and `_slugs_of`, and append:

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


GUIDE_RUNS = _runs(_words(GUIDE_TEXT))


class TestCitations:
    @pytest.mark.parametrize("doc", _skill_docs(), ids=lambda p: p.name)
    def test_every_cited_heading_exists(self, doc: Path) -> None:
        slugs = _slugs_of(GUIDE)
        for title in _CITATION_RE.findall(doc.read_text(encoding="utf-8")):
            assert _slug(title) in slugs, f"{doc.name} cites a heading the guide lacks: {title!r}"

    def test_every_step_cites_a_heading(self) -> None:
        steps = [line for line in SKILL_MD.read_text().splitlines() if re.match(r"^\d\. \*\*", line)]
        assert len(steps) == 8
        assert all(_CITATION_RE.search(step) for step in steps)

    def test_every_rule_names_a_heading(self) -> None:
        rules: list[str] = []
        for line in (SKILL_DIR / "references" / "rules.md").read_text().splitlines():
            if line.startswith("- "):
                rules.append(line)
            elif rules and line.startswith("  "):
                rules[-1] += " " + line.strip()
        assert rules
        slugs = _slugs_of(GUIDE)
        for rule in rules:
            (title,) = _CITATION_RE.findall(rule)
            assert _slug(title) in slugs, rule


class TestNothingIsCopied:
    @pytest.mark.parametrize("doc", _skill_docs(), ids=lambda p: p.name)
    def test_no_run_of_eight_words_from_the_guide(self, doc: Path) -> None:
        copied = sorted(" ".join(run) for run in _runs(_words(_prose(doc.read_text()))) & GUIDE_RUNS)
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
        slugs = _slugs_of(GUIDE)
        for line in doc.read_text().splitlines():
            if line.startswith("> "):
                match = _QUOTE_CITATION_RE.search(line)
                assert match and _slug(match.group(1)) in slugs, line


@pytest.mark.parametrize("name", ["rules.md", "capture.md", "digest.md", "harden.md", "commands.md"])
def test_each_reference_exists_and_stays_short(name: str) -> None:
    lines = (SKILL_DIR / "references" / name).read_text().splitlines()
    assert len(lines) < _MAX_REFERENCE_LINES
```

- [ ] **Step 3: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_skill.py tests/unit/test_plugin_development_guide.py -q`
Expected: FAIL (`test_each_reference_exists_and_stays_short[rules.md]` and its siblings: `FileNotFoundError`). The guide tests still pass after the `_slug` extraction.

- [ ] **Step 4: Write `rules.md`**

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

- [ ] **Step 5: Write `capture.md`, choosing the hand-off by Task 1's result**

Create `skills/graft/references/capture.md` with the content below, replacing the line `HAND-OFF` with the paragraph for the recorded `CTRL_C_REACHES_GP`.

For `CTRL_C_REACHES_GP=yes`:

```markdown
Tell the user to run that command in this Claude Code session with the `!`
prefix, as in `! gp observe --no-session interactive <url>`. Then they log in,
work through every item on the list below, and press Ctrl+C, which ends the
recorder and saves what it captured. Wait until they say it is done.
```

For `CTRL_C_REACHES_GP=no`:

```markdown
Tell the user to run that command in a separate terminal window, not with the
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

In create mode, and in enhance mode when `gp session list` shows no session for
the plugin:

```bash
gp observe --no-session interactive <url>
```

In enhance mode when a session exists, so the recording starts already logged
in:

```bash
gp observe -s <session> interactive <url>
```

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

- [ ] **Step 6: Write `digest.md`**

Create `skills/graft/references/digest.md`:

````markdown
# From the digest to a command proposal

The user should never have to know the commands in advance. The source is the
`gp observe digest <session> --endpoints-json` projection: one entry per
endpoint with `method`, `template`, `login_flow`, `content_type`, `shape`,
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

On the guide's example recording of `myshop`, the projection holds, among
others:

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

- [ ] **Step 7: Write `harden.md`**

Create `skills/graft/references/harden.md`:

````markdown
# Fixtures, tests, the gate, and the checklist

## A fixture per command

For each command, write its capture out of the recording:

```bash
gp observe fixtures <session> --match "<METHOD> <template>"
```

That writes the response and its `.meta.json` sidecar under `tests/captures/`,
which is gitignored. Copy both files, under the same names, into the fixtures
directory the command's test reads: `gp plugin new` printed it as its `Next:`
line and `gp plugin add-command` printed it after adding the stub. Then edit the
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

- [ ] **Step 8: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graft_skill.py tests/unit/test_plugin_development_guide.py -q`
Expected: PASS. If `test_no_run_of_eight_words_from_the_guide` names a run, reword that sentence of the reference in your own words (do not edit the guide to make it pass), and run again.

- [ ] **Step 9: Commit**

The message names the hand-off Task 1 chose (`in-session` for `yes`, `separate terminal` for `no`):

```bash
git add skills/graft/references/rules.md skills/graft/references/capture.md skills/graft/references/digest.md skills/graft/references/harden.md tests/unit/test_graft_skill.py tests/unit/test_plugin_development_guide.py
git commit -m "feat(skill): the step references, cited by heading and checked against copying the guide (capture hand-off: in-session)"
```

---

### Task 6: The version-bump check, its workflow, and `just skill-version`

**Files:**
- Create: `scripts/check-skill-version.sh` (executable), `.github/workflows/skill-version.yml`
- Modify: `justfile` (new recipe after `test-unit`), `.github/workflows/python-quality.yml:25-31` (paths filter)
- Test: `tests/unit/test_skill_version_script.py`

**Interfaces:**
- Consumes: the two manifests (Task 2).
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

REPO_ROOT = Path(__file__).resolve().parents[2]
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
        self.write(".claude-plugin/marketplace.json", json.dumps({"name": "graftpunk", "version": marketplace}, indent=2))
        self.write(".claude-plugin/plugin.json", json.dumps({"name": "graftpunk", "version": plugin}, indent=2))

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
# equal to each other and different from BASE's. Claude Code pins an installed
# plugin to its version string, so a skill change without a bump never reaches
# people who already installed it. Run by .github/workflows/skill-version.yml on
# pull requests, and by hand as `just skill-version`.
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

graftpunk's repository is also a Claude Code plugin marketplace, and its one
skill takes you through these six steps. Install it with `/plugin marketplace
add stavxyz/graftpunk` and `/plugin install graftpunk@graftpunk`, then run
`/graftpunk:graft myshop https://myshop.example/` in an empty directory to create
a plugin, or `/graftpunk:graft` inside a plugin project to add commands to it.
The skill runs the `gp` commands itself and stops where you have to act: the
browser recording and the first live login. It follows this guide step by step,
so where the two disagree, this guide is the reference.
```

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
changed invocation contract is a minor bump. Claude Code pins an installed
plugin to its version string, so people who installed the skill receive a
change only when the version moves
([plugins reference](https://code.claude.com/docs/en/plugins-reference), the
`version` field).

The `Skill version` workflow checks this on every pull request that touches
those paths. Run the same check before pushing with `just skill-version`, which
compares against `origin/main`, or name another base with
`just skill-version <commit>`. After a merge, users update with
`/plugin marketplace update graftpunk` and `/plugin update graftpunk@graftpunk`.
```

- [ ] **Step 4: Add the CHANGELOG line**

Append to `CHANGELOG.md` under `[Unreleased]` / `### Added`:

```markdown
- **The `/graftpunk:graft` Claude Code skill.** The repository is now a Claude Code plugin marketplace: `/plugin marketplace add stavxyz/graftpunk`, then `/plugin install graftpunk@graftpunk`. `/graftpunk:graft myshop https://myshop.example/` in an empty directory creates a plugin by walking `docs/PLUGIN_DEVELOPMENT.md` (frame, capture, understand, scaffold, implement, harden, a live check, and the publish checklist), running the `gp` commands itself and stopping only for the browser recording and the live login; `/graftpunk:graft` inside a plugin project adds commands to it. The skill is versioned apart from the package (0.1.0) and needs graftpunk 1.17.0 or later.
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

With the branch's skill installed from a local path (`/plugin marketplace add <path to this worktree>`, then `/plugin install graftpunk@graftpunk`), run in an empty scratch directory:

1. `/graftpunk:graft myshop https://example.com/`. Where no live capture is available, stand in for the capture step with this repository's synthetic recording, filed as a run the scaffold step can read: `mkdir -p ~/.local/share/graftpunk/observe/example/dry-run && cp tests/fixtures/sample.har ~/.local/share/graftpunk/observe/example/dry-run/network.har`, then tell the skill the recording is done. Carry the flow through understand, scaffold, implement, and harden until the project's gate is green; skip the live check and say so in the notes.
2. In the resulting project, `/graftpunk:graft` with no arguments, adding one command.

Paste into the pull request's test plan every `gp` command the skill ran and the output of each gate run, and note any step where the skill deviated from `SKILL.md`. This is the one check no unit test can make. Remove the stand-in run afterwards with `gp observe clean example --force`.
