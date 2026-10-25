# How graftpunk Works

This document explains the internals of graftpunk: browser automation, session management, the plugin system, observability, and the CLI.

---

## Architecture Overview

graftpunk captures authenticated browser sessions and exposes them as CLI commands and a Python API. The flow is:

1. **Login** — A browser opens, you log in (manually or via automation), cookies are captured.
2. **Cache** — The session (cookies + headers) is encrypted and stored locally.
3. **Use** — CLI commands or `GraftpunkClient` load the cached session into a plain `requests.Session` and make API calls without a browser.

Plugins define both the login flow and the commands available for a site. The same plugins work with both the CLI (`gp mysite cmd`) and the Python API (`GraftpunkClient("mysite").cmd()`); multi-word commands are `gp mysite by-parcel` on the CLI and `client.by_parcel()` / `client.execute("by-parcel")` in Python — the client accepts either spelling.

---

## Browser Backends

graftpunk supports two browser automation backends, selectable per plugin via the `backend` attribute (typed as `Literal["selenium", "nodriver"]`).

### Selenium (`backend = "selenium"`)

Uses [undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver) with [selenium-stealth](https://github.com/diprajpatra/selenium-stealth) for anti-detection. This is the default backend.

- Sync API (`driver.get()`, `driver.find_element()`)
- Screenshot support via `driver.get_screenshot_as_png()`
- Console and performance log collection via `driver.get_log()`
- Stealth mode enabled by default (`use_stealth=True`)
- Exceptions use `selenium.common.exceptions.WebDriverException` as the base class

### NoDriver (`backend = "nodriver"`)

Uses [nodriver](https://github.com/ultrafunkamsterdam/nodriver), a direct Chrome DevTools Protocol client. Better anti-detection than Selenium for sites with aggressive bot protection.

- Async API (`await tab.get()`, `await tab.select()`)
- Requires `await session.start_async()` before use
- No synchronous screenshot support (returns `None` from sync capture)
- Cookie transfer via `await session.transfer_nodriver_cookies_to_session()`
- `tab.select(selector)` returns `None` on timeout — it does **not** raise an exception. Code that checks element presence should test the return value, not catch exceptions.

**Event loops.** nodriver's browser, tabs, and websocket work only on the event loop that started the browser. A synchronous `NoDriverBackend.start()` creates one private loop in a daemon thread named `graftpunk-nodriver-loop` and runs every later synchronous call there; `stop()` stops that loop and joins its thread. `start_async()` starts the browser on the caller's loop instead, which the backend uses from other threads and never stops. On a backend already started, `start_async()` does nothing while the browser's loop is still running (or is the current loop); when that loop has finished, it ends the old Chrome and starts a new one. Inside the browser's own loop, where waiting on that loop would deadlock, a synchronous `start()` raises `RuntimeError`, `navigate()` raises `BrowserError`, and the other synchronous methods log a warning and return their fallback (`''`, `[]`, `0`, `False`); async code awaits nodriver's objects and the backend's `stop_async()`, which `async with BrowserSession(...)` does on exit. `stop()` is the exception: inside the browser's loop it ends Chrome without the loop (SIGTERM, then SIGKILL if needed), blocking the loop until Chrome exits, and logs `nodriver_backend_stop_in_own_loop` at warning level. The same loop-free end runs when the browser's loop has finished, and when a start or stop is interrupted; from a coroutine it runs on a worker thread. Chrome's subprocess transport is closed on its loop's thread once that loop has collected Chrome's exit status, or about 4 seconds after the loop first tries if it never does. That needs the loop to run again after the stop, which `asyncio.run()` does as it shuts down. A hand-managed loop closed right after the stop without running again drops the scheduled close, and the close then runs as the loop drops it, without the loop: asyncio's own close needs the loop for its pipes' last step, so graftpunk closes the pipes' file descriptors directly and reads Chrome's exit status itself (on Linux the loop's child watcher can no longer do that; on macOS its thread already has). Exit then prints no `Exception ignored in: BaseSubprocessTransport.__del__ ... Event loop is closed`, and with warnings enabled no `unclosed transport` or `subprocess ... is still running` ResourceWarning.

**Call timeouts.** nodriver 0.50.6 puts no limit on a CDP call: `Connection.send` awaits Chrome's reply with no timeout, and its websocket's ping timeout is 15 minutes. `Browser.get` sends `Page.navigate` and awaits the reply, which Chrome sends once the navigation has its response or has failed, without waiting for the page's `load` event. A Chrome that stops answering without closing its websocket would therefore block a synchronous call indefinitely, so each synchronous call waits for the browser's loop with a limit: `navigate_timeout` (default 120 seconds) for `navigate()`, which waits on the server, and `call_timeout` (default 60 seconds) for `current_url`, `page_title`, `page_source`, `get_cookies()`, `set_cookies()`, `delete_all_cookies()`, and `get_user_agent()`. Both are `NoDriverBackend` arguments that `BrowserSession(backend="nodriver", ...)` passes through, and `None` turns a limit off. When a limit passes, graftpunk cancels the call on the browser's loop and waits for the cancellation to land (at most about 10 seconds more), and the method fails the way it fails on any CDP error: `navigate()` raises `BrowserError`, and the others log their usual warning, whose `error` reads `NoDriverBackend.<method> did not finish within <n> s`, and return their fallback. A call that finishes just as its limit passes is not reported as timed out; it returns its result. The loop keeps running, so the next call works.

Cancelling ends graftpunk's wait, not a command Chrome has already received. For `navigate()` that matters: Chrome keeps loading the URL, and while it does the tab answers no `Runtime.evaluate`, so `current_url`, `page_title`, and `page_source` would each wait out their own limit, and the tab would then land on the URL `navigate()` reported as failed. A `navigate()` that times out therefore sends `Page.stopLoading` to the tab it was navigating, waits up to 2 seconds for Chrome's answer, and then raises. The tab stays on the page it showed before (`about:blank` right after `start()`) and answers at once. A Chrome too frozen to answer within those 2 seconds receives the command when it resumes, with the same result. When the stop fails, or gets no answer within its 2 seconds, graftpunk logs `nodriver_backend_stop_loading_failed`. `start()`, `stop()`, and `stop_async()` are not limited (a start retries its connection and a stop bounds its own waits), and neither is async code that awaits nodriver's objects directly.

`start()`, `start_async()`, `stop()`, and `stop_async()` share a per-backend lock, so two threads starting one backend launch one Chrome whichever start each uses, and a stop that races another thread's start stops the browser that start launched. `start_async()` and `stop_async()` wait for the lock by polling it, so their event loop keeps running meanwhile. A `stop()` inside the browser's own loop skips the lock, because a start on another thread may hold it while waiting on that loop. The lock is not reentrant. A coroutine whose own event loop holds it through another task's `start_async()` waits for that task, since it can finish meanwhile: a second `start_async()` (two under `asyncio.gather`, say) then finds the backend started, and a `stop_async()` stops the browser the start launched. A `start_async()` waits the same way for a `stop_async()` on its loop and then starts. Otherwise no call waits for the lock while its own thread holds it. While a `stop_async()` holds it partway through (paused at an await, or suspended on a loop that stopped), a `stop()` or `stop_async()` on the same thread stops the browser itself without the lock (through the browser's loop while that loop runs, and without it otherwise), and a `start()` there raises `RuntimeError`, as does a `start_async()` on another loop of that thread, which the holder cannot finish on; the paused `stop_async()`, if it resumes, finds the backend stopped. If that `stop_async()` is already ending Chrome without the browser's loop (it was cancelled, or that loop had stopped), the second stop finds nothing left to stop and returns before Chrome has exited (up to about 4 seconds before), and the paused `stop_async()` finishes ending Chrome. Either way the private loop is closed only after Chrome has exited or the stop has given up on it, since on Linux that loop's child watcher is what collects Chrome's exit status. While a `start_async()` holds the lock, a `start()` on its thread raises, and a `stop()` on its thread, or a `stop_async()` on another loop of that thread, finds the backend not started yet and returns, after which the start finishes and leaves the backend started; `await stop_async()` on the start's own loop waits for it instead.

A `start_async()` or `stop_async()` that never resumes (its task left on a loop that stopped and never runs again) would keep the lock for good. Elapsed time cannot tell that holder from a slow one, so a call on another thread watches the holder's event loop while it waits. While that loop runs, the holder can finish, and the call waits however long it takes; a `start_async()` whose Chrome never answers its first CDP call is waited for without a limit, as a synchronous `start()` is. Once the loop is closed, the holder can never finish, and the call gives up at once; a loop that has stopped but is not closed (a hand-managed loop between `run_until_complete()` calls) may run again, so the call gives up only after it has not run for 60 seconds straight. A loop run in bursts shorter than the call's 50 ms poll still counts as running. A call on the holder's own running loop does not give up on it while it has not returned, since the holder runs whenever that call polls. On giving up, the call logs `nodriver_backend_lock_holder_abandoned` at warning level. The first `stop()` or `stop_async()` to give up on a hold ends Chrome without the browser's loop, as above, and without the lock, which the holder still has; a `start()` or `start_async()` raises `RuntimeError` naming the holder, since the backend cannot start again until that holder returns, and ends nothing, so a stop that gives up after it still ends Chrome. Later calls give up on the same hold without waiting out the 60 seconds again. A later stop waits (up to 20 seconds) for the first one's end of Chrome and returns. The lock stays held until that end is done: a holder whose loop runs again during the end returns without releasing the lock, and the end releases it once Chrome has exited. A call that comes in during the end, on any thread or loop, including the holder's own once the holder has returned, waits for it (up to 20 seconds); a start then starts if the lock has come free, rather than launch a Chrome whose loop the end would close and which it would forget, and raises if the holder has still not returned. A `stop_async()` runs that end, including the lock's release, on a worker thread, as `stop()` runs it on its own thread, and hands it to that thread before its own event loop can run again, so the end finishes even if that loop stops meanwhile, in the same iteration or later. When the loop's default executor cannot take the work (it was shut down, as at the end of `asyncio.run()` or at interpreter exit), the end runs on a thread of its own, and when no thread can be started, on the loop itself, blocking it until Chrome has exited. The exception is a `stop_async()` awaited on the browser's private loop, which runs the end on that loop, since a worker closing the loop would wait for the loop's thread while that thread waits for the worker; that loop keeps running on its own thread until the end stops it. One limit remains: an end that has not finished after 20 seconds (Chrome's exit and the private loop's join are each bounded well under that, so it takes something like a removal of Chrome's profile that hangs) keeps the lock held until it finishes. Meanwhile a start raises `RuntimeError`, saying the end is outstanding once the holder has returned, and a stop returns after 20 seconds and logs `nodriver_backend_given_up_end_still_running`. If the holder's loop does run again, a `start_async()` notices that it was given up on, ends whatever it launched and raises `RuntimeError` instead of marking the backend started, and a `stop_async()` finds the backend stopped. A synchronous `start()` or `stop()` that holds the lock is waited for without a limit, since its thread is running the start or stop. The 60 seconds is not configurable.

### BrowserSession

`BrowserSession` (in `src/graftpunk/session.py`) wraps both backends with a unified interface. It extends `requestium.Session`, which itself extends `requests.Session`.

```python
session = BrowserSession(
    backend="selenium",  # or "nodriver"
    headless=False,  # show browser window
    use_stealth=True,  # anti-detection; selenium requires True, nodriver ignores it
    observe_mode="off",  # "off" or "full"
)
```

**Driver access:** The `session.driver` property raises `BrowserError` if no browser has been started. Callers that need the driver optionally should catch `BrowserError` rather than using `getattr()`.

**Context manager protocol:**

```python
# Selenium (sync)
with BrowserSession(backend="selenium") as session:
    session.driver.get("https://example.com")
    # On exit: captures observability data, quits browser

# NoDriver (async)
async with BrowserSession(backend="nodriver") as session:
    tab = await session.driver.get("https://example.com")
    # On exit: captures observability data, stops browser
```

The context manager handles:
- Starting observability capture on enter (skipped gracefully with a warning if no driver is available, and dropped with a warning if the capture fails to start); see [Modes](#modes) for what each backend records
- Flushing observability data (screenshots, HAR, console logs) on exit — each flush step is isolated so one failure doesn't prevent others
- Browser cleanup (quit/stop) on exit

**Serialization:** `BrowserSession` implements `__getstate__` to strip browser-related state (driver handles, async loops) before pickling. The nodriver backend serializes only HTTP client state (cookies, headers, session name) — browser driver handles are excluded; the selenium backend delegates to `requests.Session.__getstate__`. Both backends preserve the session's rider attributes through the pickle roundtrip — the token cache, the browser header roles and the account identifier — from one registry (`graftpunk.tokens.SESSION_RIDER_ATTRS`); CSRF tokens are deliberately excluded, being per-request and stale on restore.

### Browser process hygiene

The nodriver backend leaves a Chrome subprocess behind whenever the Python process that launched it dies without running its cleanup: SIGKILL, an OOM kill, a closed terminal, a crash. graftpunk handles that in four places.

**A marker on every browser.** Each of the three launch sites (the `NoDriverBackend`, `gp observe`, and browser token extraction) passes `--graftpunk-owner-pid=<pid>` in `browser_args`, beside the `--test-type` switch graftpunk has always passed. Chrome ignores switches it does not recognise, and the marker survives into the command line that `ps` reports, so a later run can tell graftpunk's own browsers apart from the user's and knows which process owned each one. It is on the browser process only: Chrome copies an allowlisted subset of switches to its renderer and GPU helpers, and a vendor switch is not on that list. Ending the browser process is what ends its helpers. A `NoDriverBackend` start also passes `--graftpunk-launch-id=<random hex>`, one value per start, which tells the Chrome that start launched apart from one that another start launched at the same moment (see the profile paragraph below).

**A sweep before each start.** Before `nodriver.start`, all three launch sites scan `ps` for processes carrying `--remote-debugging-port`. One whose marker names a pid that is no longer alive is ended with SIGTERM, then SIGKILL after a three second grace period. A narrower second rule covers browsers launched before the marker existed: no marker, a nodriver temp profile as its `--user-data-dir`, and a parent of pid 1. A browser without a debugging port, and any browser whose owner is still alive, is never touched. The same pass then deletes nodriver temp profiles that no live browser names in its `--user-data-dir` and that are more than an hour old. Set `GRAFTPUNK_KEEP_ORPHANED_CHROME=1` to skip the start-time sweep. The SIGTERM and SIGHUP handlers and the per-stop profile removal are unaffected.

**A profile deleted on every stop.** nodriver creates its temp profile with `tempfile.mkdtemp(prefix="uc_")` and deletes it only from an `atexit` handler that iterates its registry of live browsers. graftpunk removes its browser from that registry on stop, to keep that handler's output off stdout, so nothing used to delete the directory and every session leaked one. All three stop paths now delete it themselves, and so does a `NoDriverBackend` start attempt that fails after Chrome is already up. A `NoDriverBackend` start that fails or is interrupted inside `nodriver.start` (a failed connection, Ctrl-C, a cancelled `start_async()`) never receives its browser, so it looks in nodriver's registry for the Chrome carrying its own `--graftpunk-launch-id` value, ends that one, and removes its profile; a browser whose arguments it cannot read is left alone. A stop or failed start that cannot use the browser's event loop ends Chrome with SIGTERM (through the subprocess handle on Windows), sends SIGKILL if Chrome has not exited after three seconds, and removes the profile only once Chrome has exited. A custom `profile_dir` is never touched: nodriver's own `uses_custom_data_dir` flag says which is which, and only a directory sitting directly under the temp directory is ever removed.

**Signals.** A live browser registers a handle with `graftpunk.signals`, and the SIGTERM and SIGHUP handlers terminate every registered browser, restore the default disposition, and re-raise the signal, so the exit status stays the conventional 128 + signum. The handler sends one SIGTERM per browser and does nothing else, because it runs while the process is on its way out: a browser ended that way leaves its temp profile behind, and the next launch's sweep collects it. A slot a host program already handles is left alone. Importing graftpunk installs nothing, and neither does running a `gp` command that opens no browser: the CLI sets `graftpunk.signals.auto_install`, and `graftpunk.browser_launch.arm_termination_handlers()` arms the handlers on the main thread at the first browser launch, before the orphan sweep runs on a worker thread. A library consumer opts in by calling `install_termination_cleanup()` (`from graftpunk.signals import install_termination_cleanup`) or by setting the same flag. SIGKILL and the OOM killer cannot be caught, which is what the start-time sweep is for.

Chrome shares the Python process group. nodriver launches it with `asyncio.create_subprocess_exec` and passes no `start_new_session`, so the child inherits its parent's session and process group: a terminal's SIGHUP and `kill -- -<pgid>` reach Chrome without any process-group handling in graftpunk.

---

## Session Management

### Caching

When a login completes, the session is cached:

```python
from graftpunk import cache_session

cache_session(session, "mysite")
```

This:
1. Serializes the session with `dill` (preserves cookies, headers, browser state)
2. Computes a SHA256 checksum
3. Encrypts with Fernet (AES-128-CBC + HMAC-SHA256)
4. Stores encrypted data + metadata via the storage backend

Sessions have a configurable TTL (`GRAFTPUNK_SESSION_TTL_HOURS`). Metadata tracks domain, cookie count, cookie domains, status, and the `account_identifier` recorded at login (see Multi-Account Sessions).

An HTTPie-compatible session file can also be exported alongside the cache, falling back gracefully (with a warning log) if cookie extraction fails due to browser state issues.

### Multi-Account Sessions

Sessions are keyed by plugin **and** account: `gp myshop login` as alice caches
`myshop@alice`; a second login as bob caches `myshop@bob` beside it, and logging
in never evicts another account's session. The label is the slugified value of
an account identifier field. A field is one when its name, lower-cased with
`_`, `-`, `.` and spaces removed, is exactly one of `username`, `email`,
`emailaddress`, `useremail`, `loginemail`, `login`, `loginid`, `loginname`,
`identifier`, `user` or `userid` (`graftpunk.session_identity.is_identifier_field`,
the allowlist observe redaction uses, which also holds `account` and
`accountid`). Among those fields, the label comes from the first whose name,
lower-cased with its separators kept, contains `username`, then `email`,
`login`, `identifier` and `user`, and within one of those words the first
declared: the order 1.24.0 used, so a label that came from an identifier field
is unchanged (`{user_name, email}` labels from `email`, since `user_name` does
not contain `username` as written). A field that contains none of those words
as written (`account`, `account_id`, `e_mail`) gives no label, as before. Any
other field is never derived from, so a PIN, a one-time code or a password
cannot name a session. When no field qualifies the session keeps its bare
name; use `--as` to name it.
graftpunk 1.24.0 and earlier took any field holding one of those words that
passed a deny-list, and could label a session with a secret (`userPin` gave
`myshop@4417`, #293). Slots written by the release that fixed #293 and later
record `account_rule: 2` in their metadata, so a slot without it is one that
rule may have named. At the next login of the same credentials, gp finds a
slot named after a static secret (a PIN, a password). When the bare name is
free and no other slot of the plugin was named that way, it copies the slot
there before the login, reading the copy back before removing the old slot,
and removing a copy that fails; otherwise it removes the old slot once the
login has written the new name. Its observe runs follow, and a `.gp-session`
in the working directory that pinned it is rewritten. A slot named after a
one-time code cannot be found that way and is left; the login counts such
slots, and the "Several sessions cached" error (and `gp http`) shows them as
`<plugin>@<earlier label>`. A 1.24.0 slot under a `--as` label recorded the
secret as its account; a successful login with that label replaces the record
in the metadata alone, so an expired slot is handled too, and a later refresh
takes the account from that metadata. The login never prints the old name or
the recorded secret, and the storage calls that read or remove such a slot run
under `graftpunk.logging.redacted_values`, so their log lines show the
placeholder.
`gp myshop login --as work` pins a label. Metadata records the
identifier (what was typed at login, not server-verified), shown in
`gp session list`'s Account column.

Which session a command uses is resolved once per invocation:
`--session` > `GRAFTPUNK_SESSION` > `.gp-session` in the working directory >
the plugin's cached sessions (exactly one → used; several → the command refuses
and lists them — never "most recent wins"). The env and file tiers apply only to
their own plugin: a pin for another plugin's base name (`othersite@bob` while
running `gp myshop ...`) is ignored and resolution continues, so one pinned
shell does not misdirect every other site. An explicit `--session` is not
base-scoped — it always wins outright. Both pins are validated before anything
is listed or loaded, so a malformed name is a loud error, not a lookup miss.
`GraftpunkClient` resolves the same way but deliberately ignores
the ambient env/file tiers; pass `GraftpunkClient("site", session="myshop@alice")`
to pin. It resolves on **first session use**, not at construction: building a
client performs no session-storage I/O and never raises for session *state*
reasons, so `AmbiguousSessionError` (several accounts cached, unpinned),
`SessionNotFoundError`, `SessionExpiredError` and the browser-extra
`ImportError` all surface from the first `execute()` — catch
`AmbiguousSessionError` next to `SessionNotFoundError` in whatever wraps your
command calls. Two things still happen at construction: plugin discovery
(`PluginError`), and validation of the pin string — a malformed pin
(`session="MyShop@Alice"`) is rejected there with `ValueError`, since that
costs no I/O. A labelled pin never lists at all, hit or miss.

**The pin contract.** A *bare* pin (`--session myshop`, `GRAFTPUNK_SESSION=myshop`,
`GraftpunkClient(..., session="myshop")`) names a **base**, not a slot, and is
resolved when the session is loaded: a slot cached under the bare name itself
wins (an exact hit — nothing is listed), otherwise the single labelled account
under that base, otherwise `AmbiguousSessionError` naming the candidates. A
*labelled* pin (`myshop@alice`) is exact — a miss is a miss, never another
account's session. Whichever slot the load lands on is the operating name for
the rest of that invocation: every write-back keys off the name the load
resolved, not the name that was typed, so a refresh can never fork into another
slot (or vanish into one that does not exist).

Inside a plugin command handler, `self.get_session()` follows the resolution
the CLI (or `GraftpunkClient`) already performed for that invocation, so it
loads the slot the command is pinned to rather than resolving the plugin's bare
base name a second time. It still returns a second `requests.Session`, not
`ctx.session`; what it no longer does is disagree with the pin. Outside a
command (a script, a test, library code that already holds a name), pass the
name: `plugin.get_session("myshop@alice")` loads that slot exactly, and
`plugin.get_session("myshop")` resolves the base.

A `requires_session=False` command carries no operating session scope, so
`self.get_session()` in its handler resolves the plugin's bare base name;
pass an explicit name there when the handler needs one particular account.

The session-management commands (`gp session show`, `gp session clear`, `gp
session use`) and `gp http --session` apply a narrower rule for a registered
plugin's base name: they address the exact slot cached under that base when
one exists, and resolve accounts only when it does not. An unpinned plugin
command applies the general pin contract above and refuses when several
candidates share a base, even when one of them is cached under the bare base
name itself; that surface writes the session back on every command, and
refusing forces the one-time migration described in the CHANGELOG.

### Loading

For CLI commands, sessions are loaded without a browser:

```python
from graftpunk.cache import load_session_for_api

session = load_session_for_api("mysite")  # returns GraftpunkSession
```

This extracts cookies and headers from the cached `BrowserSession` into a `GraftpunkSession` (a `requests.Session` subclass) suitable for API calls. A bare base name like `"mysite"` is tried as an exact key first and, only if nothing is cached under it, falls back to resolving it against the single cached account for that base (or raises `AmbiguousSessionError` naming the candidates when several are cached) — so a consumer holding a bare name keeps working after a labelled `gp mysite login`. The listing that resolution needs happens only on that miss path.

If you write the session back afterwards, load it through the variant that tells you which slot answered:

```python
from graftpunk.cache import load_session_for_api_resolved, update_session_cookies

session, operating_name = load_session_for_api_resolved("mysite")  # e.g. "mysite@alice"
# ... use the session ...
update_session_cookies(session, operating_name)  # states the slot at the call site
```

#### Loading from bytes (no filesystem, no browser stack)

`load_session_for_api_from_bytes(encrypted, *, key=None)` does the same thing for callers that already hold the encrypted blob and cannot use a storage backend or the browser stack — the motivating case is a Cloudflare Python (Pyodide) Worker that read the session through an R2 binding and holds the Fernet key as a secret:

```python
from graftpunk import load_session_for_api_from_bytes

api = load_session_for_api_from_bytes(r2_object_bytes, key=worker_secret_key)
```

Deserialization here never imports the browser stack: an unpickler stubs out classes it cannot import (`graftpunk.session.BrowserSession` and friends) and keeps the plain state (cookies, headers, header roles, and cached tokens). That is what makes the lite install (`graftpunk-lite`, which has no browser stack) usable under Pyodide. It is the only browser-free loader. On graftpunk-lite, `gp version`, `gp session list`, `gp session show`, `gp config`, `gp plugins`, `gp http` with `--no-session`, a plugin command that needs no session, and `load_session_for_api_from_bytes` work. Everything that loads a cached session from the session store (`load_session_for_api`, `gp http` with a session from `--session`, `gp session use` or `GRAFTPUNK_SESSION`, a plugin command that uses its session, and `gp session export`) and every command that drives a browser (recording with `gp observe`, and a plugin's `login` command) need `graftpunk-lite[browser]` or `graftpunk`; without it, those commands print the install command and exit 1. The session-store loaders go through `load_session`, which reconstructs the real cached object.

Errors are deliberately distinguishable: `EncryptionError` means the bytes could not be decrypted with that key (malformed, wrong, or rotated — an operator/config problem), while `SessionExpiredError` means the payload decrypted but is not a usable session (log in again).

**Trust boundary:** the Fernet key is the *only* thing gating what reaches the unpickler, and unpickling can execute code. The post-load structure check is a shape guard, not a safety control. Never pass a blob and a key that come from different trust domains.

### GraftpunkSession and Browser Header Replay

`GraftpunkSession` extends `requests.Session` with browser header roles captured during login. When a session is cached after login, graftpunk captures the actual HTTP headers the browser sends (via CDP network events) and classifies them into roles:

- **navigation** — Headers from top-level page loads (Accept, Accept-Language, Accept-Encoding, User-Agent, etc.)
- **xhr** — Headers from XMLHttpRequest/fetch calls (typically includes additional headers like X-Requested-With)
- **form** — Headers from form submissions

When `load_session_for_api()` loads a session, it returns a `GraftpunkSession` that automatically detects and applies the appropriate header role (navigation, xhr, or form) for each request based on its characteristics. This means API calls look like they came from the same Chrome browser that logged in, not from Python's default `requests` User-Agent.

**Browser identity separation:** `GraftpunkSession` separates headers into two axes: *browser identity* (User-Agent, sec-ch-ua, sec-ch-ua-mobile, sec-ch-ua-platform, Accept-Language, Accept-Encoding) and *request type* (Accept, Sec-Fetch-*, X-Requested-With). Browser identity headers are extracted at init and set as session defaults — they can never leak, even when the detected request-type role wasn't captured during login. When a detected role is missing (e.g., login was an SPA that only produced xhr/form requests, but the plugin later makes a navigation-style request), registered role headers are used as fallback.

```python
api = load_session_for_api("mysite")
# api.headers contains browser identity headers:
# User-Agent, sec-ch-ua, Accept-Language, Accept-Encoding (including br), etc.
# Request-type headers (Accept, Sec-Fetch-*, etc.) are applied per-request.
response = api.get("https://example.com/api/data")
```

Brotli (`br`) is included as a core dependency so `Accept-Encoding: gzip, deflate, br` is properly supported.

#### Request-Type Methods

For explicit control over which header role is used, `GraftpunkSession` provides three methods:

```python
# XHR-style request (Accept: application/json, X-Requested-With, etc.)
resp = session.xhr("GET", "https://example.com/api/data", referer="/dashboard")

# Navigation-style request (Accept: text/html, Sec-Fetch-Mode: navigate, etc.)
resp = session.navigate("GET", "https://example.com/page")

# Form submission (Content-Type: application/x-www-form-urlencoded, etc.)
resp = session.form_submit(
    "POST", "https://example.com/login", referer="/login", data={"user": "me"}
)
```

Each method:
1. Starts from the captured role headers for that request type (if available)
2. Falls back to registered role headers (if the role wasn't captured during login)
3. Browser identity headers are already on `self.headers` from session init
4. The `referer` kwarg resolves paths against `gp_base_url` (paths like `"/invoice/list"` become full URLs)
5. Caller-supplied `headers=` override role headers
6. All other `**kwargs` pass through to `requests.Session.request()`

This eliminates the need for plugins to maintain their own header-building infrastructure. A plugin that previously needed 150 lines of request helpers can reduce to one-line calls.

#### Auto-Detection Rules

When using `session.get()`, `session.post()`, etc. (the standard `requests.Session` methods), `GraftpunkSession` auto-detects which role to apply via `_detect_role()`:

| Condition | Role |
|-----------|------|
| `gp_default_role` is set | Whatever it's set to |
| Non-GET/POST method (DELETE, PUT, PATCH, HEAD, OPTIONS) | `xhr` — browsers can only issue these via `fetch()`/`XMLHttpRequest` (HTML spec §4.10.18.6) |
| `Accept: application/json` in caller headers | `xhr` |
| POST with `json=` kwarg | `xhr` |
| POST with `data=` kwarg | `form` |
| GET without `Accept: application/json` | `navigation` |

The explicit methods (`xhr()`, `navigate()`, `form_submit()`) bypass auto-detection and apply the requested role directly. Their headers are passed as request-level headers, which take precedence over session-level auto-detected headers in the `requests` merge logic.

### Session Persistence After Commands

Commands can opt into saving session changes back to the cache. This is useful when API responses set new cookies (e.g., refreshed auth tokens):

```python
@command(help="Refresh token", saves_session=True)
def refresh(self, ctx: CommandContext):
    resp = ctx.session.post(f"{self.base_url}/api/refresh")
    # CommandContext.save_session() is called automatically after the handler
    return resp.json()
```

The `update_session_cookies()` function merges new cookies from the API session back into the cached `BrowserSession`. It uses the session name **literally, with one fallback**: a slot cached under that exact name always wins, and it never resolves a bare base name against the list of cached sessions, so it can never fork a bare `mysite` slot open beside the `mysite@alice` a session was loaded from. The fallback covers the case where the name has no slot of its own. A session remembers in memory which slot it was last loaded from or saved to, so `update_session_cookies(session, "mysite")` on a session loaded from `mysite@alice` refreshes `mysite@alice` when no bare `mysite` slot exists ([#174](https://github.com/stavxyz/graftpunk/issues/174)). Last load or save wins, and a bare `mysite` slot that does exist is written instead. A bare name with neither a slot of its own nor a remembered one still drops the refresh with only a log line, which is why callers that hold the operating name the load resolved (the CLI's resolved name, `ctx.save_session()`, or `load_session_for_api_resolved`'s second return value) keep passing it.

### Bot-Detection Cookie Filtering

When injecting cookies into a nodriver browser (for observe mode, token extraction, etc.), graftpunk automatically skips known WAF bot-detection cookies. These cookies — set by services like Akamai Bot Manager — carry bot-classification state from previous sessions. Injecting stale copies causes WAFs to immediately flag the new browser, resulting in connection-level rejection (`ERR_HTTP2_PROTOCOL_ERROR`).

Filtered by default:
- `bm_*`, `ak_bmsc`, `_abck` (Akamai Bot Manager)

The filter lives in `inject_cookies_to_nodriver()`, the single chokepoint through which all cookies flow into nodriver browsers (observe mode, token extraction, interactive observe). The function returns a `tuple[int, int]` of (injected, skipped) so callers have visibility into what was filtered. If all cookies are filtered, a warning is logged indicating the session may not work.

Cookies with `None` name or value are also skipped with a debug log (malformed cookie guard).

Each injected cookie keeps its domain, path, `Secure` flag, HttpOnly flag and expiry; a session cookie stays a session cookie, and an expired cookie is skipped. The HttpOnly flag is read from the `graftpunk-http-only` nonstandard attribute, which `cookie_from_browser` writes for a cookie the browser reported HttpOnly and `BrowserCookiePolicy` writes or clears from each `Set-Cookie` header it accepts; the `HttpOnly` key alone cannot be trusted, because `requests` gives every cookie it creates one. Captured cookies in a session cached by 1.21.0 or earlier recorded no `Secure` flag or expiry (cookies a server set did), and no session cached before this release has the marker, so their HttpOnly cookies are injected without HttpOnly until the next login. A cookie stored under a policy other than `BrowserCookiePolicy`, such as a stock `requests.Session`'s, gets no marker from its `Set-Cookie` and keeps any `graftpunk-http-only` attribute the server sent, and a cookie built from an `http.cookies.Morsel` gets no marker for its `httponly` flag. `SameSite` is not recorded by `http.cookiejar`, so an injected cookie gets Chrome's default. A domain without a leading dot reaches Chrome as host-only (Chrome 155, measured on 2026-10-18: a cookie injected for `site.localhost` was not sent to `sub.site.localhost`). Through 1.22.0 only the name, value, domain and path were passed, so Chrome stored every injected cookie as a non-`Secure`, script-readable session cookie ([#261](https://github.com/stavxyz/graftpunk/issues/261)).

The filter can be disabled per-call with `skip_bot_cookies=False`. The `BOT_DETECTION_COOKIE_PREFIXES` tuple can be extended with patterns for Cloudflare, Imperva, PerimeterX, DataDome, and other WAFs as they are encountered.

### Cookie Policy

A graftpunk session should send the cookies the browser that captured it would send. Capture keeps each cookie's attributes: `transfer_nodriver_cookies_to_session` (nodriver) and `transfer_driver_cookies_to_session` (selenium) store every cookie the browser reports through `graftpunk.cookies.cookie_from_browser`, with its domain, path, `Secure` flag, expiry (a session cookie stays a session cookie) and HttpOnly flag. A reported domain with a leading dot is stored as a domain cookie and one without as host-only; Chrome 155 reports the two kinds this way through CDP (measured on 2026-10-16); through ChromeDriver it was not measured. Through CDP, Chrome 155 reports a cookie set without `Domain` by `http://localhost:<port>` with the domain `localhost`, by `http://[::1]:<port>` with `[::1]` (brackets included) and by `http://site.localhost:<port>` with `site.localhost` (measured on 2026-10-16). A host-only cookie for a host without a dot is stored under `<host>.local` (`localhost.local`, `[::1].local`), the name `http.cookiejar` files that host's own `Set-Cookie` cookies under, so a server's refresh of the cookie replaces it instead of being sent beside it. That name is also the domain of a cookie from the mDNS host `<host>.local`, so a host-only cookie records the host that set it (lowercase, an IPv6 literal in brackets) in the nonstandard attribute `graftpunk-host`: `cookie_from_browser` records the host the browser reported, and `BrowserCookiePolicy` records the request's host when the jar accepts a host-only `Set-Cookie`, so a server's refresh carries it too. The recorded host leaves out any `user:password@` the request URL carried, which the standard library keeps in the stored domain (`user:pw@localhost.local`). A server can send an attribute named `graftpunk-host` in its `Set-Cookie`, and `http.cookiejar` keeps it. `BrowserCookiePolicy` overwrites it on a host-only cookie, but a cookie extracted under another policy (`requests`' `response.cookies`, a jar whose policy a program chose, or a session cached by 1.21.0 under HTTPie's policy) keeps the server's value, so graftpunk trusts a recorded host only when the jar files that host under the cookie's own domain (`printer` or `printer.local` for `printer.local`), and treats any other cookie as having none. The attribute is never sent, and it is kept when the session is cached and when `requests` copies the jar. `inject_cookies_to_nodriver` and the cache's cookie domain list (which `gp session show` prints, and which supplies the session's domain when it has no URL) give the recorded host back, so a cookie captured from `printer` returns to the browser as `printer` and one from `printer.local` as `printer.local`. For a host-only cookie without the attribute (stored by an earlier release, or by other code), they give the stored domain without any `user:password@`, `localhost` and `[::1]` without the suffix, and any other `<name>.local` as it is, since that may be a real `.local` host. An IPv6 host reported without brackets is stored with them. Through 1.21.0 both paths stored every cookie as a non-`Secure` session cookie without host-only scope, and the selenium path also dropped its path, so a `Secure` cookie captured at login was sent over plain HTTP to any host.

`graftpunk.cookies.BrowserCookiePolicy` is the standard library's `http.cookiejar.DefaultCookiePolicy` with two rules changed. The first is a `Secure` cookie over plain HTTP: the standard library never sends one, and Chrome 155 (measured on 2026-10-16) sends one to `http://localhost`, `http://<name>.localhost`, `http://127.0.0.1` and `http://[::1]`, so the policy sends one to those hosts. Other addresses in `127.0.0.0/8` were not measured and are not treated as secure. The second is a host-only cookie (one stored without a `Domain`, from capture or from a `Set-Cookie` header): the standard library also sends it to subdomains, and the policy sends it to its own host alone, as RFC 6265 (section 5.4) specifies and as Chrome 155 did when measured on 2026-10-16 (a cookie set without `Domain` by `http://site.localhost:<port>` was sent to `site.localhost` and not to `sub.site.localhost`). A host-only cookie that recorded the host that set it is sent to requests for that host only, so a cookie from `http://printer` does not go to `http://printer.local`, or the reverse, though both are stored as `printer.local`. One without a recorded host goes to the host its domain names, which for `printer.local` means `printer` and `printer.local` both, as the standard library sends it. Other differences remain: `http.cookiejar` does not model `SameSite`, the `__Host-` and `__Secure-` name prefixes, or `Partitioned` cookies, and graftpunk does not apply them. Whether Chrome would withhold a cookie on those grounds from the requests graftpunk makes was not measured. It subclasses the class `http.cookiejar` defines, found once at import, never whatever class `http.cookiejar.DefaultCookiePolicy` names at the time, which HTTPie or a program may have replaced. It pickles as a call of `http.cookiejar.DefaultCookiePolicy`, looked up by name when loaded, with its settings, so earlier releases, which have no `graftpunk.cookies`, load a session this release cached as they load one 1.21.0 cached; graftpunk's load paths turn it back into a `BrowserCookiePolicy`. A subclass a program defines carries rules of its own, so it pickles by reference, and `copy.copy` and `copy.deepcopy` return the subclass.

Every `requests` session graftpunk builds carries the policy: `BrowserSession`, the `GraftpunkSession` that `load_session_for_api` and `load_session_for_api_from_bytes` return, and the plain session a command gets when it needs none (`graftpunk.cookies.BrowserCookieSession`, used for `gp http --no-session` and plugins with `requires_session = False`). Stock `requests` ignores a jar's policy when it sends (`Session.prepare_request` merges cookies into a new jar with the default policy, psf/requests#5449), so these sessions mix in `BrowserCookiesMixin`, whose `prepare_request` is the stock one with the merged jar given the session jar's policy. Everything else happens in stock order: a `Cookie` header the caller set is left alone, and an `auth` handler runs after the header is computed and may replace it. Redirects keep the policy, because `requests` follows them with a copy of the prepared request's jar and `RequestsCookieJar.copy()` copies the policy.

Which jars get the policy: the jar a graftpunk session creates gets a new `BrowserCookiePolicy` with the standard library's default settings, whatever class `http.cookiejar.DefaultCookiePolicy` names at the time and whatever settings that class's constructor gives. A jar graftpunk did not create (one loaded from a cache, or one a program assigns to `session.cookies`) has its policy replaced, keeping its settings, when the policy is a default: the standard library's `DefaultCookiePolicy`, HTTPie's `HTTPieCookiePolicy` (which sessions cached by 1.21.0 and earlier carry), or the class `http.cookiejar.DefaultCookiePolicy` names at that moment, which a jar created or unpickled under a replaced global was given without anyone choosing it. A policy of any other class was set on that jar by a program, and is kept. A program that sets a policy of the global's own class on a jar it hands to graftpunk gets it replaced, because graftpunk cannot tell that from the default. These rules hold whether the global was replaced before or after graftpunk was imported. As with stock `requests`, `session.cookies` may be any `http.cookiejar.CookieJar`, such as a `MozillaCookieJar`.

`gp session export` (`BrowserSession.save_httpie_session`) writes each cookie's name, value, domain (without any `user:password@` the request URL carried), path, `Secure` flag and expiry to the HTTPie session file, so HTTPie sends it only where its domain, path and `Secure` flag allow ([#260](https://github.com/stavxyz/graftpunk/issues/260)), except that HTTPie's own policy sends a `Secure` cookie over plain HTTP to `localhost` names (not to `127.0.0.1` or `[::1]`); through 1.21.0 it wrote names and values only, and HTTPie sent every cookie to every host. HTTPie's file format has no host-only flag and HTTPie reads every cookie in it back as a domain cookie, so HTTPie sends an exported host-only cookie to subdomains too. Nor can the file record the host that set a cookie: a host-only cookie for a host without a dot is exported under `<host>.local`, the only domain HTTPie sends to that host, and HTTPie sends a cookie exported for `printer` or for `printer.local` to both hosts.

graftpunk leaves `http.cookiejar.DefaultCookiePolicy` as it found it. HTTPie replaces that global for the whole process when `httpie.compat` is imported (`httpie.context` imports it too), so graftpunk imports those modules only for `gp session export` (`BrowserSession.save_httpie_session`), inside `graftpunk.cookies.importing_httpie`, which puts the global back when the import returns. It holds a lock so one thread at a time runs it; the lock is reentrant so that a caller already inside `importing_httpie` can call `save_httpie_session`. Loading a session cached by 1.21.0 or earlier imports `httpie.cookies`, whose class that cache names; that module leaves the global alone. After `import graftpunk`, loading a session, a plugin command, `gp http` or `gp session export`, a program's own `requests.Session` uses whatever policy it would without graftpunk. One effect remains: Python imports a module once, so after graftpunk has exported a session, a later `import httpie.compat` in the same process does nothing and HTTPie's global is not installed. A program that runs HTTPie in-process and needs HTTPie's localhost rule can import `httpie.compat` before graftpunk exports.

### Storage Backends

Controlled by `GRAFTPUNK_STORAGE_BACKEND`:
- `"local"` (default) — Filesystem storage
- `"supabase"` — Cloud storage via Supabase

### CLI Commands

```bash
gp session list              # List cached sessions
gp session show <session>    # Session metadata (domain, cookies, expiry)
gp session clear [session]   # Remove one or all sessions (--all for all)
gp session export <session>  # Export cookies to HTTPie session format
gp session use <session>     # Set active session for subsequent commands
gp session unset             # Clear active session
```

---

## Plugin System

This section is the reference. For the how-to, from recording a site to a tested, installed plugin, see [Writing a graftpunk plugin](PLUGIN_DEVELOPMENT.md).

Plugins define CLI command groups for specific sites. Each plugin has a `site_name` (the CLI subcommand) and a `session_name` — the **base** session name, an input to session resolution rather than the storage key itself. The key a command actually reads and writes is the operating name resolved per invocation (`myshop` or `myshop@alice`); see Multi-Account Sessions.

### Plugin Types

#### Python Plugins

Subclass `SitePlugin` and use the `@command` decorator:

Command names are the kebab-cased Python names (`by_parcel` becomes `gp <site> by-parcel`, a class `AccountStatements` becomes the group `account-statements`); pass `@command(name="...")` to choose a different CLI name.

```python
from graftpunk.plugins import CommandContext, SitePlugin, command


class MyPlugin(SitePlugin):
    site_name = "mysite"
    session_name = "mysite"
    base_url = "https://example.com"
    api_version = 1

    @command(help="List items")
    def items(self, ctx: CommandContext, page: int = 1):
        return ctx.session.get(f"{self.base_url}/api/items?page={page}").json()
```

Commands receive a `CommandContext` (dataclass) with:
- `session` — A `requests.Session` loaded from the cache (cookies pre-loaded)
- `plugin_name` — The plugin's `site_name`
- `command_name` — The command being executed
- `api_version` — The plugin's API version
- `base_url` — The plugin's `base_url` (empty string if not set)
- `config` — The full `PluginConfig` object (or `None`)
- `observe` — An `ObservabilityContext` for screenshots, logging, and timing

Parameters are auto-introspected from the method signature. Type hints (`int`, `float`, `bool`, `str`, also as `X | None`) are used for CLI parsing, with or without `from __future__ import annotations`. Every introspected parameter becomes an `--option`, a required one included, and a `bool` becomes a flag; declare `params=` on `@command` for a positional argument. See the guide's [CLI parameter types](PLUGIN_DEVELOPMENT.md#cli-parameter-types).

**Plugin protocol:** All plugin types implement `CLIPluginProtocol` (a structural typing `Protocol`). This defines the interface: `site_name`, `session_name`, `backend` (typed as `Literal["selenium", "nodriver"]`), `requires_session`, and `get_commands()`. Login capability is detected via duck typing (presence of `login()` method or `LoginConfig`).

#### YAML Plugins

Define commands declaratively in YAML:

```yaml
site_name: mysite
base_url: "https://example.com"
requires_session: true

commands:
  items:
    help: "List items"
    method: GET
    url: "/api/items"
    params:
      - name: page
        type: int
        default: 1
```

YAML plugins support:
- **HTTP methods:** GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS
- **URL parameters:** `{param}` in URL path become positional arguments
- **Query parameters:** Remaining params become query string / `--option` flags
- **Headers:** Per-command or plugin-wide, with `${ENV_VAR}` expansion
- **JMESPath filtering:** Extract nested data from JSON responses
- **Resource limits:** `timeout`, `max_retries`, `rate_limit` per command

### Plugin Discovery

graftpunk discovers plugins from three sources, in order:

1. **Entry points** — Python packages with `[project.entry-points."graftpunk.plugins"]` in `pyproject.toml`
2. **YAML files** — `~/.config/graftpunk/plugins/*.yaml` and `*.yml`
3. **Python files** — `~/.config/graftpunk/plugins/*.py` (files starting with `_` are skipped)

**Shared discovery API:** `discover_all_plugins()` and `get_plugin()` in `graftpunk.plugins` provide unified plugin lookup across all sources. Both the CLI and `GraftpunkClient` use these functions. `discover_all_plugins()` is cached (`@lru_cache`) — call `discover_all_plugins.cache_clear()` to force re-discovery.

**Collision detection:** If two plugins share the same `site_name`, registration fails with a `PluginError` showing both sources (e.g., `yaml:/path/a.yaml` vs `entry_point:module.name`). The `GraftpunkApp.add_plugin_group()` method also rejects duplicate group names. This prevents silent shadowing.

Discovery errors are accumulated as `PluginDiscoveryError` / `YAMLDiscoveryError` / `PythonDiscoveryError` (all frozen dataclasses) — valid plugins from the same source still load even if others fail.

### Plugin Configuration

`PluginConfig` is a frozen dataclass — the canonical configuration object. It is constructed via the `build_plugin_config()` factory which handles defaulting and validation. Fields can be set explicitly or inferred:

- `site_name` — Required. Can be auto-inferred from `base_url` (e.g., `https://news.ycombinator.com` → `"ycombinator"`) or from the YAML plugin filename (e.g., `httpbin.yaml` → `"httpbin"`)
- `session_name` — Defaults to `site_name`
- `help_text` — Defaults to `"Commands for {site_name}"`
- `backend` — `"selenium"` (default) or `"nodriver"`
- `requires_session` — Whether commands need a cached session (default `True`)
- `api_version` — Plugin interface version (currently `1`)

`PluginParamSpec` is also a frozen dataclass defining CLI parameter specifications (name, type, required, default, help text, is_option flag). An option marked `secret=True` (`secret: true` in YAML) has its value scrubbed from an observe run, kept out of `--help`, graftpunk's error messages and every line logged through `graftpunk.logging.get_logger` while the command runs (from any thread, every value of the line), and prompted for with hidden input on stderr when it is required, missing, and stdin is a terminal. A YAML option can read its value from `envvar: NAME`, or from a `default` of `${NAME}` or `${NAME:fallback}`, resolved when the command runs (never for `--help` or shell completion) through the process environment and then the workstation env file, as headers are.

### Building a plugin from a recording

`gp plugin new <name> --from-run SESSION [--run RUN_ID]` fills a scaffold
from a run's digest: `base_url` from the primary host, `login_config` from
the first detected login form, `token_config` from paired header/meta or
header/cookie candidates, and one command stub per endpoint (JSON first, up
to twelve). `--run` names a specific run instead of the session's newest
one, and is an error without `--from-run`. Everything the digest could not
determine is marked `# GP-FILL: <what to fill in>`. `gp plugin new <name>`
with no `--from-run` scaffolds a bare project from `--url` alone.

A plugin name starts with a letter, uses letters, digits, hyphens, and
underscores, does not end with a hyphen or underscore, and is at most 40
characters; the command refuses anything else with a message naming the
rule. It is also refused when it collides with a reserved top-level `gp`
command name, which is any command name registered on the CLI at the time
plugins attach, including `plugin`, `plugins`, `session`, `http`, `config`,
`keepalive`, and `observe`. The name `lite` (in any case) is refused because
a standalone project for it would be named `graftpunk_lite`, which is
graftpunk's own browser-free distribution, `graftpunk-lite`. A name ending
in a hyphen or underscore (`shop-`) is refused because a standalone project
for it would be named `graftpunk_shop_`, which is not a valid distribution
name. Hyphenated names are legal and map to an importable package:
`gp plugin new my-shop` writes `src/graftpunk_my_shop/plugin.py` and
`tests/test_my_shop.py`, registers the entry point
`my-shop = "graftpunk_my_shop.plugin:MyShopPlugin"`, and the plugin's own
CLI command stays `gp my-shop`. A generated project passes its own
`ruff check` and `ruff format --check` as written, whatever the captured
site facts contain (long paths, long parameter names, quotes in selectors):
the scaffold bounds generated identifiers and wraps wide literals so the
developer never starts from a red gate.

Generated (and hand-written) commands call two `CommandContext` methods
instead of the session directly:

- `ctx.request_json(method, url, *, role="xhr", **kwargs) -> Any` sends
  *url* (relative to `base_url`, or absolute) with role headers and returns
  the parsed JSON body. A 401 or 403 raises `SessionRejectedError`; a 2xx
  whose body is a login page also does (a stale session's tell); any other
  non-JSON 2xx raises `UnexpectedResponseError`; any other 4xx/5xx raises
  `CommandError`.
- `ctx.request_text(method, url, *, role="navigation", **kwargs) -> str`
  returns any 2xx body as text, detecting rejection by status only, so an
  HTML endpoint's real response is never mistaken for an expired session.

Both helpers normalise a `params` or `data` mapping before sending it, so a
command can pass Python values and the site still sees what it recorded.
`True` and `False` go out as `true` and `false`, not as `requests`' own
`True`/`False`, and a key whose value is `None` is dropped from the request
entirely. That second rule is what makes a generated stub's
`keyword_search: bool | None = None` mean "omit this parameter unless the
caller asked for it".

A generated stub declares an option for a parameter only when that option can
send the type and shape the recording shows; a field that cannot be sent as
recorded is left undeclared, with a `GP-FILL` comment naming it. A recorded form
body goes out as `data=`, a JSON body as `json=` holding only the fields the
caller gave, a bool is a `--x/--no-x` flag, and a list (a repeated query or form
key, or a JSON array) is a repeatable option sent as repeated keys or a JSON
array. The JSON body fields left undeclared are an object, a `mixed` value, or
an array of objects, booleans, arrays, mixed elements, or only empty arrays. A
body field typed apart from a query parameter of the same name gets its own
`--body-<name>` option.

Path segments and names are judged apart, because they cost different things: a
name read as an id costs a field the site expects, and a path segment read as an
id costs only an extra placeholder, while a path segment missed keeps its value
in every generated file. A path segment fails closed. One that holds a digit
stays literal only when every part of it (split on `_`, `.`, `-`, `~`, and `$`)
holds no digit, is a word spelled with the consonant pairs English uses and no
run of four or more capitals followed by a digit run of at most two (`address2`,
`windows10`, `ec2`), is a version of at most two digits (`v2`, `v1beta1`;
`v40912` is not), or is a digit run of at most two standing alone, and a lone
digit run is the only part holding a digit (`/page/2` and `step-2` stay literal;
`03-14-87`, `10.0.0.1`, and `acct-12-34` do not). Every other segment holding a
digit becomes a placeholder: a date, a card, phone, or national id number
written in digit groups, a long number, a short random token (`kqzpwmab47`,
`x7Kq29Lp`), or three or more short letter-and-digit parts (`ab12-cd34-ef56`). A
segment the name rule below reads as an id (an email, for one) becomes one too.
The path rule's known limits: a letters-only segment that holds no id stays
literal, and so does a word with one or two digits after it (`smith42`,
`mary12`). A family of many sibling segments that each carry a digit
(`red-widget-1`, `red-widget-2`, ...) collapses by count.

A name is dropped only on strong evidence. Every name position goes through one
rule, `graftpunk.har.paths.holds_an_id`: query, JSON body, and form keys;
response keys; request header names; cookie and token candidate names; and a
login form's element ids, input names, and hidden input names. A name holds an
id when, percent-decoded, it or one of its parts (split on `_`, `.`, `-`, `~`,
and `$`, so a file extension splits off) is an email; a run of six or more
digits (`user_40912873`); hex of twelve or more characters mixing digits and
letters (`a3f9c2d1e0b4`, which also catches a UUID by its last group), or `0x`
and twelve or more hex digits; three or more all-digit parts totalling seven or
more digits (`4111-1111-1111-1111`, `123-45-6789`; a date such as `2024-01-15`
as a whole is a name), or a phone number with its area code in parentheses
(`(555)123-4567`); a prefixed id whose tail is twelve or more characters mixing
upper case, lower case, and digits (`cus_NffrFeUfNV2Hib`), at the start of the
name or of any `_`/`-` part (`otp_cus_NffrFeUfNV2Hib`); or a base64-like token
of twenty-four or more characters switching between letters and digits at least
five times. A response object whose keys are ids as a group (three or more keys
of one length of twelve or more, each an alphanumeric run mixing letters and
digits that does not read as a word with a short number, and not one name with
different trailing numbers such as `addressLine1` to `addressLine3`; push ids
and record ids are such groups) has every key replaced by `{key}`.

A path segment that holds an id becomes a placeholder; a query, body, or form
key or a header name that holds one is dropped and counted, and so is a key that
is not a field name at all (one starting with a digit, holding a character
outside the field-name alphabet, or longer than 64 characters; `$` is inside it,
so OData's `$filter` and WebForms' `ctl00$Main$txtSearch` are field names), each
cause counted apart and stated in its own `GP-FILL` comment in the generated
stub; a response key that holds one becomes `{key}` in the shape; a cookie or
token name that holds one is left out and counted, written in no form (not even
hashed: a hash of a short id is reversed by brute force); and a hidden login
input whose name holds one is dropped and counted.

A login form's roles follow HTML semantics, anchored on its password input: the
one marked `autocomplete="current-password"`, else the first password input. The
username is the input `autocomplete="username"` names anywhere in the form; else
the one `autocomplete="email"` names nearest before the password; else the
text-like input nearest before the password with a username hint (an `email`
type, or a name holding user, email, login, or account); else the text-like
input nearest before it; else, when nothing text-like precedes the password, the
first input after it named exactly `username`, `email`, `login`, or `user`. The
submit is the first submit control after the password (an image input counts),
and a control whose `form` attribute names the form belongs to it wherever it
sits; one outside its form is selected by its id, else by `tag[form="id"]` with
its name or type when that picks it alone on the page, else it is unresolved.
Each other text-like input between the username and that submit is a role keyed
by its name. A checkbox, radio, file, image, reset, range, or hidden input is
never a field role, each role is assigned once, and an empty `type=""` counts as
no type. A registration form (no input marked `current-password`, and either one
marked `new-password` or a second password input named as a confirmation) is
left out, and so is a lone one with a confirmation password; a lone form with
one password marked `new-password` is kept (the attribute misused on a login
form), and a password-plus-PIN form is a login form; a change-password form (a
`current-password` input, a new password, and no username-like input) is not a
login form at all (judged on the page's forms alone, so nothing outside a form
changes what a form gives); a stale-session check still counts any form with a
password input as a login page (a `<form>`; inputs outside one do not count
there). A password input with no form owner (in no `<form>` and with no `form`
attribute, or with a `form` attribute naming no form on the page) makes a login
form of its own, listed after the page's forms: a login modal whose inputs sit
in a `<div>` and that a script sends. Its controls are the ownerless ones inside
the innermost element around the password, below `<body>`, that holds a submit
control, a `<button type="button">`, a text-like input before the password, or
another password input; with no such element, the password alone. A control
inside two such elements belongs to the innermost, so a header search box, a
newsletter input, a cart button, or a cookie banner elsewhere on the page never
takes a role, and the same modal on pages that differ elsewhere is one form.
Each group is judged by the rules above on its own controls, so a sign-up modal
beside a login modal leaves the login's, and the groups are listed with one
holding a `current-password` input first, then one whose element's id or class
and controls' ids and names do not name a sign-up (`signup`, `register`,
`create`, `join`, and the like), then in document order. The same rules pick its
roles, except that a `<button type="button">` after the password is the submit
when nothing after it submits, and a missing submit is unresolved like a missing
username, so the generated step carries a `GP-FILL` for each. Its `action` and
`method` are empty, since no browser submits it; each of its inputs is selected
by its id, else by a name no other input on the page shares, else it is
unresolved; a control inside `<noscript>` is left out; and the digest treats it
as a form with no target of its own (below). A login modal that a script renders
only after a click is in no recorded HTML response, and is in the page source
saved when the recording stops only if it is still open then, so its inputs are
usually still not found. In a `<form>`, each input is selected by its id, else
its name (when no
other control inside any form with the same action shares it: a header
mini-login and the main form can post to one action), else its type (the same
way) (`input:not([type])` for a typeless input), and an id or a name that holds
an account value is never used; a name that holds one, or no name, gets a
neutral role key (`field_1`, never a name another input of the form has) and a
`GP-FILL` naming which of the two it was. A selector by type is used only when
it picks one input of the form, and is never printed without the form scope:
when the form's action holds an id the printed selectors drop the scope, so only
an id selector, or a name no other input on the page shares, is printed. A form
whose action is empty or only a fragment (`#`, `#login`) is scoped to match
that, and one whose action names another scheme than `http` or `https` has no
path to template: it keeps its scope and is printed as written
(`javascript:void(0)`), unless its text holds an id or a token by the path rule
(`javascript:go(12345678)`) or an email anywhere
(`javascript:go('alice@example.com', 'ref')`), when it is printed as
`javascript:{id}` and its selectors lose the scope. A role left without a
selector is listed in `LoginForm.unresolved_roles` and the projection's
`unresolved_roles`, and the generated `LoginStep` carries a `GP-FILL` naming it
and why; a username the form has no input for (the password page of a multi-step
login) gets its own `GP-FILL` saying so. A POST to where a login form a GET
served posts (the same host and path, the action resolved against the page as
requested and compared before any email in it is masked) is the credential post
whatever its password field is named; a form in a POST's own response never
marks that POST, and a slash-less action from a saved page source (`session`,
`./session`), which has no page URL to resolve against, matches any POST path
ending in `/session`, a wider match than a resolved action gets. Each credential
post makes one earlier page the login form's page: among the pages whose form
posts where the post went, the one whose matching form sits on the fewest
recorded pages (a dedicated login page's form, not a site-wide header form),
then the one whose form covers more of the post's body names, then the nearest,
however many assets lie between; a post found by its field names alone (no
recorded form, a saved page source's included, posts where it went) promotes
only the nearest page whose form does not post where the credential post went
(an empty, `#`, or `javascript:` action, one naming the page itself, or inputs
outside any form), and no page when every earlier form posts somewhere real.
When every such form on that page is a formless group and one of them is on more
than one recorded page (a login modal shown site-wide), the post is credited
instead to the page, among the earlier ones showing that group, whose path has
the fewest segments (`/` when it was recorded), then the earliest recorded: that
is the page `login_config.url` opens whichever page the user signed in from, and
no page's GET is part of the login flow, since a modal shown on every page makes
none of them a login page. A POST whose body asks for a new password
(`new_password`, `password_confirm`) is never a credential post by its field
names. Every other page carrying a login form is an ordinary page and
keeps its stub. The same form on several pages is one form, told by its
structure (its element id and each control's tag, type, name, and id), and is
kept as the copy whose selectors resolve best. Among the forms, one a credential
post went to is listed first; among those, the one on the page that post
promoted, then the one the earliest credential post went to (a login precedes a
password change; a post found by its field names alone went to the form with no
target of its own on the page it promoted, so a script login's form ranks ahead
of a later form posting to its own page, such as a change-email form), then the
one whose control names cover the most of the post's body, then one that sits on
no page a credential post did not promote (a site-wide header form ranks below
the main form), then the one with fewer unresolved roles, so the generator's
`login_config` is built from the form the recording used. Only that login is the
login flow: its promoted page; the credential posts that went to its form, or,
when none did, the posts found by their field names alone to the first target no
recorded form posts to (a script posting elsewhere than the form says); and each
hop of those posts' redirect chains, a request to where the previous hop sent
the client (on the same host) or a POST that submits an OAuth `form_post` page's
form (a post form of hidden inputs only, a `<noscript>` submit button aside and
a `<select>` or `<textarea>` counted as visible, on the previous hop's page,
carrying only the hidden names of that page's forms posting there, to another
host than the page's, hosts compared without case or a default port, as an
identity provider's page posts to the app; a cart or logout form on a landing
page is not one, and neither is a same-site form a script submits). A POST
carrying a password field is never such a hop. A password change, an account
edit, a later password-confirmed action (whatever the login form's action, its
own page's or none), or any later POST answering with a redirect keeps its
commands, and its redirect is not the login's landing page. When the landing is
ambiguous, none is taken and `success_url` gets a `GP-FILL` saying why: a
missing success signal is visible, and a wrong one would fail every login. A
credential post answering 200 (a script login) starts no chain, whatever its
page holds; a chain that rests on a 200 page holding a `form_post`-shaped form
it did not follow (a same-host identity provider's, or a hidden-only logout form
on the landing page) may or may not continue past it; and a chain whose last hop
is a `form_post` submission answering without a redirect leaves the client on a
URL no redirect named; so none of these takes a landing. Each credential post
starts over, so a later post never brings back a landing an earlier post's chain
refused. A landing whose glob would match the login page takes none either,
since a failed login returns there: a later attempt redirecting back to
`/login`, or a landing on `/account` for a login at `/account/login`, whose
`*/account*` matches `/account/login?error=1`. The match is fnmatch, which has
no notion of a query string: `?` is its own wildcard (any single character),
not an anchor for the one that starts a query, so a pattern or a page holding a
literal `?` follows fnmatch's ordinary rules there, and the generated `GP-FILL`
failure text is the backstop for a wrong call this produces.

Each rule is measured in the position it guards
(`tests/unit/test_id_miss_rates.py`). Every entry of a key-position table of
those shapes must be caught as a name, and each sub-rule of the name rule must
be the only catch of one entry. The share of ordinary names read as ids is held
under a ceiling on three corpora: the regression corpus (the names reviewers
raised), a tuned corpus of public SDK and API names (written as a held-out
corpus, and then the name rule's thresholds were set against it), and a fresh
corpus of 229 field, header, and cookie names written after those thresholds
were set and not tuned against. When the rule last changed they read 0 of 482, 0
of 259, and 0 of 229 as ids. Random tokens are covered where they occur, in
paths: the path rule's miss rate on seeded random tokens of each shape is held
under a ceiling (8.2% of eight-character lower-case base36 tokens, most of them
letters only). The name rule's known limit: a short random token used as a field
name (`kqzpwmab47`, `x7Kq29Lp`, `usr_8fk2x9qa`) is kept, and so is any account
value in a shape it does not list. From the path rule: `login_config.url` is the
page the login form was on, or a `GP-FILL` comment when that page's path holds
an id; and one in `success_url`'s landing path is a `*`. A missed id is kept, so
read what a digest prints before you commit it; a name read as an id is
templated or dropped, which fails safe, and the `GP-FILL` counts make it
visible.

`graftpunk.testing` (pytest-free) supplies `make_context()` for building a
`CommandContext` directly in a test, and `FixtureSession`/`fixture_context()`
for answering `ctx.request_json`/`request_text` from a file under
`tests/fixtures/` instead of the network, named the way `gp observe
fixtures` names captures. A plugin added to an existing suite gets
`tests/fixtures/<module>/` of its own, since fixture names are per endpoint
and two plugins in one suite can share an endpoint path. `gp observe
fixtures` writes a `<file>.meta.json` sidecar beside every capture (a `schema`
number, the status, the content type, the body parameter names, the hash of the
captured body, every cookie name the recording set or sent except a cookie
whose every non-empty recorded value is form state and that has neither a token or
session-like name nor a `Set-Cookie` with `HttpOnly` (in the header or in the
HAR's `response.cookies` array), and the token names the run's digest recorded, a
name that holds an id left out and counted in `redacted_names`; from schema 2,
also the reasons each of those cookie and token names is flagged
(`flagged_reasons`, which the fixtures check repeats in its problem line); it
never holds the URL or the capture time), written to be
committed beside the fixture derived from it once you have read its body
parameter names and its `flagged_names`: a body key that does not read as a
field name or holds an id is dropped, but the rule is lexical, so an account
value in a shape it does not read as an id is kept, and some sites put account
data in a cookie's name. A value is form state when, with one pair of
surrounding double quotes taken off, it reads as words
(`graftpunk.har.paths.reads_as_words`: a search term, a short number, a date),
or as cased words (`graftpunk.har.paths.reads_as_cased_words`: one word in
capitals such as `LANDOWNER`, a camelCase name such as `currentTab` or
`tabLISTresults`, or one of 12 letters or more that starts with a capital such
as `LISTviewResults`, or a name such as `SMITH, JOHN`), or when it is ASCII
punctuation and spaces alone (a field cleared to `""`; a control character
such as `%00` is not punctuation). The cased-words rule is
told from a random id only by spelling, so it leaves out some random ids that
`reads_as_words` alone flagged. These are shares added over 1.24.0, not totals (the guide gives totals): measured on 100,000 values per shape
(2026-10-24, `scripts/cookie_value_rates.py`), 0.67% of 8 mixed-case
letters and 0.20% of 12, 42% of 4
capitals and 5.2% of 12,
and 1.1% of 8 capitals and digits. `FixtureSession` reads the sidecar through
`graftpunk.testing.sidecar` for status and content type, so a fixture copied
from a capture keeps its recorded status.
`graftpunk.testing.plugin.site_env_scrubber(prefix)` returns a pytest
fixture that removes prefixed environment variables for the duration of each
test; a generated `conftest.py` imports it and assigns the result to a
module-level name, which is what registers the fixture.

---

## Login System

graftpunk supports three login approaches, from simplest to most flexible.

### 1. Declarative Login (YAML or Python)

Define login configuration with CSS selectors. The login engine generates the browser automation. Login flows are defined as a sequence of **steps**, where each step can fill fields, click a submit button, and optionally wait for elements or add delays.

#### LoginStep Fields

Each step in a login flow can include:

- **`fields`**: Dict mapping credential names to CSS selectors. Can be empty for click-only steps.
- **`submit`**: CSS selector for the submit button (optional).
- **`wait_for`**: CSS selector to wait for before this step executes (nodriver only).
- **`delay`**: Seconds to pause after submit (optional).

#### LoginConfig Fields

The top-level login configuration includes:

- **`steps`**: Required list of `LoginStep` objects defining the login flow.
- **`url`**: Optional login page path (appended to `base_url`), or an absolute URL (used as-is) when the login form lives on a different host than `base_url`.
- **`wait_for`**: Optional top-level wait for element before any steps begin.
- **`failure`**: Optional text that indicates login failure.
- **`success`**: Optional CSS selector that indicates login success.
- **`success_url`**: Optional glob matched against the whole browser URL after the last step, such as `https://app.example.com/*` or `*/dashboard*`. It counts only once the URL differs from the one the last submit was clicked from, so a glob that also matches the login page reports nothing rather than reporting success too early. Use it for a login that finishes on a recognisable URL, on its own or alongside `success`; with both set, both have to hold.
- **`timeout`**: Seconds to wait after the last step for the success or failure signal (default `30`). Raise it for a login that finishes through a slow identity-provider redirect chain. It bounds the whole post-submit wait: the document-readiness wait that follows the signal spends what the poll left of it. It also caps each step's readiness wait before the step acts, at whichever is smaller of it and 5 seconds.
- **`settle`**: Seconds to wait after the success signal, once the document has finished loading, before cookies are captured (default `1`). A login with no success signal never reaches it.
- **`headless`**: Run the login browser headless (default `false`, so a human can solve a CAPTCHA or 2FA prompt). `gp <plugin> login --headless` / `--headful` override it for one invocation in either direction; the flags are offered only on declarative logins (a plugin's own `login()` method cannot honour them).

#### Field filling is verified

Some server-rendered sites re-render their login form shortly after load (jQuery/select2 and friends). The element handle resolved for a field can be detached from the DOM by the time keys are sent; the keystrokes then go nowhere and the browser refuses to submit the empty `required` field. The nodriver engine guards against this: each attempt re-selects the element, clears it, types, waits briefly (the browser acknowledges key events before the DOM value updates — reading back immediately makes things worse), and reads the field's value back from the live DOM by selector. An empty read-back means the value never reached the live element: it retries up to 3 times and then fails with an error naming the field rather than the credentials. A non-empty read-back that differs from what was typed (sites that lowercase, trim or mask input) is logged and accepted.

#### Diagnosing a failed login

`login` reporting a failure means the page did not reach the expected post-login state. The engine logs what it actually detected: the configured `failure` text, a `Too Many Requests` (rate-limit) page on any pass that read the page text (the last pass before the timeout included, so a login that gives up still names a limiter when one answered), or the success signal never appearing before `timeout` (that warning names the missing selector or URL pattern and the URL the page ended on). Read the warning before assuming bad credentials, and if the login really does take longer than `timeout`, raise it. To capture the whole flow, run `gp --observe=full <plugin> login`: it records an observe run under the plugin's session name whether or not the login succeeds, and prints the run directory. `gp observe list` shows it. With the nodriver backend the run holds a screenshot, the page source, a HAR with response bodies, and console logs; with the selenium backend `BrowserSession` owns the capture, which yields the HAR (with request and response bodies, read from the performance log, which ChromeDriver keeps only when observe is on), console logs, and a screenshot only if the login raised. The login's header-role capture reads the same log, so a Selenium login caches header roles only when run with `--observe full`; without it, Chrome keeps no performance log and the login caches none. The nodriver login engine starts its own capture on the tab the login opens and does not hand `observe_mode` to its `BrowserSession`, so the login is recorded once.

Redirect chains are recorded one HAR entry per hop, linked by `response.redirectURL`, so a login form whose `POST` answers with a `302` shows up as the `POST` (with its status, `Location`, and its redacted body when CDP inlined it or the request-time fetch described under Capture Backends got it; a body neither gave is lost across the redirect, logged as `redirect_hop_post_data_unavailable`, and marked `request._postDataNotCaptured` on the hop's entry) followed by the request it redirected to.

Both backends also subscribe to `Network.requestWillBeSentExtraInfo` / `responseReceivedExtraInfo`, the events that carry the raw wire headers Chromium filters out of `responseReceived`: the HAR's `response.headers` include `Set-Cookie` (also parsed into `response.cookies`) and `request.headers` include the raw `Cookie` header (parsed into `request.cookies`). On the login `POST → 302` hop that means the cookie the server minted is visible on the hop that minted it: CDP does not guarantee the order these events arrive in, so headers are buffered and correlated by request id (a late `responseReceivedExtraInfo` is matched to a waiting redirect hop by status code) rather than assumed in order; if a hop's ExtraInfo never arrives, the drain logs `extra_info_unmatched` with the count of hops still waiting. One measured caveat (counted at the event handler, on a real login of 11 requests): nodriver 0.48.1 delivered 1 `requestWillBeSentExtraInfo` event (none carrying `Cookie`) while 0.50.3 delivered 9 (6 with `Cookie`), so raw request `Cookie` headers depend on the nodriver version — measured on those two versions only, intermediate releases untested. `Set-Cookie` capture worked on both.

**The run is sensitive.** The credentials you submitted are scrubbed from the HAR (raw, URL-encoded, JSON-escaped and base64 forms), and from the text request bodies written to the run's `bodies/` directory (a login POST too large for the HAR, for one), but the exchange still contains the session cookies the login minted. A binary request body in `bodies/` has each of those forms' bytes masked with as many `*` bytes, so its length and offsets stay as recorded, and the file is named in the log; response bodies in `bodies/` are not scanned. A body file that cannot be read or rewritten is logged and left as it was (it is rewritten atomically, so never truncated), and the HAR is written regardless. Treat the run directory like a cached session, and delete it when you are done.

#### Single-Step Login (Traditional Form)

For simple login forms where username and password are on the same page:

```python
from graftpunk.plugins import LoginConfig, LoginStep


class MyPlugin(SitePlugin):
    base_url = "https://example.com"
    backend = "selenium"

    login_config = LoginConfig(
        url="/login",
        failure="Invalid credentials",
        success=".dashboard",
        success_url="*/dashboard*",
        steps=[
            LoginStep(
                fields={"username": "#email", "password": "#password"},
                submit="button[type=submit]",
            ),
        ],
    )
```

Or in YAML:

```yaml
login:
  url: /login
  failure: "Invalid credentials"
  success_url: "*/dashboard*"
  steps:
    - fields:
        username: "input#username"
        password: "input#password"
      submit: "button#login"
```

#### Multi-Step Login (Identifier-First)

For modern login flows where username and password are on separate screens (common with Azure AD B2C, Okta, Google, etc.):

```python
from graftpunk.plugins import LoginConfig, LoginStep


class MyPlugin(SitePlugin):
    base_url = "https://example.com"
    backend = "nodriver"  # nodriver recommended for multi-step flows

    login_config = LoginConfig(
        url="/login",
        wait_for="#login-form",
        failure="Invalid credentials",
        # The identity provider hands control back to the app: wait for its URL,
        # not for a fixed pause, and give the chain room to finish.
        success_url="https://app.example.com/*",
        timeout=60.0,
        steps=[
            LoginStep(
                fields={"username": "input#signInName"},
                submit="button#next",
            ),
            LoginStep(
                wait_for="#password-section",  # Wait for password field to appear
                fields={"password": "input#password"},
                submit="button#submit",
                delay=0.5,  # Brief pause after submit
            ),
        ],
    )
```

Or in YAML:

```yaml
login:
  url: /login
  wait_for: "#login-form"
  failure: "Invalid credentials"
  success_url: "https://app.example.com/*"
  timeout: 60
  headless: false  # default; true for sites that need no CAPTCHA/2FA
  steps:
    - fields:
        username: "input#signInName"
      submit: "button#next"
    - wait_for: "#password-section"
      fields:
        password: "input#password"
      submit: "button#submit"
      delay: 0.5
```

#### Click-Only Steps

A step with only `submit` (no `fields`) is useful for dismissing modals, accepting terms, or clicking through interstitials:

```yaml
login:
  url: /login
  steps:
    - submit: "button#accept-cookies"   # Dismiss cookie banner
    - fields:
        username: "input#user"
        password: "input#pass"
      submit: "button#login"
```

#### How It Works

The declarative engine executes each step in sequence:

1. Opens the browser to `{base_url}{login.url}`
2. **(If top-level `wait_for` is set)** Waits for the specified CSS selector before proceeding
3. **For each step:**
   a. **(If step `wait_for` is set)** Waits for the step's selector to appear. **(Otherwise)** Waits for the page to be ready (see below), except on the first step when the top-level `wait_for` is set
   b. Clicks each field element and types the credential value
   c. **(If `submit` is set)** Clicks the submit button
   d. **(If `delay` is set)** Pauses for the specified duration
4. Polls the page every half second, up to `timeout` seconds, starting one interval after the last step rather than immediately. Each pass reads the current URL; it reads the page text when the page text can decide that pass, which is every pass when `failure` is set, every pass once a `success_url` matches (a rate-limited response is served at the landing URL itself), and in every configuration the last pass before the timeout. The `failure` text ends the wait as a failure, a `Too Many Requests` page on a pass that read the text ends it as a failure, and the success signal ends it as a success. The success signal is the `success` element being present, the URL having moved off the one the last submit was clicked from and matching the `success_url` glob, or both when both are configured.
5. Waits for the document to finish loading, bounded by what the poll left of the same `timeout` budget, then pauses for `settle` seconds
6. Transfers cookies and caches the session

The wait is what makes a slow login work: sites that finish through an identity-provider redirect can take tens of seconds to mint their cookies, and the engine holds until the signal it was told to look for appears. When the timeout passes first, the login fails and the warning names the signal that never appeared and the URL the page ended on.

Write both signals so they cannot match the login page itself. An element that is already on the form, or a glob as loose as `*example.com*`, would otherwise be satisfied before the submit has navigated anywhere, and the session cached would be the one from before the login. The engine guards the URL half of that (a `success_url` counts only once the page has moved off the URL the submit was clicked from) and delays the first pass by one interval, but a `success` element that the form and the landing page share is beyond its reach. That guard needs the URL the submit was clicked from: when the browser could not read it (nodriver reports no URL while a tab navigates), a `success_url` cannot be confirmed at all and the login waits out its timeout, so a site where that happens needs a `success` selector.

Configure at least one of `failure`, `success`, and `success_url`. With neither `success` nor `success_url` set there is nothing to wait for, so the engine watches the page for three seconds for the `failure` text (and for a rate-limit page), stops the moment either shows, and then takes the single verdict it has always taken from the page text. `timeout` and `settle` play no part on that path. With none of the three set, the verdict is always success and a warning advises you to add validation.

**Readiness before a step acts.** A client-rendered page puts its login link in the server HTML before the script that handles it has loaded, so a click that lands first does nothing (#275). Before each step (on nodriver, each step that has no `wait_for` of its own), the engine waits until `document.readyState` is `complete` and the page has been quiet for half a second, counted from the start of that step's wait. Quiet means: no DOM change (nodes, text, or the `class`, `disabled`, `hidden` and `href` attributes; `style` is left out because an animation writes it every frame); no `<script src>` the browser fetches still without a resource timing entry; no script, fetch, XHR or frame resource finishing, and no script preload finishing (a `<link>` whose `rel` holds the token `modulepreload`, or `preload` with `as="script"`, matched in any case), since a bundler's entry module runs only once its preloaded chunks arrive (images, styles, fonts and beacons are left out); and, on nodriver only, no document, script, XHR or fetch request starting, finishing or in flight according to the tab's `Network` events. A request in flight for longer than 5 seconds stops counting, so a long poll or a lost finish event cannot hold every later step. Scripts the browser never fetches are not counted as pending: a classic script with `nomodule` (a module script ignores it), a type string that is neither a JavaScript MIME type nor `module` (`text/plain`, `application/json`, `text/template`, or `text/vbscript` from `language="vbscript"` with no `type`), and a `data:` or `blob:` URL. The pending check is skipped once the timing buffer is full (250 entries). A page can empty the buffer with `performance.clearResourceTimings()`, which monitoring libraries call after they report, so the probe keeps every entry name and the latest finish time it has read on the document and judges against those: a script that had its entry before the clear stays fetched, a script inserted after it stays pending until its own entry arrives, and a script cleared before any read saw its entry counts as pending, which holds the wait to the cap rather than ending it early. One known case still holds the wait to the cap: a `<script src>` inserted through `innerHTML` is never fetched, and nothing a page-side script can read tells it apart from one still downloading. The page keeps its own quiet clock across steps (the page-side observer is installed on the first read of each document), but a wait never counts quiet time from before it started: otherwise a step that follows a navigating click could read the old document, quiet since the previous step, and act on the new one before it loaded. The wait lasts at most 5 seconds or `timeout`, whichever is smaller; a page that never goes quiet (a ticking clock, a long-poll request younger than 5 seconds) is acted on at that point and an info-level `login_page_not_quiet` event says so. A read that fails (the execution context is torn down mid-navigation) or returns something unexpected counts as not ready and is read again; a page never readable before the cap is acted on with an info-level `login_page_ready_unreadable` event. On a static page the wait costs the half-second window on every step: measured on 2026-10-24 in headless Chrome through nodriver 0.50.6, the step of a static form waited 0.51 s. Some hydration has no DOM or network trace the engine can see (a handler attached by a timer after the page went quiet), so the wait cannot promise the handler is there; on nodriver, a `wait_for` naming an element or class only the page's script renders can. A step with `wait_for` (or the first step, when the top-level `wait_for` is set) skips the default wait, which is also how a nodriver plugin opts out of it on a page that never goes quiet. Selenium rejects `wait_for`, so a Selenium login always runs the wait, with the DOM probe alone, and cannot turn it off.

**Which element a step acts on.** A field or submit selector is first waited for as before (`_select_with_retry`), then the engine asks the page how many elements it matches, which are visible, and which are disabled. Visible means laid out, not hidden by `display`, `visibility` or `opacity: 0`, and not pushed off the left or top of the page or past its right edge, where an off-canvas drawer parks its copy. The right edge is the page's scroll width when the page is wider than the window, so a fixed-width layout in a narrow window keeps its right-hand column; an element below the fold is visible. One case reads wrong: an absolutely positioned or transformed copy past the window that no ancestor clips widens the scroll width to cover itself, and nothing the probe can read tells it from a fixed-width column, so it counts as visible (such a page shows a horizontal scrollbar). One match is acted on whether or not it is visible, as before. With several matches the engine acts on the first visible one and logs `login_selector_ambiguous` once per selector with the count, the number visible and the position it chose; a responsive page often carries a hidden mobile-menu copy of a link, or of the whole form, ahead of the visible one. A field is clicked, cleared, typed into and read back as that one match: the clear check and the read-back find it again by its position among the matches in the live document. If a copy is added or removed ahead of the field between typing and reading back, the position names another element; the read-back then misses the value, the `login_field_value_mismatch` warning says the selector now picks a different match than the one typed into, and the next attempt finds the field again. When none of several matches is visible, or the chosen match is disabled, the engine keeps asking until the 30-second element timeout and then fails the step with a `PluginError` that names the count (`matches 2 elements and none of them is visible`). After such a wait, and when a page transition interrupts the lookup, the match is looked up again rather than reusing a handle that may be detached. Selenium follows the same rule through `find_elements` and `execute_script`. A probe that cannot be read leaves the first match, as before.

**The verdict prints last.** `gp <plugin> login` prints its verdict as the final lines of output, after whatever the browser and the capture logged on the way, and the verdict carries its own reason: the signal that did not appear (or the failure text, rate-limit page, or unreadable page that ended the wait) and the URL the page ended on, or the step error and that URL when a step failed. The engine records both while it runs (`recording_login_verdict` in `graftpunk.plugins.login_settle`), so a truncated terminal that keeps only the tail still shows what happened. A hand-written `login()` that returns `False` records no reason, so the CLI prints a generic one (`the page did not reach the expected post-login state (see the warnings above).`) with no `Page:` line. The `Re-run with --observe=full` hint is its own line, left out of a run already made with `--observe=full`.

**Per-step `wait_for` is nodriver-only.** Setting `wait_for` on individual steps requires `backend = "nodriver"`. The top-level `wait_for` is also nodriver-only. Setting `wait_for` on a plugin with `backend = "selenium"` raises a `PluginError` at login time with guidance to switch to the nodriver backend.

**Resilient element selection (nodriver):** During page transitions (cross-origin redirects, SPA navigation), the DOM document node itself can become invalid, causing nodriver's `tab.select()` to throw a `ProtocolException` instead of returning `None`. The login engine wraps all pre-submit element selection calls with a retry helper (`_select_with_retry`) that catches `ProtocolException` and retries with a 30-second deadline and 1-second intervals. This gives the browser time to complete redirects and render the form. After the last step the success selector is looked up once per poll pass instead, with no retry inside a pass: the poll is the retry, and each pass has to come back before the next one is due.

**Backend differences in success detection:**
- **Selenium:** Uses `driver.find_element()` with a try/except for `NoSuchElementException`
- **NoDriver:** Before the submit, element lookups go through `_select_with_retry` around `await tab.select(selector)`, which returns `None` rather than raising when the element is not there yet. Each pass of the post-submit poll calls `await tab.query_selector(selector)` once instead and checks for a `None` return: the pass does not wait, because the poll is the retry.

### 2. Custom Login Method (Python)

Define a `login()` method on your plugin. It receives a `credentials` dict:

```python
class MyPlugin(SitePlugin):
    def login(self, credentials: dict[str, str]) -> bool:
        with self.browser_session_sync() as (session, driver):
            driver.get(f"{self.base_url}/login")
            driver.find_element("id", "email").send_keys(credentials["username"])
            driver.find_element("id", "pass").send_keys(credentials["password"])
            driver.find_element("id", "submit").click()
            # browser_session_sync handles cookie transfer and caching
        return True
```

For nodriver (async):

```python
async def login(self, credentials: dict[str, str]) -> bool:
    async with self.browser_session() as (session, tab):
        await tab.get(f"{self.base_url}/login")
        # ... async login logic
    return True
```

The `browser_session()` and `browser_session_sync()` context managers handle browser lifecycle, cookie transfer, and session caching automatically.

**Where the account-qualified name comes from.** During `gp mysite login`, the
CLI derives the account label from the login identifier (or takes it from
`--as`) and sets the operating session scope around your `login()` call. Two
ways of caching pick that name up for you: the `browser_session()` and
`browser_session_sync()` helpers, which cache on their success path, and
`cache_login_session(self, session)` (exported from `graftpunk.plugins`) when
you build the session yourself. Inside `login()`, `self.session_name` is the
plugin's bare base name (`mysite`), never the account-qualified slot, because
nothing mutates the plugin instance. A `login()` that calls
`cache_session(session, self.session_name)` by hand therefore writes the bare
slot, and the CLI says so after the login: it reports that nothing was written
to the slot it named, and names the two fixes. Switch to
`cache_login_session(self, session)`, or declare `session_name` and
`account_identifier` as keyword arguments on `login()` and the CLI passes both
in.

**Return value contract:** `login()` should return `True` on success and `False` on failure. The CLI checks for `is False` specifically — returning `None` or other values triggers a warning log but is treated as success for backwards compatibility.

### 3. Auto-Generated Login Command

If a plugin has either a `login()` method or declarative login configuration, graftpunk auto-generates a `login` CLI command:

```bash
gp mysite login
```

Credentials are resolved in order:
1. **Environment variables** — `{SITE_PREFIX}_{FIELD_NAME}` (e.g., `MYSITE_USERNAME`, `MYSITE_PASSWORD`). Plugins can override envvar names via `username_envvar` / `password_envvar`. Empty environment variable values are treated as unset (with a debug log).
2. **Interactive prompts** — Fields containing "password", "secret", "token", or "key" use masked input.

```bash
MYSITE_USERNAME=x MYSITE_PASSWORD=y gp mysite login  # No prompts
```

---

## Command Execution

### Execution Paths

Commands execute through two paths that share the same retry/rate-limit core:

- **CLI path** — The CLI callback calls `execute_plugin_command()` from `graftpunk.client`. The CLI handles session loading, token injection, 403 retry, session persistence, and output formatting around it.
- **Python API path** — `GraftpunkClient._execute_command()` manages the full pipeline: lazy session resolution and loading, token injection, retry/rate-limit (via the shared `_run_handler_with_limits()`), 403 token refresh, and session persistence. All commands return `CommandResult`.

Both paths use the same `_run_handler_with_limits()` function for retry and rate-limit enforcement, ensuring consistent behavior.

### Resource Limits

Commands can specify resource limits on `CommandSpec`:

```python
@command(help="Fetch data")
def fetch(self, ctx: CommandContext): ...


# Or set on the spec directly:
CommandSpec(name="fetch", handler=fn, timeout=30, max_retries=2, rate_limit=1.0)
```

- **`timeout`** — Request timeout in seconds (passed to the handler context)
- **`max_retries`** — Number of retry attempts on transient failures. Uses exponential backoff (2^attempt seconds). Only retries `requests.RequestException`, `ConnectionError`, `TimeoutError`, and `OSError`. Programming errors propagate immediately.
- **`rate_limit`** — Minimum seconds between consecutive calls to the same command.

YAML plugins set these per command:

```yaml
commands:
  fetch:
    timeout: 30
    max_retries: 2
    rate_limit: 1.0
```

### Output Formatting

All plugin commands support `--format` / `-f` with five built-in formatters:

- **`json`** (default): Pretty-printed JSON with syntax highlighting via Rich.
- **`table`**: Rich tables. Lists of dicts become rows; single dicts become key-value pairs. Falls back to JSON for other types.
- **`csv`**: Comma-separated values. Headers are the union of all row keys. Nested dicts/lists are JSON-serialized in cells. A cell that is not a number and starts with `=`, `+`, `-`, `@`, a tab, a carriage return, a line feed, or a full-width `＝`, `＋`, `－` or `＠` gets a leading `'`, so a spreadsheet shows it as text and never runs it as a formula (#288). Header cells and `json_to_csv` follow the same rule.
- **`xlsx`**: Excel workbook saved to disk. Files are written to `GP_DOWNLOADS_DIR` (default: `./gp-downloads/`), with bold headers and auto-sized columns. Every non-empty string is written as a text cell, never as a formula or a hyperlink; an empty string or a missing key leaves a blank cell, and numbers stay numeric cells (#288).
- **`raw`**: Strings pass through directly; other types are JSON-dumped without highlighting.

Third-party formatters can be registered via the `graftpunk.formatters` entry-point group. They must implement the `OutputFormatter` protocol (a `name` property and a `format()` method).

If a command returns a `CommandResult`, the `data` field is extracted for formatting and the optional `format_hint` is applied (see [Output Format Precedence](#output-format-precedence)).

#### Multi-View Rendering

Commands can define multiple **views** on their response data using `OutputConfig` with one or more `ViewConfig` entries. Each view selects a subset of the data by path and optionally filters columns.

How each formatter handles multiple views:

| Formatter | Multi-View Behavior |
|-----------|-------------------|
| **table** | Renders each view as a separate section with a titled Rule header. Single views render without a header. Empty views are silently skipped. |
| **xlsx** | Creates one worksheet per view. Worksheet tabs use `view.title` or `view.name` (truncated to 31 chars for Excel compatibility). |
| **csv** | Warns that only one view can render and uses the default view. Suggests `--view` to select a specific view. |
| **json** | Ignores views — always renders the full data structure. |
| **raw** | Ignores views — renders as-is. |

Example from a Python plugin:

```python
from graftpunk.plugins.cli_plugin import CommandResult
from graftpunk.plugins.output_config import OutputConfig, ViewConfig


@command(help="List invoices with summary")
def list(self, ctx: CommandContext):
    data = ctx.session.get(f"{self.base_url}/api/invoices").json()
    return CommandResult(
        data=data,
        format_hint="table",
        output_config=OutputConfig(
            views=[
                ViewConfig(name="invoices", path="items", title="Invoices"),
                ViewConfig(name="summary", path="totals", title="Summary"),
            ],
            default_view="invoices",
        ),
    )
```

#### The `--view` CLI Option

Users can select specific views and restrict columns with `--view`:

```bash
# Show only the "invoices" view
gp mysite list --view invoices

# Show "invoices" view with only id and amount columns
gp mysite list --view invoices:id,amount

# Show multiple views
gp mysite list --view invoices --view summary
```

The `--view` option is repeatable. When used with a single view, section headers are suppressed for cleaner output.

#### OutputConfig Types

All output configuration types are frozen dataclasses with tuple fields to prevent post-construction mutation.

- **`OutputConfig`** — Container for views. Fields: `views: tuple[ViewConfig, ...]`, `default_view: str`. Provides `filter_views(names, column_overrides)` for `--view` support and `get_default_view()` for single-view formatters like CSV.
- **`ViewConfig`** — A single named view. Fields: `name`, `path` (dot-separated extraction path), `title` (display label), `columns` (optional `ColumnFilter`), `display` (column display config).
- **`ColumnFilter`** — Include or exclude columns. Fields: `mode: Literal["include", "exclude"]`, `columns: tuple[str, ...]`.

YAML plugins configure views declaratively:

```yaml
commands:
  list:
    url: "/api/invoices"
    output:
      default_view: invoices
      views:
        - name: invoices
          path: items
          title: "Invoice List"
          columns:
            mode: include
            columns: [id, date, amount, status]
        - name: summary
          path: totals
          title: "Summary"
```

#### Downloads Directory

File-based formatters (XLSX) write to a downloads directory:

- **`GP_DOWNLOADS_DIR`** environment variable overrides the default
- Default: `./gp-downloads/` (resolved to an absolute path)
- The directory is created automatically if it doesn't exist
- Files are named `output-YYYYMMDD-HHMMSS.xlsx`

---

## Observability

The observability system captures browser activity during login and command execution for debugging and auditing.

### Modes

Set via `--observe` CLI flag or `observe_mode` on `BrowserSession`:

- **`off`** (default): No capture. All observability calls are no-ops (`NoOpObservabilityContext`), no run directory is created, and Chrome is launched as it would be without observability.
- **`full`**: Captures everything the session can give: a HAR with request and response bodies, plus screenshots, console logs and events where there is a browser.

What `full` records depends on where it is used:

- **A plugin command** (`gp --observe=full <plugin> <command>`). Commands run on a `requests` session, not a browser, so the run records that session's HTTP traffic (`HttpCaptureBackend`, in `graftpunk.observe.http_capture`). While the capture runs, the session's `send` and the `send` of each adapter mounted on it are wrapped. The adapter wrapper notes each response, with when its request was sent; the session wrapper records the responses of one call when requests is done with it. requests follows redirects by calling `send` again, so one outer call holds every hop. A request is recorded whether it went through `Session.request`, `Session.send()` or carried its own `hooks=`. Recording changes nothing a request does. The recorder reads no body itself, before or after anything raises: it records each body as requests read it (each hop's in `resolve_redirects`, the last one in `Session.send` unless streamed), so a request returns, raises, runs its response hooks and sets its cookies exactly as with observe off, as fast. A hop whose body requests failed to read and tolerated (a redirect labelled gzip that is not, a truncated redirect body) is still followed, and its entry is marked `read_failed`. A final response whose body fails to read (broken, undecodable, or a read timeout, which requests raises as `ConnectionError`) raises to the command as before, after its hooks and cookies, and its entry is marked `read_failed: <error>`; a `ConnectionError` an adapter raised on a send belongs to that request instead. Only a read that raised is blamed, and only the response whose read raised it: that response is found as the `self` of `Response.iter_content`'s frame in the error's traceback. When the call fails for another reason before requests read a body (a response hook raised, a `raise_for_status` hook on a 400, a hook's side call on another session was refused, or another response's read failed), the body is not read and is marked `not_read: <error>`. The caller can still read it from the response, which a `raise_for_status` error carries as `exc.response`; another exception may carry no response. A `KeyboardInterrupt` or other non-`Exception` reaches the caller untouched, and nothing is recorded for that call, including redirect hops that had already answered. A request that got no response (a refused connection, also on the hop after a redirect) is recorded with status 0, its error in `response._error`, and `_bodyNotCapturedReason: no_response`. A body nothing read (a 401 a response hook replaced with a new request) is marked `not_read`. A response the command asked for with `stream=True`, or sent on a session whose `stream` is on, is marked `streamed`, unless it is a redirect hop or has `Content-Length: 0`. The adapter wrapper also measures how long each send took, so a response an auth handler re-sends through the adapter (`HTTPDigestAuth` does) gets its real time. An adapter whose pickled state is not `HTTPAdapter`'s is not wrapped, since its wrapper could be pickled with the session, and neither is one mounted after the capture starts. A request through either is recorded from the response the session returns, its hops in chain order and timed from the call's start, each after the one before, but one that raises is not, and `http_capture_adapter_not_recorded` is logged. A session whose pickled state is not `requests.Session`'s (a subclass that pickles its own instance dict) is not wrapped either, logged as `http_capture_session_not_recorded`; its requests are then recorded by the adapters alone, without bodies (`not_read`). Both wrappers are instance attributes that `HTTPAdapter.__getstate__` and `requests.Session.__getstate__` leave out, so a session pickled mid-command carries no recorder and no entries, and a session loaded from it records nothing. The capture starts before token extraction and stops when the command ends, whether it succeeded or failed, and the run's `network.har` is written then. Each request sent is one HAR entry, timed from when it was sent, so each redirect hop is its own entry with `response.redirectURL`, as in a browser HAR. Entries are scrubbed and spooled to `network.har.spool` in the run directory as they are recorded, in the order recorded, and streamed into `network.har` at the end, so the recorder holds one call's responses at a time, not the whole command's traffic. The HAR is written to a temporary file and renamed into place only when complete, and the spool is removed whether the write succeeded or not; a response that arrives after the capture stopped (a request still in flight on a thread the command did not join) is dropped, so it cannot recreate the spool. Entries are built by the browser captures' builder and bodies follow their rules (see [Response bodies](#response-bodies)), with ids `http-1`, `http-2` and so on. A request body sent from a file or generator is marked `request._postDataNotCaptured`. The HAR is scrubbed as a login run's is (`graftpunk.observe.run.Redaction.from_credentials`), of every login credential gp can read without running anything: for each login field, its environment variable (`{SITE}_{FIELD}`, or `username_envvar` / `password_envvar`), else a static value in the workstation env file; a `$(...)` command value is not run. A secret is replaced wherever it occurs. An identifier field's value is left in the URL fields (`request.url`, `request.queryString`, `response.redirectURL`) so the digest's endpoints stay readable, and is replaced in headers and bodies only where it stands as a whole value: not preceded or followed by an ASCII letter or digit (text in another script, as in `用户bob` or `bobé`, does not hide it), where a neighbour is read decoded: an escape (`%XX`, `%25XX` in a double-encoded URL, a JSON escape such as `\n` or `\u0022`, an HTML entity such as `&quot;` or `&#34;`) counts as the character it stands for, so it delimits the value unless it stands for a letter or digit (`%41bob` is `Abob`, and `\\nbob` in raw JSON is a backslash, then `nbob`). It is matched in its raw, percent-encoded, JSON-escaped and base64 forms, so a username `data` leaves `metadata` alone and `returnUrl=%2Faccount%3Femail%3Dbob%40example.com` loses the address. A field is an identifier only by an allowlist (`graftpunk.observe.run.is_identifier_field`): its name, lower-cased with `_`, `-`, `.` and spaces removed, is exactly `username`, `user`, `userid`, `email`, `emailaddress`, `useremail`, `loginemail`, `login`, `loginid`, `loginname`, `identifier`, `account` or `accountid`. Every other field is a secret: name heuristics missed secret spellings (`user_pass`, `userPINCode`), and a secret taken for an identifier stays in URLs. The account label (`gp <plugin> login`) comes only from fields on the same allowlist. An `Authorization: Basic` credential whose decoded `user:password` holds a credential is replaced whole, wherever it appears: the scheme in any case, the token raw, JSON-escaped (`\/`) or URL-encoded (`Basic%20...`, `Basic+...`). Values of options the plugin marks `secret=True` are scrubbed too (see [Secret options](PLUGIN_DEVELOPMENT.md#secret-options)); nothing else a command sends is scrubbed. The run is filed under the plugin's `site_name` and has no screenshots or console logs; `ctx.observe.screenshot()` returns None there. A capture that fails to start is logged as `observe_start_capture_failed`, a warning is printed, the empty run directory is removed, and the command runs without it.
- **`gp http`** records its request with the same capture, started just before the request is sent, so each redirect hop is one entry timed from when it was sent, and a 403 token refresh's requests are recorded too. A request that fails still writes a run, holding the hops that answered and the request that got no response, with its error. `--no-observe` turns it off.
- **`BrowserSession` on Selenium.** The capture reads Chrome's performance log at stop, and ChromeDriver keeps that log only when the session is created with `goog:loggingPrefs = {"performance": "ALL"}`. The stealth driver (`create_stealth_driver(performance_log=True)`) is launched with it whenever `observe_mode` is not `off`, and only then. A Selenium login's header-role capture reads the same log, so a Selenium login caches header roles only when run with `--observe full`; no release before this one enabled the log, so a Selenium login never cached header roles before. `BrowserSession(backend="selenium", use_stealth=False)` raises `ValueError` (#287).
- **`BrowserSession` on nodriver.** The capture records only through CDP handlers, which `start_capture_async()` registers. `async with BrowserSession(backend="nodriver", observe_mode="full")` starts it on the session's event loop, on the browser's first page tab (the tab `driver.get(url)` navigates when no new tab is asked for) before anything loads there, and stops it with `stop_capture_async()`, which fetches the remaining bodies while the browser is alive. A page opened in another tab is not recorded. The synchronous `with` cannot register the handlers; on nodriver it warns that the run will have no HAR entries.

### ObservabilityContext

Plugins interact with observability through the `ctx.observe` handle:

```python
@command(help="Transfer funds")
def transfer(self, ctx: CommandContext, amount: float):
    ctx.observe.mark("transfer_start")
    ctx.observe.screenshot("before-transfer")

    result = ctx.session.post(f"{self.base_url}/api/transfer", json={"amount": amount})

    ctx.observe.log("transfer_complete", {"amount": amount, "status": result.status_code})
    ctx.observe.screenshot("after-transfer")
    ctx.observe.mark("transfer_end")
    return result.json()
```

All methods are safe to call regardless of mode — they're no-ops when observability is off. This means plugins don't need conditional checks.

The `build_observe_context()` factory function creates the appropriate context based on mode and driver availability. With a browser driver it builds that browser's capture; with no driver and an `http_session`, the HTTP capture described under [Modes](#modes); with neither, a context without capture capability, and it logs a warning. It does not start the capture: the caller calls `ObservabilityContext.start()` before the work and `ObservabilityContext.finish()` after it, which stops the capture and writes the HAR and any console logs. A run directory that cannot be created leaves observability off for that run, with a warning.

### Capture Backends

Capture backends implement the `CaptureBackend` protocol and collect browser data:

- **SeleniumCaptureBackend** — Screenshots via `get_screenshot_as_png()`, console logs via `get_log("browser")`, HAR data via `get_log("performance")`. All Selenium calls catch `WebDriverException` specifically (not bare `Exception`), logging errors without crashing the session.
- **NodriverCaptureBackend**: Full async capture via CDP events. Listens for `Network.LoadingFinished` to eagerly fetch response bodies before Chrome's CDP buffer evicts them. Bodies are streamed to disk (configurable `max_body_size`). Records only after `start_capture_async()` has registered its CDP handlers on a tab, and fetches the bodies it still needs in `stop_capture_async()`. The sync `start_capture()` / `stop_capture()` register and fetch nothing. A body fetch that fails is logged per request at debug level (`nodriver_eager_body_fetch_failed`, `nodriver_response_body_fetch_failed`), and each pass that lost bodies logs one warning with the count at stop (`nodriver_eager_body_fetch_summary`, `nodriver_response_body_fetch_summary`): one line per asset buried a login's verdict under hundreds of lines (#275).
- **HttpCaptureBackend**: Records a `requests` session at its transport adapters, for plugin commands and `gp http`; see [Modes](#modes). No screenshots, page source or console logs.

### Response bodies

Every recorder stores a response body by one rule (`_process_response_body` in `graftpunk.observe.capture`). The browser captures fetch every response's body except where HTTP says there is none: a HEAD request, or a 1xx, 204, 205 or 304 status. A body of a binary type (images, audio, video, PDF, archives, octet-stream and Office documents) is kept as binary, checked first and on the type without its parameters, so a type that happens to name a text word is not taken for text. A body of a text type (a type naming JSON, HTML, text, XML, JavaScript, CSS, CSV, form encoding or YAML) within `max_body_size` (5 MB by default) goes into the HAR as `response.content.text`. The browser hands most text over already decoded; bytes are decoded with the charset the `Content-Type` declares, else as UTF-8. No charset is guessed: a guess can be wrong, and the original bytes would then be lost. Every other body, of a binary, unknown or missing type, larger than `max_body_size`, or one that does not decode, is written as raw bytes to the run's `bodies/` as `<request id><ext>` and referenced by `response.content._bodyFile`, with why in `_bodyFileReason` (`binary`, `not_text`, `over_max_body_size` or `not_decodable`). Without a `bodies/` directory (a capture library code built without `bodies_dir`), or when the write fails, the body is left out and marked with `response.content._bodyNotCaptured: true` and `_bodyNotCapturedReason` (`binary`, `not_text`, `over_max_body_size`, `not_decodable` or `write_failed`). The browser captures also mark a body they could not fetch: `fetch_failed: <error>` when `Network.getResponseBody` failed at stop, `browser_disconnected` for every body still unfetched when the browser went away, and `no_tab` when stop had no tab to fetch from. The HTTP recorder adds `streamed`, `read_failed`, `read_failed: <error>`, `not_read: <error>` (the call raised before requests read the body), `not_read` (a body nothing read, or an adapter used without the session's `send`) and `no_response`. An empty body is stored as empty text. Response bodies in `bodies/` are not scrubbed of secrets; request bodies are.

A request body reaches the HAR's `request.postData` one of three ways. Chrome inlines most bodies on `Network.requestWillBeSent` (a string, `URLSearchParams`, or bytes body sent by `fetch` or XHR, a 100,008-byte JSON string among them, measured in headless Chrome through nodriver 0.50.6 on 2026-10-19). An inlined body arrives twice: as `postData`, which Chrome decodes as UTF-8 but, for bytes that are not UTF-8, maps byte for byte to the code points U+0000 to U+00FF; and as `postDataEntries`, the base64 bytes. The recorder takes the bytes from `postDataEntries` (measured on 2026-10-19: their join equals what the server received for a string, `URLSearchParams`, `Uint8Array` and text-only multipart body), so a binary `Uint8Array` body takes the binary path below rather than being stored as mapped text. With no `postDataEntries`, or with an entry that carries no bytes, the body is stored as the `postData` text Chrome gave; whether any Chrome sends a binary body inline without entries is unverified. A body it does not inline (a `Blob` body, or multipart with a file) arrives as `hasPostData` with no `postData`, and the nodriver backend asks `Network.getRequestPostData` for it from a second `RequestWillBeSent` handler, while the request is in flight; this matters because a request from a page the tab has navigated away from is no longer answered at stop time (measured on nodriver 0.48.1). Stop first lets every fetch task already scheduled start, waits up to 5 seconds for them to finish, cancels any still running so it cannot answer beside the retry and store the body twice, and asks again for whatever they missed; however stop ends, any fetch still running is cancelled, and one that has not started by then marks its entry instead of writing into it, before the marks below are set. The Selenium backend reads its events from the performance log only at stop time, so that is its one attempt. A capture whose HAR is never written (the login engine's header-only capture) keeps, fetches and logs nothing about request bodies. An empty body Chrome returns is a captured empty body. A body every attempt misses is logged once as `post_data_unavailable` (request id, method, URL, and the CDP error) and marked on its HAR entry as `request._postDataNotCaptured: true`, a HAR 1.2 custom field like `_bodyFile`. A request body is stored the way a response body is: in the HAR when it is UTF-8 and within `max_body_size` (`--max-body-size`, 5 MB by default, measured on its bytes), and otherwise written to the run's `bodies/` directory as `<request id>.request<ext>` (a 307 or 308 redirect resends the body under the same CDP request id, so the request it redirects to gets `<request id>.request-2<ext>`, and so on down the chain), referenced by `request._postDataFile` and `request._postDataSize`. A base64 body that is not UTF-8 is never decoded into U+FFFD text. The parser reads a UTF-8 file back, so the digest still sees its fields; a file that is not UTF-8 is a binary body, which the digest reports as `body not text` with the file's name and size; a file that is missing, unreadable, or outside the HAR's directory counts as not captured. Every gp recorder that writes a HAR has a `bodies/` directory, `gp --observe=full` included; without one (a capture backend that library code built without `bodies_dir`) such a body is left out, logged as `post_data_omitted`, and marked with `_postDataNotCaptured` and `_postDataOmitted` (`size` in bytes and `reason`, `over_max_body_size` or `binary`), which the digest reports as `body left out`. CDP's own description of `getRequestPostData` says it omits files from multipart requests; whether Chrome returns a large `File` sent as a whole `fetch` body is unverified. For each of the three outcomes, `--endpoints-json` sets the endpoint's `body_not_captured` and `gp plugin new` leaves a `GP-FILL` in its stub saying which, rather than presenting an endpoint that takes no body.

The nodriver backend sends every body fetch to the CDP session its `Network.enable` went to. On nodriver 0.50.6, `Browser.get(url)`, which `gp observe` uses for its first navigation, and `Tab.get(url)` each navigate and then call `attach()` again, which opens a second session on the same tab without detaching the first. Network events keep arriving from the first session, but `tab.send` addresses the second, where Network was never enabled, and Chrome answers `Network.getRequestPostData` there with `-32000 No resource with given id was found` for every request, even while the request is in flight. Measured on 2026-10-19 in headless Chrome through nodriver 0.50.6: each `browser.get` gave the tab a new session id, and the recorder naming the capture's session kept every body (the `Blob` one included) across two navigations, one of them cross-site, with no reload and one HAR entry per request. The backend names the session through `send`'s `sessionId` keyword, which nodriver 0.50 merges into the CDP message. nodriver 0.48.1 has no session ids (each target has its own websocket) and its `Browser.get` does not re-attach (read from its source), so no session is named there and none needs to be; the same recording on nodriver 0.48.1 on 2026-10-19 kept all twelve bodies too, with no session named.

The `create_capture_backend()` factory validates the backend type and creates the appropriate backend instance.

### Storage

Observability data is stored in a structured directory:

```
~/.local/share/graftpunk/observe/{slugified_session_name}/{run_id}/
    screenshots/
        001-before-login.png
        002-after-login.png
    bodies/
    events.jsonl
    network.har
    console.jsonl
    metadata.json
```

The run directory is created when the run starts. `screenshots/` and `bodies/` are created only when a file is written to them, and `console.jsonl` only when there are console logs, so a plugin command's run holds `network.har` (and `bodies/` when a body went there) and nothing empty. A run is readable only by its owner: the run directory, its subdirectories and a session directory gp creates are 0700, and every file gp writes in it is 0600, whatever the umask. A session directory that already exists keeps its mode, and runs written by graftpunk 1.23.0 or earlier keep theirs.

The directory component is the session name slugified (`session_dirname`): `myshop@alice` becomes `myshop-alice`, `my_shop` becomes `my-shop`. Every writer and every `gp observe list/show/clean` lookup applies that one mapping, so a labelled run stays discoverable.

**Path safety:** `ObserveStorage` validates session names and run IDs on construction — rejecting empty strings, path separators (`/`, `\`), directory traversal (`..`), leading dots, spaces, and special characters. Only alphanumeric characters, hyphens, dots (non-leading), and underscores are allowed.

**Screenshot labels** are sanitized: non-alphanumeric characters (except `.`, `-`, `_`) are replaced with `-` to prevent filesystem issues.

**Events** are stored as JSONL (one JSON object per line). Corrupt lines are skipped on read, allowing partial recovery.

### Observability Teardown

When `BrowserSession` exits, observability data is flushed in isolated steps:

1. Final screenshot (if mode allows)
2. Stop capture backend
3. Write HAR entries
4. Write console logs

Each step has its own error handling — a failure in one (e.g., HAR collection fails) does not prevent the others from completing. Errors are logged at the error level.

### CLI Commands

```bash
gp observe list                          # List all observability runs
gp observe show <session>                # Show run details (file list, sizes)
gp observe clean [session]               # Remove observability data
gp observe -s <session> go <url>         # Automated capture (waits --wait seconds)
gp observe -s <session> interactive <url> # Interactive capture (Ctrl+C to stop)
gp observe digest <session> [<run>]      # Read a run into a digest of hosts, endpoints, login, tokens
gp observe digest --har <path>           # Digest a bare HAR file instead of a run
gp observe fixtures <session> --match "<METHOD> <template>"  # Write matching bodies for deriving test fixtures
```

---

## Token and CSRF Support

graftpunk supports declarative token extraction for sites that use CSRF tokens, dynamic session tokens, or other non-cookie authentication mechanisms.

### Token Configuration

Tokens are defined via `TokenConfig` on the plugin (Python) or `tokens:` block (YAML):

```yaml
tokens:
  - name: csrf_token
    source: cookie        # Extract from a cookie
    cookie_name: _csrf
  - name: api_key
    source: response_header  # Extract from a response header
    response_header: X-API-Key
  - name: session_token
    source: page           # Extract from page content via regex
    page_url: /dashboard
    pattern: 'data-token="([^"]+)"'
    cache_duration: 300    # Cache for 5 minutes
```

Token sources:
- **`cookie`** — Extract from a named cookie in the session
- **`response_header`** — Extract from a response header
- **`page`** — Fetch a page and extract via regex pattern

### EAFP Injection and 403 Retry

Token injection follows an EAFP (Easier to Ask Forgiveness than Permission) strategy:

1. Cached tokens are injected into request headers **even if expired** — the server is the arbiter of validity
2. If the server responds with 403, the token cache is cleared, tokens are re-extracted, and the request is retried once
3. This avoids unnecessary token refreshes when the server still accepts a technically-expired token

### Token Cache Persistence

Tokens extracted during login are persisted through the session serialization lifecycle:

- `BrowserSession.__getstate__` includes the `_gp_cached_tokens` attribute when serializing
- `BrowserSession.__setstate__` restores it on deserialization, defaulting to an empty dict if absent
- `update_session_cookies()` preserves the token cache when saving session changes back to the cache
- `load_session_for_api()` copies cached tokens from the stored `BrowserSession` to the `GraftpunkSession`

### Login-Time Token Extraction

When a plugin has both `login_config` and `token_config`, tokens are extracted during the login flow using the already-open browser session. This avoids launching a separate browser instance for token extraction:

- **Nodriver**: `_extract_and_cache_tokens_nodriver()` navigates the existing tab to token pages and extracts via regex polling
- **Selenium**: `_extract_and_cache_tokens_selenium()` uses the existing driver to navigate and extract

Both backends use `_build_token_cache()` to construct `CachedToken` instances from extracted values. Token extraction during login is best-effort — failures are logged but don't prevent session caching.

### Auto-Injection (Standard Flow)

When a plugin has `token_config`, the command executor:
1. Checks the token cache for each token (injecting even if expired — EAFP)
2. Extracts missing tokens via HTTP or browser (two-phase: HTTP first, then batch browser extraction for failures)
3. Injects all tokens into request headers
4. On 403 responses, clears the cache, re-extracts, and retries once

---

### Failure Modes

Extraction failures are typed so callers can react without parsing messages. All of them subclass `TokenExtractionError`, which is also a `ValueError` (earlier releases raised plain `ValueError`; that base is permanent):

- `SessionInvalidatedError` — a cookie-source token's cookie is absent. The session is no longer authenticated: log in again (`gp <site> login`).
- `TokenPatternMismatchError` — a page pattern or response header did not match in HTTP mode. The site changed: update the Token config.
- `TokenExtractionError` (base) — browser-mode extraction returned nothing; the cause is not distinguishable without inspecting the page.

This is distinct from `SessionExpiredError`, which means the *cached* session failed to load (TTL, decrypt, deserialize) before any request was made.

## Ad-hoc HTTP Requests (`gp http`)

The `gp http` command makes authenticated HTTP requests using cached session cookies and browser headers, without writing a plugin:

```bash
gp http get -s mybank https://secure.mybank.example.com/api/accounts
gp http post -s mybank https://secure.mybank.example.com/api/transfer --data '{"amount": 100}'
```

All HTTP methods are supported: `get`, `post`, `put`, `patch`, `delete`, `head`, `options`.

The session is loaded as a `GraftpunkSession` with full browser header replay, so requests have the same fingerprint as the browser that logged in.

### Header Roles (`--role`)

By default, `gp http` sends navigation-style headers. Some API endpoints expect XHR headers instead. The `--role` flag selects the correct combination of `Sec-Fetch-*`, `Accept`, and `X-Requested-With` headers:

```bash
gp http get -s mybank --role xhr https://secure.mybank.example.com/api/status
gp http post -s mybank --role form https://secure.mybank.example.com/submit
gp http get -s mybank --role navigate https://secure.mybank.example.com/page
```

Three built-in roles are registered at import time via `register_role()`: `navigation`, `xhr`, and `form`. The CLI accepts `navigate` as a shorthand alias for `navigation` via `_ROLE_ALIASES`. Plugins can define custom roles via a `header_roles` dict — these are merged into the session at request time, so `--role api` (or any custom name) works the same way.

Under the hood, `--role` calls `session.request_with_role()` directly with the role name. The session checks captured headers first, then registered roles (built-in or plugin-defined), then logs a warning and applies no role-specific headers for unknown role names.

---

## Authenticated Browser Capture (`gp observe go`)

`gp observe go` opens a URL in an authenticated browser session and captures network traffic, screenshots, and console logs:

```bash
gp observe -s mybank go https://secure.mybank.example.com/dashboard
```

This:
1. Loads the cached session cookies for the named session
2. Injects them into a fresh browser instance
3. Navigates to the URL
4. Captures all network requests, responses (with bodies), and console output
5. Stores everything in `~/.local/share/graftpunk/observe/`

Useful for debugging API interactions and discovering undocumented endpoints.

### Interactive Mode (`gp observe interactive`)

Interactive mode keeps the browser open for manual exploration while recording all network traffic:

```bash
gp observe -s mybank interactive https://secure.mybank.example.com/dashboard

# Or as a flag on observe go:
gp observe -s mybank go --interactive https://secure.mybank.example.com/dashboard
```

This:
1. Loads cookies and injects them into a visible browser
2. Navigates to the starting URL
3. Records all network traffic (with response bodies fetched eagerly via CDP)
4. Blocks until you press Ctrl+C
5. Saves HAR data, a final screenshot, page source, and console logs

The `--interactive` flag on `observe go` delegates to the same implementation — `--wait` is ignored when interactive mode is active.

Saved artifacts:
- `network.har` — Full HAR file with request/response bodies
- `screenshots/` — Final screenshot at time of Ctrl+C
- `page-source.html` — Page HTML at time of Ctrl+C
- `console.jsonl` — Browser console log entries
- `bodies/`: request and response bodies too large for the HAR, or binary, written to disk during capture (a request body as `<request id>.request<ext>`)

---

## CLI Architecture

### GraftpunkApp

The CLI is built on Typer/Click. `GraftpunkApp` is a custom `typer.Typer` subclass that manages plugin group registration and provides a custom `__call__` that handles error output for unknown commands.

Plugin command groups are registered as Click subgroups via `add_plugin_group()`, which rejects duplicate names to prevent silent overwriting. Plugin subcommands use `TyperCommand` / `TyperGroup` for consistent rich help formatting across core and plugin commands. Nested subcommand groups created by `_ensure_group_hierarchy()` also use `TyperGroup` (not plain `click.Group`) to maintain rich help formatting at every nesting level.

### Logging

Structured logging via `structlog`. Quiet by default (`WARNING` level). Controlled by:

- `GRAFTPUNK_LOG_LEVEL` environment variable
- `-v` flag (sets `INFO`)
- `-vv` flag (sets `DEBUG`)
- `GRAFTPUNK_LOG_FORMAT` environment variable (`json` or `console`)
- `--network-debug` flag (enables wire-level HTTP tracing via `http.client`, `urllib3`, `httpx`, `httpcore`)

Logging is configured early (before plugin registration) so that plugin load messages respect the configured level.

As a library, graftpunk never configures structlog for the program that imports it (#163). While structlog is unconfigured, graftpunk writes its log lines to stderr, never stdout; once the program configures structlog, they go where that configuration sends them. Every module logs through `graftpunk.logging.get_logger()`, which returns a `GraftpunkLogger` that decides on each call: while structlog is configured (by the host program, or by the `gp` CLI), the line goes through that configuration, and while it is not, through a configuration the logger keeps to itself (stderr, or the null device when `sys.stderr` is None; `WARNING` and above). A host program's own loggers keep structlog's defaults, its `structlog.configure(...)` or `configure_once(...)` takes effect whether it runs before or after the import, and a graftpunk logger bound earlier follows it. Only the `gp` CLI configures structlog for the process, through `configure_logging()`, when `graftpunk.cli.main` is imported (importing another `graftpunk.cli` submodule does not import it). If the host program configured structlog first, that import leaves it alone; only an actual CLI invocation with `-v`/`-vv`/`--log-format` reconfigures. The `--network-debug` flag is independent of verbosity level: it enables stdlib DEBUG logging on network libraries and sets `HTTPConnection.debuglevel = 1` for raw HTTP traffic output.

---

## API Version Contract

`api_version = 1` defines the plugin interface contract:

- **Synchronous handlers** — Command handlers are regular methods (not `async def`). The framework calls them synchronously.
- **CommandContext** — Handlers receive a `CommandContext` with `session`, `base_url`, `config`, and `observe`.
- **LoginConfig-based declarative login** — Plugins declare login flows via `login_config = LoginConfig(...)` (a frozen dataclass).
- **`list[CommandSpec]` from `get_commands()`** — Plugins return a list of `CommandSpec` objects describing available commands.
- **`setup()` / `teardown()` lifecycle** — Plugins may define `setup()` (called once after registration) and `teardown()` (called during application shutdown).

New `api_version` values may introduce breaking changes to this contract (e.g., async-first handlers, different context shapes). Plugins that declare `api_version = 1` are guaranteed to work with the v1 interface.

---

## YAML Plugin Limitations

YAML plugins are ideal for simple REST API wrappers but have inherent limitations. When you outgrow YAML, "graduate" to a Python plugin for full flexibility:

- **No command groups** — YAML commands are flat; nested subcommands (`gp site group cmd`) require Python.
- **No request chaining** — Each command makes a single HTTP request. Multi-step workflows need Python.
- **No conditional logic** — No if/else branching based on response data or parameters.
- **No custom transforms** — Only JMESPath extraction is supported. Complex data reshaping needs Python.
- **No pagination** — Automatic page iteration requires Python loop logic.
- **No token refresh** — OAuth refresh flows or session renewal logic require Python.
- **No custom login flows** — YAML supports declarative `LoginConfig` with multi-step support. CAPTCHA-handling or complex conditional login flows need a Python `login()` method.

The Python template at `~/.config/graftpunk/plugins/` provides a starting point for migration.

---

## Output Format Precedence

The output format for command results follows this precedence (highest to lowest):

1. **`--format` CLI flag** — Explicit user choice always wins. Detection uses Click's `get_parameter_source()` to distinguish "user passed `-f json`" from "json is the default."
2. **`CommandResult.format_hint` naming a formatter the plugin registered or an entry point added** (any name in `SitePlugin.format_overrides` or `CommandResult.format_overrides`, a built-in name included, or a `graftpunk.formatters` entry point under a name that is not built in). It applies to the terminal, a pipe and an `--output` file, as through 1.23.0; the formatter receives `output_path` and the console and decides from them.
3. **`CommandResult.format_hint` naming a built-in formatter the plugin did not override** (`json`, `table`, `raw`, `csv`, `xlsx`, `pdf`). It applies only when there is no `--output` and stdout is a terminal (`graftpunk.console.stream_is_tty`), so a plugin can set it unconditionally (#284).
4. **`--output` suffix**: `.json`, `.csv`, `.xlsx` or `.pdf` (any case, a bare `.csv` name included) picks that format. With an empty result, `.csv` writes an empty file, or the header row when the default view lists its columns.
5. **Default (`json`)**: pretty-printed JSON on a terminal, plain JSON otherwise.

The `--view` option applies after format selection. It filters `OutputConfig.views` by name and optionally overrides columns per view. If the command has no `output_config`, `--view` is ignored with a warning.

Example:

```python
@command(help="List users")
def users(self, ctx: CommandContext):
    data = ctx.session.get(f"{self.base_url}/api/users").json()
    return CommandResult(data=data, format_hint="table")
    # A table in a terminal; JSON in a pipe or in -o out.json; --format wins
```

---

## Error Handling for Plugin Authors

Plugins should use the exception hierarchy for clear error reporting:

- **`CommandError("message")`** — Raise for expected failures: validation errors, missing resources, business rule violations. The framework displays the message cleanly without a traceback.
- **`PluginError("message")`** — Raise for infrastructure failures: missing configuration, broken dependencies, plugin setup errors.
- **Other exceptions** — Any unhandled exception is treated as a crash. The framework logs a full traceback and reports an internal error.

```python
from graftpunk.exceptions import CommandError


@command(help="Get account")
def account(self, ctx: CommandContext, account_id: int):
    resp = ctx.session.get(f"{self.base_url}/api/accounts/{account_id}")
    if resp.status_code == 404:
        raise CommandError(f"Account {account_id} not found")
    return resp.json()
```

---

## Async Handler Detection

Handlers must be synchronous in API version 1. If a handler is defined as `async def`, the framework auto-detects this and executes it using `asyncio.run()`, but this is not officially supported and may change in future versions. For reliable behavior, use synchronous handlers:

```python
# Supported (v1 contract)
@command(help="List items")
def items(self, ctx: CommandContext):
    return ctx.session.get(f"{self.base_url}/api/items").json()


# Auto-detected but not officially supported
@command(help="List items")
async def items(self, ctx: CommandContext):
    return ctx.session.get(f"{self.base_url}/api/items").json()
```

---

## Plugin Configuration Convention

Per-user plugin configuration files follow this convention:

```
~/.config/graftpunk/plugin-config/{site_name}.yaml
```

The graftpunk framework does **not** read this directory. It is a convention for plugin authors who need per-user settings (API keys, preferences, environment-specific URLs). Plugin code is responsible for loading and parsing its own config file:

```python
from pathlib import Path
import yaml


@command(help="List items")
def items(self, ctx: CommandContext):
    config_path = Path.home() / ".config/graftpunk/plugin-config" / f"{self.site_name}.yaml"
    if config_path.exists():
        plugin_settings = yaml.safe_load(config_path.read_text())
    # ... use plugin_settings
```

---

## Credentials and the Workstation Env File

Credentials reach a login through environment variables named `<SITE_NAME>_<FIELD_NAME>`, and graftpunk's per-machine env file (`~/.config/graftpunk/env`, managed with `gp config`) supplies them as static entries or as `$(command)` entries resolved at lookup time. The user-facing description is [Secrets and configuration](PLUGIN_DEVELOPMENT.md#secrets-and-configuration) in the plugin guide; the design is [docs/rfcs/2026-07-28-workstation-env.md](rfcs/2026-07-28-workstation-env.md).

---

## Command Groups

The `@command` decorator supports both individual commands and command groups (nested subcommands). When applied to a class, it creates a command group; methods within the class become subcommands.

### Defining a Command Group

```python
from graftpunk.plugins import CommandContext, SitePlugin, command


class MyPlugin(SitePlugin):
    site_name = "bank"
    base_url = "https://api.bank.com"
    api_version = 1

    @command(help="Account operations")
    class Accounts:
        def list(self, ctx: CommandContext):
            return ctx.session.get(f"{ctx.base_url}/accounts").json()

        def detail(self, ctx: CommandContext, id: int):
            return ctx.session.get(f"{ctx.base_url}/accounts/{id}").json()
```

This creates CLI commands: `gp bank accounts list` and `gp bank accounts detail <id>`.

### Nesting Groups with `parent=`

Command groups can be nested using the `parent=` parameter:

```python
@command(help="Account operations")
class Accounts:
    def list(self, ctx: CommandContext): ...

    def detail(self, ctx: CommandContext, id: int): ...


@command(help="Account statements", parent=Accounts)
class Statements:
    def download(self, ctx: CommandContext, account_id: int): ...

    def summary(self, ctx: CommandContext, account_id: int): ...
```

This creates: `gp bank accounts list`, `gp bank accounts detail <id>`, `gp bank accounts statements download <account_id>`, etc.

### How It Works

- `@command` on a class stores `CommandGroupMeta` (name, help text, parent reference).
- `@command` on a method within a class stores `CommandMetadata` as usual.
- Methods starting with `_` are skipped during auto-discovery.
- `CommandSpec.group` links a command to its parent group name.
- `SitePlugin.get_commands()` flattens groups into `CommandSpec` objects with the `group` field set.

---

## Core Types Reference

| Type | Module | Frozen | Purpose |
|------|--------|--------|---------|
| `GraftpunkClient` | `client` | No | Stateful Python API client for a single plugin. Context-manager-friendly. |
| `SitePlugin` | `plugins.cli_plugin` | N/A (class) | Base class for Python plugins |
| `CommandContext` | `plugins.cli_plugin` | No | Execution context: `session`, `plugin_name`, `command_name`, `api_version`, `base_url`, `config`, `observe` |
| `CommandResult` | `plugins.cli_plugin` | Yes | Optional structured return: `data`, `metadata`, `format_hint` |
| `CommandSpec` | `plugins.cli_plugin` | Yes | Command spec: `name`, `handler`, `help_text`, `params`, `timeout`, `max_retries`, `rate_limit`, `requires_session`, `group` |
| `CommandMetadata` | `plugins.cli_plugin` | Yes | Metadata stored by `@command` decorator on methods |
| `CommandGroupMeta` | `plugins.cli_plugin` | Yes | Metadata stored by `@command` decorator on classes (command groups) |
| `LoginConfig` | `plugins.cli_plugin` | Yes | Declarative browser login configuration: `steps`, `url`, `wait_for`, `failure`, `success`, `success_url`, `headless`, `timeout`, `settle` |
| `LoginStep` | `plugins.cli_plugin` | Yes | Single step in a login flow: `fields`, `submit`, `wait_for`, `delay` |
| `PluginConfig` | `plugins.cli_plugin` | Yes | Canonical config: `site_name`, `session_name`, `help_text`, `base_url`, `requires_session`, `backend`, `api_version`, `username_envvar`, `password_envvar`, `login_config`, `plugin_version`, `plugin_author`, `plugin_url` |
| `PluginParamSpec` | `plugins.cli_plugin` | Yes | CLI parameter specification |
| `CLIPluginProtocol` | `plugins.cli_plugin` | N/A (Protocol) | Structural typing contract for all plugins |
| `OutputConfig` | `plugins.output_config` | Yes | Multi-view output configuration: `views`, `default_view`. Provides `filter_views()` and `get_default_view()` |
| `ViewConfig` | `plugins.output_config` | Yes | Single named view: `name`, `path`, `title`, `columns`, `display` |
| `ColumnFilter` | `plugins.output_config` | Yes | Column include/exclude filter: `mode`, `columns` |
| `OutputFormatter` | `plugins.formatters` | N/A (Protocol) | Protocol for output formatters: `name` property, `format()` method |
| `CommandError` | `exceptions` | N/A | Expected command failure with user-facing message |
| `PluginError` | `exceptions` | N/A | Infrastructure/plugin loading failure |
| `ObservabilityContext` | `observe.context` | No | Plugin-facing observability handle |
| `NoOpObservabilityContext` | `observe.context` | No | Null object for disabled observability |
| `CaptureBackend` | `observe.capture` | N/A (Protocol) | Protocol for browser capture |
| `ObserveStorage` | `observe.storage` | No | File-based observability storage |
| `GraftpunkSession` | `graftpunk_session` | No | `requests.Session` subclass with browser header roles |
| `BrowserCookiePolicy` | `cookies` | No | The standard library's cookie policy plus Chrome's loopback `Secure` rule and host-only scoping |
| `BrowserCookiesMixin` | `cookies` | N/A (mixin) | Mix in ahead of `requests.Session` so each request's `Cookie` header is computed with the jar's policy |
| `BrowserCookieSession` | `cookies` | No | A `requests.Session` with `BrowserCookiesMixin` |
| `BrowserSession` | `session` | No | Browser automation wrapper |
| `Token` | `tokens` | Yes | Token extraction configuration |
| `TokenConfig` | `tokens` | Yes | Collection of token extraction rules |
| `CachedToken` | `tokens` | Yes | Extracted token value with TTL |
| `PluginDiscoveryError` | `cli.plugin_commands` | Yes | Error during plugin discovery |
| `YAMLDiscoveryError` | `plugins.yaml_loader` | Yes | Error during YAML plugin loading |
| `PythonDiscoveryError` | `plugins.python_loader` | Yes | Error during Python plugin loading |

### Key Functions

| Function | Module | Purpose |
|----------|--------|---------|
| `execute_plugin_command()` | `client` | Shared execution core with retry/rate-limit and `CommandResult` normalization. Used by the CLI callback. |
| `discover_all_plugins()` | `plugins` | Cached discovery across all sources (entry points, YAML, Python files). Returns tuple of plugins. |
| `get_plugin()` | `plugins` | Look up a single plugin by `site_name`. Raises `PluginError` if unknown. |
| `format_output()` | `plugins.formatters` | Format and print data. Resolves formatter, unwraps `CommandResult`, applies `--view` filtering. |
| `discover_formatters()` | `plugins.formatters` | Discover built-in + entry-point formatters. Returns dict of name → formatter. |
| `write_rendered_to_file()` | `plugins.formatters` | Render with a colour-less 200-column Rich console and write the result to a file (UTF-8). |
| `stream_is_tty()` | `console` | Whether a console's stream is an interactive terminal; `False` for a closed stream, a `None` stdout or no `isatty`. |
| `cookie_from_browser()` | `cookies` | A jar cookie for one cookie a browser reported (a WebDriver dict or a CDP `Network.Cookie`), with its domain, path, `Secure` flag, expiry, HttpOnly flag, host-only scope and, for a host-only cookie, the host that set it. |
| `use_browser_cookie_policy()` | `cookies` | Give a jar graftpunk did not create a `BrowserCookiePolicy`, keeping its settings, unless a program chose its policy. Returns the jar. |
| `importing_httpie()` | `cookies` | Context manager: import HTTPie modules inside it and leave `http.cookiejar.DefaultCookiePolicy` as it was. |
| `get_downloads_dir()` | `plugins.formatters` | Resolve download directory for file-based formatters (XLSX). Uses `GP_DOWNLOADS_DIR` or `./gp-downloads/`. |

---

## Security

- **Session encryption** — All cached sessions are encrypted with Fernet (AES-128-CBC + HMAC-SHA256). Sessions are authenticated before decryption.
- **Checksum verification** — SHA256 checksums guard against data corruption.
- **TTL expiration** — Sessions expire after a configurable time (`GRAFTPUNK_SESSION_TTL_HOURS`).
- **Path validation** — Observability storage validates session names and run IDs against a strict allowlist pattern to prevent path traversal. Screenshot labels are sanitized.
- **Narrow exception handling** — Selenium operations catch `WebDriverException` specifically, not bare `Exception`. This prevents masking programming errors.
- **Frozen value types** — Configuration and metadata types (`PluginConfig`, `LoginConfig`, `LoginStep`, `CommandMetadata`, `PluginParamSpec`, `OutputConfig`, `ViewConfig`, `ColumnFilter`, discovery error types) are frozen dataclasses with tuple container fields, preventing accidental mutation after construction.
- **Local-only by default** — Sessions are stored on the local filesystem. The Supabase backend is opt-in.

**Note:** Session deserialization uses `dill` (pickle). Only load sessions from trusted sources — this is by design for local-machine use.
