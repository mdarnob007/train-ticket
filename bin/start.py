#!/usr/bin/env python
"""Take seats for the target train and land on the passenger form, hands off.

The seat is won by whoever clicks it, so the SPA that the browser runs must
take the hold itself - an API-side reserve would leave its client state empty
and Continue would fail with "Please choose seat(s)!". The API still does the
thinking and picks *which* seats; the browser turns that into clicks:

Flow:
  1. reuse the browser session, logging in only if it is missing or stale
  2. search-trips-v2 for the configured route/date/class
  3. find DEFAULT_TRAIN; report and stop if it is absent or has no seats
  4. load the results page in the browser and take its Turnstile token (the
     only readable widget - the seat modal consumes its own)
5. seat-layout via the API with that token -> decide which seats to take
      - SEAT_PREFERENCES / SEAT_COACH (optional): a ranked preference list such
        as 24,25,29,28 with the coach prefix added (e.g. GA-24,GA-25,...);
        preferred seats that are not free are topped up from the same coach,
        and blank preferences mean "any free seat"
   6. open the seat modal via the card's AC_S row (each card lists one BOOK NOW
     per class) and guard that the modal's coaches match the API layout's, then
     click those seats
     - normally the API's ticket ids exist on the SPA grid and are clicked
       exactly; if the SPA's search resolved a different route (which happens),
       adjacent free seats are read straight from the grid and clicked instead
  7. click Continue once the selection registers and wait for the passenger
     form to load

The browser stops at the passenger form with the seats selected and held. You
add passengers, pay and the OTP is yours.

Usage:
  ./.venv/bin/python bin/start.py [--count N] [--train NAME] [--wait]
  ./.venv/bin/python bin/start.py --dry-run      # resolve only, click nothing
  ./.venv/bin/python bin/start.py --select-only  # click the seats, then STOP at
                                                 # the seat modal (you press
                                                 # Continue yourself)
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
from app.models.job import BookingJob, JobConfig, Passenger  # noqa: E402
from app.rpa import driver as dv  # noqa: E402
from app.rpa import layout as layout_mod  # noqa: E402
from app.rpa import reserve as reserve_mod  # noqa: E402
from app.rpa import search as search_mod  # noqa: E402
from app.rpa import seat_picker as seat_picker_mod  # noqa: E402
from app.rpa import session as session_mod  # noqa: E402
from app.rpa.api_client import ApiClient  # noqa: E402

log = logging.getLogger("start")


def _wait_for_release_time(lead_secs: float = 3.0) -> None:
    """Idle until DEFAULT_RELEASE_TIME (off unless --wait is passed).

    Tickets appear server-side at the release minute, so a pre-08:00 run would
    search an empty timetable. Everything expensive (login, layout, seat choice)
    still happens after the wait, so this only moves the first request.
    """
    now = time.time()
    target = time.mktime(time.strptime(settings.default_release_time, "%H:%M"))
    seconds = target - now
    if seconds < 0:
        seconds += 24 * 3600  # already past today, aim at tomorrow
    if seconds < lead_secs:
        log.info("Release time %s has passed - starting now", settings.default_release_time)
        return
    log.info("Waiting %.0fs until release time %s", seconds, settings.default_release_time)
    time.sleep(seconds - lead_secs)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--count", type=int, default=settings.default_passengers)
    parser.add_argument("--train", default=settings.default_train)
    parser.add_argument("--from", dest="from_city", default=settings.default_from)
    parser.add_argument("--to", dest="to_city", default=settings.default_to)
    parser.add_argument("--date", default=settings.journey_date)
    parser.add_argument("--class", dest="seat_class", default=settings.default_class)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve the seats and the coach but click nothing; prints exactly "
        "what a real run would click, then exits",
    )
    mode.add_argument(
        "--select-only",
        action="store_true",
        help="click the chosen seats, then STOP at the seat modal without "
        "pressing Continue - you take over from there",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="wait until DEFAULT_RELEASE_TIME before searching",
    )
    args = parser.parse_args()

    job = BookingJob(
        JobConfig(
            train_name=args.train,
            from_city=args.from_city,
            to_city=args.to_city,
            date=args.date,
            seat_class=args.seat_class,
            trigger="auto",
            timeout_min=1.0,
        )
    )
    count = min(args.count or settings.default_passengers, settings.max_passengers)
    job.config.passengers = [
        Passenger(name=f"Passenger {i + 1}", gender="male", age=30, mobile="")
        for i in range(count)
    ]

    drv = dv.create_driver()
    if dv.is_attached():
        # Never touch the user's existing tabs - work in a tab of our own.
        dv.open_own_tab(drv)
    keep_open = False
    try:
        if args.wait:
            _wait_for_release_time()

        if not session_mod.ensure_session(drv, job):
            log.error("FAILED: no session - check RAILWAY_USER/RAILWAY_PASS in .env")
            return 3

        job.auth = dv.harvest_auth(drv)
        client = ApiClient(job.auth)

        started = time.time()
        trains = search_mod.parse_trains(
            client.search_trips(
                job.config.from_city, job.config.to_city, job.config.date, job.config.seat_class
            ),
            job.config.seat_class,
        )
        log.info("search: %d train(s) in %.1fs", len(trains), time.time() - started)

        train = search_mod.find_train(trains, job.config.train_name)
        if not train:
            log.error(
                "FAILED: train '%s' not found for %s -> %s on %s in %s",
                job.config.train_name, job.config.from_city, job.config.to_city,
                job.config.date, job.config.seat_class,
            )
            return 3
        log.info("Found %s (trip %s)", train.name, train.trip_id)
        if not search_mod.has_seats(train):
            log.error(
                "FAILED: %s has no %s seats available on %s",
                job.config.train_name, job.config.seat_class, job.config.date,
            )
            return 3

        # The results page is the only place with a readable Turnstile token: it
        # renders a `cf-turnstile-response` hidden input holding a live token,
        # whereas the seat modal's widget is consumed by the SPA and leaves
        # nothing to read. So load the results page, take its token, do all the
        # API work, and only open the modal at the end for the handoff.
        #
        # The server requires a token (a tokenless layout is a hard 422
        # TURNSTILE_TOKEN_REQUIRED) but not a fresh one, so one token covers the
        # layout and every seat.
        card, cft = search_mod.load_results_card_with_token(drv, job.config, train)
        log.info(
            "Turnstile token ready (%d chars); it will cover the layout and all %d seat(s)",
            len(cft), count,
        )

        started = time.time()
        layout = layout_mod.fetch_layout_api(client, train, cft)
        free = [s for s in layout if s.status == "available"]
        log.info("layout: %d free of %d parsed in %.1fs", len(free), len(layout), time.time() - started)

        per_coach: dict[str, int] = {}
        for s in free:
            per_coach[s.coach] = per_coach.get(s.coach, 0) + 1
        log.info(
            "coach availability: %s",
            "; ".join(f"{coach}: {n} free" for coach, n in sorted(per_coach.items())),
        )

        if len(free) < count:
            log.error(
                "FAILED: %d seat(s) available for %s on %s, need %d",
                len(free), job.config.train_name, job.config.date, count,
            )
            return 3

        # Optional ranked preference from SEAT_PREFERENCES (+ SEAT_COACH): the
        # coach prefix is added here ("24,25" -> "GA-24,GA-25"), and any
        # preferred seat that is not free is filled by choose_seats from the
        # same coach. Empty preferences keep the auto-select behaviour.
        if settings.seat_preferences or settings.seat_coach:
            job.config.seats = seat_picker_mod.resolve_preferences(
                settings.seat_preferences, settings.seat_coach, layout
            )
            log.info(
                "seat preferences: %s%s",
                ", ".join(job.config.seats) or "none usable",
                f" (coach from env: {settings.seat_coach})" if settings.seat_coach else "",
            )

        chosen = reserve_mod.choose(job, layout)
        log.info("Chose %s", ", ".join(s.seat_number or s.id for s in chosen))
        log.info("Available: %d; want %d", len(free), count)
        if len({s.coach for s in chosen}) > 1:
            log.info(
                "note: seats span coaches (%s) - the fill coach ran out of free "
                "seats, so the remainder was topped up from another",
                ", ".join(sorted({s.coach for s in chosen})),
            )

        if len(chosen) < count:
            log.error(
                "FAILED: could not choose %d seat(s) from %d available",
                count, len(free),
            )
            return 3

        # The seat is won by whoever clicks it, so the SPA must take the hold
        # itself - that is the only way its client state, the Seat Details tab
        # and Continue all keep working. The API chose *which* seats: open the
        # modal for the matched train's AC_S row (each card has one BOOK NOW per
        # class, and clicking the first is how an S_CHAIR booking slipped in)
        # and try those exact tickets first.
        search_mod.open_seat_modal(drv, card, job.config.seat_class)

        # Class guard: never click a seat in a modal opened for the wrong class.
        api_coaches = {s.coach for s in layout}
        if not seat_picker_mod.verify_modal_class(drv, api_coaches):
            log.error(
                "FAILED: modal coach set %s has no overlap with the API layout "
                "coaches %s - refusing to click in a wrong-class modal",
                sorted(seat_picker_mod.modal_coaches(drv)),
                sorted(api_coaches),
            )
            return 3

        if args.dry_run:
            log.info("DRY RUN - resolving without clicking (seats are not held):")
            try:
                targets = seat_picker_mod.resolve_in_grid(drv, chosen, train.trip_route_id)
                for t in targets:
                    log.info("  would click %s", t.describe())
            except RuntimeError as exc:
                log.info("  %s", exc)
                grid = seat_picker_mod.grid_seats(drv)
                fallback = seat_picker_mod.pick_fallback_seats(grid, len(chosen))
                for s in fallback:
                    log.info("  would click (grid fallback) %s", s.describe())
            log.info("Exiting.")
            return 0
        try:
            clicked = seat_picker_mod.select_seats_in_grid(drv, chosen, train.trip_route_id)
            log.info("API-chosen seats clicked: %s", ", ".join(t.describe() for t in clicked))
        except RuntimeError as exc:
            # The search the SPA ran can return a different route than our API
            # call (observed: identical query, distinct route ids and coach
            # sets), so the API-chosen ticket ids may not exist on this grid.
            # Fall back to picking adjacent free seats straight from the grid -
            # exactly what the browser will act on - and click those.
            log.warning("API choice not in the grid: %s", exc)
            grid = seat_picker_mod.grid_seats(drv)
            grid_free = [s for s in grid if s.state == "available"]
            log.info("grid shows %d free seats in the current coach", len(grid_free))
            fallback = seat_picker_mod.pick_fallback_seats(grid, len(chosen))
            clicked = seat_picker_mod.select_grid_seats(drv, fallback)
            log.warning(
                "API layout and the grid diverge - substituted grid seats: %s",
                ", ".join(t.describe() for t in clicked),
            )

        if args.select_only:
            log.info(
                "PASS: %s selected and highlighted at the seat modal - click "
                "Continue yourself to proceed",
                ", ".join(t.describe() for t in clicked),
            )
            keep_open = True
            return 0

        seat_picker_mod.activate_continue(drv)
        if not seat_picker_mod.wait_for_passenger_page(drv):
            log.error("FAILED: Continue was clicked but the passenger page never loaded")
            return 3

        log.info(
            "PASS: %s selected and Continue activated for %s",
            ", ".join(t.describe() for t in clicked),
            job.config.train_name,
        )
        log.info(
            "Stopped at the passenger form - fill passengers, then pay; the OTP is yours."
        )
        keep_open = True
        return 0
    finally:
        # On success the browser is deliberately left open so the user can click
        # Continue, add passengers and pay. Every failure path already logged
        # why, so close it rather than leaving an orphan Chrome behind.
        if not keep_open:
            try:
                # In attach mode this closes only our tab - `quit()` would
                # shut down the user's whole browser and every tab they have.
                dv.close_tab_only(drv)
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
