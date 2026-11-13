"""Tests for S3 storage backend."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from graftpunk.exceptions import SessionNotFoundError, StorageError

pytest.importorskip("boto3")

from graftpunk.storage.base import SessionMetadata


@pytest.fixture
def sample_metadata():
    """Create sample session metadata."""
    now = datetime.now(UTC)
    return SessionMetadata(
        name="test-session",
        checksum="abc123",
        created_at=now,
        modified_at=now,
        expires_at=now + timedelta(hours=24),
        domain="example.com",
        current_url="https://example.com/dashboard",
        cookie_count=5,
        cookie_domains=["example.com", ".example.com"],
        status="active",
    )


@pytest.fixture
def mock_s3_client():
    """Create a mock boto3 S3 client."""
    return MagicMock()


@pytest.fixture
def storage(mock_s3_client):
    """Create S3SessionStorage with mocked client."""
    from graftpunk.storage.s3 import S3SessionStorage

    return S3SessionStorage(
        bucket="test-bucket",
        region="us-east-1",
        endpoint_url="https://test.r2.example.com",
        client=mock_s3_client,
    )


class TestS3SessionStorageInit:
    """Tests for S3SessionStorage initialization."""

    def test_storage_initialization_with_injected_client(self, mock_s3_client):
        """Test that storage initializes correctly with injected client."""
        from graftpunk.storage.s3 import S3SessionStorage

        storage = S3SessionStorage(
            bucket="test-bucket",
            endpoint_url="https://test.r2.example.com",
            client=mock_s3_client,
        )
        assert storage.bucket == "test-bucket"
        assert storage.endpoint_url == "https://test.r2.example.com"

    @patch("boto3.client")
    def test_storage_creates_client_when_not_injected(self, mock_boto_client):
        """Test that storage creates boto3 client when not injected."""
        from graftpunk.storage.s3 import S3SessionStorage

        mock_boto_client.return_value = MagicMock()
        storage = S3SessionStorage(
            bucket="test-bucket",
            region="us-west-2",
            endpoint_url="https://r2.example.com",
        )
        mock_boto_client.assert_called_once_with(
            "s3",
            region_name="us-west-2",
            endpoint_url="https://r2.example.com",
        )
        assert storage.bucket == "test-bucket"

    @patch("boto3.client")
    def test_storage_skips_region_when_auto(self, mock_boto_client):
        """Test that region='auto' is treated as no region (for R2)."""
        from graftpunk.storage.s3 import S3SessionStorage

        mock_boto_client.return_value = MagicMock()
        S3SessionStorage(
            bucket="test-bucket",
            region="auto",
            endpoint_url="https://r2.example.com",
        )
        mock_boto_client.assert_called_once_with(
            "s3",
            endpoint_url="https://r2.example.com",
        )

    def test_boto3_import_error_raises_storage_error(self):
        """Test that missing boto3 raises StorageError with helpful message."""
        import sys

        # Temporarily hide boto3 to simulate ImportError
        boto3_backup = sys.modules.get("boto3")
        sys.modules["boto3"] = None  # type: ignore[assignment]

        try:
            # Reload the s3 module to trigger import error
            from importlib import reload

            import graftpunk.storage.s3 as s3_module

            # Force reload to clear cached import
            reload(s3_module)

            with pytest.raises(StorageError, match="boto3 is required"):
                s3_module.S3SessionStorage(bucket="test")
        finally:
            # Restore boto3
            if boto3_backup is not None:
                sys.modules["boto3"] = boto3_backup
            else:
                sys.modules.pop("boto3", None)


class TestSaveSession:
    """Tests for save_session method."""

    def test_save_session_uploads_pickle_and_metadata(
        self, storage, mock_s3_client, sample_metadata
    ):
        """Test that save uploads both pickle and metadata files."""
        encrypted_data = b"encrypted-pickle-data"

        location = storage.save_session("test-session", encrypted_data, sample_metadata)

        assert location == "s3://test-bucket/sessions/test-session/session.pickle"
        assert mock_s3_client.put_object.call_count == 2

        # Verify pickle upload
        pickle_call = mock_s3_client.put_object.call_args_list[0]
        assert pickle_call.kwargs["Bucket"] == "test-bucket"
        assert pickle_call.kwargs["Key"] == "sessions/test-session/session.pickle"
        assert pickle_call.kwargs["Body"] == encrypted_data
        assert pickle_call.kwargs["ContentType"] == "application/octet-stream"

        # Verify metadata upload
        metadata_call = mock_s3_client.put_object.call_args_list[1]
        assert metadata_call.kwargs["Key"] == "sessions/test-session/metadata.json"
        assert metadata_call.kwargs["ContentType"] == "application/json"


class TestLoadSession:
    """Tests for load_session method."""

    def test_load_session_success(self, storage, mock_s3_client, sample_metadata):
        """Test successful session load."""
        metadata_dict = {
            "name": "test-session",
            "checksum": "abc123",
            "created_at": sample_metadata.created_at.isoformat(),
            "modified_at": sample_metadata.modified_at.isoformat(),
            "expires_at": sample_metadata.expires_at.isoformat(),
            "domain": "example.com",
            "current_url": "https://example.com/dashboard",
            "cookie_count": 5,
            "cookie_domains": ["example.com", ".example.com"],
            "status": "active",
        }
        mock_metadata_body = MagicMock()
        mock_metadata_body.read.return_value = json.dumps(metadata_dict).encode()

        mock_session_body = MagicMock()
        mock_session_body.read.return_value = b"encrypted-data"

        def get_object_side_effect(**kwargs):
            if kwargs["Key"].endswith("metadata.json"):
                return {"Body": mock_metadata_body}
            return {"Body": mock_session_body}

        mock_s3_client.get_object.side_effect = get_object_side_effect

        data, metadata = storage.load_session("test-session")

        assert data == b"encrypted-data"
        assert metadata.name == "test-session"
        assert metadata.domain == "example.com"

    def test_load_session_not_found(self, storage, mock_s3_client):
        """Test load raises SessionNotFoundError when metadata not found."""
        from botocore.exceptions import ClientError

        error_response = {
            "Error": {"Code": "NoSuchKey"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        mock_s3_client.get_object.side_effect = ClientError(error_response, "GetObject")

        with pytest.raises(SessionNotFoundError, match="not found"):
            storage.load_session("nonexistent")

    def test_load_session_expired_ttl(self, storage, mock_s3_client):
        """Test load raises SessionExpiredError when TTL has passed."""
        from graftpunk.exceptions import SessionExpiredError

        now = datetime.now(UTC)
        past = now - timedelta(hours=24)

        metadata_dict = {
            "name": "expired-session",
            "checksum": "abc",
            "created_at": (now - timedelta(hours=48)).isoformat(),
            "modified_at": (now - timedelta(hours=48)).isoformat(),
            "expires_at": past.isoformat(),
            "domain": "example.com",
            "current_url": None,
            "cookie_count": 0,
            "cookie_domains": [],
            "status": "active",
        }
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps(metadata_dict).encode()
        mock_s3_client.get_object.return_value = {"Body": mock_body}

        with pytest.raises(SessionExpiredError, match="expired"):
            storage.load_session("expired-session")


class TestListSessions:
    """Tests for list_sessions method."""

    def test_list_sessions_empty(self, storage, mock_s3_client):
        """Test listing sessions when none exist."""
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": []}]
        mock_s3_client.get_paginator.return_value = mock_paginator

        sessions = storage.list_sessions()
        assert sessions == []

    def test_list_sessions_with_results(self, storage, mock_s3_client):
        """Test listing sessions with results."""
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "sessions/session-b/session.pickle"},
                    {"Key": "sessions/session-b/metadata.json"},
                    {"Key": "sessions/session-a/session.pickle"},
                    {"Key": "sessions/session-a/metadata.json"},
                ]
            }
        ]
        mock_s3_client.get_paginator.return_value = mock_paginator

        sessions = storage.list_sessions()
        assert sessions == ["session-a", "session-b"]


class FakeS3:
    """An in-memory bucket answering the S3 calls the backend makes.

    ``list_objects_v2`` pages hold *page_size* keys, so a test can make a
    listing span several pages the way a large bucket does.
    """

    def __init__(self, keys, page_size=1000):
        self.objects = dict.fromkeys(keys, b"x")
        self.page_size = page_size
        self.pages_served = 0

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix=""):  # noqa: N803 (boto3's argument names)
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        for start in range(0, len(keys), self.page_size):
            self.pages_served += 1
            yield {"Contents": [{"Key": k} for k in keys[start : start + self.page_size]]}
        if not keys:
            yield {}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.objects.pop(Key, None)
        return {}


# A slot cleared by a release that deleted only the two files it wrote, beside
# a labelled slot for the same site: the shape #382 was found in.
PHANTOM_BUCKET = [
    "sessions/myshop/metadata.json.pre-test2",
    "sessions/myshop/session.pickle.pre-test2",
    "sessions/myshop@jane/metadata.json",
    "sessions/myshop@jane/session.pickle",
]


def _s3_storage(client):
    from graftpunk.storage.s3 import S3SessionStorage

    return S3SessionStorage(bucket="test-bucket", client=client)


class TestDeleteSession:
    """delete_session removes every object under the slot's prefix (#382)."""

    def test_delete_session_removes_the_pair(self):
        s3 = FakeS3(["sessions/myshop/session.pickle", "sessions/myshop/metadata.json"])

        assert _s3_storage(s3).delete_session("myshop") is True
        assert s3.objects == {}

    def test_delete_session_removes_leftovers_and_reports_them(self):
        s3 = FakeS3(PHANTOM_BUCKET)

        removed = _s3_storage(s3).remove_session("myshop")

        assert removed == [
            "sessions/myshop/metadata.json.pre-test2",
            "sessions/myshop/session.pickle.pre-test2",
        ]
        assert sorted(s3.objects) == [
            "sessions/myshop@jane/metadata.json",
            "sessions/myshop@jane/session.pickle",
        ]

    def test_delete_session_does_not_touch_a_slot_whose_name_extends_it(self):
        s3 = FakeS3(["sessions/myshop@jane/metadata.json", "sessions/myshopper/metadata.json"])

        assert _s3_storage(s3).delete_session("myshop") is False
        assert len(s3.objects) == 2

    def test_delete_session_pages_through_a_large_slot(self):
        keys = [f"sessions/myshop/backup-{i:03}" for i in range(7)]
        s3 = FakeS3([*keys, "sessions/myshop/metadata.json"], page_size=3)

        removed = _s3_storage(s3).remove_session("myshop")

        assert len(removed) == 8
        assert s3.pages_served == 3
        assert s3.objects == {}

    def test_delete_session_of_nothing_is_false(self):
        assert _s3_storage(FakeS3([])).delete_session("myshop") is False

    def test_delete_session_raises_on_a_server_error(self, mock_s3_client):
        from botocore.exceptions import ClientError

        mock_s3_client.get_paginator.return_value.paginate.return_value = [
            {"Contents": [{"Key": "sessions/myshop/metadata.json"}]}
        ]
        mock_s3_client.delete_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "DeleteObject"
        )

        with pytest.raises(StorageError, match="Failed to delete"):
            _s3_storage(mock_s3_client).delete_session("myshop")

    def test_delete_session_skips_an_object_already_gone(self, mock_s3_client):
        from botocore.exceptions import ClientError

        mock_s3_client.get_paginator.return_value.paginate.return_value = [
            {"Contents": [{"Key": "sessions/myshop/metadata.json"}]}
        ]
        mock_s3_client.delete_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey"}}, "DeleteObject"
        )

        assert _s3_storage(mock_s3_client).remove_session("myshop") == []

    def test_delete_session_raises_when_the_listing_fails(self, mock_s3_client):
        from botocore.exceptions import ClientError

        mock_s3_client.get_paginator.return_value.paginate.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "ListObjectsV2"
        )

        with pytest.raises(StorageError, match="Failed to list objects"):
            _s3_storage(mock_s3_client).delete_session("myshop")


class TestPhantomSlots:
    """Objects under sessions/<name>/ without metadata.json are not a session (#382)."""

    def test_leftovers_do_not_make_a_session(self):
        assert _s3_storage(FakeS3(PHANTOM_BUCKET)).list_sessions() == ["myshop@jane"]

    def test_leftovers_are_an_incomplete_slot(self):
        assert _s3_storage(FakeS3(PHANTOM_BUCKET)).incomplete_sessions() == ["myshop"]

    def test_leftovers_are_never_a_resolver_candidate(self):
        from graftpunk.session_identity import resolve_account_session

        sessions = _s3_storage(FakeS3(PHANTOM_BUCKET)).list_sessions()

        assert resolve_account_session("myshop", sessions) == "myshop@jane"

    def test_a_pickle_without_metadata_is_not_a_session(self):
        s3 = FakeS3(["sessions/myshop/session.pickle"])

        assert _s3_storage(s3).list_sessions() == []
        assert _s3_storage(s3).incomplete_sessions() == ["myshop"]

    def test_listing_spans_pages(self):
        keys = [f"sessions/site-{i}/{f}" for i in range(5) for f in ("metadata.json", "x.bak")]
        s3 = FakeS3(keys, page_size=3)

        assert _s3_storage(s3).list_sessions() == [f"site-{i}" for i in range(5)]
        assert s3.pages_served == 4

    def test_keys_outside_a_slot_are_ignored(self):
        s3 = FakeS3(["sessions/metadata.json", "other/x/metadata.json", "sessions//metadata.json"])

        assert _s3_storage(s3).list_sessions() == []
        assert _s3_storage(s3).incomplete_sessions() == []

    def test_clear_leaves_nothing_under_the_prefix(self, monkeypatch):
        from graftpunk import cache as cache_mod

        s3 = FakeS3(PHANTOM_BUCKET)
        monkeypatch.setattr(cache_mod, "_get_session_storage_backend", lambda **_: _s3_storage(s3))

        assert cache_mod.clear_session_cache() == ["myshop@jane", "myshop"]
        assert s3.objects == {}


class TestGetSessionMetadata:
    """Tests for get_session_metadata method."""

    def test_get_session_metadata_not_found(self, storage, mock_s3_client):
        """Test getting metadata for non-existent session."""
        from botocore.exceptions import ClientError

        error_response = {
            "Error": {"Code": "NoSuchKey"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        mock_s3_client.get_object.side_effect = ClientError(error_response, "GetObject")

        metadata = storage.get_session_metadata("non-existent")
        assert metadata is None

    def test_get_session_metadata_success(self, storage, mock_s3_client, sample_metadata):
        """Test getting metadata for existing session."""
        metadata_dict = {
            "name": "my-session",
            "checksum": "sha256abc",
            "created_at": sample_metadata.created_at.isoformat(),
            "modified_at": sample_metadata.modified_at.isoformat(),
            "expires_at": None,
            "domain": "example.com",
            "current_url": "https://example.com",
            "cookie_count": 3,
            "cookie_domains": ["example.com"],
            "status": "active",
        }
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps(metadata_dict).encode()
        mock_s3_client.get_object.return_value = {"Body": mock_body}

        metadata = storage.get_session_metadata("my-session")
        assert metadata is not None
        assert metadata.name == "my-session"
        assert metadata.domain == "example.com"


class TestUpdateSessionMetadata:
    """Tests for update_session_metadata method."""

    def test_update_session_metadata_invalid_status(self, storage):
        """Test that invalid status raises ValueError."""
        with pytest.raises(ValueError, match="Invalid status"):
            storage.update_session_metadata("test", status="invalid-status")

    def test_update_session_metadata_success(self, storage, mock_s3_client, sample_metadata):
        """Test successful metadata update."""
        metadata_dict = {
            "name": "test",
            "checksum": "abc",
            "created_at": sample_metadata.created_at.isoformat(),
            "modified_at": sample_metadata.modified_at.isoformat(),
            "expires_at": None,
            "domain": None,
            "current_url": None,
            "cookie_count": 0,
            "cookie_domains": [],
            "status": "active",
        }
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps(metadata_dict).encode()
        mock_s3_client.get_object.return_value = {"Body": mock_body}

        result = storage.update_session_metadata("test", status="logged_out")
        assert result is True
        mock_s3_client.put_object.assert_called_once()

    def test_update_session_metadata_sets_the_account(
        self, storage, mock_s3_client, sample_metadata
    ):
        """The account keywords reach the uploaded metadata; status is left alone (#293)."""
        metadata_dict = {
            "name": "test",
            "checksum": "abc",
            "created_at": sample_metadata.created_at.isoformat(),
            "modified_at": sample_metadata.modified_at.isoformat(),
            "expires_at": None,
            "domain": None,
            "current_url": None,
            "cookie_count": 0,
            "cookie_domains": [],
            "status": "active",
            "account_identifier": "829341",
        }
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps(metadata_dict).encode()
        mock_s3_client.get_object.return_value = {"Body": mock_body}

        assert storage.update_session_metadata("test", account_identifier=None, account_rule=2)

        uploaded = json.loads(mock_s3_client.put_object.call_args.kwargs["Body"])
        assert uploaded["account_identifier"] is None
        assert uploaded["account_rule"] == 2
        assert uploaded["status"] == "active"
        assert uploaded["checksum"] == "abc"

    def test_update_session_metadata_not_found(self, storage, mock_s3_client):
        """Test update returns False when session not found."""
        from botocore.exceptions import ClientError

        error_response = {
            "Error": {"Code": "NoSuchKey"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        mock_s3_client.get_object.side_effect = ClientError(error_response, "GetObject")

        result = storage.update_session_metadata("nonexistent", status="active")
        assert result is False


class TestS3StorageIdentity:
    """Tests for S3SessionStorage identity properties."""

    def test_s3_backend_without_r2_endpoint(self, mock_s3_client):
        """Test plain S3 backend without endpoint_url returns 's3'."""
        from graftpunk.storage.s3 import S3SessionStorage

        storage = S3SessionStorage(
            bucket="my-bucket",
            region="us-east-1",
            client=mock_s3_client,
        )
        assert storage.storage_backend == "s3"
        assert storage.storage_location == "s3://my-bucket"

    def test_r2_backend_detected_from_endpoint(self, mock_s3_client):
        """Test R2 endpoint auto-detects as 'r2' backend."""
        from graftpunk.storage.s3 import S3SessionStorage

        storage = S3SessionStorage(
            bucket="gp-sessions",
            endpoint_url="https://abc123.r2.cloudflarestorage.com",
            client=mock_s3_client,
        )
        assert storage.storage_backend == "r2"
        assert storage.storage_location == "r2://gp-sessions"

    def test_non_r2_custom_endpoint(self, mock_s3_client):
        """Test non-R2 custom endpoint (e.g., MinIO) returns 's3'."""
        from graftpunk.storage.s3 import S3SessionStorage

        storage = S3SessionStorage(
            bucket="local-bucket",
            endpoint_url="http://localhost:9000",
            client=mock_s3_client,
        )
        assert storage.storage_backend == "s3"
        assert storage.storage_location == "s3://local-bucket"

    def test_save_stamps_storage_fields(self, mock_s3_client, sample_metadata):
        """Test that save_session stamps storage fields onto metadata JSON."""
        from graftpunk.storage.s3 import S3SessionStorage

        storage = S3SessionStorage(
            bucket="gp-sessions",
            endpoint_url="https://abc123.r2.cloudflarestorage.com",
            client=mock_s3_client,
        )

        storage.save_session("test-session", b"encrypted-data", sample_metadata)

        # Second put_object call is the metadata upload
        metadata_call = mock_s3_client.put_object.call_args_list[1]
        metadata_json = json.loads(metadata_call.kwargs["Body"].decode("utf-8"))

        assert metadata_json["storage_backend"] == "r2"
        assert metadata_json["storage_location"] == "r2://gp-sessions"


class TestRetryLogic:
    """Tests for retry logic in S3 storage."""

    @patch("graftpunk.storage.s3.time.sleep")
    def test_save_session_retries_on_server_error(
        self, mock_sleep, storage, mock_s3_client, sample_metadata
    ):
        """Test that 5xx errors trigger retries."""
        from botocore.exceptions import ClientError

        storage.max_retries = 3
        storage.base_delay = 0.01

        error_response = {"Error": {"Code": "500"}, "ResponseMetadata": {"HTTPStatusCode": 500}}
        mock_s3_client.put_object.side_effect = ClientError(error_response, "PutObject")

        with pytest.raises(StorageError, match="after 3 attempts"):
            storage.save_session("test", b"data", sample_metadata)

        assert mock_s3_client.put_object.call_count == 3
        assert mock_sleep.call_count == 2  # (max_retries - 1) sleeps

    def test_save_session_no_retry_on_client_error(self, storage, mock_s3_client, sample_metadata):
        """Test that 4xx errors don't trigger retries."""
        from botocore.exceptions import ClientError

        error_response = {"Error": {"Code": "400"}, "ResponseMetadata": {"HTTPStatusCode": 400}}
        mock_s3_client.put_object.side_effect = ClientError(error_response, "PutObject")

        with pytest.raises(StorageError):
            storage.save_session("test", b"data", sample_metadata)

        assert mock_s3_client.put_object.call_count == 1

    @patch("graftpunk.storage.s3.time.sleep")
    def test_retry_on_throttling(self, mock_sleep, storage, mock_s3_client, sample_metadata):
        """Test retry on SlowDown/Throttling errors."""
        from botocore.exceptions import ClientError

        storage.max_retries = 2

        error_response = {
            "Error": {"Code": "SlowDown"},
            "ResponseMetadata": {"HTTPStatusCode": 503},
        }
        mock_s3_client.put_object.side_effect = [
            ClientError(error_response, "PutObject"),
            {},  # Success
            {},  # Metadata upload
        ]

        storage.save_session("test", b"data", sample_metadata)
        assert mock_s3_client.put_object.call_count == 3  # retry + 2 uploads

    @patch("graftpunk.storage.s3.time.sleep")
    def test_retry_on_connection_error(self, mock_sleep, storage, mock_s3_client, sample_metadata):
        """Test retry on ConnectionError."""
        storage.max_retries = 3
        storage.base_delay = 0.01

        mock_s3_client.put_object.side_effect = [
            ConnectionError("Network unreachable"),
            {},  # Success on second try
            {},  # Metadata upload
        ]

        storage.save_session("test", b"data", sample_metadata)
        assert mock_s3_client.put_object.call_count == 3  # retry + 2 uploads
        assert mock_sleep.call_count == 1

    @patch("graftpunk.storage.s3.time.sleep")
    def test_retry_on_timeout_error(self, mock_sleep, storage, mock_s3_client, sample_metadata):
        """Test retry on TimeoutError."""
        storage.max_retries = 3
        storage.base_delay = 0.01

        mock_s3_client.put_object.side_effect = [
            TimeoutError("Connection timed out"),
            {},  # Success on second try
            {},  # Metadata upload
        ]

        storage.save_session("test", b"data", sample_metadata)
        assert mock_s3_client.put_object.call_count == 3  # retry + 2 uploads

    @patch("graftpunk.storage.s3.time.sleep")
    def test_connection_error_exhausts_retries(
        self, mock_sleep, storage, mock_s3_client, sample_metadata
    ):
        """Test that ConnectionError after max retries raises StorageError."""
        storage.max_retries = 2
        storage.base_delay = 0.01

        mock_s3_client.put_object.side_effect = ConnectionError("Network unreachable")

        with pytest.raises(StorageError, match="after 2 attempts"):
            storage.save_session("test", b"data", sample_metadata)

        assert mock_s3_client.put_object.call_count == 2
