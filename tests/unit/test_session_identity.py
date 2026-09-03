"""The name@label grammar, derivation, and resolution policy (#151)."""

from __future__ import annotations

import pytest

from graftpunk.exceptions import AmbiguousSessionError, GraftpunkError
from graftpunk.session_identity import (
    GP_ACCOUNT_ATTR,
    derive_account_identity,
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


class TestDerivation:
    def test_keyword_major_order(self) -> None:
        fields = {"login_token": "t", "email": "a@example.com", "username": "alice"}
        identifier, label = derive_account_identity(fields, SECRETS)
        assert identifier == "alice"  # username keyword checked before email
        assert label == "alice"

    def test_user_substring_overlap_is_harmless(self) -> None:
        identifier, _ = derive_account_identity({"user": "bob", "password": "x"}, SECRETS)
        assert identifier == "bob"

    def test_no_identifier_shaped_field_yields_none(self) -> None:
        # No identifier keyword matches: the session keeps its bare name
        # rather than persisting an arbitrary field value (#151).
        assert derive_account_identity({"account_no": "42", "password": "x"}, SECRETS) == (
            None,
            None,
        )

    @pytest.mark.parametrize(
        "fields",
        [
            {"passphrase": "hunter2"},
            {"otp": "123456", "password": "x"},
            {"pin": "9999", "password": "x"},
            {"security_answer": "fluffy", "password": "x"},
            {"login_code": "abc123", "password": "x"},
            {"account_no": "42", "password": "x"},
        ],
    )
    def test_non_identifier_fields_are_never_derived(self, fields: dict[str, str]) -> None:
        assert derive_account_identity(fields, SECRETS) == (None, None)

    def test_identifier_shaped_field_still_derives(self) -> None:
        fields = {"login_code": "abc123", "username": "alice", "password": "x"}
        assert derive_account_identity(fields, SECRETS) == ("alice", "alice")

    def test_unlabelable_identifier_falls_back_to_bare_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defense in depth: today's slugify cannot emit a label that fails
        # validate_account_label, so the guard is exercised with a stub. A
        # rejected label must not become a session label; the identifier is
        # still recorded (B-M7).
        import graftpunk.session_identity as si

        monkeypatch.setattr(si, "slugify", lambda value: "-nope-")
        identifier, label = si.derive_account_identity({"username": "alice"}, SECRETS)
        assert identifier == "alice"
        assert label is None

    def test_all_secret_or_empty_yields_none(self) -> None:
        assert derive_account_identity({"password": "x"}, SECRETS) == (None, None)
        assert derive_account_identity({"username": ""}, SECRETS) == (None, None)

    def test_label_is_slugified(self) -> None:
        identifier, label = derive_account_identity({"email": "Alice.B@Example.com"}, SECRETS)
        assert identifier == "Alice.B@Example.com"
        assert label == "alice-b-example-com"

    def test_attr_name_constant(self) -> None:
        assert GP_ACCOUNT_ATTR == "_gp_account_identifier"

    def test_secret_field_skipped_even_when_identifier_keyword_matches(self) -> None:
        fields = {"login_token": "tok-abc", "username": "alice@example.com", "password": "x"}
        identifier, label = derive_account_identity(fields, SECRETS)
        assert identifier == "alice@example.com"
        assert label == "alice-example-com"

    def test_all_fields_secret_yields_none(self) -> None:
        assert derive_account_identity({"api_token": "t", "password": "x"}, SECRETS) == (
            None,
            None,
        )


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
