"""Scrubbing a hand-typed login from a recorded run at save (#318).

``gp observe interactive`` and ``gp observe go`` record a login graftpunk never
knew the credentials of. ``save_observe_run(..., scrub_credential_posts=True)``
reads them from the run's own credential posts and scrubs them from every file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from graftpunk.har.digest import DigestSource, digest
from graftpunk.observe.run import (
    REDACTED,
    _marked_failed_secrets,
    credential_post_redaction,
    names_a_password,
    save_observe_run,
)
from graftpunk.observe.storage import ObserveStorage

_HOST = "https://myshop.example"
_EMAIL = "alice@example.com"
_JSON_PASSWORD = "hunter2-json-pass"  # noqa: S105 - placeholder, not a credential
_FORM_PASSWORD = "Tr0ub4dor&3"  # noqa: S105 - placeholder, not a credential
_LOGIN_PAGE = (
    "<html><body><form action='/session' method='post'>"
    "<input type='email' name='email'><input type='password' name='password'>"
    "<button>Sign in</button></form></body></html>"
)


def _entry(
    method: str,
    path: str,
    *,
    body: str = "{}",
    content_type: str = "application/json",
    post_data: str | None = None,
    post_type: str = "application/json",
    status: int = 200,
    started: str = "2026-10-31T10:00:00.000Z",
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "startedDateTime": started,
        "time": 5,
        "request": {
            "method": method,
            "url": f"{_HOST}{path}",
            "headers": [],
            "cookies": [],
            "queryString": [],
        },
        "response": {
            "status": status,
            "statusText": "OK",
            "headers": [{"name": "Content-Type", "value": content_type}],
            "cookies": [],
            "redirectURL": "",
            "content": {"mimeType": content_type, "text": body, "size": len(body)},
        },
    }
    if post_data is not None:
        entry["request"]["headers"].append({"name": "Content-Type", "value": post_type})
        entry["request"]["postData"] = {"mimeType": post_type, "text": post_data}
    return entry


def _login_page() -> dict[str, Any]:
    entry = _entry("GET", "/login", body=_LOGIN_PAGE, content_type="text/html")
    entry["request"]["_resourceType"] = "Document"
    return entry


def _json_post(password: str = _JSON_PASSWORD) -> dict[str, Any]:
    return _entry(
        "POST",
        "/session",
        post_data=json.dumps({"email": _EMAIL, "password": password}),
        status=401,
        started="2026-10-31T10:00:05.000Z",
    )


def _form_post(password: str = _FORM_PASSWORD) -> dict[str, Any]:
    from urllib.parse import urlencode

    return _entry(
        "POST",
        "/session",
        post_data=urlencode({"email": _EMAIL, "password": password}),
        post_type="application/x-www-form-urlencoded",
        status=302,
        started="2026-10-31T10:00:09.000Z",
    )


class _Backend:
    """A stopped nodriver capture, as ``save_observe_run`` reads one."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries
        self.page_source = "<html><body>Orders</body></html>"

    async def take_screenshot(self) -> bytes:
        return b"\x89PNG"

    async def get_page_source(self) -> str:
        return self.page_source

    async def stop_capture_async(self) -> None:
        return None

    def get_har_entries(self) -> list[dict[str, Any]]:
        return json.loads(json.dumps(self._entries))

    def get_console_logs(self) -> list[dict[str, Any]]:
        return []


def _storage(tmp_path: Path, run_id: str = "run1") -> ObserveStorage:
    return ObserveStorage(tmp_path, "sess", run_id)


def _run_bytes(run_dir: Path) -> dict[str, bytes]:
    """Every file of a run but its screenshots, whose names carry a time."""
    return {
        path.relative_to(run_dir).as_posix(): path.read_bytes()
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and "screenshots" not in path.parts
    }


def _everything(run_dir: Path) -> bytes:
    return b"\n".join(_run_bytes(run_dir).values())


async def _save(
    storage: ObserveStorage, backend: Any, console: Any = None, *, scrub: bool = True
) -> None:
    await save_observe_run(
        storage, backend, "interactive-final", console=console, scrub_credential_posts=scrub
    )


class TestCredentialPostsAreScrubbed:
    @pytest.mark.asyncio
    async def test_json_and_form_posts_leave_no_password_in_har_or_bodies(
        self, tmp_path: Path
    ) -> None:
        storage = _storage(tmp_path)
        bodies = storage.run_dir / "bodies"
        bodies.mkdir(parents=True)
        # A form login large enough to land in bodies/ carries the password the same.
        big = _form_post()
        big["request"]["postData"]["text"] += "&pad=" + "x" * 64
        (bodies / "big.txt").write_text(big["request"].pop("postData")["text"])
        big["request"]["_postDataFile"] = "bodies/big.txt"
        echo = _entry(
            "GET",
            "/account",
            body=json.dumps({"user": _EMAIL, "hint": _JSON_PASSWORD}),
            started="2026-10-31T10:00:10.000Z",
        )
        backend = _Backend([_login_page(), _json_post(), _form_post(), big, echo])

        await _save(storage, backend)

        har = (storage.run_dir / "network.har").read_bytes()
        body_file = (bodies / "big.txt").read_bytes()
        for password in (_JSON_PASSWORD, _FORM_PASSWORD):
            for form in (password, password.replace("&", "%26")):
                assert form.encode() not in har
                assert form.encode() not in body_file
        assert b"%40example.com" not in har
        assert REDACTED.encode() in har
        # Field names and body shape stay.
        posts = [
            e["request"]
            for e in json.loads(har)["log"]["entries"]
            if e["request"]["method"] == "POST"
        ]
        assert json.loads(posts[0]["postData"]["text"]) == {
            "email": REDACTED,
            "password": REDACTED,
        }
        assert posts[1]["postData"]["text"].startswith("email=")
        assert "&password=" in posts[1]["postData"]["text"]

    @pytest.mark.asyncio
    async def test_the_digest_still_reports_the_credential_post(self, tmp_path: Path) -> None:
        storage = _storage(tmp_path)
        await _save(storage, _Backend([_login_page(), _json_post(), _form_post()]))

        result = digest(DigestSource.from_run_dir(storage.run_dir, session="sess", run_id="run1"))

        posts = [step for step in result.login if step.kind == "credential_post"]
        assert posts
        assert all({"email", "password"} <= set(step.fields) for step in posts)

    @pytest.mark.asyncio
    async def test_page_source_and_snapshots_are_scrubbed(self, tmp_path: Path) -> None:
        storage = _storage(tmp_path)
        snapshots = storage.run_dir / "snapshots"
        snapshots.mkdir(parents=True)
        # A snapshot of a later post's page that shows the first post's values.
        (snapshots / "2.html").write_text(f"<p>{_EMAIL}</p><p>{_JSON_PASSWORD}</p>")
        backend = _Backend([_login_page(), _json_post(), _form_post()])
        backend.page_source = f"<html><body>Signed in as {_EMAIL}</body></html>"

        await _save(storage, backend)

        everything = _everything(storage.run_dir)
        assert _EMAIL.encode() not in everything
        assert _JSON_PASSWORD.encode() not in everything
        assert (snapshots / "2.html").read_text() == f"<p>{REDACTED}</p><p>{REDACTED}</p>"

    @pytest.mark.asyncio
    async def test_log_and_console_say_how_many_never_which(self, tmp_path: Path) -> None:
        console = MagicMock()
        with patch("graftpunk.observe.run.LOG") as log:
            await _save(
                _storage(tmp_path), _Backend([_login_page(), _json_post(), _form_post()]), console
            )

        summary = [c for c in log.info.call_args_list if c.args[0] == "observe_credential_scrub"]
        assert summary[0].kwargs == {
            "credential_posts": 2,
            "values_scrubbed": 3,
            "values_post_only": 0,
            "values_left": 0,
            "bodies_unread": 0,
            "logins_missed": 0,
            "password_input_unmatched": False,
        }
        printed = " ".join(str(c.args[0]) for c in console.print.call_args_list)
        assert "Scrubbed 3 typed credential values from the run" in printed
        said = repr(log.mock_calls) + printed
        for value in (_EMAIL, _JSON_PASSWORD, _FORM_PASSWORD):
            assert value not in said


class TestRunWithoutCredentialPost:
    @pytest.mark.asyncio
    async def test_is_written_byte_for_byte_as_without_the_scrub(self, tmp_path: Path) -> None:
        entries = [
            _login_page(),
            _entry("POST", "/search", post_data=json.dumps({"q": "hunter2-json-pass"})),
            _entry("GET", "/account", body=json.dumps({"user": _EMAIL})),
        ]
        plain, scrubbed = _storage(tmp_path / "a"), _storage(tmp_path / "b")
        for storage in (plain, scrubbed):
            (storage.run_dir / "snapshots").mkdir(parents=True)
            (storage.run_dir / "snapshots" / "1.html").write_text(f"<p>{_EMAIL}</p>")

        await _save(plain, _Backend(entries), scrub=False)
        await _save(scrubbed, _Backend(entries))

        assert _run_bytes(scrubbed.run_dir) == _run_bytes(plain.run_dir)
        assert _EMAIL.encode() in _everything(scrubbed.run_dir)


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_password_scrubs_the_identifier_only(self, tmp_path: Path) -> None:
        storage = _storage(tmp_path)

        await _save(storage, _Backend([_login_page(), _json_post(password="")]))

        har = json.loads((storage.run_dir / "network.har").read_text())
        body = json.loads(har["log"]["entries"][1]["request"]["postData"]["text"])
        assert body == {"email": REDACTED, "password": ""}

    @pytest.mark.asyncio
    async def test_a_password_under_three_characters_is_left_and_said(self, tmp_path: Path) -> None:
        storage = _storage(tmp_path)
        console = MagicMock()

        await _save(storage, _Backend([_login_page(), _json_post("pw")]), console)

        har = json.loads((storage.run_dir / "network.har").read_text())
        body = json.loads(har["log"]["entries"][1]["request"]["postData"]["text"])
        assert body == {"email": REDACTED, "password": "pw"}
        printed = " ".join(str(c.args[0]) for c in console.print.call_args_list)
        assert "1 credential value is left in the run (under 3 characters" in printed

    @pytest.mark.asyncio
    async def test_every_attempt_is_scrubbed_and_a_marked_one_is_left_as_marked(
        self, tmp_path: Path
    ) -> None:
        storage = _storage(tmp_path)
        typo = "hunter2-jsn-pass"  # noqa: S105 - placeholder, not a credential
        # The recorder replaced the marked wrong password in its post's own body.
        marked = _json_post(REDACTED)
        entries = [_login_page(), marked, _json_post(typo), _json_post(), _form_post()]
        console = MagicMock()

        await _save(storage, _Backend(entries), console)

        everything = _everything(storage.run_dir)
        for password in (typo, _JSON_PASSWORD, _FORM_PASSWORD):
            assert password.encode() not in everything
        har = json.loads((storage.run_dir / "network.har").read_text())
        assert json.loads(har["log"]["entries"][1]["request"]["postData"]["text"]) == {
            "email": REDACTED,
            "password": REDACTED,
        }
        printed = " ".join(str(c.args[0]) for c in console.print.call_args_list)
        # The email, and three passwords; the placeholder is no value.
        assert "Scrubbed 4 typed credential values from the run[/green] (4 credential posts)" in (
            printed
        )

    @pytest.mark.asyncio
    async def test_a_password_that_is_also_an_ordinary_word_is_scrubbed_everywhere(
        self, tmp_path: Path
    ) -> None:
        storage = _storage(tmp_path)
        page = _entry(
            "GET", "/help", body="<p>Contact our sunflower team</p>", content_type="text/html"
        )

        await _save(storage, _Backend([_login_page(), _json_post("sunflower"), page]))

        har = json.loads((storage.run_dir / "network.har").read_text())
        assert har["log"]["entries"][2]["response"]["content"]["text"] == (
            f"<p>Contact our {REDACTED} team</p>"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("password", ["password", "pass", "email"])
    async def test_a_password_inside_a_field_name_keeps_the_names(
        self, tmp_path: Path, password: str
    ) -> None:
        storage = _storage(tmp_path)
        console = MagicMock()

        await _save(
            storage, _Backend([_login_page(), _json_post(password), _form_post(password)]), console
        )

        har = json.loads((storage.run_dir / "network.har").read_text())
        json_post, form_post = (e["request"] for e in har["log"]["entries"][1:])
        assert json.loads(json_post["postData"]["text"]) == {
            "email": REDACTED,
            "password": REDACTED,
        }
        assert form_post["postData"]["text"] == f"email={REDACTED}&password={REDACTED}"
        result = digest(DigestSource.from_run_dir(storage.run_dir, session="sess", run_id="run1"))
        posts = [step for step in result.login if step.kind == "credential_post"]
        assert posts
        assert all({"email", "password"} <= set(step.fields) for step in posts)
        printed = " ".join(str(c.args[0]) for c in console.print.call_args_list)
        assert "scrubbed from the login's own body only" in printed

    @pytest.mark.asyncio
    async def test_a_body_the_rule_cannot_parse_is_left_and_nothing_breaks(
        self, tmp_path: Path
    ) -> None:
        storage = _storage(tmp_path)
        broken = _json_post()
        broken["request"]["postData"]["text"] = '{"email": "alice@example.com", "password": "hun'

        await _save(storage, _Backend([_login_page(), broken]))

        har = json.loads((storage.run_dir / "network.har").read_text())
        assert har["log"]["entries"][1]["request"]["postData"]["text"].endswith('"hun')

    def test_a_body_file_that_is_not_text_is_counted_unread_and_logged(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "bodies").mkdir()
        (tmp_path / "bodies" / "1.bin").write_bytes(b"\xff\xfepassword=x")
        post = _entry("POST", "/upload")
        post["request"]["_postDataFile"] = "bodies/1.bin"

        with patch("graftpunk.observe.run.LOG") as log:
            found = credential_post_redaction([_login_page(), post], tmp_path)

        assert (found.posts, found.unread, found.missed, bool(found.redaction)) == (0, 1, 0, False)
        assert log.info.call_args.args[0] == "credential_scrub_body_unread"
        assert not log.warning.called

    def test_an_unread_body_sent_to_the_login_form_is_a_missed_login(self, tmp_path: Path) -> None:
        (tmp_path / "bodies").mkdir()
        (tmp_path / "bodies" / "1.bin").write_bytes(b"\xff\xfepassword=x")
        post = _entry("POST", "/session")
        post["request"]["_postDataFile"] = "bodies/1.bin"

        with patch("graftpunk.observe.run.LOG") as log:
            found = credential_post_redaction([_login_page(), post], tmp_path)

        assert (found.posts, found.unread, found.missed) == (0, 1, 1)
        assert log.warning.call_args.args[0] == "credential_scrub_body_unread"

    def test_a_rule_that_raises_on_one_post_skips_only_that_post(self, tmp_path: Path) -> None:
        calls = iter([RecursionError("deep"), True])

        def flaky(_text: str, _type: str) -> bool:
            result = next(calls)
            if isinstance(result, BaseException):
                raise result
            return result

        with (
            patch("graftpunk.har.digest.is_credential_body", side_effect=flaky),
            patch("graftpunk.observe.run.LOG") as log,
        ):
            found = credential_post_redaction([_json_post(), _form_post()], tmp_path)

        assert (found.posts, found.unread) == (1, 1)
        assert _FORM_PASSWORD in found.redaction.secrets
        assert _JSON_PASSWORD not in found.redaction.secrets
        assert log.warning.call_args.kwargs["exc_type"] == "RecursionError"

    def test_a_multipart_login_is_no_credential_post_but_its_password_is_taken(
        self, tmp_path: Path
    ) -> None:
        body = (
            '--b\r\nContent-Disposition: form-data; name="password"\r\n\r\n'
            f"{_JSON_PASSWORD}\r\n--b--\r\n"
        )
        post = _entry(
            "POST", "/session", post_data=body, post_type="multipart/form-data; boundary=b"
        )

        found = credential_post_redaction([post], tmp_path)

        assert (found.posts, found.missed, found.body_indices) == (0, 0, (0,))
        assert _JSON_PASSWORD in found.redaction.secrets


class TestObserveGoScrubs:
    @pytest.mark.asyncio
    async def test_observe_go_saves_with_the_scrub(self) -> None:
        from graftpunk.cli.observe_browser import ObserveSession, run_observe_go

        tab = MagicMock()
        tab.sleep = AsyncMock()
        observed = ObserveSession(
            browser=MagicMock(),
            handle=MagicMock(),
            tab=tab,
            storage=MagicMock(),
            backend=MagicMock(),
        )
        where = "graftpunk.cli.observe_browser"
        with (
            patch(f"{where}.setup_observe_session", AsyncMock(return_value=observed)),
            patch(f"{where}.save_observe_run", new_callable=AsyncMock) as save,
            patch(f"{where}.stop_observe_browser"),
        ):
            await run_observe_go("myshop", f"{_HOST}/login", 0.0, 1024)

        assert save.await_args.kwargs["scrub_credential_posts"] is True


def _har(storage: ObserveStorage) -> list[dict[str, Any]]:
    return json.loads((storage.run_dir / "network.har").read_text())["log"]["entries"]


def _printed(console: MagicMock) -> str:
    return " ".join(str(c.args[0]) for c in console.print.call_args_list)


def _credential_posts(storage: ObserveStorage) -> list[Any]:
    result = digest(DigestSource.from_run_dir(storage.run_dir, session="sess", run_id="run1"))
    return [step for step in result.login if step.kind == "credential_post"]


class TestFileReferencesAreKept:
    @pytest.mark.asyncio
    async def test_a_password_inside_a_body_file_name_keeps_the_reference(
        self, tmp_path: Path
    ) -> None:
        """A password that is part of graftpunk's own file reference (the word
        ``request`` in ``bodies/<id>.request.txt``) leaves the reference as written,
        and the body file is still found and scrubbed."""
        storage = _storage(tmp_path)
        bodies = storage.run_dir / "bodies"
        bodies.mkdir(parents=True)
        snapshots = storage.run_dir / "snapshots"
        snapshots.mkdir()
        post = _form_post("request")
        ref = "bodies/4711.12.request.txt"
        (storage.run_dir / ref).write_text(post["request"].pop("postData")["text"])
        post["request"]["_postDataFile"] = ref
        post["request"]["_domSnapshot"] = "snapshots/4711.12.request.html"
        post["request"]["_domAfter"] = "snapshots/4711.12.request.after.html"
        (snapshots / "4711.12.request.html").write_text("<p>request</p>")
        post["response"]["content"]["_bodyFile"] = "bodies/4711.12.request.html"

        await _save(storage, _Backend([_login_page(), post]))

        entry = _har(storage)[1]
        assert entry["request"]["_postDataFile"] == ref
        assert entry["request"]["_domSnapshot"] == "snapshots/4711.12.request.html"
        assert entry["request"]["_domAfter"] == "snapshots/4711.12.request.after.html"
        assert entry["response"]["content"]["_bodyFile"] == "bodies/4711.12.request.html"
        body = (storage.run_dir / ref).read_text()
        assert body == f"email={REDACTED}&password={REDACTED}"
        assert entry["request"]["_postDataSize"] == len(body)
        assert (snapshots / "4711.12.request.html").read_text() == f"<p>{REDACTED}</p>"


class TestTheBodyIsScrubbedByItsStructure:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("password", "posted"),
        [("hunter~22", "hunter%7E22"), ("my pass*word", "my+pass*word")],
    )
    async def test_a_browser_form_spelling_is_scrubbed(
        self, tmp_path: Path, password: str, posted: str
    ) -> None:
        """A browser's form posts ``~`` as ``%7E`` and a space as ``+`` with ``*``
        raw (the WHATWG form serializer): scrubbed in the login's own body, and in an
        echo of the same spelling elsewhere."""
        storage = _storage(tmp_path)
        post = _form_post()
        post["request"]["postData"]["text"] = f"email=alice%40example.com&password={posted}"
        echo = _entry(
            "POST",
            "/profile",
            post_data=f"hint={posted}&x=1",
            post_type="application/x-www-form-urlencoded",
        )

        await _save(storage, _Backend([_login_page(), post, echo]))

        entries = _har(storage)
        assert entries[1]["request"]["postData"]["text"] == (
            f"email={REDACTED}&password={REDACTED}"
        )
        assert entries[2]["request"]["postData"]["text"] == f"hint={REDACTED}&x=1"
        everything = _everything(storage.run_dir)
        assert posted.encode() not in everything
        assert password.encode() not in everything
        assert _credential_posts(storage)

    @pytest.mark.asyncio
    async def test_a_numeric_json_password_leaves_valid_json_in_the_har_and_bodies(
        self, tmp_path: Path
    ) -> None:
        storage = _storage(tmp_path)
        bodies = storage.run_dir / "bodies"
        bodies.mkdir(parents=True)
        inline = _json_post()
        inline["request"]["postData"]["text"] = json.dumps({"email": _EMAIL, "password": 482916})
        on_disk = _json_post()
        on_disk["request"].pop("postData")
        (bodies / "2.request.json").write_text(json.dumps({"email": _EMAIL, "password": 482916}))
        on_disk["request"]["_postDataFile"] = "bodies/2.request.json"

        await _save(storage, _Backend([_login_page(), inline, on_disk]))

        entries = _har(storage)
        expected = {"email": REDACTED, "password": REDACTED}
        assert json.loads(entries[1]["request"]["postData"]["text"]) == expected
        assert json.loads((bodies / "2.request.json").read_text()) == expected
        assert b"482916" not in _everything(storage.run_dir)
        posts = _credential_posts(storage)
        assert len(posts) == 2
        assert all({"email", "password"} <= set(step.fields) for step in posts)

    @pytest.mark.asyncio
    async def test_crlf_line_endings_are_kept(self, tmp_path: Path) -> None:
        storage = _storage(tmp_path)
        backend = _Backend([_login_page(), _json_post()])
        backend.page_source = f"<html>\r\n<body>{_JSON_PASSWORD}</body>\r\n</html>"

        await _save(storage, backend)

        assert (storage.run_dir / "page-source.html").read_bytes() == (
            f"<html>\r\n<body>{REDACTED}</body>\r\n</html>".encode()
        )


class TestTheDigestsRule:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("password", True),
            ("j_passwd", True),
            ("user[pass]", True),
            ("PW", True),
            ("userPin", True),
            ("pin_code", True),
            ("passcode", True),
            ("client_secret", True),
            ("bypass", False),
            ("spinner", False),
            ("email", False),
            (None, False),
        ],
    )
    def test_names_a_password(self, name: str | None, expected: bool) -> None:
        assert names_a_password(name) is expected

    @pytest.mark.asyncio
    async def test_a_nested_json_login_to_the_login_form_is_scrubbed(self, tmp_path: Path) -> None:
        storage = _storage(tmp_path)
        post = _json_post()
        post["request"]["postData"]["text"] = json.dumps(
            {"user": {"email": _EMAIL, "password": "sunflower99"}}
        )

        await _save(storage, _Backend([_login_page(), post]))

        assert json.loads(_har(storage)[1]["request"]["postData"]["text"]) == {
            "user": {"email": REDACTED, "password": REDACTED}
        }
        assert b"sunflower99" not in _everything(storage.run_dir)
        assert _credential_posts(storage)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["pass", "pw", "pin", "passcode", "userSecret"])
    async def test_a_password_under_another_name_posted_to_the_login_form_is_scrubbed(
        self, tmp_path: Path, name: str
    ) -> None:
        storage = _storage(tmp_path)
        page = _login_page()
        page["response"]["content"]["text"] = _LOGIN_PAGE.replace(
            "name='password'", f"name='{name}'"
        )
        post = _form_post()
        post["request"]["postData"]["text"] = f"email=alice%40example.com&{name}=sunflower99"

        await _save(storage, _Backend([page, post]))

        assert _har(storage)[1]["request"]["postData"]["text"] == (
            f"email={REDACTED}&{name}={REDACTED}"
        )
        assert b"sunflower99" not in _everything(storage.run_dir)

    @pytest.mark.asyncio
    async def test_a_nested_json_login_with_no_login_form_is_scrubbed_without_a_warning(
        self, tmp_path: Path
    ) -> None:
        """No login form in the run, so a nested JSON login is no credential post by
        the digest's rule: its password is still taken by its name, and its username
        with it."""
        storage = _storage(tmp_path)
        post = _json_post()
        post["request"]["postData"]["text"] = json.dumps(
            {"user": {"email": _EMAIL, "password": "sunflower99"}}
        )
        console = MagicMock()

        await _save(storage, _Backend([post]), console)

        assert json.loads(_har(storage)[0]["request"]["postData"]["text"]) == {
            "user": {"email": REDACTED, "password": REDACTED}
        }
        everything = _everything(storage.run_dir)
        assert b"sunflower99" not in everything
        assert _EMAIL.encode() not in everything
        assert "could not read" not in _printed(console)

    @pytest.mark.asyncio
    async def test_a_run_with_no_login_prints_nothing_about_credentials(
        self, tmp_path: Path
    ) -> None:
        storage = _storage(tmp_path)
        bodies = storage.run_dir / "bodies"
        bodies.mkdir(parents=True)
        (bodies / "up.bin").write_bytes(b"\x89PNG\xff\xfe")
        upload = _entry("POST", "/upload")
        upload["request"]["_postDataFile"] = "bodies/up.bin"
        console = MagicMock()

        with patch("graftpunk.observe.run.LOG") as log:
            await _save(storage, _Backend([_entry("GET", "/"), upload]), console)

        printed = _printed(console)
        assert "credential" not in printed
        assert "could not read" not in printed
        assert not log.warning.called

    @pytest.mark.asyncio
    async def test_an_identifier_first_login_scrubs_the_username_too(self, tmp_path: Path) -> None:
        storage = _storage(tmp_path)
        first = _entry(
            "POST",
            "/login/identifier",
            post_data=json.dumps({"username": "alice.example"}),
            started="2026-10-31T10:00:03.000Z",
        )
        second = _entry(
            "POST",
            "/login/password",
            post_data=json.dumps({"password": _JSON_PASSWORD}),
            started="2026-10-31T10:00:05.000Z",
        )
        account = _entry("GET", "/account", body=json.dumps({"name": "alice.example"}))

        await _save(storage, _Backend([first, second, account]))

        everything = _everything(storage.run_dir)
        assert b"alice.example" not in everything
        assert _JSON_PASSWORD.encode() not in everything


class TestEchoesAreScrubbed:
    @pytest.mark.asyncio
    async def test_basic_auth_query_string_and_cookie(self, tmp_path: Path) -> None:
        import base64

        storage = _storage(tmp_path)
        basic = base64.b64encode(f"{_EMAIL}:{_JSON_PASSWORD}".encode()).decode()
        echo = _entry("GET", f"/api?token={_JSON_PASSWORD}")
        echo["request"]["queryString"] = [{"name": "token", "value": _JSON_PASSWORD}]
        echo["request"]["headers"].append({"name": "Authorization", "value": f"Basic {basic}"})
        cookie = base64.b64encode(_JSON_PASSWORD.encode()).decode()
        echo["request"]["cookies"].append({"name": "remember", "value": cookie})

        await _save(storage, _Backend([_login_page(), _json_post(), echo]))

        request = _har(storage)[2]["request"]
        assert request["url"] == f"{_HOST}/api?token={REDACTED}"
        assert request["queryString"] == [{"name": "token", "value": REDACTED}]
        assert {"name": "Authorization", "value": f"Basic {REDACTED}"} in request["headers"]
        assert request["cookies"] == [{"name": "remember", "value": REDACTED}]
        everything = _everything(storage.run_dir)
        for value in (_JSON_PASSWORD, basic, cookie):
            assert value.encode() not in everything


class _MarkedBackend(_Backend):
    """A backend whose user marked a post failed: the recorder kept its password."""

    def __init__(self, entries: list[dict[str, Any]], marked: tuple[str, ...]) -> None:
        super().__init__(entries)
        self._marked = marked

    def take_marked_failed_secrets(self) -> tuple[str, ...]:
        marked, self._marked = self._marked, ()
        return marked


class TestWrongPasswordFirst:
    @pytest.mark.asyncio
    async def test_the_marked_password_is_scrubbed_from_the_whole_run(self, tmp_path: Path) -> None:
        storage = _storage(tmp_path)
        typo = "hunter2-jsn-pass"  # noqa: S105 - placeholder, not a credential
        echo = _entry("GET", f"/retry?last={typo}")
        entries = [_login_page(), _json_post(REDACTED), _json_post(), echo]

        await _save(storage, _MarkedBackend(entries, (typo,)))

        everything = _everything(storage.run_dir)
        assert typo.encode() not in everything
        assert _JSON_PASSWORD.encode() not in everything

    @pytest.mark.asyncio
    async def test_enter_after_the_real_login_still_scrubs_the_real_password(
        self, tmp_path: Path
    ) -> None:
        """Enter pressed after the real login: the marked post is the real one, its
        body already scrubbed by the recorder, and the save scrubs its password from
        everything else (a header, a later URL, the page source)."""
        storage = _storage(tmp_path)
        echo = _entry("GET", f"/welcome?pw={_JSON_PASSWORD}")
        echo["request"]["headers"].append({"name": "X-Pass", "value": _JSON_PASSWORD})
        backend = _MarkedBackend([_login_page(), _json_post(REDACTED), echo], (_JSON_PASSWORD,))
        backend.page_source = f"<html><body>{_JSON_PASSWORD}</body></html>"

        await _save(storage, backend)

        assert _JSON_PASSWORD.encode() not in _everything(storage.run_dir)
        assert _har(storage)[2]["request"]["url"] == f"{_HOST}/welcome?pw={REDACTED}"


_API_ME = {"verified": True, "admin": False, "id": 312, "plan": "standard"}
_MISSED_LINE = "looks like a login the scrub could not read"
_UNMATCHED_LINE = "A recorded page has a password field, but no request of the run sent a password"


def _api_me() -> dict[str, Any]:
    return _entry("GET", "/api/me?active=true&standard=1", body=json.dumps(_API_ME))


def _warnings(log: MagicMock) -> list[Any]:
    return [
        c for c in log.warning.call_args_list if c.args[0] == "observe_credential_scrub_incomplete"
    ]


class TestWhichValuesArePasswords:
    @pytest.mark.parametrize(
        "name",
        [
            "rememberPassword",
            "showPassword",
            "pw_visible",
            "secret_question",
            "passwordStrength",
            "pass_type",
            "forgot-password",
            "password.reset",
            "user[password_hint]",
        ],
    )
    def test_a_setting_about_the_password_names_none(self, name: str) -> None:
        assert names_a_password(name) is False

    @pytest.mark.parametrize("name", ["password", "pin", "newPassword", "user[pass]"])
    def test_the_password_itself_is_named(self, name: str) -> None:
        assert names_a_password(name) is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "extra",
        [
            "rememberPassword=true",
            "showPassword=false",
            "secret_question=312",
            "passwordStrength=4",
        ],
    )
    async def test_a_setting_beside_the_password_leaves_the_rest_of_the_run_unchanged(
        self, tmp_path: Path, extra: str
    ) -> None:
        storage = _storage(tmp_path)
        post = _form_post()
        post["request"]["postData"]["text"] = f"email=alice%40example.com&password=hunter12&{extra}"

        await _save(storage, _Backend([_login_page(), post, _api_me()]))

        entries = _har(storage)
        assert entries[1]["request"]["postData"]["text"] == (
            f"email={REDACTED}&password={REDACTED}&{extra}"
        )
        assert entries[2]["request"]["url"] == f"{_HOST}/api/me?active=true&standard=1"
        assert json.loads(entries[2]["response"]["content"]["text"]) == _API_ME
        assert b"hunter12" not in _everything(storage.run_dir)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "secret"),
        [
            ({"email": _EMAIL, "password": "hunter12"}, "hunter12"),
            ({"email": _EMAIL, "pin": 4417}, "4417"),
            ({"email": _EMAIL, "newPassword": "Fresh-Pass-31"}, "Fresh-Pass-31"),
        ],
    )
    async def test_a_password_pin_or_new_password_is_scrubbed(
        self, tmp_path: Path, body: dict[str, Any], secret: str
    ) -> None:
        storage = _storage(tmp_path)
        post = _entry("POST", "/session", post_data=json.dumps(body))
        echo = _entry("GET", "/account", body=json.dumps({"last": secret}))

        await _save(storage, _Backend([_login_page(), post, echo]))

        assert secret.encode() not in _everything(storage.run_dir)
        assert json.loads(_har(storage)[1]["request"]["postData"]["text"]) == dict.fromkeys(
            body, REDACTED
        )

    def test_a_form_flag_value_under_a_password_name_is_no_password(self, tmp_path: Path) -> None:
        post = _form_post()
        post["request"]["postData"]["text"] = "email=alice%40example.com&password=true"

        found = credential_post_redaction([_login_page(), post], tmp_path)

        assert found.redaction.secrets == ()


class TestWhenTheScrubWarns:
    async def _run(
        self, tmp_path: Path, entries: list[dict[str, Any]], page_source: str | None = None
    ) -> tuple[ObserveStorage, str, MagicMock]:
        storage = _storage(tmp_path)
        backend = _Backend(entries)
        if page_source is not None:
            backend.page_source = page_source
        console = MagicMock()
        with patch("graftpunk.observe.run.LOG") as log:
            await _save(storage, backend, console)
        return storage, _printed(console), log

    @pytest.mark.asyncio
    async def test_an_identifier_first_login_to_one_target_does_not_warn(
        self, tmp_path: Path
    ) -> None:
        first = _entry("POST", "/session", post_data=json.dumps({"email": _EMAIL}))
        second = _entry(
            "POST", "/session", post_data=json.dumps({"email": _EMAIL, "password": "sunflower99"})
        )

        storage, printed, log = await self._run(tmp_path, [_login_page(), first, second])

        assert _MISSED_LINE not in printed
        assert _UNMATCHED_LINE not in printed
        assert not _warnings(log)
        everything = _everything(storage.run_dir)
        assert b"sunflower99" not in everything
        assert _EMAIL.encode() not in everything

    @pytest.mark.asyncio
    async def test_a_cart_post_to_a_page_whose_login_form_has_no_action_does_not_warn(
        self, tmp_path: Path
    ) -> None:
        modal = (
            "<html><body><form method='post'><input name='email'>"
            "<input type='password' name='password'></form></body></html>"
        )
        page = _entry("GET", "/cart", body=modal, content_type="text/html")
        cart = _entry("POST", "/cart", post_data=json.dumps({"sku": "A-100", "qty": 2}))

        _storage_, printed, log = await self._run(tmp_path, [page, cart])

        assert "credential" not in printed
        assert _MISSED_LINE not in printed
        assert _UNMATCHED_LINE not in printed
        assert not log.warning.called

    def test_a_login_form_in_a_post_response_makes_no_later_post_a_login(
        self, tmp_path: Path
    ) -> None:
        """A form in a POST's own response is skipped, as the digest skips it: the
        later POST to where it posts is no credential post, and nothing warns."""
        header = _entry("POST", "/search", post_data=json.dumps({"q": "boots"}))
        header["response"]["content"] = {
            "mimeType": "text/html",
            "text": _LOGIN_PAGE.replace("/session", "/cart"),
        }
        cart = _entry("POST", "/cart", post_data=json.dumps({"sku": "A-100", "qty": 2}))

        found = credential_post_redaction([header, cart], tmp_path)

        assert (found.posts, found.missed, found.password_input_unmatched) == (0, 0, False)

    @pytest.mark.asyncio
    async def test_a_sign_up_whose_password_is_taken_does_not_warn(self, tmp_path: Path) -> None:
        signup = _entry(
            "POST",
            "/register",
            post_data=json.dumps(
                {"email": _EMAIL, "password": "Sign-Up-77", "password_confirmation": "Sign-Up-77"}
            ),
        )

        storage, printed, log = await self._run(tmp_path, [_entry("GET", "/"), signup])

        assert _MISSED_LINE not in printed
        assert _UNMATCHED_LINE not in printed
        assert not _warnings(log)
        assert b"Sign-Up-77" not in _everything(storage.run_dir)
        assert json.loads(_har(storage)[1]["request"]["postData"]["text"]) == {
            "email": REDACTED,
            "password": REDACTED,
            "password_confirmation": REDACTED,
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["credential", "passwort", "p"])
    async def test_a_script_posted_login_under_an_unread_name_warns(
        self, tmp_path: Path, name: str
    ) -> None:
        """The login form has no action and its password input a name the scrub does
        not read as one; a script posts it elsewhere. The value stays, and the run
        says so, with no value."""
        page_html = (
            "<html><body><form id='f'><input type='email' name='username'>"
            f"<input type='password' name='{name}'><button>Sign in</button></form></body></html>"
        )
        page = _entry("GET", "/login", body=page_html, content_type="text/html")
        post = _entry(
            "POST", "/api/auth", post_data=json.dumps({"username": _EMAIL, name: "Zebra-Plum-77"})
        )

        storage, printed, log = await self._run(tmp_path, [page, post], page_source=page_html)

        assert _UNMATCHED_LINE in printed
        assert _warnings(log)[0].kwargs == {"logins_missed": 0, "password_input_unmatched": True}
        assert "Zebra-Plum-77" not in repr(log.mock_calls) + printed
        # Pinned: the value is left; the warning is all the scrub gives.
        assert b"Zebra-Plum-77" in _everything(storage.run_dir)

    @pytest.mark.asyncio
    async def test_a_login_post_whose_body_was_not_captured_warns(self, tmp_path: Path) -> None:
        post = _entry("POST", "/session")
        post["request"]["_postDataNotCaptured"] = True

        _storage_, printed, log = await self._run(tmp_path, [_login_page(), post])

        assert f"1 request {_MISSED_LINE}" in printed
        assert _warnings(log)[0].kwargs == {"logins_missed": 1, "password_input_unmatched": False}
        assert log.warning.call_args_list[0].args[0] == "credential_scrub_body_not_captured"

    @pytest.mark.asyncio
    async def test_an_uncaptured_body_sent_where_no_login_form_posts_does_not_warn(
        self, tmp_path: Path
    ) -> None:
        post = _entry("POST", "/upload")
        post["request"]["_postDataNotCaptured"] = True

        _storage_, printed, log = await self._run(tmp_path, [_entry("GET", "/"), post])

        assert _MISSED_LINE not in printed
        assert not log.warning.called

    @pytest.mark.asyncio
    async def test_a_body_that_cannot_be_split_and_names_a_password_warns(
        self, tmp_path: Path
    ) -> None:
        post = _entry(
            "POST", "/api/auth", post_data='{"email": "alice@example.com", "password": "hun'
        )

        _storage_, printed, log = await self._run(tmp_path, [_entry("GET", "/"), post])

        assert f"1 request {_MISSED_LINE}" in printed
        assert _warnings(log)[0].kwargs == {"logins_missed": 1, "password_input_unmatched": False}


class TestKnownGaps:
    """What the docs name as left in the run, pinned so a change is seen."""

    @pytest.mark.asyncio
    async def test_a_boolean_password_is_never_taken(self, tmp_path: Path) -> None:
        storage = _storage(tmp_path)
        post = _entry("POST", "/session", post_data=json.dumps({"email": _EMAIL, "password": True}))
        console = MagicMock()

        await _save(storage, _Backend([_login_page(), post]), console)

        assert json.loads(_har(storage)[1]["request"]["postData"]["text"]) == {
            "email": REDACTED,
            "password": True,
        }
        # The page held a password input and no password was taken.
        assert _UNMATCHED_LINE in _printed(console)

    @pytest.mark.asyncio
    async def test_an_identifier_step_with_no_step_word_keeps_the_username(
        self, tmp_path: Path
    ) -> None:
        storage = _storage(tmp_path)
        first = _entry("POST", "/identify", post_data=json.dumps({"username": "alice.example"}))
        second = _entry("POST", "/session", post_data=json.dumps({"password": "sunflower99"}))
        console = MagicMock()

        await _save(storage, _Backend([_login_page(), first, second]), console)

        everything = _everything(storage.run_dir)
        assert b"alice.example" in everything
        assert b"sunflower99" not in everything
        assert _MISSED_LINE not in _printed(console)
        assert _UNMATCHED_LINE not in _printed(console)

    @pytest.mark.asyncio
    async def test_a_numeric_password_is_replaced_inside_other_numbers(
        self, tmp_path: Path
    ) -> None:
        storage = _storage(tmp_path)
        post = _entry(
            "POST", "/session", post_data='{"email": "alice@example.com", "password": 123456}'
        )
        orders = _entry("GET", "/api/orders", body='{"order_id": 91234567, "total": 5}')

        await _save(storage, _Backend([_login_page(), post, orders]))

        text = _har(storage)[2]["response"]["content"]["text"]
        assert text == f'{{"order_id": 9{REDACTED}7, "total": 5}}'
        with pytest.raises(json.JSONDecodeError):
            json.loads(text)


class TestMarkedFailedSecrets:
    def test_a_backend_that_raises_gives_none_and_logs_an_error(self) -> None:
        backend = MagicMock()
        backend.take_marked_failed_secrets.side_effect = RuntimeError("gone")

        with patch("graftpunk.observe.run.LOG") as log:
            assert _marked_failed_secrets(backend) == ()

        log.error.assert_called_once_with(
            "observe_marked_failed_secrets_failed", exc_type="RuntimeError"
        )

    @pytest.mark.parametrize(
        ("returned", "expected"),
        [("Real-Typo-44", ()), (None, ()), (["Real-Typo-44", 7], ("Real-Typo-44",))],
    )
    def test_only_a_tuple_or_list_of_strings_is_taken(
        self, returned: Any, expected: tuple[str, ...]
    ) -> None:
        backend = MagicMock()
        backend.take_marked_failed_secrets.return_value = returned

        assert _marked_failed_secrets(backend) == expected

    def test_a_backend_without_the_method_gives_none(self) -> None:
        assert _marked_failed_secrets(object()) == ()
