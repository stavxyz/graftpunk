"""#268 part 3: the failure text a wrong-password attempt suggests, computed from the
page read after the attempt (raw HTML, in memory only) against the page at the post.

Every candidate is checked by the engine's own rule
(:func:`graftpunk.plugins.login_settle.failure_text_on_page`, a case-insensitive
substring of the raw serialized HTML), so these tests state what the engine would do
with each candidate and assert the function agrees.
"""

from __future__ import annotations

import json

import pytest

from graftpunk.observe.failure_text import (
    MAX_FAILURE_CANDIDATES,
    AccountGuard,
    failed_password_values,
    failure_candidates,
    without_success_page,
)
from graftpunk.plugins.login_settle import failure_text_on_page

_BODY = json.dumps({"username": "jsmith", "password": "wrongpass1"})
_JSON = "application/json"
_BEFORE = (
    "<html><body><form><input name='username'><input type='password' name='password'>"
    "<button>Sign in</button></form></body></html>"
)


def _guard(body: str = _BODY, content_type: str = _JSON) -> AccountGuard:
    guard = AccountGuard.of(body, content_type)
    assert guard is not None
    return guard


def _after(error: str, *, extra: str = "") -> str:
    return _BEFORE.replace("<form>", f"{extra}<form>{error}")


def _found(after: str | list[str], before: str = _BEFORE, **kwargs: object) -> tuple[str, ...]:
    reads = [after] if isinstance(after, str) else after
    guard = kwargs.pop("guard", _guard())
    found = failure_candidates(reads, before, guard)  # type: ignore[arg-type]
    for text in found:
        # What the engine would do with each one: it matches every read it came from
        # (at least one) and never the page at the post.
        assert any(failure_text_on_page(read, text) for read in reads), text
        assert not failure_text_on_page(before, text), text
    return found


class TestFailureCandidates:
    def test_an_error_in_a_div_is_found(self) -> None:
        after = _after("<div>Invalid username or password.</div>")
        assert _found(after) == ("Invalid username or password.",)

    def test_whitespace_around_a_text_is_trimmed(self) -> None:
        after = _after("<div>\n    Invalid username or password.\n  </div>")
        assert _found(after) == ("Invalid username or password.",)

    @pytest.mark.parametrize("text", ["Wrong p", "x" * 201])
    def test_a_text_under_8_or_over_200_characters_is_not_found(self, text: str) -> None:
        assert _found(_after(f"<div>{text}</div>")) == ()

    def test_8_and_200_characters_are_found(self) -> None:
        after = _after("<div>Wrong pw</div><p>" + "y" * 200 + "</p>")
        assert _found(after) == ("Wrong pw", "y" * 200)

    @pytest.mark.parametrize(
        "elsewhere",
        [
            "<template><div>Invalid username or password.</div></template>",
            "<script>var t = {err: 'Invalid username or password.'};</script>",
        ],
    )
    def test_text_also_in_a_template_or_script_on_the_login_page_is_dropped(
        self, elsewhere: str
    ) -> None:
        """The engine reads raw HTML, scripts and templates included: text the login
        page already carries there would fail every login on its first tick."""
        before = _BEFORE.replace("</body>", f"{elsewhere}</body>")
        after = before.replace("<form>", "<form><div>Invalid username or password.</div>")
        assert failure_text_on_page(before, "Invalid username or password.")
        assert _found(after, before) == ()

    def test_text_only_in_a_script_or_template_of_the_read_is_no_candidate(self) -> None:
        """Not page text a person sees; the read's script and template contents give no
        text node."""
        after = _BEFORE.replace(
            "</body>",
            "<script>var e = 'Invalid username or password.';</script>"
            "<template><p>Your session has expired.</p></template></body>",
        )
        assert _found(after) == ()

    def test_text_split_by_an_element_gives_each_part_never_the_whole(self) -> None:
        """The engine matches a contiguous substring of the raw HTML, so the sentence
        "Your login details were not recognised by us" never matches as one: each
        text node does, and is the candidate."""
        after = _after("<div>Your login details were <b>not recognised</b> by us</div>")
        found = _found(after)
        assert found == ("Your login details were", "not recognised")
        assert not failure_text_on_page(after, "Your login details were not recognised by us")

    def test_text_holding_an_escaped_ampersand_is_dropped(self) -> None:
        """The serialized HTML holds ``&amp;``, so the decoded text never matches it."""
        after = _after("<div>Email &amp; password do not match</div>")
        assert not failure_text_on_page(after, "Email & password do not match")
        assert _found(after) == ()

    def test_text_already_on_the_page_at_the_post_is_dropped(self) -> None:
        before = _BEFORE.replace("<form>", "<form><p>Forgot your password?</p>")
        after = before.replace("<form>", "<form><div>Invalid username or password.</div>")
        assert _found(after, before) == ("Invalid username or password.",)

    def test_case_is_ignored_on_the_before_side(self) -> None:
        before = _BEFORE.replace(
            "</body>", "<script>ERR='INVALID USERNAME OR PASSWORD.'</script></body>"
        )
        after = before.replace("<form>", "<form><div>Invalid username or password.</div>")
        assert _found(after, before) == ()

    def test_without_the_page_at_the_post_nothing_is_suggested(self) -> None:
        after = _after("<div>Invalid username or password.</div>")
        assert failure_candidates([after], None, _guard()) == ()

    def test_without_a_guard_nothing_is_suggested(self) -> None:
        after = _after("<div>Invalid username or password.</div>")
        assert failure_candidates([after], _BEFORE, None) == ()

    @pytest.mark.parametrize(
        "error",
        [
            "No account matches jsmith here",
            "No account matches JSMITH here",
            "No account matches j_smith here",
            "The password wrongpass1 is wrong",
            "Write to alice@example.com for help",
            "Write to ALICE@EXAMPLE.COM for help",
        ],
    )
    def test_text_holding_an_account_value_or_an_email_is_dropped(self, error: str) -> None:
        after = _after(f"<div>{error}</div><p>Please try again later.</p>")
        assert _found(after) == ("Please try again later.",)

    def test_an_echoed_email_username_is_dropped_in_any_case(self) -> None:
        body = json.dumps({"email": "Alice.Smith@Example.com", "password": "wrongpass1"})
        after = _after("<div>No account for alice.smith@example.com</div><p>Try again.</p>")
        assert _found(after, guard=_guard(body)) == ("Try again.",)

    def test_an_email_local_part_is_an_account_value(self) -> None:
        body = json.dumps({"email": "alice.smith@example.com", "password": "wrongpass1"})
        after = _after("<div>Hello alice.smith, that failed</div><p>Please try again later.</p>")
        assert _found(after, guard=_guard(body)) == ("Please try again later.",)

    def test_an_unreadable_body_gives_no_guard(self) -> None:
        assert AccountGuard.of("<xml/>", "application/xml") is None
        assert AccountGuard.of(None) is None

    def test_errors_marked_as_alerts_rank_first_then_document_order(self) -> None:
        after = _after(
            "<p>Need help signing in?</p>"
            "<span class='form-Error-text'>Check the fields below.</span>"
            "<div><p>Please try again later.</p></div>"
            "<div role='alert'><p>That did not work out.</p></div>"
            "<div aria-live='polite'>Something went wrong here.</div>"
        )
        assert _found(after) == (
            "Check the fields below.",
            "That did not work out.",
            "Something went wrong here.",
        )

    @pytest.mark.parametrize(
        "wrapper",
        [
            "<div class='invalid-feedback'>{}</div>",
            "<div class='alert alert-danger'>{}</div>",
            "<div role='alert'>{}</div>",
            "<div aria-live='assertive'>{}</div>",
        ],
    )
    def test_each_alert_marker_ranks_first(self, wrapper: str) -> None:
        after = _after("<p>Need help signing in?</p>" + wrapper.format("Sign-in failed, sorry."))
        assert _found(after)[0] == "Sign-in failed, sorry."

    def test_aria_live_off_is_no_alert(self) -> None:
        after = _after(
            "<p>Need help signing in?</p><div aria-live='off'>Sign-in failed, sorry.</div>"
        )
        assert _found(after) == ("Need help signing in?", "Sign-in failed, sorry.")

    def test_at_most_3(self) -> None:
        texts = [f"Error number {n} happened" for n in range(6)]
        after = _after("".join(f"<p>{text}</p>" for text in texts))
        assert _found(after) == tuple(texts[:MAX_FAILURE_CANDIDATES])
        assert MAX_FAILURE_CANDIDATES == 3

    def test_two_reads_each_checked_against_itself_and_merged(self) -> None:
        """The toast in the first read is gone from the second; the text in both is
        given once."""
        first = _after(
            "<div role='alert'>Invalid username or password.</div><p>Try again soon.</p>"
        )
        second = _after("<p>Try again soon.</p><p>Locked for a minute now.</p>")
        assert _found([first, second]) == (
            "Invalid username or password.",
            "Try again soon.",
            "Locked for a minute now.",
        )

    def test_a_text_repeated_in_another_case_is_given_once(self) -> None:
        after = _after("<p>Invalid credentials given</p><p>INVALID CREDENTIALS GIVEN</p>")
        assert _found(after) == ("Invalid credentials given",)


class TestWithoutSuccessPage:
    def test_a_candidate_also_on_the_logged_in_page_is_dropped(self) -> None:
        landed = "<html><body><p>Try again soon.</p><div id='account'></div></body></html>"
        kept = without_success_page(
            ("Invalid username or password.", "TRY AGAIN SOON."), landed, _guard()
        )
        assert kept == ("Invalid username or password.",)

    def test_a_candidate_holding_the_real_post_s_value_is_dropped(self) -> None:
        real = _guard(json.dumps({"username": "jsmith2024", "password": "realsecret9"}))
        kept = without_success_page(("Unknown user jsmith2024", "Bad credentials"), None, real)
        assert kept == ("Bad credentials",)

    def test_no_guard_for_the_real_post_drops_everything(self) -> None:
        assert without_success_page(("Bad credentials",), None, None) == ()


class TestFailedPasswordValues:
    def test_the_password_field_s_values(self) -> None:
        body = "username=jsmith&password=wrongpass1&remember=1"
        assert failed_password_values(body, "application/x-www-form-urlencoded") == ["wrongpass1"]

    def test_a_nested_json_password(self) -> None:
        body = json.dumps({"user": {"login": "jsmith", "password": "wrongpass1"}})
        assert failed_password_values(body, _JSON) == ["wrongpass1"]
