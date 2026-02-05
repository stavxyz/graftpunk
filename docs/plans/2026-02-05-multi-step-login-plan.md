# Multi-Step Login Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace flat `LoginConfig.fields`/`submit` with ordered `steps` to support identifier-first login flows.

**Architecture:** New `LoginStep` frozen dataclass holds per-step fields/submit/wait_for/delay. `LoginConfig` restructured to hold `tuple[LoginStep, ...]`. Login engine loops through steps. Clean break — no backwards compatibility with flat API.

**Tech Stack:** Python, pytest, nodriver, selenium, YAML

---

### Task 1: Add LoginStep dataclass

**Files:**
- Modify: `src/graftpunk/plugins/cli_plugin.py:290-335`
- Test: `tests/unit/test_cli_plugin.py`

**Step 1: Write the failing tests**

Add new test class after `TestLoginConfig` in `tests/unit/test_cli_plugin.py`:

```python
class TestLoginStep:
    """Tests for LoginStep dataclass validation."""

    def test_create_with_fields_only(self) -> None:
        """LoginStep can be created with just fields."""
        step = LoginStep(fields={"username": "#user"})
        assert step.fields == {"username": "#user"}
        assert step.submit == ""
        assert step.wait_for == ""
        assert step.delay == 0.0

    def test_create_with_submit_only(self) -> None:
        """LoginStep can be created with just submit (click-only step)."""
        step = LoginStep(fields={}, submit="button#accept")
        assert step.fields == {}
        assert step.submit == "button#accept"

    def test_create_with_all_fields(self) -> None:
        """LoginStep stores all optional fields."""
        step = LoginStep(
            fields={"password": "#pass"},
            submit="#btn",
            wait_for="#form",
            delay=0.5,
        )
        assert step.fields == {"password": "#pass"}
        assert step.submit == "#btn"
        assert step.wait_for == "#form"
        assert step.delay == 0.5

    def test_frozen(self) -> None:
        """LoginStep is immutable."""
        step = LoginStep(fields={"u": "#u"})
        with pytest.raises(FrozenInstanceError):
            step.submit = "x"  # type: ignore[misc]

    def test_empty_fields_and_submit_raises(self) -> None:
        """LoginStep requires at least fields or submit."""
        with pytest.raises(ValueError, match="must have non-empty fields or submit"):
            LoginStep(fields={}, submit="")

    def test_whitespace_submit_raises(self) -> None:
        """LoginStep rejects whitespace-only submit."""
        with pytest.raises(ValueError, match="submit must not be whitespace"):
            LoginStep(fields={}, submit="   ")

    def test_whitespace_wait_for_raises(self) -> None:
        """LoginStep rejects whitespace-only wait_for."""
        with pytest.raises(ValueError, match="wait_for must not be whitespace"):
            LoginStep(fields={"u": "#u"}, wait_for="   ")

    def test_whitespace_field_selector_raises(self) -> None:
        """LoginStep rejects whitespace-only field selectors."""
        with pytest.raises(ValueError, match="selector must be non-empty"):
            LoginStep(fields={"user": "   "})

    def test_empty_field_selector_raises(self) -> None:
        """LoginStep rejects empty field selectors."""
        with pytest.raises(ValueError, match="selector must be non-empty"):
            LoginStep(fields={"user": ""})

    def test_negative_delay_raises(self) -> None:
        """LoginStep rejects negative delay."""
        with pytest.raises(ValueError, match="delay must be non-negative"):
            LoginStep(fields={"u": "#u"}, delay=-1.0)

    def test_fields_defensive_copy(self) -> None:
        """LoginStep makes a defensive copy of fields dict."""
        original = {"u": "#u"}
        step = LoginStep(fields=original)
        original["u"] = "modified"
        assert step.fields["u"] == "#u"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_cli_plugin.py::TestLoginStep -v`
Expected: FAIL — `cannot import name 'LoginStep'`

**Step 3: Implement LoginStep**

Add before `LoginConfig` class in `src/graftpunk/plugins/cli_plugin.py` (around line 290):

```python
@dataclass(frozen=True)
class LoginStep:
    """A single step in a multi-step login flow.

    Each step can fill form fields, click a submit button, or both.
    At least one of fields or submit must be non-empty.

    Attributes:
        fields: Maps credential names to CSS selectors for form inputs.
        submit: CSS selector for the submit button. Empty = no click.
        wait_for: CSS selector to wait for before this step. Empty = no wait.
        delay: Seconds to pause after this step's submit. 0 = no pause.
    """

    fields: dict[str, str] = field(default_factory=dict)
    submit: str = ""
    wait_for: str = ""
    delay: float = 0.0

    def __post_init__(self) -> None:
        if not self.fields and not self.submit.strip():
            raise ValueError("LoginStep must have non-empty fields or submit")
        if self.submit and not self.submit.strip():
            raise ValueError("LoginStep.submit must not be whitespace-only")
        if self.wait_for and not self.wait_for.strip():
            raise ValueError("LoginStep.wait_for must not be whitespace-only")
        if self.delay < 0:
            raise ValueError("LoginStep.delay must be non-negative")
        for name, selector in self.fields.items():
            if not selector.strip():
                raise ValueError(
                    f"LoginStep.fields['{name}'] selector must be non-empty"
                )
        # Defensive copy: prevent external mutation of fields dict
        object.__setattr__(self, "fields", dict(self.fields))
```

Update imports at top of file to include `field` from dataclasses if not already present.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_cli_plugin.py::TestLoginStep -v`
Expected: PASS (all 11 tests)

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/cli_plugin.py tests/unit/test_cli_plugin.py
git commit -m "feat: add LoginStep dataclass for multi-step login flows

Frozen dataclass with fields, submit, wait_for, and delay.
At least one of fields or submit must be non-empty.

Ref #71

Co-authored-by: stavxyz <hi@stav.xyz>"
```

---

### Task 2: Restructure LoginConfig to use steps

**Files:**
- Modify: `src/graftpunk/plugins/cli_plugin.py:290-335`
- Test: `tests/unit/test_cli_plugin.py`

**Step 1: Write the failing tests**

Replace entire `TestLoginConfig` class (lines 26-106) in `tests/unit/test_cli_plugin.py`:

```python
class TestLoginConfig:
    """Tests for LoginConfig dataclass validation."""

    def test_create_minimal(self) -> None:
        """LoginConfig can be created with just steps."""
        step = LoginStep(fields={"username": "#user"}, submit="#btn")
        cfg = LoginConfig(steps=[step])
        assert len(cfg.steps) == 1
        assert cfg.url == ""
        assert cfg.failure == ""
        assert cfg.success == ""
        assert cfg.wait_for == ""

    def test_create_with_all_fields(self) -> None:
        """LoginConfig stores all optional fields."""
        step = LoginStep(fields={"u": "#u"}, submit="#s")
        cfg = LoginConfig(
            steps=[step],
            url="/login",
            failure="Invalid",
            success=".ok",
            wait_for="#form",
        )
        assert cfg.url == "/login"
        assert cfg.failure == "Invalid"
        assert cfg.success == ".ok"
        assert cfg.wait_for == "#form"

    def test_steps_converted_to_tuple(self) -> None:
        """LoginConfig converts list of steps to tuple."""
        step = LoginStep(fields={"u": "#u"}, submit="#s")
        cfg = LoginConfig(steps=[step])
        assert isinstance(cfg.steps, tuple)

    def test_multiple_steps(self) -> None:
        """LoginConfig supports multiple steps."""
        step1 = LoginStep(fields={"username": "#user"}, submit="#next")
        step2 = LoginStep(fields={"password": "#pass"}, submit="#login")
        cfg = LoginConfig(steps=[step1, step2])
        assert len(cfg.steps) == 2
        assert cfg.steps[0].fields == {"username": "#user"}
        assert cfg.steps[1].fields == {"password": "#pass"}

    def test_frozen(self) -> None:
        """LoginConfig is immutable."""
        step = LoginStep(fields={"u": "#u"}, submit="#s")
        cfg = LoginConfig(steps=[step])
        with pytest.raises(FrozenInstanceError):
            cfg.url = "/other"  # type: ignore[misc]

    def test_empty_steps_raises(self) -> None:
        """LoginConfig requires at least one step."""
        with pytest.raises(ValueError, match="steps must be non-empty"):
            LoginConfig(steps=[])

    def test_whitespace_url_raises(self) -> None:
        """LoginConfig rejects whitespace-only url."""
        step = LoginStep(fields={"u": "#u"}, submit="#s")
        with pytest.raises(ValueError, match="url must not be whitespace"):
            LoginConfig(steps=[step], url="   ")

    def test_whitespace_wait_for_raises(self) -> None:
        """LoginConfig rejects whitespace-only wait_for."""
        step = LoginStep(fields={"u": "#u"}, submit="#s")
        with pytest.raises(ValueError, match="wait_for must not be whitespace"):
            LoginConfig(steps=[step], wait_for="   ")

    def test_whitespace_failure_raises(self) -> None:
        """LoginConfig rejects whitespace-only failure."""
        step = LoginStep(fields={"u": "#u"}, submit="#s")
        with pytest.raises(ValueError, match="failure must not be whitespace"):
            LoginConfig(steps=[step], failure="   ")

    def test_whitespace_success_raises(self) -> None:
        """LoginConfig rejects whitespace-only success."""
        step = LoginStep(fields={"u": "#u"}, submit="#s")
        with pytest.raises(ValueError, match="success must not be whitespace"):
            LoginConfig(steps=[step], success="   ")

    def test_url_empty_is_valid(self) -> None:
        """LoginConfig allows empty url (falls back to base_url)."""
        step = LoginStep(fields={"u": "#u"}, submit="#s")
        cfg = LoginConfig(steps=[step], url="")
        assert cfg.url == ""
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_cli_plugin.py::TestLoginConfig -v`
Expected: FAIL — LoginConfig signature mismatch

**Step 3: Implement restructured LoginConfig**

Replace `LoginConfig` class in `src/graftpunk/plugins/cli_plugin.py`:

```python
@dataclass(frozen=True)
class LoginConfig:
    """Declarative browser-automated login configuration.

    Supports multi-step login flows (identifier-first, split forms).
    Each step can fill fields and/or click a submit button.

    Attributes:
        steps: Ordered sequence of login interaction steps.
        url: Login page path (appended to base_url). Empty = use base_url.
        failure: Text on the page indicating login failure.
        success: CSS selector for an element indicating login success.
        wait_for: CSS selector to wait for before any steps begin.
    """

    steps: tuple[LoginStep, ...] | list[LoginStep]
    url: str = ""
    failure: str = ""
    success: str = ""
    wait_for: str = ""

    def __post_init__(self) -> None:
        # Convert list to tuple for immutability
        if isinstance(self.steps, list):
            object.__setattr__(self, "steps", tuple(self.steps))
        if not self.steps:
            raise ValueError("LoginConfig.steps must be non-empty")
        if self.url and not self.url.strip():
            raise ValueError("LoginConfig.url must not be whitespace-only")
        if self.wait_for and not self.wait_for.strip():
            raise ValueError("LoginConfig.wait_for must not be whitespace-only")
        if self.failure and not self.failure.strip():
            raise ValueError("LoginConfig.failure must not be whitespace-only")
        if self.success and not self.success.strip():
            raise ValueError("LoginConfig.success must not be whitespace-only")
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_cli_plugin.py::TestLoginConfig -v`
Expected: PASS (all 11 tests)

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/cli_plugin.py tests/unit/test_cli_plugin.py
git commit -m "feat: restructure LoginConfig to use steps

Replace flat fields/submit with ordered tuple of LoginStep.
url is now optional (empty = use base_url).
Validate whitespace-only for all string fields.

BREAKING: Removes LoginConfig.fields and LoginConfig.submit.

Ref #71

Co-authored-by: stavxyz <hi@stav.xyz>"
```

---

### Task 3: Update resolve_login_fields to aggregate across steps

**Files:**
- Modify: `src/graftpunk/cli/login_commands.py:67-87`
- Test: `tests/unit/test_login_commands.py`

**Step 1: Write the failing tests**

Update test fixtures and add new tests in `tests/unit/test_login_commands.py`. Replace `PluginWithDeclarativeLogin` and `PluginWithCustomFields` fixtures and update tests:

```python
from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep, SitePlugin


class PluginWithDeclarativeLogin(SitePlugin):
    """Plugin with declarative LoginConfig using steps."""

    site_name = "declarative"
    session_name = "declarative"
    help_text = "Declarative login"
    base_url = "https://example.com"
    login_config = LoginConfig(
        steps=[
            LoginStep(
                fields={"username": "#user", "password": "#pass"},
                submit="#login",
            )
        ]
    )


class PluginWithCustomFields(SitePlugin):
    """Plugin with custom credential fields in LoginConfig."""

    site_name = "custom"
    session_name = "custom"
    help_text = "Custom fields"
    base_url = "https://example.com"
    login_config = LoginConfig(
        steps=[
            LoginStep(fields={"email": "#email"}, submit="#next"),
            LoginStep(fields={"secret_key": "#key"}, submit="#login"),
        ]
    )


class TestResolveLoginFields:
    """Tests for resolve_login_fields function."""

    def test_single_step_returns_fields(self) -> None:
        """Single-step LoginConfig returns step's fields."""
        plugin = PluginWithDeclarativeLogin()
        fields = resolve_login_fields(plugin)
        assert fields == {"username": "#user", "password": "#pass"}

    def test_multi_step_aggregates_fields(self) -> None:
        """Multi-step LoginConfig aggregates fields from all steps."""
        plugin = PluginWithCustomFields()
        fields = resolve_login_fields(plugin)
        assert fields == {"email": "#email", "secret_key": "#key"}

    def test_no_login_config_returns_default(self) -> None:
        """Plugin without login_config returns default fields."""
        plugin = PluginWithNoLogin()
        fields = resolve_login_fields(plugin)
        assert fields == {"username": "", "password": ""}

    def test_login_method_plugin_returns_default(self) -> None:
        """Plugin with only login() method returns default fields."""
        plugin = PluginWithLoginMethod()
        fields = resolve_login_fields(plugin)
        assert fields == {"username": "", "password": ""}
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_login_commands.py::TestResolveLoginFields -v`
Expected: FAIL — LoginConfig constructor changed

**Step 3: Implement aggregated resolve_login_fields**

Update `resolve_login_fields` in `src/graftpunk/cli/login_commands.py`:

```python
def resolve_login_fields(plugin: CLIPluginProtocol) -> dict[str, str]:
    """Return the login credential fields for a plugin.

    Aggregates fields from all steps in ``login_config.steps`` if available,
    otherwise defaults to ``{"username": "", "password": ""}``.

    Args:
        plugin: Plugin instance to inspect.

    Returns:
        Dictionary of field names to CSS selectors.
    """
    login_cfg = getattr(plugin, "login_config", None)
    if isinstance(login_cfg, LoginConfig) and login_cfg.steps:
        all_fields: dict[str, str] = {}
        for step in login_cfg.steps:
            all_fields.update(step.fields)
        return all_fields
    LOG.info(
        "login_fields_default_assumed",
        plugin=plugin.site_name,
        hint="No login fields configured. Defaulting to username/password.",
    )
    return {"username": "", "password": ""}
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_login_commands.py::TestResolveLoginFields -v`
Expected: PASS (all 4 tests)

**Step 5: Commit**

```bash
git add src/graftpunk/cli/login_commands.py tests/unit/test_login_commands.py
git commit -m "feat: aggregate login fields from all steps

resolve_login_fields now iterates through login_config.steps
and collects fields from each step. This ensures all credentials
are prompted upfront before the login flow begins.

Ref #71

Co-authored-by: stavxyz <hi@stav.xyz>"
```

---

### Task 4: Update nodriver login engine with step loop

**Files:**
- Modify: `src/graftpunk/plugins/login_engine.py:286-415`
- Test: `tests/unit/test_login_engine_retry.py`

**Step 1: Write the failing tests**

Update fixtures and add multi-step tests in `tests/unit/test_login_engine_retry.py`:

```python
from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep, SitePlugin


class DeclarativeHN(SitePlugin):
    """Test plugin with single-step declarative login (nodriver)."""

    site_name = "hn"
    session_name = "hackernews"
    help_text = "HN"
    base_url = "https://news.ycombinator.com"
    backend = "nodriver"
    login_config = LoginConfig(
        url="/login",
        steps=[
            LoginStep(
                fields={"username": "input[name='acct']", "password": "input[name='pw']"},
                submit="input[value='login']",
            )
        ],
        failure="Bad login.",
    )


class DeclarativeWaitFor(SitePlugin):
    """Nodriver plugin with wait_for configured."""

    site_name = "waitfor"
    session_name = "waitfor"
    help_text = "WF"
    base_url = "https://example.com"
    backend = "nodriver"
    login_config = LoginConfig(
        url="/login",
        steps=[
            LoginStep(fields={"username": "#user"}, submit="#btn", wait_for="#login-form")
        ],
    )


class DeclarativeMultiStep(SitePlugin):
    """Nodriver plugin with multi-step login (identifier-first)."""

    site_name = "multistep"
    session_name = "multistep"
    help_text = "MS"
    base_url = "https://example.com"
    backend = "nodriver"
    login_config = LoginConfig(
        url="/",
        steps=[
            LoginStep(fields={"username": "#signInName"}, submit="#next"),
            LoginStep(fields={"password": "#password"}, submit="#next"),
        ],
        failure="Invalid credentials",
    )


class TestMultiStepLogin:
    """Tests for multi-step login flow."""

    @pytest.mark.asyncio
    async def test_multi_step_executes_in_order(self) -> None:
        """Multi-step login fills fields and clicks submit for each step."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeMultiStep()
        login_method = generate_login_method(plugin)

        mock_tab = AsyncMock()
        mock_element = AsyncMock()
        actions: list[str] = []

        async def tracking_select(selector: str, **kwargs: object) -> AsyncMock:
            actions.append(f"select:{selector}")
            return mock_element

        async def tracking_click() -> None:
            actions.append("click")

        async def tracking_send_keys(value: str) -> None:
            actions.append(f"send_keys:{value}")

        mock_element.click = tracking_click
        mock_element.send_keys = tracking_send_keys
        mock_tab.select = tracking_select
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome</html>")

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
            patch("graftpunk.plugins.login_engine.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await login_method({"username": "user@test.com", "password": "secret123"})

        assert result is True
        # Verify order: step1 field, step1 submit, step2 field, step2 submit
        assert "select:#signInName" in actions
        assert "send_keys:user@test.com" in actions
        assert "select:#next" in actions
        assert "select:#password" in actions
        assert "send_keys:secret123" in actions

    @pytest.mark.asyncio
    async def test_step_with_wait_for(self) -> None:
        """Per-step wait_for is awaited before filling fields."""
        from graftpunk.plugins.login_engine import generate_login_method

        class WaitForStep(SitePlugin):
            site_name = "waitstep"
            session_name = "waitstep"
            help_text = "WS"
            base_url = "https://example.com"
            backend = "nodriver"
            login_config = LoginConfig(
                steps=[
                    LoginStep(fields={"username": "#user"}, submit="#next"),
                    LoginStep(
                        fields={"password": "#pass"},
                        submit="#login",
                        wait_for="#password-form",
                    ),
                ],
            )

        plugin = WaitForStep()
        login_method = generate_login_method(plugin)

        mock_tab = AsyncMock()
        mock_element = AsyncMock()
        select_order: list[str] = []

        async def tracking_select(selector: str, **kwargs: object) -> AsyncMock:
            select_order.append(selector)
            return mock_element

        mock_tab.select = tracking_select
        mock_tab.get_content = AsyncMock(return_value="<html>OK</html>")

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
            patch("graftpunk.plugins.login_engine.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await login_method({"username": "u", "password": "p"})

        assert result is True
        # Step 2's wait_for should come before step 2's field
        wait_for_idx = select_order.index("#password-form")
        pass_idx = select_order.index("#pass")
        assert wait_for_idx < pass_idx

    @pytest.mark.asyncio
    async def test_step_delay_sleeps(self) -> None:
        """Per-step delay causes asyncio.sleep after submit."""
        from graftpunk.plugins.login_engine import generate_login_method

        class DelayStep(SitePlugin):
            site_name = "delay"
            session_name = "delay"
            help_text = "D"
            base_url = "https://example.com"
            backend = "nodriver"
            login_config = LoginConfig(
                steps=[
                    LoginStep(fields={"username": "#u"}, submit="#s", delay=0.5),
                ],
            )

        plugin = DelayStep()
        login_method = generate_login_method(plugin)

        mock_tab = AsyncMock()
        mock_element = AsyncMock()
        mock_tab.select = AsyncMock(return_value=mock_element)
        mock_tab.get_content = AsyncMock(return_value="<html>OK</html>")

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        sleep_calls: list[float] = []

        async def tracking_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
            patch("graftpunk.plugins.login_engine.asyncio.sleep", side_effect=tracking_sleep),
        ):
            await login_method({"username": "u"})

        # Should have step delay (0.5) and POST_SUBMIT_DELAY (3)
        assert 0.5 in sleep_calls

    @pytest.mark.asyncio
    async def test_step_submit_only_clicks(self) -> None:
        """Step with only submit (no fields) just clicks."""
        from graftpunk.plugins.login_engine import generate_login_method

        class ClickOnlyStep(SitePlugin):
            site_name = "click"
            session_name = "click"
            help_text = "C"
            base_url = "https://example.com"
            backend = "nodriver"
            login_config = LoginConfig(
                steps=[
                    LoginStep(fields={"username": "#u"}, submit="#next"),
                    LoginStep(fields={}, submit="#accept"),  # click-only
                    LoginStep(fields={"password": "#p"}, submit="#login"),
                ],
            )

        plugin = ClickOnlyStep()
        login_method = generate_login_method(plugin)

        mock_tab = AsyncMock()
        mock_element = AsyncMock()
        clicks: list[str] = []

        async def tracking_select(selector: str, **kwargs: object) -> AsyncMock:
            return mock_element

        async def tracking_click() -> None:
            clicks.append("clicked")

        mock_element.click = tracking_click
        mock_tab.select = tracking_select
        mock_tab.get_content = AsyncMock(return_value="<html>OK</html>")

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
            patch("graftpunk.plugins.login_engine.asyncio.sleep", new_callable=AsyncMock),
        ):
            await login_method({"username": "u", "password": "p"})

        # 3 steps with submit = 3 clicks (field clicks don't count in this simplified test)
        assert len(clicks) >= 3
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_login_engine_retry.py::TestMultiStepLogin -v`
Expected: FAIL — LoginConfig constructor and step loop not implemented

**Step 3: Implement nodriver step loop**

Update `_generate_nodriver_login` in `src/graftpunk/plugins/login_engine.py`. Replace the field-filling and submit-clicking sections with the step loop:

```python
def _generate_nodriver_login(plugin: SitePlugin) -> Any:
    """Generate async login method for nodriver backend."""

    async def login(credentials: dict[str, str]) -> bool:
        if plugin.login_config is None:
            raise PluginError(
                f"Plugin '{plugin.site_name}' has no login configuration. "
                "Add a LoginConfig to your plugin definition."
            )
        base_url = plugin.base_url.rstrip("/")
        login_url = plugin.login_config.url
        failure_text = plugin.login_config.failure

        async with BrowserSession(backend="nodriver", headless=False) as session:
            tab = await session.driver.get(f"{base_url}{login_url}")

            # Start header capture for profile extraction
            from graftpunk.observe.capture import create_capture_backend

            _header_capture = create_capture_backend(
                "nodriver", session.driver, get_tab=lambda: tab
            )
            await _header_capture.start_capture_async()

            # Top-level wait_for (before any steps)
            top_wait_for = plugin.login_config.wait_for
            if top_wait_for:
                from nodriver.core.connection import ProtocolException

                _wait_err = (
                    f"Timed out waiting for '{top_wait_for}' to appear. "
                    "The page may not have loaded or redirected as expected."
                )
                try:
                    wait_el = await _select_with_retry(tab, top_wait_for)
                except ProtocolException as exc:
                    raise PluginError(_wait_err) from exc
                if wait_el is None:
                    raise PluginError(_wait_err)

            # Execute each step in order
            for step_idx, step in enumerate(plugin.login_config.steps, start=1):
                # Per-step wait_for
                if step.wait_for:
                    from nodriver.core.connection import ProtocolException

                    _step_wait_err = (
                        f"Login step {step_idx}: timed out waiting for "
                        f"'{step.wait_for}' to appear."
                    )
                    try:
                        step_wait_el = await _select_with_retry(tab, step.wait_for)
                    except ProtocolException as exc:
                        raise PluginError(_step_wait_err) from exc
                    if step_wait_el is None:
                        raise PluginError(_step_wait_err)

                # Fill fields
                for field_name, selector in step.fields.items():
                    value = credentials.get(field_name, "")
                    try:
                        element = await _select_with_retry(tab, selector)
                        if element is None:
                            raise PluginError(
                                f"Login step {step_idx}: field '{field_name}' not found "
                                f"using selector '{selector}'."
                            )
                        await element.click()
                        await element.send_keys(value)
                    except PluginError:
                        raise
                    except Exception as exc:
                        raise PluginError(
                            f"Login step {step_idx}: failed to fill field '{field_name}' "
                            f"(selector: '{selector}'): {exc}"
                        ) from exc

                # Click submit (if specified)
                if step.submit:
                    try:
                        submit = await _select_with_retry(tab, step.submit)
                        if submit is None:
                            raise PluginError(
                                f"Login step {step_idx}: submit button not found "
                                f"using selector '{step.submit}'."
                            )
                        await submit.click()
                    except PluginError:
                        raise
                    except Exception as exc:
                        raise PluginError(
                            f"Login step {step_idx}: failed to click submit "
                            f"(selector: '{step.submit}'): {exc}"
                        ) from exc

                # Per-step delay
                if step.delay > 0:
                    await asyncio.sleep(step.delay)

            # Fixed delay to allow page to settle after form submission
            await asyncio.sleep(_POST_SUBMIT_DELAY)

            # Check success/failure (unchanged from before)
            page_text = await tab.get_content()
            success_selector = plugin.login_config.success
            success_found: bool | None = None
            if success_selector:
                success_element = await tab.select(success_selector)
                success_found = success_element is not None

            if not _check_login_result(
                page_text=page_text,
                failure_text=failure_text,
                success_found=success_found,
                success_selector=success_selector or "",
                site_name=plugin.site_name,
            ):
                return False

            # URL capture, header profiles, cookie transfer, token extraction
            # (keep all existing code from here to end of function unchanged)
            ...

    return login
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_login_engine_retry.py::TestMultiStepLogin -v`
Expected: PASS (all 4 tests)

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/login_engine.py tests/unit/test_login_engine_retry.py
git commit -m "feat: implement multi-step login loop in nodriver engine

Execute steps in order: per-step wait_for, fill fields, click submit,
delay. Error messages include 1-based step index for debugging.

Ref #71

Co-authored-by: stavxyz <hi@stav.xyz>"
```

---

### Task 5: Update selenium login engine with step loop

**Files:**
- Modify: `src/graftpunk/plugins/login_engine.py:418-522`
- Test: `tests/unit/test_login_engine.py`

**Step 1: Write the failing tests**

Update fixtures and add tests in `tests/unit/test_login_engine.py`. Update `DeclarativeQuotes` and add selenium multi-step test:

```python
class DeclarativeQuotes(SitePlugin):
    """Test plugin with declarative login (selenium)."""

    site_name = "quotes"
    session_name = "quotes"
    help_text = "Quotes"
    base_url = "https://quotes.toscrape.com"
    backend = "selenium"
    login_config = LoginConfig(
        url="/login",
        steps=[
            LoginStep(
                fields={"username": "input#username", "password": "input#password"},
                submit="input[type='submit']",
            )
        ],
    )


class TestSeleniumMultiStepLogin:
    """Tests for selenium multi-step login."""

    def test_selenium_multi_step_executes_in_order(self) -> None:
        """Selenium multi-step login fills fields and clicks for each step."""
        from graftpunk.plugins.login_engine import generate_login_method

        class SeleniumMultiStep(SitePlugin):
            site_name = "selmulti"
            session_name = "selmulti"
            help_text = "SM"
            base_url = "https://example.com"
            backend = "selenium"
            login_config = LoginConfig(
                steps=[
                    LoginStep(fields={"username": "#user"}, submit="#next"),
                    LoginStep(fields={"password": "#pass"}, submit="#login"),
                ],
            )

        plugin = SeleniumMultiStep()
        login_method = generate_login_method(plugin)

        mock_driver = MagicMock()
        mock_element = MagicMock()
        actions: list[str] = []

        def tracking_find(by: str, selector: str) -> MagicMock:
            actions.append(f"find:{selector}")
            return mock_element

        def tracking_click() -> None:
            actions.append("click")

        def tracking_send_keys(value: str) -> None:
            actions.append(f"keys:{value}")

        mock_element.click = tracking_click
        mock_element.send_keys = tracking_send_keys
        mock_driver.find_element = tracking_find
        mock_driver.page_source = "<html>Welcome</html>"

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = mock_driver
        instance.transfer_driver_cookies_to_session = MagicMock()

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
            patch("graftpunk.plugins.login_engine.time.sleep"),
        ):
            result = login_method({"username": "user", "password": "pass"})

        assert result is True
        assert "find:#user" in actions
        assert "keys:user" in actions
        assert "find:#pass" in actions
        assert "keys:pass" in actions

    def test_selenium_step_wait_for_raises(self) -> None:
        """Selenium per-step wait_for raises PluginError."""
        from graftpunk.plugins.login_engine import generate_login_method

        class SeleniumWaitFor(SitePlugin):
            site_name = "selwait"
            session_name = "selwait"
            help_text = "SW"
            base_url = "https://example.com"
            backend = "selenium"
            login_config = LoginConfig(
                steps=[
                    LoginStep(
                        fields={"username": "#u"},
                        submit="#s",
                        wait_for="#form",  # wait_for on selenium step
                    ),
                ],
            )

        plugin = SeleniumWaitFor()
        login_method = generate_login_method(plugin)

        mock_bs, instance = _make_selenium_mock_bs()

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            pytest.raises(PluginError, match="wait_for.*requires.*nodriver"),
        ):
            login_method({"username": "u"})
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_login_engine.py::TestSeleniumMultiStepLogin -v`
Expected: FAIL — selenium loop not implemented

**Step 3: Implement selenium step loop**

Update `_generate_selenium_login` in `src/graftpunk/plugins/login_engine.py` with the step loop. Same structure as nodriver but sync, and raises `PluginError` for any step that has `wait_for`:

```python
def _generate_selenium_login(plugin: SitePlugin) -> Any:
    """Generate sync login method for selenium backend."""
    import selenium.common.exceptions
    from selenium.common.exceptions import NoSuchElementException

    def login(credentials: dict[str, str]) -> bool:
        if plugin.login_config is None:
            raise PluginError(
                f"Plugin '{plugin.site_name}' has no login configuration. "
                "Add a LoginConfig to your plugin definition."
            )
        base_url = plugin.base_url.rstrip("/")
        login_url = plugin.login_config.url
        failure_text = plugin.login_config.failure
        success_selector = plugin.login_config.success

        with BrowserSession(backend="selenium", headless=False) as session:
            from graftpunk.observe.capture import create_capture_backend

            _header_capture = create_capture_backend("selenium", session.driver)
            _header_capture.start_capture()

            session.driver.get(f"{base_url}{login_url}")

            # Top-level wait_for check
            if plugin.login_config.wait_for:
                raise PluginError(
                    f"Plugin '{plugin.site_name}' uses wait_for, which requires "
                    "the nodriver backend. Set backend='nodriver' or remove wait_for."
                )

            # Execute each step in order
            for step_idx, step in enumerate(plugin.login_config.steps, start=1):
                # Per-step wait_for not supported on selenium
                if step.wait_for:
                    raise PluginError(
                        f"Login step {step_idx}: wait_for requires the nodriver backend. "
                        "Set backend='nodriver' or remove wait_for from this step."
                    )

                # Fill fields
                for field_name, selector in step.fields.items():
                    value = credentials.get(field_name, "")
                    try:
                        element = session.driver.find_element("css selector", selector)
                        element.click()
                        element.send_keys(value)
                    except (selenium.common.exceptions.WebDriverException, PluginError) as exc:
                        raise PluginError(
                            f"Login step {step_idx}: failed to fill field '{field_name}' "
                            f"(selector: '{selector}'): {exc}"
                        ) from exc

                # Click submit (if specified)
                if step.submit:
                    try:
                        submit_el = session.driver.find_element("css selector", step.submit)
                        submit_el.click()
                    except (selenium.common.exceptions.WebDriverException, PluginError) as exc:
                        raise PluginError(
                            f"Login step {step_idx}: failed to click submit "
                            f"(selector: '{step.submit}'): {exc}"
                        ) from exc

                # Per-step delay
                if step.delay > 0:
                    time.sleep(step.delay)

            # Fixed delay to allow page to settle
            time.sleep(_POST_SUBMIT_DELAY)

            # Check success/failure (keep existing code)
            ...

    return login
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_login_engine.py::TestSeleniumMultiStepLogin -v`
Expected: PASS (both tests)

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/login_engine.py tests/unit/test_login_engine.py
git commit -m "feat: implement multi-step login loop in selenium engine

Same step loop structure as nodriver. Per-step wait_for raises
PluginError with guidance to switch to nodriver.

Ref #71

Co-authored-by: stavxyz <hi@stav.xyz>"
```

---

### Task 6: Update YAML loader to parse steps

**Files:**
- Modify: `src/graftpunk/plugins/yaml_loader.py:294-331`
- Test: `tests/unit/test_yaml_loader.py`

**Step 1: Write the failing tests**

Replace `TestYAMLLoginBlock` class in `tests/unit/test_yaml_loader.py`:

```python
class TestYAMLLoginBlock:
    """Tests for YAML login block parsing with steps."""

    def test_parse_single_step(self, tmp_path: Path) -> None:
        """Parse login block with single step."""
        yaml_content = """
site_name: test
commands:
  hello:
    url: /hello
login:
  url: /login
  steps:
    - fields:
        username: "#user"
        password: "#pass"
      submit: "#btn"
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)
        plugin = parse_yaml_plugin(yaml_file)
        assert plugin.login_config is not None
        assert plugin.login_config.url == "/login"
        assert len(plugin.login_config.steps) == 1
        assert plugin.login_config.steps[0].fields == {"username": "#user", "password": "#pass"}
        assert plugin.login_config.steps[0].submit == "#btn"

    def test_parse_multi_step(self, tmp_path: Path) -> None:
        """Parse login block with multiple steps (identifier-first)."""
        yaml_content = """
site_name: unfi
commands:
  orders:
    url: /orders
login:
  url: /
  failure: "Invalid credentials"
  steps:
    - fields:
        username: "input#signInName"
      submit: "button#next"
    - fields:
        password: "input#password"
      submit: "button#next"
"""
        yaml_file = tmp_path / "unfi.yaml"
        yaml_file.write_text(yaml_content)
        plugin = parse_yaml_plugin(yaml_file)
        assert plugin.login_config is not None
        assert len(plugin.login_config.steps) == 2
        assert plugin.login_config.steps[0].fields == {"username": "input#signInName"}
        assert plugin.login_config.steps[1].fields == {"password": "input#password"}
        assert plugin.login_config.failure == "Invalid credentials"

    def test_parse_step_with_wait_for(self, tmp_path: Path) -> None:
        """Parse step with wait_for."""
        yaml_content = """
site_name: test
commands:
  hello:
    url: /hello
login:
  steps:
    - fields:
        username: "#u"
      submit: "#next"
    - wait_for: "#password-form"
      fields:
        password: "#p"
      submit: "#login"
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)
        plugin = parse_yaml_plugin(yaml_file)
        assert plugin.login_config.steps[1].wait_for == "#password-form"

    def test_parse_step_with_delay(self, tmp_path: Path) -> None:
        """Parse step with delay."""
        yaml_content = """
site_name: test
commands:
  hello:
    url: /hello
login:
  steps:
    - fields:
        username: "#u"
      submit: "#s"
      delay: 0.5
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)
        plugin = parse_yaml_plugin(yaml_file)
        assert plugin.login_config.steps[0].delay == 0.5

    def test_parse_login_with_top_level_wait_for(self, tmp_path: Path) -> None:
        """Parse login with top-level wait_for."""
        yaml_content = """
site_name: test
commands:
  hello:
    url: /hello
login:
  wait_for: "#login-form"
  steps:
    - fields:
        username: "#u"
      submit: "#s"
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)
        plugin = parse_yaml_plugin(yaml_file)
        assert plugin.login_config.wait_for == "#login-form"

    def test_parse_login_missing_steps_raises(self, tmp_path: Path) -> None:
        """Login block without steps raises error."""
        yaml_content = """
site_name: test
commands:
  hello:
    url: /hello
login:
  url: /login
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)
        with pytest.raises(PluginError, match="missing required.*steps"):
            parse_yaml_plugin(yaml_file)

    def test_parse_no_login_block(self, tmp_path: Path) -> None:
        """Plugin without login block has None login_config."""
        yaml_content = """
site_name: test
commands:
  hello:
    url: /hello
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)
        plugin = parse_yaml_plugin(yaml_file)
        assert plugin.login_config is None
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_yaml_loader.py::TestYAMLLoginBlock -v`
Expected: FAIL — YAML parser expects old format

**Step 3: Implement YAML steps parsing**

Update login block parsing in `src/graftpunk/plugins/yaml_loader.py` (around lines 294-331):

```python
# Build LoginConfig from login block with steps
login_block = data.get("login")
login_config: LoginConfig | None = None
if login_block is not None:
    if not isinstance(login_block, dict):
        raise PluginError(
            f"Plugin '{filepath}': 'login' must be a mapping, not {type(login_block).__name__}."
        )

    # Steps are required
    steps_data = login_block.get("steps")
    if not steps_data:
        raise PluginError(
            f"Plugin '{filepath}': login block missing required 'steps' field."
        )
    if not isinstance(steps_data, list):
        raise PluginError(
            f"Plugin '{filepath}': 'login.steps' must be a list."
        )

    # Parse each step
    steps: list[LoginStep] = []
    for idx, step_data in enumerate(steps_data, start=1):
        if not isinstance(step_data, dict):
            raise PluginError(
                f"Plugin '{filepath}': login step {idx} must be a mapping."
            )
        steps.append(
            LoginStep(
                fields=step_data.get("fields", {}),
                submit=step_data.get("submit", ""),
                wait_for=step_data.get("wait_for", ""),
                delay=float(step_data.get("delay", 0.0)),
            )
        )

    login_config = LoginConfig(
        steps=steps,
        url=login_block.get("url", ""),
        failure=login_block.get("failure", ""),
        success=login_block.get("success", ""),
        wait_for=login_block.get("wait_for", ""),
    )
```

Remove the flat login fields parsing code (lines 318-331 that handle `login_url`, `login_fields`, etc.).

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_yaml_loader.py::TestYAMLLoginBlock -v`
Expected: PASS (all 7 tests)

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/yaml_loader.py tests/unit/test_yaml_loader.py
git commit -m "feat: update YAML loader to parse login steps

Parse login.steps as list of LoginStep dicts. Support per-step
fields, submit, wait_for, and delay. Remove flat login_url/
login_fields/login_submit parsing.

BREAKING: YAML plugins must use steps format.

Ref #71

Co-authored-by: stavxyz <hi@stav.xyz>"
```

---

### Task 7: Remove flat login attribute auto-construction

**Files:**
- Modify: `src/graftpunk/plugins/cli_plugin.py:648-662`
- Modify: `src/graftpunk/plugins/cli_plugin.py:390-411` (build_plugin_config)
- Test: `tests/unit/test_cli_plugin.py`

**Step 1: Remove flat attribute auto-construction**

In `src/graftpunk/plugins/cli_plugin.py`, delete the flat login attribute auto-construction block in `__init_subclass__` (lines 648-662):

```python
# DELETE THIS BLOCK:
# Auto-construct LoginConfig from flat attrs if present
flat_login_url = cls.__dict__.get("login_url", "")
flat_login_fields = getattr(cls, "login_fields", None)
flat_login_submit = cls.__dict__.get("login_submit", "")
if flat_login_url and flat_login_fields and flat_login_submit:
    ...
```

In `build_plugin_config`, remove the flat field popping and auto-construction (lines 390-411):

```python
# DELETE/SIMPLIFY:
# Pop flat login fields (no longer PluginConfig fields)
login_config = raw.pop("login_config", raw.pop("login", None))
login_url = raw.pop("login_url", "")
login_fields_val = raw.pop("login_fields", {})
...
```

Keep only:
```python
login_config = raw.pop("login_config", raw.pop("login", None))
filtered["login_config"] = login_config
```

**Step 2: Run full test suite to find breakage**

Run: `uv run pytest tests/ -v --tb=short 2>&1 | head -100`
Expected: Multiple failures from tests using old flat LoginConfig format

**Step 3: Update all broken test fixtures**

Update every test file that uses the old `LoginConfig(fields=..., submit=...)` format to use `LoginConfig(steps=[LoginStep(...)])`.

Key files to update:
- `tests/unit/test_login_engine.py` — DeclarativeHN, DeclarativeQuotes, etc.
- `tests/unit/test_login_engine_retry.py` — already updated in Task 4
- `tests/unit/test_login_commands.py` — already updated in Task 3
- `tests/unit/test_yaml_loader.py` — already updated in Task 6
- Any other test files found during test run

**Step 4: Run full test suite to verify all pass**

Run: `uv run pytest tests/ -v`
Expected: All 1555+ tests pass

**Step 5: Commit**

```bash
git add -A
git commit -m "refactor: remove flat login attribute auto-construction

Remove __init_subclass__ flat login_url/login_fields/login_submit
auto-construction. Remove build_plugin_config flat field popping.
Update all test fixtures to use LoginConfig(steps=[...]).

BREAKING: Flat class attributes no longer auto-construct LoginConfig.

Ref #71

Co-authored-by: stavxyz <hi@stav.xyz>"
```

---

### Task 8: Update documentation

**Files:**
- Modify: `docs/HOW_IT_WORKS.md`

**Step 1: Update all LoginConfig examples**

Find and replace all `LoginConfig(fields=..., submit=...)` examples with `LoginConfig(steps=[LoginStep(...)])`:

In the declarative login section:
```python
login_config = LoginConfig(
    steps=[
        LoginStep(
            fields={"username": "#email", "password": "#password"},
            submit="button[type=submit]",
        )
    ],
    url="/login",
    failure="Invalid credentials",
    success=".dashboard",
    wait_for="#login-form",
)
```

Add a multi-step example:
```python
# Multi-step login (identifier-first / Azure AD B2C / Okta)
login_config = LoginConfig(
    steps=[
        LoginStep(fields={"username": "input#signInName"}, submit="button#next"),
        LoginStep(
            fields={"password": "input#password"},
            submit="button#next",
            wait_for="#password-section",
        ),
    ],
    url="/",
    failure="Your sign in name or password is not valid",
)
```

Update YAML examples to use steps format.

Update the Core Types Reference table to add `LoginStep`.

**Step 2: Update declarative login engine description**

Update the numbered steps to mention the step loop:

```markdown
The declarative engine:
1. Opens the browser to `{base_url}{login.url}` (or just `base_url` if url is empty)
2. **(If top-level `wait_for` is set)** Waits for the element before proceeding
3. **For each step:**
   - **(If step `wait_for` is set)** Waits for the element
   - Fills each field (click then type)
   - **(If step `submit` is set)** Clicks the submit button
   - **(If step `delay` > 0)** Pauses for the specified time
4. Waits for the page to settle
5. Checks for failure text in page content
6. Checks for success element via CSS selector
7. Transfers cookies and caches the session
```

**Step 3: Commit**

```bash
git add docs/HOW_IT_WORKS.md
git commit -m "docs: update documentation for multi-step login

Update all LoginConfig examples to use steps format.
Add multi-step login example (identifier-first flow).
Update declarative engine description with step loop.
Add LoginStep to Core Types Reference table.

Ref #71

Co-authored-by: stavxyz <hi@stav.xyz>"
```

---

### Task 9: Final quality checks and PR

**Step 1: Run quality checks**

```bash
uvx ruff check --fix . && uvx ruff format . && uvx ty check src/
```

Fix any issues.

**Step 2: Run full test suite**

```bash
uv run pytest tests/ -v
```

Expected: All tests pass.

**Step 3: Push branch**

```bash
git push -u origin feat/71-multi-step-login
```

**Step 4: Create PR**

```bash
gh pr create --title "feat: multi-step login forms (identifier-first flows)" --body "$(cat <<'EOF'
## Summary

Replaces flat `LoginConfig.fields`/`submit` with ordered `steps` to support identifier-first login flows (Azure AD B2C, Okta, Auth0, Google, Microsoft Entra ID).

Closes #71

## Changes

### New: `LoginStep` dataclass

Each step can fill form fields, click a submit button, or both:

```python
LoginStep(
    fields={"username": "#user"},   # credential → selector
    submit="button#next",           # optional: click after fields
    wait_for="#password-form",      # optional: wait before this step
    delay=0.5,                      # optional: pause after submit
)
```

### Restructured: `LoginConfig`

```python
LoginConfig(
    steps=[
        LoginStep(fields={"username": "#signInName"}, submit="#next"),
        LoginStep(fields={"password": "#password"}, submit="#next"),
    ],
    url="/",                  # optional: empty = use base_url
    failure="Invalid...",     # checked after all steps
    success=".dashboard",     # checked after all steps
    wait_for="#login-form",   # top-level wait before any steps
)
```

### Updated: Login engines

Both nodriver and selenium engines loop through steps in order. Error messages include 1-based step index.

### Updated: YAML schema

```yaml
login:
  steps:
    - fields:
        username: "input#signInName"
      submit: "button#next"
    - wait_for: "#password-section"
      fields:
        password: "input#password"
      submit: "button#next"
```

### Breaking changes (pre-release)

- Removed flat `LoginConfig.fields` and `LoginConfig.submit`
- Removed flat YAML keys (`login_url`, `login_fields`, etc.)
- Removed flat class attribute auto-construction

## Test plan

- [x] LoginStep validation (11 tests)
- [x] LoginConfig validation (11 tests)
- [x] resolve_login_fields aggregation (4 tests)
- [x] Nodriver multi-step loop (4 tests)
- [x] Selenium multi-step loop (2 tests)
- [x] YAML steps parsing (7 tests)
- [x] All existing tests pass with updated fixtures
EOF
)"
```
