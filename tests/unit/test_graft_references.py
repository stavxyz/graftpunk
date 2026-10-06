"""The graft skill's references against the guide they cite, and the commands
against commands.md, their one owner (graft skill spec, 2026-09-21, "Testing")."""

from __future__ import annotations

import dataclasses
import json
import re
import shlex
from pathlib import Path
from typing import Any

import pytest

from graftpunk.har.digest import DigestSource, digest
from graftpunk.har.documents import LoginForm
from graftpunk.har.report import endpoints_projection
from tests.unit.guide_harness import (
    GUIDE,
    GUIDE_TEXT,
    REPO_ROOT,
    gp_invocations,
    outside_fences,
    section,
    slug,
    slugs_of,
)
from tests.unit.skill_harness import (
    COMMANDS_MD,
    SKILL_DIR,
    SKILL_MD,
    declared_commands,
    skill_docs,
)

_CITATION_RE = re.compile(r"\(guide: ([^)]+)\)")
_QUOTE_CITATION_RE = re.compile(r"\(guide: ([^)]+)\)\s*$")
# "Cite, do not copy", enforced at a stated threshold.
_COPY_RUN = 8
_MAX_REFERENCE_LINES = 120


def _words(text: str) -> list[str]:
    """Lowercased words with punctuation stripped."""
    return re.sub(r"[^\w\s]", " ", text.lower()).split()


def _runs(words: list[str]) -> set[tuple[str, ...]]:
    return {tuple(words[i : i + _COPY_RUN]) for i in range(len(words) - _COPY_RUN + 1)}


def _prose(text: str) -> str:
    """*text* outside fenced blocks and quotations, with its citations removed: what the
    copy check compares. A quotation is a blockquote line ending with a citation.
    Fences are the harness's to find (``outside_fences``)."""
    kept = [
        line
        for line in outside_fences(text).splitlines()
        if not (line.startswith("> ") and _QUOTE_CITATION_RE.search(line))
    ]
    return _CITATION_RE.sub(" ", "\n".join(kept))


def _without_section(text: str, heading: str) -> str:
    """*text* without the section under the line *heading*, where the section ends by
    the harness's one rule (``section``); *text* itself when it has no such line."""
    lines = text.splitlines()
    if heading not in lines:
        return text
    start = lines.index(heading)
    end = start + len(section(text, heading).splitlines())
    return "\n".join(lines[:start] + lines[end:])


# The guide's "With the skill" section describes this skill, and holds only its
# install and invocation facts, which the skill may state in the same words;
# every other section is the guide's to state and the skill's to cite.
GUIDE_RUNS = _runs(_words(_without_section(GUIDE_TEXT, "## With the skill")))

# The section the copy detector's self-tests take their probe sentence from.
_PROBE_HEADING = "## Capture"


def _probe() -> tuple[str, str]:
    """The first prose sentence of the guide's _PROBE_HEADING section, and that
    section's title, read from GUIDE_TEXT at test time so a guide edit cannot leave
    the self-tests probing a sentence the guide no longer holds."""
    paragraphs = outside_fences(section(GUIDE_TEXT, _PROBE_HEADING)).split("\n\n")[1:]
    paragraph = " ".join(next(p for p in paragraphs if p.strip()).split())
    sentence = re.split(r"(?<=[.:])\s", paragraph)[0]
    assert len(_words(sentence)) >= _COPY_RUN, sentence
    return sentence, _PROBE_HEADING.lstrip("# ")


class TestCitations:
    @pytest.mark.parametrize("doc", skill_docs(), ids=lambda p: p.name)
    def test_every_cited_heading_exists(self, doc: Path) -> None:
        slugs = slugs_of(GUIDE)
        for title in _CITATION_RE.findall(doc.read_text(encoding="utf-8")):
            assert slug(title) in slugs, f"{doc.name} cites a heading the guide lacks: {title!r}"

    def test_every_step_cites_a_heading(self) -> None:
        steps = [
            line for line in SKILL_MD.read_text().splitlines() if re.match(r"^\d+\. \*\*", line)
        ]
        assert steps
        assert all(_CITATION_RE.search(step) for step in steps)

    def test_every_rule_names_a_heading(self) -> None:
        rules: list[str] = []
        for line in (SKILL_DIR / "references" / "rules.md").read_text().splitlines():
            if line.startswith("- "):
                rules.append(line)
            elif rules and line.startswith("  "):
                rules[-1] += " " + line.strip()
        assert rules
        slugs = slugs_of(GUIDE)
        for rule in rules:
            (title,) = _CITATION_RE.findall(rule)
            assert slug(title) in slugs, rule


class TestNothingIsCopied:
    @pytest.mark.parametrize("doc", skill_docs(), ids=lambda p: p.name)
    def test_no_run_of_eight_words_from_the_guide(self, doc: Path) -> None:
        copied = sorted(
            " ".join(run) for run in _runs(_words(_prose(doc.read_text()))) & GUIDE_RUNS
        )
        assert copied == [], f"{doc.name} copies the guide: {copied[:3]}"

    def test_the_detector_catches_a_copied_sentence(self) -> None:
        sentence, _title = _probe()
        assert _runs(_words(_prose(sentence))) & GUIDE_RUNS

    def test_a_quotation_is_exempt_only_with_a_citation(self) -> None:
        sentence, title = _probe()
        assert not _runs(_words(_prose(f"> {sentence} (guide: {title})"))) & GUIDE_RUNS
        assert _runs(_words(_prose(f"> {sentence}"))) & GUIDE_RUNS

    @pytest.mark.parametrize("doc", skill_docs(), ids=lambda p: p.name)
    def test_every_quotation_cites_a_heading_that_exists(self, doc: Path) -> None:
        slugs = slugs_of(GUIDE)
        for line in doc.read_text().splitlines():
            if line.startswith("> "):
                match = _QUOTE_CITATION_RE.search(line)
                assert match and slug(match.group(1)) in slugs, line


@pytest.mark.parametrize(
    "name", ["rules.md", "capture.md", "digest.md", "harden.md", "commands.md"]
)
def test_each_reference_exists_and_stays_short(name: str) -> None:
    lines = (SKILL_DIR / "references" / name).read_text().splitlines()
    assert len(lines) < _MAX_REFERENCE_LINES


# gp invocations the skill docs spell that Claude does not run from commands.md:
# lines printed for the user to fill in, one named only to say its output is never
# printed, and a worked example with real values. The one exemption list, so a new
# exemption shows in review as a change to this test.
_NOT_RUN_FROM_COMMANDS_MD = (
    "gp config set",  # printed for the user with placeholder values (SKILL.md, Secrets)
    "gp config get --resolve",  # named only to say its output is never printed
    "gp plugin new myshop --from-run myshop",  # digest.md's worked example
)


def _token_pattern(token: str) -> re.Pattern[str]:
    """A commands.md token as a pattern: each ``<placeholder>`` in it stands for any
    non-empty value."""
    return re.compile(".+".join(re.escape(part) for part in re.split(r"<[^<>]+>", token)))


def _is_declared(invocation: str, declared: list[str]) -> bool:
    """The one matcher: whether *invocation* is a commands.md line, with its
    placeholders filled or not, or the leading words of one (prose that names "the
    `gp plugin new` line" points at it). Leading words stop before the first option,
    so a partial command with options of its own must match a line in full."""
    words = shlex.split(invocation)
    for line in declared:
        tokens = shlex.split(line)
        if len(words) > len(tokens):
            continue
        if not all(_token_pattern(t).fullmatch(w) for t, w in zip(tokens, words, strict=False)):
            continue
        if len(words) == len(tokens) or not any(w.startswith("-") for w in words):
            return True
    return False


@pytest.mark.parametrize("doc", skill_docs(), ids=lambda p: p.name)
def test_commands_md_owns_every_gp_command_the_skill_spells(doc: Path) -> None:
    """Containment for the commands themselves: every gp invocation a skill doc
    spells, backticked or fenced, is a commands.md line or the leading words of one,
    or a named exemption. Outside commands.md a placeholder is never spelled: the
    doc names the commands.md block instead, so each templated command has one
    spelling. A command added to a step without adding it to commands.md fails here."""
    declared = declared_commands()
    for line_no, invocation in gp_invocations(doc.read_text(encoding="utf-8")):
        if invocation.startswith(_NOT_RUN_FROM_COMMANDS_MD):
            continue
        where = f"{doc.name}:{line_no}: {invocation}"
        assert _is_declared(invocation, declared), f"{where} is not declared in commands.md"
        if doc != COMMANDS_MD:
            assert "<" not in invocation, f"{where} spells a command commands.md owns"


# digest.md's closed lists, each compared with what the digest produces. The
# projection's type labels are not a closed list there: graftpunk.har.digest
# builds them across several functions and keeps no constant naming them all, so
# digest.md says which labels the list includes, and no test here pins them.
_DIGEST_MD = SKILL_DIR / "references" / "digest.md"


def _listed(opening: str, closing: str) -> set[str]:
    """The backticked names in digest.md between *opening* and the next *closing*,
    whitespace collapsed first: one of its closed lists."""
    flat = " ".join(_DIGEST_MD.read_text(encoding="utf-8").split())
    start = flat.index(opening) + len(opening)
    return set(re.findall(r"`([^`]+)`", flat[start : flat.index(closing, start)]))


def _projection() -> dict[str, Any]:
    """endpoints_projection on the repository's sample recording, with one login form
    added, since the sample records none."""
    result = digest(DigestSource.from_har(REPO_ROOT / "tests" / "fixtures" / "sample.har"))
    form = LoginForm(
        action="/session",
        method="POST",
        fields={"username": "#username", "password": "#password"},
        submit=None,
        hidden=(),
        source="https://myshop.example/login",
    )
    return endpoints_projection(dataclasses.replace(result, login_forms=(form,)))


def _har_entry(
    method: str,
    url: str,
    *,
    status: int = 200,
    body: str = "",
    headers: tuple[tuple[str, str], ...] = (),
    post: str | None = None,
) -> dict[str, Any]:
    response_headers = [("Content-Type", "text/html"), *headers]
    entry: dict[str, Any] = {
        "startedDateTime": "2026-09-10T10:00:00.000Z",
        "time": 5,
        "request": {"method": method, "url": url, "headers": [], "cookies": [], "queryString": []},
        "response": {
            "status": status,
            "statusText": "",
            "headers": [{"name": n, "value": v} for n, v in response_headers],
            "cookies": [],
            "content": {"mimeType": "text/html", "text": body, "size": len(body)},
        },
    }
    if post is not None:
        entry["request"]["postData"] = {"mimeType": "application/json", "text": post}
    return entry


def _login_projection(tmp_path: Path) -> dict[str, Any]:
    """endpoints_projection of a recorded login: the form page, the credential post
    answering a redirect, one redirect hop, and the landing page setting a cookie."""
    form = (
        '<form action="/session" method="post"><input type="email" name="email">'
        '<input type="password" name="password"></form>'
    )
    credentials = json.dumps({"email": "alice@example.com", "password": "x"})
    entries = [
        _har_entry("GET", "https://myshop.example/signin", body=form),
        _har_entry(
            "POST",
            "https://myshop.example/session",
            status=302,
            headers=(("Location", "/sso/callback"),),
            post=credentials,
        ),
        _har_entry(
            "GET",
            "https://myshop.example/sso/callback",
            status=302,
            headers=(("Location", "/dashboard"),),
        ),
        _har_entry(
            "GET",
            "https://myshop.example/dashboard",
            body="<p>orders</p>",
            headers=(("Set-Cookie", "s=v; Path=/"),),
        ),
    ]
    har = tmp_path / "login.har"
    har.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))
    return endpoints_projection(digest(DigestSource.from_har(har)))


class TestDigestListsMatchTheirSource:
    def test_the_auth_url_kinds_are_the_ones_a_login_produces(self, tmp_path: Path) -> None:
        """Derived from behaviour: a recorded login that walks a form page, a
        credential post, a redirect hop, and a landing that sets a cookie, digested,
        and the kinds its auth_urls hold. That recording produces every login kind,
        so digest.md's list must equal them."""
        produced = {o["kind"] for o in _login_projection(tmp_path)["login"]["auth_urls"]}
        assert produced
        assert _listed("each of kind", ". A plain form") == produced

    def test_the_endpoint_fields_are_the_projections(self) -> None:
        (endpoint, *_rest) = _projection()["endpoints"]
        assert _listed("one entry per endpoint with", ", plus a") == set(endpoint)

    def test_the_login_summary_keys_are_the_projections(self) -> None:
        text = _DIGEST_MD.read_text(encoding="utf-8")
        for key in _projection()["login"]:
            assert f"`{key}`" in text, key

    def test_the_form_keys_are_the_projections(self) -> None:
        (form,) = _projection()["login"]["forms"]
        assert _listed("`forms` holds each login form's", ". Name every") == set(form)
