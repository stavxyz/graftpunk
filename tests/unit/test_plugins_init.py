"""Tests for graftpunk.plugins discovery and loading."""

from unittest.mock import MagicMock, patch

import pytest

from graftpunk.plugins import (
    KEEPALIVE_HANDLERS_GROUP,
    PLUGINS_GROUP,
    STORAGE_GROUP,
    discover_keepalive_handlers,
    discover_plugins,
    discover_site_plugins,
    discover_storage_backends,
    get_keepalive_handler,
    get_storage_backend,
    list_available_plugins,
    load_handler_from_string,
)

EP_PATCH = "graftpunk.plugins.entry_points"


def _make_entry_point(
    name: str,
    load_return: object = None,
    load_exc: Exception | None = None,
) -> MagicMock:
    ep = MagicMock()
    ep.name = name
    if load_exc is not None:
        ep.load.side_effect = load_exc
    else:
        ep.load.return_value = load_return
    return ep


class TestDiscoverPlugins:
    @patch(EP_PATCH)
    def test_successful_load(self, mock_eps: MagicMock) -> None:
        sentinel = object()
        ep = _make_entry_point("myplugin", load_return=sentinel)
        mock_eps.return_value = [ep]

        result = discover_plugins("test.group")

        mock_eps.assert_called_once_with(group="test.group")
        assert result == {"myplugin": sentinel}

    @patch(EP_PATCH)
    def test_load_failure_skips_plugin(self, mock_eps: MagicMock) -> None:
        ep = _make_entry_point("badplugin", load_exc=ImportError("no module"))
        mock_eps.return_value = [ep]

        with pytest.warns(UserWarning, match="badplugin.*failed to load"):
            result = discover_plugins("test.group")

        assert result == {}

    @patch(EP_PATCH)
    def test_empty_group(self, mock_eps: MagicMock) -> None:
        mock_eps.return_value = []

        result = discover_plugins("empty.group")

        assert result == {}

    @patch(EP_PATCH)
    def test_multiple_plugins_mixed(self, mock_eps: MagicMock) -> None:
        good = _make_entry_point("good", load_return="loaded")
        bad = _make_entry_point("bad", load_exc=RuntimeError("boom"))
        mock_eps.return_value = [good, bad]

        with pytest.warns(UserWarning, match="bad.*failed to load"):
            result = discover_plugins("mixed.group")

        assert result == {"good": "loaded"}


class TestConvenienceDiscoveryFunctions:
    @patch("graftpunk.plugins.discover_plugins")
    def test_discover_storage_backends(self, mock_dp: MagicMock) -> None:
        mock_dp.return_value = {"s3": "backend"}
        result = discover_storage_backends()
        mock_dp.assert_called_once_with(STORAGE_GROUP)
        assert result == {"s3": "backend"}

    @patch("graftpunk.plugins.discover_plugins")
    def test_discover_keepalive_handlers(self, mock_dp: MagicMock) -> None:
        mock_dp.return_value = {"mysite": "handler"}
        result = discover_keepalive_handlers()
        mock_dp.assert_called_once_with(KEEPALIVE_HANDLERS_GROUP)
        assert result == {"mysite": "handler"}

    @patch("graftpunk.plugins.discover_plugins")
    def test_discover_site_plugins(self, mock_dp: MagicMock) -> None:
        mock_dp.return_value = {"site1": "plugin"}
        result = discover_site_plugins()
        mock_dp.assert_called_once_with(PLUGINS_GROUP)
        assert result == {"site1": "plugin"}


class TestGetKeepaliveHandler:
    @patch(EP_PATCH)
    def test_returns_handler_when_found(self, mock_eps: MagicMock) -> None:
        handler_cls = MagicMock()
        ep = _make_entry_point("mysite", load_return=handler_cls)
        mock_eps.return_value = [ep]

        result = get_keepalive_handler("mysite")

        assert result is handler_cls

    @patch(EP_PATCH)
    def test_returns_none_when_not_found(self, mock_eps: MagicMock) -> None:
        mock_eps.return_value = []

        result = get_keepalive_handler("nonexistent")

        assert result is None


class TestGetStorageBackend:
    @patch(EP_PATCH)
    def test_returns_backend_when_found(self, mock_eps: MagicMock) -> None:
        backend_cls = MagicMock()
        ep = _make_entry_point("local", load_return=backend_cls)
        mock_eps.return_value = [ep]

        result = get_storage_backend("local")

        assert result is backend_cls

    @patch(EP_PATCH)
    def test_returns_none_when_not_found(self, mock_eps: MagicMock) -> None:
        mock_eps.return_value = []

        result = get_storage_backend("nonexistent")

        assert result is None


class TestLoadHandlerFromString:
    @patch("importlib.import_module")
    def test_valid_spec(self, mock_import: MagicMock) -> None:
        mock_cls = MagicMock()
        mock_module = MagicMock()
        mock_module.MyHandler = mock_cls
        mock_import.return_value = mock_module

        result = load_handler_from_string("mypackage.handler:MyHandler")

        mock_import.assert_called_once_with("mypackage.handler")
        mock_cls.assert_called_once()
        assert result is mock_cls.return_value

    def test_missing_colon_raises_value_error(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="Invalid handler specification"):
            load_handler_from_string("mypackage.handler.MyHandler")

    @patch("importlib.import_module")
    def test_bad_module_raises_import_error(self, mock_import: MagicMock) -> None:
        import pytest

        mock_import.side_effect = ImportError("No module named 'bad'")

        with pytest.raises(ImportError, match="Cannot import module"):
            load_handler_from_string("bad.module:Handler")

    @patch("importlib.import_module")
    def test_bad_class_raises_attribute_error(self, mock_import: MagicMock) -> None:
        import pytest

        mock_module = MagicMock(spec=[])
        mock_import.return_value = mock_module

        with pytest.raises(AttributeError, match="not found in module"):
            load_handler_from_string("mypackage:NonExistent")


class TestListAvailablePlugins:
    @patch("graftpunk.plugins.discover_site_plugins")
    @patch("graftpunk.plugins.discover_keepalive_handlers")
    @patch("graftpunk.plugins.discover_storage_backends")
    def test_returns_all_groups(
        self,
        mock_storage: MagicMock,
        mock_keepalive: MagicMock,
        mock_site: MagicMock,
    ) -> None:
        mock_storage.return_value = {"s3": "x", "local": "y"}
        mock_keepalive.return_value = {"mysite": "h"}
        mock_site.return_value = {"tool": "t"}

        result = list_available_plugins()

        assert result == {
            "storage": ["s3", "local"],
            "keepalive_handlers": ["mysite"],
            "plugins": ["tool"],
        }
