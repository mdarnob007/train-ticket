#!/usr/bin/env python
"""Sale-day smoke test: warm the session and verify the target train is reachable.

Runs the full pre-booking warm-up (browser boot, login/session reuse, OTP if the
site asks) and one search poll. Does NOT book anything.

Usage:  ./.venv/bin/python bin/warmup.py --train "SUNDARBAN EXPRESS" --from Dhaka \
        --to "Cox's Bazar" --date 2026-09-30 --class AC_S
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

from app.models.job import BookingJob, JobConfig, TriggerMode  # noqa: E402
from app.rpa import driver as dv  # noqa: E402
from app.rpa import search as search_mod  # noqa: E402
from app.rpa import session as session_mod  # noqa: E402
from app.rpa.api_client import ApiClient  # noqa: E402


def _fmt_date(value: str) -> str:
    """Normalise an ISO or dd-MMM-yyyy date to the railway format dd-MMM-yyyy."""
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value, fmt).strftime("%d-%b-%Y")
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {value!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--train", default="SUNDARBAN EXPRESS")
    parser.add_argument("--from", dest="from_city", default="Dhaka")
    parser.add_argument("--to", dest="to_city", default="Cox's Bazar")
    parser.add_argument("--date", default="2026-09-30")
    parser.add_argument("--class", dest="seat_class", default="AC_S")
    args = parser.parse_args()

    config = JobConfig(
        train_name=args.train,
        from_city=args.from_city,
        to_city=args.to_city,
        date=_fmt_date(args.date),
        seat_class=args.seat_class,
        trigger=TriggerMode.MANUAL,
        timeout_min=1.0,
    )
    job = BookingJob(config)
    drv = dv.create_driver()
    if dv.is_attached():
        # Never navigate the user's current tab - work in a tab of our own.
        dv.open_own_tab(drv)
    try:
        ok = session_mod.ensure_session(drv, job, otp_timeout=120)
        if not ok:
            logging.getLogger("warmup").error("no valid session - check RAILWAY_USER/RAILWAY_PASS in .env")
            return 1
        job.auth = dv.harvest_auth(drv)
        client = ApiClient(job.auth)
        trains = search_mod.parse_trains(client.search_trips(job.config.from_city, job.config.to_city, job.config.date, job.config.seat_class))
        target = search_mod.find_train(trains, job.config.train_name)
        log = logging.getLogger("warmup")
        log.info("search returned %d train(s)", len(trains))
        for t in trains[:10]:
            log.info("  %-28s dep=%s arr=%s seats=%s  trip=%s", t.name, t.departure, t.arrival, t.seats_left, t.trip_number)
        if target:
            log.info("TARGET MATCH: %s (seats_left=%s) trip_id=%s route=%s", target.name, target.seats_left, target.trip_id, target.trip_route_id)
            return 0
        log.warning("target train not found in results")
        return 0
    finally:
        # Attach mode: closing does not quit() the user's whole browser - it
        # closes only the tab we opened. Non-attach drivers quit normally.
        dv.close_tab_only(drv)


if __name__ == "__main__":
    sys.exit(main())