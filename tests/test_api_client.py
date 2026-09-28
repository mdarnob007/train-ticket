from __future__ import annotations

import json

import pytest

from app.models.job import AuthBundle
from app.rpa import api_client as apic


class _Resp:
    status_code = 200
    text = "{}"
    headers: dict[str, str] = {}

    @staticmethod
    def json() -> dict:
        return {"ok": True}


class _Resp429:
    status_code = 429
    text = '{"code":429}'
    headers: dict[str, str] = {}


def _make_client() -> apic.ApiClient:
    auth = AuthBundle(token="TOK", device_id="DEV1", device_key="DEVK", action_token="NONCE")
    return apic.ApiClient(auth)


def _patch_request(monkeypatch, resp_seq):
    calls = []

    def req(method, url, **kw):
        calls.append({"method": method, "url": url, **kw})
        resp = resp_seq.pop(0) if isinstance(resp_seq, list) else resp_seq
        return resp

    monkeypatch.setattr(apic.SESSION, "request", req)
    return calls


def test_auth_headers_are_formatted_from_bundle(monkeypatch):
    calls = _patch_request(monkeypatch, _Resp())
    _make_client().search_trips("Dhaka", "Chattogram", "30-Sep-2026", "AC_S")
    headers = calls[0]["headers"]
    assert headers["Authorization"] == "Bearer TOK"
    assert headers["x-device-id"] == "DEV1"
    assert headers["x-device-key"] == "DEVK"


def test_headers_omit_bearer_when_no_token():
    client = apic.ApiClient(AuthBundle(token="", device_id="DEV1", device_key="DEVK"))
    assert "Authorization" not in client._headers()


def test_sign_in_auth_builds_bundle_from_flat_response(monkeypatch):
    monkeypatch.setattr(
        apic.ApiClient,
        "_call",
        lambda self, key, body=None, max_attempts=4, **ctx: {"token": "JWT", "status": True},
    )
    auth = apic.ApiClient(AuthBundle()).sign_in_auth("01676651991", "pw", "cft", device_id="DID", device_key="DKEY")
    assert auth.token == "JWT"
    assert auth.device_id == "DID"
    assert auth.device_key == "DKEY"


def test_sign_in_auth_reads_token_from_data_wrapper(monkeypatch):
    monkeypatch.setattr(
        apic.ApiClient,
        "_call",
        lambda self, key, body=None, max_attempts=4, **ctx: {"data": {"token": "JWT2", "uudid": "NID"}},
    )
    auth = apic.ApiClient(AuthBundle()).sign_in_auth("01676651991", "pw", "cft")
    assert auth.token == "JWT2"
    assert auth.device_id == "NID"


def test_sign_in_auth_raises_when_no_token(monkeypatch):
    monkeypatch.setattr(apic.ApiClient, "_call", lambda self, key, body=None, max_attempts=4, **ctx: {"message": "bad"})
    with pytest.raises(apic.ApiError):
        apic.ApiClient(AuthBundle()).sign_in_auth("01676651991", "pw", "cft")


def test_sign_in_auth_prefers_server_device_key_and_user(monkeypatch):
    token = _jwt({"display_name": "Arnab", "email": "a@b.c", "phone_number": "01676651991", "nidn": "1234", "nidnt": "NID"})

    def _call(self, key, body=None, max_attempts=4, **ctx):
        return {
            "data": {
                "token": token,
                "device_key": "SRV-KEY",
                "user": {"nid_validated": 1, "is_email_verified": True},
            }
        }

    monkeypatch.setattr(apic.ApiClient, "_call", _call)
    auth = apic.ApiClient(AuthBundle()).sign_in_auth("01676651991", "pw", "cft", device_id="DID", device_key="RANDOM")
    assert auth.token == token
    assert auth.device_key == "SRV-KEY"
    assert auth.device_id == "DID"
    assert auth.user["display_name"] == "Arnab"
    assert auth.user["identification_number"] == "1234"
    assert auth.user["nid_validated"] == 1
    assert auth.user["is_email_verified"] is True


def test_user_merge_mirrors_spa_guard_shape():
    token = _jwt({"display_name": "Mohammad Bahar Uddin", "email": "m@x.com", "phone_number": "01676651991", "username": "01676651991", "nidn": "1920303789", "nidnt": "NID"})
    user = apic._merge_user_from_token(token, {"nid_validated": 1, "is_email_verified": 0})
    expected_keys = {
        "display_name", "email", "phone_number", "alternative_mobile_number", "username",
        "dob", "identification_number", "identification_type", "is_email_verified",
        "is_email_verification_required", "is_nid_verification_required", "nid_validated",
        "is_submitted_for_manual_verification", "passport",
    }
    assert set(user) == expected_keys
    assert user["display_name"] == "Mohammad Bahar Uddin"
    assert user["identification_number"] == "1920303789"
    assert user["identification_type"] == "NID"


def _jwt(claims: dict) -> str:
    import base64

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    return f"{b64(b'{}')}.{b64(json.dumps(claims).encode())}.{b64(b'{}')}"


def test_headers_match_spa_shape():
    client = apic.ApiClient(AuthBundle(token="TOK", device_id="DEV1", device_key="DEVK"))
    h = client._headers()
    assert h["Accept"] == "application/json"
    assert h["DNT"] == "1"
    assert '"Not_A Brand";v="8"' in h["Sec-Ch-Ua"]
    assert h["Authorization"] == "Bearer TOK"


def test_search_trips_params(monkeypatch):
    calls = _patch_request(monkeypatch, _Resp())
    _make_client().search_trips("Dhaka", "Chattogram", "30-Sep-2026", "AC_S")
    c = calls[0]
    assert c["method"] == "GET"
    assert c["url"] == "https://railspaapi.shohoz.com/v1.0/web/bookings/search-trips-v2"
    expected = {
        "from_city": "Dhaka",
        "to_city": "Chattogram",
        "date_of_journey": "30-Sep-2026",
        "seat_class": "AC_S",
    }
    assert c["params"] == expected


def test_reserve_seat_body_per_seat(monkeypatch):
    calls = _patch_request(monkeypatch, _Resp())
    _make_client().reserve_seat(
        ticket_id=12345,
        route_id=678,
        seat_number="S-5A",
        trip_number="735",
        origin_name="Dhaka",
        destination_name="Chattogram",
        action_token="1.abcd.efgh",
    )
    c = calls[0]
    assert c["method"] == "PATCH"
    assert c["url"].endswith("/bookings/reserve-seat")
    body = json.loads(c["data"])
    assert body["ticket_id"] == 12345
    assert body["route_id"] == 678
    assert body["extras"]["seat_number"] == "S-5A"
    assert body["action_token"] == "1.abcd.efgh"


def test_verify_otp_keeps_typed_list_and_otp(monkeypatch):
    calls = _patch_request(monkeypatch, _Resp())
    _make_client().verify_otp(trip_id=1, trip_route_id=2, ticket_ids=[12345, 12346], otp="6779")
    body = json.loads(calls[0]["data"])
    assert body["ticket_ids"] == [12345, 12346]
    assert body["ticket_ids"][0] == 12345
    assert body["otp"] == "6779"


def test_429_retries_with_backoff_then_succeeds(monkeypatch):
    calls = _patch_request(monkeypatch, [_Resp429(), _Resp()])
    _make_client().search_trips("Dhaka", "Chattogram", "30-Sep-2026", "AC_S")
    assert len(calls) == 2


class _RespWithNonce(_Resp):
    def __init__(self, nonce: str) -> None:
        self.headers = {"X-Action-Token": nonce}


def test_action_token_header_only_on_seat_actions(monkeypatch):
    """The SPA attaches X-Action-Token to reserve/release and nothing else."""
    calls = _patch_request(monkeypatch, _Resp())
    client = _make_client()
    client.search_trips("Dhaka", "Chattogram", "07-Oct-2026", "AC_S")
    client.seat_layout("1", "2", cft_response="cft.1")
    client.reserve_seat(1, 2, "GA-1", "704", "Dhaka", "Chattogram", action_token="1.a.b")
    client.release_seat(1, 2, "GA-1", "704", "Dhaka", "Chattogram", action_token="1.a.b")
    sent = ["X-Action-Token" in c["headers"] for c in calls]
    assert sent == [False, False, True, True]


def test_seat_layout_sends_the_turnstile_token_as_a_query_param(monkeypatch):
    """`cft_response` rides in the URL, and a real token is mandatory.

    A tokenless layout call is a hard 422 TURNSTILE_TOKEN_REQUIRED (verified
    live), so the client substitutes a placeholder rather than omitting the
    param - but that placeholder is not an acceptable value.
    """
    client = _make_client()
    calls = _patch_request(monkeypatch, _Resp())
    client.seat_layout("1", "2", cft_response="1.payload.sig")
    assert calls[0]["params"]["cft_response"] == "1.payload.sig"

    calls = _patch_request(monkeypatch, _Resp())
    client.seat_layout("1", "2")
    assert calls[0]["params"]["cft_response"] == " "


def test_action_token_is_captured_from_response_header(monkeypatch):
    _patch_request(monkeypatch, _RespWithNonce("NONCE-FROM-SERVER"))
    client = _make_client()
    client.seat_layout("1", "2", cft_response="cft.1")
    assert client.auth.action_token == "NONCE-FROM-SERVER"


def test_reserve_replays_the_nonce_returned_by_the_previous_call(monkeypatch):
    """Reserves are serial: each sends the nonce its predecessor handed back."""
    calls = _patch_request(
        monkeypatch,
        [_RespWithNonce("T1"), _RespWithNonce("T2"), _RespWithNonce("T3")],
    )
    client = _make_client()
    client.auth.action_token = ""
    client.seat_layout("1", "2", cft_response="cft.1")
    assert calls[0]["headers"].get("X-Action-Token") is None  # layout does not send one
    client.reserve_seat(1, 2, "GA-1", "704", "Dhaka", "Chattogram", action_token="1.a.b")
    assert calls[1]["headers"]["X-Action-Token"] == "T1"
    client.reserve_seat(1, 2, "GA-2", "704", "Dhaka", "Chattogram", action_token="1.c.d")
    assert calls[2]["headers"]["X-Action-Token"] == "T2"
    assert client.auth.action_token == "T3"


def test_reserve_without_captured_nonce_fails_fast(monkeypatch):
    """Never send a seat action with no nonce - the backend would reject it."""
    calls = _patch_request(monkeypatch, _Resp())
    client = apic.ApiClient(AuthBundle(token="TOK", device_id="DEV1", device_key="DEVK"))
    with pytest.raises(apic.ApiError, match="X-Action-Token"):
        client.reserve_seat(1, 2, "GA-1", "704", "Dhaka", "Chattogram", action_token="1.a.b")
    assert not calls
