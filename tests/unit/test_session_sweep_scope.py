"""What a sweep may delete, on every backend (#382, review round 3).

A container is an incomplete slot only when it holds a file graftpunk writes or
a leftover named after one; any other folder, prefix or directory is never
listed and never deleted. A sweep never stops partway.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from graftpunk import cache as cache_mod
from graftpunk.cli.session_commands import session_app
from graftpunk.storage.base import SessionMetadata, is_slot_file, metadata_to_dict


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
    monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path / "cfg"))
    cache_mod._reset_session_storage_backend()
    try:
        yield cache_mod._get_session_storage_backend()
    finally:
        cache_mod._reset_session_storage_backend()


def _clear(*args: str):
    app = typer.Typer()
    app.add_typer(session_app, name="session")
    return CliRunner().invoke(app, ["session", "clear", *args], env={"COLUMNS": "200"})


@pytest.mark.parametrize(
    ("file_name", "ours"),
    [
        ("session.pickle", True),
        ("metadata.json", True),
        ("metadata.json.pre-test2", True),
        ("session.pickle.bak", True),
        ("notes.txt", False),
        ("old-metadata.json", False),
        ("jane.png", False),
    ],
)
def test_is_slot_file(file_name: str, ours: bool) -> None:
    assert is_slot_file(file_name) is ours


class TestLocal:
    def test_a_foreign_directory_is_never_listed_or_swept(self, local_backend) -> None:
        """Review round 2 (toolkit N2): a user's ``old-backups/`` survives ``--all``."""
        foreign = local_backend.base_dir / "old-backups"
        (foreign / "2026-10").mkdir(parents=True)
        (foreign / "2026-10" / "notes.txt").write_text("x")
        (foreign / "export.csv").write_text("x")
        local_backend.save_session("myshop", b"data", _metadata("myshop"))

        assert cache_mod.list_incomplete_sessions() == []
        result = _clear("--all", "--force")

        assert result.exit_code == 0, result.output
        assert "old-backups" not in result.output
        assert (foreign / "export.csv").exists()
        assert not (local_backend.base_dir / "myshop").exists()

    def test_a_backup_of_metadata_nested_deeper_does_not_make_a_slot(self, local_backend) -> None:
        foreign = local_backend.base_dir / "backup"
        (foreign / "myshop").mkdir(parents=True)
        (foreign / "myshop" / "metadata.json").write_text("{}")

        assert cache_mod.list_incomplete_sessions() == []
        assert cache_mod.clear_session_cache() == []
        assert (foreign / "myshop" / "metadata.json").exists()

    def test_leftovers_and_a_pickle_only_slot_are_swept_as_before(self, local_backend) -> None:
        left = local_backend.base_dir / "myshop"
        left.mkdir(parents=True)
        (left / "metadata.json.pre-test2").write_text("{}")
        half = local_backend.base_dir / "half"
        half.mkdir()
        (half / "session.pickle").write_bytes(b"x")

        assert cache_mod.list_incomplete_sessions() == ["half", "myshop"]
        assert sorted(cache_mod.clear_session_cache()) == ["half", "myshop"]

    def test_the_sweep_skips_a_slot_linked_outside_and_finishes(
        self, local_backend, tmp_path: Path
    ) -> None:
        """Review round 2 (superpowers R1): the sweep went on raising partway."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "session.pickle").write_bytes(b"x")
        (outside / "metadata.json").write_text("{}")
        (local_backend.base_dir / "aalink").symlink_to(outside, target_is_directory=True)
        local_backend.save_session("myshop", b"data", _metadata("myshop"))
        local_backend.save_session("zshop", b"data", _metadata("zshop"))

        result = cache_mod.clear_session_cache()

        assert sorted(result) == ["myshop", "zshop"]
        assert result.skipped == ("aalink",)
        assert (outside / "session.pickle").exists()

    def test_the_sweep_logs_a_skipped_slot_once(
        self, local_backend, tmp_path: Path, captured_logs
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "session.pickle").write_bytes(b"x")
        (local_backend.base_dir / "aalink").symlink_to(outside, target_is_directory=True)

        cache_mod.clear_session_cache()

        warnings = [e for e in captured_logs if e.get("event") == "session_not_cleared"]
        assert [w["name"] for w in warnings] == ["aalink"]


class TestS3:
    @pytest.fixture
    def s3(self):
        pytest.importorskip("boto3")
        from tests.unit.test_storage_s3 import FakeS3

        return FakeS3

    def _storage(self, client):
        from graftpunk.storage.s3 import S3SessionStorage

        return S3SessionStorage(bucket="b", client=client)

    def test_a_foreign_prefix_is_never_listed_or_swept(self, s3, monkeypatch) -> None:
        """Review round 2 (toolkit N3): another tool's objects under ``sessions/``."""
        client = s3(
            [
                "sessions/reports/2026-10.csv",
                "sessions/reports/archive/metadata.json",
                "sessions/myshop/metadata.json.pre-test2",
                "sessions/myshop@jane/metadata.json",
                "sessions/myshop@jane/session.pickle",
            ]
        )
        storage = self._storage(client)
        monkeypatch.setattr(cache_mod, "_get_session_storage_backend", lambda **_: storage)

        assert storage.incomplete_sessions() == ["myshop"]
        assert sorted(cache_mod.clear_session_cache()) == ["myshop", "myshop@jane"]
        assert sorted(client.objects) == [
            "sessions/reports/2026-10.csv",
            "sessions/reports/archive/metadata.json",
        ]


class TestSupabase:
    def _storage(self, bucket):
        pytest.importorskip("supabase")
        with patch("supabase.create_client") as create:
            from graftpunk.storage.supabase import SupabaseSessionStorage

            storage = SupabaseSessionStorage(url="https://x.supabase.co", service_key="k")
        create.return_value.storage.from_.return_value = bucket
        return storage

    def test_a_foreign_root_folder_is_never_listed_or_swept(self, monkeypatch) -> None:
        """Review round 2 (toolkit N1): ``avatars/`` at the bucket root."""
        pytest.importorskip("supabase")
        from tests.unit.test_storage_supabase import FakeBucket

        bucket = FakeBucket(
            [
                "avatars/jane.png",
                "avatars/john.png",
                "myshop/metadata.json",
                "myshop/session.pickle",
                "half/session.pickle",
            ]
        )
        storage = self._storage(bucket)
        monkeypatch.setattr(cache_mod, "_get_session_storage_backend", lambda **_: storage)

        assert storage.incomplete_sessions() == ["half"]
        assert sorted(cache_mod.clear_session_cache()) == ["half", "myshop"]
        assert bucket.files == {"avatars/jane.png", "avatars/john.png"}

    def test_a_differently_cased_sibling_does_not_complete_a_legacy_slot(self) -> None:
        """Review round 2 (toolkit N4): the folder listing ignores letter case."""
        pytest.importorskip("supabase")
        from tests.unit.test_storage_supabase import FakeBucket

        class CaseFoldingBucket(FakeBucket):
            def list(self, path=None, options=None):
                if not path:
                    return super().list(path, options)
                self.list_calls += 1
                prefix = f"{path.lower()}/"
                entries = {}
                for file in self.files:
                    if file.lower().startswith(prefix):
                        head = file[len(prefix) :].split("/")[0]
                        entries[head] = {"name": head, "id": f"id-{file}"}
                return [entries[k] for k in sorted(entries)]

        bucket = CaseFoldingBucket(
            ["MyShop/metadata.json.pre-test2", "myshop/metadata.json", "myshop/session.pickle"]
        )
        storage = self._storage(bucket)

        assert storage.list_sessions() == ["myshop"]
        assert storage.incomplete_sessions() == ["MyShop"]

    def test_a_delete_that_keeps_files_is_reported_and_exits_1(self, monkeypatch) -> None:
        """Review round 2 (toolkit N5): a refused delete answers 200 with no rows."""
        pytest.importorskip("supabase")
        from tests.unit.test_storage_supabase import FakeBucket

        class RefusingBucket(FakeBucket):
            def download(self, path):
                return json.dumps(metadata_to_dict(_metadata("myshop"))).encode()

            def remove(self, paths):
                kept = {p for p in paths if p.endswith("session.pickle")}
                self.remove_calls.append(list(paths))
                self.files -= set(paths) - kept
                return [{"name": p} for p in paths if p not in kept]

        bucket = RefusingBucket(["myshop/metadata.json", "myshop/session.pickle"])
        storage = self._storage(bucket)
        monkeypatch.setattr(cache_mod, "_get_session_storage_backend", lambda **_: storage)

        result = _clear("myshop", "--force")

        assert result.exit_code == 1
        assert "myshop kept 1 file(s): myshop/session.pickle" in result.output
        assert "myshop/metadata.json" in result.output
        assert bucket.files == {"myshop/session.pickle"}

    def test_the_sweep_skips_a_slot_that_kept_files(self, monkeypatch) -> None:
        pytest.importorskip("supabase")
        from tests.unit.test_storage_supabase import FakeBucket

        class RefusingBucket(FakeBucket):
            def remove(self, paths):
                kept = {p for p in paths if p.startswith("aashop/")}
                self.files -= set(paths) - kept
                return [{"name": p} for p in paths if p not in kept]

        bucket = RefusingBucket(
            ["aashop/metadata.json", "aashop/session.pickle", "myshop/metadata.json"]
        )
        storage = self._storage(bucket)
        monkeypatch.setattr(cache_mod, "_get_session_storage_backend", lambda **_: storage)

        result = cache_mod.clear_session_cache()

        assert list(result) == ["myshop"]
        assert result.skipped == ("aashop",)


@pytest.fixture
def locked_slot(local_backend):
    """A slot whose ``locked/`` subdirectory has no write permission, so the file
    inside it cannot be deleted (a read-only file alone can be: unlinking needs
    write permission on its directory). Permissions are restored in teardown."""
    import os
    import stat
    import sys

    if sys.platform == "win32" or os.geteuid() == 0:
        pytest.skip("POSIX directory permissions, not as root")
    local_backend.save_session("myshop", b"data", _metadata("myshop"))
    locked = local_backend.base_dir / "myshop" / "locked"
    locked.mkdir()
    keep = locked / "metadata.json.pre-test2"
    keep.write_text("{}")
    keep.chmod(stat.S_IRUSR)
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        yield local_backend
    finally:
        locked.chmod(stat.S_IRWXU)
        keep.chmod(stat.S_IRUSR | stat.S_IWUSR)


class TestALocalDeleteThatKeepsFiles:
    """Same rule as Supabase: a delete that leaves files behind says so."""

    def test_remove_session_names_what_remains(self, locked_slot) -> None:
        from graftpunk.exceptions import SessionNotFullyClearedError

        with pytest.raises(SessionNotFullyClearedError) as err:
            locked_slot.remove_session("myshop")

        assert err.value.remaining == ["myshop/locked/metadata.json.pre-test2"]
        assert "myshop/metadata.json" in err.value.removed
        assert (locked_slot.base_dir / "myshop" / "locked" / "metadata.json.pre-test2").exists()

    def test_gp_session_clear_prints_it_and_exits_1(self, locked_slot) -> None:
        result = _clear("myshop", "--force")

        assert result.exit_code == 1
        assert "myshop kept 1 file(s): myshop/locked/metadata.json.pre-test2" in result.output

    def test_the_sweep_reports_it_in_skipped(self, locked_slot) -> None:
        locked_slot.save_session("zshop", b"data", _metadata("zshop"))

        result = cache_mod.clear_session_cache()

        assert list(result) == ["zshop"]
        assert result.skipped == ("myshop",)

    def test_a_flat_file_that_cannot_be_deleted_is_reported(
        self, local_backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.exceptions import SessionNotFullyClearedError

        flat = local_backend.base_dir / "oldshop.session.pickle"
        flat.write_bytes(b"x")

        def refuse(self, *args, **kwargs):
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "unlink", refuse)
        with pytest.raises(SessionNotFullyClearedError) as err:
            local_backend.remove_session("oldshop")

        assert err.value.remaining == ["oldshop.session.pickle"]
