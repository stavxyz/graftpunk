# Justfile for graftpunk - see https://github.com/casey/just

# Show available commands
default:
    @just --list

# --------------------------------------------------------------------------
# Development Setup
# --------------------------------------------------------------------------

# Complete development setup (all extras)
setup:
    uv sync --all-extras
    @echo "✅ Setup complete!"

# Minimal dev setup (no optional backends)
setup-dev:
    uv sync --group dev
    @echo "✅ Dev setup complete!"

# --------------------------------------------------------------------------
# Quality Checks
# --------------------------------------------------------------------------

# Run all quality checks (lint, format, typecheck, test)
check: lint test
    @echo "✅ All checks passed!"

# Run linter and type checker
lint:
    uvx ruff check .
    uvx ruff format --check .
    uvx ty check src/

# Auto-format code
format:
    uvx ruff format .
    uvx ruff check --fix .

# --------------------------------------------------------------------------
# Testing
# --------------------------------------------------------------------------

# Run all tests
test *ARGS:
    uv run pytest tests/ -v {{ARGS}}

# Run tests with coverage report
test-cov:
    uv run pytest tests/ --cov=src/graftpunk --cov-report=term-missing --cov-report=html

# Run only unit tests (fast)
test-unit:
    uv run pytest tests/unit/ -v

# --------------------------------------------------------------------------
# Building & Publishing
# --------------------------------------------------------------------------

# Build package (sdist and wheel)
build: clean
    uvx --from build pyproject-build
    @echo "✅ Built dist/"
    @ls -la dist/

# Check package before upload
check-dist: build
    uvx twine check dist/*

# Upload to Test PyPI
publish-test: check-dist
    uvx twine upload --repository testpypi dist/*
    @echo "📦 Uploaded to Test PyPI"
    @echo "🔗 https://test.pypi.org/project/graftpunk/"

# Upload to PyPI (production) — MANUAL FALLBACK ONLY.
# The normal path is `just release` → the tag triggers CI, which publishes via
# Trusted Publishing (no token). Use this only if you must publish by hand with
# a local token (e.g. CI is unavailable).
publish: check-dist
    @echo "⚠️  Publishing to PyPI (production) — manual fallback"
    @read -p "Continue? [y/N] " confirm && [ "$$confirm" = "y" ]
    uvx twine upload dist/*
    @echo "📦 Uploaded to PyPI"
    @echo "🔗 https://pypi.org/project/graftpunk/"

# --------------------------------------------------------------------------
# Release (full workflow)
# --------------------------------------------------------------------------

# Get version from pyproject.toml
_get-version:
    @grep '^version = ' pyproject.toml | head -1 | cut -d'"' -f2

# Full release: tag + push (CI builds, publishes to PyPI, cuts the GitHub release)
#
# Publishing runs in GitHub Actions (.github/workflows/release.yml) via PyPI
# Trusted Publishing (OIDC) — no local PyPI credentials, and the test/build
# gate runs on a CI-controlled Python rather than your local interpreter.
# This recipe only validates + pushes the tag; the pushed tag does the rest.
release:
    #!/usr/bin/env bash
    set -euo pipefail

    VERSION=$(grep '^version = ' pyproject.toml | head -1 | cut -d'"' -f2)
    TAG="v${VERSION}"

    echo "📦 Releasing ${TAG}"
    echo ""

    # ----------------------------
    # Pre-flight checks
    # ----------------------------

    # Check required tools
    for cmd in gh uv git; do
        if ! command -v "$cmd" &>/dev/null; then
            echo "❌ Required command not found: $cmd"
            exit 1
        fi
    done

    # Check version format (semver-ish)
    if ! echo "$VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+'; then
        echo "❌ Invalid version format: ${VERSION} (expected X.Y.Z)"
        exit 1
    fi

    # Check for uncommitted changes
    if ! git diff --quiet HEAD; then
        echo "❌ Uncommitted changes. Commit or stash first."
        exit 1
    fi

    # Check for untracked files in src/
    if [ -n "$(git ls-files --others --exclude-standard src/)" ]; then
        echo "❌ Untracked files in src/. Add or ignore them first."
        git ls-files --others --exclude-standard src/
        exit 1
    fi

    # Check we're on main
    BRANCH=$(git branch --show-current)
    if [ "$BRANCH" != "main" ]; then
        echo "❌ Must be on main branch (currently on ${BRANCH})"
        exit 1
    fi

    # Check we're up to date with origin
    git fetch origin main --quiet
    LOCAL=$(git rev-parse HEAD)
    REMOTE=$(git rev-parse origin/main)
    if [ "$LOCAL" != "$REMOTE" ]; then
        echo "❌ Local main differs from origin/main. Pull or push first."
        echo "   Local:  $LOCAL"
        echo "   Remote: $REMOTE"
        exit 1
    fi

    # Check tag doesn't exist locally or remotely
    if git rev-parse "$TAG" >/dev/null 2>&1; then
        echo "❌ Tag ${TAG} already exists locally"
        exit 1
    fi
    if git ls-remote --tags origin | grep -q "refs/tags/${TAG}$"; then
        echo "❌ Tag ${TAG} already exists on origin"
        exit 1
    fi

    # Check CHANGELOG has entry for this version
    if ! grep -q "## \[${VERSION}\]" CHANGELOG.md; then
        echo "❌ CHANGELOG.md missing entry for version ${VERSION}"
        echo "   Add a section: ## [${VERSION}] - $(date +%Y-%m-%d)"
        exit 1
    fi

    # Check version not already on PyPI
    if curl -s "https://pypi.org/pypi/graftpunk/${VERSION}/json" | grep -q '"version"'; then
        echo "❌ Version ${VERSION} already exists on PyPI"
        exit 1
    fi

    # Check the latest CI run for this commit is green (best-effort: a failure
    # blocks; success or no-run-found proceeds, since not every commit triggers
    # the path-filtered quality workflow).
    echo "🔍 Checking CI status for HEAD (${LOCAL})..."
    CI_CONCLUSION=$(gh run list --commit "$LOCAL" --workflow "Python Code Quality" \
        --limit 1 --json conclusion --jq '.[0].conclusion // ""' 2>/dev/null || echo "")
    case "$CI_CONCLUSION" in
        success) echo "   CI is green" ;;
        "")      echo "   (no CI run found for HEAD — proceeding)" ;;
        *)
            echo "❌ CI for HEAD concluded '${CI_CONCLUSION}'. Fix before releasing."
            exit 1
            ;;
    esac

    # ----------------------------
    # Confirmation
    # ----------------------------

    echo "✅ All pre-flight checks passed"
    echo ""
    echo "This will:"
    echo "  1. Create git tag ${TAG}"
    echo "  2. Push tag to origin"
    echo ""
    echo "The pushed tag triggers .github/workflows/release.yml, which tests,"
    echo "builds, publishes to PyPI (Trusted Publishing / OIDC), and creates"
    echo "the GitHub release. No local PyPI credentials are used."
    echo ""
    read -p "Continue? [y/N] " confirm
    if [ "$confirm" != "y" ]; then
        echo "Aborted."
        exit 1
    fi

    # ----------------------------
    # Release
    # ----------------------------

    # Create and push tag — CI (release.yml) does build + publish + GH release.
    git tag -a "$TAG" -m "Release ${TAG}"
    git push origin "$TAG" --no-verify

    echo ""
    echo "✅ Tag ${TAG} pushed — the Release workflow will publish to PyPI"
    echo "🔗 Watch:   https://github.com/stavxyz/graftpunk/actions/workflows/release.yml"
    echo "🔗 Release: https://github.com/stavxyz/graftpunk/releases/tag/${TAG}"
    echo "🔗 PyPI:    https://pypi.org/project/graftpunk/${VERSION}/"

# --------------------------------------------------------------------------
# Version Bump
# --------------------------------------------------------------------------

# Bump version, sync lockfile, commit, and open a PR
bump VERSION:
    #!/usr/bin/env bash
    set -euo pipefail

    NEW="{{VERSION}}"

    # Validate semver format
    if ! echo "$NEW" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
        echo "❌ Invalid version: ${NEW} (expected X.Y.Z)"
        exit 1
    fi

    OLD=$(grep '^version = ' pyproject.toml | head -1 | cut -d'"' -f2)
    if [ "$OLD" = "$NEW" ]; then
        echo "❌ Already at version ${NEW}"
        exit 1
    fi

    echo "📦 Bumping ${OLD} → ${NEW}"

    # Update pyproject.toml
    sed -i '' "s/^version = \"${OLD}\"/version = \"${NEW}\"/" pyproject.toml

    # Update __init__.py
    sed -i '' "s/__version__ = \"${OLD}\"/__version__ = \"${NEW}\"/" src/graftpunk/__init__.py

    # Update CHANGELOG: rename [Unreleased] to [X.Y.Z] with today's date
    DATE=$(date +%Y-%m-%d)
    if grep -q '## \[Unreleased\]' CHANGELOG.md; then
        sed -i '' "s/## \[Unreleased\]/## [${NEW}] - ${DATE}/" CHANGELOG.md
        echo "✅ CHANGELOG.md: [Unreleased] → [${NEW}] - ${DATE}"
    else
        echo "❌ CHANGELOG.md has no [Unreleased] section."
        echo "   Add your changes under '## [Unreleased]' before bumping."
        echo "   The bump will rename it to '## [${NEW}] - ${DATE}'."
        # Revert pyproject.toml and __init__.py changes before exiting
        git checkout -- pyproject.toml src/graftpunk/__init__.py
        exit 1
    fi

    # Update lockfile
    uv lock --quiet
    echo "✅ Updated pyproject.toml, __init__.py, uv.lock"

    # Create branch, commit, and PR
    BRANCH="chore/bump-v${NEW}"
    git checkout -b "$BRANCH"
    git add pyproject.toml src/graftpunk/__init__.py uv.lock CHANGELOG.md
    git commit -m "chore: bump version to ${NEW}"
    git push -u origin "$BRANCH"
    gh pr create --title "chore: bump version to ${NEW}" --body "Bump version ${OLD} → ${NEW} (pyproject.toml, __init__.py, uv.lock)"

    echo ""
    echo "✅ PR created for v${NEW}"

# --------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------

# Clean build artifacts
clean:
    rm -rf .mypy_cache .pytest_cache .ruff_cache .coverage htmlcov/
    rm -rf dist/ build/ *.egg-info src/*.egg-info
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    @echo "🧹 Cleaned"

# Show current version
version:
    @uv run python -c "import graftpunk; print(graftpunk.__version__)"

# Run the CLI
cli *ARGS:
    uv run python -m graftpunk.cli.main {{ARGS}}
