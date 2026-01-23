# Justfile for BSC - see https://github.com/casey/just

# Show available commands
default:
    @just --list

# Complete development setup
setup:
    python3 -m venv .venv
    .venv/bin/pip install -e ".[dev]"
    @echo "✅ Setup complete!"
    @echo "📝 Activate virtualenv: source .venv/bin/activate"

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

# Run all tests
test *ARGS:
    pytest tests/ -v {{ARGS}}

# Run tests with coverage
test-cov:
    pytest tests/ --cov=src/bsc --cov-report=term-missing

# Clean build artifacts
clean:
    rm -rf .mypy_cache .pytest_cache .ruff_cache __pycache__ *.egg-info .coverage dist build
