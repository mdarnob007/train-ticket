#!/usr/bin/env python
"""Assisted post-login capture: watch what the SPA calls right after login.

Boots the profile (optionally clears storage so you get a fresh login) into
/login, installs the XHR/fetch logger, then waits for YOU to log in by typing
the credentials in the opened browser. Every railspaapi request fired by the
login flow itself (sign-in, user profile, device bootstrap, entitlements...)
is written to `data/discovery/login_flow_requests.json`.

This is the decisive capture for the search-trips-v2 401: the API/login paths
mint a token but the SPA never shows the user menu, so we need to see the exact
post-login calls a REAL successful form login makes (and their order/headers).

Usage:  ./.venv/bin/python bin/capture_login_flow.py [--clear] [--wait-limit 300]
Log in in the opened browser once, then the script dumps the captures.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

from app.config import settings  # noqa: E402
from app.rpa import driver as dv  # noqa: E402
from bin.cap_request import LOGGER_JS  # noqa: E402

SAVE_PATH = "data/discovery/login_flow_requests.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clear", action="store_true", help="clear existing profile storage before boot (forces full login)")
    parser.add_argument("--wait-limit", type=int, default=300, help="max seconds to wait for login before giving up")
    args = parser.parse_args()

    log = logging.getLogger("capture_login_flow")
    drv = dv.create_driver()
    try:
        drv.get(settings.base_url)
        dv.wait_for_cloudflare(drv, timeout=45)
        time.sleep(2)

        if args.clear:
            log.info("--clear: wiping storage to force a fresh login")
            drv.execute_script("try { localStorage.clear(); } catch (e) {}")
            drv.refresh()
            dv.wait_for_cloudflare(drv, timeout=45)

        log.info("installing request logger")
        try:
            drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": LOGGER_JS})
        except Exception as exc:
            log.warning("CDP new-document hook failed (%s) - live-page logger only", exc)
        drv.execute_script(LOGGER_JS)
        drv.execute_script("try { sessionStorage.removeItem('__rpa_cap'); } catch (e) {}")

        drv.get(settings.base_url + settings.login_path)
        dv.wait_for_cloudflare(drv, timeout=45)
        log.info(">>> LOG IN NOW in the opened browser (mobile 01676651991 / your password)")
        log.info(">>> signing in fires the post-login railspaapi calls we want")

        deadline = time.time() + args.wait_limit
        seen = 0
        captured_login = False
        while time.time() < deadline:
            try:
                entries = drv.execute_script(
                    "try { return JSON.parse(sessionStorage.getItem('__rpa_cap')) || []; } catch (e) { return []; }"
                ) or []
            except Exception:
                entries = []
            if len(entries) > seen:
                seen = len(entries)
                log.info("... %d request(s) captured", seen)
            if not captured_login:
                try:
                    token = drv.execute_script("return localStorage.getItem('token');")
                except Exception:
                    token = None
                if token:
                    captured_login = True
                    log.info("token appeared in storage - login flow detected, watching for the tail")
                    deadline = min(deadline, time.time() + 30)
            if captured_login and seen >= 10 and not entries:
                time.sleep(1)
            elif time.time() >= deadline:
                break
            else:
                time.sleep(1)

        rpa = [e for e in entries if "railspaapi.shohoz.com" in e.get("url", "")]
        out = Path(SAVE_PATH)
        out.write_text(json.dumps(rpa, indent=2))
        log.info("--- post-login railspaapi calls (%d) ---", len(rpa))
        for e in rpa:
            log.info("  %s %s -> %s", e.get("method", "?"), e.get("url", "?")[:110], e.get("status"))
            for k, v in (e.get("requestHeaders") or {}).items():
                pretty = str(v)
                log.info("      %s: %s", k, pretty[:90] + ("..." if len(pretty) > 90 else ""))
        log.info("saved to %s", out)
        return 0 if rpa else 2
    finally:
        drv.quit()


if __name__ == "__main__":
    sys.exit(main())