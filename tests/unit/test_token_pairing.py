"""#369: a request header pairs with the source whose value it carried, by value: a
hidden input on a page loaded before it (in a form or not), a meta tag, or a cookie
the same request sent. A cookie with a similar name whose values never matched does
not win, and no value is ever printed."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from graftpunk.devtools.scaffold.render import ScaffoldSpec, render
from graftpunk.exceptions import TokenPatternMismatchError
from graftpunk.har.digest import DigestSource, RunDigest, digest
from graftpunk.har.naming import capture_slug
from graftpunk.har.report import render_json, render_markdown
from graftpunk.har.token_pairing import MIN_PAIRING_VALUE_LENGTH, TokenPairing
from graftpunk.testing import FixtureSession
from graftpunk.tokens import Token, TokenConfig, extract_token, prepare_session, read_page_token
from tests.unit.test_har_digest import _entry, _write_har

_HOST = "https://myshop.example.com"
_API = f"{_HOST}/GraphQLController"
_PAGE_TOKENS = ("pgTokAAAA1111aaaa", "pgTokBBBB2222bbbb", "pgTokCCCC3333cccc", "pgTokDDDD4444dddd")
_BODY = json.dumps(
    {"operationName": "user", "variables": {}, "query": "query user { user { id } }"}
)


def _shell(token: str, *, in_form: bool = False) -> str:
    """The #365 shape: a React shell whose CSRF hidden input sits outside any form
    beside a bootstrap-data input, or inside one when *in_form*."""
    inputs = (
        f'<input type="hidden" name="csrf" value="{token}">'
        '<input type="hidden" name="js_bootstrap_data" value="{&quot;userID&quot;:null}">'
    )
    if in_form:
        inputs = f'<form action="/search">{inputs}<input name="q"></form>'
    return f'<!DOCTYPE html><html><body>{inputs}<div id="root"></div></body></html>'


class _Run:
    """A recording built in order, the rotating cookie's value moving on every
    response as the #369 site's did."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self._cookie = 0

    def _at(self) -> str:
        return f"2026-11-04T10:{len(self.entries) // 60:02d}:{len(self.entries) % 60:02d}.000Z"

    def _rotate(self) -> tuple[str, list[str]]:
        sent = f"x-csrf-v1=rot{self._cookie:011d}"
        self._cookie += 1
        return sent, [f"x-csrf-v1=rot{self._cookie:011d}; Path=/; HttpOnly"]

    def page(self, path: str, body: str) -> None:
        sent, set_cookies = self._rotate()
        self.entries.append(
            _entry(
                "GET",
                f"{_HOST}{path}",
                content_type="text/html; charset=utf-8",
                body=body,
                request_headers={"Cookie": sent},
                set_cookies=set_cookies,
                started=self._at(),
            )
        )

    def post(self, headers: dict[str, str]) -> None:
        sent, set_cookies = self._rotate()
        self.entries.append(
            _entry(
                "POST",
                _API,
                body='{"data":{"user":null}}',
                request_headers={**headers, "Cookie": sent},
                post_data=_BODY,
                set_cookies=set_cookies,
                started=self._at(),
            )
        )

    def digest(self, tmp_path: Path) -> RunDigest:
        return digest(DigestSource.from_har(_write_har(tmp_path, self.entries)))


def _plugin(d: RunDigest) -> str:
    spec = ScaffoldSpec(
        name="myshop", mode="new_project", backend="nodriver", base_url=_HOST, digest=d
    )
    return render(spec)["src/graftpunk_myshop/plugin.py"]


def _outputs(d: RunDigest) -> str:
    return "\n".join((render_markdown(d), render_json(d), _plugin(d)))


def _token_config(plugin_code: str) -> TokenConfig | None:
    namespace: dict[str, object] = {}
    exec(compile(plugin_code, "plugin.py", "exec"), namespace)  # noqa: S102
    return namespace["MyshopPlugin"].token_config  # type: ignore[attr-defined]


def _synthetic_run() -> _Run:
    """Four full page loads, alternating / and /log-in, each with its own hidden
    csrf value, and two GraphQL posts after each carrying that value in x-csrf."""
    run = _Run()
    for load, token in enumerate(_PAGE_TOKENS):
        run.page("/" if load % 2 == 0 else "/log-in", _shell(token))
        run.post({"x-csrf": token})
        run.post({"x-csrf": token})
    return run


def test_the_header_pairs_with_the_hidden_input_and_not_the_rotating_cookie(
    tmp_path: Path,
) -> None:
    d = _synthetic_run().digest(tmp_path)

    assert d.token_pairs == (
        TokenPairing(
            header="x-csrf", kind="hidden_input", name="csrf", page="/", matched=8, sent=8
        ),
    )
    assert ("cookie", "x-csrf-v1") in {(t.kind, t.name) for t in d.tokens}
    plugin = _plugin(d)
    assert (
        "    token_config = TokenConfig(\n"
        "        tokens=[\n"
        "            Token.from_hidden_input(\n"
        '                name="csrf",\n'
        '                header="x-csrf",\n'
        "            ),\n"
        "        ]\n"
        "    )\n"
    ) in plugin
    assert "from_cookie" not in plugin
    assert "from_meta_tag" not in plugin
    assert "# GP-FILL: unpaired token candidate: cookie 'x-csrf-v1'" in plugin
    assert "unpaired token candidate: header" not in plugin
    assert "unpaired token candidate: hidden_input" not in plugin
    markdown = render_markdown(d)
    assert (
        "- header `x-csrf` carried the value of hidden input `csrf` on GET / on 8 of 8 requests"
        in markdown
    )
    rendered = json.loads(render_json(d))
    assert rendered["token_pairs"] == [
        {
            "header": "x-csrf",
            "kind": "hidden_input",
            "name": "csrf",
            "page": "/",
            "matched": 8,
            "sent": 8,
        }
    ]
    text = _outputs(d)
    for token in _PAGE_TOKENS:
        assert token not in text
    assert "rot0" not in text


def test_a_hidden_input_inside_a_form_pairs_too(tmp_path: Path) -> None:
    run = _Run()
    run.page("/", _shell(_PAGE_TOKENS[0], in_form=True))
    run.post({"x-csrf": _PAGE_TOKENS[0]})
    d = run.digest(tmp_path)
    assert [(p.kind, p.name) for p in d.token_pairs] == [("hidden_input", "csrf")]


def test_a_page_other_than_the_root_is_the_page_url(tmp_path: Path) -> None:
    run = _Run()
    run.page("/log-in", _shell(_PAGE_TOKENS[0]))
    run.post({"x-csrf": _PAGE_TOKENS[0]})
    plugin = _plugin(run.digest(tmp_path))
    assert (
        'name="csrf",\n                header="x-csrf",\n                page_url="/log-in",'
        in (plugin)
    )


def test_each_request_is_compared_with_the_page_loaded_before_it(tmp_path: Path) -> None:
    """A header carrying the previous load's value after a new load does not match:
    only the latest value of each input counts."""
    run = _Run()
    run.page("/", _shell(_PAGE_TOKENS[0]))
    run.post({"x-csrf": _PAGE_TOKENS[0]})
    run.page("/", _shell(_PAGE_TOKENS[1]))
    run.post({"x-csrf": _PAGE_TOKENS[0]})
    run.post({"x-csrf": _PAGE_TOKENS[1]})
    (pair,) = run.digest(tmp_path).token_pairs
    assert (pair.matched, pair.sent) == (2, 3)


def test_a_rotating_cookie_named_as_the_header_does_not_pair_by_name(tmp_path: Path) -> None:
    """A cookie named exactly as the header pairs by name only when the recording
    never showed the header carrying another value: here the hidden input matched,
    and with no page, nothing did, so it stays unpaired."""
    run = _Run()
    for token in _PAGE_TOKENS[:2]:
        run.page("/", _shell(token))
        run.post({"x-csrf-v1": token})
    d = run.digest(tmp_path)
    assert [(p.kind, p.name) for p in d.token_pairs] == [("hidden_input", "csrf")]
    assert "from_cookie" not in _plugin(d)

    bare = _Run()
    bare.post({"x-csrf-v1": _PAGE_TOKENS[0]})
    bare.post({"x-csrf-v1": _PAGE_TOKENS[1]})
    d = bare.digest(tmp_path)
    assert d.token_pairs == ()
    plugin = _plugin(d)
    assert "\n    token_config = TokenConfig(" not in plugin
    assert "unpaired token candidate: header 'x-csrf-v1'" in plugin
    assert "unpaired token candidate: cookie 'x-csrf-v1'" in plugin


def test_no_match_stays_unpaired(tmp_path: Path) -> None:
    run = _Run()
    run.page("/", _shell(_PAGE_TOKENS[0]))
    run.post({"x-csrf": "someOtherValue123"})
    d = run.digest(tmp_path)
    assert d.token_pairs == ()
    plugin = _plugin(d)
    assert "\n    token_config = TokenConfig(" not in plugin
    assert "# GP-FILL: unpaired token candidate: header 'x-csrf'" in plugin
    assert "carried the value" not in render_markdown(d)
    assert "token_pairs" not in json.loads(render_json(d))


def test_a_short_value_is_no_evidence(tmp_path: Path) -> None:
    short = "a" * (MIN_PAIRING_VALUE_LENGTH - 1)
    run = _Run()
    run.page("/", _shell(short))
    run.post({"x-csrf": short})
    assert run.digest(tmp_path).token_pairs == ()


def test_a_meta_tag_whose_content_the_header_carries_pairs_whatever_its_name(
    tmp_path: Path,
) -> None:
    run = _Run()
    run.page(
        "/account",
        f'<html><head><meta name="csrf-token" content="{_PAGE_TOKENS[0]}"></head></html>',
    )
    run.post({"X-CSRF-Token": _PAGE_TOKENS[0]})
    d = run.digest(tmp_path)
    assert [(p.header, p.kind, p.name, p.page) for p in d.token_pairs] == [
        ("X-CSRF-Token", "meta", "csrf-token", "/account")
    ]
    plugin = _plugin(d)
    assert (
        "Token.from_meta_tag(\n"
        '                name="csrf-token",\n'
        '                header="X-CSRF-Token",\n'
        '                page_url="/account",\n'
    ) in plugin
    assert "- header `X-CSRF-Token` carried the value of meta tag `csrf-token` on GET /account" in (
        render_markdown(d)
    )


def test_a_cookie_whose_value_the_header_carries_keeps_from_cookie(tmp_path: Path) -> None:
    """The double-submit pattern: the header equals a cookie the same request sent,
    which also equals a hidden input on the page. The cookie wins the tie, as the
    name pairing gave before."""
    entries = [
        _entry(
            "GET",
            f"{_HOST}/",
            content_type="text/html",
            body=_shell(_PAGE_TOKENS[0]),
            started="2026-11-04T10:00:00.000Z",
        ),
        _entry(
            "POST",
            f"{_HOST}/api/save",
            request_headers={
                "X-XSRF-TOKEN": _PAGE_TOKENS[0],
                "Cookie": f"XSRF-TOKEN={_PAGE_TOKENS[0]}",
            },
            post_data="{}",
            started="2026-11-04T10:00:01.000Z",
        ),
    ]
    d = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
    assert [(p.kind, p.name) for p in d.token_pairs] == [("cookie", "XSRF-TOKEN")]
    plugin = _plugin(d)
    assert 'Token.from_cookie(\n                cookie_name="XSRF-TOKEN",' in plugin
    assert "page_url" not in plugin


def test_the_generated_token_config_reads_the_hidden_input_through_fixture_session(
    tmp_path: Path,
) -> None:
    """The generated plugin's token_config, run by prepare_session against a
    FixtureSession answering GET / with the recorded shell, extracts the value."""
    d = _synthetic_run().digest(tmp_path)
    config = _token_config(_plugin(d))
    assert config is not None
    assert tuple(config.tokens) == (Token.from_hidden_input(name="csrf", header="x-csrf"),)

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / f"{capture_slug('GET', '/')}.html").write_text(_shell(_PAGE_TOKENS[3]))
    session = FixtureSession(fixtures)
    prepare_session(session, config, _HOST)
    assert session._gp_csrf_tokens == {"x-csrf": _PAGE_TOKENS[3]}  # type: ignore[attr-defined]
    assert [(r.method, r.path) for r in session.requests] == [("GET", "/")]


def _value(html: str, name: str = "csrf", *, kind: str = "hidden_input") -> str | None:
    """What the runtime reads: the generated Token through read_page_token."""
    if kind == "meta":
        return read_page_token(Token.from_meta_tag(name=name, header="x-csrf"), html)
    return read_page_token(Token.from_hidden_input(name=name, header="x-csrf"), html)


class TestFromHiddenInput:
    def test_any_attribute_order_and_case(self) -> None:
        assert _value('<input type="hidden" name="csrf" value="AbC123xyz">') == "AbC123xyz"
        assert _value("<INPUT value='AbC123xyz' NAME=csrf Type=HIDDEN />") == "AbC123xyz"
        # Unquoted, the "/" is part of the value (WHATWG HTML 13.2.5.38), so a browser
        # reads type "HIDDEN/" and this is no hidden input.
        assert _value("<INPUT value='AbC123xyz' NAME=csrf Type=HIDDEN/>") is None

    def test_another_input_does_not_match(self) -> None:
        assert _value('<input type="hidden" name="csrf_other" value="no">') is None
        assert _value('<input type="text" name="csrf" value="no">') is None
        assert _value('<input type="hidden" data-name="csrf" value="no">') is None
        assert _value('<input type="hidden" name="csrf" value="">') is None

    def test_the_named_input_among_others(self) -> None:
        html = (
            '<input type="hidden" name="js_bootstrap_data" value="x">'
            '<input type="hidden" name="csrf" value="Z9z9Z9z9">'
        )
        assert _value(html) == "Z9z9Z9z9"

    def test_a_name_with_pattern_characters_is_literal(self) -> None:
        assert _value('<input type="hidden" name="a.b" value="v1v1v1v1">', "a.b") == "v1v1v1v1"
        assert _value('<input type="hidden" name="aXb" value="v1v1v1v1">', "a.b") is None

    def test_it_is_a_page_token_with_the_meta_tag_defaults(self) -> None:
        token = Token.from_hidden_input(name="csrf", header="x-csrf", page_url="/log-in")
        meta = Token.from_meta_tag(name="csrf", header="x-csrf", page_url="/log-in")
        assert (
            token.name,
            token.source,
            token.page_url,
            token.cache_duration,
            token.extraction,
        ) == (
            meta.name,
            meta.source,
            meta.page_url,
            meta.cache_duration,
            meta.extraction,
        )
        assert (token.element, token.element_name, token.pattern) == ("hidden_input", "csrf", None)

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"source": "cookie", "cookie_name": "c"}, "requires source='page'"),
            ({"pattern": "(x)"}, "no pattern"),
            ({"element_name": ""}, "an element_name"),
        ],
    )
    def test_an_element_token_is_a_page_token_without_a_pattern(
        self, overrides: dict[str, str], message: str
    ) -> None:
        kwargs: dict[str, Any] = {"source": "page", "element": "meta", "element_name": "csrf"}
        with pytest.raises(ValueError, match=message):
            Token(name="x-csrf", **{**kwargs, **overrides})

    def test_an_unknown_element_is_refused(self) -> None:
        kwargs: dict[str, Any] = {"source": "page", "element": "script", "element_name": "csrf"}
        with pytest.raises(ValueError, match="Token.element must be"):
            Token(name="x-csrf", **kwargs)

    def test_an_absent_element_names_what_it_read_in_http_mode(self, tmp_path: Path) -> None:
        session = FixtureSession(tmp_path)
        for token, target in (
            (
                Token.from_hidden_input(name="csrf", header="x-csrf", extraction="http"),
                '<input type="hidden" name="csrf"> not found',
            ),
            (
                Token.from_meta_tag(name="csrf", header="x-csrf", extraction="http"),
                '<meta name="csrf"> not found',
            ),
        ):
            (tmp_path / f"{capture_slug('GET', '/')}.html").write_text("<html></html>")
            with pytest.raises(TokenPatternMismatchError, match=re.escape(target)):
                extract_token(session, token, _HOST)


# Each page shape the reviewers probed: the kind of the csrf source, the page, the
# value a browser (and so the runtime) reads, and a decoy value the page also holds
# that neither the digest nor the runtime may read.
_SHAPES: dict[str, tuple[str, str, str, str | None]] = {
    "meta content before name": (
        "meta",
        '<meta content="metaValAAAA1111" name="csrf">',
        "metaValAAAA1111",
        None,
    ),
    "meta extra attribute between": (
        "meta",
        '<meta name="csrf" data-turbo="1" content="metaValBBBB2222">',
        "metaValBBBB2222",
        None,
    ),
    "meta upper case": (
        "meta",
        '<META NAME="csrf" CONTENT="metaValCCCC3333">',
        "metaValCCCC3333",
        None,
    ),
    "meta character reference": (
        "meta",
        '<meta name="csrf" content="metaVal&#43;DDDD&amp;4444">',
        "metaVal+DDDD&4444",
        None,
    ),
    "input character reference": (
        "hidden_input",
        '<input type="hidden" name="csrf" value="abcd&#43;efgh&#43;ijkl">',
        "abcd+efgh+ijkl",
        None,
    ),
    "input upper case": (
        "hidden_input",
        '<INPUT TYPE="HIDDEN" VALUE="inputValEEEE5555" NAME="csrf">',
        "inputValEEEE5555",
        None,
    ),
    "duplicate inputs, first wins": (
        "hidden_input",
        '<form><input type="hidden" name="csrf" value="formOneAAAA1111"></form>'
        '<input type="hidden" name="csrf" value="shellBBBB2222xx">',
        "formOneAAAA1111",
        "shellBBBB2222xx",
    ),
    "duplicate metas, first wins": (
        "meta",
        '<meta name="csrf" content="metaFirst111111"><meta name="csrf" content="metaSecond22222">',
        "metaFirst111111",
        "metaSecond22222",
    ),
    "input inside a script": (
        "hidden_input",
        '<script>var s=\'<input type="hidden" name="csrf" value="scriptVal1111">\';</script>'
        '<input type="hidden" name="csrf" value="realValue11111">',
        "realValue11111",
        "scriptVal1111",
    ),
    "input inside a comment": (
        "hidden_input",
        '<!-- <input type="hidden" name="csrf" value="commentVal111"> -->'
        '<input type="hidden" name="csrf" value="realValue22222">',
        "realValue22222",
        "commentVal111",
    ),
    "input inside a template": (
        "hidden_input",
        '<template><input type="hidden" name="csrf" value="templateVal11"></template>'
        '<input type="hidden" name="csrf" value="realValue33333">',
        "realValue33333",
        "templateVal11",
    ),
    "input inside a textarea": (
        "hidden_input",
        '<textarea><input type="hidden" name="csrf" value="textareaVal11"></textarea>'
        '<input type="hidden" name="csrf" value="realValue44444">',
        "realValue44444",
        "textareaVal11",
    ),
    "meta inside a style and a comment": (
        "meta",
        '<style>/* <meta name="csrf" content="styleVal11111"> */</style>'
        '<!-- <meta name="csrf" content="commentMeta11"> -->'
        '<meta name="csrf" content="realMeta555555">',
        "realMeta555555",
        "styleVal11111",
    ),
    "a > inside an earlier quoted attribute": (
        "hidden_input",
        '<input data-x="a>b" type="hidden" name="csrf" value="abcdefgh12">',
        "abcdefgh12",
        None,
    ),
}


@pytest.mark.parametrize("shape", list(_SHAPES))
def test_the_digest_pairs_the_value_the_generated_token_reads(tmp_path: Path, shape: str) -> None:
    """The digest and the runtime read a page with one reader (graftpunk.page_values):
    a header carrying the value a browser reads pairs, and the generated Token, run
    through prepare_session against a FixtureSession serving that page, sends that
    value."""
    kind, html, value, _decoy = _SHAPES[shape]
    page = f"<!DOCTYPE html><html><head></head><body>{html}</body></html>"
    run = _Run()
    run.page("/", page)
    run.post({"x-csrf-token": value})
    d = run.digest(tmp_path)
    assert [(p.kind, p.name, p.page) for p in d.token_pairs] == [(kind, "csrf", "/")]
    assert _value(page, kind=kind) == value

    config = _token_config(_plugin(d))
    assert config is not None
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / f"{capture_slug('GET', '/')}.html").write_text(page)
    session = FixtureSession(fixtures)
    prepare_session(session, config, _HOST)
    assert session._gp_csrf_tokens == {"x-csrf-token": value}  # type: ignore[attr-defined]


@pytest.mark.parametrize("shape", [name for name, shape in _SHAPES.items() if shape[3]])
def test_a_decoy_value_the_runtime_does_not_read_never_pairs(tmp_path: Path, shape: str) -> None:
    """A header carrying a value the runtime would not read (a later duplicate, or a
    value inside a script, comment, template, textarea, or style) is not paired with
    that source, which the recording shows compared and unmatched."""
    kind, html, _read, decoy = _SHAPES[shape]
    assert decoy is not None
    page = f"<html><body>{html}</body></html>"
    run = _Run()
    run.page("/", page)
    run.post({"x-csrf-token": decoy})
    d = run.digest(tmp_path)
    assert d.token_pairs == ()
    assert ("x-csrf-token", kind, "csrf") in d.token_mismatches
    assert _value(page, kind=kind) != decoy


def _page_mismatches(d: RunDigest) -> list[tuple[str, str, str]]:
    """The meta and hidden input mismatches; _Run's rotating cookie adds its own."""
    return [m for m in d.token_mismatches if m[1] != "cookie"]


_META_PAGE = '<html><head><meta name="x-csrf-token" content="{}"></head></html>'


def test_a_page_at_a_templated_path_replaces_the_value_and_keeps_the_name_pairing(
    tmp_path: Path,
) -> None:
    """GET / carries the meta's value A; GET /orders/123 (a templated path, which
    no Token can name) carries B; the posts send B. B is the latest value, so the
    meta is not shown unmatched, and the 1.39.0 name pairing is written again."""
    run = _Run()
    run.page("/", _META_PAGE.format("metaValueAAAA1111"))
    run.page("/orders/123", _META_PAGE.format("metaValueBBBB2222"))
    run.post({"x-csrf-token": "metaValueBBBB2222"})
    run.post({"x-csrf-token": "metaValueBBBB2222"})
    d = run.digest(tmp_path)
    assert d.token_pairs == ()
    assert _page_mismatches(d) == []
    plugin = _plugin(d)
    assert (
        "            Token.from_meta_tag(\n"
        '                name="x-csrf-token",\n'
        '                header="x-csrf-token",\n'
        "            ),\n"
    ) in plugin
    assert "unpaired token candidate: meta" not in plugin


@pytest.mark.parametrize(
    ("method", "url", "status"),
    [
        ("GET", "https://www.myshop.example.com/", 200),  # another first-party host
        ("GET", f"{_HOST}/missing", 404),  # an error page whose body was kept
        ("POST", f"{_HOST}/search", 200),  # a POST answered with a page
    ],
)
def test_a_later_page_no_token_can_fetch_still_holds_the_latest_value(
    tmp_path: Path, method: str, url: str, status: int
) -> None:
    entries = [
        _entry(
            "GET",
            f"{_HOST}/",
            content_type="text/html",
            body=_META_PAGE.format("metaValueAAAA1111"),
            started="2026-11-04T10:00:00.000Z",
        ),
        _entry(
            method,
            url,
            status=status,
            content_type="text/html",
            body=_META_PAGE.format("metaValueBBBB2222"),
            post_data="q=x" if method == "POST" else None,
            started="2026-11-04T10:00:01.000Z",
        ),
        _entry(
            "POST",
            f"{_HOST}/api/save",
            request_headers={"x-csrf-token": "metaValueBBBB2222"},
            post_data="{}",
            started="2026-11-04T10:00:02.000Z",
        ),
    ]
    d = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
    assert d.token_pairs == ()
    assert d.token_mismatches == ()
    assert 'Token.from_meta_tag(\n                name="x-csrf-token",' in _plugin(d)


@pytest.mark.parametrize("unread", ["304", "body file gone"])
def test_a_page_load_the_recording_kept_no_body_for_leaves_nothing_compared(
    tmp_path: Path, unread: str
) -> None:
    """After a page load whose body is unknown (a 304, or a body file no longer on
    disk), the earlier page value may be stale: a header that does not equal it is
    no evidence against it."""
    page_load = _entry(
        "GET",
        f"{_HOST}/account",
        status=304 if unread == "304" else 200,
        content_type="text/html",
        body="",
        request_headers={"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"},
        started="2026-11-04T10:00:01.000Z",
    )
    if unread == "body file gone":
        page_load["response"]["content"] = {
            "mimeType": "text/html",
            "size": 0,
            "_bodyFile": "bodies/missing.html",
        }
    entries = [
        _entry(
            "GET",
            f"{_HOST}/",
            content_type="text/html",
            body=_META_PAGE.format("metaValueAAAA1111"),
            started="2026-11-04T10:00:00.000Z",
        ),
        page_load,
        _entry(
            "POST",
            f"{_HOST}/api/save",
            request_headers={"x-csrf-token": "metaValueCCCC3333"},
            post_data="{}",
            started="2026-11-04T10:00:02.000Z",
        ),
    ]
    d = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
    assert d.token_mismatches == ()
    assert 'Token.from_meta_tag(\n                name="x-csrf-token",' in _plugin(d)


def test_a_same_named_meta_the_header_never_carried_is_not_paired_by_name(
    tmp_path: Path,
) -> None:
    """The meta was read on the latest page before the post and did not hold the
    header's value, so the name pairing is dropped."""
    run = _Run()
    run.page("/", _META_PAGE.format("metaValueAAAA1111"))
    run.post({"x-csrf-token": "headerOnlyCCCC3333"})
    d = run.digest(tmp_path)
    assert _page_mismatches(d) == [("x-csrf-token", "meta", "x-csrf-token")]
    plugin = _plugin(d)
    assert "\n    token_config = TokenConfig(" not in plugin
    assert "# GP-FILL: unpaired token candidate: meta 'x-csrf-token'" in plugin


def test_a_short_page_value_is_never_compared(tmp_path: Path) -> None:
    """A same-named meta too short to be a token is no evidence against the name
    pairing."""
    run = _Run()
    run.page("/", _META_PAGE.format("a" * (MIN_PAIRING_VALUE_LENGTH - 1)))
    run.post({"x-csrf-token": "headerOnlyCCCC3333"})
    d = run.digest(tmp_path)
    assert _page_mismatches(d) == []
    assert 'Token.from_meta_tag(\n                name="x-csrf-token",' in _plugin(d)


def test_a_header_sent_before_any_page_loads_stays_unpaired(tmp_path: Path) -> None:
    run = _Run()
    run.post({"x-csrf": _PAGE_TOKENS[0]})
    run.page("/", _shell(_PAGE_TOKENS[0]))
    d = run.digest(tmp_path)
    assert d.token_pairs == ()
    assert _page_mismatches(d) == []


def test_a_generated_page_url_reads_that_page_through_fixture_session(tmp_path: Path) -> None:
    run = _Run()
    run.page("/log-in", _shell("logInValueAAAA1111"))
    run.post({"x-csrf": "logInValueAAAA1111"})
    config = _token_config(_plugin(run.digest(tmp_path)))
    assert config is not None
    assert tuple(config.tokens) == (
        Token.from_hidden_input(name="csrf", header="x-csrf", page_url="/log-in"),
    )
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / f"{capture_slug('GET', '/log-in')}.html").write_text(
        _shell("logInValue&#43;BBBB2222")
    )
    session = FixtureSession(fixtures)
    prepare_session(session, config, _HOST)
    assert session._gp_csrf_tokens == {"x-csrf": "logInValue+BBBB2222"}  # type: ignore[attr-defined]
    assert [(r.method, r.path) for r in session.requests] == [("GET", "/log-in")]


def test_no_token_value_reaches_the_log_at_any_level(
    tmp_path: Path, captured_logs: list[dict[str, Any]], capfd: pytest.CaptureFixture[str]
) -> None:
    """Digest, scaffold, and a FixtureSession run of the generated token_config log
    no page, header, or cookie value, at any level."""
    d = _synthetic_run().digest(tmp_path)
    config = _token_config(_plugin(d))
    assert config is not None
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / f"{capture_slug('GET', '/')}.html").write_text(_shell(_PAGE_TOKENS[3]))
    session = FixtureSession(fixtures)
    prepare_session(session, config, _HOST)
    assert session._gp_csrf_tokens == {"x-csrf": _PAGE_TOKENS[3]}  # type: ignore[attr-defined]

    assert captured_logs, "nothing was logged, so the check below would prove nothing"
    logged = repr(captured_logs) + "".join(capfd.readouterr())
    for value in (*_PAGE_TOKENS, "rot0000"):
        assert value not in logged
