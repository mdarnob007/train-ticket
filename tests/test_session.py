from __future__ import annotations

import pytest

from app.models.job import AuthBundle, BookingJob, JobConfig
from app.rpa import driver as dv
from app.rpa import otp as otp_mod
from app.rpa import session as session_mod

_REAL_LOGIN_PAGE_BLOCKED = session_mod._login_page_blocked


class _FakeElement:
    def click(self) -> None:
        self.clicked = True


class _FakeDriver:
    def __init__(self) -> None:
        self.visits = []
        self.scripts = []
        self.cookies_cleared = False

    def get(self, url: str) -> None:
        self.visits.append(url)

    def find_elements(self, by, value):
        return [_FakeElement()]

    def execute_script(self, script, *args):
        self.scripts.append(script)
        return None

    def delete_all_cookies(self) -> None:
        self.cookies_cleared = True


class _FakeGate:
    def __init__(self) -> None:
        self.wait_calls = 0
        self.value = "1234"

    def wait(self, timeout: float | None = None) -> str | None:
        self.wait_calls += 1
        return self.value

    def reset(self) -> None:
        pass


def _job() -> BookingJob:
    return BookingJob(JobConfig(train_name="X", from_city="A", to_city="B", date="03-Oct-2026"))


@pytest.fixture(autouse=True)
def _never_touch_a_real_page(monkeypatch):
    """Stub the login-page probe by default.

    It navigates and waits on document.readyState, so leaving it live makes any
    test that reaches the stale-token path burn explicit_wait_secs per call.
    `real_login_page_blocked` exposes the genuine one to the tests below.
    """
    monkeypatch.setattr(session_mod, "_login_page_blocked", lambda d, timeout=15: False)


@pytest.fixture
def real_login_page_blocked(monkeypatch):
    """Undo the autouse stub so the probe itself can be tested."""
    real = _REAL_LOGIN_PAGE_BLOCKED
    monkeypatch.setattr(session_mod, "_login_page_blocked", real)
    return real


def test_run_otp_gate_uses_injected_gate(monkeypatch):
    job = _job()
    filled = []
    monkeypatch.setattr(dv, "field_present", lambda driver, spec, timeout=0: True)
    monkeypatch.setattr(dv, "fill_split_input", lambda driver, spec, code: filled.append(code))
    monkeypatch.setattr(dv, "wait_visible", lambda driver, spec, timeout=0: _FakeElement())
    monkeypatch.setattr(dv, "locator", lambda spec: ("css selector", spec))
    gate = _FakeGate()
    ok = otp_mod.run_otp_gate(_FakeDriver(), job, "css.field", "css.submit", gate=gate, detect_secs=1)
    assert ok is True
    assert filled == ["1234"]
    assert gate.wait_calls == 1


def test_ensure_session_reuses_token(monkeypatch):
    job = _job()
    driver = _FakeDriver()
    monkeypatch.setattr(session_mod.dv, "wait_for_cloudflare", lambda d, timeout=None: None)
    monkeypatch.setattr(
        session_mod.dv,
        "harvest_auth",
        lambda d: AuthBundle(token="TOK", device_id="DEV", device_key="KEY"),
    )
    # A stored token is only reused once the server confirms it is still live.
    monkeypatch.setattr(session_mod, "_token_live", lambda bundle, timeout=12.0: True)
    ok = session_mod.ensure_session(driver, job)
    assert ok is True
    assert job.auth.token == "TOK"
    assert driver.visits[0].startswith("https://")


def test_ensure_session_relogins_when_stored_token_is_stale(monkeypatch):
    """A token left in storage by a dead session must not be reused."""
    job = _job()
    driver = _FakeDriver()
    # Pin the engine so this covers the standalone-browser path regardless of
    # what .env happens to say.
    monkeypatch.setattr(session_mod.dv.settings, "driver_engine", "uc")
    monkeypatch.setattr(session_mod.dv, "wait_for_cloudflare", lambda d, timeout=None: None)
    monkeypatch.setattr(
        session_mod.dv,
        "harvest_auth",
        lambda d: AuthBundle(token="STALE", device_id="DEV", device_key="KEY"),
    )
    monkeypatch.setattr(session_mod, "_token_live", lambda bundle, timeout=12.0: False)
    monkeypatch.setattr(session_mod, "login_via_api", lambda d, j: True)
    ok = session_mod.ensure_session(driver, job)
    assert ok is True
    # The stale token must be purged, otherwise the SPA keeps redirecting
    # /login to the home page and no Turnstile token can be minted.
    assert driver.cookies_cleared is True
    assert any("removeItem" in s for s in driver.scripts)


def test_ensure_session_clears_only_site_storage_when_attached(monkeypatch):
    """Attaching clears this site's dead auth keys, but never the user's cookies.

    The server has already rejected the token, so removing the SPA's own storage
    keys costs the user nothing and is what unblocks /login. The cookie wipe is
    browser-wide and would sign them out of unrelated sites, so it stays off.
    """
    job = _job()
    driver = _FakeDriver()
    monkeypatch.setattr(session_mod.dv.settings, "driver_engine", "attach")
    monkeypatch.setattr(session_mod.dv, "wait_for_cloudflare", lambda d, timeout=None: None)
    monkeypatch.setattr(
        session_mod.dv,
        "harvest_auth",
        lambda d: AuthBundle(token="STALE", device_id="DEV", device_key="KEY"),
    )
    monkeypatch.setattr(session_mod, "_token_live", lambda bundle, timeout=12.0: False)
    monkeypatch.setattr(session_mod, "login_via_api", lambda d, j: True)
    assert session_mod.ensure_session(driver, job) is True
    assert any("removeItem" in s for s in driver.scripts)
    assert driver.cookies_cleared is False


def test_is_logged_in_false_when_no_token(monkeypatch):
    monkeypatch.setattr(
        session_mod.dv, "harvest_auth", lambda d: AuthBundle(token="", device_id="", device_key="")
    )
    assert session_mod.is_logged_in(_FakeDriver()) is False


def test_ensure_session_tries_api_first_then_form(monkeypatch):
    job = _job()
    driver = _FakeDriver()
    monkeypatch.setattr(session_mod.dv, "wait_for_cloudflare", lambda d, timeout=None: None)
    monkeypatch.setattr(session_mod.dv, "harvest_auth", lambda d: AuthBundle(token="", device_id="", device_key=""))
    calls = []
    monkeypatch.setattr(session_mod, "login_via_api", lambda driver, job: calls.append("api") or False)
    monkeypatch.setattr(session_mod, "_login", lambda driver, job: calls.append("form") or True)
    assert session_mod.ensure_session(driver, job) is True
    assert calls == ["api", "form"]


def test_ensure_session_uses_api_and_skips_form_when_api_succeeds(monkeypatch):
    job = _job()
    driver = _FakeDriver()
    monkeypatch.setattr(session_mod.dv, "wait_for_cloudflare", lambda d, timeout=None: None)
    monkeypatch.setattr(session_mod.dv, "harvest_auth", lambda d: AuthBundle(token="", device_id="", device_key=""))
    calls = []
    monkeypatch.setattr(session_mod, "login_via_api", lambda driver, job: calls.append("api") or True)
    monkeypatch.setattr(session_mod, "_login", lambda driver, job: calls.append("form") or True)
    assert session_mod.ensure_session(driver, job) is True
    assert calls == ["api"]


def test_ensure_session_fails_when_form_and_api_fail(monkeypatch):
    job = _job()
    driver = _FakeDriver()
    monkeypatch.setattr(session_mod.dv, "wait_for_cloudflare", lambda d, timeout=None: None)
    monkeypatch.setattr(session_mod.dv, "harvest_auth", lambda d: AuthBundle(token="", device_id="", device_key=""))
    monkeypatch.setattr(session_mod, "_login", lambda driver, job: False)
    monkeypatch.setattr(session_mod, "login_via_api", lambda driver, job: False)
    assert session_mod.ensure_session(driver, job) is False
    assert job.state == "failed"


def test_ensure_session_runs_api_when_form_login_raises(monkeypatch):
    job = _job()
    driver = _FakeDriver()
    monkeypatch.setattr(session_mod.dv, "wait_for_cloudflare", lambda d, timeout=None: None)
    monkeypatch.setattr(session_mod.dv, "harvest_auth", lambda d: AuthBundle(token="", device_id="", device_key=""))
    calls = []
    monkeypatch.setattr(session_mod, "_login", lambda driver, job: (_ for _ in ()).throw(RuntimeError("stall")))
    monkeypatch.setattr(session_mod, "login_via_api", lambda driver, job: calls.append("api") or True)
    assert session_mod.ensure_session(driver, job) is True
    assert calls == ["api"]


def test_login_via_api_uses_env_creds_and_persists(monkeypatch):
    job = _job()
    driver = _FakeDriver()
    monkeypatch.setattr(session_mod.settings, "railway_user", "01676651991", raising=False)
    monkeypatch.setattr(session_mod.settings, "railway_pass", "pass", raising=False)

    def fake_harvest(d):
        return AuthBundle(token="", device_id="DID", device_key="DKEY")

    monkeypatch.setattr(session_mod.dv, "harvest_auth", fake_harvest)
    monkeypatch.setattr(session_mod.dv, "harvest_turnstile_token", lambda d, timeout=15: "cft.123")
    persisted = []

    class _FakeClient:
        def __init__(self, auth) -> None:
            self.auth = auth

        def sign_in_auth(self, mobile_number, password, cft_response, **kw) -> AuthBundle:
            return AuthBundle(token="JWT-API", device_id="DID", device_key="DKEY")

        def handshake(self, handshake_hash: str = "") -> dict:
            return {"meta": {"hash": "HSH"}, "extra": {"hash": "HSH"}}

    monkeypatch.setattr(session_mod, "ApiClient", _FakeClient)
    monkeypatch.setattr(session_mod.dv, "set_auth_storage", lambda d, auth: persisted.append(auth))

    assert session_mod.login_via_api(driver, job) is True
    assert job.auth.token == "JWT-API"
    assert job.auth.handshake_hash == "HSH"
    assert persisted == [job.auth]

class _ProbeDriver:
    """Driver stub for _login_page_blocked with a controllable rendered state."""

    def __init__(self, *, on_login_path: bool, has_button: bool, raise_on_get: bool = False) -> None:
        base = session_mod.settings.base_url
        self._login_url = base + session_mod.settings.login_path
        self.current_url = self._login_url if on_login_path else base
        self._has_button = has_button
        self._raise_on_get = raise_on_get
        self.visits: list[str] = []

    def get(self, url: str) -> None:
        if self._raise_on_get:
            raise RuntimeError("detached")
        self.visits.append(url)
        # The SPA route guard bounces /login back to the home page while a dead
        # token is still in localStorage.
        self.current_url = self._login_url

    def execute_script(self, script, *args):
        return "complete"

    def find_elements(self, by, value):
        if value == "button":
            return [_FakeElement()] if self._has_button else []
        return []


def test_login_page_blocked_false_when_form_renders(real_login_page_blocked):
    """A rendered login form means we can attempt a fresh login."""
    driver = _ProbeDriver(on_login_path=True, has_button=True)
    assert real_login_page_blocked(driver, timeout=5) is False
    assert len(driver.visits) == 1


def test_login_page_blocked_true_when_page_never_renders(real_login_page_blocked):
    """Bounced back to a home page with no form: re-login is impossible."""
    driver = _ProbeDriver(on_login_path=False, has_button=False)
    assert real_login_page_blocked(driver, timeout=1) is True
    assert driver.visits


def test_login_page_blocked_survives_driver_errors(real_login_page_blocked):
    """A dead session must not raise - it just means 'not rendered'."""
    driver = _ProbeDriver(on_login_path=True, has_button=True, raise_on_get=True)
    assert real_login_page_blocked(driver, timeout=1) is True
