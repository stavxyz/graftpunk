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

# Upload to PyPI (production)
publish: check-dist
    @echo "⚠️  Publishing to PyPI (production)"
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

# Full release: tag, GitHub release, PyPI upload
release: check
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

    # ----------------------------
    # Confirmation
    # ----------------------------

    echo "✅ All pre-flight checks passed"
    echo ""
    echo "This will:"
    echo "  1. Create git tag ${TAG}"
    echo "  2. Push tag to origin"
    echo "  3. Create GitHub release"
    echo "  4. Build and upload to PyPI"
    echo ""
    read -p "Continue? [y/N] " confirm
    if [ "$confirm" != "y" ]; then
        echo "Aborted."
        exit 1
    fi

    # ----------------------------
    # Release
    # ----------------------------

    # Create and push tag
    git tag -a "$TAG" -m "Release ${TAG}"
    git push origin "$TAG" --no-verify
    echo "✅ Tag ${TAG} pushed"

    # Create GitHub release
    gh release create "$TAG" --title "${TAG}" --generate-notes
    echo "✅ GitHub release created"

    # Build and upload to PyPI
    just clean
    uvx --from build pyproject-build
    uvx twine check dist/*
    uvx twine upload dist/*

    echo ""
    echo "✅ Released ${TAG}"
    echo "🔗 https://github.com/stavxyz/graftpunk/releases/tag/${TAG}"
    echo "🔗 https://pypi.org/project/graftpunk/${VERSION}/"

# --------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------

# Clean build artifacts
clean:
    rm -rf .pytest_cache .ruff_cache .coverage htmlcov/
    rm -rf dist/ build/ *.egg-info src/*.egg-info
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    @echo "🧹 Cleaned"

# Show current version
version:
    @uv run python -c "import graftpunk; print(graftpunk.__version__)"

# Run the CLI
cli *ARGS:
    uv run python -m graftpunk.cli.main {{ARGS}}
