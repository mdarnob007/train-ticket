from __future__ import annotations

import pytest

from app.rpa.seat_picker import (
    GridSeat,
    _coach_option_value,
    choose_seats,
    grid_seats,
    pick_fallback_seats,
    resolve_seat_element,
    seat_locator,
    seat_state,
    select_coach,
)
from app.models.job import SeatInfo


def seat(seat_number: str, coach: str, status: str = "available", sid: str | None = None, raw: dict | None = None) -> SeatInfo:
    return SeatInfo(
        id=sid or f"{coach}:{seat_number}",
        seat_number=seat_number,
        coach=coach,
        status=status,
        raw=raw or {},
    )


def alt_class_seat(seat_number: str, status: str = "available") -> SeatInfo:
    """A seat in a coach whose group-level seat_type is 1 (a different class)."""
    return seat(seat_number, seat_number.split("-")[0], status, raw={"floor_seat_type": 1})


LAYOUT = [
    seat("KHA-1", "KHA"), seat("KHA-2", "KHA"), seat("KHA-3", "KHA"), seat("KHA-4", "KHA"),
    seat("KHA-5", "KHA"), seat("KHA-6", "KHA"),
    seat("KHA-7", "KHA", "booked"), seat("KHA-8", "KHA", "booked"),
    seat("KHA-9", "KHA", "held"),
    seat("KHA-10", "KHA"), seat("KHA-23", "KHA"), seat("KHA-21", "KHA"), seat("KHA-24", "KHA"),
    seat("KHB-1", "KHB"), seat("KHB-2", "KHB"), seat("KHB-3", "KHB"),
]


def test_preferred_seats_booked_in_order():
    chosen = choose_seats(LAYOUT, ["KHA-23", "KHA-21"], 2)
    assert [s.seat_number for s in chosen] == ["KHA-23", "KHA-21"]


def test_second_preference_when_first_not_available():
    layout = LAYOUT + [seat("KHA-99", "KHA", "booked")]
    chosen = choose_seats(layout, ["KHA-99", "KHA-21"], 1)
    assert chosen[0].seat_number == "KHA-21"


def test_fallback_nearest_in_same_coach():
    chosen = choose_seats(LAYOUT, ["KHA-22"], 1)
    # nearest free to 22 in KHA coach: 23 (dist 1) before 24 (dist 2), 21 (dist 1)
    # sort by distance: 23(1), 21(1), 24(2)... stable order picks 23 first
    assert chosen[0].seat_number in ("KHA-23", "KHA-21")


def test_fallback_any_free_same_coach_when_no_number_nearby():
    layout = [seat("KHA-1", "KHA"), seat("KHB-5", "KHB"), seat("KHB-9", "KHB")]
    chosen = choose_seats(layout, ["KHA-40"], 1)
    assert chosen[0].coach == "KHA" and chosen[0].status == "available"


def test_one_seat_locked_excludes_known_statuses():
    chosen = choose_seats(LAYOUT, [], 1)
    assert chosen[0].status == "available"


def test_raises_when_not_enough_available():
    layout = [seat("KHA-1", "KHA", "booked"), seat("KHA-2", "KHA")]
    with pytest.raises(ValueError):
        choose_seats(layout, [], 2)


def test_respects_count():
    chosen = choose_seats(LAYOUT, [], 3)
    assert len(chosen) == 3
    assert len({s.id for s in chosen}) == 3


def test_auto_select_groups_in_single_coach():
    layout = [
        seat("KHA-1", "KHA"), seat("KHA-2", "KHA"), seat("KHA-3", "KHA"),
        seat("KHB-1", "KHB"), seat("KHB-2", "KHB"),
    ]
    chosen = choose_seats(layout, [], 3)
    assert {s.coach for s in chosen} == {"KHA"}


def test_auto_select_prefers_first_coach_with_enough():
    layout = [
        seat("KHA-1", "KHA"), seat("KHA-2", "KHA"),
        seat("KHB-1", "KHB"), seat("KHB-2", "KHB"), seat("KHB-3", "KHB"), seat("KHB-4", "KHB"),
        seat("KHC-1", "KHC"), seat("KHC-2", "KHC"), seat("KHC-3", "KHC"),
    ]
    chosen = choose_seats(layout, [], 3)
    assert {s.coach for s in chosen} == {"KHB"}


def test_auto_select_spreads_when_no_single_coach_has_enough():
    layout = [
        seat("KHA-1", "KHA"), seat("KHA-2", "KHA"),
        seat("KHB-1", "KHB"),
        seat("KHC-1", "KHC"),
    ]
    chosen = choose_seats(layout, [], 3)
    assert [s.coach for s in chosen] == ["KHA", "KHA", "KHB"]


def test_auto_select_ignores_coach_seat_type():
    """A coach flagged seat_type 1 with free seats is still selectable.

    The seat-layout response is already scoped to the class chosen on the
    search results, so the coach-level `seat_type` carries no booking
    restriction and must not shrink the candidate pool.
    """
    layout = [
        seat("KHA-1", "KHA"), seat("KHA-2", "KHA"),
        alt_class_seat("XTR1-1"), alt_class_seat("XTR1-2"), alt_class_seat("XTR1-3"),
    ]
    chosen = choose_seats(layout, [], 3)
    assert {s.seat_number for s in chosen} == {"XTR1-1", "XTR1-2", "XTR1-3"}


def test_auto_select_prefers_one_coach_that_fits_the_party():
    layout = [
        seat("KHA-1", "KHA"),
        alt_class_seat("XTR1-1"), alt_class_seat("XTR1-2"),
    ]
    assert {s.coach for s in choose_seats(layout, [], 2)} == {"XTR1"}


def test_explicit_preference_is_picked_from_any_coach():
    layout = [seat("KHA-1", "KHA"), alt_class_seat("XTR1-1")]
    assert choose_seats(layout, ["XTR1-1"], 1)[0].seat_number == "XTR1-1"


def test_raises_when_fewer_seats_free_than_requested():
    layout = [alt_class_seat("XTR1-1"), alt_class_seat("XTR1-2")]
    with pytest.raises(ValueError, match="only 2 seats available"):
        choose_seats(layout, [], 3)


def test_seat_locator_targets_the_wrapper_by_ticket_and_route_id():
    assert seat_locator("627508686", "59449368") == (
        "div.seat-wrapper[ticketid='627508686'][routeid='59449368'] button.btn-seat"
    )


class _Btn:
    def __init__(self, cls: str, text: str = "KA-3"):
        self._cls, self.text = cls, text

    def get_attribute(self, name: str) -> str | None:
        return self.text if name == "title" else self._cls


def test_seat_state_classifies_all_four_rendered_states():
    assert seat_state(_Btn("btn-seat seat-available")) == "available"
    assert seat_state(_Btn("btn-seat seat-available seat-in-progress")) == "in_progress"
    assert seat_state(_Btn("btn-seat seat-hidden seat-booked")) == "booked"
    # the SPA adds seat-selected only after a click registers
    assert seat_state(_Btn("btn-seat seat-selected")) == "selected"


class _Opt:
    def __init__(self, text: str, value: str):
        self.text, self._value = text, value

    def get_attribute(self, name: str) -> str:
        return self._value


def test_coach_option_value_matches_by_coach_prefix():
    options = [_Opt("KA - 15 Seat(s)", "KA"), _Opt("GA - 23 Seat(s)", "GA")]
    assert _coach_option_value(options, "GA") == "GA"
    assert _coach_option_value(options, "ga") == "GA"  # case-insensitive
    assert _coach_option_value(options, "THA") is None


def test_select_coach_raises_when_the_coach_is_not_offered():
    class _Driver:
        def find_elements(self, by, value):
            assert value == "app-seat-layout select.form-control option"
            return [_Opt("KA - 15 Seat(s)", "KA")]

        def execute_script(self, *args):
            raise AssertionError("execute_script must not run when no option matches")

    with pytest.raises(RuntimeError, match="THA' not offered"):
        select_coach(_Driver(), "THA")


def test_select_coach_sets_the_option_and_dispatches_change():
    captured = {}

    class _Select:
        pass

    el = _Select()

    class _Driver:
        def find_elements(self, by, value):
            return [_Opt("GA - 23 Seat(s)", "GA"), _Opt("KA - 15 Seat(s)", "KA")]

        def find_element(self, by, value):
            captured["el"] = value
            return el

        def execute_script(self, js, target, opt):
            captured["js"] = js
            captured["target"] = target
            captured["opt"] = opt

    select_coach(_Driver(), "GA")
    assert captured == {"el": "app-seat-layout select.form-control", "js": "change", "target": el, "opt": "GA"} or \
        (captured["opt"] == "GA" and captured["target"] is el and "change" in captured["js"])


def test_resolve_seat_element_refuses_a_booked_seat():
    class _BtnBoxed:
        def get_attribute(self, name):
            return "btn-seat seat-hidden seat-booked"

    class _Driver:
        def find_elements(self, by, value):
            return [_BtnBoxed()]

    with pytest.raises(RuntimeError, match="is booked"):
        resolve_seat_element(_Driver(), alt_class_seat("KA-1", status="booked"), "ROUTE1")


def test_resolve_seat_element_returns_none_when_seat_is_not_rendered():
    class _Driver:
        def find_elements(self, by, value):
            return []

    assert resolve_seat_element(_Driver(), alt_class_seat("KA-1"), "ROUTE1") is None


def gseat(seat_number: str, state: str = "available", ticket: str = "t-1", route: str = "r-1") -> GridSeat:
    return GridSeat(ticket_id=f"{ticket}-{seat_number}", route_id=route, seat_number=seat_number, state=state)


def test_pick_fallback_seats_prefers_adjacent_run_in_one_coach():
    grid = [
        gseat("KA-1"), gseat("KA-2"), gseat("KA-4"), gseat("KA-5"),
        gseat("GA-8"), gseat("GA-9"), gseat("GA-10"), gseat("GA-11"),
    ]
    picked = pick_fallback_seats(grid, 3)
    assert [p.seat_number for p in picked] == ["GA-8", "GA-9", "GA-10"]


def test_pick_fallback_seats_skips_booked_seats():
    grid = [
        gseat("KA-1"), gseat("KA-2", "booked"), gseat("KA-3"), gseat("KA-4"),
    ]
    picked = pick_fallback_seats(grid, 2)
    assert [p.seat_number for p in picked] == ["KA-3", "KA-4"]


def test_pick_fallback_seats_uses_lowest_free_when_no_run():
    grid = [gseat("KA-1", "booked"), gseat("KA-3"), gseat("GA-9"), gseat("GA-1")]
    picked = pick_fallback_seats(grid, 2)
    assert [p.seat_number for p in picked] == ["GA-1", "KA-3"]


def test_pick_fallback_seats_raises_when_more_requested_than_free():
    grid = [gseat("KA-1")]
    with pytest.raises(ValueError, match="only 1 seats free"):
        pick_fallback_seats(grid, 2)


def test_pick_fallback_seats_ignores_held_seats():
    grid = [gseat("KA-1"), gseat("KA-2", "in_progress"), gseat("KA-3"), gseat("KA-4")]
    picked = pick_fallback_seats(grid, 2)
    assert all(p.seat_number in {"KA-3", "KA-4"} for p in picked)


def test_grid_seats_parses_wrappers_and_in_progress_wins():
    payload = [
        {"ticketid": "627665857", "routeid": "59461782", "seat": "KA-3",
         "cls": "btn-seat seat-available"},
        {"ticketid": "627665858", "routeid": "59461782", "seat": "KA-4",
         "cls": "btn-seat seat-available seat-in-progress"},
        {"ticketid": "627665859", "routeid": "59461782", "seat": "KA-5",
         "cls": "btn-seat seat-hidden seat-booked"},
    ]

    class _Driver:
        def execute_script(self, js):
            import json
            return json.dumps(payload)

    seats = grid_seats(_Driver())
    assert [(s.seat_number, s.state) for s in seats] == [
        ("KA-3", "available"), ("KA-4", "in_progress"), ("KA-5", "booked"),
    ]
    assert seats[0].coach == "KA"
    assert seats[0].ticket_id == "627665857"
    assert seats[0].route_id == "59461782"


def test_continue_button_skips_disabled_and_returns_enabled():
    class _Btn:
        def __init__(self, cls, displayed):
            self._cls = cls
            self._displayed = displayed

        def is_displayed(self):
            return self._displayed

        def get_attribute(self, name):
            if name == "disabled":
                return None
            return self._cls

    from app.rpa import seat_picker as sp

    class _Driver:
        def find_elements(self, by, value):
            if not self._done:
                self._done = True
                return [_Btn("continue-btn disabled", True)]
            return [_Btn("continue-btn", True)]

        def execute_script(self, js, *args):
            return None

    d = _Driver()
    d._done = False
    button = sp._continue_button(d)
    assert button is not None and "disabled" not in (button.get_attribute("class") or "")


def test_modal_coaches_parses_picker_labels():
    from app.rpa.seat_picker import modal_coaches

    class _Driver:
        def execute_script(self, js):
            return ["KA - 12 Seat(s)", "THA - 0 Seat(s)", "GA - 23 Seat(s)"]

    assert modal_coaches(_Driver()) == {"KA", "THA", "GA"}


def test_verify_modal_class_true_on_overlap_and_false_without():
    from app.rpa.seat_picker import verify_modal_class

    class _Driver:
        def execute_script(self, js):
            return ["KA - 12 Seat(s)", "THA - 0 Seat(s)"]

    assert verify_modal_class(_Driver(), {"KA", "GA"})
    assert not verify_modal_class(_Driver(), {"GA", "GHA", "UMA"})


def test_resolve_preferences_prepends_coach_to_bare_numbers():
    from app.rpa.seat_picker import resolve_preferences

    assert resolve_preferences("24,25,29,28", "GA", LAYOUT) == ["GA-24", "GA-25", "GA-29", "GA-28"]


def test_resolve_preferences_auto_coach_when_none_given_and_empty_with_no_info():
    from app.rpa.seat_picker import resolve_preferences

    # KHA is the first coach that can seat the whole party of two.
    assert resolve_preferences("1,2", "", LAYOUT) == ["KHA-1", "KHA-2"]
    assert resolve_preferences("", "", LAYOUT) == []
    assert resolve_preferences(None, "", LAYOUT) == []


def test_resolve_preferences_inline_passthrough_mixed_dedupe_and_normalised():
    from app.rpa.seat_picker import resolve_preferences

    assert resolve_preferences("24, GA-10, 24", "KA", LAYOUT) == ["KA-24", "GA-10"]
    assert resolve_preferences("GA-04, 5", "GA", LAYOUT) == ["GA-4", "GA-5"]
    assert resolve_preferences(" 24 , 25 ", "ga", LAYOUT) == ["GA-24", "GA-25"]


def test_choose_seats_fills_unavailable_preference_from_same_coach():
    layout = [
        seat("GA-24", "GA"), seat("GA-25", "GA"), seat("GA-28", "GA"), seat("GA-30", "GA"),
        seat("GA-29", "GA", "booked"),
        seat("KA-1", "KA", "booked"),
    ]
    chosen = choose_seats(layout, ["GA-24", "GA-25", "GA-29", "GA-28"], 4)
    assert {s.seat_number for s in chosen} == {"GA-24", "GA-25", "GA-28", "GA-30"}
    assert {s.coach for s in chosen} == {"GA"}
    assert chosen[0].seat_number == "GA-24"
    assert chosen[1].seat_number == "GA-25"


def _ja_cha_layout():
    ja = [seat(f"JA-{n}", "JA") for n in range(1, 41)]
    cha = [seat(f"CHA-{n}", "CHA") for n in range(1, 50)]
    return ja + cha


def test_fill_rechecks_same_coach_when_preferred_seat_absent():
    # User case: SEAT_COACH=JA, SEAT_PREFERENCES=41,25,29,28. JA seats 1-40 are
    # free (41+ out of range), 25/29/28 match. The 4th must come from JA
    # (cluster-adjacent -> JA-24), NOT jump to CHA.
    chosen = choose_seats(_ja_cha_layout(), ["JA-41", "JA-25", "JA-29", "JA-28"], 4)
    assert {s.seat_number for s in chosen} == {"JA-25", "JA-29", "JA-28", "JA-24"}
    assert {s.coach for s in chosen} == {"JA"}


def test_fill_two_gaps_from_same_coach_blocks_up():
    layout = [
        seat(f"JA-{n}", "JA") if n != 28 else seat(f"JA-{n}", "JA", "booked")
        for n in range(1, 41)
    ] + [seat(f"CHA-{n}", "CHA") for n in range(1, 10)]
    chosen = choose_seats(layout, ["JA-41", "JA-25", "JA-29", "JA-28"], 4)
    assert {s.seat_number for s in chosen} == {"JA-25", "JA-29", "JA-24", "JA-23"}
    assert {s.coach for s in chosen} == {"JA"}


def test_fill_seed_anchor_when_no_preferred_seat_matches():
    layout = [seat(f"JA-{n}", "JA") for n in range(1, 41)]
    chosen = choose_seats(layout, ["JA-41"], 1)
    assert chosen[0].seat_number == "JA-40"


def test_fill_falls_back_to_first_available_coach_when_prefix_coach_absent():
    # SEAT_COACH pointing at a coach that does not exist (e.g. "SCHA"): keep the
    # old behaviour of using the first available seat's coach, without a crash.
    layout = [seat("JA-1", "JA", "booked"), seat("JA-2", "JA"), seat("CHA-5", "CHA")]
    chosen = choose_seats(layout, ["SCHA-41", "SCHA-25"], 2)
    assert {s.coach for s in chosen} == {"JA", "CHA"}
