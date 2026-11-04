"""graftpunk.page_values reads a page's meta tags and hidden inputs by the WHATWG
tokenizer rules, with the same answer on every Python release (#369)."""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from graftpunk.page_values import page_values, skipped_region_mentioning

_REAL = '<input type="hidden" name="csrf" value="realValue12345">'
_DECOY = '<input type="hidden" name="csrf" value="decoyValue1234">'


def _input(html: str) -> str | None:
    return page_values(html).get(("hidden_input", "csrf"))


def _meta_content(raw: str) -> str | None:
    return page_values(f'<meta name="csrf" content="{raw}">').get(("meta", "csrf"))


@pytest.mark.parametrize(
    ("raw", "read"),
    [
        # A named reference without ";" followed by a letter, a digit, or "=" stays
        # literal in an attribute (13.2.5.78); html.parser before CPython 3.13.10
        # decoded these.
        ("abc&amp123", "abc&amp123"),
        ("abc&copy=1", "abc&copy=1"),
        ("abc&copyright12", "abc&copyright12"),
        ("&notit;", "&notit;"),
        ("a&ampb=c&amp;d", "a&ampb=c&d"),
        # The same strings written escaped decode to the text above.
        ("&amp;amp123", "&amp123"),
        ("&amp;copy=1", "&copy=1"),
        ("abc&amp;copyright12", "abc&copyright12"),
        ("&amp;#43;", "&#43;"),
        ("&amp;amp;", "&amp;"),
        # Decoded: with ";", or without ";" before anything else.
        ("&#43;", "+"),
        ("&#x2B;", "+"),
        ("&amp;", "&"),
        ("&AMP;z", "&z"),
        ("&notin;", "∉"),
        ("abc&copy", "abc©"),
        ("abc&copy.", "abc©."),
        # Numeric references a browser replaces (13.2.5.84).
        ("&#0;x", "�x"),
        ("&#x80;x", "€x"),
        ("&#xD800;x", "�x"),
        ("&#x110000;x", "�x"),
        ("&#" + "9" * 5000 + ";x", "�x"),
        # No digits, or no name in the table: literal.
        ("&#x;", "&#x;"),
        ("&#;", "&#;"),
        ("&zzzz;", "&zzzz;"),
        ("a & b", "a & b"),
    ],
)
def test_attribute_character_references(raw: str, read: str) -> None:
    assert _meta_content(raw) == read


@pytest.mark.parametrize(
    "page",
    [
        # Raw text and RCDATA: only the element's own end tag closes it.
        f"<script>var s = '{_DECOY}';</script>{_REAL}",
        f"<style>/* {_DECOY} */</style>{_REAL}",
        f"<textarea>{_DECOY}</textarea>{_REAL}",
        f"<title>{_DECOY}</title>{_REAL}",
        f"<xmp>{_DECOY}</xmp>{_REAL}",
        f"<iframe>{_DECOY}</iframe>{_REAL}",
        f"<noembed>{_DECOY}</noembed>{_REAL}",
        f"<noframes>{_DECOY}</noframes>{_REAL}",
        f"<noscript>{_DECOY}</noscript>{_REAL}",
        f"<TEXTAREA>{_DECOY}</TextArea >{_REAL}",
        f"<textarea><script></textarea>{_REAL}",
        f"<textarea></textareax>{_DECOY}</textarea>{_REAL}",
        f"<title><!--</title>{_REAL}",
        # A self-closing slash on a non-void element is ignored: it stays open.
        f"<textarea/>{_DECOY}</textarea>{_REAL}",
        f'<script src="a.js"/>{_DECOY}</script>{_REAL}',
        f"<style/>{_DECOY}</style>{_REAL}",
        f"<template/>{_DECOY}</template>{_REAL}",
        # Template content, to its matching end tag; a stray end tag inside does
        # not end it.
        f"<template>{_DECOY}</template>{_REAL}",
        f"<template></textarea>{_DECOY}</template>{_REAL}",
        f"<template></style>{_DECOY}</template>{_REAL}",
        f"<template><template>{_DECOY}</template>{_DECOY}</template>{_REAL}",
        f"<template><textarea></template>{_DECOY}</textarea></template>{_REAL}",
        f"</template>{_REAL}{_DECOY}",
        # Comments, including the abrupt and "--!>" ends.
        f"<!-- {_DECOY} -->{_REAL}",
        f"<!-->{_REAL}",
        f"<!--->{_REAL}",
        f"<!-- {_DECOY} --!>{_REAL}",
        f"<!-- {_DECOY} -- >{_DECOY}-->{_REAL}",
        f"<!x {_DECOY[1:]}{_REAL}",
        f"<? {_DECOY[1:]}{_REAL}",
        f"</ {_DECOY[1:]}{_REAL}",
        f"<!DOCTYPE html>{_REAL}",
        f"</>{_REAL}",
        f"<1 <= 2>{_REAL}",
        f"{_REAL}</",
        f"{_REAL}</div",
        f"{_REAL}</ x",
        f"{_REAL}<!x",
        f"{_REAL}<?x",
        # Script escapes: inside "<!--", a "<script>" makes "</script>" not end it.
        f"<script><!-- <script> </script> {_DECOY} --></script>{_REAL}",
        f"<script><!-- x </script>{_REAL}",
        f"<script><!--></script>{_REAL}",
        f"<script>a <script> </script>{_REAL}",
        # A tag a browser reads where a regex might not.
        f"<div data-x='a>b {_DECOY}'>{_REAL}",
        f"<a href=x title='{_DECOY}'>{_REAL}",
    ],
)
def test_the_real_input_and_never_the_decoy(page: str) -> None:
    assert _input(page) == "realValue12345"


@pytest.mark.parametrize(
    ("page", "read"),
    [
        ('<input type="hidden" name="csrf" value="abc', None),  # ends inside the tag
        ('<input type="hidden" name="csrf" value=abc', None),
        (f"<script>{_REAL}", None),  # the script runs to the end
        (f"<!-- {_REAL}", None),
        (f"<plaintext>{_REAL}", None),
        (f"<template>{_REAL}", None),
        ('<input type=" hidden" name="csrf" value="abcdefgh12">', None),  # a text input
        ("<INPUT value='AbC123xyz' NAME=csrf Type=HIDDEN/>", None),  # type "HIDDEN/"
        ("<input type=hidden name=csrf value=abcdefgh12/>", "abcdefgh12/"),
        ("<input type=HiDdEn name=csrf value=abcdefgh12 />", "abcdefgh12"),
        ('<input type="hidden" name="csrf" value="ab\r\ncd\ref">', "ab\ncd\nef"),
        ('<input type="hidden" name="csrf" value="ab\0cd">', "ab�cd"),
        ('<input type="hidden" name="csrf" value="first1234" value="second12">', "first1234"),
        ('<input type="hidden" name="csrf" value="">' + _REAL, "realValue12345"),
        ('<input type="hidden"name="csrf"value="noSpaces1234">', "noSpaces1234"),
        ('<input type = "hidden" name = csrf value = "spaced1234">', "spaced1234"),
        ('<input/type="hidden"/name="csrf"/value="slashes1234">', "slashes1234"),
        ('<input type="hidden" name="csrf" value="x"y z="1">', "x"),
        ('<input =x type="hidden" name="csrf" value="eqName1234">', "eqName1234"),
    ],
)
def test_edge_shapes(page: str, read: str | None) -> None:
    assert _input(page) == read


def test_names_are_kept_as_written_and_attribute_names_lower_only_ascii() -> None:
    values = page_values(
        '<META NAME="CSRF-Token" CONTENT="metaValue1234">'
        '<input TYPE="hidden" NAME="Csrf" VALUE="inputValue1234">'
        '<meta İname="other" name="x" content="dottedName12">'
    )
    assert dict(values) == {
        ("meta", "CSRF-Token"): "metaValue1234",
        ("hidden_input", "Csrf"): "inputValue1234",
        ("meta", "x"): "dottedName12",
    }


def test_the_result_is_read_only_and_parsed_once_per_page_text() -> None:
    page = f"<html><body>{_REAL}</body></html>"
    first = page_values(page)
    assert page_values("".join(list(page))) is first  # an equal text, another object
    with pytest.raises(TypeError):
        first[("meta", "x")] = "y"  # type: ignore[index]


@pytest.mark.parametrize(
    ("page", "where"),
    [
        (
            "<script>document.head.insertAdjacentHTML('beforeend', "
            '\'<meta name="csrf" content="scriptValue12">\');</script>',
            "script",
        ),
        ('<script>var t = "<meta name=\\"csrf\\" content=\\"x\\">";</script>', "script"),
        ('<!-- <meta name="csrf" content="commentVal12"> -->', "comment"),
        ('<template><meta name="csrf" content="tmplValue123"></template>', "template"),
        ('<noscript><meta name="csrf" content="noscript1234"></noscript>', "noscript"),
        ('<!x <meta name="csrf" content="bogusValue12">', "comment"),
        ('</ <meta name="csrf" content="bogusValue12">', "comment"),
        ('<template><template></template><meta name="csrf" content="x">', "template"),
        ('<script>var t = \'<meta name="csrf-token" content="x">\';</script>', None),
        ('<meta name="other" content="x">', None),
    ],
)
def test_skipped_region_mentioning_names_where_a_tag_was_not_read(
    page: str, where: str | None
) -> None:
    assert page_values(page).get(("meta", "csrf")) is None
    assert skipped_region_mentioning(page, "meta", "csrf") == where


def _seconds(build: Callable[[int], str], n: int) -> float:
    """The least CPU time of five parses of a page of size *n* (a fresh text each
    time, so the cache does not answer). CPU time and the minimum keep a busy
    machine (other xdist workers) from reading as a slow parse."""
    best = float("inf")
    for attempt in range(5):
        page = build(n) + f"<!--{attempt}-->"
        start = time.process_time()
        page_values(page)
        best = min(best, time.process_time() - start)
    return best


_ROW = '<tr><td class="c">cell &amp; text</td><td><a href="/x?a=1&b=2">link</a></td></tr>\n'


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda n: f"<table>{_ROW * (n // len(_ROW))}</table>{_REAL}", id="page"),
        pytest.param(lambda n: "</" * (n // 2), id="lt-slash"),
        pytest.param(lambda n: "<a" * (n // 2), id="lt-a"),
        pytest.param(lambda n: "<a " * (n // 3), id="lt-a-space"),
        pytest.param(lambda n: "<!" * (n // 2), id="lt-bang"),
        pytest.param(lambda n: "<script>" + "</scrip" * (n // 7), id="script-almost-end"),
        pytest.param(lambda n: "<script><!--" + "<script>" * (n // 8), id="script-escapes"),
        pytest.param(lambda n: "<template>" * (n // 10), id="nested-templates"),
        pytest.param(
            lambda n: '<input type=hidden name=csrf value="' + "&a" * (n // 2) + '">',
            id="references",
        ),
    ],
)
def test_cost_grows_linearly(build: Callable[[int], str]) -> None:
    """Eight times the text costs about eight times the time; a quadratic parse
    would cost sixty-four. The bound of sixteen leaves room for timer noise."""
    small = _seconds(build, 250_000)
    large = _seconds(build, 2_000_000)
    assert large < 16 * max(small, 1e-3), (small, large)
