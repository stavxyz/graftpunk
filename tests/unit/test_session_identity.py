"""The name@label grammar, derivation, and resolution policy (#151, #293)."""

from __future__ import annotations

import pytest

from graftpunk.exceptions import AmbiguousSessionError, GraftpunkError
from graftpunk.session_identity import (
    GP_ACCOUNT_ATTR,
    IDENTIFIER_FIELD_NAMES,
    _legacy_account_field,
    _legacy_derivable,
    _legacy_derive_account_identity,
    _legacy_secret_field,
    account_field,
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


#: Field names from the issue, both reviews of #300, the docs, the YAML template
#: and the scaffold, mixed into every ordered pair and triple below.
FIELD_NAME_POOL = [
    "username",
    "user_name",
    "User Name",
    "email",
    "email_address",
    "user_email",
    "login_email",
    "login",
    "login_name",
    "login_id",
    "identifier",
    "user",
    "userid",
    "user_id",
    "account",
    "account_id",
    "loginCode",
    "userPin",
    "userPINCode",
    "user2FA",
    "user_pass",
    "password",
    "otp",
    "shipping_email",
    "j_username",
    "secret_key",
]


def _field_sets() -> list[dict[str, str]]:
    sets = []
    for first in FIELD_NAME_POOL:
        sets.append({first: "v-" + first})
        for second in FIELD_NAME_POOL:
            if second == first:
                continue
            sets.append({first: "v-" + first, second: "v-" + second})
            for third in ("password", "user_id", "email", "loginCode"):
                if third not in (first, second):
                    sets.append({first: "v-" + first, second: "v-" + second, third: "v-" + third})
    return sets


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

    def test_the_label_is_the_1_24_label_whenever_1_24_picked_an_identifier(self) -> None:
        """Ruling for #300: a label that did not come from a secret never changes."""
        checked = 0
        for fields in _field_sets():
            legacy_field = _legacy_account_field(fields, SECRETS)
            if legacy_field is not None and is_identifier_field(legacy_field):
                assert derive_account_identity(fields) == _legacy_derive_account_identity(
                    fields, SECRETS
                ), fields
                checked += 1
            picked = account_field(fields)
            assert picked is None or is_identifier_field(picked), fields
        assert checked > 1000

    @pytest.mark.parametrize(
        ("fields", "label"),
        [
            (
                {"user_name": "alice", "email": "alice@example.com", "password": "p"},
                "alice-example-com",
            ),
            (
                {"user_name": "alice", "login": "alice@example.com", "password": "p"},
                "alice-example-com",
            ),
            ({"user_email": "a@example.com", "email": "b@example.com"}, "a-example-com"),
            ({"login_email": "a@example.com", "email": "b@example.com"}, "a-example-com"),
            ({"userid": "a", "user": "b"}, "a"),
            ({"login_name": "a", "login": "b"}, "a"),
            ({"email_address": "a@x", "email": "b@x"}, "a-x"),
            ({"login_id": "a", "login": "b"}, "a"),
            ({"user_id": "a", "user": "b"}, "a"),
            ({"identifier": "alice@example.com", "user": "bob"}, "alice-example-com"),
            ({"username": "alice", "password": "p"}, "alice"),
            ({"password": "p", "username": "alice"}, "alice"),
            ({"email": "alice@example.com", "secret_key": "k"}, "alice-example-com"),
        ],
    )
    def test_the_reviewers_and_docs_field_sets_keep_their_label(
        self, fields: dict[str, str], label: str
    ) -> None:
        assert derive_account_identity(fields)[1] == label
        assert _legacy_derive_account_identity(fields, SECRETS)[1] == label

    @pytest.mark.parametrize("field_name", sorted(IDENTIFIER_FIELD_NAMES))
    def test_every_allowlisted_name_passes_the_old_deny_list(self, field_name: str) -> None:
        """Why the current rule equals the old one on identifier fields."""
        assert _legacy_derivable(field_name, SECRETS)

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

    @pytest.mark.parametrize("field_name", sorted(IDENTIFIER_FIELD_NAMES))
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
            # Allowlisted for observe redaction, but holding no label keyword as a
            # substring: 1.24.0
            # never named a session after them, and neither does this release.
            "account",
            "accountId",
            "account_id",
            "e_mail",
        ],
    )
    def test_any_other_name_gives_no_label(self, field_name: str) -> None:
        assert derive_account_identity({field_name: "someone", "password": "pw-x"}) == (
            None,
            None,
        )

    def test_an_empty_identifier_falls_through_to_the_next(self) -> None:
        fields = {"username": "", "email": "a@example.com"}
        assert derive_account_identity(fields)[0] == "a@example.com"

    @pytest.mark.parametrize(
        ("field_names", "secret_field"),
        [
            (["userPin", "password"], "userPin"),
            (["loginCode", "user_id"], "loginCode"),
            (["user2FA"], "user2FA"),
            (["user_pass", "password"], "user_pass"),
            (["username", "password"], None),
            (["user_name", "email"], None),
            (["identifier", "password"], None),
            (["account", "password"], None),
            (["otp", "password"], None),
        ],
    )
    def test_the_plugins_1_24_secret_field(
        self, field_names: list[str], secret_field: str | None
    ) -> None:
        assert _legacy_secret_field(field_names, SECRETS) == secret_field

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
