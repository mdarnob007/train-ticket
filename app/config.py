from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    railway_user: str = ""
    railway_pass: str = ""
    contact_email: str = ""

    default_from: str = "Dhaka"
    default_to: str = "Cox's Bazar"
    default_class: str = "AC_S"
    default_train: str = ""  # DEFAULT_TRAIN in .env - target train for seat select
    default_passengers: int = Field(default=2, validation_alias="DEFAULT_NUMBER_OF_PASSENGERS")
    seat_preferences: str = ""  # SEAT_PREFERENCES - optional ranked seat numbers, e.g. "24,25,29,28"
    seat_coach: str = ""  # SEAT_COACH - optional coach code prepended to SEAT_PREFERENCES, e.g. "GA"
    journey_date: str = ""  # journey date dd-MMM-yyyy; empty = required on the form
    seat_classes: str = "AC_S,S_CHAIR,SNIGDHA,AC_B"
    default_release_time: str = "08:00"
    date_format: str = Field(default="%d-%b-%Y", validation_alias="DEFAULT_DATE_FORMAT")

    @property
    def seat_class_set(self) -> frozenset[str]:
        return frozenset({c.strip().upper() for c in self.seat_classes.split(",") if c.strip()})

    profile_dir: Path = ROOT / "profiles" / "chrome"
    data_dir: Path = ROOT / "data"

    headless: bool = False
    driver_engine: str = "uc"  # "uc" = undetected-chromedriver, "selenium" = plain, "attach" = an already-running Chrome
    debugger_address: str = "127.0.0.1:9222"  # only used when driver_engine = "attach"
    implicit_wait_secs: int = 5
    explicit_wait_secs: int = 30

    max_passengers: int = 4
    poll_interval_secs: float = 2.0
    retry_delay_secs: float = 2.0
    backoff_max_secs: float = 60.0
    timeout_min: float = 10.0

    base_url: str = "https://eticket.railway.gov.bd"
    login_path: str = "/login"
    # Results page the SPA serves directly from query params, so the bot never
    # has to drive the search form (autocomplete + datepicker) to see results.
    train_search_path: str = "/booking/train/search"
    verify_path: str = "/verify-ticket"
    api_base: str = "https://railspaapi.shohoz.com"

    def ensure_dirs(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()