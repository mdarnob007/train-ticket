"""Shohoz API definitions for eticket.railway.gov.bd.

Captured verbatim from a real booking (Phase 2 discovery run, 2026-09-23).

Two distinct tokens are in play and they are NOT interchangeable:
  * `cft_response` / body `action_token` - Cloudflare Turnstile tokens
    (`1.<payload>.<sig>`), minted by the widget in the warm browser via
    `harvest_turnstile_token`. A token is MANDATORY: a tokenless seat-layout
    is a hard `422 TURNSTILE_TOKEN_REQUIRED` (verified live 2026-09-27). It is
    not however required to be *fresh* per call - a real SPA capture reuses one
    `cft_response` across two seat-layout calls - so one mint per run is enough
    and requesting a widget per seat only risks a Cloudflare rate-limit.
  * header `X-Action-Token` - a 160-hex server-issued rolling nonce
    (`device_key[0:96] + 64-hex`). The backend returns a fresh one on every
    response (first seen on seat-layout) and rejects a reused value, so seat
    actions must be sent serially. `ApiClient` captures and replays it.
"""

from __future__ import annotations

from typing import Any

# Common headers the SPA attaches to every railspaapi call.
AUTH_HEADERS: dict[str, str] = {
    "Authorization": "Bearer {token}",
    "x-device-id": "{device_id}",
    "x-device-key": "{device_key}",
    "Content-Type": "application/json",
    "Origin": "https://eticket.railway.gov.bd",
    "Referer": "https://eticket.railway.gov.bd/",
}

# Method, path, query params, body template per logical operation.
# `{name}` placeholders are formatted with caller kwargs; Auth headers are
# formatted from the AuthBundle. Body may be given per-call instead (ApiClient
# methods build dynamic bodies like confirm_booking).
APIS: dict[str, dict[str, Any]] = {
    "handshake": {
        "method": "POST",
        "path": "/v1.0/web/handshake",
        "params": None,
        "body": {"hash": "EF19EFA325123043835C7D7AFBD760DA", "eticket": True, "shohoz": False, "trainBkash": False, "trainNagad": False},
    },
    "sign_in": {
        "method": "POST",
        "path": "/v1.0/web/auth/sign-in",
        "params": None,
        "body": {"mobile_number": "{mobile_number}", "password": "{password}", "cft_response": "{cft_response}"},
    },
    "search_trips": {
        "method": "GET",
        "path": "/v1.0/web/bookings/search-trips-v2",
        "params": {
            "from_city": "{from_city}",
            "to_city": "{to_city}",
            "date_of_journey": "{date}",
            "seat_class": "{seat_class}",
        },
        "body": None,
    },
    "seat_layout": {
        "method": "GET",
        "path": "/v1.0/web/bookings/seat-layout",
        "params": {
            "trip_id": "{trip_id}",
            "trip_route_id": "{trip_route_id}",
            "cft_response": "{cft_response}",
        },
        "body": None,
    },
    "reserve_seat": {
        "method": "PATCH",
        "path": "/v1.0/web/bookings/reserve-seat",
        "params": None,
        "body": {
            "ticket_id": "{ticket_id}",
            "route_id": "{route_id}",
            "extras": {
                "seat_number": "{seat_number}",
                "trip_number": "{trip_number}",
                "origin_name": "{origin_name}",
                "destination_name": "{destination_name}",
            },
            "action_token": "{action_token}",
        },
    },
    "release_seat": {
        "method": "PATCH",
        "path": "/v1.0/web/bookings/release-seat",
        "params": None,
        "body": {
            "ticket_id": "{ticket_id}",
            "route_id": "{route_id}",
            "extras": {
                "seat_number": "{seat_number}",
                "trip_number": "{trip_number}",
                "origin_name": "{origin_name}",
                "destination_name": "{destination_name}",
            },
            "action_token": "{action_token}",
        },
    },
    "passenger_details": {
        "method": "POST",
        "path": "/v1.0/web/bookings/passenger-details",
        "params": None,
        "body": {"trip_id": "{trip_id}", "trip_route_id": "{trip_route_id}", "ticket_ids": "{ticket_ids}"},
    },
    "verify_otp": {
        "method": "POST",
        "path": "/v1.0/web/bookings/verify-otp",
        "params": None,
        "body": {"trip_id": "{trip_id}", "trip_route_id": "{trip_route_id}", "ticket_ids": "{ticket_ids}", "otp": "{otp}"},
    },
    "confirm_booking": {
        "method": "PATCH",
        "path": "/v1.0/web/bookings/confirm",
        "params": None,
        "body": "{body}",
    },
}