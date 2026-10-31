"""The guide harness's markdown reading: code spans and fenced blocks by CommonMark.

The guide's invocation checks are only as wide as the parser that finds the
invocations. A naive backtick pairing once let one double-backtick span hide
more than half the guide's invocations while the suite still passed (#339), so
the parser is tested on its own and the guide's count has a floor.
"""

from __future__ import annotations

from tests.unit.guide_harness import GUIDE_TEXT, blocks, code_spans, fences, gp_invocations

# The number of gp invocations gp_invocations finds in docs/PLUGIN_DEVELOPMENT.md
# with the CommonMark parser (2026-10-30). Raise it when the guide gains some;
# lowering it means the guide lost some, which should be on purpose.
GUIDE_INVOCATION_FLOOR = 126


def _contents(text: str) -> list[str]:
    return [content for _line, content in code_spans(text)]


def test_a_single_backtick_span() -> None:
    assert code_spans("run `gp plugin check` now") == [(1, "gp plugin check")]


def test_double_backticks_around_a_single_backtick() -> None:
    """A run of two closes only on a run of two; the one backtick inside is content."""
    assert _contents("names hold (`` . $ ` ~ ``) and `gp version`") == [". $ ` ~", "gp version"]


def test_one_leading_and_trailing_space_is_stripped() -> None:
    assert _contents("`` `myshop_session` ``") == ["`myshop_session`"]
    assert _contents("`  two  `") == [" two "]


def test_a_space_on_one_side_only_is_kept() -> None:
    assert _contents("` a`") == [" a"]


def test_an_all_space_span_is_kept() -> None:
    assert _contents("` ` and `  `") == [" ", "  "]


def test_an_unclosed_span_is_literal_and_later_spans_still_parse() -> None:
    assert _contents("an unclosed `` run, then `gp version` and `gp --help`") == [
        "gp version",
        "gp --help",
    ]


def test_a_span_wraps_a_line_but_not_a_paragraph() -> None:
    assert code_spans("intro `gp\nobserve list` prints it") == [(1, "gp observe list")]
    assert code_spans("an open `gp\n\nobserve` here") == []


def test_an_escaped_backtick_opens_nothing() -> None:
    assert _contents(r"a \`literal and `gp version`") == ["gp version"]


def test_a_backslash_inside_a_span_is_literal() -> None:
    assert _contents(r"`foo\`bar`") == ["foo\\"]


def test_backticks_inside_a_fence_are_ignored() -> None:
    text = "before `one`\n\n```bash\ngp `odd\n```\n\nafter `two`\n"
    assert code_spans(text) == [(1, "one"), (7, "two")]


def test_a_tilde_fence_hides_its_backticks() -> None:
    text = "~~~\n`` odd\n~~~\n`two`\n"
    assert code_spans(text) == [(4, "two")]


def test_a_fence_closes_only_on_a_fence_at_least_as_long_of_the_same_character() -> None:
    text = "````bash\ngp a\n```\n~~~~\ngp b\n`````\n`after`\n"
    (fence,) = fences(text)
    assert (fence.open, fence.close, fence.language) == (1, 6, "bash")
    assert fence.lines == ("gp a", "```", "~~~~", "gp b")
    assert code_spans(text) == [(7, "after")]
    assert blocks(text, "bash") == [(1, "gp a\n```\n~~~~\ngp b\n")]


def test_an_unclosed_fence_runs_to_the_end() -> None:
    text = "`one`\n```\n`two`\n"
    assert fences(text)[0].close == 3
    assert code_spans(text) == [(1, "one")]


def test_a_backtick_line_with_a_backtick_in_its_info_string_is_prose() -> None:
    text = "``` `gp version` ```\n"
    assert fences(text) == []
    assert _contents(text) == ["`gp version`"]


def test_an_indented_fence_strips_its_indent_from_content() -> None:
    (fence,) = fences("  ```bash\n  gp a\n gp b\ngp c\n  ```\n")
    assert fence.lines == ("gp a", "gp b", "gp c")


def test_a_form_feed_and_a_line_separator_do_not_end_a_line() -> None:
    """CommonMark ends a line only at a line feed or carriage return; str.splitlines
    would also split at these, moving later line numbers and ending spans early."""
    for separator in ("\f", "\u2028"):
        text = f"one `gp{separator}version` here\n`gp --help`\n"
        assert code_spans(text) == [(1, f"gp{separator}version"), (2, "gp --help")]


def test_carriage_returns_end_lines() -> None:
    assert code_spans("`a`\r\n`b`\r`c`\n`d`") == [(1, "a"), (2, "b"), (3, "c"), (4, "d")]


def test_a_form_feed_does_not_split_a_fence() -> None:
    (fence,) = fences("```bash\ngp a\f```\n```\n`after`\n")
    assert (fence.open, fence.close, fence.lines) == (1, 3, ("gp a\f```",))


def test_the_guide_has_at_least_the_invocations_it_had() -> None:
    """A parsing regression shrinks the checked set without failing any one check;
    the floor makes it fail."""
    found = len(gp_invocations(GUIDE_TEXT))
    assert found >= GUIDE_INVOCATION_FLOOR, (
        f"found {found} gp invocations in the guide, fewer than {GUIDE_INVOCATION_FLOOR}"
    )


def test_a_double_backtick_span_does_not_hide_later_invocations() -> None:
    """The #339 regression: an odd backtick count inside a double-backtick span
    used to re-pair every later span in the document."""
    title, rest = GUIDE_TEXT.split("\n", 1)
    edited = f"{title}\n\nCookie names may hold (`` . $ ! * ^ ` ~ ``).\n{rest}"
    before = gp_invocations(GUIDE_TEXT)
    after = gp_invocations(edited)
    assert len(after) == len(before)
    assert [invocation for _line, invocation in after] == [
        invocation for _line, invocation in before
    ]
