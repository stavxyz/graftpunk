"""The fixture sidecar's one owner: keys per schema version and the loader
(graft skill spec, 2026-09-21, "A sidecar with one owner")."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from graftpunk.contracts import current_schema
from graftpunk.testing.sidecar import (
    SIDECAR_FIELDS,
    Sidecar,
    SidecarError,
    is_sidecar,
    load_sidecar,
    sidecar_path,
    sidecar_payload,
    sidecar_text,
)

_V1_FIELDS = {"status", "content_type", "body_params", "capture_sha256", "flagged_names"}


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
    }
    payload.update(overrides)
    return payload


class TestTheFormat:
    def test_version_one_is_the_committable_key_set(self) -> None:
        assert SIDECAR_FIELDS[1] == _V1_FIELDS

    def test_every_version_up_to_the_current_one_has_a_key_set(self) -> None:
        assert set(SIDECAR_FIELDS) == set(range(1, current_schema("sidecar") + 1))

    def test_the_payload_writes_exactly_the_current_schemas_keys(self) -> None:
        payload = sidecar_payload(Sidecar(status=200, content_type="text/html"))
        assert set(payload) == SIDECAR_FIELDS[current_schema("sidecar")] | {"schema"}
        assert payload["schema"] == current_schema("sidecar")

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
            flagged_names=("shop_session",),
        )
        path = tmp_path / "x.json.meta.json"
        path.write_text(sidecar_text(sidecar), encoding="utf-8")
        assert load_sidecar(path) == sidecar

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

    def test_a_missing_key_is_refused(self, tmp_path: Path) -> None:
        payload = _v1()
        del payload["flagged_names"]
        with pytest.raises(SidecarError, match="flagged_names"):
            load_sidecar(_write(tmp_path / "a.meta.json", payload))

    @pytest.mark.parametrize(
        "payload",
        [["not", "an", "object"], "text", _v1(status="200"), _v1(body_params="page")],
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
