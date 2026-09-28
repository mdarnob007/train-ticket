from __future__ import annotations

import bin.start as start_mod
from app.models.job import SeatInfo, TrainInfo


def _free_seat(number: str) -> SeatInfo:
    return SeatInfo(id=f"t-{number}", seat_number=f"GA-{number}", coach="GA", status="available")


class _Clicked:
    def __init__(self, label: str) -> None:
        self.label = label

    def describe(self) -> str:
        return self.label


def _stub_pipeline(monkeypatch, calls: dict[str, int]):
    train = TrainInfo(trip_id="T", trip_route_id="R")
    monkeypatch.setattr(start_mod.dv, "create_driver", lambda: object())
    monkeypatch.setattr(start_mod.dv, "is_attached", lambda: False)
    monkeypatch.setattr(start_mod.session_mod, "ensure_session", lambda *a, **k: True)
    monkeypatch.setattr(start_mod.dv, "harvest_auth", lambda d: None)
    monkeypatch.setattr(start_mod.ApiClient, "__init__", lambda self, auth: None)
    monkeypatch.setattr(start_mod.ApiClient, "search_trips", lambda self, *a, **k: {})
    monkeypatch.setattr(start_mod.search_mod, "parse_trains", lambda data, cls: [train])
    monkeypatch.setattr(start_mod.search_mod, "find_train", lambda trains, name: train)
    monkeypatch.setattr(start_mod.search_mod, "has_seats", lambda tr: True)
    monkeypatch.setattr(start_mod.search_mod, "load_results_card_with_token", lambda d, cfg, tr: (object(), "cft"))
    monkeypatch.setattr(start_mod.search_mod, "open_seat_modal", lambda d, card, cls: None)
    monkeypatch.setattr(start_mod.layout_mod, "fetch_layout_api", lambda client, tr, cft: [_free_seat("1"), _free_seat("2")])
    monkeypatch.setattr(start_mod.reserve_mod, "choose", lambda job, layout: layout[:1])
    monkeypatch.setattr(start_mod.seat_picker_mod, "verify_modal_class", lambda d, coaches: True)
    monkeypatch.setattr(start_mod.seat_picker_mod, "select_seats_in_grid", lambda d, chosen, route: [_Clicked("GA-1")])
    monkeypatch.setattr(start_mod.seat_picker_mod, "activate_continue", lambda d: calls.__setitem__("continue", calls["continue"] + 1))
    monkeypatch.setattr(start_mod.seat_picker_mod, "wait_for_passenger_page", lambda d: calls.__setitem__("passenger", calls["passenger"] + 1) or True)
    monkeypatch.setattr(start_mod.dv, "close_tab_only", lambda d: calls.__setitem__("closed_tab", calls["closed_tab"] + 1))
    monkeypatch.setattr(start_mod.settings, "seat_preferences", "")
    monkeypatch.setattr(start_mod.settings, "seat_coach", "")
    return train


def test_select_only_clicks_seats_but_never_presses_continue(monkeypatch):
    calls = {"continue": 0, "passenger": 0, "closed_tab": 0}
    _stub_pipeline(monkeypatch, calls)
    monkeypatch.setattr(start_mod.sys, "argv", ["start.py", "--select-only", "--count", "1"])

    assert start_mod.main() == 0
    assert calls["continue"] == 0
    assert calls["passenger"] == 0
    assert calls["closed_tab"] == 0


def test_full_run_hits_continue_and_waits_for_the_passenger_page(monkeypatch):
    calls = {"continue": 0, "passenger": 0, "closed_tab": 0}
    _stub_pipeline(monkeypatch, calls)
    monkeypatch.setattr(start_mod.sys, "argv", ["start.py", "--count", "1"])

    assert start_mod.main() == 0
    assert calls["continue"] == 1
    assert calls["passenger"] == 1
    assert calls["closed_tab"] == 0