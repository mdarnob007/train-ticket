from __future__ import annotations

import logging
import time
import sys
from datetime import datetime
from pathlib import Path

# When running this file directly (python session.py) ensure the project
# root is on sys.path so imports like `from app.config import settings`
# resolve. The project root is two levels above this file.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from selenium.webdriver.remote.webdriver import WebDriver

from app.config import settings
from app.models.job import AuthBundle, BookingJob, RunState
from app.models.selectors import sel
from app.rpa import driver as dv
from app.rpa.api_client import ApiClient

log = logging.getLogger("rpa.session")


def is_logged_in(driver: WebDriver) -> bool:
    """Check for an auth token, then prove it is still accepted by the server.

    A token sitting in storage is not proof of a live session: the profile keeps
    an expired one after the server session dies, and a merely-present token made
    the SPA render stale pages (empty station/class dropdowns, deep links
    bouncing to /login). The cheapest authenticated call is the search endpoint,
    so use it as the liveness probe.
    """
    bundle = dv.harvest_auth(driver)
    if not bundle.token:
        return False
    if _token_live(bundle):
        return True
    log.info("Stored token rejected by the server (stale session) - logging in again")
    return False


def _token_live(bundle: object, timeout: float = 12.0) -> bool:
    """True when the server accepts the stored token. Never raises."""
    from app.rpa.api_client import ApiClient, ApiError

    try:
        ApiClient(bundle, timeout=timeout).search_trips(  # type: ignore[arg-type]
            "Dhaka", "Chattogram", datetime.now().strftime(settings.date_format), "AC_S"
        )
        return True
    except ApiError as exc:
        status = getattr(exc, "status", None)
        if status in (401, 403):
            return False
        # Rate limiting / transport hiccup is not proof the token is dead.
        log.warning("Session probe inconclusive (%s: %s) - treating session as live", status, exc)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Session probe failed (%s) - treating session as live", exc)
        return True


def _locator_ready(name: str, spec: str | None) -> bool:
    if not spec:
        log.warning("Selector %s not discovered yet (run Phase 2 discovery first)", name)
        return False
    return True


def ensure_session(
    driver: WebDriver,
    job: BookingJob,
    otp_timeout: float = 120.0,
    otp_gate=None,
) -> bool:
    """Make sure we are logged in.

    Returns True when a valid session exists. Uses the persistent Chrome profile,
    so usually the previous session is simply reused and login is skipped. When a
    fresh login is needed the sign-in API is tried first (it now mirrors the SPA
    exactly - pre-auth handshake + the SPA-minted device id, then the server-issued
    device key - and search/review accept those sessions, verified end-to-end).
    The SPA form is the last resort for when Cloudflare hard-blocks the API. The
    railway login on this account never asks for an SMS OTP; `otp_timeout`/
    `otp_gate` are kept for API/probe callers but unused.
    """
    job.set_state(RunState.WAIT_CF, "Loading site, waiting for Cloudflare check")
    driver.get(settings.base_url)
    dv.wait_for_cloudflare(driver)

    if is_logged_in(driver):
        job.auth = dv.harvest_auth(driver)
        job.set_state(RunState.LOGIN, "Session reused from profile (no login needed)")
        log.info("Session reused: token present, login skipped")
        if sel.LOGGED_IN_INDICATOR:
            try:
                dv.wait_visible(driver, sel.LOGGED_IN_INDICATOR, timeout=10)
            except Exception:
                pass
        return True

    # A token was present but the server rejected it. Purge it, or the SPA keeps
    # bouncing /login back to the home page and we can never mint a Turnstile
    # token for the sign-in API.
    #
    # The server has already proven this token is worthless, so clearing the
    # SPA's own storage keys costs the user nothing even when we are attached to
    # their real browser. We still skip the browser-wide cookie wipe there:
    # that would sign them out of every other site for no benefit.
    if dv.is_attached():
        log.info(
            "Stored token was rejected by the server - clearing this site's auth "
            "keys (leaving your cookies and other sites alone) so /login can render"
        )
        dv.clear_auth_storage(driver, cookies=False)
    else:
        dv.clear_auth_storage(driver)
        log.info("Purged stale auth storage so the login page can render")

    api_error = None
    try:
        if login_via_api(driver, job):
            return True
    except Exception as exc:  # 429/CF stalls can still happen; fall through to the form
        api_error = exc

    if _login(driver, job):
        return True

    job.set_state(RunState.FAILED, "Login failed on both the sign-in API and the SPA form")
    # If /login still refuses to render, that is the real cause and the user can
    # only fix it by hand - say so instead of surfacing a generic login failure.
    if _login_page_blocked(driver):
        raise RuntimeError(
            "Login failed and the login page is not rendering either. The stale "
            "auth state could not be cleared from this browser. Open "
            f"{settings.base_url}{settings.login_path} in that Chrome window, log in "
            "manually, then re-run."
        )
    if api_error is not None:
        raise api_error
    return False


def _login_page_blocked(driver: WebDriver, timeout: int = 15) -> bool:
    """True when the login page refuses to render because a stale token remains.

    The SPA's route guard reads localStorage["user"]; while a dead token is
    still stored it treats the SPA as signed in and redirects /login back to the
    home page, which has no Turnstile widget - so neither the sign-in API nor
    the form can authenticate. Detecting that here turns a confusing 90-second
    timeout into an actionable message.
    """
    from selenium.webdriver.common.by import By

    by, value = dv.locator(sel.LOGIN_MOBILE_INPUT)
    login_path = settings.login_path.rstrip("/")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            driver.get(settings.base_url + settings.login_path)
            dv.wait_ready(driver, timeout=5)
            on_login = driver.current_url.rstrip("/").endswith(login_path)
            if driver.find_elements(by, value) or on_login:
                if dv.field_present(driver, sel.LOGIN_MOBILE_INPUT, timeout=1) or driver.find_elements(
                    By.TAG_NAME, "button"
                ):
                    return False
        except Exception as exc:
            # A page that never reaches readyState (or a detached session) is
            # simply "not rendered yet" - keep probing until the deadline.
            log.debug("Login page probe failed (%s); retrying", exc)
        time.sleep(1)
    return True


def login_via_api(driver: WebDriver, job: BookingJob) -> bool:
    """Browser-assisted API login: POST sign-in with a freshly minted Turnstile token.

    Sign-in still needs `cft_response`, the Cloudflare Turnstile token that the
    login page mints, so a live page (or at least a Turnstile widget) is always
    required. The credentials come from `.env`. The device id/key pair is reused
    from the profile when available (the SPA establishes it before auth), else a
    fresh pair is generated client-side - the server binds the token to whatever
    pair was presented at sign-in.

    Returns True on success. The token + device pair are written back into the
    profile's storage so the session is reused on later runs too.
    """
    if not settings.railway_user or not settings.railway_pass:
        job.set_state(RunState.FAILED, "RAILWAY_USER/RAILWAY_PASS missing in .env")
        return False

    # The SPA fingerprint-mints its own `uudid` (X-Device-Id) on every boot and
    # ignores anything we write into storage - signing in with a different id
    # binds the server-issued ssdk to a device the SPA never uses, and the
    # bookings API then rejects search with "Please login first" (401). So boot
    # the app first and harvest the uudid it mints, like the real login does.
    deadline = time.time() + 20
    device_id = dv.harvest_auth(driver).device_id
    while not device_id and time.time() < deadline:
        if not driver.current_url.endswith(settings.login_path.lstrip("/")):
            driver.get(settings.base_url + settings.login_path)
            dv.wait_for_cloudflare(driver)
        device_id = dv.harvest_auth(driver).device_id
        if not device_id:
            time.sleep(1)
    if not device_id:
        job.set_state(RunState.FAILED, "SPA did not mint a uudid; cannot sign in")
        return False

    cft_response = dv.harvest_turnstile_token(driver, timeout=15)
    if not cft_response:
        driver.get(settings.base_url + settings.login_path)
        dv.wait_for_cloudflare(driver)
        cft_response = dv.harvest_turnstile_token(driver, timeout=15)
    if not cft_response:
        job.set_state(RunState.LOGIN, "No Turnstile token minted - falling back to form login")
        return False

    job.set_state(RunState.LOGIN, "Logging in via Shohoz sign-in API")
    # Pre-auth handshake with hash "" (no device key yet) - the SPA does this
    # right before sign-in and the bookings API remembers it in the session.
    pre_handshake = ApiClient(AuthBundle(device_id=device_id)).handshake("")
    pre_hash = str(((pre_handshake or {}).get("meta") or {}).get("hash") or "")

    client = ApiClient(AuthBundle(device_id=device_id, device_key=""))
    auth = client.sign_in_auth(
        settings.railway_user,
        settings.railway_pass,
        cft_response,
        device_id=device_id,
        device_key="",
    )
    # Post-auth handshake: now with Bearer + the device key the server just
    # issued, echoing the hash from the pre-auth handshake - same order/headers
    # as a real SPA login (captured 2026-09-24).
    hs = ApiClient(auth).handshake(pre_hash) or {}
    auth.handshake_hash = str((hs.get("meta") or {}).get("hash") or pre_hash)
    auth.handshake_data = hs if isinstance(hs, dict) else None
    job.auth = auth
    dv.set_auth_storage(driver, auth)
    job.set_state(RunState.LOGIN, "API login successful")
    log.info("API login successful for %s", settings.railway_user)
    return True


def _login(driver: WebDriver, job: BookingJob) -> bool:
    if not _locator_ready("LOGIN_MOBILE_INPUT", sel.LOGIN_MOBILE_INPUT) or not _locator_ready(
        "LOGIN_PASSWORD_INPUT", sel.LOGIN_PASSWORD_INPUT
    ):
        raise RuntimeError(
            "Login selectors are not configured. Run the Phase 2 discovery "
            "flow to capture the live DOM, then fill app/models/selectors.py."
        )

    if not settings.railway_user or not settings.railway_pass:
        job.set_state(RunState.FAILED, "RAILWAY_USER/RAILWAY_PASS missing in .env")
        raise RuntimeError("RAILWAY_USER/RAILWAY_PASS not set in .env")

    job.set_state(RunState.LOGIN, f"Logging in as {settings.railway_user}")

    mobile = password = None
    for attempt in range(1, 3):
        if attempt > 1:
            log.warning("Login form did not render; reloading /login (attempt %d/2)", attempt)
        driver.get(settings.base_url + settings.login_path)
        dv.wait_for_cloudflare(driver)
        try:
            mobile = dv.wait_visible(driver, sel.LOGIN_MOBILE_INPUT, timeout=60)
            password = dv.wait_visible(driver, sel.LOGIN_PASSWORD_INPUT, timeout=20)
            break
        except Exception:
            if attempt == 2:
                job.set_state(RunState.FAILED, "Login form not found; selectors may be stale")
                raise
    if mobile is None or password is None:
        job.set_state(RunState.FAILED, "Login form not found; selectors may be stale")
        raise RuntimeError("Login form did not render after retry")

    mobile.clear()
    mobile.send_keys(settings.railway_user)
    password.clear()
    password.send_keys(settings.railway_pass)

    # Turnstile solves a few seconds after the form renders. Submitting before
    # the widget has a token makes the SPA drop the request with no visible
    # error, so gate the click on the token actually being present.
    if not dv.harvest_turnstile_token(driver, timeout=20):
        log.warning("Login form submitted with no Turnstile token; it may be rejected")
    else:
        log.debug("Turnstile token ready for login submit")

    if sel.LOGIN_SUBMIT_BUTTON:
        dv.wait_visible(driver, sel.LOGIN_SUBMIT_BUTTON).click()
    else:
        password.submit()

    dv.wait_for_cloudflare(driver)

    # The railway login itself never asks for an SMS OTP on this account.
    # (Every seat confirm still goes through app.rpa.otp.run_otp_gate.)

    # Wait for the auth token AND the user object to appear in storage - the SPA
    # only considers the session valid once `user` exists (its login guard). Also
    # confirm the UI logged in (user menu) so the session works outside the SPA
    # storage, not just a minted token.
    deadline = time.time() + settings.explicit_wait_secs
    while time.time() < deadline:
        if is_logged_in(driver):
            raw = dv.get_local_storage(driver)
            if raw.get("user") and _logged_in_ui(driver):
                job.auth = dv.harvest_auth(driver)
                job.set_state(RunState.LOGIN, "Login successful")
                log.info("Login successful, auth token harvested")
                return True
        time.sleep(1)

    job.set_state(RunState.FAILED, "Login did not complete (no auth token)")
    # Surface whatever the page is actually showing. A rejected submit is
    # otherwise silent, which makes this look like a selector or timing problem.
    raise RuntimeError(
        f"Login did not complete: no auth token in storage. Page: {dv.page_summary(driver)}"
    )


def _logged_in_ui(driver: WebDriver) -> bool:
    """True when the SPA header shows the logged-in user menu.

    The anonymous header shows a "Login | Register" link; a logged-in header
    shows the `railway-logged-user` dropdown (user name + Logout). Text like
    "Verify Ticket" is public chrome and must NOT be used as a marker. A minted
    token alone is not enough: API-minted tokens fail here while still passing
    storage checks, which is exactly the 401 symptom we're avoiding.
    """
    if sel.LOGGED_IN_INDICATOR:
        try:
            dv.wait_visible(driver, sel.LOGGED_IN_INDICATOR, timeout=10)
            return True
        except Exception:
            pass
    # Fallback: the SPA dispatches the stored `user` into the header; look for
    # our own display_name in the page without the anonymous Login/Register link.
    try:
        body = driver.find_element("tag name", "body").text.lower()
    except Exception:
        return False
    name = _stored_display_name(driver)
    if not name:
        return False
    if "login" in body and "register" in body:
        return False
    import re

    return re.sub(r"\s+", " ", name).lower() in re.sub(r"\s+", " ", body)


def _stored_display_name(driver: WebDriver) -> str:
    try:
        raw = dv.get_local_storage(driver).get("user") or ""
    except Exception:
        return ""
    try:
        import json

        return str((json.loads(raw) or {}).get("display_name") or "")
    except Exception:
        return ""