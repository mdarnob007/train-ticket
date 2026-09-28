from __future__ import annotations

import re

import pytest
from selenium.webdriver.common.by import By

from app.models import selectors
from app.rpa import driver as dv


class _FakeSwitchTo:
    def __init__(self) -> None:
        self.new_tab_called = False

    def new_window(self, kind):
        self.new_tab_called = True
        return "tab-handle"


class _FakeAttachedDriver:
    def __init__(self) -> None:
        self.current_window_handle = "win-1"
        self.switch_to = _FakeSwitchTo()
        self.closed = False
        self.quit_called = False
        self.implicit_wait = None

    def implicitly_wait(self, secs):
        self.implicit_wait = secs

    def close(self) -> None:
        self.closed = True

    def quit(self) -> None:
        self.quit_called = True


def test_create_driver_attaches_without_launching_a_browser(monkeypatch):
    """DRIVER_ENGINE=attach must never start its own Chrome."""
    monkeypatch.setattr(dv.settings, "driver_engine", "attach")
    monkeypatch.setattr(dv.settings, "debugger_address", "127.0.0.1:9222")
    fake = _FakeAttachedDriver()
    launched = {}

    def fake_chrome(options):
        launched["debugger_address"] = options.debugger_address
        launched["args"] = list(options.arguments)
        return fake

    monkeypatch.setattr(dv.webdriver, "Chrome", fake_chrome)
    got = dv.create_driver()
    assert got is fake
    assert launched["debugger_address"] == "127.0.0.1:9222"
    # No --user-data-dir and no automation-masking argument: the real browser
    # already carries the session and has no automation markers to hide.
    assert not any("user-data-dir" in a for a in launched["args"])


def test_attach_raises_with_launch_hint_when_no_chrome_is_listening(monkeypatch):
    import urllib.request

    monkeypatch.setattr(dv.settings, "driver_engine", "attach")

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as exc:
        dv.create_driver()
    assert "user-data-dir" in str(exc.value)


def test_close_tab_only_closes_a_single_tab_when_attached(monkeypatch):
    """quit() on an attached browser would close every tab the user has open."""
    monkeypatch.setattr(dv.settings, "driver_engine", "attach")
    fake = _FakeAttachedDriver()
    dv.close_tab_only(fake)
    assert fake.closed is True
    assert fake.quit_called is False


def test_close_tab_only_quits_normally_when_not_attached(monkeypatch):
    monkeypatch.setattr(dv.settings, "driver_engine", "uc")
    fake = _FakeAttachedDriver()
    dv.close_tab_only(fake)
    assert fake.quit_called is True
    assert fake.closed is False


def test_is_attached_reflects_the_configured_engine(monkeypatch):
    monkeypatch.setattr(dv.settings, "driver_engine", "attach")
    assert dv.is_attached() is True
    monkeypatch.setattr(dv.settings, "driver_engine", "uc")
    assert dv.is_attached() is False


def test_locator_strips_the_bare_css_prefix():
    """`css` is a bare marker: the remainder is the CSS selector verbatim."""
    assert dv.locator("css.trip-collapsible") == (By.CSS_SELECTOR, ".trip-collapsible")
    assert dv.locator("cssbutton.book-now-btn") == (By.CSS_SELECTOR, "button.book-now-btn")
    assert dv.locator("css#confirmbooking .continue-btn") == (By.CSS_SELECTOR, "#confirmbooking .continue-btn")
    assert dv.locator("cssul.ui-autocomplete li") == (By.CSS_SELECTOR, "ul.ui-autocomplete li")


def test_locator_keeps_the_double_slash_of_an_xpath():
    """`//` is part of the expression; dropping it yields a relative XPath.

    A relative `input[...]` only matches children of the context node, so it
    silently finds 0 elements for real page markup and the login form appears
    to "never render".
    """
    assert dv.locator("xpath//input[@id='x']") == (By.XPATH, "//input[@id='x']")
    assert dv.locator("xpath//app-login") == (By.XPATH, "//app-login")


def test_every_xpath_selector_constant_starts_with_double_slash():
    for name in dir(selectors):
        spec = getattr(selectors, name)
        if isinstance(spec, str) and spec.startswith("xpath"):
            assert dv.locator(spec)[1].startswith("//"), name


def test_every_class_selector_constant_keeps_its_leading_dot():
    """Guards against `css.foo` silently becoming the element name `foo`."""
    for name in dir(selectors):
        spec = getattr(selectors, name)
        if not (name.isupper() and isinstance(spec, str)):
            continue
        by, value = dv.locator(spec)
        if by is not By.CSS_SELECTOR:
            continue
        assert not value.startswith("css"), f"{name}={spec!r} left the css prefix in place"
        # Reject a bare element name, which is what a dropped leading dot leaves.
        assert not re.fullmatch(r"[A-Za-z][\w-]*", value), (
            f"{name}={spec!r} expands to the element name {value!r} - "
            "a class/id/attribute selector needs its leading dot or hash"
        )
