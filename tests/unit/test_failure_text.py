"""#268 part 3: the failure text a wrong-password attempt suggests, computed from the
page read after the attempt (raw HTML, in memory only) against the page at the post.

Every candidate is checked by the engine's own rule
(:func:`graftpunk.plugins.login_settle.failure_text_on_page`, a case-insensitive
substring of the raw serialized HTML), so these tests state what the engine would do
with each candidate and assert the function agrees.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from graftpunk.observe.failure_text import (
    MAX_FAILURE_CANDIDATES,
    AccountGuard,
    failed_password_values,
    failure_candidates,
    page_at_post,
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

    def test_a_value_only_its_percent_encoded_spelling_finds_is_dropped(self) -> None:
        """A page echoing the password as encodeURIComponent writes it: the compact
        needles miss it, the snapshot redaction's spellings do not."""
        body = json.dumps({"username": "jsmith", "password": "p@ss word!"})
        guard = _guard(body)
        assert "p%40ss%20word!" in guard.spellings
        after = _after("<div>Retry p%40ss%20word! later</div><p>Please try again later.</p>")
        assert _found(after, guard=guard) == ("Please try again later.",)

    @pytest.mark.parametrize(
        "body",
        [
            "username=jo&password=wrongpass1",
            json.dumps({"username": "jo", "password": "wrongpass1"}),
        ],
    )
    def test_a_short_username_is_dropped_as_a_whole_word(self, body: str) -> None:
        content_type = (
            "application/json" if body.startswith("{") else "application/x-www-form-urlencoded"
        )
        guard = _guard(body, content_type)
        after = _after(
            "<div>Hello JO, wrong password</div><p>Your job did not run.</p>"
            "<p>Please try again later.</p>"
        )
        # "job" holds "jo" only inside a longer word.
        assert _found(after, guard=guard) == ("Your job did not run.", "Please try again later.")

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
            "<div role='alert'>Invalid username or password.</div>"
            "<p class='error'>Try again soon.</p>"
        )
        second = _after(
            "<p class='error'>Try again soon.</p><p class='error'>Locked for a minute now.</p>"
        )
        assert _found([first, second]) == (
            "Invalid username or password.",
            "Try again soon.",
            "Locked for a minute now.",
        )

    def test_only_alerts_when_any_alert_text_is_kept(self) -> None:
        """A re-rendered heading or a timestamp beside a ``role="alert"`` message is
        page churn, not the error."""
        after = _after(
            "<p>Last visit at 10:42 today</p><div role='alert'>That did not work out.</div>"
        )
        assert _found(after) == ("That did not work out.",)
        # With no alert text at all, the others are still suggested.
        assert _found(_after("<p>That did not work out.</p>")) == ("That did not work out.",)

    @pytest.mark.parametrize(
        "markup",
        [
            "<title>Sign-in failed, sorry</title>",
            "<select><option>Sign-in failed, sorry</option></select>",
            "<label for='x'>Sign-in failed, sorry</label>",
            "<label><span>Sign-in failed, sorry</span></label>",
            "<button type='button'>Sign-in failed, sorry</button>",
            "<fieldset><legend>Sign-in failed, sorry</legend></fieldset>",
        ],
    )
    def test_never_text_naming_the_page_or_a_control(self, markup: str) -> None:
        after = _after(markup)
        assert failure_text_on_page(after, "Sign-in failed, sorry")
        assert _found(after) == ()

    def test_never_a_placeholder(self) -> None:
        after = _after("<input type='text' placeholder='Sign-in failed, sorry'>")
        assert failure_text_on_page(after, "Sign-in failed, sorry")
        assert _found(after) == ()

    def test_a_read_that_is_no_login_page_is_ignored(self) -> None:
        """Enter pressed after a real login: the read is the landed page, whose text
        is all new and none of it a failure."""
        landed = (
            "<html><body><div class='alert-banner'>Free shipping on orders over 50</div>"
            "<p>Your recent orders</p></body></html>"
        )
        assert _found(landed) == ()
        # The same text on a read that is still a login page counts.
        assert _found(_after("<div class='alert-banner'>Free shipping on orders over 50</div>"))

    def test_a_server_error_page_re_rendering_the_form_counts(self) -> None:
        """A form POST answered with a whole new document that shows the form again
        and the error: still a login page."""
        error_page = (
            "<html><head><title>Sign in</title></head><body><main>"
            "<p class='flash-error'>Invalid login. Please try again.</p>"
            "<form method='post' action='/session'><input name='username'>"
            "<input type='password' name='password'><button>Sign in</button></form>"
            "</main></body></html>"
        )
        assert _found(error_page) == ("Invalid login. Please try again.",)

    def test_a_password_input_inside_a_template_makes_no_login_page(self) -> None:
        read = (
            "<html><body><p class='error'>Session ended for safety</p>"
            "<template><input type='password'></template></body></html>"
        )
        assert _found(read) == ()

    def test_a_text_repeated_in_another_case_is_given_once(self) -> None:
        after = _after("<p>Invalid credentials given</p><p>INVALID CREDENTIALS GIVEN</p>")
        assert _found(after) == ("Invalid credentials given",)


class TestThePageAtThePost:
    _SHELL = "<html><body><div id='root'></div><script src='/app.js'></script></body></html>"

    def test_the_snapshot_when_one_was_kept(self) -> None:
        assert page_at_post(_BEFORE, self._SHELL) == _BEFORE

    def test_else_the_served_page_when_it_holds_a_login_form(self) -> None:
        assert page_at_post(None, _BEFORE) == _BEFORE

    def test_else_nothing(self) -> None:
        assert page_at_post(None, self._SHELL) is None
        assert page_at_post(None, None) is None

    def test_a_script_rendered_shell_suggests_nothing(self) -> None:
        """The rendered login's own labels and heading are not on the shell, so every
        one would pass as new text and fail every login on the engine's first tick."""
        rendered = (
            "<html><body><div id='root'><h1>Sign in to your account</h1>"
            "<p class='msg'>Those details did not match.</p><form><input name='username'>"
            "<input type='password' name='password'></form></div></body></html>"
        )
        assert failure_candidates([rendered], page_at_post(None, self._SHELL), _guard()) == ()


class TestCost:
    """CPU time for a page eight times as long stays near eight times as much, well
    under the 64 times a search of the whole page per text node would take."""

    _ALERT = "<div role='alert'>Sign-in failed, sorry.</div>"

    @staticmethod
    def _filler(paragraphs: int) -> str:
        return "".join(f"<p>Paragraph number {n} of the help text</p>" for n in range(paragraphs))

    @staticmethod
    def _cpu(pages: Callable[[int], tuple[str, str]], paragraphs: int) -> float:
        import time

        before, after = pages(paragraphs)
        guard = _guard()
        best = float("inf")
        for _ in range(3):
            start = time.process_time()
            assert failure_candidates([after, after], before, guard) == ("Sign-in failed, sorry.",)
            best = min(best, time.process_time() - start)
        return best

    def _ratio(self, pages: Callable[[int], tuple[str, str]]) -> float:
        return self._cpu(pages, 16_000) / self._cpu(pages, 2_000)

    def test_text_the_page_at_the_post_shares(self) -> None:
        """The usual read: the login page again, with the error added."""

        def pages(paragraphs: int) -> tuple[str, str]:
            before = _BEFORE.replace("<form>", f"{self._filler(paragraphs)}<form>")
            return before, before.replace("<form>", f"{self._ALERT}<form>")

        assert self._ratio(pages) < 16

    def test_text_only_the_read_holds(self) -> None:
        """A read whose every text is new (a page the server rendered anew)."""

        def pages(paragraphs: int) -> tuple[str, str]:
            return _BEFORE, _after(self._ALERT, extra=self._filler(paragraphs))

        assert self._ratio(pages) < 16


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

    def test_a_hint_only_in_a_parent_name_is_no_password(self) -> None:
        """The nearest name decides: ``note`` under ``password_policy`` is no
        password, and scrubbing it would lose a value that is not secret."""
        body = json.dumps(
            {
                "username": "jsmith",
                "password": "wrongpass1",
                "password_policy": {"note": "x9y8z7w6"},
            }
        )
        assert failed_password_values(body, _JSON) == ["wrongpass1"]

    def test_a_nested_json_password(self) -> None:
        body = json.dumps({"user": {"login": "jsmith", "password": "wrongpass1"}})
        assert failed_password_values(body, _JSON) == ["wrongpass1"]
