"""#268 part 3: the failure texts a recording's wrong-password step suggests, read
from the failed post's HAR entry (``request._failureTextCandidates``), checked again
against every credential post's account values, and written by ``gp plugin new`` as
commented ``failure=`` lines under a ``GP-FILL``; the live ``failure`` stays as it
was."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from graftpunk.devtools.scaffold.render import ScaffoldSpec, render
from graftpunk.har.digest import DigestSource, RunDigest, digest, post_identifier_values
from graftpunk.har.parser import parse_har_file
from graftpunk.har.report import endpoints_projection, render_endpoints_json, render_json
from tests.unit.test_har_digest import _LOGIN_CONFIG_ON_MAIN, _comment_text, _entry, _write_har

_HOST = "https://myshop.example.com"
_LOGIN = (
    "<html><body><form action='/session' method='post'><input type='email' id='email' "
    "name='email'><input type='password' id='password' name='password'><button id='go'>"
    "Sign in</button></form></body></html>"
)
_WRONG = {"email": "alice@example.com", "password": "***REDACTED***"}
_RIGHT = {"email": "alice@example.com", "password": "hunter2hunter2"}
_TEXTS = ["Invalid email or password.", "Too many attempts may lock you out."]
# The page the real login landed on, as the HAR recorded it: it shows neither text.
_LANDED = "<html><body><p>Your orders</p></body></html>"
_FORM = "application/x-www-form-urlencoded"

_FAILURE_GP_FILL = (
    "GP-FILL: the recording suggests failure text: shown after the wrong password it "
    "marked. Confirm it appears after a wrong password and not after a right one, then "
    "replace the failure line above with one suggestion (all of its lines):"
)


def _entries(
    candidates: Any = _TEXTS, *, marked: bool = True, real: dict | None = None, landed: bool = True
) -> list[dict]:
    """The login page, the marked failed post, the real login, and, with *landed*, the
    page it landed on."""
    failed = _entry(
        "POST",
        f"{_HOST}/session",
        post_data=json.dumps(_WRONG),
        started="2026-09-10T10:00:05.000Z",
    )
    if marked:
        failed["request"]["_userMarkedFailed"] = True
        failed["request"]["_failureTextCandidates"] = candidates
    right = _entry(
        "POST",
        f"{_HOST}/session",
        post_data=json.dumps(real or _RIGHT),
        started="2026-09-10T10:00:09.000Z",
    )
    entries = [
        _entry("GET", f"{_HOST}/login", content_type="text/html", body=_LOGIN),
        failed,
        right,
    ]
    if landed:
        entries.append(
            _entry(
                "GET",
                f"{_HOST}/account",
                content_type="text/html",
                body=_LANDED,
                started="2026-09-10T10:00:10.000Z",
            )
        )
    return entries


def _digest(run: Path, entries: list[dict]) -> RunDigest:
    run.mkdir(parents=True, exist_ok=True)
    return digest(DigestSource(har_path=_write_har(run, entries)))


def _plugin(result: RunDigest, backend: str = "nodriver") -> dict[str, str]:
    return render(
        ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend=backend,  # type: ignore[arg-type]
            base_url=_HOST,
            digest=result,
        )
    )


def _login_config(code: str) -> Any:
    from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep

    start = code.index("LoginConfig(")
    end = code.index("\n    )\n", start) + 6
    return eval(code[start:end], {"LoginConfig": LoginConfig, "LoginStep": LoginStep})  # noqa: S307


def _confirmed(code: str, which: int) -> str:
    """*code* as an author who confirmed the *which*-th suggestion leaves it: the live
    ``failure=`` line deleted, and the suggestion's lines uncommented."""
    out: list[str] = []
    seen = -1
    open_block = False
    for line in code.split("\n"):
        stripped = line.lstrip()
        pad = line[: len(line) - len(stripped)]
        if stripped.startswith('failure="GP-FILL'):
            continue
        if stripped.startswith("# failure="):
            seen += 1
            if seen == which:
                out.append(pad + stripped[2:])
                open_block = stripped.endswith("(")
                continue
        elif open_block and stripped.startswith("#"):
            out.append(pad + stripped[2:])
            open_block = not stripped.startswith("# )")
            continue
        out.append(line)
    return "\n".join(out)


class TestTheParser:
    def test_the_keys_are_read(self, tmp_path: Path) -> None:
        har = _write_har(tmp_path, _entries())
        failed = parse_har_file(har).entries[1].request
        assert failed.user_marked_failed is True
        assert failed.failure_text_candidates == tuple(_TEXTS)
        assert parse_har_file(har).entries[2].request.user_marked_failed is False

    @pytest.mark.parametrize(
        ("raw", "kept"),
        [
            ("Invalid email or password.", ()),
            ([1, None, "Short", "x" * 201, "Invalid email or password."], (_TEXTS[0],)),
            (
                [f"Error number {n} here" for n in range(5)],
                tuple(f"Error number {n} here" for n in range(3)),
            ),
        ],
    )
    def test_only_strings_of_8_to_200_characters_and_at_most_3(
        self, tmp_path: Path, raw: Any, kept: tuple[str, ...]
    ) -> None:
        har = _write_har(tmp_path, _entries(raw))
        assert parse_har_file(har).entries[1].request.failure_text_candidates == kept

    def test_a_flag_that_is_not_true_marks_nothing(self, tmp_path: Path) -> None:
        entries = _entries()
        entries[1]["request"]["_userMarkedFailed"] = "yes"
        har = _write_har(tmp_path, entries)
        assert parse_har_file(har).entries[1].request.user_marked_failed is False


class TestTheDigest:
    def test_the_marked_post_s_candidates_are_suggested(self, tmp_path: Path) -> None:
        result = _digest(tmp_path, _entries())
        assert result.failure_candidates == tuple(_TEXTS)
        payload = json.loads(render_endpoints_json(result))
        assert payload["login"]["failure_candidates"] == _TEXTS
        assert json.loads(render_json(result))["failure_candidates"] == _TEXTS

    def test_without_a_marked_post_nothing_is_suggested(self, tmp_path: Path) -> None:
        entries = _entries(marked=False)
        entries[1]["request"]["_failureTextCandidates"] = _TEXTS
        assert _digest(tmp_path, entries).failure_candidates == ()

    @pytest.mark.parametrize(
        "text",
        [
            "No account for alice here.",
            "No account for ALICE here.",
            "Write to help@myshop.example for help",
            "Password ***REDACTED*** is wrong",
            "Your hunter2hunter2 was wrong",
        ],
    )
    def test_a_text_holding_an_account_value_an_email_or_a_redaction_is_dropped(
        self, tmp_path: Path, text: str
    ) -> None:
        result = _digest(tmp_path, _entries([text, _TEXTS[0]]))
        assert result.failure_candidates == (_TEXTS[0],)

    def test_a_text_holding_a_redaction_is_dropped_by_that_check_alone(
        self, tmp_path: Path
    ) -> None:
        """The failed post's password is a plain value here, so no account value of
        either post is in the text: only the redaction check drops it."""
        entries = _entries(["Your ***REDACTED*** was wrong", _TEXTS[0]])
        entries[1]["request"]["postData"]["text"] = json.dumps(
            {"email": "alice@example.com", "password": "wrongpass1"}
        )
        assert _digest(tmp_path, entries).failure_candidates == (_TEXTS[0],)

    def test_the_last_marked_post_s_candidates_are_the_ones(self, tmp_path: Path) -> None:
        entries = _entries(["Unknown sign-in, sorry."])
        second = json.loads(json.dumps(entries[1]))
        second["startedDateTime"] = "2026-09-10T10:00:07.000Z"
        second["request"]["_failureTextCandidates"] = _TEXTS
        entries.insert(2, second)
        assert _digest(tmp_path, entries).failure_candidates == tuple(_TEXTS)

    def test_a_text_on_the_page_after_the_real_login_is_dropped(self, tmp_path: Path) -> None:
        """A stop soon after the real login cancels the recorder's read of the landed
        page; the page the HAR recorded after that login still drops the text."""
        landed = f"<html><body><p>{_TEXTS[1].upper()}</p><nav id='account'></nav></body></html>"
        entries = [
            *_entries(),
            _entry(
                "GET",
                f"{_HOST}/account",
                content_type="text/html",
                body=landed,
                started="2026-09-10T10:00:11.000Z",
            ),
        ]
        assert _digest(tmp_path, entries).failure_candidates == (_TEXTS[0],)

    def test_only_the_last_page_after_the_real_login_drops(self, tmp_path: Path) -> None:
        """The last page the HAR recorded after the real login is the logged-in one; an
        earlier one (a redirect's interstitial, say) drops nothing."""

        def page(path: str, text: str, started: str) -> dict:
            body = f"<html><body><p>{text}</p></body></html>"
            return _entry(
                "GET", f"{_HOST}{path}", content_type="text/html", body=body, started=started
            )

        entries = [
            *_entries(landed=False),
            page("/welcome", _TEXTS[0], "2026-09-10T10:00:10.000Z"),
            page("/account", _TEXTS[1], "2026-09-10T10:00:11.000Z"),
        ]
        assert _digest(tmp_path, entries).failure_candidates == (_TEXTS[0],)

    def test_a_page_after_the_marked_post_with_no_real_login_drops_nothing(
        self, tmp_path: Path
    ) -> None:
        """With no real login after the marked post, a page browsed after the wrong
        password (a help page) is no logged-in page."""
        login, failed, _right = _entries(landed=False)
        help_page = _entry(
            "GET",
            f"{_HOST}/help",
            content_type="text/html",
            body=f"<html><body><p>{_TEXTS[1]}</p></body></html>",
            started="2026-09-10T10:00:07.000Z",
        )
        assert _digest(tmp_path, [login, failed, help_page]).failure_candidates == tuple(_TEXTS)

    def test_a_page_before_the_real_login_drops_nothing(self, tmp_path: Path) -> None:
        """Only a page after the real login is a logged-in page: one between the
        wrong password and the real login (a help page, say) is not."""
        between = f"<html><body><p>{_TEXTS[1]}</p></body></html>"
        entries = _entries()
        entries.insert(
            2,
            _entry(
                "GET",
                f"{_HOST}/help",
                content_type="text/html",
                body=between,
                started="2026-09-10T10:00:07.000Z",
            ),
        )
        assert _digest(tmp_path, entries).failure_candidates == tuple(_TEXTS)

    def test_a_text_on_the_page_source_is_dropped(self, tmp_path: Path) -> None:
        tmp_path.mkdir(exist_ok=True)
        page = tmp_path / "page-source.html"
        page.write_text(f"<html><body><footer>{_TEXTS[0]}</footer></body></html>")
        har = _write_har(tmp_path, _entries())
        result = digest(DigestSource(har_path=har, page_source=page))
        assert result.failure_candidates == (_TEXTS[1],)


class TestTheLandedPage:
    """A real login after the marked post: the texts are kept only when something
    shows the page it landed on, since one of them may be on it and fail every good
    login."""

    def test_nothing_shows_it_so_nothing_is_suggested(self, tmp_path: Path) -> None:
        assert _digest(tmp_path, _entries(landed=False)).failure_candidates == ()

    def test_the_real_login_s_after_read_shows_it(self, tmp_path: Path) -> None:
        """The recorder read the landed page and dropped the texts on it; the
        after-read it wrote (structure only) is the sign."""
        entries = _entries(landed=False)
        (tmp_path / "snapshots").mkdir(parents=True)
        (tmp_path / "snapshots" / "r.after.html").write_text("<html><body></body></html>")
        entries[2]["request"]["_domAfter"] = "snapshots/r.after.html"
        assert _digest(tmp_path, entries).failure_candidates == tuple(_TEXTS)

    def test_the_page_source_shows_it(self, tmp_path: Path) -> None:
        tmp_path.mkdir(exist_ok=True)
        page = tmp_path / "page-source.html"
        page.write_text(_LANDED)
        har = _write_har(tmp_path, _entries(landed=False))
        result = digest(DigestSource(har_path=har, page_source=page))
        assert result.failure_candidates == tuple(_TEXTS)


class TestAnIdentifierFirstLogin:
    """The username is posted by a step before the password post, which holds no
    username of its own."""

    _STEP2 = (
        "<html><body><form action='/login/password' method='post'>"
        "<input type='password' name='password'><button>Sign in</button></form></body></html>"
    )
    _ERROR = "The password for jsmith is not right."

    def _entries(self) -> list[dict]:
        page1 = (
            "<html><body><form action='/login/identifier' method='post'>"
            "<input type='text' name='username'><button>Next</button></form></body></html>"
        )
        entries = [
            _entry("GET", f"{_HOST}/login", content_type="text/html", body=page1),
            _entry(
                "POST",
                f"{_HOST}/login/identifier",
                post_data="username=jsmith",
                started="2026-09-10T10:00:02.000Z",
            ),
            _entry(
                "GET",
                f"{_HOST}/login/password",
                content_type="text/html",
                body=self._STEP2,
                started="2026-09-10T10:00:03.000Z",
            ),
            _entry(
                "POST",
                f"{_HOST}/login/password",
                status=401,
                post_data="password=***REDACTED***",
                started="2026-09-10T10:00:05.000Z",
            ),
            _entry(
                "POST",
                f"{_HOST}/login/password",
                post_data="password=RealSecret9",
                started="2026-09-10T10:00:09.000Z",
            ),
            _entry(
                "GET",
                f"{_HOST}/account",
                content_type="text/html",
                body=_LANDED,
                started="2026-09-10T10:00:10.000Z",
            ),
        ]
        for entry in entries:
            if entry["request"]["method"] == "POST":
                entry["request"]["postData"]["mimeType"] = _FORM
                entry["request"]["headers"] = [{"name": "Content-Type", "value": _FORM}]
        entries[3]["request"]["_userMarkedFailed"] = True
        entries[3]["request"]["_failureTextCandidates"] = [self._ERROR, _TEXTS[1]]
        return entries

    def test_a_text_holding_the_username_a_step_posted_is_dropped(self, tmp_path: Path) -> None:
        assert _digest(tmp_path, self._entries()).failure_candidates == (_TEXTS[1],)

    @pytest.mark.parametrize("name", ["j_username", "user_login", "loginfmt", "handle", "uid"])
    def test_a_step_field_under_any_name_is_the_username(self, tmp_path: Path, name: str) -> None:
        """No identifier field's name and no recorded login form's: every value of a
        POST up to the real login counts."""
        entries = self._entries()
        content = entries[0]["response"]["content"]
        content["text"] = content["text"].replace("name='username'", f"name='{name}'")
        entries[1]["request"]["postData"]["text"] = f"{name}=jsmith"
        assert _digest(tmp_path, entries).failure_candidates == (_TEXTS[1],)

    def test_a_login_form_s_username_name_counts_in_a_request_that_is_no_post(
        self, tmp_path: Path
    ) -> None:
        """``handle`` is no identifier field's name, but the recorded login form takes
        it as its username: the page's script sends it in a step of its own (a PUT
        here, whose other values do not count) before the form posts the password."""
        entries = self._entries()
        entries[0]["response"]["content"]["text"] = (
            "<html><body><form action='/login/password' method='post'>"
            "<input type='text' name='handle'><input type='password' name='password'>"
            "<button>Sign in</button></form></body></html>"
        )
        entries[1]["request"]["method"] = "PUT"
        entries[1]["request"]["postData"]["text"] = "handle=jsmith"
        assert _digest(tmp_path, entries).failure_candidates == (_TEXTS[1],)
        entries[0]["response"]["content"]["text"] = entries[0]["response"]["content"][
            "text"
        ].replace("name='handle'", "name='nickname'")
        assert _digest(tmp_path, entries).failure_candidates == (self._ERROR, _TEXTS[1])
        entries[0]["response"]["content"]["text"] = entries[0]["response"]["content"][
            "text"
        ].replace("name='handle'", "name='nickname'")
        assert _digest(tmp_path, entries).failure_candidates == (self._ERROR, _TEXTS[1])

    def test_a_username_posted_after_the_real_login_drops_nothing(self, tmp_path: Path) -> None:
        """Only requests up to and including the real login count: a later one (a
        profile search, say) is no login."""
        entries = self._entries()
        entries[1]["request"]["postData"]["text"] = "username=bob_tester"
        later = _entry(
            "POST",
            f"{_HOST}/api/search",
            post_data="username=jsmith",
            started="2026-09-10T10:00:12.000Z",
        )
        later["request"]["postData"]["mimeType"] = _FORM
        later["request"]["headers"] = [{"name": "Content-Type", "value": _FORM}]
        assert _digest(tmp_path, [*entries, later]).failure_candidates == (
            self._ERROR,
            _TEXTS[1],
        )

    def test_the_real_login_s_own_step_counts(self, tmp_path: Path) -> None:
        """The window runs to the last credential post: the real login's identifier
        step, posted after the marked post, is checked too."""
        entries = self._entries()
        entries[1]["request"]["postData"]["text"] = "username=bob_tester"
        step = _entry(
            "POST",
            f"{_HOST}/login/identifier",
            post_data="username=jsmith2024",
            started="2026-09-10T10:00:07.000Z",
        )
        step["request"]["postData"]["mimeType"] = _FORM
        step["request"]["headers"] = [{"name": "Content-Type", "value": _FORM}]
        entries[3]["request"]["_failureTextCandidates"] = [
            "The password for jsmith2024 is not right.",
            _TEXTS[1],
        ]
        assert _digest(tmp_path, [*entries[:4], step, *entries[4:]]).failure_candidates == (
            _TEXTS[1],
        )

    def test_a_credential_post_whose_values_cannot_be_read_suggests_nothing(
        self, tmp_path: Path
    ) -> None:
        entries = _entries()
        del entries[2]["request"]["postData"]
        entries[2]["request"]["_postDataNotCaptured"] = True
        assert _digest(tmp_path, entries).failure_candidates == ()

    def test_the_projection_s_login_keys(self, tmp_path: Path) -> None:
        login = endpoints_projection(_digest(tmp_path, _entries()))["login"]
        assert set(login) == {"auth_urls", "forms", "success_candidates", "failure_candidates"}


class TestPostIdentifierValues:
    def test_an_identifier_name_anywhere_on_the_path(self) -> None:
        body = json.dumps({"user": {"name": "jsmith"}, "meta": {"note": "first visit"}})
        assert post_identifier_values(body, "application/json") == ["jsmith"]


class TestTheGenerator:
    @pytest.mark.parametrize("backend", ["nodriver", "selenium"])
    def test_without_candidates_the_output_is_main_s(self, tmp_path: Path, backend: str) -> None:
        """A run with a marked post and no surviving candidate, and a run with no
        marked post, generate the same files byte for byte, and main's login config."""
        plain = _plugin(_digest(tmp_path / "plain", _entries(marked=False)), backend)
        empty = _plugin(_digest(tmp_path / "empty", _entries([])), backend)
        assert empty == plain
        code = plain["src/graftpunk_myshop/plugin.py"]
        assert _LOGIN_CONFIG_ON_MAIN in code
        assert "# failure=" not in code

    @pytest.mark.parametrize("backend", ["nodriver", "selenium"])
    def test_they_are_written_commented_and_failure_stays_as_it_was(
        self, tmp_path: Path, backend: str
    ) -> None:
        from tests.unit import test_scaffold_render as rendered

        files = _plugin(_digest(tmp_path, _entries()), backend)
        code = files["src/graftpunk_myshop/plugin.py"]
        assert '        failure="GP-FILL: text on the page indicating login failure",' in code
        assert _FAILURE_GP_FILL in _comment_text(code)
        assert '        # failure="Invalid email or password.",' in code
        assert '        # failure="Too many attempts may lock you out.",' in code
        compile(code, "plugin.py", "exec")
        assert _login_config(code).failure.startswith("GP-FILL")
        checker = rendered.TestRenderedTreeIsRuffClean()
        checker._assert_tree_is_clean(checker._write_tree(tmp_path / "tree", files))
        for which, text in enumerate(_TEXTS):
            confirmed = _confirmed(code, which)
            compile(confirmed, "plugin.py", "exec")
            assert _login_config(confirmed).failure == text

    @pytest.mark.parametrize(
        "text", ["y" * 200, 'He said "no" \\ twice', "Fehler: Zugang verweigert é x"]
    )
    def test_any_text_is_ruff_clean_and_reads_back(self, tmp_path: Path, text: str) -> None:
        from tests.unit import test_scaffold_render as rendered

        result = dataclasses.replace(_digest(tmp_path, _entries()), failure_candidates=(text,))
        files = _plugin(result)
        checker = rendered.TestRenderedTreeIsRuffClean()
        checker._assert_ruff_clean(checker._write_tree(tmp_path / "tree", files), "check")
        code = files["src/graftpunk_myshop/plugin.py"]
        compile(code, "plugin.py", "exec")
        confirmed = _confirmed(code, 0)
        compile(confirmed, "plugin.py", "exec")
        assert _login_config(confirmed).failure == text
