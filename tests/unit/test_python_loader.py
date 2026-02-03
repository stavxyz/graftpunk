"""Tests for Python plugin auto-discovery."""

from pathlib import Path

import pytest


class TestDiscoverPythonPlugins:
    """Tests for Python plugin discovery from plugins directory."""

    def test_discover_no_plugins_dir(self, isolated_config: Path) -> None:
        """Test discovery when plugins directory doesn't exist."""
        from graftpunk.plugins.python_loader import discover_python_plugins

        result = discover_python_plugins()
        assert result.plugins == []
        assert result.errors == []

    def test_discover_empty_plugins_dir(self, isolated_config: Path) -> None:
        """Test discovery with empty plugins directory."""
        from graftpunk.plugins.python_loader import discover_python_plugins

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        result = discover_python_plugins()
        assert result.plugins == []
        assert result.errors == []

    def test_discover_single_plugin(self, isolated_config: Path) -> None:
        """Test discovering a single Python plugin."""
        from graftpunk.plugins.python_loader import discover_python_plugins

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        plugin_code = """
from graftpunk.plugins import SitePlugin, command

class TestPlugin(SitePlugin):
    site_name = "testplugin"
    session_name = "test"
    help_text = "Test plugin"

    @command(help="Test command")
    def test_cmd(self, ctx):
        return {"test": True}
"""
        (plugins_dir / "test_plugin.py").write_text(plugin_code)

        result = discover_python_plugins()
        assert len(result.plugins) == 1
        assert result.plugins[0].site_name == "testplugin"
        assert result.errors == []

    def test_discover_skips_underscore_files(self, isolated_config: Path) -> None:
        """Test that files starting with _ are skipped."""
        from graftpunk.plugins.python_loader import discover_python_plugins

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        (plugins_dir / "__init__.py").write_text("")
        (plugins_dir / "_helper.py").write_text("x = 1")

        result = discover_python_plugins()
        assert result.plugins == []
        assert result.errors == []

    def test_discover_handles_import_error(self, isolated_config: Path) -> None:
        """Test that import errors are collected, not raised."""
        from graftpunk.plugins.python_loader import discover_python_plugins

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        (plugins_dir / "bad_plugin.py").write_text("this is not valid python!")

        result = discover_python_plugins()
        assert result.plugins == []
        assert result.has_errors
        assert len(result.errors) == 1
        assert "bad_plugin.py" in str(result.errors[0].filepath)

    def test_discover_multiple_plugins_in_file(self, isolated_config: Path) -> None:
        """Test that multiple SitePlugin subclasses in one file are all discovered."""
        from graftpunk.plugins.python_loader import discover_python_plugins

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        plugin_code = """
from graftpunk.plugins import SitePlugin, command

class PluginA(SitePlugin):
    site_name = "plugin_a"
    session_name = ""
    help_text = "Plugin A"

class PluginB(SitePlugin):
    site_name = "plugin_b"
    session_name = ""
    help_text = "Plugin B"
"""
        (plugins_dir / "multi.py").write_text(plugin_code)

        result = discover_python_plugins()
        assert len(result.plugins) == 2
        site_names = {p.site_name for p in result.plugins}
        assert site_names == {"plugin_a", "plugin_b"}

    def test_discover_skips_non_siteplugin_classes(self, isolated_config: Path) -> None:
        """Test that classes not inheriting SitePlugin are skipped."""
        from graftpunk.plugins.python_loader import discover_python_plugins

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        plugin_code = """
class NotAPlugin:
    site_name = "fake"

class AlsoNotAPlugin:
    pass
"""
        (plugins_dir / "not_plugins.py").write_text(plugin_code)

        result = discover_python_plugins()
        assert result.plugins == []
        assert result.errors == []


class TestSystemExitReRaise:
    """Tests for SystemExit/KeyboardInterrupt re-raise in plugin instantiation."""

    def test_system_exit_propagates(self, isolated_config: Path) -> None:
        """Test that SystemExit during plugin instantiation is re-raised."""
        from graftpunk.plugins.python_loader import discover_python_plugins

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        plugin_code = """
from graftpunk.plugins import SitePlugin

class ExitPlugin(SitePlugin):
    site_name = "exitplugin"
    session_name = "exit"
    help_text = "Plugin that calls sys.exit"

    def __init__(self):
        raise SystemExit(1)
"""
        (plugins_dir / "exit_plugin.py").write_text(plugin_code)

        with pytest.raises(SystemExit):
            discover_python_plugins()

    def test_keyboard_interrupt_propagates(self, isolated_config: Path) -> None:
        """Test that KeyboardInterrupt during plugin instantiation is re-raised."""
        from graftpunk.plugins.python_loader import discover_python_plugins

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        plugin_code = """
from graftpunk.plugins import SitePlugin

class InterruptPlugin(SitePlugin):
    site_name = "interruptplugin2"
    session_name = "interrupt"
    help_text = "Plugin that raises KeyboardInterrupt"

    def __init__(self):
        raise KeyboardInterrupt()
"""
        (plugins_dir / "interrupt_plugin2.py").write_text(plugin_code)

        with pytest.raises(KeyboardInterrupt):
            discover_python_plugins()
