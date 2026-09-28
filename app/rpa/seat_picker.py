from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

from app.models.job import SeatInfo

_NUM = re.compile(r"(\d+)")


def _seat_number_num(seat_number: str) -> int | None:
    m = _NUM.search(seat_number or "")
    return int(m.group(1)) if m else None


def _coaches(layout: list[SeatInfo]) -> set[str]:
    return {s.coach for s in layout}


def choose_seats(layout: list[SeatInfo], preferred: list[str], count: int) -> list[SeatInfo]:
    """Ranked seat selection: book preferred seats in order, else fall back.

    Availability is taken straight from the layout: the seat-layout response is
    already scoped to the class that was chosen on the search results (via its
    `trip_route_id`) and marks free cells with `seat_availability == 1`. Nothing
    here inspects the coach-level `seat_type` - that field marks the class a
    coach belongs to, and treating it as seat quality mislabelled the fully
    sold-out coach as "premium" while the two coaches with free seats were
    treated as standard.

    Fallback springs: preferred seats that are available; otherwise any free
    seat, preferring the coach of the first preference.

    Fallback order:
      1. preferred seats that are available, in the order given;
      2. fill the remainder with the free seat in the same coach that is
         *closest to the seats already chosen* (the cluster), lowest seat
         number on a tie - so a missing JA-41 next to JA-25/29/28 is topped
         up with JA-24, keeping the party together;
      3. any free seat in the coach of the first preference;
      4. any free seat anywhere.

    Returns up to `count` seats. Raises ValueError if fewer are available.
    """
    available = [s for s in layout if s.status == "available"]
    if len(available) < count:
        raise ValueError(
            f"only {len(available)} seats available but {count} requested"
            f" (layout: {len(layout)} seats total)"
        )

    pref_norm = [p.strip().upper() for p in preferred if p and p.strip()]

    if not pref_norm:
        # Auto-select with no preference: group by coach so the seats we pick
        # sit together, preferring whichever coach can satisfy the whole party.
        by_coach: dict[str, list[SeatInfo]] = {}
        for s in available:
            by_coach.setdefault(s.coach, []).append(s)
        for seats in by_coach.values():
            if len(seats) >= count:
                return seats[:count]
        groups = sorted(by_coach.values(), key=lambda g: -len(g))
        combined = groups[0][:]
        for g in groups[1:]:
            combined.extend(g)
        return combined[:count]


    chosen: list[SeatInfo] = []
    taken_ids = set()

    def pick(seat: SeatInfo) -> None:
        chosen.append(seat)
        taken_ids.add(seat.id)

    # 1. preferred seats, in order
    for pref in pref_norm:
        if len(chosen) >= count:
            return chosen[:count]
        for seat in available:
            if seat.id in taken_ids:
                continue
            if seat.seat_number and seat.seat_number.upper() == pref:
                pick(seat)
                break

    if len(chosen) >= count:
        return chosen[:count]

    remaining = [s for s in available if s.id not in taken_ids]
    first_pref = pref_norm[0] if pref_norm else None

    def _pref_coach(seat_number: str) -> str:
        return seat_number.partition("-")[0]

    target_coach = ""
    for s in layout:
        if first_pref and s.seat_number and s.seat_number.upper() == first_pref:
            target_coach = s.coach
            break
    if not target_coach and first_pref:
        # The preferred seat itself is absent (e.g. the number is out of that
        # coach's range), but the coach it names may still be the right one to
        # fill from - only give up on it when that coach is not in the layout
        # at all (e.g. a WRONG SEAT_COACH), and only then use the first
        # available seat's coach.
        implied = _pref_coach(first_pref)
        if implied and any(s.upper() == implied for s in _coaches(layout)):
            target_coach = implied
        else:
            target_coach = remaining[0].coach if remaining else ""

    # 2. fill from the same coach, closest to the cluster already chosen
    in_coach = [s for s in remaining if s.coach == target_coach]
    if in_coach:
        # Seed the cluster with the first preference's number when nothing
        # matched at all (e.g. every preferred seat is booked), so the anchor
        # still points at the range the user asked for.
        anchor_nums = [
            _seat_number_num(s.seat_number) for s in chosen if s.coach == target_coach
        ]
        if not anchor_nums and first_pref and "-" in first_pref:
            anchor_nums = [_seat_number_num(first_pref)]
        anchor_nums = [n for n in anchor_nums if n is not None]

        def _cluster_key(seat: SeatInfo) -> tuple[int, int]:
            n = _seat_number_num(seat.seat_number)
            if anchor_nums and n is not None:
                return (min(abs(n - a) for a in anchor_nums), n)
            return (0, n if n is not None else 0)

        while len(chosen) < count and in_coach:
            seat = min(in_coach, key=_cluster_key)
            in_coach.remove(seat)
            anchor_nums.append(_seat_number_num(seat.seat_number) or 0)
            pick(seat)

    # 3. any free seat in the preferred coach
    while len(chosen) < count and in_coach:
        seat = in_coach.pop(0)
        if seat.id in taken_ids:
            continue
        pick(seat)

    # 4. any free seat anywhere
    while len(chosen) < count and remaining:
        seat = remaining.pop(0)
        if seat.id in taken_ids:
            continue
        pick(seat)

    # re-sort chosen to preferred order for stable seat numbers if possible
    chosen = chosen[:count]
    return chosen


def _auto_coach(layout: list[SeatInfo], count: int) -> str:
    """Coach the auto-select would pick: first that can seat the whole party."""
    by_coach: dict[str, int] = {}
    for s in layout:
        if s.status == "available":
            by_coach[s.coach] = by_coach.get(s.coach, 0) + 1
    for coach, free in by_coach.items():
        if free >= count:
            return coach
    return next(iter(by_coach), "")


def resolve_preferences(prefs: str | list[str] | None, coach: str, layout: list[SeatInfo]) -> list[str]:
    """Build the ranked preferred-seat list (COACH-NUM) from env-style input.

    Accepts a comma/space separated string or a list of mixed tokens:
      "24,25,29,28"   bare numbers -> each prefixed with `coach`, or with the
                      auto coach (first that can seat the whole party) when no
                      coach is given, so "you add the coach name before the
                      number"
      "24, GA-10"     inline COACH-NUM tokens pass through unchanged
      "GA-24,KA-30"   all explicit, per-seat coaches

    Empty input returns [] so the caller keeps today's auto-select behaviour.
    Numbers are normalised (leading zeros dropped) so "GA-04" matches the real
    seat_number "GA-4", and tokens are deduped order-preserving. The ranked
    choice itself (preferred seats first, then whatever is free on the same
    coach) belongs to choose_seats.
    """
    raw = re.split(r"[\s,]+", prefs) if isinstance(prefs, str) else list(prefs or [])
    bare = [t for t in raw if t.strip() and "-" not in t]
    resolved_coach = coach.strip().upper() or (_auto_coach(layout, len(bare)) if bare else "")

    result: list[str] = []
    seen: set[str] = set()

    def _token(part: str) -> str:
        part = part.strip()
        if not part:
            return ""
        if "-" in part:
            code, sep, number = part.partition("-")
            return f"{code.strip().upper()}-{_norm_num(number)}"
        if resolved_coach:
            return f"{resolved_coach}-{_norm_num(part)}"
        return _norm_num(part)

    for part in raw:
        token = _token(part)
        if token and token not in seen:
            result.append(token)
            seen.add(token)
    return result


def _norm_num(piece: str) -> str:
    piece = piece.strip()
    return str(int(piece)) if piece.isdigit() else piece.upper()


_STATE_CLASSES = {
    "in_progress": "seat-in-progress",
    "booked": "seat-booked",
    "selected": "seat-selected",
    "available": "seat-available",
}


@dataclass(frozen=True)
class SeatTarget:
    """A seat on the page, resolved for clicking (nothing clicked here)."""

    id: str
    seat_number: str
    coach: str
    wrapper: WebElement
    button: WebElement
    state: str

    def describe(self) -> str:
        return (
            f"{self.coach}-{self.seat_number.split('-')[-1]} "
            f"(ticketid={self.id}, state={self.state})"
        )


def seat_locator(ticket_id: str, route_id: str) -> str:
    """CSS selector for one specific seat, matched by the ids the grid carries.

    The wrapper is `div.seat-wrapper[ticketid=...][routeid=...]` and the
    clickable element inside is `button.btn-seat`. Matching on the ticket id
    rather than the rendered label is what makes the choice from our API layout
    line up with the SPA's grid: `ticket_id` is a server-side per-seat id that
    both responses share.
    """
    return (
        f"div.seat-wrapper[ticketid='{ticket_id}'][routeid='{route_id}'] button.btn-seat"
    )


def seat_state(button: WebElement) -> str:
    """Classify a grid seat button into its state.

    The SPA keeps three rendered states on the page - free, booked (someone
    else's hold or a sale), in-progress (someone else is mid-reserve) - and adds
    a fourth, selected, only after we click. The `seat-booked` and
    `seat-in-progress` seats are disabled, so those are refused before any click.
    """
    classes = (button.get_attribute("class") or "").split()
    for state, cls in _STATE_CLASSES.items():
        if cls in classes:
            return state
    return "unknown"


def select_coach(driver: WebDriver, coach: str) -> None:
    """Switch the grid to render `coach` via the Select Coach dropdown.

    The grid renders one coach at a time; the picker is a `<select>` inside
    `app-seat-layout` whose options read `"GA - 23 Seat(s)"`. The value is set
    and a `change` event dispatched so Angular's change detection picks it up.
    """
    from app.rpa.driver import locator
    from app.models.selectors import sel

    if not sel.SEAT_COACH_PICKER:
        raise RuntimeError("SEAT_COACH_PICKER selector missing - run bin/discover.py")

    by, value = locator(sel.SEAT_COACH_PICKER)
    options = driver.find_elements(by, f"{value} option")
    option_value = _coach_option_value(options, coach)
    if option_value is None:
        labels = [o.text for o in options]
        raise RuntimeError(
            f"coach {coach!r} not offered by the picker; options: {labels}"
        )
    driver.execute_script(
        """
        const sel = arguments[0];
        sel.value = arguments[1];
        sel.dispatchEvent(new Event('change', {bubbles: true}));
        """,
        driver.find_element(by, value),
        option_value,
    )


def _coach_option_value(options: list[WebElement], coach: str) -> str | None:
    """Return the select's value for the option labelled `COACH - N Seat(s)`."""
    prefix = (coach or "").strip().upper() + " -"
    for option in options:
        if prefix and (option.text or "").strip().upper().startswith(prefix):
            return option.get_attribute("value")
    return None


def resolve_seat_element(
    driver: WebDriver, seat: SeatInfo, train_route_id: str
) -> SeatTarget | None:
    """Find one chosen seat in the grid and say what state it is in.

    Returns None when the seat is not rendered at all (wrong coach showing, or
    the layout differs from ours). Raises RuntimeError if it is rendered but no
    longer clickable, and returns a SeatTarget only for a currently available
    seat - this is the dry-run gate that keeps one seat from being half-started.
    """
    from app.rpa.driver import locator

    by, value = locator(seat_locator(seat.id or "", train_route_id))
    buttons = driver.find_elements(by, value)
    if not buttons:
        return None
    button = buttons[0]
    state = seat_state(button)
    if state != "available":
        raise RuntimeError(
            f"seat {seat.seat_number} is {state}, not available - refusing to click"
        )
    wrapper = button.find_element(By.XPATH, "ancestor::div[contains(@class,'seat-wrapper')]")
    return SeatTarget(seat.id, seat.seat_number, seat.coach, wrapper, button, state)


def resolve_in_grid(
    driver: WebDriver, seats: list[SeatInfo], route_id: str, wait: float = 8.0
) -> list[SeatTarget]:
    """Resolve every chosen seat in the rendered grid, switching coaches as needed.

    The grid shows one coach at a time, so each new coach is selected first and
    the seat given a moment to render. Purely a resolution step - nothing is
    clicked and no hold is taken.
    """
    targets: list[SeatTarget] = []
    seen_coaches: set[str] = set()
    for seat in seats:
        if seat.coach not in seen_coaches:
            select_coach(driver, seat.coach)
            seen_coaches.add(seat.coach)
        deadline = time.time() + wait
        target = None
        while time.time() < deadline:
            target = resolve_seat_element(driver, seat, route_id)
            if target is not None:
                break
            time.sleep(0.5)
        if target is None:
            raise RuntimeError(
                f"seat {seat.seat_number} never rendered in the grid after selecting coach "
                f"{seat.coach} - the layout no longer matches the page"
            )
        targets.append(target)
    return targets


def select_seats_in_grid(
    driver: WebDriver, seats: list[SeatInfo], route_id: str
) -> list[SeatTarget]:
    """Click the chosen seats in the grid so the SPA itself owns the hold.

    This is what makes the handoff work: the SPA only treats a seat as chosen
    when it took the seat itself, and the whole failure you saw came from
    reserving behind its back. Clicking order is `js_click` then a native
    `.click()`, both matching how the SPA's own handler is reached.

    A successful click leaves `seat-available` (either selected or passed into
    an in-progress reserve); anything else means the seat was taken between the
    API layout and the click and is left untouched.
    """
    from app.rpa.driver import js_click

    targets = resolve_in_grid(driver, seats, route_id)
    return _click_targets(driver, targets)


def _click_targets(driver: WebDriver, targets: list[SeatTarget]) -> list[SeatTarget]:
    """Click a list of already-resolved targets and verify each registered.

    The SPA reacts to the press sequence (mousedown/mouseup/click), not to a
    bare DOM `element.click()`, so each technique is tried and *verified* before
    the next: the first one that leaves the seat no longer `available` wins.
    """
    from app.rpa.driver import js_click

    def registered(target: SeatTarget, window: float) -> bool:
        deadline = time.time() + window
        while time.time() < deadline:
            if seat_state(target.button) != "available":
                return True
            time.sleep(0.3)
        return False

    clicked: list[SeatTarget] = []
    for target in targets:
        if seat_state(target.button) != "available":
            raise RuntimeError(f"{target.describe()} is no longer available - refused to click")

        confirmed = False
        for fire in (
            lambda: driver.execute_script(
                "arguments[0].dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));"
                "arguments[0].dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));"
                "arguments[0].dispatchEvent(new MouseEvent('click', {bubbles:true}));",
                target.button,
            ),
            lambda: js_click(driver, target.button),
            lambda: target.button.click(),
        ):
            try:
                fire()
            except Exception:  # noqa: BLE001
                continue
            if registered(target, 3.0):
                confirmed = True
                break
        if not confirmed:
            raise RuntimeError(f"{target.describe()} did not register - no selection was made")
        clicked.append(target)
    return clicked


class GridSeat:
    """A seat parsed straight from the rendered grid (one coach at a time).

    Unlike SeatInfo from the API, this is what the SPA itself is showing, so its
    `ticket_id`/`route_id` always exist on the page the browser will act on.
    """

    __slots__ = ("ticket_id", "route_id", "seat_number", "coach", "state")

    def __init__(
        self, ticket_id: str, route_id: str, seat_number: str, state: str
    ) -> None:
        self.ticket_id = ticket_id
        self.route_id = route_id
        self.seat_number = seat_number
        self.coach = seat_number.split("-")[0] if "-" in seat_number else ""
        self.state = state

    def describe(self) -> str:
        return (
            f"{self.seat_number} (ticketid={self.ticket_id}, route={self.route_id}, "
            f"state={self.state})"
        )


def modal_coaches(driver: WebDriver) -> set[str]:
    """The coach codes the seat modal's picker offers, e.g. {'KA', 'THA'}.

    Labels read `"GA - 23 Seat(s)"`; the code is the token before the hyphen.
    Used as the class guard: the API layout's coach set (GA/GHA/UMA for AC_S)
    must overlap what the SPA is showing, or the modal was opened for the wrong
    class and no seat should be clicked.
    """
    labels = driver.execute_script(
        """
        const sel = document.querySelector('app-seat-layout select.form-control');
        return sel ? [...sel.options].map(o => (o.text || '').trim()) : [];
        """
    )
    return {label.split(" - ")[0].strip().upper() for label in labels if " - " in label}


def verify_modal_class(driver: WebDriver, expected_coaches: set[str]) -> bool:
    """True when the modal's coach set overlaps the expected (API layout) one."""
    shown = modal_coaches(driver)
    if not expected_coaches or not shown:
        return False
    return bool(expected_coaches & shown)


def grid_seats(driver: WebDriver, wait: float = 8.0) -> list[GridSeat]:
    """Parse every rendered seat wrapper - the SPA's ground-truth availability.

    The grid renders one coach at a time; this captures only the coach currently
    shown, so callers that want another coach must `select_coach` first.
    """
    deadline = time.time() + wait
    while True:
        payload = driver.execute_script(
            """
            return JSON.stringify(
              [...document.querySelectorAll('div.seat-wrapper[ticketid][routeid]')]
                .map(w => {
                  const b = w.querySelector('button.btn-seat');
                  if (!b) return null;
                  return {
                    ticketid: w.getAttribute('ticketid'),
                    routeid: w.getAttribute('routeid'),
                    seat: (b.innerText || b.getAttribute('aria-label') || '').trim(),
                    cls: (b.className || '').trim(),
                  };
                })
                .filter(Boolean)
            );
            """
        )
        seats = [_json_to_grid_seat(item) for item in json.loads(payload)]
        if seats or time.time() >= deadline:
            return seats
        time.sleep(0.5)


def _json_to_grid_seat(item: dict) -> GridSeat:
    classes = (item.get("cls") or "").split()
    state = "unknown"
    for state_name, cls in _STATE_CLASSES.items():
        if cls in classes:
            state = state_name
            break
    return GridSeat(
        ticket_id=item.get("ticketid") or "",
        route_id=item.get("routeid") or "",
        seat_number=item.get("seat") or "",
        state=state,
    )


def pick_fallback_seats(grid: list[GridSeat], count: int) -> list[GridSeat]:
    """Choose `count` free seats straight from the grid when the API choice does
    not line up with what the SPA is showing.

    Preference: a run of `count` consecutive seat numbers in one coach so the
    party sits together (mirroring `choose_seats` for the API layout). If no
    coach has a full run, the lowest-numbered free seats are used.
    """
    free = [s for s in grid if s.state == "available"]
    if len(free) < count:
        raise ValueError(
            f"only {len(free)} seats free in the current coach but {count} requested"
        )
    by_coach: dict[str, list[GridSeat]] = {}
    for seat in free:
        by_coach.setdefault(seat.coach, []).append(seat)
    for coach_seats in sorted(
        by_coach.values(), key=lambda g: (-len(g), min(_seat_number_num(s.seat_number) or 0 for s in g))
    ):
        cohort = sorted(coach_seats, key=lambda s: _seat_number_num(s.seat_number) or 0)
        for i in range(len(cohort) - count + 1):
            run = cohort[i : i + count]
            nums = [_seat_number_num(s.seat_number) for s in run]
            if all(nums[k] is not None and nums[k] == nums[0] + k for k in range(count)):
                return run
    return sorted(
        free, key=lambda s: (_seat_number_num(s.seat_number) or 0, s.seat_number)
    )[:count]


def select_grid_seats(driver: WebDriver, picks: list[GridSeat]) -> list[SeatTarget]:
    """Click grid-parsed seats so the SPA itself owns the hold."""
    from app.rpa.driver import locator

    targets: list[SeatTarget] = []
    for pick in picks:
        by, value = locator(seat_locator(pick.ticket_id, pick.route_id))
        buttons = driver.find_elements(by, value)
        if not buttons:
            raise RuntimeError(f"{pick.describe()} disappeared before it could be clicked")
        button = buttons[0]
        wrapper = button.find_element(By.XPATH, "ancestor::div[contains(@class,'seat-wrapper')]")
        targets.append(
            SeatTarget(pick.ticket_id, pick.seat_number, pick.coach, wrapper, button, pick.state)
        )
    return _click_targets(driver, targets)


def _continue_button(driver: WebDriver, wait: float = 10.0) -> WebElement | None:
    """Poll for the Continue button and report when it is enabled (polled, not clicked)."""
    from app.models.selectors import sel
    from app.rpa.driver import locator

    if not sel.SEAT_CONTINUE_BUTTON:
        raise RuntimeError("SEAT_CONTINUE_BUTTON selector missing - run bin/discover.py")
    by, value = locator(sel.SEAT_CONTINUE_BUTTON)
    deadline = time.time() + wait
    while time.time() < deadline:
        buttons = driver.find_elements(by, value)
        for button in buttons:
            classes = (button.get_attribute("class") or "").split()
            if not button.is_displayed() or "disabled" in classes:
                continue
            if button.get_attribute("disabled") == "true":
                continue
            return button
        time.sleep(0.5)
    return None


def activate_continue(driver: WebDriver, wait: float = 10.0) -> WebElement:
    """Wait until the Continue button is enabled and click it once.

    The SPA keeps Continue disabled until at least one seat is selected, so
    selecting the seats comes first. The click uses the same js-then-native
    ordering as the seat clicks.
    """
    from app.rpa.driver import js_click

    button = _continue_button(driver, wait)
    if button is None:
        raise RuntimeError("Continue stayed disabled - no seat selection was registered")
    for click in (js_click, button.click):
        try:
            click(driver, button)
            return button
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("could not click Continue by any method")


def wait_for_passenger_page(driver: WebDriver, wait: float = 60.0) -> bool:
    """True once Continue has navigated off the seat grid to the passenger form.

    The passenger step is detected three ways, whichever comes first: the
    captured name input (`#pname0`) is visible; the seat modal's grid is gone
    for a stable interval (the SPA replaces the modal with the next step); or
    the URL moved to a booking sub-route. A long window is used because the SPA
    can sit on the results page for a while before the step transition settles.
    """
    from app.models.selectors import sel
    from app.rpa.driver import locator

    name_present = bool(sel.PASSENGER_NAME_INPUT)
    name_by = name_present and locator(sel.PASSENGER_NAME_INPUT)
    grid_present = bool(sel.SEAT_GRID_CONTAINER)
    grid_by = grid_present and locator(sel.SEAT_GRID_CONTAINER)

    deadline = time.time() + wait
    grid_gone_since = None
    while time.time() < deadline:
        if name_present:
            for element in driver.find_elements(*name_by):
                if element.is_displayed():
                    return True
        if grid_present:
            if not driver.find_elements(*grid_by):
                if grid_gone_since is None:
                    grid_gone_since = time.time()
                elif time.time() - grid_gone_since > 3.0:
                    return True
            else:
                grid_gone_since = None
        try:
            if "booking" in driver.current_url and "search" not in driver.current_url:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)

    from app.rpa import driver as dv

    try:
        url = driver.current_url
    except Exception:  # noqa: BLE001
        url = "<unreadable>"
    log = logging.getLogger("seat_picker")
    log.info(
        "passenger page never appeared after %.1fs (url=%s) page: %s",
        wait,
        url,
        dv.page_summary(driver),
    )
    return False