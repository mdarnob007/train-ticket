# RPA — Bangladesh Railway e-Ticket Release-Time Booking Bot

Automates booking seats on https://eticket.railway.gov.bd when tickets go on
sale (daily 08:00). Heavily aligned with the live Shohoz platform: a warm, real
browser passes Cloudflare automatically; login is reused from a persistent
profile; OTP is entered manually only when the site demands it; payment is
always a manual handoff (real money). See `IMPLEMENTATION.md` for the full plan.

## Status
- **Booking core — done and live-validated.** `bin/start.py` is the single
  entry point: it searches, takes the train's Turnstile token from the results
  page, reads the seat layout via the API, chooses seats (ranked preferences or
  auto), opens the class-correct seat modal in the browser, guards the modal
  against the wrong class, clicks the chosen seats, confirms the selection and
  lands you on the passenger form. The API thinks, the browser clicks.
- **Seat picking — done.** `SEAT_PREFERENCES` / `SEAT_COACH` give a ranked list
  (e.g. `24,25,29,28` with coach `GA`); unavailable preferences are filled
  cluster-adjacent from the same coach before any other coach is used. Blank
  preferences auto-select the best coach-wide grouping (one coach that fits the
  party, else spread). 109 unit/API tests passing.
- **Discovery (2026-09-23)** — 9 real endpoints + selectors captured from
  `data/discovery/network.json` + DOM snapshots, codified in
  `app/models/apidefs.py` / `app/models/selectors.py`.
- **Automated login (2026-09-24)** — form login is the primary path (it binds
  the server-side device the bookings API requires), with a Shohoz sign-in API
  fallback. Verified live: search-trips-v2 returns 200 and the user menu shows.

> The dashboard/scheduler/confirm/payment pipeline was removed: booking is a
> deliberate, hands-on CLI step and payment must stay manual anyway. (History in
> git.)

## Setup
```bash
cd /opt/homebrew/var/www/RPA
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env      # then fill in RAILWAY_USER / RAILWAY_PASS
```

## Configuration (`.env`)
- **Login**: `RAILWAY_USER`, `RAILWAY_PASS` (phone-number login); `CONTACT_EMAIL`.
- **Journey**: `DEFAULT_FROM`, `DEFAULT_TO`, `DEFAULT_CLASS` (`AC_S` |
  `S_CHAIR` | `SNIGDHA` | `AC_B`), `DEFAULT_TRAIN` (full form, e.g.
  `MAHANAGAR PROVATI (704)`; matching also tolerates the code alone or a
  partial name), `JOURNEY_DATE` (`dd-MMM-yyyy`; blank = required on the form),
  `DEFAULT_NUMBER_OF_PASSENGERS` (≤ 4; alias for `default_passengers`),
  `DEFAULT_DATE_FORMAT` (how dates are rendered, alias for `date_format`),
  `DEFAULT_RELEASE_TIME`.
- **Seats** (optional): `SEAT_PREFERENCES` (ranked seat numbers, e.g.
  `24,25,29,28`) and `SEAT_COACH` (coach code prepended to them, e.g. `GA`).
  Unavailable preferences are filled from the same coach nearest the chosen
  cluster; blank = any free seat, auto-selected.
- **Browser**: `DRIVER_ENGINE=attach` reuses an already-running Chrome via
  `DEBUGGER_ADDRESS=127.0.0.1:9222` (launch it with
  `--remote-debugging-port=9222`); the default `uc` uses undetected-chromedriver.

## Running
```bash
./.venv/bin/python bin/start.py                      # book for real
./.venv/bin/python bin/start.py --dry-run            # resolve + print, click nothing
./.venv/bin/python bin/start.py --select-only        # click seats, STOP at the modal
./.venv/bin/python bin/start.py --wait               # idle until DEFAULT_RELEASE_TIME
```

`--dry-run` and `--select-only` are mutually exclusive. A real run opens the
seat modal, clicks the chosen seats, presses Continue and stops at the passenger
form with seats held — fill passengers, pay, and the OTP is yours.

`bin/start.py` opens its own tab when attaching; on any failure it closes only
that tab and leaves your other tabs alone. On success the tab is left open for
you to take over.

### Diagnostics
```bash
./.venv/bin/python bin/probe.py          # quick title/URL/login-state dump
./.venv/bin/python bin/verify_login.py   # full login flow + logged-in-menu check
./.venv/bin/python bin/warmup.py --train "MAHANAGAR PROVATI (704)" --class AC_S \
    --from Dhaka --to Chattogram --date 2026-10-08   # sale-day smoke: session+search
./.venv/bin/python bin/discover.py       # (re)capture endpoints + selectors
./.venv/bin/python bin/api_probe.py      # capture search/layout response shapes
./.venv/bin/python bin/diagnose_search.py bin/capture_login_flow.py \
    bin/cap_request.py bin/discover_login.py   # deeper debugging tools
```

## Layout
- `bin/` — CLI entry points: `start.py` (the booker), warmup/probe/verify_login,
  discovery and capture tools.
- `app/rpa/` — Selenium + API automation: `driver`, `session`, `search`,
  `layout`, `seat_picker`, `reserve`, `otp`, `api_client`.
- `app/models/` — job state, auth/seat/train models, selectors, API defs.
- `data/` — discovery captures (`data/discovery/`), saved sessions.
- `profiles/` — persistent Chrome user-data-dir (session + Cloudflare cookies).

## Guardrails
- Payment is never automated: the bot stops at the passenger form and pauses
  for you; payment is a manual handoff (real money).
- Max 4 seats/passengers (platform cap); 429/rate limits retried with backoff;
  the search poll retries 4×15s and logs per-attempt timings.
- Attach mode never quits your Chrome — it only closes its own tab, and only on
  failure.
- Secrets live only in `.env` (git-ignored).