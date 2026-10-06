"""The graft skill's files, found once for the skill's test modules
(tests/unit/test_graft_consent.py and tests/unit/test_graft_references.py), so no
test module imports another. Nothing here is a test.
"""

from __future__ import annotations

from pathlib import Path

from tests.unit.guide_harness import REPO_ROOT, blocks

SKILL_DIR = REPO_ROOT / "skills" / "graft"
SKILL_MD = SKILL_DIR / "SKILL.md"
COMMANDS_MD = SKILL_DIR / "references" / "commands.md"


def skill_docs() -> list[Path]:
    """SKILL.md and every reference: the files the skill's prose tests read."""
    return [SKILL_MD, *sorted((SKILL_DIR / "references").glob("*.md"))]


def commands_in(text: str) -> list[str]:
    """Every non-blank line of every fenced bash block in *text*."""
    return [
        line.strip()
        for _start, body in blocks(text, "bash")
        for line in body.splitlines()
        if line.strip()
    ]


def declared_commands() -> list[str]:
    """Every line of every fenced bash block in commands.md: the commands the steps
    run, whoever runs them."""
    return commands_in(COMMANDS_MD.read_text(encoding="utf-8"))
