---
type: spec
validated:
  sha: d6f67b5a07dc2764879902b62d6247a11b56ab1e
  date: 2026-08-26T00:52:27Z
  reviewers: [fact-check, solid-hygiene]
  findings:
    critical: 0
    important: 1
    medium: 2
    low: 6
    nitpick: 0
  net_negative_raised: 0
  net_negative_addressed: 0
  net_negative_remaining: 0
---

# README overhaul — replace the marketing tagline with precise, nerd-facing prose

**Status:** design, awaiting validation
**Scope:** docs-only + the strings the CLI/package echo; no behaviour change
**Ships in:** its own PR, before the next version bump (PyPI renders `README.md` and the `description` field, so the bump picks it up)

## Problem

The project's framing sentence is **"Turn any website into an API."** It is imprecise and pitched at the wrong reader:

- It is not what the tool does. graftpunk logs in through a real browser once, captures the authenticated session (cookies, browser-fingerprinted headers, CSRF/API tokens), encrypts it at rest, and replays it over plain HTTP from Python or a generated CLI. Nothing is turned into an API; the site's *own* XHR/JSON endpoints become scriptable because the request looks like the browser's.
- It reads like copy. The repo description the owner wrote — *"encrypted browser session persistence with stealth automation and pluggable storage backends"* — is the register the project wants: the reader is a software engineer who knows what a session, a WAF and a CDP are.

The rest of the README (Features table, Plugins, CLI Reference, Security, Development) is already written for that reader and is not the problem.

## Every place the tagline (or its register) appears

Verified by `grep -rn -i "turn any website into an api\|Graft scriptable access"` on `main` at `d6f67b5`. No test asserts any of these strings (`grep -rn -i "turn any website\|Graft scriptable" tests/` is empty).

| # | Location | Current text | Surface |
|---|---|---|---|
| 1 | `./README.md:5` (`Turn any website into an API`) | `**Turn any website into an API.**` | H1 subtitle (GitHub + PyPI page) |
| 2 | `./README.md:7` (`Graft scriptable access`) | `*Graft scriptable access onto authenticated web services.*` | second line under the H1 |
| 3 | `./README.md:300` (`turn any website into an API`) | `🔌 graftpunk - turn any website into an API` | the `gp --help` banner echoed inside the CLI Reference code block |
| 4 | `pyproject.toml:8` (`description`) | `description = "Turn any website into an API. Graft scriptable access onto authenticated web services."` | PyPI one-liner, `pip show` |
| 5 | `src/graftpunk/__init__.py:1` (`Turn any website into an API`) and `src/graftpunk/__init__.py:3` (`Graft scriptable access`) | module docstring, all three sentences (lines 1, 3 and 4 — line 4 is "Log in once, script forever.") | `help(graftpunk)` |
| 6 | `src/graftpunk/cli/main.py:1` (`graftpunk CLI - turn any website`) | module docstring | — |
| 7 | `src/graftpunk/cli/main.py:79` (`🔌 graftpunk - turn any website`) and `src/graftpunk/cli/main.py:81` (`Graft scriptable access`) | the `typer.Typer(help=...)` string that `gp --help` prints (main.py:76-95), all three sentences (lines 79, 81 and 82 — line 82 is "Log in once, script forever.") | every `gp --help` |
| 8 | `src/graftpunk/cli/main.py:140` (`"""graftpunk - turn any website into an API."""`) | Typer callback docstring `"""graftpunk - turn any website into an API."""` | not rendered — the explicit `help=` on `typer.Typer(...)` (location 7) takes precedence; edited for consistency only |
| 9 | `src/graftpunk/cli/main.py:167` (`Turn any website into an API`) | `title="Turn any website into an API"` on a Rich panel | shown by `gp version` |

Left as-is on purpose: `docs/rfcs/RFC-001-stealth-architecture-evolution.md:48` (`turn any website into an API`) quotes the old sentence inside a dated RFC; RFCs are historical records and rewriting a quote there would falsify the record.

Two README sections share the register problem without using the sentence:

- **"The Problem" / "The Solution"** (`./README.md:21` (`## The Problem`) through `./README.md:56` (`Capture network traffic`)): the *content* is right (log in once, cache encrypted, replay with browser headers) and the ASCII diagram and bullet list stay; the framing paragraphs ("That service has your data—but no API", "graftpunk gives you a third option" in The Problem; "Log in once, script forever" in The Solution) are copy.
- **"What You Can Build"** (`./README.md:58` (`## What You Can Build`) to line 60): "you can turn any authenticated website into a terminal-based interface" repeats the tagline's claim. The five example commands under it are good and stay.

## Design

### The two replacement lines

**Subtitle (locations 1, 4, 5, 6, 7, 8, 9):** the owner's repo description, verbatim:

> Encrypted browser session persistence with stealth automation and pluggable storage backends.

**Second line (locations 2, 5, 7 — wherever the current second sentence appears):** what it does, mechanically:

> Log in through a real browser once; graftpunk captures the authenticated session — cookies, browser-fingerprinted headers, CSRF/API tokens — encrypts it at rest, and replays it over plain HTTP from Python or a generated CLI, so a site's own XHR/JSON endpoints become scriptable without a WebDriver in the loop.

Rules for applying them:

- Location 4 (`pyproject.toml` `description`) takes the subtitle only — PyPI truncates long descriptions in listings; the second line is already the README's job.
- Locations 8 and 9 take the subtitle only (a callback docstring that Typer does not render because the app has an explicit `help=`, and a panel title — one sentence each).
- Location 3 is not edited by hand: it is a copy of the banner (location 7) and must be regenerated from actual `gp --help` output after the change, so the README never shows a banner the CLI does not print.
- Keep the 🔌 emoji in the banner and the H1; it is the project's mark, not copy.

> **Design note (2026-08-25):** the subtitle is hand-duplicated across six in-package/config sites (`__init__` docstring, `typer.Typer(help=...)`, callback docstring, `gp version` panel title, `pyproject.toml`), which is why this change is a nine-location sweep. This PR re-copies deliberately — a docs PR is not the place to restructure — but the in-package copies could later derive from one module-level constant so the tagline has a single owner inside the package; `pyproject.toml` and the README stay the two unavoidable external copies. Optional follow-up, not part of this PR.

### The two framing sections

**"The Problem"** — keep the heading; replace the four paragraphs (README.md:23-29) with one that names the situation an engineer recognises:

> Plenty of services you have an account with expose no API — an ISP portal, a school or medical portal, a niche shop, a municipal records site. The data is yours and it is one login away, but every request has to look like it came from a browser that already signed in: the right cookies, the browser's own headers, whatever CSRF or bearer token the page minted. Reproducing that by hand for every script is the actual chore.

**"The Solution"** — keep the heading, the ASCII diagram and the "Once your session is cached, you can:" bullet list. Replace "Log in once, script forever." with one sentence that reads as a description of the mechanism:

> graftpunk does the login in a real browser (yours, or a declaratively scripted one), captures the resulting session and header fingerprint, stores it encrypted, and hands it back as a `requests`-compatible session — locally, or from S3/Supabase when the same session needs to be shared.

(The storage-backend clause is what the repo description promises and the current README only mentions in the Features table, the install extras and the configuration table — never in the framing.)

**"What You Can Build"** — replace the intro sentence with:

> Each of these is a plugin command backed by the cached session; graftpunk generates the CLI, injects the session and tokens, and formats the output.

### `pyproject.toml` keywords

Add `"har"`, `"cdp"`, `"nodriver"`, `"csrf"` to `keywords`; drop nothing. They describe what the package actually contains (HAR capture, CDP-driven nodriver backend, CSRF token extraction) and match how someone would search for it.

## Files touched

- `README.md` (locations 1, 2, 3, the two framing sections, "What You Can Build" intro)
- `pyproject.toml` (`description`, `keywords`)
- `src/graftpunk/__init__.py` (module docstring)
- `src/graftpunk/cli/main.py` (module docstring, banner, callback docstring, panel title)
- `CHANGELOG.md` — add a new `## [Unreleased]` section above `## [1.13.1]` with a `### Changed` entry: one line ("README and CLI help describe the project as encrypted browser session persistence with stealth automation and pluggable storage backends; the 'turn any website into an API' tagline is gone").

## Testing / verification

- `NO_COLOR=1 uv run pytest tests/` (no test pins these strings; the run guards against an accidental syntax slip in `main.py`).
- `gp --help` rendered and pasted into `./README.md:300` (`turn any website into an API`) — location 3 — verified by diffing the README block against the live output.
- `python -c "import graftpunk; print(graftpunk.__doc__)"` shows the new docstring.
- `uv build` + `twine check dist/*` — confirms `pyproject.toml` still parses and the long description renders (this is what PyPI will show).
- Grep after the change: `grep -rn -i "turn any website" --include=*.py --include=*.md --include=*.toml .` returns only the RFC-001 quote, and `grep -rn -i "script forever" --include=*.py --include=*.md .` returns nothing.

> **Design note (2026-08-25):** location 3 is a manually-synced copy of `gp --help` output with no guard, so it will drift the next time the banner or the Quick-start command list changes. Regenerating it once is what this PR does; a future doc check could diff the README block against live `gp --help`. Out of scope here — recorded so the drift risk is visible.

## Non-goals

- No restructuring of README sections; no changes to Features, Plugins, CLI Reference, Configuration, Security, Development, or Acknowledgments.
- No wording changes in `docs/HOW_IT_WORKS.md` or the RFCs.
- No behaviour changes anywhere.

## Assumption to confirm

The subtitle is the owner's repo-description sentence **verbatim**. If the owner would rather have a pass taken at it, the only line that changes is that one, in every location listed above.
