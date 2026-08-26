---
type: plan
validated:
  sha: a437d58514976e5481fc17b1716e4c7f70e5cf01
  date: 2026-08-26T01:09:06Z
  reviewers: [fact-check, solid-hygiene]
  findings:
    critical: 2
    important: 3
    medium: 3
    low: 3
    nitpick: 0
  net_negative_raised: 0
  net_negative_addressed: 0
  net_negative_remaining: 0
---

# README Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the "Turn any website into an API" tagline — and the copy-register framing around it — with precise, engineer-facing prose everywhere the project describes itself (README, PyPI metadata, package docstring, `gp --help`, `gp version`).

**Architecture:** Docs/strings-only change guarded by one new regression test that reads the real artifacts (`graftpunk.__doc__`, the Typer app's `help=`, `pyproject.toml`, `README.md`) and asserts the old sentences are gone and the new subtitle is present. Package/CLI strings, PyPI metadata, README prose, and changelog are separate commits so each is reviewable alone. No behaviour changes.

**Tech Stack:** Python 3.11+, Typer (`rich_markup_mode="rich"`), `tomllib` (stdlib), pytest, `uv`, ruff.

**Spec:** `docs/superpowers/specs/2026-08-25-readme-overhaul-design.md` (validated against `d6f67b5`; read it first — every replacement sentence below is copied from it verbatim).

## Global Constraints

- **Subtitle, verbatim, everywhere:** `Authenticated browser sessions, captured once and replayed over plain HTTP: stealth login, encrypted at rest, pluggable storage.`
- **Second line, verbatim, where the old second sentence lived:** `Log in through a real browser once; graftpunk captures the authenticated session — cookies, browser-fingerprinted headers, CSRF/API tokens — encrypts it at rest, and replays it over plain HTTP from Python or a generated CLI, so a site's own XHR/JSON endpoints become scriptable without a WebDriver in the loop.`
- The old sentences must not survive anywhere in `src/`, `README.md`, `pyproject.toml`: `turn any website into an api` (any case), `Graft scriptable access`, `Log in once, script forever`. The one permitted survivor is the historical quote at `docs/rfcs/RFC-001-stealth-architecture-evolution.md:48` (`turn any website into an API`) — do not touch the RFC.
- Keep the `🔌` emoji in the README H1 and the `gp --help` banner.
- `pyproject.toml` `description` gets the subtitle only (no second line).
- **One owner in the package:** the subtitle and second line are defined once as `graftpunk.DESCRIPTION` / `graftpunk.LONG_DESCRIPTION` (implicit string concatenation, every physical line ≤ 100 columns); the banner, the `gp version` title and the docstrings derive from or wrap them. ruff's E501 applies inside strings and docstrings — never add `# noqa: E501`; wrap or derive instead.
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

> **Design note (2026-08-25):** this test deliberately reads two repository files (`README.md`, `pyproject.toml`) because they are surfaces being pinned; it locates the repo root by searching upward for `pyproject.toml` rather than a fixed `parents[2]`, so moving the file does not silently re-scope it. It is a repo-hygiene test and cannot run against an installed wheel — acceptable, since the in-package surfaces are covered by `graftpunk.DESCRIPTION` itself.

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

def _repo_root() -> Path:
    """Walk up from this file to the directory holding pyproject.toml, so the test
    does not depend on its own depth under tests/ (it reads repo files on purpose:
    README.md and pyproject.toml are two of the surfaces being pinned)."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("pyproject.toml not found above this test file")


REPO_ROOT = _repo_root()

SUBTITLE = (
    "Authenticated browser sessions, captured once and replayed over plain HTTP: "
    "stealth login, encrypted at rest, pluggable storage."
)


def _flat(text: str) -> str:
    """Collapse whitespace so a docstring that wraps the subtitle still matches."""
    return " ".join(text.split())

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
        assert SUBTITLE in _flat(text), f"subtitle missing from {name}"


def test_package_constants_are_the_single_owner() -> None:
    assert graftpunk.DESCRIPTION == SUBTITLE
    assert "Log in through a real browser once;" in graftpunk.LONG_DESCRIPTION
    assert "\n" not in graftpunk.DESCRIPTION and "\n" not in graftpunk.LONG_DESCRIPTION


def test_old_tagline_is_gone_from_every_surface() -> None:
    for name, text in _surfaces().items():
        for pattern in OLD_SENTENCES:
            assert not pattern.search(text), f"{pattern.pattern!r} still in {name}"


def test_pyproject_description_is_the_subtitle_only() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["description"] == SUBTITLE


def test_readme_cli_banner_matches_live_help() -> None:
    """README's CLI Reference block quotes the first banner line; it must be the first
    non-blank line of the Typer app's help= string (spec: location 3 is regenerated from
    the source, never hand-edited — rendered output wraps at the terminal width)."""
    readme = (REPO_ROOT / "README.md").read_text()
    first_banner_line = next(
        line.strip() for line in (app.info.help or "").splitlines() if line.strip()
    )
    assert first_banner_line in readme
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_project_description.py -v`

Expected: 5 tests, 4 FAIL, 1 PASS. `test_subtitle_is_verbatim_on_every_surface` fails with `subtitle missing from graftpunk.__doc__`; `test_old_tagline_is_gone_from_every_surface` fails with `'turn any website into an api' still in graftpunk.__doc__`; `test_pyproject_description_is_the_subtitle_only` fails; `test_package_constants_are_the_single_owner` fails with `AttributeError: module 'graftpunk' has no attribute 'DESCRIPTION'`. `test_readme_cli_banner_matches_live_help` PASSES at HEAD — `./README.md:300` (`turn any website into an API`) already quotes the current banner — and only starts failing after Task 2 changes the banner, until Task 4 updates the README.

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/unit/test_project_description.py
git commit -m "test: pin the project description across README, PyPI metadata, package and CLI"
```

---

### Task 2: One description constant; package docstring, `gp --help` banner, callback docstring, `gp version` panel derive from it

**Files:**
- Modify: `src/graftpunk/__init__.py:1-4` (`Turn any website into an API`) and add two module-level constants after the docstring
- Modify: `src/graftpunk/cli/main.py:1` (`graftpunk CLI - turn any website`); `src/graftpunk/cli/main.py:76-95` (`Quick start:`) — the `typer.Typer(help=...)` string; `src/graftpunk/cli/main.py:140` (`"""graftpunk - turn any website into an API."""`) — callback docstring; `src/graftpunk/cli/main.py:167` (`title="Turn any website into an API"`) — panel title
- Test: `tests/unit/test_project_description.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `graftpunk.DESCRIPTION: str` (the subtitle, one logical line, with its period) and `graftpunk.LONG_DESCRIPTION: str` (the second line); `app.info.help` whose first non-blank line is `🔌 graftpunk — ` + `graftpunk.DESCRIPTION` — Task 4 pastes exactly that line into the README.

> **Design note (2026-08-25):** the SOLID review flagged that pasting the subtitle into six sites leaves the "one description everywhere" invariant owned by a test. This task is the response: the in-package sites derive from `DESCRIPTION` / `LONG_DESCRIPTION` (the same pattern `__version__` uses for the version), so only `pyproject.toml` and `README.md` remain file-based copies for the test to pin.

- [ ] **Step 1: Replace the package docstring and add the constants**

`src/graftpunk/__init__.py` lines 1–4 currently read:

```python
"""graftpunk - Turn any website into an API.

Graft scriptable access onto authenticated web services.
Log in once, script forever.
```

Replace those four lines with (the docstring wraps the subtitle; every physical line stays under 100 columns):

```python
"""graftpunk - Authenticated browser sessions, captured once and replayed over plain
HTTP: stealth login, encrypted at rest, pluggable storage.

Log in through a real browser once; graftpunk captures the authenticated session -
cookies, browser-fingerprinted headers, CSRF/API tokens - encrypts it at rest, and
replays it over plain HTTP from Python or a generated CLI, so a site's own XHR/JSON
endpoints become scriptable without a WebDriver in the loop.
```

Leave the rest of the docstring (`This package provides:` onward) untouched. Then, immediately after the closing `"""` of the module docstring and before the first `import`, add:

```python
# The one owner of how graftpunk describes itself inside the package. The gp --help
# banner, the gp version panel and the module docstrings above derive from or wrap
# these; pyproject.toml and README.md carry the only file-based copies, and
# tests/unit/test_project_description.py pins every surface to them.
DESCRIPTION = (
    "Authenticated browser sessions, captured once and replayed over plain HTTP: "
    "stealth login, encrypted at rest, pluggable storage."
)
LONG_DESCRIPTION = (
    "Log in through a real browser once; graftpunk captures the authenticated session "
    "\u2014 cookies, browser-fingerprinted headers, CSRF/API tokens \u2014 encrypts it at rest, "
    "and replays it over plain HTTP from Python or a generated CLI, so a site's own "
    "XHR/JSON endpoints become scriptable without a WebDriver in the loop."
)
```

(`\u2014` is the em dash; writing it as an escape keeps the source ASCII-safe. The docstring above uses a plain hyphen for the same dashes because a docstring cannot use an escape without becoming a raw-string mess — the test normalises whitespace, not punctuation, and only asserts `SUBTITLE`, which contains no dash.)

- [ ] **Step 2: Replace the CLI module docstring**

`src/graftpunk/cli/main.py:1` (`graftpunk CLI - turn any website`) currently reads `"""graftpunk CLI - turn any website into an API.`. Change that first line (wrapping, so no physical line exceeds 100 columns) to:

```python
"""graftpunk CLI - Authenticated browser sessions, captured once and replayed over plain
HTTP: stealth login, encrypted at rest, pluggable storage.
```

The docstring's remaining lines stay.

- [ ] **Step 3: Make the `gp --help` banner an f-string over the constants**

In `src/graftpunk/cli/main.py` the `typer.Typer(...)` call's `help=` string begins:

```python
    help="""
    🔌 graftpunk - turn any website into an API

    Graft scriptable access onto authenticated web services.
    Log in once, script forever.

    \b
    Quick start:
```

Replace the first five lines of that string (banner line, blank, two sentences, blank) so it begins — note the `f` prefix and that the file already does `import graftpunk`:

```python
    help=f"""
    🔌 graftpunk — {graftpunk.DESCRIPTION}

    {graftpunk.LONG_DESCRIPTION}

    \b
    Quick start:
```

Everything from `\b` / `Quick start:` down is unchanged. Rich reflows the single-line `LONG_DESCRIPTION` to the terminal width, so it no longer renders raggedly. Confirm nothing else in the string contains `{` or `}` (it does not today), otherwise those braces would need doubling.

- [ ] **Step 4: Shorten the callback docstring**

`src/graftpunk/cli/main.py:140` (`"""graftpunk - turn any website into an API."""`) currently reads `    """graftpunk - turn any website into an API."""`. Typer does not render it (the explicit `help=` above takes precedence), so it must not be a fourth copy. Change it to:

```python
    """graftpunk CLI entry point; the help text lives on the ``typer.Typer(help=...)`` above."""
```

- [ ] **Step 5: Derive the `gp version` panel title**

`src/graftpunk/cli/main.py:167` (`title="Turn any website into an API"`) currently reads `            title="Turn any website into an API",`. Change it to:

```python
            title=graftpunk.DESCRIPTION.rstrip("."),
```

(No trailing period inside a panel title.)

- [ ] **Step 6: Run the new test — the package and CLI surfaces now pass, the rest still fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_project_description.py -v`

Expected: `test_package_constants_are_the_single_owner` PASSES. The two "every surface" tests still FAIL, but only on `pyproject description` and `README.md` (confirm the failure messages no longer mention `graftpunk.__doc__` or `gp --help banner`). `test_pyproject_description_is_the_subtitle_only` still FAILS. `test_readme_cli_banner_matches_live_help` now FAILS (the banner changed; `./README.md:300` (`turn any website into an API`) has not). So: 1 PASS, 4 FAIL.

- [ ] **Step 7: Lint and run the whole suite**

Run: `uvx ruff format src tests && uvx ruff check src tests && NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q`

Expected: ruff clean — no physical line added in this task exceeds 100 columns (the constants are implicitly concatenated, the docstrings wrap, the banner and title are expressions), so E501 has nothing to report and no `# noqa` is needed. Suite: everything passes except the 4 still-failing tests in `test_project_description.py`.

- [ ] **Step 8: Eyeball the live help and version panel**

Run: `NO_COLOR=1 uv run gp --help | head -20` and `NO_COLOR=1 uv run gp version`

Expected (80-column pipe): the banner line starts `🔌 graftpunk — Authenticated browser sessions, captured once and replayed over` and wraps onto a second line ending `pluggable storage.` (Rich wraps at 80 columns when stdout is not a TTY; in a wide terminal it is one line). The second paragraph follows, reflowed by Rich, then `Quick start:`. The version panel's top border carries the subtitle (without its period) as its title.

- [ ] **Step 9: Commit**

```bash
git add src/graftpunk/__init__.py src/graftpunk/cli/main.py
git commit -m "docs(cli): one DESCRIPTION constant; help, version and docstrings derive from it"
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

`pyproject.toml:8` (`description`) currently reads:

```toml
description = "Turn any website into an API. Graft scriptable access onto authenticated web services."
```

Change it to:

```toml
description = "Authenticated browser sessions, captured once and replayed over plain HTTP: stealth login, encrypted at rest, pluggable storage."
```

- [ ] **Step 2: Extend `keywords`**

`pyproject.toml:15` (`keywords`) currently reads:

```toml
keywords = ["browser", "session", "automation", "api", "scraping", "selenium", "requests"]
```

Change it to:

```toml
keywords = ["browser", "session", "automation", "api", "scraping", "selenium", "requests", "har", "cdp", "nodriver", "csrf"]
```

- [ ] **Step 3: Run the test**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_project_description.py -v`

Expected: `test_pyproject_description_is_the_subtitle_only` PASSES (2 PASS total). The two "every surface" tests still FAIL, now only on `README.md`. `test_readme_cli_banner_matches_live_help` still FAILS.

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
- Modify: `./README.md:5-7` (`Turn any website into an API`); `./README.md:21-33` (`## The Problem`); `./README.md:60` (`terminal-based interface`); `./README.md:300` (`turn any website into an API`)
- Test: `tests/unit/test_project_description.py`

**Interfaces:**
- Consumes: the first banner line produced by Task 2 (`🔌 graftpunk — Authenticated browser sessions, captured once and replayed over plain HTTP: stealth login, encrypted at rest, pluggable storage.`).
- Produces: a README with no old sentences and the exact banner line at `./README.md:300` (`turn any website into an API`).

- [ ] **Step 1: Replace the H1 subtitle and second line**

`./README.md:5-7` (`Turn any website into an API`) currently read:

```markdown
**Turn any website into an API.**

*Graft scriptable access onto authenticated web services.*
```

Replace with:

```markdown
**Authenticated browser sessions, captured once and replayed over plain HTTP: stealth login, encrypted at rest, pluggable storage.**

*Log in through a real browser once; graftpunk captures the authenticated session — cookies, browser-fingerprinted headers, CSRF/API tokens — encrypts it at rest, and replays it over plain HTTP from Python or a generated CLI, so a site's own XHR/JSON endpoints become scriptable without a WebDriver in the loop.*
```

- [ ] **Step 2: Rewrite "The Problem"**

`./README.md:23-29` (`That service has your data`) are four paragraphs:

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

`./README.md:33` (`Log in once, script forever.`) currently reads `Log in once, script forever.` (directly under `## The Solution`, above the ASCII diagram). Replace that line with:

```markdown
graftpunk does the login in a real browser (yours, or a declaratively scripted one), captures the resulting session and header fingerprint, stores it encrypted, and hands it back as a `requests`-compatible session — locally, or from S3/Supabase when the same session needs to be shared.
```

The ASCII diagram and the "Once your session is cached, you can:" list below it are unchanged.

- [ ] **Step 4: Rewrite the "What You Can Build" intro**

`./README.md:60` (`terminal-based interface`) currently reads:

```markdown
With graftpunk as your foundation, you can turn any authenticated website into a terminal-based interface:
```

Replace with:

```markdown
Each of these is a plugin command backed by the cached session; graftpunk generates the CLI, injects the session and tokens, and formats the output:
```

The five example commands under it are unchanged.

- [ ] **Step 5: Regenerate the CLI Reference banner line from live output**

Rendered `gp --help` wraps at 80 columns when piped, so do not paste rendered output. Take the first non-blank line of the app's `help=` source — the exact string `test_readme_cli_banner_matches_live_help` compares against:

Run: `NO_COLOR=1 uv run python -c "from graftpunk.cli.main import app; print(next(l.strip() for l in app.info.help.splitlines() if l.strip()))"`

Expected output (one line): `🔌 graftpunk — Authenticated browser sessions, captured once and replayed over plain HTTP: stealth login, encrypted at rest, pluggable storage.`

`./README.md:300` (`turn any website into an API`) currently reads ` 🔌 graftpunk - turn any website into an API` (inside the ```` ``` ```` block under `## CLI Reference`). Replace that one line with the live line, keeping its single leading space:

```text
 🔌 graftpunk — Authenticated browser sessions, captured once and replayed over plain HTTP: stealth login, encrypted at rest, pluggable storage.
```

The README block deliberately keeps its trimmed shape — one banner line, then the `Commands:` list — rather than the full `gp --help` output (the second paragraph and Quick-start list are already covered elsewhere in the README). This is the decision the spec's fact-check asked to be stated.

- [ ] **Step 6: Run the pinning test — all five should pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_project_description.py -v`

Expected: 5 PASSED.

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
- Modify: `CHANGELOG.md:8` (`## [1.13.1]`) — insert a new section above it

**Interfaces:**
- Consumes: everything above.
- Produces: an open pull request from `docs/readme-overhaul` to `main`.

- [ ] **Step 1: Add the changelog entry**

`CHANGELOG.md` has no `[Unreleased]` section after the 1.13.1 release (line 8 is `## [1.13.1] - 2026-08-25`). Insert, directly above that line:

```markdown
## [Unreleased]

### Changed

- **README, PyPI description and `gp --help` describe the project as it is** — authenticated browser sessions captured once and replayed over plain HTTP (stealth login, encrypted at rest, pluggable storage) — instead of the "Turn any website into an API" tagline. Inside the package the text has one owner, `graftpunk.DESCRIPTION` / `graftpunk.LONG_DESCRIPTION`, which the banner, the `gp version` panel and the docstrings derive from. The framing sections now name the mechanism (browser login once; session, header fingerprint and tokens captured and encrypted; replayed over plain HTTP from Python or a generated CLI). `pyproject.toml` keywords gain `har`, `cdp`, `nodriver`, `csrf`. No behaviour change; a new test pins the description across every surface.

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

Expected: ruff clean with no `# noqa` anywhere in the diff (`git diff origin/main -- src | grep -c noqa` prints `0`); the suite passes with 5 more tests than before this branch (2412 → 2417 on the current `main`); the docstring line prints `graftpunk - Authenticated browser sessions, captured once and replayed over plain` (the first physical line — the docstring wraps); help and version show the new text; `twine check` PASSED (the exact `uv build` filenames are illustrative).

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
Replaces the "Turn any website into an API" tagline everywhere the project describes itself — README H1, PyPI `description`, package docstring, `gp --help` banner, the Typer callback docstring, the `gp version` panel — with an original subtitle in the register of the repo description (*Authenticated browser sessions, captured once and replayed over plain HTTP: stealth login, encrypted at rest, pluggable storage.*) and a one-sentence mechanical description of what the tool does. "The Problem" / "The Solution" / "What You Can Build" are rewritten in the same register; the diagram, feature bullets and examples are unchanged. Keywords gain `har`, `cdp`, `nodriver`, `csrf`.

Spec: `docs/superpowers/specs/2026-08-25-readme-overhaul-design.md` (validated). Plan: `docs/superpowers/plans/2026-08-25-readme-overhaul.md`.

## Test plan
- [x] New `tests/unit/test_project_description.py` pins the subtitle on every surface (whitespace-normalised, so wrapped docstrings count), asserts the old sentences are gone, checks `graftpunk.DESCRIPTION` is the single in-package owner, checks the PyPI description is the subtitle only, and checks the README's CLI banner line is the first line of the app's `help=` source.
- [x] `NO_COLOR=1 uv run pytest tests/` green; ruff clean; `uv build` + `twine check` PASSED (README renders as the PyPI long description).
- [x] `gp --help` and `gp version` eyeballed.
- [ ] `[manual, post-merge]` After the next bump, the PyPI project page shows the new description and README.

Docs-only; no behaviour change. The historical quote in `docs/rfcs/RFC-001` is intentionally untouched.
EOF
```

Expected: the sweep prints nothing; the push succeeds; `gh pr create` prints the PR URL. Do not merge — the operator reviews and merges (and runs `/stavxyz:polish-pr` on it first).

---

## Self-review

**Spec coverage.** Locations 1–9 → Tasks 2, 3, 4 (1, 2, 3 in Task 4; 4 in Task 3; 5–9 in Task 2, where 5–7 and 9 derive from `graftpunk.DESCRIPTION` / `LONG_DESCRIPTION` and 8 becomes a short non-rendered docstring, per the spec's one-owner rule). Third sentence ("Log in once, script forever.") → Task 2 Steps 1 and 3, Task 4 Step 3, and the grep in Task 4 Step 7. Framing sections and "What You Can Build" → Task 4 Steps 2–4. Keywords → Task 3 Step 2. Changelog (new `[Unreleased]` section) → Task 5 Step 1. Verification list (pytest, regenerated banner, docstring check, `uv build`/`twine check`, survivor greps) → Task 4 Steps 5–7 and Task 5 Step 2. RFC-001 untouched → Global Constraints and Task 4 Step 7. Spec design notes (six-site duplication; banner drift) → addressed by the pinning test in Task 1 (`test_readme_cli_banner_matches_live_help` is the drift guard the second note asked for); the single-constant refactor stays a non-goal.

**Placeholders.** None: every step shows the exact before/after text or the exact command and expected output.

**Consistency.** `SUBTITLE` in Task 1 equals `graftpunk.DESCRIPTION` in Task 2 (asserted by `test_package_constants_are_the_single_owner`) and the `pyproject.toml` description in Task 3. The banner's first line is `🔌 graftpunk — ` + `DESCRIPTION` (Task 2 Step 3) and Task 4 Step 5 reads that exact line back from `app.info.help`, so the README copy cannot diverge from the source. The panel title drops the period; the test does not read the panel. `_flat()` in the test is what lets the wrapped docstrings (Task 2 Steps 1–2) satisfy the verbatim check.
