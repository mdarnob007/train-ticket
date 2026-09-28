from __future__ import annotations

from app.config import Settings, settings


def test_seat_class_set_parses_env_list():
    assert "AC_S" in settings.seat_class_set
    assert "S_CHAIR" in settings.seat_class_set


def test_seat_class_set_honours_override(monkeypatch):
    monkeypatch.setattr(settings, "seat_classes", "AC_S, SNIGDHA")
    assert settings.seat_class_set == frozenset({"AC_S", "SNIGDHA"})


def test_env_alias_default_number_of_passengers():
    # .env uses DEFAULT_NUMBER_OF_PASSENGERS, not the field-derived DEFAULT_PASSENGERS.
    s = Settings(DEFAULT_NUMBER_OF_PASSENGERS="3")
    assert s.default_passengers == 3


def test_env_alias_default_date_format():
    s = Settings(DEFAULT_DATE_FORMAT="%d-%m-%Y")
    assert s.date_format == "%d-%m-%Y"