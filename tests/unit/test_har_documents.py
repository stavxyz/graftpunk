"""HTML extraction on inline HTML, no HAR involved (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import pytest

from graftpunk.har.documents import (
    extract_login_forms,
    extract_token_candidates,
    is_login_document,
    looks_like_token_name,
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
        ("raw_action", "action", "scope"),
        [
            ("/login;jsessionid=S3CR3T?t=tok", "/login", 'form[action^="/login"]'),
            (
                "https://myshop.example.com/a;v=1/login#top",
                "https://myshop.example.com/a/login",
                'form[action^="https://myshop.example.com/a/login"]',
            ),
            ("?next=/orders", "", "form"),
        ],
    )
    def test_an_action_loses_its_query_fragment_and_path_params(
        self, raw_action: str, action: str, scope: str
    ) -> None:
        html = (
            f'<form action="{raw_action}"><input name="username">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.action == action
        assert form.fields["username"] == f'{scope} input[name="username"]'


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
