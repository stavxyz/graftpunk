"""One gate, reproduced once and pinned everywhere it is quoted
(graft skill spec, 2026-09-21, "The project gate and the project's requirements")."""

from __future__ import annotations

from graftpunk.devtools.scaffold import policy
from graftpunk.devtools.scaffold.render import ScaffoldSpec, render
from tests.unit.guide_harness import GUIDE_TEXT, blocks, check_invocation, section


def _block_lines(text: str, language: str) -> list[str]:
    """The non-blank lines of the first fenced block of *language* in *text*."""
    found = blocks(text, language)
    assert found, f"no {language} block"
    (_line_no, body) = found[0]
    return [line.strip() for line in body.splitlines() if line.strip()]


def _run_step_lines(yaml_lines: list[str]) -> list[str]:
    """The lines of the one multi-line ``run: |`` step in a workflow block."""
    start = yaml_lines.index("- run: |")
    step: list[str] = []
    for line in yaml_lines[start + 1 :]:
        if line.startswith("- "):
            break
        step.append(line)
    return step


def test_the_gate() -> None:
    assert policy.PROJECT_GATE == (
        "pytest",
        "ruff check .",
        "ruff format --check .",
        "gp plugin check",
    )


def test_the_generated_readme_quotes_the_gate() -> None:
    spec = ScaffoldSpec(
        name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example"
    )
    readme = render(spec)["README.md"]
    assert _block_lines(section(readme, "## Checks"), "bash") == list(policy.PROJECT_GATE)


def test_the_guides_gate_section_quotes_the_gate() -> None:
    gate = section(GUIDE_TEXT, "### The gate")
    assert _block_lines(gate, "bash") == list(policy.PROJECT_GATE)


def test_the_guides_ci_example_runs_the_gate_as_one_step() -> None:
    gate = section(GUIDE_TEXT, "### The gate")
    assert _run_step_lines(_block_lines(gate, "yaml")) == list(policy.PROJECT_GATE)


def test_the_publish_checklist_names_the_gate_and_reproduces_none_of_it() -> None:
    checklist = section(GUIDE_TEXT, "### Before you publish")
    items = [line for line in checklist.splitlines() if line.startswith("- [ ]")]
    assert items[0].startswith("- [ ] The gate is green")
    rest = "\n".join(items[1:])
    assert "GP-FILL" not in rest
    for entry in policy.PROJECT_GATE:
        assert f"`{entry}`" not in checklist


def test_every_gp_entry_in_the_gate_resolves_through_the_cli() -> None:
    for entry in policy.PROJECT_GATE:
        if entry.startswith("gp "):
            check_invocation(entry, "PROJECT_GATE")
