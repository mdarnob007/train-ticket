from __future__ import annotations

from datetime import datetime

import pytest

from app.rpa.search import _match_card, find_train, has_seats, normalize, parse_trains
from app.models.job import TrainInfo


def test_normalize():
    assert normalize("  SUNDARBAN   EXPRESS ") == "sundarban express"


def test_parse_trains_common_shape():
    payload = {
        "data": {
            "trains": [
                {
                    "train_name": "SUNDARBAN EXPRESS",
                    "trip_number": "726",
                    "trip_id": "123",
                    "trip_route_id": "456",
                    "departure": "08:00",
                    "arrival": "15:40",
                    "fare": 1245,
                    "seat_counts": {"online": 2, "offline": 1},
                },
                {
                    "name": "CHITRA EXPRESS",
                    "trip_number": "764",
                    "trip_id": "999",
                    "route_id": "888",
                    "seats_left": 0,
                },
            ]
        }
    }
    trains = parse_trains(payload)
    assert len(trains) == 2
    t = trains[0]
    assert t.name == "SUNDARBAN EXPRESS"
    assert t.trip_id == "123"
    assert t.trip_route_id == "456"
    assert t.seats_left == "2"
    assert has_seats(t) is True
    assert has_seats(trains[1]) is False


def test_parse_trains_real_eticket_shape():
    payload = {
        "data": {
            "trains": [
                {
                    "trip_number": "PARJOTAK EXPRESS (816)",
                    "departure_date_time": "26 Sep, 06:15 am",
                    "arrival_date_time": "26 Sep, 02:40 pm",
                    "seat_types": [
                        {
                            "type": "AC_S",
                            "trip_id": 8607198,
                            "trip_route_id": 59309432,
                            "route_id": 25713,
                            "fare": "1502.00",
                            "seat_counts": {"online": 4, "offline": 0},
                        }
                    ],
                    "boarding_points": [{"trip_point_id": 111616446, "location_name": "Kamalapur Station"}],
                }
            ]
        }
    }
    trains = parse_trains(payload, seat_class="AC_S")
    assert len(trains) == 1
    t = trains[0]
    assert t.trip_number == "PARJOTAK EXPRESS (816)"
    assert t.trip_id == "8607198"
    assert t.trip_route_id == "59309432"
    assert t.fare == "1502.00"
    assert t.seats_left == "4"


def test_has_seats_unknown_treated_available():
    t = TrainInfo(name="X", seats_left="")
    assert has_seats(t) is True


def test_find_train_by_name_and_code():
    trains = parse_trains(
        {"data": {"trains": [{"train_name": "SUNDARBAN EXPRESS", "trip_number": "726"}]}}
    )
    assert find_train(trains, "sundarban express") is trains[0]
    assert find_train(trains, "726") is trains[0]
    assert find_train(trains, "No Train") is None


def _real_multi_class_payload() -> dict:
    def row(number, classes):
        return {
            "trip_number": number,
            "departure_date_time": "26 Sep, 06:15 am",
            "seat_types": classes,
            "boarding_points": [{"trip_point_id": 111, "location_name": "Kamalapur Station"}],
        }

    return {
        "data": {
            "trains": [
                row("PARJOTAK EXPRESS (816)", [
                    {"type": "AC_S", "trip_id": 1, "trip_route_id": 10, "fare": "1502.00", "seat_counts": {"online": 0, "offline": 0}},
                    {"type": "SNIGDHA", "trip_id": 2, "trip_route_id": 20, "fare": "1254.00", "seat_counts": {"online": 9, "offline": 8}},
                    {"type": "S_CHAIR", "trip_id": 3, "trip_route_id": 30, "fare": "754.00", "seat_counts": {"online": 1, "offline": 0}},
                ]),
                row("SUNDARBAN EXPRESS (726)", [
                    {"type": "AC_S", "trip_id": 4, "trip_route_id": 40, "fare": "1245.00", "seat_counts": {"online": 5, "offline": 0}},
                    {"type": "S_CHAIR", "trip_id": 5, "trip_route_id": 50, "fare": "600.00", "seat_counts": {"online": 2, "offline": 0}},
                ]),
            ]
        }
    }


def test_parse_trains_picks_requested_class():
    trains = parse_trains(_real_multi_class_payload(), seat_class="SNIGDHA")
    assert len(trains) == 1
    t = trains[0]
    assert t.name == "PARJOTAK EXPRESS (816)"
    assert t.trip_id == "2"
    assert t.trip_route_id == "20"
    assert t.fare == "1254.00"
    assert t.seats_left == "9"


def test_parse_trains_skips_train_without_requested_class():
    trains = parse_trains(_real_multi_class_payload(), seat_class="SNIGDHA")
    assert [t.name for t in trains] == ["PARJOTAK EXPRESS (816)"]


def test_find_train_full_code_and_prefix():
    trains = parse_trains(_real_multi_class_payload(), seat_class="AC_S")
    assert find_train(trains, "PARJOTAK EXPRESS (816)") is trains[0]
    assert find_train(trains, "816") is trains[0]
    assert find_train(trains, "PARJOTAK EXPRESS") is trains[0]
    assert find_train(trains, "SUNDARBAN EXPRESS") is trains[1]
    assert find_train(trains, "726") is trains[1]
    assert find_train(trains, "Doesn't Exist") is None

class _Card:
    """Stand-in for a Selenium card element."""

    def __init__(self, text: str):
        self.text = text


def _train(departure: str = "07 Oct, 07:45 am", name: str = "MAHANAGAR PROVATI (704)"):
    return TrainInfo(
        trip_id="1", name=name, trip_route_id="2", departure=departure, seats_left="5"
    )


def test_match_card_prefers_the_departure_time_over_the_train_number():
    cards = [
        _Card("07 OCT, 02:15 PM Dhaka 06h 15m 07 OCT, 08:30 PM Chattogram T"),
        _Card("07 OCT, 07:45 AM Dhaka 05h 50m 07 OCT, 01:35 PM Chattogram T"),
    ]
    assert _match_card(cards, _train()) is cards[1]


def test_match_card_falls_back_to_the_train_number():
    # The card never repeats the API's "07 Oct, 07:45 am", only the time.
    cards = [_Card("07 OCT, 09:20 PM Dhaka 06h 10m 08 OCT, 03:30 AM Chattogram 704")]
    assert _match_card(cards, _train()) is cards[0]


def test_match_card_reports_what_was_on_the_page_when_it_finds_nothing():
    cards = [_Card("07 OCT, 02:15 PM Dhaka 06h 15m 07 OCT, 08:30 PM Chattogram T")]
    with pytest.raises(RuntimeError, match="07:45 am"):
        _match_card(cards, _train())


class _ClassBtn:
    pass


class _ClassRow:
    def __init__(self, text, buttons):
        self._text = text
        self._buttons = buttons

    def find_elements(self, by, value):
        if value == "button.book-now-btn":
            return self._buttons
        return []

    @property
    def text(self):
        return self._text


class _ClassCard:
    def __init__(self, rows):
        self._rows = rows

    def find_elements(self, by, value):
        if value == "div.single-seat-class":
            return self._rows
        return []


def test_book_now_for_class_picks_the_requested_class_row():
    from app.rpa.search import book_now_for_class

    s_chair = _ClassBtn()
    f_seat = _ClassBtn()
    ac_s = _ClassBtn()
    card = _ClassCard([
        _ClassRow("S_CHAIR\n৳495\nAvailable Tickets\n(Counter + Online)\n111\nBOOK NOW", [s_chair]),
        _ClassRow("F_SEAT\n৳685\nAvailable Tickets\n(Counter + Online)\n33\nBOOK NOW", [f_seat]),
        _ClassRow("AC_S\n৳1030\nIncluding VAT\nAvailable Tickets\n(Counter + Online)\n48\nBOOK NOW", [ac_s]),
    ])
    assert book_now_for_class(None, card, "AC_S") is ac_s


def test_book_now_for_class_is_case_insensitive_and_does_not_mix_classes():
    from app.rpa.search import book_now_for_class

    ac_s = _ClassBtn()
    ac_b = _ClassBtn()
    card = _ClassCard([
        _ClassRow("AC_B\n৳1200\nBOOK NOW", [ac_b]),
        _ClassRow("AC_S\n৳1030\nBOOK NOW", [ac_s]),
    ])
    assert book_now_for_class(None, card, "ac_s") is ac_s
