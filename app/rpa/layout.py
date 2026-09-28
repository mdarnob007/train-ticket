from __future__ import annotations

import logging
from typing import Any

from selenium.webdriver.remote.webdriver import WebDriver

from app.models.job import BookingJob, SeatInfo, TrainInfo
from app.rpa import driver as dv
from app.rpa.api_client import DiscoveryRequired

log = logging.getLogger("rpa.layout")


def parse_seat_layout(payload: dict[str, Any]) -> list[SeatInfo]:
    """Parse a seat-layout payload into SeatInfo.

    Real Shape (recovered via bin/api_probe.py, payload captured 2026-09-27):
        data.seatLayout[] = [{floor_name, seat_floor, seat_type, seat_availability,
                              seat_fare, layout: [rows:[cells]]}]
        cell = {isHidden, seat_availability: 0|1, seat_number, ticket_id, ticket_type}
    Notes on the live payload:
      * `seat_number` already carries the coach prefix ("GA-3"), which is exactly
        the form reserve-seat expects, so it is passed through untouched.
      * `seat_availability` is 1 = free, 0 = taken. It is single-valued here; 2
        is still mapped to "held" because the SPA paints that state.
      * `ticket_type` distinguishes a real bookable seat (1) from the blank grid
        positions that still carry a ticket_id (0). Seats of type 4 exist but
        were never bookable, so only type 1 is selectable.
      * group-level `seat_availability` (bool) and `totalSeats` describe the
        coach and the whole train, not what is free - neither is used.
    Falls back to the generic per-seat shapes seen before the live capture.
    """
    data = payload.get("data", payload) if isinstance(payload, dict) else payload

    layout_rows = data.get("seatLayout") if isinstance(data, dict) else None
    if isinstance(layout_rows, list):
        seats: list[SeatInfo] = []

        def add_cell(cell: dict[str, Any], floor_name: str, floor_seat_type: Any) -> None:
            if cell.get("isHidden"):
                return
            availability = cell.get("seat_availability")
            seat_number = str(cell.get("seat_number") or "")
            # Blank grid positions keep a ticket_id but carry no seat number.
            if not seat_number:
                return
            if cell.get("ticket_type") not in (None, 1):
                return
            status = {1: "available", 2: "held"}.get(availability, "booked")
            seats.append(
                SeatInfo(
                    id=str(cell.get("ticket_id") or f"{floor_name}-{seat_number}"),
                    seat_number=seat_number,
                    coach=seat_number.split("-", 1)[0] if "-" in seat_number else str(floor_name or ""),
                    status=status,
                    floor=str(floor_name or ""),
                    raw=dict(cell, floor_seat_type=floor_seat_type),
                )
            )

        for group in layout_rows:
            if not isinstance(group, dict):
                continue
            floor_name = str(group.get("floor_name") or "")
            floor_seat_type = group.get("seat_type")
            layout = group.get("layout")
            if not isinstance(layout, list):
                continue
            for row in layout:
                if not isinstance(row, list):
                    continue
                for cell in row:
                    if isinstance(cell, dict):
                        add_cell(cell, floor_name, floor_seat_type)
        # The live shape was recognised, so an empty result is a real answer
        # ("nothing bookable here") and must not fall through to the guesses
        # below, which would walk seatLayout and emit nonsense.
        return seats

    rows = data.get("seats") or data.get("seat_chart") or data.get("cof") or data.get("coach")
    if rows is None and isinstance(data, list):
        rows = data
    if rows is None and isinstance(data, dict):
        for val in data.values():
            if isinstance(val, list):
                rows = val
                break
    if not isinstance(rows, list):
        return []

    seats: list[SeatInfo] = []
    for seat_id, row in enumerate(rows):
        if isinstance(row, str):
            seats.append(SeatInfo(id=f"s{seat_id}", seat_number=row, coach="", status="available"))
            continue
        if not isinstance(row, dict):
            continue
        seat_number = (
            str(row.get("seat_name") or row.get("seat_number") or row.get("name") or row.get("no") or "")
        )
        coach = str(
            row.get("coach_name")
            or row.get("carriage")
            or row.get("coach")
            or row.get("car")
            or (seat_number.split("-")[0] if "-" in seat_number else "")
        )
        status = str(row.get("status") or "").lower()
        if not status:
            status = "booked" if row.get("is_booked") or row.get("booked") else "available"
        seats.append(
            SeatInfo(
                id=str(row.get("seat_id") or row.get("id") or f"s{seat_id}"),
                seat_number=seat_number,
                coach=coach,
                status=status,
                floor=str(row.get("floor") or row.get("floor_name") or ""),
                raw=row,
            )
        )
    return seats


def fetch_layout_api(client: Any, train: TrainInfo, cft_token: str = "") -> list[SeatInfo]:
    """Fetch the seat layout via the API using the warm session credentials."""
    payload = client.seat_layout(train.trip_id, train.trip_route_id, cft_token)
    return parse_seat_layout(payload)


def fetch_layout_ui(driver: WebDriver, job: BookingJob) -> list[SeatInfo]:
    """Fallback: read the seat grid from the rendered seat-layout tab."""
    from app.models.selectors import sel

    container = sel.SEAT_GRID_CONTAINER
    if not container:
        raise DiscoveryRequired("SEAT_GRID_CONTAINER selector not captured yet - run bin/discover.py")
    dv.wait_for_cloudflare(driver)
    dv.wait_visible(driver, container)
    seats: list[SeatInfo] = []
    if sel.SEAT_AVAILABLE:
        for idx, el in enumerate(driver.find_elements(*dv.locator(sel.SEAT_AVAILABLE))):
            label = el.get_attribute("aria-label") or el.text
            seats.append(
                SeatInfo(id=f"ui-{idx}", seat_number=label, coach=(label.split("-")[0] if "-" in label else ""), status="available")
            )
    if sel.SEAT_BOOKED:
        for el in driver.find_elements(*dv.locator(sel.SEAT_BOOKED)):
            label = el.get_attribute("aria-label") or el.text
            seats.append(SeatInfo(id=f"ui-booked-{label}", seat_number=label, coach="", status="booked"))
    if not seats:
        raise RuntimeError("Seat layout rendered but no seat elements found")
    job.seats = seats
    return seats