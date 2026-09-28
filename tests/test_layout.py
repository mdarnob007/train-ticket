from __future__ import annotations

from app.rpa.layout import parse_seat_layout


def cell(
    availability: int,
    seat_number: str = "",
    ticket_id: int = 0,
    is_hidden: bool = False,
    ticket_type: int | None = None,
) -> dict:
    """One layout cell. Blank grid spots keep a ticket_id but no seat number."""
    return {
        "isHidden": is_hidden,
        "seat_availability": availability,
        "seat_number": seat_number,
        "ticket_id": ticket_id,
        "ticket_type": 1 if (ticket_type is None and seat_number) else (0 if ticket_type is None else ticket_type),
    }


def _real_payload() -> dict:
    return {
        "data": {
            "seatLayout": [
                {
                    "floor_name": "KHA",
                    "seat_floor": 0,
                    "seat_type": 0,
                    "seat_availability": True,
                    "seat_fare": "985.00",
                    "layout": [
                        [cell(1, "KHA-1", 1001), cell(2, "KHA-2", 1002), cell(0, "KHA-3", 1003)],
                        [cell(0, "", 0), cell(0, "KHA-4", 1004, is_hidden=True)],
                    ],
                },
                {
                    "floor_name": "XTR1",
                    "seat_floor": 1,
                    "seat_type": 1,
                    "seat_availability": True,
                    "seat_fare": "1281.00",
                    "layout": [[cell(1, "XTR1-1", 2001), cell(0, "XTR1-2", 2002)]],
                },
            ],
            "totalSeats": 6,
            "reservationFee": 15,
            "reservationFeeUnit": "BDT",
        },
        "extra": {"hash": "abc123"},
    }


def test_parses_real_seat_layout_shape():
    seats = parse_seat_layout(_real_payload())
    by_number = {s.seat_number: s for s in seats}
    assert set(by_number) == {"KHA-1", "KHA-2", "KHA-3", "XTR1-1", "XTR1-2"}
    assert by_number["KHA-1"].status == "available"
    assert by_number["KHA-2"].status == "held"
    assert by_number["KHA-3"].status == "booked"
    assert by_number["XTR1-2"].status == "booked"
    assert by_number["KHA-1"].coach == "KHA"
    assert by_number["KHA-1"].id == "1001"
    assert by_number["KHA-1"].raw["ticket_id"] == 1001


def test_filters_empty_and_hidden_cells():
    seats = parse_seat_layout(_real_payload())
    assert all(s.seat_number for s in seats)
    assert all(not s.raw.get("isHidden") for s in seats)


def test_group_seat_type_is_not_treated_as_seat_quality():
    """Coach-level `seat_type` marks the class a coach belongs to.

    Reading it as seat quality mislabelled the fully sold-out coach (XTR1,
    seat_type 1) as premium while the coaches with free seats (seat_type 0)
    were treated as standard - so availability must come from the cell alone.
    """
    seats = parse_seat_layout(_real_payload())
    assert {s.coach for s in seats if s.status == "available"} == {"KHA", "XTR1"}
    assert all(s.raw.get("floor_seat_type") in (0, 1) for s in seats)
    assert not any("seat_type" in s.raw and s.raw["seat_type"] in (0, 1) for s in seats)


def test_excludes_non_bookable_ticket_types():
    """ticket_type 4 seats carry a real seat number but are not bookable."""
    payload = _real_payload()
    payload["data"]["seatLayout"][0]["layout"][0].append(cell(1, "KHA-9", 1009, ticket_type=4))
    numbers = {s.seat_number for s in parse_seat_layout(payload)}
    assert "KHA-9" not in numbers


def test_empty_available_cells_are_dropped_not_kept_as_phantom_seats():
    """A cell with no seat number is a grid gap, whatever its availability."""
    payload = _real_payload()
    payload["data"]["seatLayout"][0]["layout"].append([cell(1, "", 1099)])
    seats = parse_seat_layout(payload)
    assert all(s.seat_number for s in seats)
    assert "1099" not in {s.id for s in seats}


def test_recognised_shape_with_nothing_bookable_returns_empty():
    """Must not fall through to the generic guesses and emit nonsense."""
    payload = _real_payload()
    for group in payload["data"]["seatLayout"]:
        for row in group["layout"]:
            for c in row:
                c["seat_availability"] = 0
                c["ticket_type"] = 4
    assert parse_seat_layout(payload) == []


def test_returns_empty_for_unknown_shape():
    assert parse_seat_layout({"data": {"unexpected": [1, 2]}}) == []


def test_legacy_seat_rows_still_parse():
    payload = {"data": {"seats": [{"seat_name": "S-1", "carriage": "A", "status": "available"}]}}
    seats = parse_seat_layout(payload)
    assert seats[0].seat_number == "S-1"
    assert seats[0].status == "available"

def _c(avail: int, number: str, ticket_id: int, ttype: int = 1) -> dict:
    return {
        "isHidden": False,
        "seat_availability": avail,
        "seat_number": number,
        "ticket_id": ticket_id,
        "ticket_type": ttype,
    }


def _coach(floor_name: str, seat_floor: int, seat_type: int, rows: list[list[dict]]) -> dict:
    return {
        "floor_name": floor_name,
        "seat_floor": seat_floor,
        "seat_type": seat_type,
        "fare_type_id": 0,
        "seat_fare_type": "",
        "seat_availability": True,
        "seat_fare": "895.00",
        "layout": rows,
    }


def _live_capture_payload() -> dict:
    """Reduced but faithful copy of the live AC_S seat-layout capture (2026-09-27).

    Keeps every distinguishing feature of the real response: blank grid cells
    that still carry a ticket_id, seat_type 4 positions, real seat numbers on a
    ticket_type 0 coach, a coach whose group seat_availability is false, and the
    trailing `extra.hash`. Available seat numbers and counts are verbatim.
    """
    gap = [[_c(0, "", 9000 + i, 0) for i in range(4)]]
    ga = [
        [_c(0, "", 627508685, 0), _c(1, "GA-3", 627508686), _c(1, "GA-2", 627508687), _c(1, "GA-1", 627508688)],
        [_c(0, "", 627508689, 0), _c(1, "GA-6", 627508690), _c(0, "GA-5", 627508691), _c(0, "GA-4", 627508692)],
        *gap,
        [_c(0, "", 627508697, 0), _c(0, "GA-9", 627508698), _c(0, "GA-8", 627508699), _c(0, "GA-7", 627508700)],
        [_c(0, "", 627508701, 0), _c(0, "GA-12", 627508702), _c(0, "GA-11", 627508703), _c(0, "GA-10", 627508704)],
        *gap,
        [_c(0, "", 627508709, 0), _c(1, "GA-15", 627508710), _c(1, "GA-14", 627508711), _c(1, "GA-13", 627508712)],
        *gap,
        [_c(0, "", 627508717, 0), _c(1, "GA-18", 627508718), _c(1, "GA-17", 627508719), _c(1, "GA-16", 627508720)],
        *gap,
        [_c(0, "", 627508725, 0), _c(1, "GA-21", 627508726), _c(1, "GA-20", 627508727), _c(1, "GA-19", 627508728)],
        *gap,
        [_c(0, "", 627508733, 0), _c(0, "GA-24", 627508734), _c(1, "GA-23", 627508735), _c(1, "GA-22", 627508736)],
        [_c(0, "", 627508737, 0), _c(0, "GA-27", 627508738), _c(1, "GA-26", 627508739), _c(1, "GA-25", 627508740)],
        *gap,
        [_c(0, "", 627508745, 0), _c(1, "GA-30", 627508746), _c(1, "GA-29", 627508747), _c(1, "GA-28", 627508748)],
        [_c(0, "", 627508749, 0), _c(1, "GA-33", 627508750), _c(1, "GA-32", 627508751), _c(1, "GA-31", 627508752)],
    ]
    gha = [
        [_c(0, "", 627508753, 0), _c(1, "GHA-3", 627508754), _c(1, "GHA-2", 627508755), _c(1, "GHA-1", 627508756)],
        [_c(0, "", 627508757, 0), _c(1, "GHA-6", 627508758), _c(1, "GHA-5", 627508759), _c(1, "GHA-4", 627508760)],
        *gap,
        [_c(0, "", 627508765, 0), _c(0, "GHA-9", 627508766, 4), _c(0, "GHA-8", 627508767, 4), _c(0, "GHA-7", 627508768, 4)],
        [_c(0, "", 627508769, 0), _c(0, "GHA-12", 627508770, 4), _c(0, "GHA-11", 627508771, 4), _c(0, "GHA-10", 627508772, 4)],
        *gap,
        [_c(0, "", 627508777, 0), _c(0, "GHA-15", 627508778, 4), _c(0, "GHA-14", 627508779, 4), _c(0, "GHA-13", 627508780, 4)],
        *gap,
        [_c(0, "", 627508785, 0), _c(0, "GHA-18", 627508786, 4), _c(0, "GHA-17", 627508787, 4), _c(0, "GHA-16", 627508788, 4)],
        *gap,
        [_c(0, "", 627508793, 0), _c(1, "GHA-21", 627508794), _c(1, "GHA-20", 627508795), _c(1, "GHA-19", 627508796)],
        *gap,
        [_c(0, "", 627508801, 0), _c(1, "GHA-24", 627508802), _c(0, "GHA-23", 627508803), _c(0, "GHA-22", 627508804)],
        [_c(0, "", 627508805, 0), _c(1, "GHA-27", 627508806), _c(0, "GHA-26", 627508807), _c(0, "GHA-25", 627508808)],
        *gap,
        [_c(0, "", 627508813, 0), _c(0, "GHA-30", 627508814, 0), _c(0, "GHA-29", 627508815, 0), _c(0, "GHA-28", 627508816, 0)],
        [_c(0, "", 627508817, 0), _c(0, "GHA-33", 627508818, 0), _c(0, "GHA-32", 627508819, 0), _c(0, "GHA-31", 627508820, 0)],
    ]
    uma = [
        [_c(0, "", 627508821, 0), _c(0, "UMA-3", 627508822, 0), _c(0, "UMA-2", 627508823, 0), _c(0, "UMA-1", 627508824, 0)],
        [_c(0, "", 627508825, 0), _c(0, "UMA-6", 627508826, 0), _c(0, "UMA-5", 627508827, 0), _c(0, "UMA-4", 627508828, 0)],
    ]
    return {
        "data": {
            "seatLayout": [
                _coach("GA", 0, 0, ga),
                _coach("GHA", 1, 0, gha),
                {**_coach("UMA", 2, 1, uma), "seat_availability": False},
            ],
            "totalSeats": 204,
            "reservationFee": 30,
            "reservationFeeUnit": "%",
        },
        "extra": {"hash": "EF19EFA325123043835C7D7AFBD760DA"},
    }


def test_live_capture_availability_counts():
    """Verbatim free seats from the live capture: GA 23, GHA 11, UMA 0."""
    seats = parse_seat_layout(_live_capture_payload())
    free = [s.seat_number for s in seats if s.status == "available"]
    assert len(free) == 34
    assert len([n for n in free if n.startswith("GA-")]) == 23
    assert len([n for n in free if n.startswith("GHA-")]) == 11
    assert not [n for n in free if n.startswith("UMA-")]


def test_live_capture_seat_numbers_are_reserve_ready():
    """reserve-seat needs the coach-prefixed form, which the payload already has."""
    seats = parse_seat_layout(_live_capture_payload())
    free = {s.seat_number for s in seats if s.status == "available"}
    assert {"GA-3", "GA-2", "GA-1", "GHA-21", "GHA-24", "GHA-27"} <= free
    assert all("-" in n for n in free)


def test_live_capture_never_yields_blank_grid_cells():
    seats = parse_seat_layout(_live_capture_payload())
    assert all(s.seat_number for s in seats)


def test_live_capture_ticket_type_4_and_0_positions_excluded():
    """GHA's type 4 positions and UMA's whole grid are not bookable."""
    seats = parse_seat_layout(_live_capture_payload())
    assert not [s for s in seats if s.raw.get("ticket_type") == 4]
    assert not [s for s in seats if s.coach == "UMA"]


def test_live_capture_total_seats_is_not_availability():
    """totalSeats counts the whole train; 204 must never be read as free seats."""
    payload = _live_capture_payload()
    assert payload["data"]["totalSeats"] == 204
    assert len([s for s in parse_seat_layout(payload) if s.status == "available"]) == 34
