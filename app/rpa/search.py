from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from app.config import settings
from app.models.job import BookingJob, JobConfig, RunState, TrainInfo
from app.rpa import driver as dv

log = logging.getLogger("rpa.search")


def normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def _first(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def _strip_seats(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for k in ("online", "total", "available"):
            if k in value:
                return str(value[k] or 0)
        keys = [k for k in value if "count" in k.lower()]
        return str(value[keys[0]]) if keys else ""
    return str(value)


def _seat_type_for(row: dict[str, Any], seat_class: str) -> dict[str, Any] | None:
    """Pick the seat_types[] entry matching the searched class (SPA uses one per class).

    Returns None when the class is absent so the caller can skip the train rather
    than silently book a different class.
    """
    types = row.get("seat_types") or row.get("seatType") or row.get("classes")
    if not isinstance(types, list):
        return None
    wanted = seat_class.upper()
    for t in types:
        if isinstance(t, dict) and str(t.get("type") or t.get("seat_class") or "").upper() == wanted:
            return t
    return None


def parse_trains(payload: dict[str, Any], seat_class: str = "AC_S") -> list[TrainInfo]:
    """Parse the search-trips-v2 response into TrainInfo (shape: data.trains[])."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    rows = data.get("trains") or data.get("trips") or data.get("results")
    if rows is None:
        rows = [payload]
    if not isinstance(rows, list):
        rows = [rows]

    output: list[TrainInfo] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        st = _seat_type_for(r, seat_class)
        if st is None and (isinstance(r.get("seat_types"), list) or isinstance(r.get("seatType"), list)):
            continue
        trip_number = str(r.get("trip_number") or "")
        raw = dict(r)
        if st:
            raw["_seat_type"] = st
        output.append(
            TrainInfo(
                trip_id=str(_first(r, "trip_id", "tripId") or (st and st.get("trip_id")) or ""),
                trip_route_id=str(
                    _first(r, "trip_route_id", "tripRouteId") or (st and st.get("trip_route_id")) or ""
                ),
                trip_number=trip_number,
                name=str(
                    _first(r, "train_name", "name", "trainName", "trip_name", "train_name_en") or trip_number
                ),
                departure=str(
                    _first(r, "departure_date_time", "departure", "departure_time", "start_time", "from_time")
                    or ""
                ),
                arrival=str(
                    _first(r, "arrival_date_time", "arrival", "arrival_time", "end_time", "to_time") or ""
                ),
                fare=str(st and st.get("fare")) if st and st.get("fare") else str(_first(r, "fare") or ""),
                seats_left=_strip_seats(st.get("seat_counts")) if st and st.get("seat_counts") else _strip_seats(
                    _first(r, "seats_left", "seat_counts", "available_seats", "seats", "seat_left")
                ),
                raw=raw,
            )
        )
    return output


def has_seats(train: TrainInfo) -> bool:
    if not train.seats_left:
        # Empty header means "unknown"; treat as potentially available.
        return True
    s = train.seats_left.strip().lower()
    if s in {"0", "0.0", "sold out", "soldout", "no", "none", "n/a", "-"}:
        return False
    try:
        return int(float(s)) > 0
    except ValueError:
        return True


def _code(text: str) -> str:
    """Extract the train code, e.g. 816 from 'PARJOTAK EXPRESS (816)' or '816'."""
    compact = text.replace("(", " ").replace(")", " ").strip()
    m = re.search(r"(?<!\d)(\d{2,5})(?!\d)", compact)
    return m.group(1) if m else ""


def find_train(trains: list[TrainInfo], wanted: str) -> TrainInfo | None:
    """Match by full name, trip number, train code or name prefix (normalised).

    Order: exact name -> exact trip_number -> shared train code -> name/trip_number
    prefix. Accepts 'PARJOTAK EXPRESS (816)', 'PARJOTAK EXPRESS' or '816'.
    """
    target = normalize(wanted)
    if not target:
        return None
    target_code = _code(wanted)
    for t in trains:
        name = normalize(t.name)
        trip_number = normalize(t.trip_number or "")
        if name and name == target:
            return t
        if trip_number and trip_number == target:
            return t
        if target_code and _code(t.trip_number) == target_code:
            return t
    for t in trains:
        name = normalize(t.name)
        trip_number = normalize(t.trip_number or "")
        if name and name.startswith(target):
            return t
        if trip_number and trip_number.startswith(target):
            return t
    return None


def _time_key(value: str) -> str:
    """Reduce an API departure string like '07 Oct, 07:45 am' to just the time."""
    match = re.search(r"\d{1,2}:\d{2}\s*(?:am|pm)", (value or "").lower())
    return normalize(match.group(0)) if match else ""


def _train_number(name: str) -> str:
    """Pull the numeric train id out of 'MAHANAGAR PROVATI (704)'."""
    match = re.search(r"\((\d+)\)", name or "")
    return match.group(1) if match else ""


def search_url(cfg: JobConfig) -> str:
    """The SPA results URL, which renders cards straight from these query params.

    Confirmed working on a live session: fromcity / tocity / doj / class. (An
    earlier conclusion that the params were ignored turned out to be a stale
    token bouncing /login to the home page, not an SPA quirk.)
    """
    query = urlencode(
        {
            "fromcity": cfg.from_city,
            "tocity": cfg.to_city,
            "doj": cfg.date,
            "class": cfg.seat_class,
        }
    )
    return f"{settings.base_url}{settings.train_search_path}?{query}"


def _wait_for_cards(driver, by, value, timeout: float = 90.0) -> list:
    """Poll until the results page renders train cards.

    The SPA fetches search-trips-v2 after the route resolves, so cards appear
    seconds after the document is ready (observed ~16s on a loaded server) and
    sometimes not at all when the backend is saturated. Poll rather than sleep
    once, and report what was actually on the page when we give up.

    Wait for the cards to have *text*, not just to exist. The elements are
    inserted by Angular and populated a beat later, so returning on mere
    presence hands the caller empty cards - which then fail to match, while the
    error message a moment later shows perfectly good text and looks like a
    matching bug rather than a race.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        cards = driver.find_elements(by, value)
        if cards:
            try:
                ready = any((c.text or "").strip() for c in cards)
            except Exception:  # noqa: BLE001 - Angular re-renders mid-read; retry
                ready = False
            if ready:
                return cards
        time.sleep(1.0)
    raise RuntimeError(
        f"no train cards rendered on the search page within {timeout:.0f}s. "
        f"Page: {dv.page_summary(driver)}"
    )


def _load_results_once(driver, cfg: JobConfig, by, value, per_attempt: float = 15.0) -> list:
    """One deep-link navigation; return the cards if they render, else raise.

    The SPA fetches search-trips-v2 after the route resolves, so cards appear
    seconds after the document is ready (observed ~14-16s on a loaded server)
    and sometimes not at all when the backend is saturated or the SPA settles
    on the search form. One load is the retry unit - it either produces cards
    or it does not, and reloading is the only remedy.
    """
    driver.get(search_url(cfg))
    dv.wait_for_cloudflare(driver)
    dv.wait_ready(driver)
    return _wait_for_cards(driver, by, value, timeout=per_attempt)


def _load_cards(
    driver,
    cfg: JobConfig,
    by,
    value,
    attempts: int = 4,
    per_attempt: float = 15.0,
) -> list:
    """Load the results page, reloading until cards render, then return them.

    The deep link is unreliable in practice: on roughly a third of loads the SPA
    settles on the search *form* (with "PREV. DAY / MODIFY SEARCH") and issues no
    search-trips call at all, so no cards ever render. Nothing about the
    navigation predicts it - a cold tab, a warm tab, a fresh tab and a home page
    first all fail or succeed equally - so the only thing that helps is
    retrying. A load that does work takes about 14-16s.
    """
    last = ""
    for attempt in range(1, attempts + 1):
        started = time.time()
        try:
            return _load_results_once(driver, cfg, by, value, per_attempt=per_attempt)
        except RuntimeError as exc:
            last = str(exc)
            log.warning(
                "no train cards on attempt %d/%d after %.1fs: %s",
                attempt, attempts, time.time() - started, last,
            )
    raise RuntimeError(f"no train cards after {attempts} attempts. {last}")


def load_results_card(
    driver,
    cfg: JobConfig,
    train: TrainInfo,
    rank: int | None = None,
    attempts: int = 4,
    per_attempt: float = 15.0,
):
    """Load the results page by deep link and return the target train's card.

    The collapsed card does NOT render the train name, so we match by journey
    time (departure_* from the API response) with a rank fallback (the SPA lists
    cards in the same order as data.trains).

    Stopping here rather than opening the seat modal keeps the token harvest
    possible: the results page carries a live Turnstile widget, while the seat
    page does not, so the caller can read a token before it needs the modal.
    """
    from app.models.selectors import sel
    from app.rpa.api_client import DiscoveryRequired

    if not sel.SEARCH_TRAIN_CARD or not sel.SEARCH_BOOK_NOW_BUTTON:
        raise DiscoveryRequired("search/book selectors missing - run bin/discover.py")

    by, value = dv.locator(sel.SEARCH_TRAIN_CARD)
    last = ""
    for attempt in range(1, attempts + 1):
        started = time.time()
        try:
            return _match_card(_load_results_once(driver, cfg, by, value, per_attempt), train, rank)
        except RuntimeError as exc:
            last = str(exc)
            log.warning(
                "results page attempt %d/%d in %.1fs: %s",
                attempt, attempts, time.time() - started, last,
            )
    raise RuntimeError(f"no train card after {attempts} attempts. {last}")


def _match_card(cards, train: TrainInfo, rank: int | None = None):
    """Pick the target train's card out of the rendered results."""
    dep_time = _time_key(train.departure or "")
    number = _train_number(train.name or "")
    name_key = normalize((train.name or "").split("(")[0])

    # Prefer the departure time - the card does not repeat the API's full
    # "07 Oct, 07:45 am" string, only the time. Fall back to the train number,
    # then the name, then the card's position.
    picked = None
    for needle, label in (
        (dep_time, "departure time"),
        (number, "train number"),
        (name_key, "train name"),
    ):
        if not needle:
            continue
        for card in cards:
            if needle in normalize(card.text or ""):
                picked = card
                log.info("Matched card %d by %s (%r)", cards.index(card), label, needle)
                break
        if picked is not None:
            break
    if picked is None and rank is not None and rank < len(cards):
        picked = cards[rank]  # page order is the API order

    if picked is None:
        shown = [" ".join((c.text or "").split())[:60] for c in cards]
        raise RuntimeError(
            f"could not locate train card for {train.name!r} "
            f"(time={dep_time!r} number={number!r}). Cards: {shown}"
        )
    return picked


def load_results_card_with_token(
    driver,
    cfg: JobConfig,
    train: TrainInfo,
    rank: int | None = None,
    attempts: int = 4,
    per_attempt: float = 15.0,
) -> tuple:
    """Return the target train's card and a live Turnstile token, or keep retrying.

    Cards and the token arrive together or not at all: the SPA only builds its
    Turnstile widget as part of the search it runs to produce the cards, so a
    load that renders no cards has no widget either (measured: 4 of 6 loads
    gave both, the other 2 gave neither). So the two cannot be retried
    separately - the page load itself is the retry unit, and we only accept a
    load that produced both.

    Each attempt is exactly one page load (the nested loop of two retry layers
    used to burn minutes on a dead load); the budget is `attempts` loads of up
    to `per_attempt` seconds each plus a short token window.

    The results page is the only place with a readable token. The seat modal's
    widget is consumed by the SPA itself and leaves nothing behind to poll.
    """
    from app.models.selectors import sel

    last = ""
    for attempt in range(1, attempts + 1):
        started = time.time()
        try:
            card = load_results_card(
                driver, cfg, train, rank=rank, attempts=1, per_attempt=per_attempt
            )
            token = dv.harvest_turnstile_token(driver, timeout=10)
            if token:
                log.info(
                    "results page attempt %d/%d ok in %.1fs",
                    attempt, attempts, time.time() - started,
                )
                return card, token
            last = "cards rendered but Cloudflare issued no Turnstile token"
        except RuntimeError as exc:
            last = str(exc)
        log.warning(
            "results page attempt %d/%d unusable in %.1fs: %s",
            attempt, attempts, time.time() - started, last,
        )
    raise RuntimeError(
        f"could not get a train card and a Turnstile token after {attempts} attempts. {last}"
    )


def book_now_for_class(driver, card, seat_class: str):
    """Return the BOOK NOW button for one class row inside a train card.

    Each card lists one class row per bookable class (`div.single-seat-class`
    showing `S_CHAIR ৳495 ... BOOK NOW`), so the *first* book-now button is not
    necessarily the class we want - clicking it is how an S_CHAIR booking
    slipped through while AC_S was requested. The row's text starts with the
    class code, so the right button is the one whose row matches the class name
    (normalised, e.g. `ac_s`). Falls back to the first button so old callers
    keep working.
    """
    from selenium.webdriver.common.by import By

    from app.models.selectors import sel
    from app.rpa.api_client import DiscoveryRequired

    rows = card.find_elements(By.CSS_SELECTOR, "div.single-seat-class")
    texts = []
    for row in rows:
        try:
            texts.append(row.text or "")
        except Exception:  # noqa: BLE001 - Angular re-renders mid-read; skip
            texts.append("")
    want = normalize(seat_class) + " "
    for idx, text in enumerate(texts):
        if normalize(text).startswith(want):
            buttons = rows[idx].find_elements(By.CSS_SELECTOR, "button.book-now-btn")
            if buttons:
                return buttons[0]
            break
    fallback = card.find_elements(*dv.locator(sel.SEARCH_BOOK_NOW_BUTTON))
    if fallback:
        return fallback[0]
    raise DiscoveryRequired("no BOOK NOW button found in the train card")



def open_seat_modal(driver, card, seat_class: str | None = None) -> None:
    """Click Book Now on a card (the class-specific one, when given) and wait for the grid."""
    from app.models.selectors import sel
    from app.rpa.api_client import DiscoveryRequired

    if not sel.SEARCH_BOOK_NOW_BUTTON:
        raise DiscoveryRequired("search/book selectors missing - run bin/discover.py")
    if not sel.SEAT_GRID_CONTAINER:
        raise DiscoveryRequired("SEAT_GRID_CONTAINER selector missing - run bin/discover.py")

    button = book_now_for_class(driver, card, seat_class) if seat_class else \
        card.find_element(*dv.locator(sel.SEARCH_BOOK_NOW_BUTTON))
    dv.js_click(driver, button)
    if not dv.field_present(driver, sel.SEAT_GRID_CONTAINER, timeout=40):
        raise RuntimeError(
            f"seat layout modal did not open after Book Now. Page: {dv.page_summary(driver)}"
        )


def open_train_layout(driver, cfg, train: TrainInfo, rank: int | None = None) -> None:
    """Load the results page, then open the target train's seat layout modal."""
    open_seat_modal(driver, load_results_card(driver, cfg, train, rank=rank), cfg.seat_class)


def poll_for_train(client: Any, job: BookingJob, stop: Any) -> TrainInfo:
    """Poll search until the target train appears with seats, or timeout."""
    cfg = job.config
    interval = cfg.retry_delay or 2.0
    deadline = time.time() + float(cfg.timeout_min) * 60
    while not stop.cancelled:
        if time.time() > deadline:
            raise TimeoutError("Timed out waiting for the target train to become available")
        try:
            payload = client.search_trips(cfg.from_city, cfg.to_city, cfg.date, cfg.seat_class)
        except Exception as exc:
            log.warning("search poll error: %s", exc)
            time.sleep(interval)
            continue
        trains = parse_trains(payload, cfg.seat_class)
        job.trains = trains
        job.add_event("polling", f"{len(trains)} trains returned")
        train = find_train(trains, cfg.train_name)
        if train and has_seats(train):
            job.set_state(RunState.FOUND, f"Train available: {train.name} ({train.seats_left} seats)")
            log.info("Found target train: %s seats_left=%s", train.name, train.seats_left)
            return train
        time.sleep(interval)
    raise RuntimeError("Cancelled while polling for train")