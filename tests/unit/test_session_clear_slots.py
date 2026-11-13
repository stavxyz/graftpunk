"""Which slot a clear removes, and the names no backend deletes under (#382)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from graftpunk import cache as cache_mod
from graftpunk.cli import plugin_commands
from graftpunk.cli.session_commands import session_app
from graftpunk.exceptions import StorageError
from graftpunk.storage.base import SessionMetadata, metadata_to_dict
from graftpunk.storage.local import LocalSessionStorage

_BAD_NAMES = ["", ".", "..", "a/b", "../myshop", "myshop/..", "a\\b", "my\x00shop", "my\nshop"]


def _metadata(name: str) -> SessionMetadata:
    now = datetime.now(UTC)
    return SessionMetadata(
        name=name,
        checksum="x",
        created_at=now,
        modified_at=now,
        expires_at=now + timedelta(hours=1),
        domain="myshop.example.com",
        current_url="https://myshop.example.com/",
        cookie_count=1,
        cookie_domains=["myshop.example.com"],
        status="active",
    )


@pytest.fixture
def local_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
    cache_mod._reset_session_storage_backend()
    try:
        yield cache_mod._get_session_storage_backend()
    finally:
        cache_mod._reset_session_storage_backend()


def _clear(*args: str):
    app = typer.Typer()
    app.add_typer(session_app, name="session")
    return CliRunner().invoke(app, ["session", "clear", *args], env={"COLUMNS": "200"})


def _issue_layout(backend) -> Path:
    """The #382 shape: leftovers under the bare base, a real labelled slot beside it."""
    backend.save_session("myshop@jane", b"data", _metadata("myshop@jane"))
    slot = backend.base_dir / "myshop"
    slot.mkdir(parents=True)
    (slot / "metadata.json.pre-test2").write_text("{}")
    (slot / "session.pickle.pre-test2").write_bytes(b"data")
    return slot


class TestTheNamedSlotIsTheOneCleared:
    def test_a_plugin_site_name_clears_its_leftovers_not_the_labelled_session(
        self, local_backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(plugin_commands._plugin_session_map, "myshop", "myshop")
        slot = _issue_layout(local_backend)

        result = _clear("myshop", "--force")

        assert result.exit_code == 0, result.output
        assert "myshop/metadata.json.pre-test2" in result.output
        assert not slot.exists()
        assert (local_backend.base_dir / "myshop@jane" / "metadata.json").exists()

    def test_a_plugin_alias_clears_its_base_s_leftovers(
        self, local_backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(plugin_commands._plugin_session_map, "shop", "myshop")
        slot = _issue_layout(local_backend)

        result = _clear("shop", "--force")

        assert result.exit_code == 0, result.output
        assert not slot.exists()
        assert (local_backend.base_dir / "myshop@jane" / "metadata.json").exists()

    def test_a_bare_name_that_is_no_plugin_clears_its_leftovers(self, local_backend) -> None:
        slot = _issue_layout(local_backend)

        result = _clear("myshop", "--force")

        assert result.exit_code == 0, result.output
        assert not slot.exists()
        assert (local_backend.base_dir / "myshop@jane").exists()

    def test_with_no_slot_of_that_name_the_labelled_one_is_resolved(
        self, local_backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(plugin_commands._plugin_session_map, "myshop", "myshop")
        local_backend.save_session("myshop@jane", b"data", _metadata("myshop@jane"))

        result = _clear("myshop", "--force")

        assert result.exit_code == 0, result.output
        assert not (local_backend.base_dir / "myshop@jane").exists()

    def test_a_metadata_only_slot_is_labelled_incomplete_not_missing_metadata(
        self, local_backend
    ) -> None:
        slot = local_backend.base_dir / "myshop"
        slot.mkdir(parents=True)
        (slot / "metadata.json").write_text("{}")

        result = _clear_all_prompt()

        assert "myshop (incomplete, not a full session)" in result.output
        assert "no metadata" not in result.output


def _clear_all_prompt():
    app = typer.Typer()
    app.add_typer(session_app, name="session")
    return CliRunner().invoke(
        app, ["session", "clear", "--all"], input="n\n", env={"COLUMNS": "200"}
    )


class TestStorageErrorsPrintOneLine:
    @pytest.mark.parametrize("args", [["--all", "--force"], ["myshop", "--force"]])
    def test_an_incomplete_listing_error_exits_1_with_one_line(self, args: list[str]) -> None:
        with (
            patch("graftpunk.cli.session_commands.list_sessions_with_metadata", return_value=[]),
            patch(
                "graftpunk.cli.session_commands.list_incomplete_sessions",
                side_effect=StorageError("Failed to list sessions: denied"),
            ),
        ):
            result = _clear(*args)

        assert result.exit_code == 1
        assert "Storage backend error: Failed to list sessions: denied" in result.output
        assert "Traceback" not in result.output

    def test_a_delete_error_names_the_slot_and_exits_1(self) -> None:
        meta = [{"name": "myshop", "domain": "myshop.example.com"}]
        with (
            patch("graftpunk.cli.session_commands.list_sessions_with_metadata", return_value=meta),
            patch(
                "graftpunk.cli.session_commands.remove_session_objects",
                side_effect=StorageError("Failed to delete 'x': denied"),
            ),
        ):
            result = _clear("myshop", "--force")

        assert result.exit_code == 1
        assert "Could not clear myshop" in result.output


class TestNoDeleteUnderABadName:
    @pytest.mark.parametrize("name", _BAD_NAMES)
    def test_local(self, tmp_path: Path, name: str) -> None:
        storage = LocalSessionStorage(base_dir=tmp_path / "sessions")
        storage.save_session("myshop", b"data", _metadata("myshop"))

        with pytest.raises(ValueError):
            storage.remove_session(name)
        with pytest.raises(ValueError):
            storage.delete_session(name)

        assert (tmp_path / "sessions" / "myshop" / "session.pickle").exists()

    @pytest.mark.parametrize("name", _BAD_NAMES)
    def test_s3(self, name: str) -> None:
        pytest.importorskip("boto3")
        from graftpunk.storage.s3 import S3SessionStorage

        client = MagicMock()
        storage = S3SessionStorage(bucket="b", client=client)

        with pytest.raises(ValueError):
            storage.remove_session(name)

        assert client.mock_calls == []

    @pytest.mark.parametrize("name", _BAD_NAMES)
    def test_supabase(self, name: str) -> None:
        pytest.importorskip("supabase")
        with patch("supabase.create_client") as create:
            from graftpunk.storage.supabase import SupabaseSessionStorage

            storage = SupabaseSessionStorage(url="https://x.supabase.co", service_key="k")
        client = create.return_value
        client.reset_mock()

        with pytest.raises(ValueError):
            storage.remove_session(name)

        assert client.mock_calls == []

    @pytest.mark.parametrize("name", _BAD_NAMES)
    def test_cache_refuses_before_making_a_backend(
        self, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_backend(**_kwargs):
            raise AssertionError("a backend was made")

        monkeypatch.setattr(cache_mod, "_get_session_storage_backend", no_backend)

        with pytest.raises(ValueError):
            cache_mod.remove_session_objects(name)


class TestALegacyNameStaysClearable:
    """A slot an older release saved under a name today's rules refuse (#382)."""

    @staticmethod
    def _legacy(backend, name: str = "Legacy") -> Path:
        slot = backend.base_dir / name
        slot.mkdir(parents=True)
        (slot / "session.pickle").write_bytes(b"x")
        (slot / "metadata.json").write_text(json.dumps(metadata_to_dict(_metadata(name))))
        return slot

    def test_it_clears_by_name(self, local_backend) -> None:
        slot = self._legacy(local_backend)

        result = _clear("Legacy", "--force")

        assert result.exit_code == 0, result.output
        assert "Legacy/session.pickle" in result.output
        assert not slot.exists()

    def test_clear_all_includes_it(self, local_backend) -> None:
        slot = self._legacy(local_backend)
        local_backend.save_session("myshop", b"data", _metadata("myshop"))

        result = _clear("--all", "--force")

        assert result.exit_code == 0, result.output
        assert not slot.exists()
        assert not (local_backend.base_dir / "myshop").exists()

    def test_the_sweep_and_the_backends_include_it(self, local_backend) -> None:
        slot = self._legacy(local_backend)
        odd = local_backend.base_dir / "My Notes"
        odd.mkdir()
        (odd / "keep.txt").write_text("x")

        assert cache_mod.list_incomplete_sessions() == ["My Notes"]
        assert sorted(cache_mod.clear_session_cache()) == ["Legacy", "My Notes"]
        assert not slot.exists()

    def test_a_legacy_flat_file_clears_by_name(self, local_backend) -> None:
        flat = local_backend.base_dir / "OldShop.session.pickle"
        flat.write_bytes(b"x")

        result = _clear("OldShop", "--force")

        assert result.exit_code == 0, result.output
        assert not flat.exists()

    def test_a_slot_symlinked_outside_the_sessions_directory_is_refused(
        self, local_backend, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("x")
        (local_backend.base_dir / "myshop").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match="outside"):
            local_backend.remove_session("myshop")
        assert (outside / "keep.txt").exists()
