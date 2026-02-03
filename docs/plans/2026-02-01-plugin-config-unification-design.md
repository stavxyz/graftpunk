# Plugin Config Unification Design

## Problem

YAML and Python plugins have parallel paths that duplicate defaulting, validation, and instantiation logic:

- **Defaulting divergence**: YAML auto-defaults `session_name` to `site_name` and generates `help_text`; Python plugins don't, requiring manual configuration for the same behavior.
- **YAMLSitePlugin adapter**: ~120 lines of property delegation that mirror `SitePlugin` class attributes.
- **Validation gap**: YAML validates at parse time; Python validates late at registration. Different coverage.
- **`site_name` inference**: Implemented independently in both `yaml_loader.py` and `SitePlugin.__init_subclass__`.

## Design

### PluginConfig: single source of truth

A frozen dataclass holding all plugin-level configuration. Both YAML and Python paths produce one.

```python
@dataclass(frozen=True)
class PluginConfig:
    site_name: str
    session_name: str
    help_text: str
    base_url: str = ""
    requires_session: bool = True
    backend: str = "selenium"
    username_envvar: str = ""
    password_envvar: str = ""
    login_url: str = ""
    login_fields: tuple[tuple[str, str], ...] = ()  # frozen-friendly
    login_submit: str = ""
    login_failure: str = ""
    login_success: str = ""
```

### build_plugin_config(): shared factory

One function handles all defaulting and validation for both paths. It accepts `**kwargs`, filters to known `PluginConfig` fields via dataclass introspection, applies defaults, validates, and returns a `PluginConfig`.

- Infers `site_name` from `base_url` when not provided
- Defaults `session_name` to `site_name`
- Defaults `help_text` to `"Commands for {site_name}"`
- Validates `site_name` is non-empty (after inference)
- Lives in `cli_plugin.py` alongside `PluginConfig` and `SitePlugin`

Callers pass raw kwargs — no manual field enumeration:

```python
# YAML path: parsed dict → build
config = build_plugin_config(**normalized_yaml_dict)

# Python path: class attributes → build
config = build_plugin_config(**{
    k: v for k, v in cls.__dict__.items()
    if not k.startswith("_") and not callable(v)
})
```

### Python plugins

`SitePlugin.__init_subclass__` calls `build_plugin_config()` from class attributes, stores the result as `cls._plugin_config`, and writes inferred values back to class attributes so they stay consistent.

Plugin author API is unchanged — subclass, set attributes, use `@command`.

### YAML plugins

The YAML parser:
1. Reads YAML into a dict
2. Normalizes login blocks (nested `login:` → flat `login_url`, `login_fields`, etc.). Accepts both nested and flat forms.
3. Parses command definitions into `CommandSpec` objects with HTTP handler closures
4. Calls `build_plugin_config(**flat_dict)` → `PluginConfig`
5. Creates a dynamic `SitePlugin` subclass via `type()`, with `get_commands()` returning the parsed command specs
6. Returns a `SitePlugin` instance — same type as Python plugins

`__init_subclass__` fires on the dynamic subclass and calls `build_plugin_config()` again. This is idempotent since values are already defaulted.

### What gets deleted

- `YAMLSitePlugin` class (property delegation adapter)
- `YAMLPluginDef` dataclass (replaced by `PluginConfig`)
- `YAMLLoginDef` dataclass (login fields are flat on `PluginConfig`)
- Duplicated defaulting logic in `yaml_loader.py`
- Duplicated `site_name` inference in `SitePlugin.__init_subclass__`
- `isinstance(plugin, YAMLSitePlugin)` checks anywhere

### What stays

- YAML-specific: file reading, login block normalization, command handler closures
- Python-specific: file discovery, class introspection, `@command` decorator
- `SitePlugin` as the base class and public API
- `CommandSpec`, `ParamSpec`
- Registration logic in `plugin_commands.py` (simplified — everything is `SitePlugin`)

### Commands

Commands remain path-specific (genuinely different):
- YAML: data-driven (HTTP method + URL + jmespath) → handler closures
- Python: `@command`-decorated methods → introspected signatures

Both converge at `get_commands() → dict[str, CommandSpec]`. Registration code is already type-agnostic.

## File changes

| File | Change |
|------|--------|
| `cli_plugin.py` | Add `PluginConfig`, `build_plugin_config()`. Update `__init_subclass__`. |
| `yaml_loader.py` | Simplify to YAML parsing + login normalization. Delete `YAMLPluginDef`, `YAMLLoginDef`. Call `build_plugin_config()`. |
| `yaml_plugin.py` | Delete `YAMLSitePlugin`. Keep command handler closure builder. Add dynamic subclass factory. |
| `plugin_commands.py` | Remove `YAMLSitePlugin` imports and isinstance checks. |
| `plugins/__init__.py` | Export `PluginConfig`, `build_plugin_config`. Remove `YAMLPluginDef` re-exports if any. |
| Tests | Update to use `PluginConfig` instead of `YAMLPluginDef`. |
