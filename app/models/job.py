from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class RunState(str, Enum):
    SCHEDULED = "scheduled"
    WAIT_UNTIL = "wait_until"
    LOGIN = "login"
    WAIT_CF = "wait_cf"
    WAIT_OTP = "wait_otp"
    SEARCHING = "searching"
    FOUND = "found"
    LAYOUT = "layout"
    RESERVE_SEATS = "reserve_seats"
    PASSENGERS = "passengers"
    PAYMENT_GATEWAY = "payment_gateway"
    PAYMENT_PAUSE = "payment_pause"
    VERIFY_CONFIRMATION = "verify_confirmation"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TriggerMode(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"


@dataclass
class Passenger:
    name: str
    gender: str  # male | female
    age: int
    mobile: str = ""


@dataclass
class JobConfig:
    train_name: str
    from_city: str
    to_city: str
    date: str  # dd-MMM-yyyy, e.g. 30-Sep-2026
    seat_class: str = "AC_S"
    release_time: str = "08:00"
    trigger: TriggerMode = TriggerMode.AUTO
    seats: list[str] = field(default_factory=list)  # ranked preference, <= 4
    passengers: list[Passenger] = field(default_factory=list)
    email: str = ""  # booking contact email (defaults to CONTACT_EMAIL in .env)
    timeout_min: float = 10.0
    retry_delay: float = 2.0

    @property
    def count(self) -> int:
        return len(self.passengers) or len(self.seats) or 1


@dataclass
class AuthBundle:
    token: str = ""
    device_id: str = ""
    device_key: str = ""
    # Server-issued rolling nonce for seat actions. The backend returns a fresh
    # `X-Action-Token` on every response and expects it back on the next
    # reserve-seat/release-seat, so reserves must be serialised.
    action_token: str = ""
    user: dict[str, Any] | None = None
    handshake_data: dict[str, Any] | None = None
    handshake_hash: str = ""
    raw_storage: dict[str, Any] = field(default_factory=dict)


@dataclass
class SeatInfo:
    id: str
    seat_number: str
    coach: str
    status: str  # available | booked | held
    floor: str = ""
    raw: dict[str, Any] = field(default_factory=dict)  # source row (ticket_id, ...)


@dataclass
class TrainInfo:
    trip_id: str = ""
    trip_route_id: str = ""
    trip_number: str = ""
    name: str = ""
    departure: str = ""
    arrival: str = ""
    fare: str = ""
    seats_left: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class OtpGate:
    """Pauses the booking thread until the user submits an OTP via the dashboard."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._value: str | None = None
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._value = None
            self._event.clear()

    def submit(self, otp: str) -> bool:
        with self._lock:
            self._value = otp
        self._event.set()
        return True

    def wait(self, timeout: float | None = None) -> str | None:
        if not self._event.wait(timeout):
            return None
        with self._lock:
            return self._value


class CancelSignal:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled


class BookingJob:
    """Thread-safe runtime state for a single booking run."""

    def __init__(self, config: JobConfig) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.config = config
        self.state: RunState = RunState.SCHEDULED
        self._state_lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self._events_lock = threading.Lock()
        self.otp_gate = OtpGate()
        self.cancel = CancelSignal()
        self.release_event = threading.Event()
        self.error: str | None = None
        self.trains: list[TrainInfo] = []
        self.seats: list[SeatInfo] = []
        self.selected_seat_numbers: list[str] = []
        self.ticket_ids: list[int] = []
        self.auth: AuthBundle = AuthBundle()
        self.booking_ref: str | None = None
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None

    def set_state(self, state: RunState, message: str = "") -> None:
        with self._state_lock:
            self.state = state
        self.add_event(state.value, message)

    def add_event(self, kind: str, message: str = "") -> None:
        with self._events_lock:
            self.events.append(
                {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "kind": kind,
                    "message": message,
                }
            )

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            state = self.state.value
        return {
            "id": self.id,
            "state": state,
            "train_name": self.config.train_name,
            "from_city": self.config.from_city,
            "to_city": self.config.to_city,
            "date": self.config.date,
            "seat_class": self.config.seat_class,
            "release_time": self.config.release_time,
            "trigger": self.config.trigger.value,
            "selected_seat_numbers": list(self.selected_seat_numbers),
            "error": self.error,
            "booking_ref": self.booking_ref,
            "trains": [t.__dict__ for t in self.trains],
            "seats": [s.__dict__ for s in self.seats],
        }