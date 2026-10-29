"""Reading recorded CSS selectors: the digest's and the page listener's (#267, part 3)."""

from __future__ import annotations

import pytest

from graftpunk.har.selectors import css_unescape, selector_holds_an_id, selector_keys


class TestCssUnescape:
    @pytest.mark.parametrize(
        ("escaped", "plain"),
        [
            ("\\31 st\\:remember", "1st:remember"),
            ('a\\"b', 'a"b'),
            ("\\0 x", "�x"),
            ("\\110000", "�"),
            ("plain", "plain"),
        ],
    )
    def test_escapes_are_undone(self, escaped: str, plain: str) -> None:
        assert css_unescape(escaped) == plain


class TestSelectorKeys:
    def test_the_listener_s_and_the_digest_s_id_spellings_agree(self) -> None:
        assert selector_keys("#\\31 st\\:remember") == selector_keys('[id="1st:remember"]')
        assert selector_keys("#m-email") == {("id", "m-email")}

    def test_a_scoped_name_is_its_last_compound(self) -> None:
        scoped = (
            'form:not([action]) input[name="email"], form[action=""] input[name="email"], '
            'form[action^="#"] input[name="email"]'
        )
        assert selector_keys(scoped) == {("name", "input", "email")}
        assert selector_keys('input[name="email"]') == {("name", "input", "email")}
        assert selector_keys('form[action="/a b"] > input[name="e\\"x"]') == {
            ("name", "input", 'e"x')
        }

    def test_anything_else_is_its_text(self) -> None:
        assert selector_keys("header:nth-of-type(1)  >a:nth-of-type(2)") == {
            ("text", "header:nth-of-type(1) > a:nth-of-type(2)")
        }
        assert selector_keys('input[type="password"]') == {("text", 'input[type="password"]')}
        assert selector_keys("") == frozenset()


class TestSelectorHoldsAnId:
    @pytest.mark.parametrize(
        "selector",
        [
            "#user_40912873",
            "#\\34 0912873",
            'input[name="alice@example.com"]',
            'button[name="cus_NffrFeUfNV2Hib"]',
        ],
    )
    def test_an_account_value_anywhere_is_caught(self, selector: str) -> None:
        assert selector_holds_an_id(selector)

    @pytest.mark.parametrize(
        "selector",
        [
            "#signin-link",
            'input[name="password"]',
            "body:nth-of-type(1) > div:nth-of-type(12) > a:nth-of-type(3)",
            "#\\31 st\\:remember",
        ],
    )
    def test_an_ordinary_selector_is_kept(self, selector: str) -> None:
        assert not selector_holds_an_id(selector)


class TestLinearMatching:
    def test_many_escapes_that_do_not_match_finish_at_once(self) -> None:
        """Review round 1 (M6): overlapping escape alternatives backtracked
        exponentially when the match failed (0.146 s at 13 escapes, about 3x a step)."""
        import time

        crafted = "#" + "\\31" * 60 + "!"
        started = time.perf_counter()
        keys = selector_keys(crafted)
        assert time.perf_counter() - started < 0.5
        assert keys == {("text", crafted)}
        assert selector_keys("#\\31 \\32 3") == {("id", "123")}
