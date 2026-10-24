"""The name@label grammar, derivation, and resolution policy (#151, #293)."""

from __future__ import annotations

import pytest

from graftpunk.exceptions import AmbiguousSessionError, GraftpunkError
from graftpunk.session_identity import (
    GP_ACCOUNT_ATTR,
    IDENTIFIER_FIELD_NAMES,
    _legacy_derive_account_identity,
    derive_account_identity,
    is_identifier_field,
    join_session_name,
    resolve_account_session,
    split_session_name,
    validate_session_name,
)

SECRETS = frozenset({"password", "secret", "token", "key"})


class TestGrammar:
    def test_split_bare_and_labelled(self) -> None:
        assert split_session_name("myshop") == ("myshop", None)
        assert split_session_name("myshop@alice") == ("myshop", "alice")

    def test_join_round_trips(self) -> None:
        assert join_session_name("myshop", "alice") == "myshop@alice"
        assert join_session_name("myshop", None) == "myshop"
        base, label = split_session_name(join_session_name("myshop", "alice"))
        assert (base, label) == ("myshop", "alice")


class TestValidation:
    @pytest.mark.parametrize("name", ["myshop", "my-shop_2", "myshop@alice", "a@b"])
    def test_valid(self, name: str) -> None:
        validate_session_name(name)

    @pytest.mark.parametrize(
        "name", ["", "@alice", "myshop@", "a@b@c", "My.Shop", "myshop@Al.ice", "-x"]
    )
    def test_invalid(self, name: str) -> None:
        with pytest.raises(ValueError):
            validate_session_name(name)

    def test_cache_reexport_still_works(self) -> None:
        from graftpunk.cache import validate_session_name as cache_validate

        cache_validate("myshop@alice")


class TestResolution:
    def test_zero_candidates_returns_base(self) -> None:
        assert resolve_account_session("myshop", ["other"]) == "myshop"

    def test_one_bare_candidate(self) -> None:
        assert resolve_account_session("myshop", ["myshop", "other"]) == "myshop"

    def test_one_labelled_candidate(self) -> None:
        assert resolve_account_session("myshop", ["myshop@alice"]) == "myshop@alice"

    def test_several_raise_typed_error_with_candidates(self) -> None:
        with pytest.raises(AmbiguousSessionError) as exc:
            resolve_account_session("myshop", ["myshop", "myshop@alice", "other@bob"])
        assert issubclass(AmbiguousSessionError, GraftpunkError)
        assert exc.value.base_name == "myshop"
        assert exc.value.candidates == ["myshop", "myshop@alice"]

    def test_exported_from_package(self) -> None:
        import graftpunk

        assert "AmbiguousSessionError" in graftpunk.__all__


#: The issue's table (#293): fields whose secret value graftpunk 1.24.0 made the
#: account label. Each row is (fields, the secret 1.24.0 derived).
ISSUE_293_ROWS = [
    pytest.param({"userPin": "4417", "password": "pw-x"}, "4417", id="userPin"),
    pytest.param({"userPINCode": "4417"}, "4417", id="userPINCode"),
    pytest.param({"user2FA": "123456"}, "123456", id="user2FA"),
    pytest.param({"loginCode": "829341", "user_id": "bob"}, "829341", id="loginCode-beats-user_id"),
    pytest.param({"user_pass": "hunter2x", "password": "pw-x"}, "hunter2x", id="user_pass"),
]


class TestDerivation:
    @pytest.mark.parametrize(("fields", "secret"), ISSUE_293_ROWS)
    def test_a_secret_is_never_the_label(self, fields: dict[str, str], secret: str) -> None:
        identifier, label = derive_account_identity(fields)
        assert identifier != secret
        assert label is None or secret not in label

    @pytest.mark.parametrize(("fields", "secret"), ISSUE_293_ROWS)
    def test_the_legacy_rule_still_finds_the_secret(
        self, fields: dict[str, str], secret: str
    ) -> None:
        """The migration depends on the old rule reproducing the 1.24.0 label."""
        assert _legacy_derive_account_identity(fields, SECRETS) == (secret, secret)

    def test_a_login_code_beside_user_id_gives_the_user_id(self) -> None:
        assert derive_account_identity({"loginCode": "829341", "user_id": "bob"}) == ("bob", "bob")

    @pytest.mark.parametrize(
        "field_name",
        [
            "username",
            "user_name",
            "UserName",
            "user-name",
            "user.name",
            "user name",
            "email",
            "Email",
            "e_mail",
            "emailAddress",
            "email_address",
            "Email-Address",
            "login",
            "LOGIN",
            "loginId",
            "login_id",
            "loginName",
            "login-name",
            "user",
            "userId",
            "user_id",
            "USER_ID",
            "account",
            "accountId",
            "account_id",
            "identifier",
            "Identifier",
            "user_email",
            "userEmail",
            "user-email",
            "login_email",
            "loginEmail",
            "LOGIN_EMAIL",
        ],
    )
    def test_allowlisted_names_give_a_label(self, field_name: str) -> None:
        fields = {field_name: "Alice@Example.com", "password": "pw-x"}
        assert derive_account_identity(fields) == ("Alice@Example.com", "alice-example-com")

    @pytest.mark.parametrize("field_name", list(IDENTIFIER_FIELD_NAMES))
    def test_every_allowlisted_name_is_an_identifier(self, field_name: str) -> None:
        assert is_identifier_field(field_name)

    def test_observe_redaction_uses_the_same_classifier(self) -> None:
        from graftpunk.observe import run

        assert run.is_identifier_field is is_identifier_field
        assert run.IDENTIFIER_FIELD_NAMES is IDENTIFIER_FIELD_NAMES

    @pytest.mark.parametrize(
        "field_name",
        [
            "passphrase",
            "otp",
            "pin",
            "login_pin",
            "security_answer",
            "login_code",
            "account_no",
            "login_token",
            "shipping_email",
            "pinnacle_user",
            "j_username",
            "member_number",
        ],
    )
    def test_any_other_name_gives_no_label(self, field_name: str) -> None:
        assert derive_account_identity({field_name: "someone", "password": "pw-x"}) == (
            None,
            None,
        )

    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({"email": "a@example.com", "username": "alice"}, "alice"),
            ({"user_id": "u1", "email": "a@example.com"}, "a@example.com"),
            ({"user": "u1", "login": "l1"}, "l1"),
            ({"account": "acct1", "user": "u1"}, "u1"),
            ({"accountId": "acct1", "account": "acct2"}, "acct2"),
            ({"emailAddress": "b@example.com", "email": "a@example.com"}, "a@example.com"),
            ({"identifier": "i1", "user_email": "a@example.com"}, "a@example.com"),
            ({"user": "u1", "identifier": "i1"}, "i1"),
            ({"login_email": "b@example.com", "user_email": "a@example.com"}, "a@example.com"),
        ],
    )
    def test_several_identifiers_pick_by_allowlist_order(
        self, fields: dict[str, str], expected: str
    ) -> None:
        assert derive_account_identity(fields)[0] == expected

    @pytest.mark.parametrize(
        "fields",
        [
            {"identifier": "alice@example.com", "password": "pw-x"},
            {"user_email": "alice@example.com", "password": "pw-x"},
            {"loginEmail": "alice@example.com", "password": "pw-x"},
            {"identifier": "alice@example.com", "user": "bob"},
            {"login_email": "alice@example.com", "identifier": "carol"},
        ],
    )
    def test_names_the_old_rule_accepted_keep_their_label(self, fields: dict[str, str]) -> None:
        """No rename at the next login for these: both rules pick the same identifier."""
        assert derive_account_identity(fields) == _legacy_derive_account_identity(fields, SECRETS)

    def test_same_normalized_name_picks_the_first_declared(self) -> None:
        fields = {"user_name": "first", "UserName": "second"}
        assert derive_account_identity(fields)[0] == "first"

    def test_an_empty_identifier_falls_through_to_the_next(self) -> None:
        fields = {"username": "", "email": "a@example.com"}
        assert derive_account_identity(fields)[0] == "a@example.com"

    def test_unlabelable_identifier_falls_back_to_bare_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defense in depth: today's slugify cannot emit a label that fails
        # validate_account_label, so the guard is exercised with a stub. A
        # rejected label must not become a session label; the identifier is
        # still recorded (B-M7).
        import graftpunk.session_identity as si

        monkeypatch.setattr(si, "slugify", lambda value: "-nope-")
        identifier, label = si.derive_account_identity({"username": "alice"})
        assert identifier == "alice"
        assert label is None

    def test_all_secret_or_empty_yields_none(self) -> None:
        assert derive_account_identity({"password": "x"}) == (None, None)
        assert derive_account_identity({"username": ""}) == (None, None)

    def test_label_is_slugified(self) -> None:
        identifier, label = derive_account_identity({"email": "Alice.B@Example.com"})
        assert identifier == "Alice.B@Example.com"
        assert label == "alice-b-example-com"

    def test_attr_name_constant(self) -> None:
        assert GP_ACCOUNT_ATTR == "_gp_account_identifier"


class TestOperatingName:
    def test_callable_names_are_not_listed_when_a_pin_wins(self) -> None:
        from graftpunk.session_identity import compute_operating_session_name

        calls = 0

        def listing() -> list[str]:
            nonlocal calls
            calls += 1
            return ["myshop@alice"]

        assert (
            compute_operating_session_name("myshop@bob", "myshop", listing, use_ambient=False)
            == "myshop@bob"
        )
        assert calls == 0
        assert (
            compute_operating_session_name(None, "myshop", listing, use_ambient=False)
            == "myshop@alice"
        )
        assert calls == 1

    def test_caller_supplied_ambient_is_used_without_reading_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.session_identity import compute_operating_session_name

        monkeypatch.setenv("GRAFTPUNK_SESSION", "myshop@env")
        got = compute_operating_session_name(None, "myshop", lambda: [], ambient="myshop@caller")
        assert got == "myshop@caller"

    @pytest.mark.parametrize("bad", ["../../x", "MyShop@Alice"])
    def test_invalid_pins_raise_before_any_listing(self, bad: str) -> None:
        from graftpunk.session_identity import compute_operating_session_name

        def listing() -> list[str]:
            raise AssertionError("storage must not be touched for an invalid pin")

        with pytest.raises(ValueError):
            compute_operating_session_name(bad, "myshop", listing, use_ambient=False)
        with pytest.raises(ValueError):
            compute_operating_session_name(None, "myshop", listing, ambient=bad)

    def test_explicit_flag_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from graftpunk.session_identity import compute_operating_session_name

        monkeypatch.setenv("GRAFTPUNK_SESSION", "myshop@env")
        got = compute_operating_session_name("myshop@flag", "myshop", ["myshop@a", "myshop@b"])
        assert got == "myshop@flag"

    def test_env_beats_file_and_resolution(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        from graftpunk.session_identity import compute_operating_session_name

        monkeypatch.setenv("GRAFTPUNK_SESSION", "myshop@env")
        (tmp_path / ".gp-session").write_text("myshop@file")
        monkeypatch.chdir(tmp_path)
        got = compute_operating_session_name(None, "myshop", ["myshop@a", "myshop@b"])
        assert got == "myshop@env"

    def test_file_beats_resolution(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        from graftpunk.session_identity import compute_operating_session_name

        monkeypatch.delenv("GRAFTPUNK_SESSION", raising=False)
        (tmp_path / ".gp-session").write_text("myshop@file")
        monkeypatch.chdir(tmp_path)
        got = compute_operating_session_name(None, "myshop", ["myshop@a", "myshop@b"])
        assert got == "myshop@file"

    def test_falls_through_to_account_resolution(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from graftpunk.session_identity import compute_operating_session_name

        monkeypatch.delenv("GRAFTPUNK_SESSION", raising=False)
        monkeypatch.chdir(tmp_path)
        assert compute_operating_session_name(None, "myshop", ["myshop@a"]) == "myshop@a"

    def test_use_ambient_false_skips_env_and_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from graftpunk.session_identity import compute_operating_session_name

        monkeypatch.setenv("GRAFTPUNK_SESSION", "myshop@env")
        (tmp_path / ".gp-session").write_text("myshop@file")
        monkeypatch.chdir(tmp_path)
        got = compute_operating_session_name(None, "myshop", ["myshop@a"], use_ambient=False)
        assert got == "myshop@a"

    def test_foreign_base_ambient_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from graftpunk.session_identity import compute_operating_session_name

        monkeypatch.setenv("GRAFTPUNK_SESSION", "myshop@alice")
        monkeypatch.chdir(tmp_path)
        got = compute_operating_session_name(None, "othersite", ["othersite@bob"])
        assert got == "othersite@bob"

    def test_matching_base_ambient_still_pins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from graftpunk.session_identity import compute_operating_session_name

        monkeypatch.setenv("GRAFTPUNK_SESSION", "myshop@alice")
        monkeypatch.chdir(tmp_path)
        got = compute_operating_session_name(None, "myshop", ["myshop@alice", "myshop@bob"])
        assert got == "myshop@alice"
