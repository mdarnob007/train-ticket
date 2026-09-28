from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

import requests

from app.config import settings
from app.models import apidefs
from app.models.job import AuthBundle

log = logging.getLogger("rpa.api")

SESSION = requests.Session()


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail or {}


class DiscoveryRequired(ApiError):
    """Raised when a call needs an endpoint captured in Phase 2."""


def _format_params(spec: dict[str, Any], ctx: dict[str, Any]) -> dict[str, str] | None:
    params = spec.get("params")
    if not params:
        return None
    return {k: str(v).format(**ctx) for k, v in params.items() if v}


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Best-effort decode of an IDP JWT payload (no signature verification).

    The SPA's `getUserInfo` builds the stored `user` object from these claims
    (display_name, phone_number, email, username, nidn/nidnt...) and only then
    overrides the verification flags with `data.user` from the sign-in response.
    Our stored `user` must mirror that merged shape or the header stays logged
    out even though the token is accepted by the bookings API.
    """
    try:
        payload = token.split(".")[1]
        payload = "=".join([payload, "==="])[: len(payload) + (4 - len(payload) % 4) % 4]
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _merge_user_from_token(token: str, resp_user: dict[str, Any]) -> dict[str, Any]:
    """Build the `user` object the SPA stores after login.

    Mirrors the bundle: user = { claims from the JWT (display_name, email,
    phone_number, nidn/nidnt -> identification_*) } with the verification
    flags taken from the response's `data.user`.
    """
    claims = decode_jwt_claims(token)
    return {
        "display_name": claims.get("display_name"),
        "email": claims.get("email"),
        "phone_number": claims.get("phone_number"),
        "alternative_mobile_number": claims.get("alternative_mobile_number"),
        "username": claims.get("username"),
        "dob": claims.get("dob"),
        "identification_number": claims.get("nidn"),
        "identification_type": claims.get("nidnt"),
        "is_email_verified": resp_user.get("is_email_verified", 0),
        "is_email_verification_required": resp_user.get("is_email_verification_required", False),
        "is_nid_verification_required": resp_user.get("is_nid_verification_required", False),
        "nid_validated": resp_user.get("nid_validated", 0),
        "is_submitted_for_manual_verification": resp_user.get("is_submitted_for_manual_verification", False),
        "passport": resp_user.get("passport"),
    }


class ApiClient:
    """Client for railspaapi.shohoz.com using the captured Phase 2 contracts."""

    # Endpoints that must carry the rolling X-Action-Token. The SPA attaches it
    # to these two and nothing else (main.js: SEAT_RESERVE / SEAT_RELEASE).
    ACTION_TOKEN_ENDPOINTS = frozenset({"reserve_seat", "release_seat"})

    def __init__(self, auth: AuthBundle, base: str | None = None, timeout: float = 15.0) -> None:
        self.auth = auth
        self.base = (base or settings.api_base).rstrip("/")
        self.timeout = timeout

    def _headers(self, key: str = "") -> dict[str, str]:
        h = {k: v.format(**self.auth.__dict__) for k, v in apidefs.AUTH_HEADERS.items()}
        h = {k: v for k, v in h.items() if v and "{" not in v}
        # The real sign-in request sends no Authorization header (there is no
        # token yet); sending "Bearer " breaks it.
        if not self.auth.token:
            h.pop("Authorization", None)
        h.update(
            {
                # Browser-like fingerprint: railspaapi 429s non-browser UAs and
                # expects the SPA's exact header shape (captured 2026-09-24).
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 "
                    "Safari/537.36"
                ),
                "Accept": "application/json",
                "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
                "Sec-Ch-Ua": '"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"macOS"',
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "cross-site",
                "DNT": "1",
                "Priority": "u=1, i",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        if key in self.ACTION_TOKEN_ENDPOINTS and self.auth.action_token:
            h["X-Action-Token"] = self.auth.action_token
        return h

    def _call(self, key: str, body: dict[str, Any] | None = None, max_attempts: int = 4, **ctx) -> dict[str, Any]:
        spec = apidefs.APIS[key]
        method = spec.get("method")
        path = spec.get("path")
        if not method or not path:
            raise DiscoveryRequired(
                f"Endpoint '{key}' not captured yet - run 'bin/discover.py' and fill app/models/apidefs.py"
            )
        url = self.base + path.format(**ctx) if "{" in path else self.base + path
        params = _format_params(spec, ctx)
        payload = body if body is not None else spec.get("body")

        attempt = 0
        while True:
            resp = SESSION.request(
                method,
                url,
                params=params,
                headers=self._headers(key),
                data=None if payload is None else json_dumps(payload),
                timeout=self.timeout,
            )
            if resp.status_code == 429:
                attempt += 1
                if attempt > max_attempts:
                    raise ApiError(f"{key} rate-limited after {max_attempts} attempts", status=429)
                delay = min(settings.backoff_max_secs, settings.retry_delay_secs * (2 ** (attempt - 1)))
                log.warning("rate limited (429) on %s - backing off %.1fs", key, delay)
                time.sleep(delay)
                continue
            if resp.status_code != 200:
                raise ApiError(f"{key} failed: {resp.status_code} {resp.text[:200]}", status=resp.status_code)
            self._capture_action_token(resp)
            try:
                return resp.json()
            except Exception as exc:
                raise ApiError(f"{key} returned non-JSON: {resp.text[:200]}") from exc

    def _require_action_token(self) -> None:
        if not self.auth.action_token:
            raise ApiError(
                "no X-Action-Token captured yet - the server issues it on the seat-layout "
                "response, so fetch the layout before reserving"
            )

    def _capture_action_token(self, resp) -> None:
        """Keep the newest server-issued X-Action-Token for the next seat action.

        The backend hands out a fresh rolling nonce on every response (first seen
        on seat-layout) and rejects a reused one, so seat actions must run in
        series, each sending the nonce its predecessor returned.
        """
        token = resp.headers.get("X-Action-Token")
        if token and token != self.auth.action_token:
            log.debug("captured X-Action-Token (%d chars)", len(token))
            self.auth.action_token = token

    # ------------------------------------------------------------------ auth
    def sign_in(self, mobile_number: str, password: str, cft_response: str) -> dict[str, Any]:
        return self._call(
            "sign_in", body={"mobile_number": mobile_number, "password": password, "cft_response": cft_response}
        )

    def sign_in_auth(
        self,
        mobile_number: str,
        password: str,
        cft_response: str,
        device_id: str = "",
        device_key: str = "",
    ) -> AuthBundle:
        """POST the captured sign-in contract and build a ready AuthBundle.

        The device id header is a client fingerprint established before auth, but
        the device key (`ssdk`) is SERVER-ISSUED: the SPA stores it from
        `data.device_key` in the sign-in response. So we always prefer the
        server-returned key over any key we generated. The token is read
        defensively - top-level `token`, `data.token`, or `access_token`.
        """
        self.auth = AuthBundle(token="", device_id=device_id, device_key=device_key)
        payload = self.sign_in(mobile_number, password, cft_response)
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        token = (
            payload.get("token")
            or data.get("token")
            or payload.get("access_token")
            or data.get("access_token")
            or payload.get("id_token")
        )
        if not token:
            raise ApiError(f"sign-in response had no token: {str(payload)[:300]}", detail=payload)
        resp_user = (data.get("user") or {}) if isinstance(data.get("user"), dict) else {}
        user = _merge_user_from_token(token, resp_user)
        return AuthBundle(
            token=token,
            device_id=device_id or data.get("uudid") or data.get("x-device-id") or "",
            device_key=data.get("device_key") or data.get("ssdk") or device_key,
            user=user,
            raw_storage=payload,
        )

    def handshake(self, handshake_hash: str = "") -> dict[str, Any]:
        body = {
            "hash": handshake_hash,
            "eticket": True,
            "shohoz": False,
            "trainBkash": False,
            "trainNagad": False,
        }
        return self._call("handshake", body=body)

    # --------------------------------------------------------------- search
    def search_trips(self, from_city: str, to_city: str, date: str, seat_class: str) -> dict[str, Any]:
        return self._call("search_trips", from_city=from_city, to_city=to_city, date=date, seat_class=seat_class)

    def seat_layout(self, trip_id: str, trip_route_id: str, cft_response: str = "") -> dict[str, Any]:
        return self._call(
            "seat_layout",
            trip_id=trip_id,
            trip_route_id=trip_route_id,
            cft_response=cft_response or " ",
        )

    # -------------------------------------------------------------- booking
    def reserve_seat(
        self,
        ticket_id: int | str,
        route_id: int | str,
        seat_number: str,
        trip_number: str,
        origin_name: str,
        destination_name: str,
        action_token: str,
    ) -> dict[str, Any]:
        self._require_action_token()
        body = {
            "ticket_id": ticket_id,
            "route_id": route_id,
            "extras": {
                "seat_number": seat_number,
                "trip_number": trip_number,
                "origin_name": origin_name,
                "destination_name": destination_name,
            },
            "action_token": action_token,
        }
        return self._call("reserve_seat", body=body)

    def release_seat(
        self,
        ticket_id: int | str,
        route_id: int | str,
        seat_number: str,
        trip_number: str,
        origin_name: str,
        destination_name: str,
        action_token: str,
    ) -> dict[str, Any]:
        self._require_action_token()
        body = {
            "ticket_id": ticket_id,
            "route_id": route_id,
            "extras": {
                "seat_number": seat_number,
                "trip_number": trip_number,
                "origin_name": origin_name,
                "destination_name": destination_name,
            },
            "action_token": action_token,
        }
        return self._call("release_seat", body=body)

    def passenger_details(self, trip_id: int | str, trip_route_id: int | str, ticket_ids: list) -> dict[str, Any]:
        return self._call(
            "passenger_details",
            body={"trip_id": trip_id, "trip_route_id": trip_route_id, "ticket_ids": ticket_ids},
        )

    def verify_otp(self, trip_id: int | str, trip_route_id: int | str, ticket_ids: list, otp: str) -> dict[str, Any]:
        return self._call(
            "verify_otp",
            body={"trip_id": trip_id, "trip_route_id": trip_route_id, "ticket_ids": ticket_ids, "otp": otp},
        )

    def confirm_booking(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("confirm_booking", body=payload)


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj)