"""The graft skill's frontmatter and consent (graft skill spec, 2026-09-21,
"Testing"): the one pre-approved command, and every allow rule the skill offers
held to the commands commands.md declares."""

from __future__ import annotations

import os
import re
import shlex
from typing import Any

import pytest
import yaml

from graftpunk.devtools.scaffold.policy import PROJECT_GATE
from tests.unit.guide_harness import blocks, check_invocation, gp_invocations, section
from tests.unit.skill_harness import (
    COMMANDS_MD,
    SKILL_DIR,
    SKILL_MD,
    commands_in,
    skill_docs,
)

_SKILL_DIR_VAR = "${CLAUDE_SKILL_DIR}/"
# The whole pre-approved list. Claude Code keeps an allowed-tools grant only for
# the turn that invokes the skill (https://code.claude.com/docs/en/skills), and
# preflight is the one command that turn reliably runs.
_PREFLIGHT_ENTRY = "${CLAUDE_SKILL_DIR}/scripts/preflight.sh *"
# The two uv runners. Each builds an environment from the project's pyproject.toml
# on every run and writes no lockfile or .venv into it. The site runner, for the
# Kick the tires lines, installs the project and its main dependencies, so the
# plugin's entry point, and nothing else. The gate runner, for the Harden gate
# line, adds the project's dev extra (uv warns and continues when there is none)
# and the gate's own tools.
_SITE_RUNNER = "uv run --no-project --with-editable ."
_GATE_RUNNER = "uv run --no-project --with-editable '.[dev]' --with pytest --with ruff"
# The Harden line that runs the gate, one PROJECT_GATE command at a time.
_GATE_SLOT = "<gate-command>"
# The offered rules scoped to the plugin being built, one per declared Kick the
# tires line: the skill puts the plugin's site_name in place of <site-name> when
# it offers them. The live read-only command gets no rule and asks each time.
_SITE_RULES = [f"{_SITE_RUNNER} gp <site-name> --help", f"{_SITE_RUNNER} gp <site-name> login"]
_LIVE_READ_ONLY = f"{_SITE_RUNNER} gp <site-name> <command>"
# What the kick-the-tires step asks before the first live call, whatever the
# user's settings allow: the consent point for the live site and the login.
_LIVE_CALL_QUESTION = "run a live login and one read-only command now?"
# What commands.md says about the gate's commands an offered rule does not cover.
_GATE_RULE_SENTENCE = (
    "The skill offers an allow rule only for the gate's `gp` commands; every other "
    "command in the gate asks each time it runs, unless the user's settings allow it."
)


def _frontmatter() -> dict[str, Any]:
    text = SKILL_MD.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n", 2)[1])


def _allowed() -> list[str]:
    return re.findall(r"Bash\(([^)]*)\)", _frontmatter()["allowed-tools"])


def _commands_section(heading: str) -> str:
    """commands.md's section under *heading*, by the harness's one section rule."""
    return section(COMMANDS_MD.read_text(encoding="utf-8"), heading)


def _skill_run_commands() -> list[str]:
    """The commands under "Run by the skill", the gate's line expanded to each
    PROJECT_GATE command in its place: the only ones an offered rule may cover."""
    return [
        command
        for line in commands_in(_commands_section("## Run by the skill"))
        for command in (
            [line.replace(_GATE_SLOT, gate) for gate in PROJECT_GATE]
            if _GATE_SLOT in line
            else [line]
        )
    ]


def _preflight_commands() -> list[str]:
    return commands_in(_commands_section("## Run by preflight"))


def _offered_rules() -> list[str]:
    """The patterns of the allow rules commands.md offers: its fenced text block under
    "Allow rules for a prompt-free run", one Bash(<pattern>) rule per line."""
    rules_section = _commands_section("## Allow rules for a prompt-free run")
    (_start, body), *_rest = blocks(rules_section, "text")
    rules = [line.strip() for line in body.splitlines() if line.strip()]
    assert rules and all(re.fullmatch(r"Bash\([^()]+\)", rule) for rule in rules), rules
    return [rule.removeprefix("Bash(").removesuffix(")") for rule in rules]


def _matches(pattern: str, command: str) -> bool:
    """Claude Code's Bash permission rule: a trailing " *" matches the prefix with or
    without arguments; anything else matches exactly
    (https://code.claude.com/docs/en/permissions, Wildcard patterns).

    Valid only under the restriction test_every_rule_is_a_scoped_gp_rule enforces
    (a "*" only as the trailing " *"); it models no other wildcard form. Re-check
    it against that page whenever the page changes."""
    if pattern.endswith(" *"):
        prefix = pattern[:-2]
        return command == prefix or command.startswith(prefix + " ")
    return command == pattern


class TestFrontmatter:
    def test_it_parses_and_names_the_skill(self) -> None:
        frontmatter = _frontmatter()
        assert frontmatter["name"] == "graft"
        assert "disable-model-invocation" not in frontmatter

    def test_the_description_names_both_modes(self) -> None:
        description = _frontmatter()["description"]
        assert "Create a graftpunk site plugin" in description
        assert "add commands to an existing one" in description

    def test_allowed_tools_is_exactly_the_preflight_entry(self) -> None:
        """Pinned: anything more would read as consent the grant does not give past
        the invoking turn."""
        assert _allowed() == [_PREFLIGHT_ENTRY]

    def test_every_path_in_allowed_tools_exists(self) -> None:
        paths = [p for p in _allowed() if p.startswith(_SKILL_DIR_VAR)]
        assert paths
        for pattern in paths:
            target = SKILL_DIR / pattern.removeprefix(_SKILL_DIR_VAR).removesuffix(" *")
            assert target.is_file() and os.access(target, os.X_OK), pattern


class TestOfferedAllowRules:
    def test_every_offered_rule_matches_a_command_the_skill_runs(self) -> None:
        """Containment: a rule for a command the skill no longer runs itself fails."""
        declared = _skill_run_commands()
        for pattern in _offered_rules():
            assert any(_matches(pattern, command) for command in declared), (
                f"Bash({pattern}) matches no command under Run by the skill"
            )

    def test_no_rule_is_offered_for_what_only_preflight_runs(self) -> None:
        """Preflight runs its gp calls inside the one pre-approved call, so a rule
        for them would widen consent for nothing."""
        preflight_only = set(_preflight_commands()) - set(_skill_run_commands())
        assert preflight_only
        for command in preflight_only:
            assert not any(_matches(p, command) for p in _offered_rules()), command

    def test_every_rule_is_a_scoped_gp_rule(self) -> None:
        """Never Bash(*), never a bare Bash(gp *), never a command other than gp,
        whether gp runs from PATH or through a uv runner."""
        for pattern in _offered_rules():
            command = pattern
            for runner in (_SITE_RUNNER, _GATE_RUNNER):
                command = command.removeprefix(f"{runner} ")
            assert command.startswith("gp "), pattern
            assert command not in ("*", "gp *"), pattern
            assert "*" not in pattern.removesuffix(" *"), pattern

    def test_no_offered_rule_reaches_the_recorder_or_every_gp_command(self) -> None:
        """The other direction of containment: no rule matches a command outside
        the declared list."""
        for probe in (
            "gp observe --no-session interactive <url>",
            "gp observe -s <session> interactive <url>",
            "gp anything",
            "gp <site-name> delete-everything",
            "uv run anything",
            f"{_SITE_RUNNER} pytest",
            f"{_GATE_RUNNER} pytest",
            f"{_SITE_RUNNER} gp anything",
            f"{_GATE_RUNNER} gp anything",
            f"{_SITE_RUNNER} gp <site-name> delete-everything",
            f"{_GATE_RUNNER} gp <site-name> delete-everything",
        ):
            assert not any(_matches(pattern, probe) for pattern in _offered_rules()), probe

    def test_the_plugin_rules_are_the_declared_help_and_login_lines(self) -> None:
        rules = _offered_rules()
        assert [rule for rule in rules if "<site-name>" in rule] == _SITE_RULES
        assert set(_SITE_RULES) <= set(_skill_run_commands())

    def test_the_live_read_only_command_asks_each_time(self) -> None:
        """It reads the user's account: declared, and covered by no offered rule."""
        assert _LIVE_READ_ONLY in _skill_run_commands()
        assert not any(_matches(pattern, _LIVE_READ_ONLY) for pattern in _offered_rules())

    def test_the_gates_gp_commands_have_rules_and_commands_md_says_the_rest_do_not(
        self,
    ) -> None:
        """The gate is policy.PROJECT_GATE's to list, and runs through the gate
        runner. Every gp command in it is covered by an offered rule; an
        offered rule is only ever a gp rule (test_every_rule_is_a_scoped_gp_rule),
        so commands.md says in words that the gate's other commands ask each time."""
        rules = _offered_rules()
        gate_gp = [f"{_GATE_RUNNER} {c}" for c in PROJECT_GATE if c.startswith("gp ")]
        assert gate_gp
        for command in gate_gp:
            assert any(_matches(pattern, command) for pattern in rules), command
        commands_md = " ".join(COMMANDS_MD.read_text(encoding="utf-8").split())
        assert _GATE_RULE_SENTENCE in commands_md


def test_the_kick_the_tires_lines_and_the_gate_run_through_their_runners() -> None:
    """The kick-the-tires lines need the plugin's entry point installed, which the gp
    on PATH does not have; the gate needs the project's dev dependencies and its
    own tools as well."""
    kick = commands_in(_commands_section("### Kick the tires"))
    harden = commands_in(_commands_section("### Harden"))
    assert kick and f"{_GATE_RUNNER} {_GATE_SLOT}" in harden
    for line in kick:
        assert line.startswith(f"{_SITE_RUNNER} gp "), line


def test_the_gate_runner_installs_every_program_the_gate_runs() -> None:
    """PROJECT_GATE owns the gate's commands; the declared Harden line's --with list
    must install each program one of them runs, gp aside (the project's graftpunk
    dependency installs it). A new tool in PROJECT_GATE fails here, not on users."""
    harden = commands_in(_commands_section("### Harden"))
    (gate_line,) = [line for line in harden if _GATE_SLOT in line]
    words = shlex.split(gate_line)
    installed = {words[i + 1] for i, word in enumerate(words[:-1]) if word == "--with"}
    for command in PROJECT_GATE:
        program = command.split()[0]
        assert program == "gp" or program in installed, command


def test_the_live_step_asks_before_the_first_live_call() -> None:
    """Found by the step's bold name, up to the next numbered item or heading, so
    adding or removing a step does not move it."""
    text = SKILL_MD.read_text(encoding="utf-8")
    rest = text[text.index("**Kick the tires**") :]
    ends = [
        m.start()
        for m in (re.search(r"^\d+\. \*\*", rest, re.M), re.search(r"^## ", rest, re.M))
        if m
    ]
    step = rest[: min(ends)] if ends else rest
    assert _LIVE_CALL_QUESTION in " ".join(step.split())


SKILL_INVOCATIONS = [
    (doc.name, line_no, invocation)
    for doc in skill_docs()
    for line_no, invocation in gp_invocations(doc.read_text(encoding="utf-8"))
]


def test_the_skill_has_invocations_to_check() -> None:
    assert SKILL_INVOCATIONS


@pytest.mark.parametrize(
    ("doc", "line_no", "invocation"), SKILL_INVOCATIONS, ids=lambda v: str(v)[:60]
)
def test_every_gp_invocation_names_a_real_command_and_options(
    doc: str, line_no: int, invocation: str
) -> None:
    check_invocation(invocation, f"{doc}:{line_no}")
