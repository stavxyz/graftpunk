"""The writing side of 'captures never enter git' (plugin tooling spec, 2026-09-11).

captures.py applies the rule to disk; the pure rule, the directory and the
.gitignore text edit, is owned by captures_rule.py and tested at the end of
this module."""

from __future__ import annotations

import ast
import hashlib
import itertools
import json
import subprocess
import time
from pathlib import Path

import pytest

import graftpunk.devtools.captures_rule as captures_rule
from graftpunk.devtools.captures import (
    checked_names_of,
    ensure_ignored,
    find_repo_root,
    is_tracked,
    write_sidecar,
)
from graftpunk.devtools.captures_rule import CAPTURES_DIR, with_ignored
from graftpunk.testing.sidecar import load_sidecar, sidecar_path


def _git(argv: list[str], cwd: Path) -> None:
    # argv is always a fixed git invocation built by this test module, never
    # untrusted input; a runtime list argument (rather than a literal) also
    # keeps ruff's S607 (partial executable path) from flagging every call
    # site below, matching graftpunk.devtools.captures's own convention.
    subprocess.run(argv, cwd=cwd, check=True)  # noqa: S603


def _init_repo(root: Path) -> None:
    _git(["git", "init", "-q"], root)
    _git(["git", "config", "user.email", "alice@example.com"], root)
    _git(["git", "config", "user.name", "alice"], root)


class TestCapturesDir:
    def test_is_tests_captures(self) -> None:
        assert CAPTURES_DIR == "tests/captures"


class TestFindRepoRoot:
    def test_finds_the_root_of_a_git_work_tree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert find_repo_root(nested) == tmp_path.resolve()

    def test_none_outside_a_work_tree(self, tmp_path: Path) -> None:
        assert find_repo_root(tmp_path) is None

    def test_a_path_that_does_not_exist_yet_still_finds_the_root(self, tmp_path: Path) -> None:
        """The fixtures command's default target does not exist on a first run:
        the search starts from the nearest existing ancestor rather than failing
        to launch git at all."""
        _init_repo(tmp_path)
        not_yet_created = tmp_path / "tests" / "captures"
        assert not not_yet_created.exists()
        assert find_repo_root(not_yet_created) == tmp_path.resolve()

    def test_a_path_that_does_not_exist_yet_outside_a_work_tree_is_none(
        self, tmp_path: Path
    ) -> None:
        assert find_repo_root(tmp_path / "tests" / "captures") is None


class TestEnsureIgnored:
    def test_adds_the_line_when_absent(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        added = ensure_ignored(tmp_path, CAPTURES_DIR)
        assert added is True
        assert "tests/captures/" in (tmp_path / ".gitignore").read_text()

    def test_idempotent_second_call_reports_no_change(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        ensure_ignored(tmp_path, CAPTURES_DIR)
        added_again = ensure_ignored(tmp_path, CAPTURES_DIR)
        assert added_again is False
        assert (tmp_path / ".gitignore").read_text().count("tests/captures/") == 1

    def test_creates_gitignore_when_missing(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        ensure_ignored(tmp_path, CAPTURES_DIR)
        assert (tmp_path / ".gitignore").exists()


class TestIsTracked:
    def test_tracked_file_is_true(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        tracked = tmp_path / "committed.txt"
        tracked.write_text("hi")
        _git(["git", "add", "committed.txt"], tmp_path)
        _git(["git", "commit", "-q", "-m", "add"], tmp_path)
        assert is_tracked(tracked) is True

    def test_untracked_file_is_false(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        untracked = tmp_path / "scratch.txt"
        untracked.write_text("hi")
        assert is_tracked(untracked) is False


class TestWriteSidecar:
    def test_writes_the_schema_keys_and_the_hash_of_the_file_on_disk(self, tmp_path: Path) -> None:
        fixture = tmp_path / "get_orders.json"
        fixture.write_text('{"orders": []}', encoding="utf-8")
        path = write_sidecar(
            fixture,
            status=200,
            content_type="application/json",
            body_params=["page", "archived"],
            flagged_reasons={
                "shop_session": ["session_name", "http_only", "session_name"],
                "csrf-token": ["token_name"],
            },
        )
        assert path == sidecar_path(fixture)
        sidecar = load_sidecar(path)
        assert sidecar.capture_sha256 == hashlib.sha256(fixture.read_bytes()).hexdigest()
        assert sidecar.body_params == ("archived", "page")
        assert sidecar.flagged_names == ("csrf-token", "shop_session")
        assert sidecar.flagged_reasons == {
            "csrf-token": ("token_name",),
            "shop_session": ("http_only", "session_name"),
        }
        assert "url" not in json.loads(path.read_text())
        assert "captured_at" not in json.loads(path.read_text())

    def test_an_empty_capture_records_no_hash(self, tmp_path: Path) -> None:
        """An empty body has nothing to sanitise, and its fixture is empty too, so a
        hash would fail the suite's unchanged-copy check on a fixture that holds no
        captured value (#236)."""
        fixture = tmp_path / "get_orders.html"
        fixture.write_bytes(b"")
        path = write_sidecar(
            fixture, status=204, content_type="text/html", body_params=[], flagged_reasons={}
        )
        assert load_sidecar(path).capture_sha256 is None
        assert json.loads(path.read_text())["capture_sha256"] is None


_VALUE = "Rk9mT2vX8Lq4Pz7Nw3Yb6Hc"


class TestCheckedNamesOf:
    @pytest.mark.parametrize(
        "body",
        [
            f'{{"token": "{_VALUE}"}}',
            f"/next?csrftoken={_VALUE}&x=1",
            f"/next?ref=%2Fa%3Fcsrftoken%3D{_VALUE}",
            f"&lt;i&gt;{_VALUE}",
            '{"token": "\\u0052' + _VALUE[1:] + '"}',
        ],
        ids=["json", "query", "percent-encoded", "html-escaped", "json-escaped"],
    )
    def test_a_value_the_body_holds_in_any_form_is_checked(self, body: str) -> None:
        assert checked_names_of({"csrftoken": frozenset({_VALUE})}, body) == ("csrftoken",)

    def test_a_value_recorded_percent_encoded_is_found_decoded(self) -> None:
        values = {"csrftoken": frozenset({"ab%2Bcd%3D%3D"})}
        assert checked_names_of(values, '{"t": "ab+cd=="}') == ("csrftoken",)

    def test_a_name_whose_values_the_body_does_not_hold_is_not_checked(self) -> None:
        values = {"id": frozenset({_VALUE}), "csrftoken": frozenset({"other-value"})}
        assert checked_names_of(values, '{"id": 7, "csrftoken": "invented"}') == ()

    def test_a_value_is_matched_case_sensitively(self) -> None:
        assert checked_names_of({"id": frozenset({_VALUE})}, _VALUE.lower()) == ()

    def test_values_not_known_keep_the_name_checked(self) -> None:
        assert checked_names_of({"b": None, "a": frozenset()}, "{}") == ("b",)

    def test_a_plus_in_a_recorded_value_is_also_read_as_a_space(self) -> None:
        """A form-encoded cookie value spells a space as +; the body holds the
        space."""
        values = {"csrftoken": frozenset({"Xk3+Lp9Qv2"})}
        assert checked_names_of(values, '{"t": "Xk3 Lp9Qv2"}') == ("csrftoken",)

    @pytest.mark.parametrize("quoted", [f'"{_VALUE}"', f"%22{_VALUE}%22"])
    def test_a_quoted_cookie_value_is_found_without_its_quotes(self, quoted: str) -> None:
        """RFC 6265 lets a server quote a cookie value, and the browser sends the
        quotes; a body holds the value inside a text node without them."""
        body = f"<p>token {_VALUE} end</p>"
        assert checked_names_of({"id": frozenset({quoted})}, body) == ("id",)

    def test_a_value_json_escaped_inside_an_html_attribute_is_found(self) -> None:
        """A value holding & and / in JSON state inside a data attribute is both
        HTML-escaped and JSON-escaped; the two decodings are combined."""
        body = '<div data-x="{&quot;t&quot;:&quot;a&amp;b\\/cdefgh&quot;}">'
        assert checked_names_of({"id": frozenset({"a&b/cdefgh"})}, body) == ("id",)


# A token the cookie took after the last recorded request that sent it: in no
# recorded header, so not among the values checked_names_of is given.
_ROTATED = "Rk9mT2vX8Lq4Pz7Nw3Yb6Hc1Jd5Ks0Gf"


class TestCheckedNamesOfValuesUnderTheName:
    """A recording knows the values its cookie headers carried, not every value
    the cookie held, so a name stays checked when the capture holds a value under
    it that is not form state (#313)."""

    @pytest.mark.parametrize(
        "body",
        [
            f'{{"order": 1, "csrftoken": "{_ROTATED}"}}',
            f'{{"CSRFTOKEN": "{_ROTATED}"}}',
            f"a=1&csrftoken={_ROTATED}",
            f"/next?csrftoken%3D{_ROTATED}",
            f"{{'csrftoken': '{_ROTATED}'}}",
            f'<input type="hidden" name="csrftoken" value="{_ROTATED}">',
            f'<meta name="csrftoken" content="{_ROTATED}">',
            f'<div csrftoken="{_ROTATED}"></div>',
            '<script>var s = "{\\"csrftoken\\":\\"' + _ROTATED + '\\"}";</script>',
            "<div data-x='{&quot;csrftoken&quot;:&quot;" + _ROTATED + "&quot;}'></div>",
            f'{{"csrftoken": {{"value": "{_ROTATED}"}}}}',
            f'{{"csrftoken": ["a", "{_ROTATED}"]}}',
            "csrftoken=7Xq9WzT8823LmPk",
            f'{{"csrftoken": {{"{_ROTATED}": true}}, "n": 1}}',
            '{"csrftoken": 8237498273492}',
            '{"csrftoken": 827364918273649182736491}',
            '{"next": "/orders?csrftoken=918273645546372819&page=2"}',
        ],
        ids=[
            "json",
            "json-other-case",
            "form",
            "percent-encoded-query",
            "single-quoted",
            "hidden-input",
            "meta",
            "attribute",
            "json-in-a-script-string",
            "json-in-an-html-attribute",
            "nested-object",
            "array",
            "number-run-on",
            "opaque-key",
            "long-number",
            "24-digit-number",
            "digits-in-a-query",
        ],
    )
    def test_a_value_under_the_name_that_is_not_form_state_keeps_it_checked(
        self, body: str
    ) -> None:
        values = {"csrftoken": frozenset({"Recorded55Xc2Lp9"})}
        assert checked_names_of(values, body) == ("csrftoken",)

    @pytest.mark.parametrize(
        "body",
        [
            f"<script>Cookies.set('csrftoken', '{_ROTATED}');</script>",
            "<script>var t = getCookie('csrftoken');</script>",
            f'{{"cookies": [{{"name": "csrftoken", "value": "{_ROTATED}"}}], "n": 1}}',
            f'[{{"key": "csrftoken", "val": "{_ROTATED}"}}]',
            f"<r><csrftoken>{_ROTATED}</csrftoken><n>1</n></r>",
            f'<textarea name="csrftoken">{_ROTATED}</textarea>',
            f"<script>window.cfg = {{csrftoken: {{value: '{_ROTATED}'}}}};</script>",
            f'<script>window.cookies["csrftoken"] = "{_ROTATED}";</script>',
            f"<tr><td>csrftoken</td><td>{_ROTATED}</td></tr>",
            json.dumps({"y": json.dumps({"x": json.dumps({"csrftoken": _ROTATED})})}),
            '{"csrftoken": 7, "note": "see csrftoken"}',
            '<input name="csrftoken">',
            '<input data-name="csrftoken" value="Ann Lee">',
            json.dumps({"x": json.dumps({"csrftoken": json.dumps({"v": _ROTATED})})}),
            '<script>var c = {csrftoken: "" + token};</script>',
            "<script>var c = {csrftoken: readToken()};</script>",
            f"csrftoken=({_ROTATED})",
            '{"csrftoken": "Ann\\q"}',
            json.dumps({"log": f'csrftoken: \\"{_ROTATED}\\"'}),
            f"csrftoken='{_ROTATED}",
            f'<input name="csrftoken" value="Ann Lee"><input name="csrftoken" value="{_ROTATED}">',
            f"<li>csrftoken: <code>{_ROTATED}</code></li>",
            f"<p><b>csrftoken:</b> {_ROTATED}</p>",
            f"<dl><dt>csrftoken:</dt><dd>{_ROTATED}</dd></dl>",
            f"<tr><td>csrftoken:</td><td>{_ROTATED}</td></tr>",
            f"<pre>csrftoken:\n</pre><pre>{_ROTATED}</pre>",
            f"<p>csrftoken=<b>{_ROTATED}</b></p>",
            f'<script>if (csrftoken == "{_ROTATED}") go();</script>',
            f"<script>ok = csrftoken === '{_ROTATED}';</script>",
            f'<script>f = csrftoken => "{_ROTATED}";</script>',
            f"csrftoken= {_ROTATED}",
            f'<script>var t = "{_ROTATED}"; document.cookie = "csrftoken=" + t;</script>',
            f'<a title="csrftoken=2"{_ROTATED}">',
            f'<form><textarea name="csrftoken" value="">{_ROTATED}</textarea></form>',
            f'<ol><li id="csrftoken" value="1">{_ROTATED}</li></ol>',
            f'<button name="csrftoken" value="1">{_ROTATED}</button>',
            f'<input name="csrftoken" value="" data-token="{_ROTATED}">',
            f'<input data-token="{_ROTATED}" value="" name="csrftoken"/>',
            f'<div csrftoken="1" class="a">{_ROTATED}</div>',
            '{"csrftoken": 7, "n": 1',
            '<script>var name = "csrftoken=";</script>',
            "a=1&csrftoken=",
            f'<p>Send "csrftoken=2" with <code>{_ROTATED}</code></p>',
            f'<p><a href="/orders?csrftoken=">{_ROTATED}</a> n 1</p>',
            f'<p>Send "csrftoken=" with <code>{_ROTATED}</code>, n 1</p>',
            f'<script>go(url("/orders?csrftoken=", "{_ROTATED}"), 1);</script>',
            f'<form action="/orders?csrftoken=" data-token="{_ROTATED}" data-n="1"></form>',
            f'{{"csrftoken=": "{_ROTATED}", "n": 1}}',
            f'{{"/x?csrftoken=1": "{_ROTATED}", "n": 1}}',
            f'<script>u = ["/x?csrftoken=", "{_ROTATED}"].join("");</script>',
            f'<script>setCookie("csrftoken=", "{_ROTATED}"); x = 1;</script>',
            f"<script>u = {{'csrftoken=': '{_ROTATED}'}};</script>",
            f'<script>u = enc("csrftoken=")+"{_ROTATED}";</script>',
            f'<p><span id="csrftoken" content=""/>{_ROTATED} n 1</p>',
            f'<div id="csrftoken" content=""/>{_ROTATED}</div>',
            f'<textarea name="csrftoken" value=""/>{_ROTATED}</textarea>',
            f'<ol><li id="csrftoken" value="1"/>{_ROTATED}</ol>',
            f'<input name="csrftoken" value="" {_ROTATED}>',
            f'<input name="csrftoken" value="" {_ROTATED}="">',
            f'<input name="csrftoken" value=""/{_ROTATED}>',
            '<input name="csrftoken" value="" csrftoken>',
            "a=1&csrftoken=en-US",
            "/orders?csrftoken=2&a=1",
            "/a?csrftoken=10.5.2&n=1",
            '{"next": "/o?csrftoken=2"}',
            '<a href="/o?csrftoken=en&amp;page=2" class="x">',
            '<a href="/o?csrftoken=2" class="x">',
            f'<p><a href="/orders?csrftoken=1&quot;{_ROTATED}">x</a> n 1</p>',
            f'<p><a href="/orders?csrftoken=2"{_ROTATED}>x</a> n 1</p>',
            f'<p><a href="/orders?csrftoken=&">{_ROTATED}</a> n 1</p>',
            f'<p>Send "csrftoken=;" with <code>{_ROTATED}</code>, n 1</p>',
            f'<form action="/orders?csrftoken=#" data-token="{_ROTATED}" data-n="1"></form>',
            f'{{"csrftoken=&": "{_ROTATED}", "n": 1}}',
            f'{{"u": ["/x?csrftoken=&", "{_ROTATED}"], "n": 1}}',
            f'<param name="csrftoken" value="{_ROTATED}">',
            f'<link itemprop="csrftoken" content="" href="/x?t={_ROTATED}">',
        ],
        ids=[
            "js-call",
            "js-call-name-only",
            "name-value-pair",
            "key-value-pair",
            "element-body",
            "textarea",
            "nested-js-literal",
            "js-bracket",
            "table-cell",
            "double-escaped",
            "an-honest-pair-and-a-mention",
            "a-named-element-with-no-value",
            "an-element-named-by-another-attribute",
            "escaped-json-in-an-escaped-string",
            "a-concatenation",
            "a-call",
            "a-value-in-brackets",
            "a-string-that-is-not-json",
            "a-doubly-escaped-value",
            "an-unclosed-quote",
            "a-second-named-element",
            "a-label-and-a-colon-before-a-tag",
            "a-label-and-a-colon-in-bold",
            "a-colon-in-a-definition-term",
            "a-colon-in-a-table-cell",
            "a-colon-before-a-line-break",
            "an-equals-sign-before-a-tag",
            "a-comparison",
            "a-strict-comparison",
            "an-arrow-function",
            "a-space-after-the-equals-sign",
            "a-concatenation-after-a-pair",
            "a-quote-run-on-into-a-token",
            "a-textarea-with-a-value-attribute",
            "a-list-item-with-a-value-attribute",
            "a-button-with-a-value-attribute",
            "another-attribute-of-the-named-element",
            "another-attribute-first-and-self-closing",
            "an-attribute-named-by-the-name",
            "an-object-left-open",
            "an-empty-pair-in-a-script-string",
            "an-empty-pair-at-the-end",
            "a-pair-ended-by-a-quote-in-text",
            "an-empty-pair-in-a-link-before-its-text",
            "an-empty-pair-in-quotes-in-prose",
            "an-empty-pair-in-a-call-argument",
            "an-empty-pair-in-an-attribute-before-another",
            "an-empty-pair-as-a-json-key",
            "a-pair-in-a-json-key",
            "an-empty-pair-in-a-json-array",
            "an-empty-pair-before-a-second-argument",
            "an-empty-pair-as-a-js-key",
            "an-empty-pair-in-a-call-before-a-concatenation",
            "a-self-closing-span",
            "a-self-closing-div",
            "a-self-closing-textarea",
            "a-self-closing-list-item",
            "an-attribute-name-of-the-named-element",
            "an-attribute-name-with-a-value",
            "an-attribute-name-after-a-slash",
            "an-attribute-of-the-named-element-named-by-the-name",
            "form-state-in-a-form",
            "a-digit-in-a-query",
            "a-version-in-a-query",
            "a-query-in-a-json-string",
            "a-query-in-an-attribute",
            "a-pair-ending-an-attribute",
            "a-character-reference-quote-in-a-query",
            "a-quote-run-on-after-a-query",
            "an-empty-pair-ended-by-an-ampersand-in-a-link",
            "an-empty-pair-ended-by-a-semicolon-in-prose",
            "an-empty-pair-ended-by-a-hash-in-an-attribute",
            "an-empty-pair-ended-by-an-ampersand-as-a-json-key",
            "an-empty-pair-ended-by-an-ampersand-in-a-json-array",
            "a-param-with-a-token-value",
            "a-link-with-a-token-in-another-attribute",
        ],
    )
    def test_an_occurrence_the_reader_cannot_pair_with_a_value_keeps_it_checked(
        self, body: str
    ) -> None:
        """A name leaves the checked names only when every whole-name occurrence of
        it is a JSON key or names a void element the reader parses: any other
        mention, a query or form pair in a URL included, may sit beside a value
        no recorded header carried (#313)."""
        values = {"csrftoken": frozenset({"Recorded55Xc2Lp9"})}
        assert checked_names_of(values, body) == ("csrftoken",)

    @pytest.mark.parametrize(
        "body",
        [
            '{"csrftoken": 7}',
            '{"csrftoken": 4411}',
            '{"csrftoken": 1.5}',
            '{"csrftoken": true, "n": 1}',
            '{"csrftoken": null}',
            '{"csrftoken": ""}',
            '{"csrftoken": "alice"}',
            '{"csrftoken": {"name": "Ann Lee", "count": 2}}',
            '<input name="csrftoken" value="Ann Lee">',
            '{"csrftoken": {"csrftoken": 1}}',
            '{"csrftoken": {"label": "csrftoken"}}',
            '{"note": "' + "ß" * 12 + '", "csrftoken": 7}',
            '<script>var s = "{\\"csrftoken\\": \\"en\\"}";</script>',
            f'<script>window.csrftoken = "{_ROTATED}";</script>',
            f'{{"x-csrftoken": "{_ROTATED}"}}',
            '<input type="hidden" name="csrftoken" value="">',
            '<input value="" type="hidden" name="csrftoken">',
            '<input name="csrftoken" value="" type="hidden"/>',
            '<meta content="Ann Lee" itemprop="csrftoken" />',
            '<input autocomplete="off" value="" data-role="token" name="csrftoken" type="hidden">',
            '<param name="csrftoken" value="">',
            '<link itemprop="csrftoken" content="" href="/a" referrerpolicy="no-referrer">',
            '<input name="csrftoken" value="" maxlength="8">',
            '<script>window.s = {"csrftoken": "en", "n": 1};</script>',
            json.dumps(
                {"items": [{"n": n, "label": "Ann Lee"} for n in range(40)], "csrftoken": 7}
            ),
            "<script>var s = "
            + json.dumps(json.dumps({"rows": [{"csrftoken": "en", "n": n} for n in range(30)]}))
            + ";</script>",
        ],
        ids=[
            "number",
            "four-digit-number",
            "float",
            "boolean",
            "null",
            "empty",
            "word",
            "object-of-form-state",
            "name-in-an-input",
            "a-key-inside-its-own-value",
            "a-string-inside-its-own-value",
            "after-characters-casefolded-to-two",
            "json-in-a-script-string",
            "not-a-whole-name-after-a-dot",
            "not-a-whole-name-after-a-hyphen",
            "a-hidden-input",
            "a-hidden-input-named-last",
            "a-self-closing-input",
            "a-self-closing-meta",
            "a-hidden-input-with-more-attributes",
            "an-empty-param",
            "a-link-with-its-own-attributes",
            "an-input-attribute-whose-name-alone-is-not-form-state",
            "json-in-a-script",
            "a-long-json-body",
            "long-json-in-a-script-string",
        ],
    )
    def test_a_name_holding_only_form_state_numbers_or_booleans_is_not_checked(
        self, body: str
    ) -> None:
        """``window.csrftoken`` and ``x-csrftoken`` are not the name where the
        fixtures check looks for it, so a value after them does not count."""
        values = {"csrftoken": frozenset({"Recorded55Xc2Lp9"})}
        assert checked_names_of(values, body) == ()

    @pytest.mark.parametrize("attribute", ["name", "id", "property", "itemprop", "http-equiv"])
    @pytest.mark.parametrize("holder", ["value", "content"])
    def test_an_element_named_by_any_naming_attribute_is_judged_by_its_value(
        self, attribute: str, holder: str
    ) -> None:
        values = {"csrftoken": frozenset({"Recorded55Xc2Lp9"})}
        honest = f'<meta {attribute}="csrftoken" {holder}="Ann Lee">'
        opaque = f'<meta {attribute}="csrftoken" {holder}="{_ROTATED}">'
        assert checked_names_of(values, honest) == ()
        assert checked_names_of(values, opaque) == ("csrftoken",)

    @pytest.mark.parametrize(
        "attributes",
        list(itertools.permutations(['type="hidden"', 'name="csrftoken"', 'value=""'])),
    )
    @pytest.mark.parametrize("end", [">", "/>", " />"])
    def test_a_hidden_input_is_read_with_its_attributes_in_any_order(
        self, attributes: tuple[str, ...], end: str
    ) -> None:
        values = {"csrftoken": frozenset({"Recorded55Xc2Lp9"})}
        assert checked_names_of(values, f"<input {' '.join(attributes)}{end}") == ()

    @pytest.mark.parametrize(
        "body",
        ['<div id="main" class="x"></div>', '<div class="x" id="main"></div>', '<br id="main"/>'],
    )
    def test_an_attribute_named_by_the_name_keeps_it_checked_wherever_it_is(
        self, body: str
    ) -> None:
        """No reader reads an attribute's name, so its place in the tag does not
        change the verdict."""
        assert checked_names_of({"id": frozenset({_VALUE})}, body) == ("id",)

    @pytest.mark.parametrize(
        ("head", "unit", "tail", "small"),
        [
            (
                "",
                '<input value="Ann Lee" name="csrftoken"> {"csrftoken": [1, {"csrftoken": 2}]} '
                '[{"id": 7, "csrftoken": "en"}] <meta content="" property="csrftoken"/> '
                '<script>var s = "{\\"csrftoken\\": \\"en\\"}";</script> ',
                "",
                50,
            ),
            ('{"csrftoken": 1} ', '{"a": ', "", 1000),
            ('{"csrftoken": 1} ', '{"a": [1, "x', "", 1000),
            ('{"csrftoken": 1} ', 'a\\"', "", 1000),
            ('<input name="csrftoken" value=""> "', '{} \\" ', '"', 1000),
            (
                '<script>var t = "',
                '<a onclick=\\"go({})\\">x</a>',
                '";</script><input name="csrftoken" value="">',
                1000,
            ),
            ('<input name="csrftoken" value=""', ' data-x="csrftoken"', ">", 125),
        ],
        ids=[
            "read",
            "objects-left-open",
            "strings-left-open",
            "escaped-quotes-left-open",
            "objects-and-escaped-quotes-in-a-string",
            "escaped-markup-in-a-script-string",
            "the-name-in-many-attributes-of-one-element",
        ],
    )
    def test_the_cost_grows_linearly_with_the_body(
        self, head: str, unit: str, tail: str, small: int
    ) -> None:
        """Each occurrence is read once, one inside a value or an element already
        read is not read again, a quote inside a string already parsed does not
        start another, and failed JSON parses stop once they have scanned a few
        times the body's length, so many, nested or unclosed values and many
        occurrences in one element stay linear. Each body is measured at *small*
        and eight times *small* units, sizes at which undoing one of those (or
        parsing the JSON values or the tags again per occurrence) makes a case
        fail within about 30 seconds rather than run for minutes."""
        values = {"csrftoken": frozenset({"Recorded55Xc2Lp9"}), "id": frozenset({_VALUE})}

        def cost(n: int) -> float:
            body = head + unit * n + tail
            best = float("inf")
            for _ in range(3):
                start = time.process_time()
                assert checked_names_of(values, body) == ()
                best = min(best, time.process_time() - start)
            return best

        base, large = cost(small), cost(8 * small)
        assert large / base < 16, (base, large)

    def test_names_not_known_are_not_judged_by_what_the_body_holds(self) -> None:
        assert checked_names_of({"csrftoken": None}, '{"csrftoken": 7}') == ("csrftoken",)

    def test_a_value_too_deeply_nested_to_judge_keeps_the_name_checked(self) -> None:
        body = '{"csrftoken": ' + "[" * 100_000 + "]" * 100_000 + "}"
        values = {"csrftoken": frozenset({"Recorded55Xc2Lp9"})}
        assert checked_names_of(values, body) == ("csrftoken",)


class TestWriteSidecarCheckedNames:
    def test_without_values_every_flagged_name_is_checked(self, tmp_path: Path) -> None:
        fixture = tmp_path / "get_orders.json"
        fixture.write_text('{"id": 7}', encoding="utf-8")
        path = write_sidecar(
            fixture,
            status=200,
            content_type="application/json",
            body_params=[],
            flagged_reasons={"id": ["value_not_form_state"], "sid": ["session_name"]},
        )
        assert load_sidecar(path).checked_names == ("id", "sid")

    def test_with_values_only_the_names_whose_value_the_file_holds(self, tmp_path: Path) -> None:
        fixture = tmp_path / "get_orders.json"
        fixture.write_text(json.dumps({"id": 7, "token": _VALUE}), encoding="utf-8")
        path = write_sidecar(
            fixture,
            status=200,
            content_type="application/json",
            body_params=[],
            flagged_reasons={"id": ["value_not_form_state"], "sid": ["session_name"]},
            flagged_values={
                "id": frozenset({"Zq9XkLmPwR4t7Bv2Hn8Ks3Jd"}),
                "sid": frozenset({_VALUE}),
            },
        )
        assert load_sidecar(path).checked_names == ("sid",)
        assert _VALUE not in path.read_text()

    def test_a_flagged_name_missing_from_the_values_is_checked(self, tmp_path: Path) -> None:
        fixture = tmp_path / "get_orders.json"
        fixture.write_text("{}", encoding="utf-8")
        path = write_sidecar(
            fixture,
            status=200,
            content_type="application/json",
            body_params=[],
            flagged_reasons={"id": ["value_not_form_state"]},
            flagged_values={},
        )
        assert load_sidecar(path).checked_names == ("id",)


class TestWithIgnored:
    def test_the_line_is_added_once(self) -> None:
        assert with_ignored("", "tests/captures") == "tests/captures/\n"
        assert with_ignored("dist/", "tests/captures") == "dist/\ntests/captures/\n"
        assert with_ignored("tests/captures\n", "tests/captures/") == "tests/captures\n"

    def test_a_crlf_file_gets_a_crlf_line(self) -> None:
        assert with_ignored("dist/\r\n", "tests/captures") == "dist/\r\ntests/captures/\r\n"
        assert with_ignored("dist/", "tests/captures") == "dist/\ntests/captures/\n"
        assert with_ignored("a\r\ndist/", "tests/captures") == "a\r\ndist/\r\ntests/captures/\r\n"


def test_ensure_ignored_appends_a_crlf_line_to_a_crlf_gitignore(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_bytes(b"*.pyc\r\ndist/\r\n")
    assert ensure_ignored(tmp_path, CAPTURES_DIR)
    assert gitignore.read_bytes() == b"*.pyc\r\ndist/\r\ntests/captures/\r\n"


def test_the_rule_module_imports_nothing_that_touches_the_filesystem() -> None:
    """Scaffold modules import the rule from here, so it stays pure text."""
    assert captures_rule.__file__ is not None
    tree = ast.parse(Path(captures_rule.__file__).read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert imported <= {"__future__"}
