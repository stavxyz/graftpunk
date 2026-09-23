"""HTML extraction on inline HTML, no HAR involved (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import lxml.html
import pytest

from graftpunk.har.documents import (
    extract_login_forms,
    extract_token_candidates,
    is_login_document,
    looks_like_token_name,
    printable_selectors,
    printable_unresolved_roles,
    unscoped_selector,
)

_LOGIN_PAGE = """
<html><body>
<form action="/login" method="post">
  <input type="email" name="email" id="email-field">
  <input type="password" name="password" id="pw">
  <input type="hidden" name="_token" value="abc123">
  <button type="submit" id="login-btn">Sign in</button>
</form>
</body></html>
"""

_NO_LOGIN_PAGE = "<html><body><p>Welcome, alice@example.com</p></body></html>"

_MULTI_FORM_PAGE = """
<html><body>
<form action="/search"><input type="text" name="q"></form>
<form action="/login" method="POST">
  <input name="username">
  <input type="password" name="pw">
  <input type="submit" value="Go">
</form>
</body></html>
"""


class TestExtractLoginForms:
    def test_finds_form_with_password_input(self) -> None:
        forms = extract_login_forms(_LOGIN_PAGE, source="page-source.html")
        assert len(forms) == 1
        form = forms[0]
        assert form.action == "/login"
        assert form.method == "POST"
        assert form.source == "page-source.html"

    def test_id_selector_preferred_over_name_selector(self) -> None:
        (form,) = extract_login_forms(_LOGIN_PAGE, source="s")
        assert form.fields["username"] == "#email-field"
        assert form.fields["password"] == "#pw"  # noqa: S105

    def test_email_type_guessed_as_username(self) -> None:
        (form,) = extract_login_forms(_LOGIN_PAGE, source="s")
        assert "username" in form.fields

    def test_hidden_inputs_captured(self) -> None:
        (form,) = extract_login_forms(_LOGIN_PAGE, source="s")
        assert form.hidden == ("_token",)

    def test_submit_selector(self) -> None:
        (form,) = extract_login_forms(_LOGIN_PAGE, source="s")
        assert form.submit == "#login-btn"

    def test_form_without_password_input_is_not_a_login_form(self) -> None:
        assert extract_login_forms(_NO_LOGIN_PAGE, source="s") == ()

    def test_only_the_password_form_among_several_is_returned(self) -> None:
        forms = extract_login_forms(_MULTI_FORM_PAGE, source="s")
        assert len(forms) == 1
        assert forms[0].action == "/login"

    def test_field_without_id_falls_back_to_name_selector(self) -> None:
        (form,) = extract_login_forms(_MULTI_FORM_PAGE, source="s")
        assert form.fields["username"] == 'form[action="/login"] input[name="username"]'

    def test_submit_input_without_id_falls_back_to_type_selector(self) -> None:
        (form,) = extract_login_forms(_MULTI_FORM_PAGE, source="s")
        assert form.submit == 'form[action="/login"] input[type="submit"]'

    @pytest.mark.parametrize(
        ("raw_action", "action", "scopes"),
        [
            (
                "/login;jsessionid=S3CR3T?t=tok",
                "/login",
                (
                    'form[action="/login"]',
                    'form[action^="/login;"]',
                    'form[action^="/login?"]',
                    'form[action^="/login#"]',
                ),
            ),
            (
                "/login#top",
                "/login",
                (
                    'form[action="/login"]',
                    'form[action^="/login;"]',
                    'form[action^="/login?"]',
                    'form[action^="/login#"]',
                ),
            ),
            (
                "https://myshop.example.com/a;v=1/login#top",
                "https://myshop.example.com/a/login",
                (
                    'form[action^="https://myshop.example.com/a;"][action$="/login"]',
                    'form[action^="https://myshop.example.com/a;"][action*="/login;"]',
                    'form[action^="https://myshop.example.com/a;"][action*="/login?"]',
                    'form[action^="https://myshop.example.com/a;"][action*="/login#"]',
                ),
            ),
            (
                "?next=/orders",
                "",
                (
                    "form:not([action])",
                    'form[action=""]',
                    'form[action^="?"]',
                    'form[action^=";"]',
                ),
            ),
        ],
    )
    def test_an_action_loses_its_query_fragment_and_path_params(
        self, raw_action: str, action: str, scopes: tuple[str, ...]
    ) -> None:
        html = (
            f'<form action="{raw_action}"><input name="username">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.action == action
        assert form.fields["username"] == ", ".join(
            f'{scope} input[name="username"]' for scope in scopes
        )

    @pytest.mark.parametrize(
        ("decoy", "live"),
        [
            ('action="/login-help"', 'action="/login;jsessionid=S3CR3T?t=tok"'),
            ('action="/login2"', 'action="/login?t=tok"'),
            ('action="/other"', 'action="?next=/orders"'),
            ('action="/other"', 'action=";jsessionid=S3CR3T"'),
            ('action="/other"', ""),
            ('action="/other"', 'action=""'),
            ('action="/login-help"', 'action="/login#top"'),
            (
                'action="/account;m=DECOYVALUE/login-help"',
                'action="/account;m=MIDVALUE/login;s=LASTVALUE?t=QUERYVALUE"',
            ),
            (
                'action="/account;m=DECOYVALUE/login-help"',
                'action="/account;m=MIDVALUE/login"',
            ),
            (
                'action="/a;m=DECOYVALUE/b/c-help"',
                'action="/a;m=MIDVALUE/b;n=SECONDVALUE/c"',
            ),
        ],
        ids=[
            "prefix-decoy",
            "digit-decoy",
            "query-only",
            "params-only",
            "no-action",
            "empty",
            "fragment-only",
            "middle-param",
            "middle-param-bare-last",
            "two-middle-params",
        ],
    )
    def test_a_stripped_action_selects_the_live_form_and_not_a_decoy_before_it(
        self, decoy: str, live: str
    ) -> None:
        lxml_html = pytest.importorskip("lxml.html")
        pytest.importorskip("cssselect")
        html = (
            f'<html><body><form {decoy}><input name="username" class="decoy">'
            '<input type="password" name="password" class="decoy"></form>'
            f'<form {live}><input name="username" class="live">'
            '<input type="password" name="password" class="live"></form></body></html>'
        )
        forms = extract_login_forms(html, source="s")
        live_form = forms[1]
        document = lxml_html.fromstring(html)
        for selector in live_form.fields.values():
            selected = document.cssselect(selector)
            assert [element.get("class") for element in selected] == ["live"], selector
            for value in ("MIDVALUE", "LASTVALUE", "QUERYVALUE", "SECONDVALUE", "DECOYVALUE"):
                assert value not in selector, selector


class TestExtractTokenCandidates:
    def test_meta_tag_with_csrf_name_is_a_candidate(self) -> None:
        html = '<html><head><meta name="csrf-token" content="xyz"></head></html>'
        candidates = extract_token_candidates(html, source="page-source.html")
        assert len(candidates) == 1
        assert candidates[0].kind == "meta"
        assert candidates[0].name == "csrf-token"
        assert candidates[0].seen_on == ("page-source.html",)

    def test_meta_tag_without_token_substring_is_not_a_candidate(self) -> None:
        html = '<html><head><meta name="description" content="a shop"></head></html>'
        assert extract_token_candidates(html, source="s") == ()

    def test_hidden_input_named_authenticity_token(self) -> None:
        html = '<form><input type="hidden" name="authenticity_token" value="v"></form>'
        candidates = extract_token_candidates(html, source="s")
        assert len(candidates) == 1
        assert candidates[0].kind == "hidden_input"
        assert candidates[0].name == "authenticity_token"

    def test_no_candidates_in_plain_html(self) -> None:
        assert extract_token_candidates(_NO_LOGIN_PAGE, source="s") == ()


class TestIsLoginDocument:
    def test_true_for_password_form(self) -> None:
        assert is_login_document(_LOGIN_PAGE) is True

    def test_false_for_ordinary_page(self) -> None:
        assert is_login_document(_NO_LOGIN_PAGE) is False


class TestLooksLikeTokenName:
    """The one predicate digest.py's header and cookie scans call, instead of
    re-typing the hint tuple (validation net-negative, addressed 2026-09-12)."""

    @pytest.mark.parametrize(
        "name",
        ["X-CSRF-Token", "xsrf-token", "csrftoken", "Authorization-Token"],
    )
    def test_true_for_a_token_like_name(self, name: str) -> None:
        assert looks_like_token_name(name) is True

    @pytest.mark.parametrize("name", ["Content-Type", "Accept", "session_id"])
    def test_false_for_an_ordinary_name(self, name: str) -> None:
        assert looks_like_token_name(name) is False


class TestUnscopedSelector:
    def test_a_form_scoped_selector_keeps_its_input_part(self) -> None:
        scoped = (
            'form[action="/a/1"] input[name="username"], '
            'form[action^="/a/1;"] input[name="username"]'
        )
        assert unscoped_selector(scoped) == 'input[name="username"]'

    def test_a_type_selector_keeps_its_input_part(self) -> None:
        assert (
            unscoped_selector('form[action=""] input[type="password"]') == 'input[type="password"]'
        )

    def test_an_id_selector_is_unchanged(self) -> None:
        assert unscoped_selector("#email") == "#email"

    def test_a_selector_that_does_not_end_in_an_input_part_is_refused(self) -> None:
        """Fails closed: returning the selector would print its scope, action included."""
        assert unscoped_selector('form[action="/a/1"] input.login') is None

    def test_an_id_attribute_selector_is_unchanged(self) -> None:
        assert unscoped_selector('[id="user email"]') == '[id="user email"]'


class TestSelectorsEscapeAttributeValues:
    def test_a_name_holding_a_quote_and_a_backslash_is_escaped(self) -> None:
        html = (
            '<form action="/login"><input name="ref&quot;x\\y">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["username"] == 'form[action="/login"] input[name="ref\\"x\\\\y"]'
        assert unscoped_selector(form.fields["username"]) == 'input[name="ref\\"x\\\\y"]'

    def test_an_action_holding_a_quote_is_escaped(self) -> None:
        html = (
            '<form action="/log&quot;in"><input name="username">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["username"] == 'form[action="/log\\"in"] input[name="username"]'

    def test_an_id_that_is_not_a_css_identifier_is_an_attribute_selector(self) -> None:
        html = (
            '<form action="/login"><input id="user email" name="username">'
            '<input type="password" id="pw" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": '[id="user email"]', "password": "#pw"}


def test_a_form_whose_action_holds_an_email_gets_unscoped_selectors() -> None:
    """The action is kept with the email masked, which no live form's action
    matches, so a scope spelled from it would never select anything."""
    html = (
        '<form action="/users/alice%40example.com/session"><input name="username">'
        '<input type="password" name="password"></form>'
    )
    (form,) = extract_login_forms(html, source="s")
    assert form.action == "/users/{user_id}/session"
    assert form.fields == {
        "username": 'input[name="username"]',
        "password": 'input[name="password"]',
    }


def test_a_brace_in_the_stripped_query_does_not_hide_a_masked_email() -> None:
    """Masking is judged path to path; a "{" the query held is not a placeholder."""
    html = (
        '<form action="/users/alice%40example.com/session?next={x}"><input name="username">'
        '<input type="password" name="password"></form>'
    )
    (form,) = extract_login_forms(html, source="s")
    assert form.action == "/users/{user_id}/session"
    assert form.fields["username"] == 'input[name="username"]'


class TestLoginFormNamesGoThroughTheIdRule:
    """F1 and F2: a login form's element ids and input names are names like any
    other, and one that holds an account value is never printed."""

    def test_an_element_id_holding_an_id_falls_back_to_the_name(self) -> None:
        html = (
            '<form action="/login"><input id="user_40912873" name="username">'
            '<input type="password" id="pw" name="password">'
            '<button id="btn-5f1a9c2e8b1d" type="submit">Go</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["username"] == 'form[action="/login"] input[name="username"]'
        assert form.submit == 'form[action="/login"] button[type="submit"]'

    def test_an_input_name_holding_an_id_gets_a_neutral_role_and_a_type_selector(self) -> None:
        html = (
            '<form action="/login"><input type="email" name="email">'
            '<input type="text" name="otp_40912873">'
            '<input type="tel" name="fld_a8f3c9e2b1d4">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {
            "username": 'form[action="/login"] input[name="email"]',
            "field_1": 'form[action="/login"] input[type="text"]',
            "field_2": 'form[action="/login"] input[type="tel"]',
            "password": 'form[action="/login"] input[name="password"]',
        }

    def test_a_credential_input_name_holding_an_id_keeps_its_role(self) -> None:
        html = (
            '<form action="/login"><input type="email" name="email_40912873">'
            '<input type="password" name="pw_ab12cd34ef56"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {
            "username": 'form[action="/login"] input[type="email"]',
            "password": 'form[action="/login"] input[type="password"]',
        }

    def test_a_hidden_input_name_holding_an_id_is_dropped_and_counted(self) -> None:
        html = (
            '<form action="/login"><input type="hidden" name="7f3a9c2e8b1d4f60a9e2c3b4d5f6a7b8">'
            '<input type="hidden" name="_token"><input name="username">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.hidden == ("_token",)
        assert form.hidden_names_dropped_as_ids == 1


class TestLoginFallbackSelectors:
    """A fallback selector (no usable id or name) is used only when it picks one
    input of the recorded form, and never printed without the form scope; a
    typeless input or button gets a selector that matches it."""

    def test_the_only_candidate_for_a_role_resolved_by_type_is_unresolved_when_not_unique(
        self,
    ) -> None:
        html = (
            '<form action="/login"><input type="text" name="otp_40912873">'
            '<input type="text" name="fld_a8f3c9e2b1d4">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"password": 'form[action="/login"] input[name="password"]'}
        assert form.unresolved_roles == ("username",)

    def test_a_typeless_input_and_button_get_selectors_that_match_them(self) -> None:
        html = (
            '<form action="/login"><input name="user_40912873">'
            '<input type="password" name="password"><button>Go</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        scope = 'form[action="/login"]'
        assert form.fields["username"] == (f'{scope} input:not([type]), {scope} input[type="text"]')
        assert form.submit == f'{scope} button:not([type]), {scope} button[type="submit"]'
        assert unscoped_selector(form.fields["username"]) == (
            'input:not([type]), input[type="text"]'
        )
        _assert_first_match(html, form.fields["username"], name="user_40912873")
        _assert_first_match(html, form.submit, text="Go")

    def test_an_empty_type_is_typeless(self) -> None:
        html = (
            '<form action="/login"><input type="" name="user_40912873">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        scope = 'form[action="/login"]'
        assert form.fields["username"] == (f'{scope} input:not([type]), {scope} input[type="text"]')

    def test_a_neutral_key_never_takes_a_real_input_name(self) -> None:
        html = (
            '<form action="/login"><input type="text" name="username">'
            '<input type="text" name="field_1">'
            '<input type="tel" name="otp_40912873">'
            '<input type="password" name="password"><button>Go</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["field_1"] == 'form[action="/login"] input[name="field_1"]'
        assert form.fields["field_2"] == 'form[action="/login"] input[type="tel"]'
        assert form.neutral_roles == ("field_2",)

    def test_a_type_fallback_is_never_printed_unscoped(self) -> None:
        """A search form sits before a login form whose action holds an id: the
        printed selectors drop the scope, and input[type="text"] would match the
        search box first."""
        html = (
            '<form action="/search"><input type="text" name="q"><button>Search</button></form>'
            '<form action="/accounts/40912873/session"><input type="text">'
            '<input type="password"><button>Sign in</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        fields, submit = printable_selectors(form)
        assert fields == {"password": None, "username": None}
        assert submit is None
        assert printable_unresolved_roles(form) == ("username", "password", "submit")

    def test_a_name_printed_unscoped_must_be_unique_on_the_page(self) -> None:
        html = (
            '<form action="/newsletter"><input type="email" name="email"></form>'
            '<form action="/accounts/40912873/session"><input type="email" name="email">'
            '<input type="password" name="password"><button id="signin">Sign in</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        fields, submit = printable_selectors(form)
        assert fields == {"password": 'input[name="password"]', "username": None}
        assert submit == "#signin"
        assert printable_unresolved_roles(form) == ("username",)
        _assert_first_match(html, 'input[name="password"]', name="password")

    def test_an_unparseable_action_leaves_type_fallbacks_unresolved(self) -> None:
        html = (
            '<form action="http://[::1/login"><input type="text">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"password": 'input[name="password"]'}
        assert form.unresolved_roles == ("username",)


class TestLoginRolesFollowHtml:
    """Roles follow HTML semantics: the password input anchors them, the username is
    the text-like input nearest before it, the submit is the first submit control
    after it, and a control that is not a credential is never a role."""

    def test_the_submit_is_the_first_after_the_password(self) -> None:
        html = (
            '<form action="/login"><input type="submit" id="go" value="Go">'
            '<input name="username"><input type="password" name="password">'
            '<button id="signin">Sign in</button><button id="subscribe">Subscribe</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.submit == "#signin"

    def test_the_username_is_the_hinted_input_nearest_before_the_password(self) -> None:
        html = (
            '<form action="/login"><input type="text" name="q">'
            '<input type="email" name="email"><input type="text" name="company">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["username"] == 'form[action="/login"] input[name="email"]'
        assert form.fields["company"] == 'form[action="/login"] input[name="company"]'
        assert "q" not in form.fields

    def test_a_text_input_with_no_hint_nearest_the_password_is_the_username(self) -> None:
        html = (
            '<form action="/login"><input type="text" name="handle">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["username"] == 'form[action="/login"] input[name="handle"]'

    @pytest.mark.parametrize(
        "input_type", ["checkbox", "radio", "file", "image", "reset", "range", "hidden"]
    )
    def test_a_non_text_control_is_never_a_role(self, input_type: str) -> None:
        html = (
            f'<form action="/login"><input name="username"><input type="password" '
            f'name="password"><input type="{input_type}" name="remember_me"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert set(form.fields) == {"username", "password"}

    def test_a_later_hinted_input_never_overwrites_the_username(self) -> None:
        html = (
            '<form action="/login"><input name="username" id="user">'
            '<input type="password" name="password"><button id="signin">Sign in</button>'
            '<input type="email" name="newsletter_email"><button>Subscribe</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {
            "username": "#user",
            "password": 'form[action="/login"] input[name="password"]',
        }
        assert form.submit == "#signin"
        assert form.unresolved_roles == ()

    def test_a_nameless_input_takes_its_role_from_autocomplete(self) -> None:
        html = (
            '<form action="/login"><input type="text" name="q">'
            '<input autocomplete="username" id="login-id">'
            '<input type="text" name="nickname">'
            '<input type="password" autocomplete="current-password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["username"] == "#login-id"

    def test_a_form_with_no_username_records_it_unresolved(self) -> None:
        html = '<form action="/login"><input type="password" name="password"></form>'
        (form,) = extract_login_forms(html, source="s")
        assert set(form.fields) == {"password"}
        assert form.unresolved_roles == ("username",)

    def test_a_registration_form_is_not_a_login_form(self) -> None:
        html = (
            '<form action="/register"><input type="email" name="email">'
            '<input type="password" name="password">'
            '<input type="password" name="password_confirm"><button>Create</button></form>'
            '<form action="/login"><input type="email" name="email">'
            '<input type="password" name="password"><button>Sign in</button></form>'
        )
        forms = extract_login_forms(html, source="s")
        assert [form.action for form in forms] == ["/login"]


def _assert_first_match(
    html: str,
    selector: str,
    *,
    name: str | None = None,
    text: str | None = None,
    id_: str | None = None,
) -> None:
    """The first element *selector* picks in *html* is the intended one."""
    matches = lxml.html.fromstring(f"<html><body>{html}</body></html>").cssselect(selector)
    assert matches, selector
    first = matches[0]
    if name is not None:
        assert first.get("name") == name, (selector, first.attrib)
    if text is not None:
        assert (first.text or first.get("value")) == text, (selector, first.attrib)
    if id_ is not None:
        assert first.get("id") == id_, (selector, first.attrib)


_WEBFORMS_PAGE = (
    '<form id="aspnetForm" action="./Login.aspx" method="post">'
    '<input type="hidden" name="__VIEWSTATE" value="x">'
    '<div class="header"><input type="text" name="ctl00$Header$txtSearch"'
    ' id="ctl00_Header_txtSearch">'
    '<input type="submit" name="ctl00$Header$btnGo" id="ctl00_Header_btnGo" value="Go"></div>'
    '<input type="text" id="ctl00_MainContent_LoginUser_UserName"'
    ' name="ctl00$MainContent$LoginUser$UserName">'
    '<input type="password" id="ctl00_MainContent_LoginUser_Password"'
    ' name="ctl00$MainContent$LoginUser$Password">'
    '<input type="checkbox" id="ctl00_MainContent_LoginUser_RememberMe"'
    ' name="ctl00$MainContent$LoginUser$RememberMe">'
    '<input type="submit" id="ctl00_MainContent_LoginUser_LoginButton"'
    ' name="ctl00$MainContent$LoginUser$LoginButton" value="Log In">'
    '<div class="footer"><input type="email" name="ctl00$Footer$txtNewsletter"'
    ' id="ctl00_Footer_txtNewsletter">'
    '<input type="submit" name="ctl00$Footer$btnSubscribe" id="ctl00_Footer_btnSubscribe"'
    ' value="Subscribe"></div></form>'
)

_REACT_PAGE = (
    '<form action="/api/session" method="post">'
    '<input id=":r0:" type="text" autocomplete="username">'
    '<input id=":r1:" type="password" autocomplete="current-password">'
    '<button type="submit">Continue</button></form>'
)

_SEARCH_THEN_ID_ACTION_PAGE = (
    '<form action="/search"><input type="text" name="q"><button>Search</button></form>'
    '<form action="/accounts/40912873/session"><input type="text" name="username">'
    '<input type="password" name="password"><button type="submit" name="signin">Sign in</button>'
    "</form>"
)

_REGISTER_THEN_LOGIN_PAGE = (
    '<form action="/register"><input type="email" name="email">'
    '<input type="password" name="password" autocomplete="new-password">'
    "<button>Create account</button></form>"
    '<form action="/login"><input type="email" name="email">'
    '<input type="password" name="password"><button>Sign in</button></form>'
)


class TestLoginSelectorsPickTheIntendedElement:
    """Every selector a login form prints, run against the recorded HTML, picks the
    intended element first."""

    def test_webforms_with_header_search_and_footer_newsletter(self) -> None:
        (form,) = extract_login_forms(_WEBFORMS_PAGE, source="s")
        fields, submit = printable_selectors(form)
        assert set(fields) == {"username", "password"}
        _assert_first_match(
            _WEBFORMS_PAGE, fields["username"] or "", id_="ctl00_MainContent_LoginUser_UserName"
        )
        _assert_first_match(
            _WEBFORMS_PAGE, fields["password"] or "", id_="ctl00_MainContent_LoginUser_Password"
        )
        _assert_first_match(
            _WEBFORMS_PAGE, submit or "", id_="ctl00_MainContent_LoginUser_LoginButton"
        )

    def test_react_ids_and_autocomplete(self) -> None:
        (form,) = extract_login_forms(_REACT_PAGE, source="s")
        fields, submit = printable_selectors(form)
        _assert_first_match(_REACT_PAGE, fields["username"] or "", id_=":r0:")
        _assert_first_match(_REACT_PAGE, fields["password"] or "", id_=":r1:")
        _assert_first_match(_REACT_PAGE, submit or "", text="Continue")

    def test_a_search_form_before_a_login_form_whose_action_holds_an_id(self) -> None:
        forms = extract_login_forms(_SEARCH_THEN_ID_ACTION_PAGE, source="s")
        (form,) = forms
        fields, submit = printable_selectors(form)
        assert "40912873" not in repr((fields, submit))
        _assert_first_match(_SEARCH_THEN_ID_ACTION_PAGE, fields["username"] or "", name="username")
        _assert_first_match(_SEARCH_THEN_ID_ACTION_PAGE, fields["password"] or "", name="password")
        _assert_first_match(_SEARCH_THEN_ID_ACTION_PAGE, submit or "", name="signin")

    def test_a_register_form_before_the_login_form(self) -> None:
        (form,) = extract_login_forms(_REGISTER_THEN_LOGIN_PAGE, source="s")
        fields, submit = printable_selectors(form)
        _assert_first_match(_REGISTER_THEN_LOGIN_PAGE, submit or "", text="Sign in")
        for role in ("username", "password"):
            matches = lxml.html.fromstring(
                f"<html><body>{_REGISTER_THEN_LOGIN_PAGE}</body></html>"
            ).cssselect(fields[role] or "")
            assert matches[0].getparent().get("action") == "/login", role
