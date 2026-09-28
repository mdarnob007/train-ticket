from __future__ import annotations

import logging

from app.models.job import BookingJob, SeatInfo
from app.rpa.seat_picker import choose_seats

log = logging.getLogger("rpa.reserve")


def choose(job: BookingJob, layout: list[SeatInfo]) -> list[SeatInfo]:
    """Run the ranked-preference seat picker; sets job.selected. Raises if impossible."""
    cfg = job.config
    chosen = choose_seats(layout, cfg.seats, min(cfg.count, len(cfg.passengers) or cfg.count))
    job.selected_seat_numbers = [s.seat_number for s in chosen]
    job.seats = layout
    job.add_event("seats", f"selected seats: {', '.join(s.seat_number or s.id for s in chosen)}")
    return chosen