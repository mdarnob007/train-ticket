#!/usr/bin/env python
"""discover_login - capture the real /login form DOM.

Run it when the saved profile session has expired (the login form is what the
bot needs to re-login automatically).

Usage:  ./.venv/bin/python bin/discover_login.py

You drive the browser:
  1. Chrome opens on the /login page. If you are already logged in, log out in
     the browser so the login form shows, then press ENTER here.
  2. Fill in your mobile + password, submit, and log in (the railway login on
     this account never asks for an SMS OTP) - then press ENTER here.
It dumps the DOM + snapshots under data/discovery/ and prints ready-to-paste
locator lines for app/models/selectors.py. No booking/payment happens.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("discover_login")

from app.config import settings  # noqa: E402
from app.rpa import driver as dv  # noqa: E402

OUT = settings.data_dir / "discovery"
OUT.mkdir(parents=True, exist_ok=True)

ANALYZE_JS = """
(() => {
  try {
    const pick = el => ({
      id: el.id || null,
      name: el.getAttribute('name') || null,
      type: (el.getAttribute('type') || '').toLowerCase() || null,
      formcontrolname: el.getAttribute('formcontrolname') || null,
      placeholder: el.getAttribute('placeholder') || null,
      className: (typeof el.className === 'string' ? el.className : '') || null,
      inputmode: el.getAttribute('inputmode') || null,
      autocomplete: el.getAttribute('autocomplete') || null,
    });
    const inputs = Array.from(document.querySelectorAll('input')).map(pick);
    const buttons = Array.from(document.querySelectorAll('button')).map(b => ({
      type: (b.getAttribute('type') || '').toLowerCase() || null,
      className: (typeof b.className === 'string' ? b.className : '') || null,
      text: (b.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 60) || null,
    }));
    const auth = document.querySelector('app-auth');
    return {
      url: location.href,
      inputs,
      buttons,
      authHtml: auth ? auth.outerHTML.slice(0, 2000) : null,
      formcount: document.querySelectorAll('form').length,
    };
  } catch (err) {
    return { error: String(err && err.message || err), inputs: [], buttons: [], url: location.href, formcount: 0 };
  }
})()
"""


def analyze(drv) -> dict:
    try:
        data = drv.execute_script(ANALYZE_JS)
    except Exception as exc:
        print(f"  analysis failed: {exc}")
        return {}
    if not isinstance(data, dict):
        return {}
    data["inputs"] = data.get("inputs") or []
    data["buttons"] = data.get("buttons") or []
    return data


def analyze_html() -> dict:
    """Fallback: regex the saved snapshot when inline JS returns nothing."""
    path = OUT / "login_login_form.html"
    if not path.exists():
        return {}
    html = path.read_text(encoding="utf-8", errors="ignore")
    data = {"inputs": [], "buttons": [], "formcount": html.count("<form"), "url": "", "source": "html"}
    for attrs in re.findall(r"<input\b[^>]*>", html):
        entry = {
            "id": re.search(r'\bid="?([^"\s>]+)', attrs),
            "name": re.search(r'\bname="?([^"\s>]+)', attrs),
            "type": re.search(r'\btype="?([^"\s>]+)', attrs),
            "formcontrolname": re.search(r'\bformcontrolname="?([^"\s>]+)', attrs),
            "placeholder": re.search(r'\bplaceholder="([^"]+)"', attrs),
            "className": re.search(r'\bclass="([^"]+)"', attrs),
            "inputmode": re.search(r'\binputmode="([^"]+)"', attrs),
        }
        data["inputs"].append({k: (m.group(1) if m else None) for k, m in entry.items()})
    for attrs in re.findall(r"<button\b[^>]*>", html):
        data["buttons"].append(
            {
                "type": (re.search(r'\btype="([^"]+)"', attrs).group(1) if re.search(r'\btype="([^"]+)"', attrs) else None),
                "className": (re.search(r'\bclass="([^"]+)"', attrs).group(1) if re.search(r'\bclass="([^"]+)"', attrs) else None),
                "text": None,
            }
        )
    if not data["inputs"] and not data["buttons"]:
        print("  (html fallback found no inputs/buttons in the snapshot)")
    return data


def snapshot(drv, label: str) -> None:
    html = drv.page_source or ""
    (OUT / f"login_{label}.html").write_text(html, encoding="utf-8", errors="ignore")
    try:
        drv.save_screenshot(str(OUT / f"login_{label}.png"))
    except Exception:
        pass
    print(f"  snapshot -> data/discovery/login_{label}.html")


def pretty_dump(label: str, data: dict) -> None:
    path = OUT / "login_dom.json"
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    existing[label] = data
    path.write_text(json.dumps(existing, indent=2, default=str), encoding="utf-8")
    print(f"  report -> data/discovery/login_dom.json [{label}]")


def find_mobile(data: dict) -> str | None:
    for i in data.get("inputs") or []:
        attrs = " ".join(str(v or "") for v in i.values()).lower()
        if "mobile" in attrs or "phone" in attrs:
            return i.get("formcontrolname") or i.get("name") or i.get("id")
        if i.get("formcontrolname") and "number" in i["formcontrolname"]:
            return i["formcontrolname"]
        if i.get("type") == "tel":
            return i.get("formcontrolname") or i.get("name") or i.get("id")
    return None


def find_password(data: dict) -> str | None:
    for i in data.get("inputs") or []:
        if i.get("type") == "password":
            return i.get("formcontrolname") or i.get("name") or i.get("id")
    for i in data.get("inputs") or []:
        if i.get("formcontrolname") and "password" in i["formcontrolname"].lower():
            return i["formcontrolname"]
    return None


def find_submit(data: dict) -> str | None:
    for b in data.get("buttons") or []:
        txt = (b.get("text") or "").lower()
        cls = (b.get("className") or "").lower()
        if "login" in txt or "sign in" in txt or "next" in txt or "otp" in txt:
            return cls or ("type=" + (b.get("type") or "submit"))
    for b in data.get("buttons") or []:
        if b.get("type") == "submit" and b.get("className"):
            return b["className"]
    return None


def recommended(data: dict) -> None:
    mobile = find_mobile(data)
    password = find_password(data)
    submit = find_submit(data)

    print("\n--- recommended locator lines ---")
    if mobile:
        print(f'LOGIN_MOBILE_INPUT = "xpath//input[@formcontrolname=\'{mobile}\']"')
    else:
        print("LOGIN_MOBILE_INPUT = [check data/discovery/login_dom.json]")
    if password:
        print(f'LOGIN_PASSWORD_INPUT = "xpath//input[@formcontrolname=\'{password}\']"')
    else:
        print('LOGIN_PASSWORD_INPUT = "cssinput[type=password]"')
    if submit:
        print(f'LOGIN_SUBMIT_BUTTON = "css.{submit if submit.startswith("type=") else submit}"')
    else:
        print('LOGIN_SUBMIT_BUTTON = "cssbutton[type=submit]"')
    print("---")
    print(f"(inputs={len(data.get('inputs') or [])}, buttons={len(data.get('buttons') or [])}, "
          f"forms={data.get('formcount', 0)})")


def main(drv) -> int:
    try:
        drv.get(settings.base_url + settings.login_path)
        dv.wait_for_cloudflare(drv, timeout=45)
        time.sleep(3)

        print("\n1) The /login page is open in Chrome.")
        print("   If you are already logged in, log out in the browser so the")
        print("   login form (mobile + password) is visible.")
        input("   Press ENTER once the LOGIN FORM is visible... ")
        snapshot(drv, "login_form")
        data = analyze(drv) or {}
        if not data.get("inputs"):
            data = analyze_html() or {}
        pretty_dump("login_form", data)
        recommended(data)

        print("\n2) Now enter your mobile + password and submit the form, then log in.")
        input("   Press ENTER once you are BACK on the logged-in site... ")
        snapshot(drv, "after_login")
        data2 = analyze(drv) or {}
        pretty_dump("after_login", data2)
    finally:
        try:
            drv.quit()
        except Exception:
            pass
    print("\nPaste the recommended lines into app/models/selectors.py, then")
    print("   session.ensure_session / api_probe will re-login automatically.")
    return 0


if __name__ == "__main__":
    drv = dv.create_driver()
    try:
        rc = main(drv)
    finally:
        try:
            drv.quit()
        except Exception:
            pass
    sys.exit(rc or 0)