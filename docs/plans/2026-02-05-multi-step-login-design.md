# Multi-Step Login Forms Design

## Problem

The declarative login engine assumes all form fields are visible simultaneously. Identifier-first (split) flows — where the username is submitted before the password field appears — require a custom `login()` method, sacrificing the benefits of declarative configuration.

This is common: Azure AD B2C, Okta, Auth0, Google, and Microsoft Entra ID all use identifier-first flows.

## Design

### Data Model

New frozen dataclass `LoginStep`:

```python
@dataclass(frozen=True)
class LoginStep:
    fields: dict[str, str]   # credential name → CSS selector (can be empty)
    submit: str = ""         # CSS selector for submit (empty = no click)
    wait_for: str = ""       # CSS selector to wait for before this step
    delay: float = 0.0       # seconds to pause after this step's submit
```

Restructured `LoginConfig`:

```python
@dataclass(frozen=True)
class LoginConfig:
    steps: tuple[LoginStep, ...]  # ordered interaction steps (required)
    url: str = ""                 # path appended to base_url; empty = use base_url
    failure: str = ""
    success: str = ""
    wait_for: str = ""            # top-level wait before any steps
```

### Validation

`LoginStep`:
- At least one of `fields` or `submit` must be non-empty
- Each field selector: non-empty, non-whitespace
- `submit`: non-whitespace when non-empty
- `wait_for`: non-whitespace when non-empty
- `delay`: non-negative
- Defensive copy of `fields` dict

`LoginConfig`:
- `steps` must be non-empty
- `url`: non-whitespace when non-empty (empty = fall back to plugin base_url)
- `wait_for`, `failure`, `success`: non-whitespace when non-empty
- `steps` coerced from list to tuple if needed

### Login Engine

The step loop (nodriver):

```
top-level wait_for
  → for each step:
      step wait_for → fill fields → click submit → delay
  → _POST_SUBMIT_DELAY
  → check failure/success
```

Selenium gets the same structure with sync calls. Per-step `wait_for` on selenium raises `PluginError` (same as top-level).

Error messages include 1-based step index for debuggability.

### Credential Resolution

`resolve_login_fields` aggregates fields across all steps:

```python
all_fields = {}
for step in login_cfg.steps:
    all_fields.update(step.fields)
return all_fields
```

All credentials prompted upfront, dispensed to steps as needed.

### YAML Schema

```yaml
login:
  url: /
  wait_for: "#login-form"
  failure: "Invalid credentials"
  steps:
    - fields:
        username: "input#signInName"
      submit: "button#next"
    - wait_for: "#password-section"
      fields:
        password: "input#password"
      submit: "button#next"
```

### Migration (Clean Break)

No backwards compatibility with flat `fields`/`submit` API. Removed:
- `LoginConfig.fields`, `LoginConfig.submit`
- Flat YAML keys (`login_url`, `login_fields`, `login_submit`, etc.)
- `SitePlugin.__init_subclass__` flat attribute auto-construction
- `build_plugin_config` flat field popping

Every `LoginConfig(fields=..., submit=...)` becomes
`LoginConfig(steps=[LoginStep(fields=..., submit=...)])`.

### Files Affected

Production:
- `cli_plugin.py` — LoginConfig restructure, new LoginStep
- `login_engine.py` — step loop in both backends
- `login_commands.py` — resolve_login_fields aggregation
- `yaml_loader.py` — new steps parsing, remove flat keys
- `cli_plugin.py` — remove __init_subclass__ flat auto-construction
- `cli_plugin.py` — remove build_plugin_config flat field popping

Tests:
- `test_cli_plugin.py` — LoginConfig/LoginStep validation
- `test_login_engine.py` — login flow with steps
- `test_login_engine_retry.py` — retry/wait_for with steps
- `test_login_commands.py` — resolve_login_fields aggregation
- `test_yaml_loader.py` — YAML steps parsing

Documentation:
- `docs/HOW_IT_WORKS.md` — examples, declarative login section, core types
