"""Tests for discover_all_plugins() and get_plugin() shared discovery."""

from unittest.mock import MagicMock, patch

import pytest

from graftpunk.exceptions import PluginError
from graftpunk.plugins import (
    discover_all_plugins,
    get_plugin,
)

# Patch targets
DISCOVER_SITE = "graftpunk.plugins.discover_site_plugins"
CREATE_YAML = "graftpunk.plugins.create_yaml_plugins"
DISCOVER_PYTHON = "graftpunk.plugins.discover_python_plugins"


def _make_plugin(
    site_name: str = "testsite",
    api_version: int = 1,
    config_error: object | None = None,
) -> MagicMock:
    """Create a mock CLIPluginProtocol-compliant plugin."""
    plugin = MagicMock()
    plugin.site_name = site_name
    plugin.api_version = api_version
    if config_error is not None:
        plugin._plugin_config_error = config_error
    else:
        plugin._plugin_config_error = None
    return plugin


class TestDiscoverAllPlugins:
    """Tests for discover_all_plugins()."""

    def setup_method(self) -> None:
        """Clear the lru_cache before each test."""
        discover_all_plugins.cache_clear()

    @patch(DISCOVER_PYTHON)
    @patch(CREATE_YAML)
    @patch(DISCOVER_SITE)
    def test_combines_all_three_sources(
        self,
        mock_site: MagicMock,
        mock_yaml: MagicMock,
        mock_python: MagicMock,
    ) -> None:
        ep_plugin = _make_plugin("entrypoint_site")
        yaml_plugin = _make_plugin("yaml_site")
        python_plugin = _make_plugin("python_site")

        # Entry point plugins: dict of name -> class, we instantiate them
        ep_class = MagicMock(return_value=ep_plugin)
        mock_site.return_value = {"entrypoint_site": ep_class}

        # YAML plugins: tuple of (list[SitePlugin], list[errors])
        mock_yaml.return_value = ([yaml_plugin], [])

        # Python file plugins: PythonDiscoveryResult with .plugins and .errors
        py_result = MagicMock()
        py_result.plugins = [python_plugin]
        py_result.errors = []
        mock_python.return_value = py_result

        result = discover_all_plugins()

        assert len(result) == 3
        site_names = {p.site_name for p in result}
        assert site_names == {"entrypoint_site", "yaml_site", "python_site"}

    @patch(DISCOVER_PYTHON)
    @patch(CREATE_YAML)
    @patch(DISCOVER_SITE)
    def test_caches_results(
        self,
        mock_site: MagicMock,
        mock_yaml: MagicMock,
        mock_python: MagicMock,
    ) -> None:
        mock_site.return_value = {}
        mock_yaml.return_value = ([], [])
        py_result = MagicMock()
        py_result.plugins = []
        py_result.errors = []
        mock_python.return_value = py_result

        result1 = discover_all_plugins()
        result2 = discover_all_plugins()

        assert result1 is result2
        # Discovery functions called only once due to caching
        mock_site.assert_called_once()
        mock_yaml.assert_called_once()
        mock_python.assert_called_once()

    @patch(DISCOVER_PYTHON)
    @patch(CREATE_YAML)
    @patch(DISCOVER_SITE)
    def test_skips_plugins_with_config_error(
        self,
        mock_site: MagicMock,
        mock_yaml: MagicMock,
        mock_python: MagicMock,
    ) -> None:
        good_plugin = _make_plugin("good")
        bad_plugin = _make_plugin("bad", config_error=PluginError("bad config"))

        mock_site.return_value = {}
        mock_yaml.return_value = ([good_plugin, bad_plugin], [])
        py_result = MagicMock()
        py_result.plugins = []
        py_result.errors = []
        mock_python.return_value = py_result

        result = discover_all_plugins()

        assert len(result) == 1
        assert result[0].site_name == "good"

    @patch(DISCOVER_PYTHON)
    @patch(CREATE_YAML)
    @patch(DISCOVER_SITE)
    def test_skips_plugins_with_missing_site_name(
        self,
        mock_site: MagicMock,
        mock_yaml: MagicMock,
        mock_python: MagicMock,
    ) -> None:
        good_plugin = _make_plugin("good")
        no_name_plugin = _make_plugin("")

        mock_site.return_value = {}
        mock_yaml.return_value = ([good_plugin, no_name_plugin], [])
        py_result = MagicMock()
        py_result.plugins = []
        py_result.errors = []
        mock_python.return_value = py_result

        result = discover_all_plugins()

        assert len(result) == 1
        assert result[0].site_name == "good"

    @patch(DISCOVER_PYTHON)
    @patch(CREATE_YAML)
    @patch(DISCOVER_SITE)
    def test_skips_plugins_with_unsupported_api_version(
        self,
        mock_site: MagicMock,
        mock_yaml: MagicMock,
        mock_python: MagicMock,
    ) -> None:
        good_plugin = _make_plugin("good", api_version=1)
        future_plugin = _make_plugin("future", api_version=99)

        mock_site.return_value = {}
        mock_yaml.return_value = ([good_plugin, future_plugin], [])
        py_result = MagicMock()
        py_result.plugins = []
        py_result.errors = []
        mock_python.return_value = py_result

        result = discover_all_plugins()

        assert len(result) == 1
        assert result[0].site_name == "good"

    @patch(DISCOVER_PYTHON)
    @patch(CREATE_YAML)
    @patch(DISCOVER_SITE)
    def test_skips_entry_point_instantiation_failure(
        self,
        mock_site: MagicMock,
        mock_yaml: MagicMock,
        mock_python: MagicMock,
    ) -> None:
        good_plugin = _make_plugin("good")
        good_class = MagicMock(return_value=good_plugin)
        bad_class = MagicMock(side_effect=PluginError("init failed"))

        mock_site.return_value = {"good": good_class, "bad": bad_class}
        mock_yaml.return_value = ([], [])
        py_result = MagicMock()
        py_result.plugins = []
        py_result.errors = []
        mock_python.return_value = py_result

        result = discover_all_plugins()

        assert len(result) == 1
        assert result[0].site_name == "good"

    @patch(DISCOVER_PYTHON)
    @patch(CREATE_YAML)
    @patch(DISCOVER_SITE)
    def test_handles_entry_point_discovery_failure(
        self,
        mock_site: MagicMock,
        mock_yaml: MagicMock,
        mock_python: MagicMock,
    ) -> None:
        mock_site.side_effect = RuntimeError("entry point broken")
        yaml_plugin = _make_plugin("yaml_site")
        mock_yaml.return_value = ([yaml_plugin], [])
        py_result = MagicMock()
        py_result.plugins = []
        py_result.errors = []
        mock_python.return_value = py_result

        result = discover_all_plugins()

        assert len(result) == 1
        assert result[0].site_name == "yaml_site"

    @patch(DISCOVER_PYTHON)
    @patch(CREATE_YAML)
    @patch(DISCOVER_SITE)
    def test_handles_yaml_discovery_failure(
        self,
        mock_site: MagicMock,
        mock_yaml: MagicMock,
        mock_python: MagicMock,
    ) -> None:
        mock_site.return_value = {}
        mock_yaml.side_effect = RuntimeError("yaml broken")
        py_result = MagicMock()
        py_result.plugins = []
        py_result.errors = []
        mock_python.return_value = py_result

        result = discover_all_plugins()

        assert len(result) == 0

    @patch(DISCOVER_PYTHON)
    @patch(CREATE_YAML)
    @patch(DISCOVER_SITE)
    def test_handles_python_file_discovery_failure(
        self,
        mock_site: MagicMock,
        mock_yaml: MagicMock,
        mock_python: MagicMock,
    ) -> None:
        mock_site.return_value = {}
        mock_yaml.return_value = ([], [])
        mock_python.side_effect = RuntimeError("python loader broken")

        result = discover_all_plugins()

        assert len(result) == 0

    @patch(DISCOVER_PYTHON)
    @patch(CREATE_YAML)
    @patch(DISCOVER_SITE)
    def test_returns_tuple(
        self,
        mock_site: MagicMock,
        mock_yaml: MagicMock,
        mock_python: MagicMock,
    ) -> None:
        """Result is a tuple (immutable, hashable for caching)."""
        mock_site.return_value = {}
        mock_yaml.return_value = ([], [])
        py_result = MagicMock()
        py_result.plugins = []
        py_result.errors = []
        mock_python.return_value = py_result

        result = discover_all_plugins()

        assert isinstance(result, tuple)


class TestGetPlugin:
    """Tests for get_plugin()."""

    def setup_method(self) -> None:
        """Clear the lru_cache before each test."""
        discover_all_plugins.cache_clear()

    @patch(DISCOVER_PYTHON)
    @patch(CREATE_YAML)
    @patch(DISCOVER_SITE)
    def test_returns_plugin_by_name(
        self,
        mock_site: MagicMock,
        mock_yaml: MagicMock,
        mock_python: MagicMock,
    ) -> None:
        plugin_a = _make_plugin("alpha")
        plugin_b = _make_plugin("beta")

        mock_site.return_value = {}
        mock_yaml.return_value = ([plugin_a, plugin_b], [])
        py_result = MagicMock()
        py_result.plugins = []
        py_result.errors = []
        mock_python.return_value = py_result

        result = get_plugin("beta")

        assert result.site_name == "beta"

    @patch(DISCOVER_PYTHON)
    @patch(CREATE_YAML)
    @patch(DISCOVER_SITE)
    def test_raises_plugin_error_on_unknown(
        self,
        mock_site: MagicMock,
        mock_yaml: MagicMock,
        mock_python: MagicMock,
    ) -> None:
        plugin_a = _make_plugin("alpha")

        mock_site.return_value = {}
        mock_yaml.return_value = ([plugin_a], [])
        py_result = MagicMock()
        py_result.plugins = []
        py_result.errors = []
        mock_python.return_value = py_result

        with pytest.raises(PluginError, match="unknown"):
            get_plugin("nonexistent")

    @patch(DISCOVER_PYTHON)
    @patch(CREATE_YAML)
    @patch(DISCOVER_SITE)
    def test_error_message_lists_available_plugins(
        self,
        mock_site: MagicMock,
        mock_yaml: MagicMock,
        mock_python: MagicMock,
    ) -> None:
        plugin_a = _make_plugin("alpha")
        plugin_b = _make_plugin("beta")

        mock_site.return_value = {}
        mock_yaml.return_value = ([plugin_a, plugin_b], [])
        py_result = MagicMock()
        py_result.plugins = []
        py_result.errors = []
        mock_python.return_value = py_result

        with pytest.raises(PluginError, match="alpha.*beta"):
            get_plugin("nonexistent")
