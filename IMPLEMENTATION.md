# RPA — Bangladesh Railway e-Ticket Release-Time Booking Bot

## 1. Objective
Automatically select and hold train seats on https://eticket.railway.gov.bd when
they go on sale (daily 08:00, configurable). The user preloads config (route,
date, class, ranked seat list <=4) in `.env`, then runs `bin/start.py`. The bot
warms an authenticated browser session, searches, reads the seat layout via the
Shohoz API, chooses seats, and clicks them in the browser so the SPA itself
takes the hold — then stops at the passenger form for the user to fill in and
pay. Payment is NEVER automated.

## 2. Decisions locked with the user
- Stack: Python + Selenium (undetected-chromedriver or attach), headed Chrome.
- **CLI only** — `bin/start.py` is the single booking entry point (`--dry-run`,
  `--select-only`, `--wait`, `--count`, `--train`, `--from`, `--to`, `--date`,
  `--class`). The FastAPI dashboard/scheduler and the confirm/payment/passengers
  pipeline were **removed** (2026-10): booking is a deliberate hands-on step and
  payment must stay manual anyway.
- Seat selection: ranked list from `SEAT_PREFERENCES` (+ `SEAT_COACH`); book
  first free; fill missing seats nearest the chosen cluster **in the same
  coach** before using any other coach; blank preferences = auto-select (one
  coach that fits the party, else spread).
- Seat count max 4 (platform cap), driven by `DEFAULT_NUMBER_OF_PASSENGERS` or
  `--count`.
- **Browser does the holding**: the seat is won by whoever clicks it, so the SPA
  must take the hold itself — an API-side reserve leaves its client state empty
  and Continue fails with "Please choose seat(s)!". The API still does the
  thinking (HTTP calls for search/layout); the browser turns the chosen tickets
  into clicks.
- Cloudflare: managed challenge, passes automatically with a real browser.
- OTP: only when the site demands a fresh login (session expiry/new device):
  manual entry into the terminal when the login gate appears. Booking
  confirmation OTP and payment are the user's job after handoff.
- Payment: bot stops at the passenger form with seats held; user fills
  passengers and pays in the browser. Never automated.
- `DRIVER_ENGINE=attach` reuses the user's already-running Chrome over the CDP
  port (`DEBUGGER_ADDRESS`); the bot always works in a tab of its own, never
  quits the browser, and closes only that tab on failure. On success the tab is
  left open for handoff.
- One booking at a time. Secrets only in `.env`.

## 3. Platform facts (researched)
- Angular SPA on eticket.railway.gov.bd proxying railspaapi.shohoz.com.
- Auth: `Authorization: Bearer <token>` + `x-device-id` + `x-device-key`
  (stored in browser Local Storage).
- Known endpoints: `v1.0/web/bookings/search-trips-v2` (GET, from_city,
  to_city, date_of_journey, seat_class), `bookings/seat-layout` (needs a CF
  `cft_response` token), `bookings/seat-availability`, `reserve-seat` /
  `release-seat` (one PATCH per seat, serial, rolling `X-Action-Token` nonce).
- The seat-layout call without a Turnstile token is a hard 422
  `TURNSTILE_TOKEN_REQUIRED`; the server requires *a* token but not a fresh one,
  so a single token covers the layout and every seat. The only place a readable
  token exists is the **results page** (`cf-turnstile-response` hidden input) —
  the seat modal's widget is consumed by the SPA itself.
- Traffic is rate-limited (429 + backoff); seats lock for a few minutes after a
  Continue/order; seats per user capped at 4.
- **API and SPA can diverge**: the SPA's own search can resolve a different
  route than our API call (observed: distinct trip/route ids and coach sets with
  0% ticket-id overlap). The grid fallback handles this by reading free seats
  straight from the open modal.
- Each train card has **one BOOK NOW button per class**; clicking the first is
  how an S_CHAIR booking slipped in while AC_S was requested. The class-correct
  row is selected, and the modal's coach set is guarded against the API
  layout's before any click.

## 4. Repository layout
```
RPA/
├── requirements.txt            # selenium, undetected-chromedriver,
│                               # pydantic-settings, python-dotenv, requests, pytest
├── .env                        # RAILWAY_USER, RAILWAY_PASS, journey/seat/browser config
├── .env.example
├── IMPLEMENTATION.md           # this file (living reference)
├── README.md                   # setup + usage for the user
├── data/discovery/             # captured network.json + api_responses.json
├── profiles/                   # persistent Chrome user-data-dir
├── bin/
│   ├── start.py                # THE booker (was buy_ticket.py)
│   ├── warmup.py               # sale-day smoke: session + search
│   ├── probe.py, verify_login.py   # browser/session checks
│   ├── discover.py, discover_login.py, capture_login_flow.py, cap_request.py,
│   │   api_probe.py, diagnose_search.py   # discovery/debugging tools
└── app/
    ├── config.py               # env loading (Settings) + defaults
    ├── models/
    │   ├── job.py              # BookingJob, JobConfig, SeatInfo/TrainInfo, AuthBundle
    │   ├── selectors.py        # UI locators (from discovery)
    │   └── apidefs.py          # endpoint/method/headers/body schemas
    ├── rpa/
    │   ├── api_client.py       # typed Shohoz API client (auth, nonce replay)
    │   ├── driver.py           # Chrome factory (uc/selenium/attach), waits,
    │   │                       # close_tab_only, auth + Turnstile harvest
    │   ├── session.py          # login, session-valid check, OTP gate
    │   ├── search.py           # search-trips-v2, retry loop, results-page handling
    │   │                       # (load_results_card/with_token, book_now_for_class)
    │   ├── layout.py           # seat-layout fetch + parse
    │   ├── seat_picker.py      # choose_seats (ranked/cluster-adjacent), modal guard,
    │   │                       # grid read/parse/click, Continue + passenger page wait
    │   ├── reserve.py          # choose(): ranked-preference wrapper only
    │   └── otp.py              # manual-OTP login gate
```

## 5. Booking flow (`bin/start.py`)
```
wait_for_release_time (--wait) -> ensure_session (login or reuse) -> harvest auth
-> search-trips-v2 -> find DEFAULT_TRAIN (absent/no seats => stop)
-> load results page + Turnstile token (retry 4x, ~15s each, timed)
-> seat-layout (token) -> free count -> coach availability log
-> SEAT_PREFERENCES/SEAT_COACH -> resolve_preferences -> reserve.choose
   (cluster-adjacent fill, same coach first)
-> open class-correct seat modal (BOOK NOW of the AC_S/… row)
-> verify_modal_class guard against API layout coaches
   (mismatch => stop, return 3)
-> --dry-run: print what would be clicked, exit 0
-> click API-chosen seats on the grid
   (RuntimeError / route divergence => grid fallback: adjacent free seats)
-> --select-only: stop here, tab left open, exit 0
-> activate Continue -> wait for passenger page -> left open for handoff
```

## 6. Config (`.env`)
- Login: `RAILWAY_USER`, `RAILWAY_PASS`, `CONTACT_EMAIL`.
- Journey: `DEFAULT_FROM`, `DEFAULT_TO`, `DEFAULT_CLASS` (AC_S | S_CHAIR |
  SNIGDHA | AC_B), `DEFAULT_TRAIN` (full form, e.g. `MAHANAGAR PROVATI (704)`),
  `JOURNEY_DATE` (dd-MMM-yyyy), `DEFAULT_NUMBER_OF_PASSENGERS` (≤ 4),
  `DEFAULT_RELEASE_TIME`, `DEFAULT_DATE_FORMAT`.
- Seats: `SEAT_PREFERENCES` (ranked numbers, `24,25,29,28`), `SEAT_COACH`
  (coach prefix, `GA`). Both read via pydantic alias fields on `Settings`
  (`seat_preferences`, `seat_coach`).
- Naming: `DEFAULT_NUMBER_OF_PASSENGERS` and `DEFAULT_DATE_FORMAT` map to
  `default_passengers` / `date_format` through pydantic `validation_alias`
  (their field-derived names, `DEFAULT_PASSENGERS`/`DATE_FORMAT`, would not be
  read).
- Browser: `DRIVER_ENGINE` (uc | selenium | attach), `DEBUGGER_ADDRESS`,
  `HEADLESS`, wait timeouts. Attach mode requires Chrome started with
  `--remote-debugging-port=9222`.

## 7. Guardrails
- Never auto-submit payment; the tab is handed off at the passenger form.
- Modal class guard: never click a seat in a modal opened for the wrong class.
- `--dry-run` clicks nothing (seats are not held); `--select-only` holds seats
  but stops before Continue.
- 429/rate limits retried with backoff; the results-page load retries 4× with
  15s per attempt and logs each attempt's timing.
- Attach mode never quits the user's Chrome; only its own tab, only on failure.
- Max 4 seats/passengers. Secrets only in `.env`, never logged.

## 8. PHASE PLAN

### Phase 1 — Foundation + Session [DONE]
- venv + requirements.txt + config.py (.env loading, defaults, dataclasses).
- driver.py: Chrome factory (undetected-chromedriver by default — plain
  chromedriver carries `--test-type=webdriver` + CDP automation markers that
  Cloudflare Turnstile flags; uc launches without them), persistent profile,
  headed mode, explicit waits, wait_for_cloudflare, localStorage/cookie harvest.
- session.py: login flow, session-valid assertion, OTP pause hook, token /
  x-device-id / x-device-key extraction.

### Phase 2 — Discovery run [COMPLETE 2026-09-23: 9 endpoints captured]
- ChromeDriver's `performance` log proved unreliable under undetected-
  chromedriver (empty network.json). Rewritten to inject an in-page fetch/XHR
  logger (Page.addScriptToEvaluateOnNewDocument -> window.__railwayLog) that
  records method/url/headers/postData/status/response bodies from inside the
  SPA into `data/discovery/network.json`, codified in `app/models/apidefs.py`.
- `app/models/selectors.py` filled from the DOM snapshots.
- `bin/api_probe.py` recovered the search-trips-v2 and seat-layout response
  shapes (per-seat `ticket_id`, `seat_availability` 1=available / 2=held /
  0=booked, premium `floor.seat_type==1` skipped unless ranked) into
  `data/discovery/api_responses.json`. Non-browser User-Agents get 429'd, so
  ApiClient sends a browser UA.

### Phase 3 — Booking core  [BUILT, LIVE-VALIDATED]
- search.py: search-trips-v2 + results-page load; retry loop (4× ~15s) with
  timing logs; `book_now_for_class` picks the class-correct row (each card has
  one BOOK NOW per class).
- layout.py: seat-layout fetch (needs the results-page Turnstile token) + parse
  into SeatInfo.
- seat_picker.py: `choose_seats` (ranked -> nearest/auto fallback, cluster-
  adjacent fill in the same coach), modal class guard (`verify_modal_class` /
  `modal_coaches`), grid locate/click (mousedown+mouseup+click), grid fallback
  on API/SPA route divergence, `activate_continue`, `wait_for_passenger_page`.
- reserve.py: `choose()` only (ranked-preference wrapper); the legacy
  API-PATCH reserve / UI-click path and its `click_seat` were deleted with the
  runner pipeline. The browser is the only holder.
- otp.py: unified manual-OTP gate for login pauses.
- browser handoff: on success the tab stays open at the passenger form (seats
  selected + held); every failure path logs why and closes only its own tab.

### Phase 4 — Dashboard + scheduler  [REMOVED 2026-10]
- The FastAPI app, static UI, `app/runner.py`, and the confirm/payment/passengers
  /output pipeline were deleted. `bin/start.py` superseded the dashboard, and
  auto-submit confirm/payment was never going to be used (manual payment).
  Requirements dropped fastapi/uvicorn/httpx2; `BIND_HOST`/`BIND_PORT` removed.
- 109 unit/API tests passing (no live site): env aliases, seat-ranking +
  cluster-adjacent fill + auto-group logic, search parse (real multi-class
  shape), class-correct BOOK NOW, seat grid/modal guard helpers, api_client
  header/nonce behaviour, start.py `--dry-run`/`--select-only` flow wiring,
  driver attach behaviour.

## 9b. What remains to go live
1. Run `bin/warmup.py` before a sale day to confirm session + search still
   work.
2. A first real `bin/start.py` booking the day of — then hand off at the
   passenger form (Continue yourself, add passengers, pay). After a Continue,
   the platform blocks re-booking for ~5 minutes ("Multiple order attempt
   detected"), so run deliberately.