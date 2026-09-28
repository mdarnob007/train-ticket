from __future__ import annotations

import json
import logging
import time
from typing import Any

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait

from app.config import settings
from app.models.job import AuthBundle

log = logging.getLogger("rpa.driver")

CF_BLOCKED_TITLES = {"Just a moment...", "Attention Required! | Cloudflare"}


def _profile_path(profile_dir: str | None) -> str:
    from pathlib import Path

    return str(Path(profile_dir or settings.profile_dir).resolve())


def _common_args(options: Any) -> Any:
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--start-maximized")
    options.add_argument("--lang=en-US")
    options.add_experimental_option("prefs", {"profile.default_content_setting_values.notifications": 2})
    return options


def _chrome_major_version() -> int | None:
    """Detect the installed Chrome major version (macOS Info.plist)."""
    import plistlib
    from pathlib import Path

    candidates = [
        Path("/Applications/Google Chrome.app/Contents/Info.plist"),
        Path.home() / "Applications/Google Chrome.app/Contents/Info.plist",
    ]
    for plist_path in candidates:
        try:
            with plist_path.open("rb") as fh:
                short_version = plistlib.load(fh).get("CFBundleShortVersionString", "")
            return int(str(short_version).split(".")[0])
        except Exception:
            continue
    return None


def _create_uc_driver(profile: str, extra_options: dict[str, Any] | None) -> Any:
    import undetected_chromedriver as uc

    options = _common_args(uc.ChromeOptions())
    for key, value in (extra_options or {}).items():
        options.set_capability(key, value)
    version_main = _chrome_major_version() or None
    driver = uc.Chrome(
        user_data_dir=profile,
        options=options,
        headless=settings.headless,
        version_main=version_main,
    )
    log.info("Chrome started via undetected-chromedriver (profile=%s, headless=%s, version=%s)", profile, settings.headless, version_main)
    return driver


def _create_selenium_driver(profile: str, extra_options: dict[str, Any] | None) -> WebDriver:
    options = _common_args(Options())
    options.add_argument(f"--user-data-dir={profile}")
    if settings.headless:
        options.add_argument("--headless=new")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option("detach", False)
    for key, value in (extra_options or {}).items():
        options.set_capability(key, value)

    driver = webdriver.Chrome(options=options)
    # Mask the automation marker for non-uc runs.
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    log.info("Chrome started via plain selenium (profile=%s, headless=%s)", profile, settings.headless)
    return driver


def _chrome_launch_hint() -> str:
    return (
        'Start Chrome with a dedicated profile first:\n'
        '  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\\n'
        '    --remote-debugging-port=' + settings.debugger_address.rsplit(":", 1)[-1] +
        ' \\\n    --user-data-dir="$HOME/.rpa-chrome-debug"\n'
        "  (Chrome 136+ ignores --remote-debugging-port on the default profile.)"
    )


def _create_attached_driver() -> WebDriver:
    """Attach to a Chrome the user already started with a debugging port.

    This is the most faithful option: a real, human-launched browser with its own
    profile, extension set and cookies, so nothing about the fingerprint looks
    automated. It also reuses whatever session that profile already has.

    Two consequences callers must respect:
    - `driver.quit()` would shut down the user's *whole* browser, so prefer
      `close_tab_only()`.
    - The user's own tabs must never be navigated or closed, so work happens in
      a tab we create ourselves.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://{settings.debugger_address}/json/version", timeout=5
        ) as resp:
            version = json.loads(resp.read().decode("utf-8", "ignore"))
        log.info("Attaching to existing Chrome %s at %s", version.get("Browser"), settings.debugger_address)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(
            f"no Chrome is listening on {settings.debugger_address} ({exc}).\n{_chrome_launch_hint()}"
        ) from exc

    _ensure_a_page_exists()

    options = Options()
    options.debugger_address = settings.debugger_address
    driver = webdriver.Chrome(options=options)
    # A real browser carries no automation markers, so no webdriver mask is
    # needed (and injecting one into the user's session is needless risk).
    log.info("Attached to running Chrome (profile/settings.headless ignored)")
    return driver


def _ensure_a_page_exists() -> None:
    """Make sure the attached Chrome has at least one open page.

    chromedriver attaches to a *page*, so a browser with every tab closed fails
    with "unable to discover open pages" - a normal state after the user closes
    their last tab, and not something they should have to work around. The
    DevTools HTTP endpoint can open one, which is enough for the session to be
    created; the caller then works in that tab via `open_own_tab`.
    """
    import urllib.error
    import urllib.request

    base = f"http://{settings.debugger_address}"
    try:
        with urllib.request.urlopen(f"{base}/json/list", timeout=5) as resp:
            if json.loads(resp.read().decode("utf-8", "ignore")):
                return
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(
            f"could not list pages in Chrome at {settings.debugger_address} ({exc}).\n{_chrome_launch_hint()}"
        ) from exc

    log.info("Attached Chrome has no open tabs - opening one so we can attach")
    request = urllib.request.Request(f"{base}/json/new?about:blank", method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=10):
            pass
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(
            f"Chrome at {settings.debugger_address} has no open tabs and a new one "
            f"could not be opened ({exc}). Open any tab in that window and retry."
        ) from exc


def create_driver(profile_dir: str | None = None, extra_options: dict[str, Any] | None = None) -> WebDriver:
    """Create (or attach to) a Chrome WebDriver.

    Defaults to undetected-chromedriver because plain chromedriver launches the
    browser with `--test-type=webdriver` + CDP automation markers, and
    Cloudflare Turnstile flags those markers and rejects "Verify you are human"
    clicks even from a real human. Set DRIVER_ENGINE=selenium in .env to fall
    back to plain chromedriver, or DRIVER_ENGINE=attach to drive a Chrome the
    user has already launched (highest fidelity - no automation markers at all).

    The persistent profile is what lets us reuse a logged-in session and pass
    Cloudflare's managed challenge (a real browser does the JS proof).
    `extra_options` can enable Chrome logging prefs (e.g. performance logs for
    the Phase 2 network capture).
    """
    if settings.driver_engine == "attach":
        driver = _create_attached_driver()
        driver.implicitly_wait(settings.implicit_wait_secs)
        return driver
    profile = _profile_path(profile_dir)
    if settings.driver_engine == "uc":
        driver = _create_uc_driver(profile, extra_options)
    else:
        driver = _create_selenium_driver(profile, extra_options)
    driver.implicitly_wait(settings.implicit_wait_secs)
    return driver


def locator(spec: str) -> tuple[str, str]:
    """Expand a locator string like 'css.trip-collapsible', 'id#x' to (By, value).

    `css` is a bare prefix with no separating dot: whatever follows is the CSS
    selector verbatim. So both `css.trip-collapsible` (-> `.trip-collapsible`)
    and `cssbutton.book-now-btn` (-> `button.book-now-btn`) resolve correctly.
    Requiring a literal `css.` here instead would turn every class selector
    into a bare element name and pass `css`-prefixed ones through unstripped.
    """
    if spec.startswith("xpath//"):
        # Strip only the `xpath` prefix: the `//` is part of the expression and
        # dropping it would leave a *relative* XPath (`input[...]`) that silently
        # matches nothing outside the document element.
        return By.XPATH, spec[5:]
    if spec.startswith("id#"):
        return By.ID, spec[3:]
    if spec.startswith("name="):
        return By.NAME, spec[5:]
    if spec.startswith("tag="):
        return By.TAG_NAME, spec[4:]
    if spec.startswith("css"):
        return By.CSS_SELECTOR, spec[3:]
    return By.CSS_SELECTOR, spec


def wait_ready(driver: WebDriver, timeout: int | None = None) -> None:
    """Wait until document.readyState is 'complete'."""
    timeout = timeout or settings.explicit_wait_secs
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready = driver.execute_script("return document.readyState")
        if ready == "complete":
            return
        time.sleep(0.5)
    raise TimeoutError(f"document.readyState never reached 'complete' within {timeout}s")


CF_CHECK_JS = """
const title = (document.title || '').toLowerCase();
const blocked = ['just a moment', 'attention required', 'checking your browser'];
if (blocked.some(t => title.includes(t))) return true;
return !!document.querySelector('#challenge-running, #challenge-form, #challenge-stage');
"""


def wait_for_cloudflare(driver: WebDriver, timeout: int | None = None) -> None:
    """Wait out a Cloudflare managed challenge / JS interstitial.

    A real headed browser solves a managed (non-interactive) challenge on its
    own within seconds; we just poll until it clears. Interactive Turnstile
    widgets embedded in the app are NOT treated as blockers (they belong to the
    page and emit the cft_response token we harvest later).

    An interactive "Verify you are human" checkbox can stall without a click,
    so after ~20s we start hinting that the user may need to solve it in the
    visible browser. The wallet doesn't error out - it waits out the full
    timeout so a slow-but-automatic challenge still passes.
    """
    timeout = timeout or settings.explicit_wait_secs + 60
    deadline = time.time() + timeout
    reported_at = 0.0
    hinted_at = time.time() + 20
    while time.time() < deadline:
        try:
            blocked = driver.execute_script(CF_CHECK_JS)
            if not blocked:
                return
        except Exception:
            pass
        now = time.time()
        if now - reported_at > 5:
            log.info("Waiting for Cloudflare challenge to clear...")
            reported_at = now
        if now > hinted_at:
            log.info(
                "Cloudflare is still holding - if a 'Verify you are human' "
                "checkbox is showing in that Chrome window, tick it (the bot "
                "takes over once it clears)"
            )
            hinted_at = now + 20
        time.sleep(1)
    try:
        title = driver.title
    except Exception:
        title = "?"
    raise TimeoutError(
        f"Cloudflare challenge did not clear within {timeout}s "
        f"(current page title: {title!r}). If a 'Verify you are human' checkbox "
        "appeared in Chrome, tick it and re-run."
    )


def wait_visible(driver: WebDriver, spec: str, timeout: int | None = None) -> WebElement:
    timeout = timeout or settings.explicit_wait_secs
    by, value = locator(spec)
    return WebDriverWait(driver, timeout).until(
        lambda d: d.find_element(by, value) if d.find_elements(by, value) else False
    )


def field_present(driver: WebDriver, spec: str, timeout: float = 3.0) -> bool:
    """True if an element matching `spec` appears within `timeout` seconds."""
    deadline = time.time() + timeout
    by, value = locator(spec)
    while time.time() < deadline:
        if driver.find_elements(by, value):
            return True
        time.sleep(0.3)
    return False


def fill_split_input(driver: WebDriver, spec: str, value: str, box_digits: bool = False) -> None:
    """Fill one text input with `value`, or a row of per-digit boxes.

    Some pages render OTP as N small inputs (one char each). When multiple
    elements match `spec`, each gets one character of `value` in DOM order.
    """
    by, value_spec = locator(spec)
    els = driver.find_elements(by, value_spec)
    if not els:
        raise TimeoutError(f"no element matched '{spec}'")
    if len(els) == 1:
        els[0].clear()
        els[0].send_keys(value)
        return
    for idx, box in enumerate(els[: len(value)]):
        box.clear()
        box.send_keys(value[idx])


def wait_any(driver: WebDriver, specs: list[str], timeout: int | None = None) -> WebElement:
    """Return the first element matching any of the given locators."""
    timeout = timeout or settings.explicit_wait_secs
    deadline = time.time() + timeout
    while time.time() < deadline:
        for spec in specs:
            by, value = locator(spec)
            elements = driver.find_elements(by, value)
            if elements:
                return elements[0]
        time.sleep(0.5)
    raise TimeoutError(f"None of {specs} matched within {timeout}s")


def get_local_storage(driver: WebDriver) -> dict[str, Any]:
    try:
        return driver.execute_script(
            "let o={}; for (let i=0;i<localStorage.length;i++){let k=localStorage.key(i); o[k]=localStorage.getItem(k);} return o;"
        )
    except Exception:
        return {}


def get_session_storage(driver: WebDriver) -> dict[str, Any]:
    try:
        return driver.execute_script(
            "let o={}; for (let i=0;i<sessionStorage.length;i++){let k=sessionStorage.key(i); o[k]=sessionStorage.getItem(k);} return o;"
        )
    except Exception:
        return {}


AUTH_STORAGE_KEYS = (
    "token",
    "uudid",
    "x-device-id",
    "ssdk",
    "x-device-key",
    "user",
    "handshake_data",
    "handshake_hash",
)


def clear_auth_storage(driver: WebDriver, cookies: bool = True) -> None:
    """Purge the SPA's persisted auth keys (and optionally the cookies).

    A dead token left in the profile actively blocks re-login. The SPA's route
    guard reads `localStorage["user"]`, so as long as a stale token/user pair is
    present it treats the SPA as signed in and redirects `/login` back to the
    home page - a page with no Turnstile widget, so the sign-in API can never
    mint the `cft_response` it requires and the run dies at login. Purging
    storage makes the login route render (and mint Turnstile) again.

    `cookies=False` is for attach mode. Removing the storage keys above is
    enough to unblock `/login`, and it touches only this origin's own keys.
    `delete_all_cookies()` is browser-wide, so in someone else's browser it
    would sign them out of every other site - a real cost with no benefit here.
    """
    driver.execute_script(
        "const ks=arguments[0];"
        "for(const k of ks){try{localStorage.removeItem(k);}catch(e){}"
        "try{sessionStorage.removeItem(k);}catch(e){}}",
        list(AUTH_STORAGE_KEYS),
    )
    if not cookies:
        return
    try:
        driver.delete_all_cookies()
    except Exception:
        pass


def js_click(driver: WebDriver, element: WebElement) -> None:
    """Click via JS, bypassing overlays that intercept the real click.

    The SPA puts a sticky legal-notice sheet over the search form, so a normal
    Selenium click can fail with ElementClickIntercepted even though the target
    is the element we want.
    """
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
    driver.execute_script("arguments[0].click();", element)


def page_summary(driver: WebDriver, limit: int = 240) -> str:
    """A short snapshot of what the page is showing, for failure messages.

    Search/booking failures on this SPA surface as a toast or an empty results
    list rather than an exception, so the visible text is the only clue.
    """
    try:
        text = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    except Exception as exc:  # noqa: BLE001
        return f"<could not read page: {exc}>"
    return " ".join(text.split())[:limit]


def is_attached() -> bool:
    """True when driving a browser the user launched themselves."""
    return settings.driver_engine == "attach"


def open_own_tab(driver: WebDriver) -> str:
    """Open a fresh tab and make it the active one; return its window handle.

    In attach mode this is how we stay out of the user's way - every later
    navigation happens in this tab, so their existing tabs are never touched.
    """
    original = driver.current_window_handle
    driver.switch_to.new_window("tab")
    log.info("Opened a dedicated tab (handle=%s)", driver.current_window_handle[:12])
    return original


def close_tab_only(driver: WebDriver) -> None:
    """Close the tab we opened, never the whole browser.

    `driver.quit()` on an attached browser terminates the user's Chrome along
    with every tab they have open, so attach mode must close just our window.
    """
    if is_attached():
        try:
            driver.close()
            log.info("Closed our tab; the rest of your Chrome is untouched")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not close our tab: %s", exc)
        return
    driver.quit()


def harvest_auth(driver: WebDriver) -> AuthBundle:
    """Extract token + device headers the SPA stores for railspaapi calls.

    Key names were identified from community tools using the same platform
    (`token`, `x-device-key`/`ssdk`, `x-device-id`/`uudid`); the discovery run
    confirms the exact names on this SPA.
    """
    storage = {**get_local_storage(driver), **get_session_storage(driver)}
    token = storage.get("token") or ""
    device_id = storage.get("uudid") or storage.get("x-device-id") or ""
    device_key = storage.get("ssdk") or storage.get("x-device-key") or ""
    user_raw = storage.get("user") or ""
    user = json.loads(user_raw) if user_raw else None
    handshake_hash = storage.get("handshake_hash") or ""
    return AuthBundle(
        token=token,
        device_id=device_id,
        device_key=device_key,
        user=user if isinstance(user, dict) else None,
        handshake_hash=handshake_hash,
        raw_storage=storage,
    )


def set_auth_storage(driver: WebDriver, auth: AuthBundle) -> None:
    """Persist token + device pair into the profile so future runs reuse it.

    Writes both the prefixed and community-known key names, mirroring what the
    SPA itself does after a normal login. `user` and the handshake keys are also
    written when present - the SPA's login guard reads `localStorage["user"]`
    and without it every page redirects to the login screen.
    """
    user = json.dumps(auth.user, ensure_ascii=False) if auth.user else "{}"
    handshake_data = json.dumps(auth.handshake_data, ensure_ascii=False) if auth.handshake_data else ""
    driver.execute_script(
        "try{localStorage.setItem('token', arguments[0]);"
        "localStorage.setItem('uudid', arguments[1]);localStorage.setItem('x-device-id', arguments[1]);"
        "localStorage.setItem('ssdk', arguments[2]);localStorage.setItem('x-device-key', arguments[2]);"
        "localStorage.setItem('user', arguments[3]);"
        "if(arguments[4]){localStorage.setItem('handshake_data', arguments[4]);}"
        "if(arguments[5]){localStorage.setItem('handshake_hash', arguments[5]);}}catch(e){}",
        auth.token,
        auth.device_id,
        auth.device_key,
        user,
        handshake_data,
        auth.handshake_hash or "",
    )


def harvest_turnstile_token(driver: WebDriver, timeout: float = 10.0) -> str:
    """Return a fresh Cloudflare Turnstile response token from the current page.

    The widget auto-executes on render; this hooks the render callback and polls
    both the callback value and the token-carrying hidden inputs until the token
    appears or `timeout` passes. Turnstile tokens are single-use and host-bound,
    so mint and use them within seconds on the same page load.

    Deliberately does NOT call `window.turnstile.execute()`. Forcing an immediate
    execute while the widget is still initialising makes Cloudflare withhold the
    challenge: the hidden input is then created but never filled, and it stays
    empty for the rest of that page load, so every later mint on the same page
    returns '' too. Waiting for the automatic solve is both simpler and the only
    thing that reliably works.

    Note: uses the Selenium async callback (arguments[last]) - undone
    chromedriver does not resolve returned Promises.
    """
    js = """var done = arguments[arguments.length - 1];
try {
  if (window.turnstile) {
    window.turnstile.render = (orig => (el, opts) => {
      const successCb = opts && opts.callback;
      opts = {...opts, callback: (token) => {
        window.__lastCfToken = token;
        if (typeof successCb === 'function') successCb(token);
      }};
      return orig.call(window.turnstile, el, opts);
    })(window.turnstile.render);
  }
} catch (e) {}
var start = Date.now();
(function tick() {
  try {
    if (window.__lastCfToken) return done(window.__lastCfToken);
    var inputs = document.querySelectorAll('input[name="cf-turnstile-response"], input.cf-turnstile');
    for (var i = 0; i < inputs.length; i++) { if (inputs[i].value) return done(inputs[i].value); }
  } catch (e2) {}
  if (Date.now() - start > %TIMEOUT_MS%) return done('');
  setTimeout(tick, 400);
})();
"""
    try:
        val = driver.execute_async_script(js.replace("%TIMEOUT_MS%", str(int(timeout * 1000))))
        return str(val or "")
    except Exception:
        return ""


def wait_for_new_window(driver: WebDriver, known_handles: set[str], timeout: int | None = None) -> str:
    timeout = timeout or settings.explicit_wait_secs
    deadline = time.time() + timeout
    while time.time() < deadline:
        handles = set(driver.window_handles)
        fresh = handles - known_handles
        if fresh:
            return fresh.pop()
        time.sleep(0.3)
    raise TimeoutError("No new window/tab appeared")


def switch_to_window(driver: WebDriver, handle: str) -> None:
    driver.switch_to.window(handle)
    wait_ready(driver)