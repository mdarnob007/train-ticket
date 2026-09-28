#!/usr/bin/env python
"""Smoke check: verify an automated login produces a session the SPA accepts.

Default: reuse a valid session; when none exists, run the login flow (SPA form
first, sign-in API fallback). Then it performs a real bookings search via
`ApiClient` and checks the SPA actually renders the logged-in user menu on the
home page (a freshly minted token that the bookings API rejects with 401 still
passes storage checks - this probe requires the menu + a 200 search).

    --fresh     Clear profile storage first so a real login always happens.
    --api-only  Skip the form and use the sign-in API path directly (the
                fastest way to watch the API login + search working).

Exits 0 when login + search + logged-in UI are all good, 2 when the session is
not accepted, 3 on hard errors.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

from app.config import settings  # noqa: E402
from app.models.job import BookingJob, JobConfig  # noqa: E402
from app.rpa import driver as dv  # noqa: E402
from app.rpa import session as session_mod  # noqa: E402
from app.rpa.api_client import ApiClient  # noqa: E402


def _search(client: ApiClient, log) -> int:
    search = client.search_trips(
        settings.default_from,
        settings.default_to,
        settings.journey_date or "04-Oct-2026",
        settings.default_class,
    )
    if not isinstance(search, dict):
        log.error("search returned %s (expected dict)", type(search).__name__)
        return 0
    data = search.get("data", search)
    if isinstance(data, dict):
        trains = data.get("trains") or data.get("direct_trains") or []
    elif isinstance(data, list):
        trains = data
    else:
        trains = []
    log.info("SEARCH OK: %d train(s) on %s -> %s", len(trains), settings.default_from, settings.default_to)
    for t in (trains or [])[:5]:
        if isinstance(t, dict):
            log.info(
                "  %-28s seat_types=%d",
                str(t.get("name") or t.get("train_name") or "?")[:28],
                len(t.get("seat_types") or []),
            )
    return len(trains)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fresh", action="store_true", help="clear profile storage so a real login happens")
    parser.add_argument("--api-only", action="store_true", help="login via the sign-in API (skip the form)")
    args = parser.parse_args()

    log = logging.getLogger("verify_login")
    drv = dv.create_driver()
    try:
        drv.get(settings.base_url)
        dv.wait_for_cloudflare(drv, timeout=45)
        time.sleep(2)

        if args.fresh or args.api_only:
            log.info("--fresh/--api-only: wiping storage to force a real login")
            drv.execute_script("try { localStorage.clear(); sessionStorage.clear(); } catch (e) {}")
            drv.refresh()
            dv.wait_for_cloudflare(drv, timeout=45)

        job = BookingJob(
            JobConfig(
                train_name="x",
                from_city=settings.default_from,
                to_city=settings.default_to,
                date=settings.journey_date or "04-Oct-2026",
            )
        )
        if args.api_only:
            log.info("forcing the sign-in API path only")
            if not session_mod.login_via_api(drv, job):
                log.error("FAILED: API login did not succeed")
                return 3
        else:
            if not session_mod.ensure_session(drv, job):
                log.error("FAILED: no session could be established (form and API both failed)")
                return 3

        bundle = dv.harvest_auth(drv)
        log.info("token present: %s", bool(bundle.token))
        log.info("device_id:    %s", bundle.device_id)
        log.info("device_key:   %s", (bundle.device_key or "")[:32])
        log.info("user keys:    %s", sorted((bundle.user or {}).keys()))
        log.info("handshake:    %s", bool(bundle.handshake_hash))

        n_trains = _search(ApiClient(bundle), log)
        if not isinstance(n_trains, int) or n_trains <= 0:
            log.error("search did not return trains - session may be invalid")
            return 2

        # Reload home so the SPA rehydrates from storage and renders the menu.
        log.info("reloading home to check the logged-in header menu")
        drv.get(settings.base_url)
        dv.wait_for_cloudflare(drv, timeout=45)
        time.sleep(6)
        body = ""
        try:
            body = drv.find_element("tag name", "body").text
        except Exception as exc:
            log.warning("could not read body text: %s", exc)

        user_menu = bool(session_mod._logged_in_ui(drv))
        log.info("--- login state ---")
        log.info("logged-in user menu present: %s", user_menu)
        log.info("body (first 300 chars): %s", (body or "")[:300].replace("\n", " | "))

        if not user_menu:
            log.error("FAILED: header still anonymous even though search returned trains")
            return 2
        if user_menu and n_trains > 0:
            log.info("PASS: API session accepted - search 200 with %d train(s) and logged-in menu shown", n_trains)
        return 0
    finally:
        drv.quit()


if __name__ == "__main__":
    sys.exit(main())