# AI Assistant Instructions

## Before You Begin

Read these instructions before performing any operations.

## Critical Rules

### NEVER Push to Main Branch

- ALWAYS create feature branch: `git checkout -b feature/name`
- ALWAYS use pull requests
- Check current branch before pushing: `git branch --show-current`

### NEVER Modify System Python

- ALWAYS use virtual environments
- Check venv active: `which python` (must show `.venv/bin/python`)
- NEVER use: `pip install` without venv, `--user` flag, `sudo pip`

### ALWAYS Run Full Test Suite

- Before every commit: `pytest tests/ -v`
- Run ALL quality checks: `just check`
- Fix failing tests immediately

## Quick Reference

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Or use `just setup` to do all of the above.

### PR Workflow

```bash
git checkout -b feature/my-feature
# Make changes
just check  # Run all checks
git add <specific-files>
git commit -m "feat: description"
git push -u origin feature/my-feature
gh pr create
```

### Code Quality

```bash
just check  # Run all checks (lint, format, typecheck, test)
# Or manually:
ruff check --fix .
ruff format .
mypy src/
pytest tests/ -v
```

### Atomic Commits

Each commit = ONE logical change.

```bash
# The "AND" Test: If message uses "and", split into multiple commits
# BAD: git commit -m "feat: add feature and fix bug"
# GOOD:
git commit -m "feat: add new feature"
git commit -m "fix: resolve bug"
```

## Pre-Operation Checklist

Before ANY operation, verify:

- [ ] Check venv active: `which python` → `.venv/bin/python`
- [ ] Check branch: `git branch --show-current` → NOT main
- [ ] Read files before editing
- [ ] Plan atomic commits

## Standards

- **Line length**: 100 characters max
- **File size**: 500 lines max (code), 600 lines max (markdown)
- **Type hints**: Required on all functions
- **Docstrings**: Google-style for public functions
- **Logging**: Use structlog, never print()
- **Error handling**: Catch specific exceptions only

## Common Mistakes to Avoid

- ❌ `pip install` without checking venv
- ❌ `git push` when on main branch
- ❌ Using `print()` instead of logging
- ❌ Catching generic `Exception`
- ❌ Files over 500 lines
- ❌ Functions without type hints
- ❌ Committing without running tests
- ❌ Multiple unrelated changes in one commit
