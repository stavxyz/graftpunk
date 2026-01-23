# Justfile for BSC - see https://github.com/casey/just

# Show available commands
default:
    @just --list

# --------------------------------------------------------------------------
# Development Setup
# --------------------------------------------------------------------------

# Complete development setup
setup:
    python3 -m venv .venv
    .venv/bin/pip install -e ".[dev,supabase]"
    @echo "✅ Setup complete!"
    @echo "📝 Activate: source .venv/bin/activate"

# Install all extras (including s3)
setup-all:
    python3 -m venv .venv
    .venv/bin/pip install -e ".[all]"
    @echo "✅ Full setup complete!"

# --------------------------------------------------------------------------
# Quality Checks
# --------------------------------------------------------------------------

# Run all quality checks (lint, format, typecheck, test)
check: lint test
    @echo "✅ All checks passed!"

# Run linter and type checker
lint:
    ruff check .
    ruff format --check .
    mypy src/

# Auto-format code
format:
    ruff format .
    ruff check --fix .

# --------------------------------------------------------------------------
# Testing
# --------------------------------------------------------------------------

# Run all tests
test *ARGS:
    pytest tests/ -v {{ARGS}}

# Run tests with coverage report
test-cov:
    pytest tests/ --cov=src/bsc --cov-report=term-missing --cov-report=html

# Run only unit tests (fast)
test-unit:
    pytest tests/unit/ -v

# --------------------------------------------------------------------------
# Building & Publishing
# --------------------------------------------------------------------------

# Build package (sdist and wheel)
build: clean
    python -m build
    @echo "✅ Built dist/"
    @ls -la dist/

# Check package before upload
check-dist: build
    twine check dist/*

# Upload to Test PyPI
publish-test: check-dist
    twine upload --repository testpypi dist/*
    @echo "📦 Uploaded to Test PyPI"
    @echo "🔗 https://test.pypi.org/project/bsc/"

# Upload to PyPI (production)
publish: check-dist
    @echo "⚠️  Publishing to PyPI (production)"
    @read -p "Continue? [y/N] " confirm && [ "$$confirm" = "y" ]
    twine upload dist/*
    @echo "📦 Uploaded to PyPI"
    @echo "🔗 https://pypi.org/project/bsc/"

# Install build/publish dependencies
install-publish-deps:
    pip install build twine

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
    @python -c "import bsc; print(bsc.__version__)"

# Run the CLI
cli *ARGS:
    python -m bsc.cli.main {{ARGS}}
