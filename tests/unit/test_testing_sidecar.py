"""The fixture sidecar's one owner: keys per schema version and the loader
(graft skill spec, 2026-09-21, "A sidecar with one owner")."""

from __future__ import annotations

import copy
import json
import pickle
import subprocess
import sys
from pathlib import Path

import pytest

from graftpunk.contracts import current_schema
from graftpunk.testing.sidecar import (
    FIXTURES_PLACEHOLDER,
    FLAGGED_REASONS,
    SIDECAR_FIELDS,
    Sidecar,
    SidecarError,
    is_sidecar,
    load_sidecar,
    sidecar_path,
    sidecar_payload,
    sidecar_text,
)

_V1_FIELDS = {
    "status",
    "content_type",
    "body_params",
    "capture_sha256",
    "flagged_names",
    "redacted_names",
}


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _v1(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": 1,
        "status": 200,
        "content_type": "application/json",
        "body_params": [],
        "capture_sha256": None,
        "flagged_names": [],
        "redacted_names": 0,
    }
    payload.update(overrides)
    return payload


class TestTheFormat:
    def test_version_one_is_the_committable_key_set(self) -> None:
        assert SIDECAR_FIELDS[1] == _V1_FIELDS

    def test_version_two_adds_flagged_reasons(self) -> None:
        assert SIDECAR_FIELDS[2] == _V1_FIELDS | {"flagged_reasons"}

    def test_every_version_up_to_the_current_one_has_a_key_set(self) -> None:
        assert set(SIDECAR_FIELDS) == set(range(1, current_schema("sidecar") + 1))

    def test_the_payload_writes_exactly_the_current_schemas_keys(self) -> None:
        payload = sidecar_payload(Sidecar(status=200, content_type="text/html"))
        assert set(payload) == SIDECAR_FIELDS[current_schema("sidecar")] | {"schema"}
        assert payload["schema"] == current_schema("sidecar")

    def test_a_version_with_no_key_set_is_a_sidecar_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A schema bump that forgot its SIDECAR_FIELDS entry refuses by name
        instead of raising KeyError out of the loader."""
        monkeypatch.setattr("graftpunk.testing.sidecar.SIDECAR_FIELDS", {})
        path = _write(tmp_path / "get_orders.json.meta.json", _v1())
        with pytest.raises(SidecarError, match="no key set for sidecar schema 1"):
            load_sidecar(path)

    def test_the_path_rule(self, tmp_path: Path) -> None:
        fixture = tmp_path / "get_orders.json"
        assert sidecar_path(fixture) == tmp_path / "get_orders.json.meta.json"
        assert is_sidecar(sidecar_path(fixture))
        assert not is_sidecar(fixture)


class TestLoad:
    def test_round_trip(self, tmp_path: Path) -> None:
        sidecar = Sidecar(
            status=403,
            content_type="text/plain",
            body_params=("page",),
            capture_sha256="ab" * 32,
            flagged_reasons={"shop_session": ("session_name", "http_only")},
        )
        path = tmp_path / "x.json.meta.json"
        path.write_text(sidecar_text(sidecar), encoding="utf-8")
        assert load_sidecar(path) == sidecar
        assert load_sidecar(path).flagged_names == ("shop_session",)
        assert load_sidecar(path).flagged_reasons == {"shop_session": ("http_only", "session_name")}

    def test_a_schema_1_sidecar_still_loads_without_reasons(self, tmp_path: Path) -> None:
        """Sidecars committed under schema 1 load unchanged; they recorded no
        reasons."""
        path = tmp_path / "x.json.meta.json"
        path.write_text(json.dumps(_v1(flagged_names=["shop_session"])), encoding="utf-8")
        sidecar = load_sidecar(path)
        assert sidecar.flagged_names == ("shop_session",)
        assert sidecar.flagged_reasons == {}

    def test_a_schema_1_key_set_is_refused_under_schema_2(self, tmp_path: Path) -> None:
        path = tmp_path / "x.json.meta.json"
        path.write_text(json.dumps(_v1(schema=2)), encoding="utf-8")
        with pytest.raises(SidecarError, match="missing keys: flagged_reasons"):
            load_sidecar(path)

    @pytest.mark.parametrize(
        ("names", "reasons", "match"),
        [
            (["a"], {}, "keyed by exactly the flagged names"),
            (["a"], {"a": ["session_name"], "b": ["token_name"]}, "keyed by exactly"),
            (["a"], {"a": ["made_up"]}, "unknown reasons: made_up"),
            (["a"], {"a": []}, "must name a reason that flags it"),
            (["a"], {"a": ["sent_only"]}, "must name a reason that flags it"),
            (["a"], {"a": "session_name"}, "object of lists of strings"),
            (["a"], ["a"], "object of lists of strings"),
        ],
        ids=["missing", "extra", "unknown", "empty", "sent-only-alone", "string", "list"],
    )
    def test_schema_2_reasons_are_held_to_the_flagged_names_and_the_vocabulary(
        self, tmp_path: Path, names: list[str], reasons: object, match: str
    ) -> None:
        path = tmp_path / "x.json.meta.json"
        payload = _v1(schema=2, flagged_names=names, flagged_reasons=reasons)
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(SidecarError, match=match):
            load_sidecar(path)

    def test_a_sidecar_with_names_and_no_reasons_cannot_be_written(self) -> None:
        """The current schema records a reason for every flagged name, so a
        schema 1 sidecar's facts are refused rather than written as an
        unloadable file."""
        with pytest.raises(SidecarError, match="build the Sidecar with flagged_reasons"):
            sidecar_text(Sidecar(status=200, content_type="text/plain", flagged_names=("a",)))

    def test_reasons_changed_after_construction_are_refused_at_write(self) -> None:
        """The reasons are a plain dict, so a Sidecar pickles and copies as in
        1.26.0; the writer checks them again, so it never writes a file the
        loader refuses."""
        reasons = {"a": ["session_name"]}
        sidecar = Sidecar(status=200, content_type="text/plain", flagged_reasons=reasons)
        reasons["a"].append("made_up")
        assert sidecar.flagged_reasons == {"a": ("session_name",)}
        assert pickle.loads(pickle.dumps(sidecar)) == sidecar  # noqa: S301 - its own bytes
        assert copy.deepcopy(sidecar) == sidecar
        sidecar.flagged_reasons["a"] = ("made_up",)
        with pytest.raises(SidecarError, match="unknown reasons: made_up"):
            sidecar_text(sidecar)

    def test_the_vocabulary_is_the_digests(self) -> None:
        """The sidecar imports nothing from the digest, so the two spellings of the
        reasons are held equal here."""
        from graftpunk.har.digest import FLAGGED_REASONS as DIGEST_REASONS

        assert set(DIGEST_REASONS) == FLAGGED_REASONS

    def test_a_missing_schema_is_refused(self, tmp_path: Path) -> None:
        payload = _v1()
        del payload["schema"]
        with pytest.raises(SidecarError, match="sidecar: no schema number"):
            load_sidecar(_write(tmp_path / "a.meta.json", payload))

    def test_an_unknown_schema_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SidecarError, match="schema 99"):
            load_sidecar(_write(tmp_path / "a.meta.json", _v1(schema=99)))

    def test_a_key_outside_the_version_is_refused(self, tmp_path: Path) -> None:
        """An old-format or hand-edited sidecar cannot smuggle a URL in."""
        with pytest.raises(SidecarError, match="url"):
            load_sidecar(_write(tmp_path / "a.meta.json", _v1(url="https://myshop.example/")))

    def test_a_sidecar_that_is_not_utf8_is_refused_naming_the_path(self, tmp_path: Path) -> None:
        path = tmp_path / "a.meta.json"
        text = json.dumps(_v1(content_type="caf\u00e9"), ensure_ascii=False)
        path.write_bytes(text.encode("latin-1"))
        with pytest.raises(SidecarError, match="a.meta.json"):
            load_sidecar(path)

    def test_a_missing_key_is_refused(self, tmp_path: Path) -> None:
        payload = _v1()
        del payload["flagged_names"]
        with pytest.raises(SidecarError, match="flagged_names"):
            load_sidecar(_write(tmp_path / "a.meta.json", payload))

    @pytest.mark.parametrize(
        "payload",
        [
            ["not", "an", "object"],
            "text",
            _v1(status="200"),
            _v1(body_params="page"),
            _v1(status=True),
            _v1(capture_sha256=123),
            _v1(flagged_names=["ok", 5]),
            _v1(schema=True),
            _v1(schema=1.0),
        ],
        ids=[
            "not-an-object",
            "text",
            "status-a-string",
            "body_params-a-string",
            "status-a-bool",
            "capture_sha256-not-a-string",
            "flagged_names-item-not-a-string",
            "schema-a-bool",
            "schema-a-float",
        ],
    )
    def test_a_sidecar_that_is_not_an_object_or_has_a_wrong_type_is_refused(
        self, tmp_path: Path, payload: object
    ) -> None:
        path = _write(tmp_path / "a.meta.json", payload)
        with pytest.raises(SidecarError, match="a.meta.json"):
            load_sidecar(path)

    def test_declared_means_no_capture_hash(self) -> None:
        assert Sidecar(status=200, content_type="x").declared
        assert not Sidecar(status=200, content_type="x", capture_sha256="ab" * 32).declared


class TestCaptureSha256Format:
    """Enforced in Sidecar construction (Sidecar.__post_init__), so a bad hash
    cannot come out of the writer either, and load_sidecar names the path when
    it surfaces on read."""

    @pytest.mark.parametrize(
        "value",
        [
            "too-short",
            "g" * 64,  # not a hex digit
            "AB" + "a" * 62,  # uppercase: the format is lowercase only
            "a" * 63,  # one short
            "a" * 65,  # one long
            "",
        ],
        ids=["too-short", "non-hex-char", "uppercase", "one-short", "one-long", "empty"],
    )
    def test_a_malformed_hash_is_refused_on_construction(self, value: str) -> None:
        with pytest.raises(SidecarError, match="capture_sha256"):
            Sidecar(status=200, content_type="x", capture_sha256=value)

    def test_a_malformed_hash_is_refused_on_load_naming_the_path(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "a.meta.json", _v1(capture_sha256="not-a-hash"))
        with pytest.raises(SidecarError, match="a.meta.json"):
            load_sidecar(path)

    def test_none_is_accepted(self) -> None:
        assert Sidecar(status=200, content_type="x", capture_sha256=None).capture_sha256 is None

    def test_a_valid_hash_is_accepted(self) -> None:
        value = "ab" * 32
        sidecar = Sidecar(status=200, content_type="x", capture_sha256=value)
        assert sidecar.capture_sha256 == value


class TestNormalisation:
    """The sort and dedupe of body_params and flagged_names is Sidecar's own job:
    a Sidecar built directly (not only through write_sidecar) still serializes
    sorted and deduped."""

    def test_body_params_and_flagged_names_are_sorted_and_deduped_on_construction(self) -> None:
        sidecar = Sidecar(
            status=200,
            content_type="x",
            body_params=("page", "archived", "page"),
            flagged_names=("shop_session", "csrf-token", "shop_session"),
        )
        assert sidecar.body_params == ("archived", "page")
        assert sidecar.flagged_names == ("csrf-token", "shop_session")


def test_sidecar_error_is_not_a_value_error() -> None:
    """Plugin command code routinely catches ValueError around a fixture-backed
    .json() call; a malformed sidecar must not be swallowed by that catch."""
    assert not issubclass(SidecarError, ValueError)


def test_graftpunk_testing_imports_nothing_from_devtools() -> None:
    """The placement rule, asserted on the import graph rather than a docstring."""
    script = (
        "import sys\n"
        "import graftpunk.testing, graftpunk.testing.sidecar\n"
        "print(sorted(m for m in sys.modules if m.startswith('graftpunk.devtools')))\n"
    )
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout.strip() == "[]"


class TestRedactedNames:
    def test_the_count_round_trips(self, tmp_path: Path) -> None:
        sidecar = Sidecar(status=200, content_type="application/json", redacted_names=3)
        path = tmp_path / "get_orders.json.meta.json"
        path.write_text(sidecar_text(sidecar), encoding="utf-8")
        assert load_sidecar(path).redacted_names == 3

    @pytest.mark.parametrize("value", ["3", -1, True, 1.5, None])
    def test_a_count_that_is_not_a_non_negative_integer_is_refused(
        self, tmp_path: Path, value: object
    ) -> None:
        path = _write(tmp_path / "get_orders.json.meta.json", _v1(redacted_names=value))
        with pytest.raises(SidecarError, match="redacted_names"):
            load_sidecar(path)


class TestEveryParseFailureIsASidecarError:
    """A sidecar that breaks the JSON parser in any way is a SidecarError."""

    @pytest.mark.parametrize(
        "text",
        [
            '{"schema": 1, "status": ' + "1" * 5000 + "}",
            "[" * 200_000 + "]" * 200_000,
        ],
        ids=["int-string-limit", "deep-nesting"],
    )
    def test_a_parser_failure_is_a_sidecar_error(self, tmp_path: Path, text: str) -> None:
        path = tmp_path / "get_orders.json.meta.json"
        path.write_text(text)
        with pytest.raises(SidecarError, match="get_orders.json.meta.json"):
            load_sidecar(path)


class TestASidecarBuiltAnywhereIsFitToSerialize:
    """Sidecar refuses at construction what the loader refuses on disk."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"status": "200", "content_type": "application/json"},
            {"status": True, "content_type": "application/json"},
            {"status": 200, "content_type": 1},
            {"status": 200, "content_type": "application/json", "body_params": ("a", 1)},
            {"status": 200, "content_type": "application/json", "flagged_names": (None,)},
            {"status": 200, "content_type": "application/json", "redacted_names": -1},
            {"status": 200, "content_type": "application/json", "redacted_names": "2"},
        ],
        ids=[
            "status-text",
            "status-bool",
            "content-type-int",
            "body-param-int",
            "flagged-none",
            "redacted-negative",
            "redacted-text",
        ],
    )
    def test_a_wrong_type_or_sign_is_refused(self, kwargs: dict) -> None:
        with pytest.raises(SidecarError):
            Sidecar(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"body_params": "abc"},
        {"flagged_names": {"shop_session"}},
        {"body_params": None},
    ],
    ids=["string", "set", "none"],
)
def test_the_name_fields_must_be_a_tuple_or_list(kwargs: dict) -> None:
    """A string is iterable but is not a list of names."""
    with pytest.raises(SidecarError):
        Sidecar(status=200, content_type="application/json", **kwargs)


def test_the_placeholder_is_the_gitkeep() -> None:
    assert FIXTURES_PLACEHOLDER == ".gitkeep"
