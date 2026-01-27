# RFC-001: Browser Automation Stealth Architecture Evolution

**Status:** Draft
**Created:** 2026-01-26
**Authors:** @stavxyz

---

## Executive Summary

Modern anti-bot detection has evolved beyond what graftpunk's current stack (requestium + undetected-chromedriver + selenium-stealth) can reliably evade. Enterprise-protected sites now analyze 2,500+ signals per interaction across four detection layers: TLS/protocol fingerprinting, browser fingerprinting, behavioral analysis, and IP reputation.

This RFC proposes evolving graftpunk into a **configurable, multi-backend browser automation framework** with:

1. **Pluggable browser backends** - nodriver, Camoufox, Playwright, and legacy Selenium
2. **TLS-aware HTTP client** - curl_cffi integration for protocol-level fingerprint matching
3. **Behavioral simulation primitives** - Human-like mouse, keyboard, and navigation patterns
4. **Tiered installation profiles** - Minimal deps for simple sites, full stack for enterprise targets

The goal is **"batteries included, but swappable"** - opinionated defaults that work out of the box, with escape hatches for advanced users targeting high-security sites.

---

## Table of Contents

1. [Background and Motivation](#1-background-and-motivation)
2. [Current Architecture Analysis](#2-current-architecture-analysis)
3. [Detection Landscape (2026)](#3-detection-landscape-2026)
4. [Proposed Architecture](#4-proposed-architecture)
5. [Backend Implementations](#5-backend-implementations)
6. [HTTP Client Evolution](#6-http-client-evolution)
7. [Behavioral Simulation](#7-behavioral-simulation)
8. [Configuration and API Design](#8-configuration-and-api-design)
9. [Installation Profiles](#9-installation-profiles)
10. [Migration Strategy](#10-migration-strategy)
11. [Testing Strategy](#11-testing-strategy)
12. [Decision Log](#12-decision-log)
13. [Open Questions](#13-open-questions)
14. [References](#14-references)

---

## 1. Background and Motivation

### 1.1 graftpunk's Mission

graftpunk exists to "turn any website into an API" - enabling users to automate access to their own data on authenticated web services that don't provide official APIs. This requires:

- **Stealth browser automation** to pass anti-bot detection during login
- **Session persistence** to avoid repeated authentication
- **HTTP client capability** to make fast requests after browser-based auth

### 1.2 The Problem

Our current stack was state-of-the-art in 2023-2024 but is increasingly ineffective against modern detection:

```
Detection Evolution Timeline:
2020-2022: navigator.webdriver checks, basic fingerprinting
2023-2024: CDP detection, prototype chain inspection, behavioral analysis
2025-2026: TLS/JA4 fingerprinting, HTTP/2 analysis, multi-layer scoring
```

**Evidence of stack obsolescence:**

1. Chrome 143+ sets `navigator.webdriver` at the native Blink level *before* any JavaScript executes - our current approach explicitly acknowledges this limitation in `stealth.py:99-101`

2. Python's `requests` library has a well-known, easily-blocked JA3 fingerprint (`8d9f7747675e24454cd9b7ed35c58707`) - any site using TLS fingerprinting will block it

3. Selenium-based automation is fundamentally detectable via the WebDriver protocol communication pattern, regardless of JavaScript patches

### 1.3 Goals

- **G1:** Achieve >95% success rate against enterprise-protected sites (Cloudflare, DataDome, PerimeterX)
- **G2:** Maintain backward compatibility for existing users with simple targets
- **G3:** Provide clear upgrade path with tiered complexity
- **G4:** Keep core dependencies minimal; advanced features are opt-in

### 1.4 Non-Goals

- Building a general-purpose web scraping framework
- Competing with commercial anti-detect browsers (Multilogin, GoLogin)
- Supporting every browser automation library
- Providing CAPTCHA solving services

---

## 2. Current Architecture Analysis

### 2.1 Dependency Graph

```
graftpunk
├── Browser Automation
│   ├── requestium>=0.2.5          # Selenium + requests hybrid
│   ├── selenium>=4.0.0            # WebDriver protocol
│   ├── webdriver-manager>=4.0.0   # ChromeDriver auto-download
│   ├── undetected-chromedriver>=3.5.0  # Binary patching
│   └── selenium-stealth>=1.0.6    # JS property injection
├── Session Management
│   ├── cryptography>=42.0.0       # AES encryption
│   ├── dill>=0.3.0                # Enhanced pickling
│   └── httpie>=3.0.0              # Session export
└── [Optional Storage]
    ├── supabase>=2.10.0
    └── boto3>=1.34.0
```

### 2.2 Current Stealth Implementation

```python
# stealth.py - Current approach
def create_stealth_driver(headless: bool = False, profile_dir: Path | None = None):
    # 1. undetected-chromedriver options
    options = uc.ChromeOptions()
    options.add_argument("--disable-blink-features=AutomationControlled")

    # 2. Create undetected driver (patches ChromeDriver binary)
    driver = uc.Chrome(options=options, user_data_dir=str(profile_dir))

    # 3. Apply selenium-stealth (JS property injection)
    stealth(driver, languages=["en-US", "en"], vendor=webgl_vendor, ...)

    return driver
```

**Known Limitations (already documented in code):**

```python
# stealth.py:99-101
# Note: We do NOT inject CDP code to hide navigator.webdriver
# On Chrome 143+, navigator.webdriver is set at a level that JavaScript cannot override
```

### 2.3 Session Flow

```
┌─────────────────┐      ┌─────────────────┐      ┌─────────────────┐
│  BrowserSession │      │  Stealth Driver │      │    Requestium   │
│  (session.py)   │─────▶│  (stealth.py)   │─────▶│   (HTTP hybrid) │
└─────────────────┘      └─────────────────┘      └─────────────────┘
        │                                                  │
        ▼                                                  ▼
┌─────────────────┐                              ┌─────────────────┐
│  Cache/Encrypt  │                              │   Python        │
│  (cache.py)     │                              │   requests      │
└─────────────────┘                              │   (blocked JA3) │
                                                 └─────────────────┘
```

### 2.4 What Works Today

- Simple sites without enterprise protection (small banks, HR portals, etc.)
- Sites that rely primarily on cookie-based session validation
- Manual login with browser automation, followed by API requests

### 2.5 What Fails Today

- Sites with Cloudflare Turnstile/Interstitial
- Sites with DataDome or PerimeterX
- Sites performing TLS fingerprint validation
- Sites with sophisticated behavioral analysis
- Any site that blocks the `requests` library's JA3 fingerprint

---

## 3. Detection Landscape (2026)

### 3.1 Multi-Layer Detection Model

Modern anti-bot systems operate across four layers, each contributing to a risk score:

```
Layer 4: Behavioral Analysis     ┐
├── Mouse movement entropy       │
├── Scroll deceleration patterns │
├── Keystroke timing variation   │ Combined
└── Click event sequences        │ Risk
                                 │ Score
Layer 3: Browser Fingerprint     │
├── Canvas/WebGL hashes          │
├── Audio context fingerprint    │
├── Font enumeration             │
└── Cross-signal consistency     │
                                 │
Layer 2: Protocol Fingerprint    │
├── TLS JA3/JA4 hashes           │
├── HTTP/2 SETTINGS frames       │
├── Header ordering              │
└── Cipher suite selection       │
                                 │
Layer 1: Network/IP              │
├── IP reputation score          │
├── ASN type (datacenter/resi)   │
├── Geographic consistency       │
└── Request rate patterns        ┘
```

### 3.2 Why JavaScript Patches No Longer Work

**Problem 1: Timing Race Condition**
```javascript
// Detection script executes BEFORE stealth patches
// Chrome sets navigator.webdriver at native Blink level
console.log(navigator.webdriver);  // true (captured before patch runs)
```

**Problem 2: Prototype Chain Detection**
```javascript
// Even with direct property patched, prototype reveals truth
navigator.__proto__.webdriver  // returns true
Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver')
```

**Problem 3: CDP Serialization Detection**
```javascript
// Exploits how CDP's Runtime.consoleAPICalled serializes objects
var detected = false;
var e = new Error();
Object.defineProperty(e, 'stack', {
  get() { detected = true; }  // Only fires if CDP is active
});
console.log(e);
// detected === true means automation framework is present
```

### 3.3 TLS Fingerprinting Deep Dive

**JA3/JA4 Fingerprinting:**
- Extracts data from TLS ClientHello: version, cipher suites, extensions, curves
- Produces deterministic hash identifying client type
- Python `requests` library: `8d9f7747675e24454cd9b7ed35c58707` (well-known, blocked)
- Chrome 131: varies by platform but consistent per browser version

**HTTP/2 Fingerprinting:**
```
Chrome SETTINGS frame:     Python httpx:
INITIAL_WINDOW_SIZE: 6MB   INITIAL_WINDOW_SIZE: 64KB  ← Instant detection
HEADER_TABLE_SIZE: 65536   HEADER_TABLE_SIZE: 4096
MAX_CONCURRENT: 1000       MAX_CONCURRENT: 100
```

**Header Ordering:**
```
Browser: :method, :path, :authority, :scheme
Python:  :method, :authority, :scheme, :path  ← Different order = detection
```

### 3.4 Behavioral Analysis Techniques

**Mouse Movement Analysis:**
- Real humans: Bezier curves with micro-corrections, natural tremor
- Bots: Linear paths, teleportation, mechanical consistency
- hCaptcha uses Sigma-Lognormal decomposition to identify neuromotor signatures

**Typing Patterns:**
- Real humans: Log-normal distribution of inter-keystroke intervals
- Bots: Uniform delays, no typos, mechanical consistency

**Event Cascade Verification:**
```javascript
// Real click: mousemove → mousedown → (50-150ms) → mouseup → click
// Bot click: Just fires 'click' event
element.addEventListener('click', (e) => {
  if (!e.isTrusted || lastMousedownTime > 200) {
    flagAsBot();
  }
});
```

### 3.5 Detection Success Rates by Approach

| Approach | vs Basic Sites | vs Enterprise (CF/DD/PX) |
|----------|---------------|--------------------------|
| Current graftpunk stack | 90-95% | 40-60% |
| undetected-chromedriver alone | 85-90% | 30-50% |
| nodriver (CDP-direct) | 95%+ | 70-85% |
| Camoufox (C++ modified) | 95%+ | 90-95% |
| curl_cffi (TLS impersonation) | N/A (HTTP only) | 95%+ for API calls |

---

## 4. Proposed Architecture

### 4.1 Design Principles

1. **Abstraction over implementation** - Users interact with `graftpunk.Browser`, not specific backends
2. **Progressive complexity** - Simple use cases stay simple; advanced features are opt-in
3. **Fail-safe defaults** - Out-of-box experience works for common cases
4. **Explicit over implicit** - Users choose their stealth level; no magic detection
5. **Hybrid orchestration** - Browser for auth, curl_cffi for HTTP, seamless handoff

### 4.2 High-Level Architecture

```
                                 ┌─────────────────────────────────────┐
                                 │         graftpunk.Browser           │
                                 │      (Unified Abstraction Layer)    │
                                 └──────────────┬──────────────────────┘
                                                │
                    ┌───────────────────────────┼───────────────────────────┐
                    │                           │                           │
                    ▼                           ▼                           ▼
         ┌──────────────────┐       ┌──────────────────┐       ┌──────────────────┐
         │  NoDriverBackend │       │  CamoufoxBackend │       │  PlaywrightBack- │
         │  (Chrome, async) │       │  (Firefox, C++)  │       │  end (multi)     │
         └──────────────────┘       └──────────────────┘       └──────────────────┘
                    │                           │                           │
                    └───────────────────────────┼───────────────────────────┘
                                                │
                                                ▼
                                 ┌─────────────────────────────────────┐
                                 │       graftpunk.HttpClient          │
                                 │    (curl_cffi with TLS matching)    │
                                 └─────────────────────────────────────┘
                                                │
                                                ▼
                                 ┌─────────────────────────────────────┐
                                 │       Session Persistence           │
                                 │   (Existing cache/encrypt/storage)  │
                                 └─────────────────────────────────────┘
```

### 4.3 Backend Selection Matrix

| Backend | Stealth Level | Async | Browser | Best For |
|---------|--------------|-------|---------|----------|
| `legacy` (current) | Low | No | Chrome | Simple sites, backward compat |
| `nodriver` | High | Yes | Chrome | Most enterprise sites |
| `camoufox` | Highest | No | Firefox | Maximum security targets |
| `playwright` | Medium | Yes | Multi | Parallel sessions, testing |
| `drissionpage` | Medium-High | No | Chrome | Hybrid browser/HTTP |

### 4.4 Stealth Tiers

```python
class StealthTier(Enum):
    """Predefined stealth configurations."""

    MINIMAL = "minimal"      # Legacy stack, minimal deps
    STANDARD = "standard"    # nodriver + curl_cffi (recommended default)
    MAXIMUM = "maximum"      # Camoufox + full behavioral simulation
    CUSTOM = "custom"        # User-defined configuration
```

### 4.5 Core Abstractions

```python
# graftpunk/browser/base.py
from abc import ABC, abstractmethod
from typing import Protocol

class BrowserBackend(ABC):
    """Abstract base for browser automation backends."""

    @abstractmethod
    async def start(self, **options) -> "BrowserContext":
        """Start browser with given options."""

    @abstractmethod
    async def stop(self) -> None:
        """Clean shutdown of browser."""

    @abstractmethod
    def get_cookies(self) -> list[dict]:
        """Extract cookies for HTTP client handoff."""

    @abstractmethod
    def get_user_agent(self) -> str:
        """Get User-Agent for consistent fingerprinting."""


class BrowserContext(Protocol):
    """Protocol for browser page/context operations."""

    async def goto(self, url: str) -> None: ...
    async def fill(self, selector: str, value: str) -> None: ...
    async def click(self, selector: str) -> None: ...
    async def wait_for_url(self, pattern: str) -> None: ...
    async def screenshot(self, path: str) -> None: ...
```

---

## 5. Backend Implementations

### 5.1 NoDriver Backend (Recommended Default)

**Why nodriver:**
- Created by the author of undetected-chromedriver (natural successor)
- Direct CDP communication without WebDriver binary
- No `navigator.webdriver=true` because chromedriver never sets it
- CDP-minimal approach avoids `Runtime.enable` detection
- Async-first design

**Limitations:**
- Does NOT forge browser fingerprints (intentional design choice)
- Headless mode broken as of late 2025 (requires Xvfb)
- Chrome/Chromium only

**Implementation sketch:**

```python
# graftpunk/browser/backends/nodriver_backend.py
import nodriver as nd
from graftpunk.browser.base import BrowserBackend, BrowserContext

class NoDriverBackend(BrowserBackend):
    """Chrome automation via direct CDP (no WebDriver)."""

    def __init__(
        self,
        headless: bool = False,
        profile_dir: Path | None = None,
        proxy: str | None = None,
    ):
        self._headless = headless
        self._profile_dir = profile_dir
        self._proxy = proxy
        self._browser = None

    async def start(self, **options) -> BrowserContext:
        config = nd.Config()

        if self._profile_dir:
            config.user_data_dir = str(self._profile_dir)

        if self._proxy:
            config.proxy = self._proxy

        # Note: headless=True is broken in nodriver as of late 2025
        # Use Xvfb for server deployment instead
        if self._headless:
            LOG.warning("nodriver_headless_broken_using_xvfb_recommended")

        self._browser = await nd.start(config=config)
        return NoDriverContext(self._browser)

    async def stop(self) -> None:
        if self._browser:
            self._browser.stop()

    def get_cookies(self) -> list[dict]:
        return await self._browser.cookies.get_all()

    def get_user_agent(self) -> str:
        return await self._page.evaluate("navigator.userAgent")
```

### 5.2 Camoufox Backend (Maximum Stealth)

**Why Camoufox:**
- Modifies Firefox at C++ implementation level (not JavaScript injection)
- 0% detection on CreepJS, passes all major WAFs
- Built-in humanized mouse movement (`humanize=True`)
- Automatic timezone/locale from proxy IP (`geoip=True`)
- Properties modified at C++ level: navigator, screen, WebGL, WebRTC, canvas, audio, fonts

**Limitations:**
- Firefox only
- ~200MB memory footprint per instance
- Original maintainer hospitalized since March 2025; community fork available
- Requires fetching custom Firefox binary (~80MB)

**Implementation sketch:**

```python
# graftpunk/browser/backends/camoufox_backend.py
from camoufox.sync_api import Camoufox
from browserforge.fingerprints import Screen

class CamoufoxBackend(BrowserBackend):
    """Firefox automation with C++ level fingerprint spoofing."""

    def __init__(
        self,
        headless: bool = False,
        profile_dir: Path | None = None,
        proxy: dict | None = None,
        humanize: bool = True,
        os_target: str = "windows",  # windows, macos, linux
    ):
        self._headless = headless
        self._profile_dir = profile_dir
        self._proxy = proxy
        self._humanize = humanize
        self._os_target = os_target

    def start(self, **options) -> BrowserContext:
        screen = Screen(max_width=1920, max_height=1080)

        self._browser = Camoufox(
            humanize=self._humanize,
            os=self._os_target,
            geoip=True,  # Auto-detect timezone from proxy
            screen=screen,
            headless="virtual" if self._headless else False,
            persistent_context=True,
            user_data_dir=str(self._profile_dir) if self._profile_dir else None,
            proxy=self._proxy,
        )

        return CamoufoxContext(self._browser.new_page())
```

### 5.3 Playwright Backend (Flexibility)

**Why Playwright:**
- Multi-browser support (Chromium, Firefox, WebKit)
- Excellent async API with auto-waiting
- Well-maintained, Microsoft-backed
- Good for parallel sessions and testing
- 32% smaller containers, 30% faster cold starts vs Selenium

**Limitations:**
- Requires stealth plugin (playwright-stealth)
- Not as stealthy as nodriver/Camoufox for protected sites
- Detection possible via CDP serialization

**Implementation sketch:**

```python
# graftpunk/browser/backends/playwright_backend.py
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

class PlaywrightBackend(BrowserBackend):
    """Multi-browser automation via Playwright."""

    def __init__(
        self,
        browser_type: str = "chromium",  # chromium, firefox, webkit
        headless: bool = False,
        profile_dir: Path | None = None,
        proxy: dict | None = None,
    ):
        self._browser_type = browser_type
        self._headless = headless
        self._profile_dir = profile_dir
        self._proxy = proxy

    async def start(self, **options) -> BrowserContext:
        self._playwright = await async_playwright().start()

        browser_launcher = getattr(self._playwright, self._browser_type)

        launch_options = {
            "headless": self._headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }

        self._browser = await browser_launcher.launch(**launch_options)

        context_options = {
            "viewport": {"width": 1920, "height": 1080},
            "locale": "en-US",
        }

        if self._proxy:
            context_options["proxy"] = self._proxy

        if self._profile_dir:
            context_options["storage_state"] = str(self._profile_dir / "state.json")

        self._context = await self._browser.new_context(**context_options)
        page = await self._context.new_page()

        # Apply stealth patches
        await stealth_async(page)

        return PlaywrightContext(page, self._context)
```

### 5.4 Legacy Backend (Backward Compatibility)

**Purpose:** Maintain current behavior for users who don't need enterprise-level stealth.

```python
# graftpunk/browser/backends/legacy_backend.py
from graftpunk.stealth import create_stealth_driver

class LegacyBackend(BrowserBackend):
    """Current undetected-chromedriver + selenium-stealth stack."""

    def __init__(self, headless: bool = False, profile_dir: Path | None = None):
        self._headless = headless
        self._profile_dir = profile_dir

    def start(self, **options) -> BrowserContext:
        # Existing implementation
        driver = create_stealth_driver(
            headless=self._headless,
            profile_dir=self._profile_dir,
        )
        return LegacyContext(driver)
```

### 5.5 Future: graftpunk-browser (Custom Fork)

If ecosystem tools don't meet our needs, we're prepared to fork Camoufox and create a graftpunk-specific browser with stealth baked in at the C++ level.

**Potential improvements over upstream:**
- Tighter Python integration
- graftpunk-specific fingerprint profiles
- Integrated behavioral simulation
- Better session persistence primitives

**Prerequisites before forking:**
- Clear evidence that upstream Camoufox is unmaintained
- Specific features we can't achieve otherwise
- Commitment to ongoing maintenance

---

## 6. HTTP Client Evolution

### 6.1 The Problem with `requests`

Python's `requests` library has a well-known, easily-blocked TLS fingerprint:

```
JA3 Hash: 8d9f7747675e24454cd9b7ed35c58707
```

Any site using JA3/JA4 fingerprinting will block this immediately. Our current requestium-based approach inherits this vulnerability.

### 6.2 curl_cffi as the Solution

curl_cffi provides browser-impersonation at the TLS/HTTP level:

```python
from curl_cffi import requests

# Impersonates Chrome 131's TLS and HTTP/2 fingerprints
response = requests.get(
    "https://protected-site.com",
    impersonate="chrome131"
)
```

**Supported impersonation profiles:**
- Chrome 99-136
- Safari 153-260
- Firefox 133-135
- Edge 99-101
- Mobile variants (chrome131_android, safari184_ios)

**Verified match rate:** 99.8% JA3 match against real browsers

### 6.3 Hybrid Orchestration Pattern

The key insight: Use browser for JavaScript-heavy auth, then switch to curl_cffi for fast HTTP requests.

```python
# graftpunk/http/client.py
from curl_cffi import requests as curl_requests

class GraftpunkHttpClient:
    """HTTP client with TLS fingerprint impersonation."""

    def __init__(
        self,
        impersonate: str = "chrome131",
        cookies: list[dict] | None = None,
        user_agent: str | None = None,
    ):
        self._session = curl_requests.Session()
        self._session.impersonate = impersonate

        if cookies:
            for cookie in cookies:
                self._session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain", ""),
                )

        if user_agent:
            self._session.headers["User-Agent"] = user_agent

    def get(self, url: str, **kwargs) -> Response:
        return self._session.get(url, **kwargs)

    def post(self, url: str, **kwargs) -> Response:
        return self._session.post(url, **kwargs)
```

### 6.4 Browser-to-HTTP Handoff

```python
# Complete flow: Browser auth → HTTP client
async def authenticated_session(url: str, credentials: dict) -> GraftpunkHttpClient:
    # Phase 1: Browser authentication
    async with Browser(backend="nodriver") as browser:
        await browser.goto(url)
        await browser.fill("#username", credentials["user"])
        await browser.fill("#password", credentials["pass"])
        await browser.click("#submit")
        await browser.wait_for_url("**/dashboard**")

        # Extract session for HTTP client
        cookies = browser.get_cookies()
        user_agent = browser.get_user_agent()

    # Phase 2: Fast HTTP requests with browser session
    client = GraftpunkHttpClient(
        impersonate="chrome131",
        cookies=cookies,
        user_agent=user_agent,
    )

    return client
```

---

## 7. Behavioral Simulation

### 7.1 Why Behavioral Simulation Matters

Enterprise anti-bot systems analyze interaction patterns:

- **Mouse:** Entropy, acceleration curves, micro-tremor
- **Keyboard:** Inter-keystroke timing, typo patterns
- **Navigation:** Scroll patterns, dwell time, click sequences

Passing fingerprint checks but failing behavioral analysis still triggers CAPTCHA.

### 7.2 Mouse Movement Simulation

```python
# graftpunk/behavior/mouse.py
import numpy as np
from scipy import interpolate

def generate_human_path(
    start: tuple[int, int],
    end: tuple[int, int],
    control_points: int = 4,
    deviation: int = 15,
) -> list[tuple[int, int]]:
    """Generate realistic mouse path using B-spline interpolation."""

    x = np.linspace(start[0], end[0], num=control_points, dtype=int)
    y = np.linspace(start[1], end[1], num=control_points, dtype=int)

    # Add human imperfection (don't modify endpoints)
    for i in range(1, control_points - 1):
        x[i] += np.random.randint(-deviation, deviation)
        y[i] += np.random.randint(-deviation, deviation)

    # Create smooth curve
    degree = min(3, control_points - 1)
    tck, _ = interpolate.splprep([x, y], k=degree)

    # Generate path points based on distance
    distance = np.sqrt((end[0] - start[0])**2 + (end[1] - start[1])**2)
    num_points = max(10, int(distance / 30))
    u_new = np.linspace(0, 1, num=num_points)

    return list(zip(*interpolate.splev(u_new, tck)))
```

### 7.3 Typing Simulation

```python
# graftpunk/behavior/keyboard.py
import numpy as np

def human_typing_delay(wpm: int = 60, variance: float = 0.4) -> float:
    """Log-normal distributed typing delay (matches human patterns)."""
    cpm = wpm * 5  # Characters per minute
    base_delay = 60.0 / cpm

    delay = np.random.lognormal(np.log(base_delay), variance)
    return max(0.02, min(delay, 0.5))  # Clamp to realistic range


async def type_humanlike(
    page,
    selector: str,
    text: str,
    wpm: int = 60,
    error_rate: float = 0.02,
):
    """Type text with human-like timing and occasional typos."""
    element = await page.query_selector(selector)

    for char in text:
        # Occasional typo and correction
        if np.random.random() < error_rate:
            wrong_char = np.random.choice(list("asdfghjkl"))
            await element.type(wrong_char)
            await asyncio.sleep(human_typing_delay(wpm))
            await element.press("Backspace")
            await asyncio.sleep(human_typing_delay(wpm) * 1.5)  # Pause to "notice"

        await element.type(char)
        await asyncio.sleep(human_typing_delay(wpm))
```

### 7.4 Click Simulation

```python
# graftpunk/behavior/click.py

async def human_click(page, selector: str):
    """Click with proper event cascade and offset from center."""
    element = await page.query_selector(selector)
    box = await element.bounding_box()

    # Don't click exact center (humans don't)
    offset_x = np.random.randint(-box["width"] // 4, box["width"] // 4)
    offset_y = np.random.randint(-box["height"] // 4, box["height"] // 4)

    target_x = box["x"] + box["width"] / 2 + offset_x
    target_y = box["y"] + box["height"] / 2 + offset_y

    # Move mouse along human-like path
    current_pos = await page.evaluate("({x: window.mouseX || 0, y: window.mouseY || 0})")
    path = generate_human_path(
        (current_pos["x"], current_pos["y"]),
        (target_x, target_y),
    )

    for x, y in path:
        await page.mouse.move(x, y)
        await asyncio.sleep(np.random.uniform(0.01, 0.03))

    # Click with proper hold duration
    await page.mouse.down()
    await asyncio.sleep(np.random.uniform(0.05, 0.12))
    await page.mouse.up()
```

### 7.5 Session Warm-Up

```python
# graftpunk/behavior/warmup.py

async def warm_up_session(browser, target_url: str):
    """Establish browsing history before sensitive operations."""

    # 1. Start from search engine (establishes referrer)
    await browser.goto("https://www.google.com")
    await asyncio.sleep(np.random.uniform(2, 5))

    # 2. Visit target domain's public pages first
    base_url = extract_base_url(target_url)
    await browser.goto(base_url)

    # 3. Simulate reading (scroll, pause, mouse movement)
    for _ in range(np.random.randint(2, 5)):
        scroll_amount = np.random.randint(100, 400)
        await browser.evaluate(f"window.scrollBy(0, {scroll_amount})")
        await asyncio.sleep(np.random.uniform(1.5, 4))

    # 4. Navigate to login naturally
    await browser.goto(target_url)
    await asyncio.sleep(np.random.uniform(2, 4))
```

---

## 8. Configuration and API Design

### 8.1 Configuration Schema

```python
# graftpunk/config.py (extended)
from pydantic import BaseModel
from typing import Literal

class BrowserConfig(BaseModel):
    """Browser automation configuration."""

    backend: Literal["legacy", "nodriver", "camoufox", "playwright"] = "nodriver"
    headless: bool = False
    profile_dir: Path | None = None
    proxy: str | None = None

    # Behavioral simulation
    humanize: bool = True
    warm_up: bool = True

    # Camoufox-specific
    os_target: Literal["windows", "macos", "linux"] = "windows"

    # Playwright-specific
    browser_type: Literal["chromium", "firefox", "webkit"] = "chromium"


class HttpConfig(BaseModel):
    """HTTP client configuration."""

    impersonate: str = "chrome131"
    timeout: int = 30
    retries: int = 3


class StealthConfig(BaseModel):
    """Combined stealth configuration."""

    tier: Literal["minimal", "standard", "maximum", "custom"] = "standard"
    browser: BrowserConfig = BrowserConfig()
    http: HttpConfig = HttpConfig()
```

### 8.2 High-Level API

```python
# Simple usage (opinionated defaults)
from graftpunk import Browser, HttpClient

async with Browser() as browser:
    await browser.goto("https://site.com/login")
    await browser.fill("#user", "myuser")
    await browser.fill("#pass", "mypass")
    await browser.click("#submit")

    # Automatic handoff to HTTP client
    client = browser.to_http_client()

response = client.get("https://site.com/api/data")
```

```python
# Advanced usage (explicit configuration)
from graftpunk import Browser, StealthConfig

config = StealthConfig(
    tier="maximum",
    browser=BrowserConfig(
        backend="camoufox",
        proxy="http://user:pass@residential-proxy.com:8080",
        humanize=True,
    ),
    http=HttpConfig(
        impersonate="chrome131",
    ),
)

async with Browser(config=config) as browser:
    # Maximum stealth operations
    ...
```

### 8.3 Backward Compatibility

Existing code continues to work:

```python
# Old API (still works)
from graftpunk import BrowserSession, cache_session, load_session_for_api

session = BrowserSession(headless=False, use_stealth=True)
# ... login ...
cache_session(session, "mysite")

api = load_session_for_api("mysite")
response = api.get("https://site.com/api/data")
```

The `use_stealth=True` parameter maps to `backend="legacy"` internally.

---

## 9. Installation Profiles

### 9.1 Minimal (Current Behavior)

```bash
pip install graftpunk
```

**Dependencies:** Current stack (requestium, selenium, undetected-chromedriver, selenium-stealth)

**Use case:** Simple sites without enterprise protection

### 9.2 Standard (Recommended)

```bash
pip install graftpunk[standard]
```

**Additional dependencies:**
- nodriver
- curl_cffi

**Use case:** Most enterprise sites (Cloudflare, Akamai)

### 9.3 Maximum

```bash
pip install graftpunk[maximum]
```

**Additional dependencies:**
- camoufox[geoip]
- curl_cffi
- numpy, scipy (behavioral simulation)
- humancursor

**Use case:** High-security targets (DataDome, PerimeterX, financial sites)

### 9.4 Full

```bash
pip install graftpunk[all]
```

**Includes:** All backends, all storage options, development tools

### 9.5 pyproject.toml Updates

```toml
[project.optional-dependencies]
# Stealth tiers
standard = [
    "nodriver>=0.30",
    "curl_cffi>=0.6.0",
]
maximum = [
    "graftpunk[standard]",
    "camoufox[geoip]>=0.4",
    "numpy>=1.24",
    "scipy>=1.10",
    "humancursor>=1.0",
]

# Browser backends (individual)
nodriver = ["nodriver>=0.30"]
camoufox = ["camoufox[geoip]>=0.4"]
playwright = ["playwright>=1.40", "playwright-stealth>=1.0"]

# Combined
all = [
    "graftpunk[maximum,supabase,s3,jmespath,dev]",
    "graftpunk[playwright]",
]
```

---

## 10. Migration Strategy

### 10.1 Phase 0: Research & Planning (This RFC)

- [x] Document detection landscape
- [x] Evaluate alternative backends
- [x] Design abstraction layer
- [ ] Community feedback
- [ ] Finalize scope

### 10.2 Phase 1: Foundation

**Goal:** Introduce abstraction layer without breaking changes

- [ ] Create `graftpunk.browser` package with base abstractions
- [ ] Implement `LegacyBackend` wrapping current code
- [ ] Add `backend` parameter to `BrowserSession` (default: "legacy")
- [ ] Write comprehensive tests for abstraction layer

**Breaking changes:** None

### 10.3 Phase 2: NoDriver Integration

**Goal:** Add nodriver as recommended default

- [ ] Implement `NoDriverBackend`
- [ ] Add `graftpunk[standard]` installation profile
- [ ] Update documentation with backend selection guide
- [ ] Change default backend to "nodriver" (with deprecation notice for "legacy")

**Breaking changes:** Default behavior changes (with opt-out)

### 10.4 Phase 3: HTTP Client Evolution

**Goal:** Replace requests with curl_cffi for TLS fingerprint matching

- [ ] Create `GraftpunkHttpClient` with curl_cffi
- [ ] Implement browser-to-HTTP handoff
- [ ] Update `load_session_for_api()` to use new client
- [ ] Maintain backward compatibility layer for `requests` API

**Breaking changes:** Internal implementation (API compatible)

### 10.5 Phase 4: Camoufox Integration

**Goal:** Add maximum stealth option

- [ ] Implement `CamoufoxBackend`
- [ ] Add behavioral simulation primitives
- [ ] Create `graftpunk[maximum]` installation profile
- [ ] Document Firefox-specific considerations

**Breaking changes:** None (additive)

### 10.6 Phase 5: Behavioral Simulation

**Goal:** Human-like interaction primitives

- [ ] Implement mouse path generation
- [ ] Implement typing simulation
- [ ] Implement session warm-up
- [ ] Integrate with all backends

**Breaking changes:** None (additive)

### 10.7 Phase 6: Playwright Integration (Optional)

**Goal:** Multi-browser support for parallel sessions

- [ ] Implement `PlaywrightBackend`
- [ ] Add `graftpunk[playwright]` installation profile
- [ ] Document use cases (parallel sessions, testing)

**Breaking changes:** None (additive)

### 10.8 Phase 7: Custom Browser (If Needed)

**Goal:** Fork Camoufox if ecosystem doesn't meet needs

- [ ] Evaluate upstream Camoufox maintenance status
- [ ] Identify specific gaps we need to address
- [ ] Create graftpunk-browser fork
- [ ] Maintain alongside upstream

**Prerequisites:** Clear justification, maintenance commitment

---

## 11. Testing Strategy

### 11.1 Unit Tests

- Backend abstraction compliance
- Cookie extraction and handoff
- Behavioral simulation algorithms
- Configuration validation

### 11.2 Integration Tests

- Each backend against detection test sites
- Browser-to-HTTP handoff flow
- Session persistence across backends

### 11.3 Detection Test Sites

| Site | Tests | Expected Result |
|------|-------|-----------------|
| bot.sannysoft.com | Basic fingerprint | Pass (baseline) |
| nowsecure.nl | Cloudflare | Pass with nodriver+ |
| creepjs.com | Advanced fingerprint | Pass with Camoufox |
| browserleaks.com | TLS fingerprint | Pass with curl_cffi |

### 11.4 CI/CD Considerations

- Detection tests are flaky by nature (sites update)
- Run detection tests in separate workflow (not blocking)
- Maintain known-good baselines for comparison

---

## 12. Decision Log

| Decision | Rationale | Date |
|----------|-----------|------|
| Abstraction layer over backends | Allows progressive adoption, backward compat | 2026-01-26 |
| NoDriver as default (Phase 2) | Best balance of stealth and ease of use | 2026-01-26 |
| curl_cffi for HTTP | 99.8% JA3 match, drop-in requests replacement | 2026-01-26 |
| Keep legacy backend | Backward compatibility, simple use cases | 2026-01-26 |
| Willing to fork Camoufox | Insurance if ecosystem stagnates | 2026-01-26 |

---

## 13. Open Questions

1. **Async vs Sync API:** NoDriver and Playwright are async-first. Should graftpunk's primary API be async, with sync wrappers?

2. **Default backend timing:** When should we switch default from "legacy" to "nodriver"? v2.0? After 6 months deprecation?

3. **Camoufox maintenance:** Original maintainer hospitalized since March 2025. Should we adopt community fork or wait?

4. **Proxy integration:** Should graftpunk provide proxy management (rotation, health checks) or leave to users?

5. **MFA handling:** Current TOTP support works. Should we expand MFA capabilities (magic link, SMS)?

6. **Metrics/telemetry:** Should we add opt-in success rate tracking to inform backend selection?

---

## 14. References

### 14.1 Tools and Libraries

- [nodriver](https://github.com/ultrafunkamsterdam/nodriver) - CDP-direct Chrome automation
- [Camoufox](https://github.com/daijro/camoufox) - C++ modified Firefox
- [curl_cffi](https://github.com/yifeikong/curl_cffi) - TLS fingerprint impersonation
- [DrissionPage](https://github.com/g1879/DrissionPage) - Hybrid browser/HTTP
- [Playwright](https://playwright.dev/) - Multi-browser automation
- [HumanCursor](https://github.com/riflosnake/HumanCursor) - Mouse movement simulation

### 14.2 Detection Research

- [Castle.io: From Puppeteer stealth to Nodriver](https://blog.castle.io/from-puppeteer-stealth-to-nodriver-how-anti-detect-frameworks-evolved-to-evade-bot-detection/)
- [DataDome: How New Headless Chrome & CDP Signal Impact Bot Detection](https://datadome.co/threat-research/how-new-headless-chrome-the-cdp-signal-are-impacting-bot-detection/)

### 14.3 Fingerprint Testing

- TLS/JA3: https://tls.browserleaks.com/json
- HTTP/2: https://tls.peet.ws/api/all
- Bot detection: https://bot.sannysoft.com/
- Cloudflare: https://nowsecure.nl

---

## Appendix A: Current vs Proposed Dependency Comparison

### Current (graftpunk 1.1.0)

```
requestium>=0.2.5
selenium>=4.0.0
webdriver-manager>=4.0.0
undetected-chromedriver>=3.5.0
selenium-stealth>=1.0.6
```

### Proposed (graftpunk 2.0 standard)

```
# Core (always installed)
cryptography>=42.0.0
dill>=0.3.0
pydantic-settings>=2.0.0
typer>=0.9.0
rich>=13.0.0
structlog>=23.0.0

# Standard stealth (graftpunk[standard])
nodriver>=0.30
curl_cffi>=0.6.0

# Maximum stealth (graftpunk[maximum])
camoufox[geoip]>=0.4
numpy>=1.24
scipy>=1.10
humancursor>=1.0

# Legacy (backward compat, optional)
requestium>=0.2.5
selenium>=4.0.0
undetected-chromedriver>=3.5.0
selenium-stealth>=1.0.6
```

---

## Appendix B: Detection Signal Reference

### Signals graftpunk Currently Handles

| Signal | Current Approach | Effectiveness (2026) |
|--------|-----------------|---------------------|
| navigator.webdriver | selenium-stealth patch | Low (Chrome 143+ defeats) |
| ChromeDriver binary | undetected-chromedriver patch | Medium |
| WebGL vendor/renderer | Platform fingerprinting | Medium |
| Window size | 1920x1080 default | High |
| Profile persistence | Chrome user data dir | High |

### Signals graftpunk Should Handle

| Signal | Proposed Approach | Backend Required |
|--------|------------------|------------------|
| TLS fingerprint (JA3/JA4) | curl_cffi impersonation | standard+ |
| HTTP/2 SETTINGS | curl_cffi | standard+ |
| CDP detection | nodriver (no Runtime.enable) | standard+ |
| Canvas fingerprint | Camoufox C++ spoofing | maximum |
| Mouse movement | Behavioral simulation | maximum |
| Typing patterns | Log-normal delays | maximum |

---

*This RFC is a living document. Updates will be tracked via git history.*
