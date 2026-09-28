#!/usr/bin/env python
"""Phase 1 probe: boot the warm browser and confirm we reach the live site.

Usage:  ./.venv/bin/python bin/probe.py
No credentials required. Verifies driver factory + Cloudflare wait behaviour.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

from app.rpa import driver as dv  # noqa: E402
from app.rpa.session import is_logged_in  # noqa: E402

BASE = "https://eticket.railway.gov.bd"
LOGIN = BASE + "/login"
SEARCH = (BASE + "/booking/train/search"
          "?fromcity=Dhaka&tocity=Cox%27s%20Bazar&doj=30-Sep-2026&class=AC_S")


def main() -> int:
    drv = dv.create_driver()
    try:
        log = logging.getLogger("probe")
        log.info("Loading %s", BASE)
        drv.get(BASE)
        dv.wait_for_cloudflare(drv, timeout=45)
        log.info("title=%r url=%r", drv.title, drv.current_url)
        log.info("logged_in=%s", is_logged_in(drv))
        log.info("storage keys=%s", sorted(dv.harvest_auth(drv).raw_storage.keys()))
        return 0
    finally:
        drv.quit()


if __name__ == "__main__":
    sys.exit(main())