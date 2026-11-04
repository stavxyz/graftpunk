"""#369: a request header pairs with the source whose value it carried, by value: a
hidden input on a page loaded before it (in a form or not), a meta tag, or a cookie
the same request sent. A cookie with a similar name whose values never matched does
not win, and no value is ever printed."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from graftpunk.devtools.scaffold.render import ScaffoldSpec, render
from graftpunk.har.digest import DigestSource, RunDigest, digest
from graftpunk.har.naming import capture_slug
from graftpunk.har.report import render_json, render_markdown
from graftpunk.har.token_pairing import MIN_PAIRING_VALUE_LENGTH, TokenPairing
from graftpunk.testing import FixtureSession
from graftpunk.tokens import Token, TokenConfig, prepare_session
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


class TestFromHiddenInput:
    def _value(self, html: str, name: str = "csrf") -> str | None:
        token = Token.from_hidden_input(name=name, header="x-csrf")
        assert token.pattern is not None
        match = re.search(token.pattern, html)
        return match.group(1) if match else None

    def test_any_attribute_order_and_case(self) -> None:
        assert self._value('<input type="hidden" name="csrf" value="AbC123xyz">') == "AbC123xyz"
        assert self._value("<INPUT value='AbC123xyz' NAME=csrf Type=HIDDEN/>") == "AbC123xyz"

    def test_another_input_does_not_match(self) -> None:
        assert self._value('<input type="hidden" name="csrf_other" value="no">') is None
        assert self._value('<input type="text" name="csrf" value="no">') is None
        assert self._value('<input type="hidden" data-name="csrf" value="no">') is None

    def test_the_named_input_among_others(self) -> None:
        html = (
            '<input type="hidden" name="js_bootstrap_data" value="x">'
            '<input type="hidden" name="csrf" value="Z9z9Z9z9">'
        )
        assert self._value(html) == "Z9z9Z9z9"

    def test_a_name_with_pattern_characters_is_literal(self) -> None:
        assert self._value('<input type="hidden" name="a.b" value="v1v1v1v1">', "a.b") == "v1v1v1v1"
        assert self._value('<input type="hidden" name="aXb" value="v1v1v1v1">', "a.b") is None

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
