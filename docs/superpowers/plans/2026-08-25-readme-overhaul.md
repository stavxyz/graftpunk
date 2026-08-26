# README Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the "Turn any website into an API" tagline — and the copy-register framing around it — with precise, engineer-facing prose everywhere the project describes itself (README, PyPI metadata, package docstring, `gp --help`, `gp version`).

**Architecture:** Docs/strings-only change guarded by one new regression test that reads the real artifacts (`graftpunk.__doc__`, the Typer app's `help=`, `pyproject.toml`, `README.md`) and asserts the old sentences are gone and the new subtitle is present. Package/CLI strings, PyPI metadata, README prose, and changelog are separate commits so each is reviewable alone. No behaviour changes.

**Tech Stack:** Python 3.11+, Typer (`rich_markup_mode="rich"`), `tomllib` (stdlib), pytest, `uv`, ruff.

**Spec:** `docs/superpowers/specs/2026-08-25-readme-overhaul-design.md` (validated against `d6f67b5`; read it first — every replacement sentence below is copied from it verbatim).

## Global Constraints

- **Subtitle, verbatim, everywhere:** `Encrypted browser session persistence with stealth automation and pluggable storage backends.`
- **Second line, verbatim, where the old second sentence lived:** `Log in through a real browser once; graftpunk captures the authenticated session — cookies, browser-fingerprinted headers, CSRF/API tokens — encrypts it at rest, and replays it over plain HTTP from Python or a generated CLI, so a site's own XHR/JSON endpoints become scriptable without a WebDriver in the loop.`
- The old sentences must not survive anywhere in `src/`, `README.md`, `pyproject.toml`: `turn any website into an api` (any case), `Graft scriptable access`, `Log in once, script forever`. The one permitted survivor is the historical quote at `docs/rfcs/RFC-001-stealth-architecture-evolution.md:48` — do not touch the RFC.
- Keep the `🔌` emoji in the README H1 and the `gp --help` banner.
- `pyproject.toml` `description` gets the subtitle only (no second line).
- Every commit message is a normal human commit: no `Co-Authored-By: Claude`, no `Generated with Claude Code` footer.
- Run tests as `NO_COLOR=1 FORCE_COLOR= uv run pytest …` — this environment exports `FORCE_COLOR=3`, which breaks 7 unrelated output-format tests.
- Work on branch `docs/readme-overhaul` in the worktree `/Users/stavxyz/src/graftpunk/.claude/worktrees/aug-fixes` (already exists; `git rev-parse --show-toplevel` must end in `aug-fixes` before any git command). Never operate on `/Users/stavxyz/src/graftpunk`.

---

### Task 1: Regression test that pins the project description

**Files:**
- Create: `tests/unit/test_project_description.py`

**Interfaces:**
- Consumes: `graftpunk.__doc__`, `graftpunk.cli.main.app` (a `typer.Typer`; its help text is `app.info.help`), `pyproject.toml` at the repo root, `README.md` at the repo root.
- Produces: the constants `SUBTITLE` and `OLD_SENTENCES` that Tasks 2–5 make true. Later tasks do not import from this file; they make it pass.

- [ ] **Step 1: Write the failing test**

```python
"""The project describes itself the same way everywhere, in the intended register.

Pins the outcome of docs/superpowers/specs/2026-08-25-readme-overhaul-design.md:
the marketing tagline is gone from every surface and the subtitle is verbatim.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import graftpunk
from graftpunk.cli.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]

SUBTITLE = (
    "Encrypted browser session persistence with stealth automation "
    "and pluggable storage backends."
)

# Sentences that must not survive anywhere the project describes itself.
OLD_SENTENCES = (
    re.compile(r"turn any website into an api", re.IGNORECASE),
    re.compile(r"Graft scriptable access"),
    re.compile(r"Log in once, script forever"),
)


def _surfaces() -> dict[str, str]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return {
        "graftpunk.__doc__": graftpunk.__doc__ or "",
        "gp --help banner (app.info.help)": app.info.help or "",
        "pyproject description": pyproject["project"]["description"],
        "README.md": (REPO_ROOT / "README.md").read_text(),
    }


def test_subtitle_is_verbatim_on_every_surface() -> None:
    for name, text in _surfaces().items():
        assert SUBTITLE in text, f"subtitle missing from {name}"


def test_old_tagline_is_gone_from_every_surface() -> None:
    for name, text in _surfaces().items():
        for pattern in OLD_SENTENCES:
            assert not pattern.search(text), f"{pattern.pattern!r} still in {name}"


def test_pyproject_description_is_the_subtitle_only() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["description"] == SUBTITLE


def test_readme_cli_banner_matches_live_help() -> None:
    """README's CLI Reference block quotes the first banner line; it must be the
    line the app actually prints (spec: location 3 is regenerated, never hand-edited)."""
    readme = (REPO_ROOT / "README.md").read_text()
    first_banner_line = next(
        line.strip() for line in (app.info.help or "").splitlines() if line.strip()
    )
    assert first_banner_line in readme
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_project_description.py -v`

Expected: 4 tests, all FAIL. `test_subtitle_is_verbatim_on_every_surface` fails with `subtitle missing from graftpunk.__doc__`; `test_old_tagline_is_gone_from_every_surface` fails with `'turn any website into an api' still in graftpunk.__doc__`; the other two fail on `pyproject description` / the banner line.

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/unit/test_project_description.py
git commit -m "test: pin the project description across README, PyPI metadata, package and CLI"
```

---

### Task 2: Package docstring, `gp --help` banner, callback docstring, `gp version` panel

**Files:**
- Modify: `src/graftpunk/__init__.py:1-4`
- Modify: `src/graftpunk/cli/main.py:1`, `:76-95` (the `typer.Typer(help=...)` string), `:140` (callback docstring), `:167` (panel title)
- Test: `tests/unit/test_project_description.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `app.info.help` whose first non-blank line is `🔌 graftpunk — Encrypted browser session persistence with stealth automation and pluggable storage backends.` — Task 4 pastes exactly that line into the README.

- [ ] **Step 1: Replace the package docstring**

`src/graftpunk/__init__.py` lines 1–4 currently read:

```python
"""graftpunk - Turn any website into an API.

Graft scriptable access onto authenticated web services.
Log in once, script forever.
```

Replace those four lines with:

```python
"""graftpunk - Encrypted browser session persistence with stealth automation and pluggable storage backends.

Log in through a real browser once; graftpunk captures the authenticated session —
cookies, browser-fingerprinted headers, CSRF/API tokens — encrypts it at rest, and
replays it over plain HTTP from Python or a generated CLI, so a site's own XHR/JSON
endpoints become scriptable without a WebDriver in the loop.
```

Leave the rest of the docstring (`This package provides:` onward) untouched.

- [ ] **Step 2: Replace the CLI module docstring**

`src/graftpunk/cli/main.py:1` currently reads `"""graftpunk CLI - turn any website into an API.`. Change that line to:

```python
"""graftpunk CLI - Encrypted browser session persistence with stealth automation and pluggable storage backends.
```

(Only the first line changes; the docstring's remaining lines stay.)

- [ ] **Step 3: Replace the `gp --help` banner**

In `src/graftpunk/cli/main.py` the `typer.Typer(...)` call's `help=` string begins:

```python
    help="""
    🔌 graftpunk - turn any website into an API

    Graft scriptable access onto authenticated web services.
    Log in once, script forever.

    \b
    Quick start:
```

Replace the first five lines of that string (banner line, blank, two sentences, blank) so it begins:

```python
    help="""
    🔌 graftpunk — Encrypted browser session persistence with stealth automation and pluggable storage backends.

    Log in through a real browser once; graftpunk captures the authenticated session —
    cookies, browser-fingerprinted headers, CSRF/API tokens — encrypts it at rest, and
    replays it over plain HTTP from Python or a generated CLI, so a site's own XHR/JSON
    endpoints become scriptable without a WebDriver in the loop.

    \b
    Quick start:
```

Everything from `\b` / `Quick start:` down is unchanged.

- [ ] **Step 4: Replace the callback docstring**

`src/graftpunk/cli/main.py:140` currently reads `    """graftpunk - turn any website into an API."""`. Typer does not render it (the explicit `help=` above takes precedence) but it must not contradict the banner. Change it to:

```python
    """graftpunk - Encrypted browser session persistence with stealth automation and pluggable storage backends."""
```

- [ ] **Step 5: Replace the `gp version` panel title**

`src/graftpunk/cli/main.py:167` currently reads `            title="Turn any website into an API",`. Change it to:

```python
            title="Encrypted browser session persistence with stealth automation and pluggable storage backends",
```

(No trailing period inside a panel title.)

- [ ] **Step 6: Run the new test — two of four should now pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_project_description.py -v`

Expected: `test_readme_cli_banner_matches_live_help` FAILS (README not updated yet), `test_pyproject_description_is_the_subtitle_only` FAILS, the other two still FAIL only on the `pyproject description` and `README.md` surfaces — confirm the failure messages no longer mention `graftpunk.__doc__` or `gp --help banner`.

- [ ] **Step 7: Lint and run the whole suite**

Run: `uvx ruff format src tests && uvx ruff check src tests && NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q`

Expected: ruff clean (the banner line is inside a string, so E501 does not apply; if ruff format re-wraps anything else, keep its output). Suite: everything passes except the 3 still-failing tests in `test_project_description.py`.

- [ ] **Step 8: Eyeball the live help and version panel**

Run: `NO_COLOR=1 uv run gp --help | head -12` and `NO_COLOR=1 uv run gp version`

Expected: the banner's first line is `🔌 graftpunk — Encrypted browser session persistence with stealth automation and pluggable storage backends.`, followed by the four-line second paragraph, then `Quick start:`. The version panel's top border carries the subtitle as its title.

- [ ] **Step 9: Commit**

```bash
git add src/graftpunk/__init__.py src/graftpunk/cli/main.py
git commit -m "docs(cli): describe graftpunk as encrypted session persistence, not an API generator"
```

---

### Task 3: PyPI metadata

**Files:**
- Modify: `pyproject.toml:8` (`description`), `pyproject.toml:15` (`keywords`)
- Test: `tests/unit/test_project_description.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `[project].description == SUBTITLE`; `keywords` gains `har`, `cdp`, `nodriver`, `csrf`.

- [ ] **Step 1: Replace `description`**

`pyproject.toml:8` currently reads:

```toml
description = "Turn any website into an API. Graft scriptable access onto authenticated web services."
```

Change it to:

```toml
description = "Encrypted browser session persistence with stealth automation and pluggable storage backends."
```

- [ ] **Step 2: Extend `keywords`**

`pyproject.toml:15` currently reads:

```toml
keywords = ["browser", "session", "automation", "api", "scraping", "selenium", "requests"]
```

Change it to:

```toml
keywords = ["browser", "session", "automation", "api", "scraping", "selenium", "requests", "har", "cdp", "nodriver", "csrf"]
```

- [ ] **Step 3: Run the test**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_project_description.py -v`

Expected: `test_pyproject_description_is_the_subtitle_only` PASSES. The two "every surface" tests still FAIL, now only on `README.md`. `test_readme_cli_banner_matches_live_help` still FAILS.

- [ ] **Step 4: Confirm the metadata still builds and renders**

Run: `uv build 2>&1 | tail -2 && uvx twine check dist/*`

Expected: `Successfully built dist/graftpunk-1.13.1.tar.gz` and the wheel; `twine check` prints `PASSED` for both (it validates that `README.md` renders as the long description and that `description` is well-formed). Then `rm -rf dist/` so the build artefacts are not committed.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "docs(pyproject): subtitle as the PyPI description; add har/cdp/nodriver/csrf keywords"
```

---

### Task 4: README — subtitle, framing sections, "What You Can Build", CLI banner block

**Files:**
- Modify: `README.md:5-7`, `:21-33`, `:60`, `:300`
- Test: `tests/unit/test_project_description.py`

**Interfaces:**
- Consumes: the first banner line produced by Task 2 (`🔌 graftpunk — Encrypted browser session persistence with stealth automation and pluggable storage backends.`).
- Produces: a README with no old sentences and the exact banner line at `README.md:300`.

- [ ] **Step 1: Replace the H1 subtitle and second line**

`README.md:5-7` currently read:

```markdown
**Turn any website into an API.**

*Graft scriptable access onto authenticated web services.*
```

Replace with:

```markdown
**Encrypted browser session persistence with stealth automation and pluggable storage backends.**

*Log in through a real browser once; graftpunk captures the authenticated session — cookies, browser-fingerprinted headers, CSRF/API tokens — encrypts it at rest, and replays it over plain HTTP from Python or a generated CLI, so a site's own XHR/JSON endpoints become scriptable without a WebDriver in the loop.*
```

- [ ] **Step 2: Rewrite "The Problem"**

`README.md:23-29` are four paragraphs:

```markdown
That service has your data—but no API.

Your ISP account. Your kid's school portal. Your local library. That niche e-commerce site you order from. Your medical records. They all have data that belongs to *you*, locked behind a login page with no API in sight.

You're left with two options: click through the UI manually every time, or give up.

**graftpunk gives you a third option.**
```

Replace all four with this single paragraph:

```markdown
Plenty of services you have an account with expose no API — an ISP portal, a school or medical portal, a niche shop, a municipal records site. The data is yours and it is one login away, but every request has to look like it came from a browser that already signed in: the right cookies, the browser's own headers, whatever CSRF or bearer token the page minted. Reproducing that by hand for every script is the actual chore.
```

- [ ] **Step 3: Rewrite the one sentence under "The Solution"**

`README.md:33` currently reads `Log in once, script forever.` (directly under `## The Solution`, above the ASCII diagram). Replace that line with:

```markdown
graftpunk does the login in a real browser (yours, or a declaratively scripted one), captures the resulting session and header fingerprint, stores it encrypted, and hands it back as a `requests`-compatible session — locally, or from S3/Supabase when the same session needs to be shared.
```

The ASCII diagram and the "Once your session is cached, you can:" list below it are unchanged.

- [ ] **Step 4: Rewrite the "What You Can Build" intro**

`README.md:60` currently reads:

```markdown
With graftpunk as your foundation, you can turn any authenticated website into a terminal-based interface:
```

Replace with:

```markdown
Each of these is a plugin command backed by the cached session; graftpunk generates the CLI, injects the session and tokens, and formats the output:
```

The five example commands under it are unchanged.

- [ ] **Step 5: Regenerate the CLI Reference banner line from live output**

Run: `NO_COLOR=1 uv run gp --help | sed 's/[[:space:]]*$//' | grep -n "🔌 graftpunk"`

Expected output (one line): `4: 🔌 graftpunk — Encrypted browser session persistence with stealth automation and pluggable storage backends.`

`README.md:300` currently reads ` 🔌 graftpunk - turn any website into an API` (inside the ```` ``` ```` block under `## CLI Reference`). Replace that one line with the live line, keeping its single leading space:

```text
 🔌 graftpunk — Encrypted browser session persistence with stealth automation and pluggable storage backends.
```

The README block deliberately keeps its trimmed shape — one banner line, then the `Commands:` list — rather than the full `gp --help` output (the second paragraph and Quick-start list are already covered elsewhere in the README). This is the decision the spec's fact-check asked to be stated.

- [ ] **Step 6: Run the pinning test — all four should pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_project_description.py -v`

Expected: 4 PASSED.

- [ ] **Step 7: Grep for survivors**

Run:

```bash
grep -rn -i "turn any website" --include=*.py --include=*.md --include=*.toml . | grep -v "^./.venv"
grep -rn -i "script forever\|Graft scriptable access" --include=*.py --include=*.md --include=*.toml . | grep -v "^./.venv"
```

Expected: the first prints exactly one line, `./docs/rfcs/RFC-001-stealth-architecture-evolution.md:48:…` (the permitted historical quote) — plus lines inside `docs/superpowers/` (the spec and this plan quote the old sentences on purpose; they are not project surfaces). The second prints only `docs/superpowers/` lines.

- [ ] **Step 8: Commit**

```bash
git add README.md
git commit -m "docs(readme): engineer-facing framing; subtitle from the repo description; regenerated CLI banner"
```

---

### Task 5: Changelog, full verification, push, PR

**Files:**
- Modify: `CHANGELOG.md:8` (insert a new section above `## [1.13.1] - 2026-08-25`)

**Interfaces:**
- Consumes: everything above.
- Produces: an open pull request from `docs/readme-overhaul` to `main`.

- [ ] **Step 1: Add the changelog entry**

`CHANGELOG.md` has no `[Unreleased]` section after the 1.13.1 release (line 8 is `## [1.13.1] - 2026-08-25`). Insert, directly above that line:

```markdown
## [Unreleased]

### Changed

- **README, PyPI description and `gp --help` describe the project as it is** — encrypted browser session persistence with stealth automation and pluggable storage backends — instead of the "Turn any website into an API" tagline. The framing sections now name the mechanism (browser login once; session, header fingerprint and tokens captured and encrypted; replayed over plain HTTP from Python or a generated CLI). `pyproject.toml` keywords gain `har`, `cdp`, `nodriver`, `csrf`. No behaviour change; a new test pins the description across every surface.

```

- [ ] **Step 2: Full verification**

Run, in order:

```bash
uvx ruff check src tests && uvx ruff format --check src tests
NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q
NO_COLOR=1 uv run python -c "import graftpunk; print(graftpunk.__doc__.splitlines()[0])"
NO_COLOR=1 uv run gp --help | head -10
NO_COLOR=1 uv run gp version
uv build >/dev/null && uvx twine check dist/* && rm -rf dist/
```

Expected: ruff clean; the suite passes with 4 more tests than before this branch (2412 → 2416 on the current `main`); the docstring line prints `graftpunk - Encrypted browser session persistence with stealth automation and pluggable storage backends.`; help and version show the new text; `twine check` PASSED.

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog entry for the README and description overhaul"
```

- [ ] **Step 4: Attribution sweep, push, open the PR**

```bash
git log --format="%H" $(git merge-base origin/main HEAD)..HEAD | while read sha; do git show -s --format=%B "$sha" | grep -qE "Co-Authored-By: Claude|Generated with .*Claude" && echo "ATTRIBUTED: $sha"; done
git push -u origin docs/readme-overhaul
gh pr create --base main --title "docs: describe graftpunk as encrypted session persistence, not an API generator" --body-file - <<'EOF'
## Summary
Replaces the "Turn any website into an API" tagline everywhere the project describes itself — README H1, PyPI `description`, package docstring, `gp --help` banner, the Typer callback docstring, the `gp version` panel — with the repo description verbatim (*Encrypted browser session persistence with stealth automation and pluggable storage backends.*) and a one-sentence mechanical description of what the tool does. "The Problem" / "The Solution" / "What You Can Build" are rewritten in the same register; the diagram, feature bullets and examples are unchanged. Keywords gain `har`, `cdp`, `nodriver`, `csrf`.

Spec: `docs/superpowers/specs/2026-08-25-readme-overhaul-design.md` (validated). Plan: `docs/superpowers/plans/2026-08-25-readme-overhaul.md`.

## Test plan
- [x] New `tests/unit/test_project_description.py` pins the subtitle on every surface, asserts the old sentences are gone, checks the PyPI description is the subtitle only, and checks the README's CLI banner line is the one `gp --help` actually prints.
- [x] `NO_COLOR=1 uv run pytest tests/` green; ruff clean; `uv build` + `twine check` PASSED (README renders as the PyPI long description).
- [x] `gp --help` and `gp version` eyeballed.
- [ ] `[manual, post-merge]` After the next bump, the PyPI project page shows the new description and README.

Docs-only; no behaviour change. The historical quote in `docs/rfcs/RFC-001` is intentionally untouched.
EOF
```

Expected: the sweep prints nothing; the push succeeds; `gh pr create` prints the PR URL. Do not merge — the operator reviews and merges (and runs `/stavxyz:polish-pr` on it first).

---

## Self-review

**Spec coverage.** Locations 1–9 → Tasks 2, 3, 4 (1, 2, 3 in Task 4; 4 in Task 3; 5–9 in Task 2). Third sentence ("Log in once, script forever.") → Task 2 Steps 1 and 3, Task 4 Step 3, and the grep in Task 4 Step 7. Framing sections and "What You Can Build" → Task 4 Steps 2–4. Keywords → Task 3 Step 2. Changelog (new `[Unreleased]` section) → Task 5 Step 1. Verification list (pytest, regenerated banner, docstring check, `uv build`/`twine check`, survivor greps) → Task 4 Steps 5–7 and Task 5 Step 2. RFC-001 untouched → Global Constraints and Task 4 Step 7. Spec design notes (six-site duplication; banner drift) → addressed by the pinning test in Task 1 (`test_readme_cli_banner_matches_live_help` is the drift guard the second note asked for); the single-constant refactor stays a non-goal.

**Placeholders.** None: every step shows the exact before/after text or the exact command and expected output.

**Consistency.** `SUBTITLE` in Task 1 equals the string used in Tasks 2, 3, 4 (with the period; the panel title in Task 2 Step 5 and the banner line both derive from it — the panel drops the period, the banner keeps it, and the test only asserts the subtitle *with* its period appears on the four surfaces it reads, none of which is the panel). The banner's first line in Task 2 Step 3 is byte-identical to the line pasted in Task 4 Step 5 (em dash, trailing period).
