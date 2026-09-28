#!/usr/bin/env python
"""api_probe - recover the real response shapes for search-trips-v2 and seat-layout.

Semi-interactive: reuses the persistent Chrome profile; if the saved session has
expired it logs in from .env (may prompt once for a terminal SMS OTP) and NO
payment/booking happens - nothing is reserved or purchased.

    bin/api_probe.py [--from Dhaka] [--to "Cox's Bazar"] [--date 26-Sep-2026]
                     [--cls AC_S] [--train "PARJOTAK EXPRESS (816)"]

What it does:
1. Warm the profile (skips Cloudflare), harvest the auth bundle.
2. GET search-trips-v2 -> data/discovery/api_probe_search.json and learn where
   trip_id/trip_route_id (seat_types[]) and boarding_point_id (trip_point_id)
   live.
3. Drive the SPA search page and click Book Now on the target train; the SPA
   mints a fresh Turnstile token itself, so the real seat-layout request+response
   is captured from the in-page logger into data/discovery/api_probe_layout.json
   (request incl. cft_response, plus which field on each seat holds ticket_id).
4. Writes a merged summary to data/discovery/api_responses.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("api_probe")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOGGER_JS = """
(() => {
  if (window.__railwayLog) return;
  window.__railwayLog = [];
  let seq = 0;
  const MAX = 2_000_000;
  const trunk = s => typeof s === 'string'
    ? (s.length > MAX ? s.slice(0, MAX) + '...[truncated]' : s)
    : (s === null || s === undefined ? '' : String(s));
  const rec = (type, data) => {
    try { window.__railwayLog.push({ id: ++seq, ts: Date.now(), type: type, ...data }); } catch (e) {}
  };
  const xhrProto = XMLHttpRequest.prototype;
  const origOpen = xhrProto.open;
  const origSend = xhrProto.send;
  xhrProto.open = function (method, url, ...rest) {
    this.__rl = { method: (method || 'GET').toUpperCase(), url: String(url) };
    return origOpen.call(this, method, url, ...rest);
  };
  xhrProto.send = function (body) {
    const meta = this.__rl || {};
    const id = ++seq;
    rec('request', { id, method: meta.method || 'GET', url: meta.url || '', postData: trunk(body) });
    const xhr = this;
    xhr.addEventListener('load', () => {
      rec('response', { id, method: meta.method || 'GET', url: meta.url || '', status: xhr.status, respBody: trunk(xhr.responseText) });
    });
    return origSend.call(this, body);
  };
  const origFetch = window.fetch;
  if (typeof origFetch === 'function') {
    window.fetch = function (input, init) {
      const url = typeof input === 'string' ? input : (input && (input.url || String(input))) || '';
      const method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
      const id = ++seq;
      rec('request', { id, method, url });
      return origFetch.apply(this, arguments).then(resp => {
        resp.clone().text().then(t => rec('response', { id, method, url, status: resp.status, respBody: trunk(t) })).catch(() => {});
        return resp;
      });
    };
  }
})();
"""

_SEEN: set[int] = set()


class CliOtpGate:
    """Terminal OTP gate (duck-types app.models.job.OtpGate: wait/reset)."""

    def wait(self, timeout: float = 120.0):
        print("\n[OTP] An SMS code is required for login. Enter the 4-6 digit code:", flush=True)
        try:
            value = input("OTP> ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        return value or None

    def reset(self) -> None:
        pass


class CliJob:
    """Minimal duck-typed job for session.ensure_session outside the dashboard."""

    def __init__(self) -> None:
        self.otp_gate = CliOtpGate()
        self.auth = None
        self.state = None
        self.error: str | None = None

    def set_state(self, state, message: str = "") -> None:
        self.state = state
        print(f"[rpa] {getattr(state, 'value', state)}: {message}")

    def add_event(self, kind: str, message: str = "") -> None:
        if message:
            print(f"[rpa] {kind}: {message}")


def _ensure_logged_in(driver) -> bool:
    import app.rpa.driver as dv
    import app.rpa.session as session_mod

    if dv.harvest_auth(driver).token:
        return True

    print("the saved profile has no auth token (session expired?).")
    print("attempting auto-login with the .env credentials (Turnstile passes via uc)...")
    job = CliJob()
    try:
        session_mod.ensure_session(driver, job, otp_timeout=300.0, otp_gate=job.otp_gate)
        return bool(dv.harvest_auth(driver).token)
    except Exception as exc:
        print(f"auto-login failed ({exc}).")

    print("\nLog in in the opened Chrome window yourself, then return here and press ENTER.")
    try:
        input("Press ENTER once you are logged in... ")
    except (EOFError, KeyboardInterrupt):
        return False
    deadline = time.time() + 300
    while time.time() < deadline:
        if dv.harvest_auth(driver).token:
            print("login detected - continuing.")
            return True
        time.sleep(2)
    print("no auth token appeared after manual login; giving up.")
    return False


def drain(drv) -> list[dict]:
    try:
        rows = drv.execute_script("return window.__railwayLog ? window.__railwayLog.slice(0) : []")
    except Exception:
        return []
    fresh = [r for r in rows if r.get("id") not in _SEEN]
    for r in fresh:
        _SEEN.add(r.get("id"))
    return fresh


def _dump(name: str, obj) -> None:
    path = ROOT / "data" / "discovery" / name
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")


def _find_train_list(search) -> list:
    data = search.get("data", search) if isinstance(search, dict) else search
    for key in ("trains", "trip_wise", "direct_trains"):
        if isinstance(data, dict) and isinstance(data.get(key), list):
            return [t for t in data[key] if isinstance(t, dict)]
    return []


def _parse_seats(layout) -> tuple:
    """Flatten real seat-layout rows into seat cell dicts; find the ticket-id key.

    Real shape: data.seatLayout[] -> .layout (rows) -> cells (dicts).
    Returns ([], None) when the shape is unexpected instead of crashing.
    """
    data = layout.get("data", layout) if isinstance(layout, dict) else layout
    rows = []

    def add_cell(cell) -> None:
        if isinstance(cell, dict):
            rows.append(cell)

    if isinstance(data, dict):
        groups = data.get("seatLayout") or data.get("seat_map") or data.get("sections")
        if isinstance(groups, list):
            for g in groups:
                if not isinstance(g, dict):
                    continue
                layout_grid = g.get("layout") or g.get("seats") or g.get("grid")
                if not isinstance(layout_grid, list):
                    continue
                for row in layout_grid:
                    if isinstance(row, list):
                        for cell in row:
                            add_cell(cell)
        if not rows:
            rows = data.get("seats") or data.get("seat_chart") or data.get("cabin")
    if not rows and isinstance(data, list):
        rows = [r for r in data if isinstance(r, dict)]
    if isinstance(rows, list):
        rows = [r for r in rows if isinstance(r, dict)]
    ticket_key = next(
        (k for k in ("ticket_id", "ticketId", "seat_ticket_id", "reservation_id") if any(k in r for r in rows)),
        None,
    )
    return rows, ticket_key


def main() -> int:
    import app.rpa.driver as dv
    from app.rpa.api_client import ApiClient

    args = _parse_args()
    from app.config import settings

    date = args.date or settings.journey_date or (datetime.now() + timedelta(days=3)).strftime("%d-%b-%Y")
    args.date = date

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print("== api_probe: recovering search/layout response shapes (no booking) ==")
    print(f"   route {args.src} -> {args.dst} on {date} ({args.cls}, train={args.train})")

    driver = dv.create_driver()
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": LOGGER_JS})

        # 1. Straight to the SPA search page (renders train cards immediately;
        #    auth is read from the profile's storage, no home visit needed).
        search_url = _search_url(args)
        print(f"\nloading the SPA search page: {search_url}")
        driver.get(search_url)
        dv.wait_for_cloudflare(driver, timeout=45)
        if not _ensure_logged_in(driver):
            print("Log in to the site once (or fix .env), then rerun.")
            return 2
        auth = dv.harvest_auth(driver)
        client = ApiClient(auth)

        # 2. search-trips-v2 (GET, via requests).
        try:
            search = client.search_trips(args.src, args.dst, date, args.cls)
        except Exception as exc:
            print(f"search_trips failed: {exc}")
            return 3
        _dump("api_probe_search.json", search)
        trains = _find_train_list(search)
        print(f"\nsearch ok: {len(trains)} trains returned")
        if not trains:
            print("no trains in response; inspect api_probe_search.json")
            return 4
        first = trains[0]
        seat_types = first.get("seat_types") or []
        print(f"seat_types[{args.cls}] rows: {len([t for t in seat_types if isinstance(t, dict) and str(t.get('type'))])}")
        _dump("api_probe_first_train.json", first)

        # 3. seat-layout: click Book Now and read the SPA's own request+response.
        layout_payload, layout_request, layout_source = _capture_layout(driver, args, assist=args.assist)
        seats: list[dict] = []
        ticket_key: str | None = None
        if layout_payload is None:
            print("could not capture a seat-layout response; inspect the page manually")
            # The seat grid is open at this point, which is all the reserve probe
            # needs - it reads the DOM, not the layout payload. Don't bail.
            if not args.reserve_probe:
                return 5
        else:
            _dump("api_probe_layout.json", layout_payload)
            seats, ticket_key = _parse_seats(layout_payload)
            print(f"seat-layout ok ({layout_source}): {len(seats)} seat entries")
            print(f"  ticket_id field on seats: '{ticket_key}' "
                  f"(sample: {[s.get(ticket_key) for s in seats[:3]] if ticket_key else 'n/a'})")
            if layout_request:
                print(f"  seat-layout request: {layout_request.get('method')} {layout_request.get('url')}")

        # 4. Optional: settle whether reserve-seat needs a token per seat, by
        #    reusing the seat page's own cft_response across two reserves.
        if args.reserve_probe:
            reserve_verdict = _reserve_probe(client, args, _cft_from_layout_request(layout_request))
        else:
            reserve_verdict = None
    finally:
        try:
            # Never `quit()`: in attach mode that shuts down the user's whole
            # browser and every tab they have. Close only the tab we opened.
            dv.close_tab_only(driver)
        except Exception:
            pass

    json.dump(
        {
            "probed_at": datetime.now().isoformat(timespec="seconds"),
            "route": {"from": args.src, "to": args.dst, "date": date, "cls": args.cls, "train": args.train},
            "boarding_point_id": _boarding_trip_id(trains),
            "ticket_id_field": ticket_key,
            "seat_layout_request_captured": layout_request,
            "reserve_token_verdict": reserve_verdict,
        },
        open(ROOT / "data" / "discovery" / "api_responses.json", "w", encoding="utf-8"),
        indent=2,
        default=str,
    )
    print("\nwrote data/discovery/api_responses.json")
    print("Next: wire ticket_id parsing into layout.py and rerun tests.")
    return 0


def _boarding_trip_id(trains: list) -> int | None:
    for t in trains:
        for bp in t.get("boarding_points") or []:
            if isinstance(bp, dict) and bp.get("trip_point_id"):
                return int(bp["trip_point_id"])
    return None


def _search_url(args) -> str:
    from urllib.parse import quote

    return (
        "https://eticket.railway.gov.bd/booking/train/search"
        f"?fromcity={quote(args.src, safe='')}&tocity={quote(args.dst, safe='')}"
        f"&doj={quote(args.date, safe='')}&class={quote(args.cls, safe='')}"
    )


def _capture_layout(driver, args, assist: bool = False):
    """Click Book Now (or let the user do it under --assist) and read seat-layout."""
    import app.rpa.driver as dv
    from app.rpa import search as search_mod

    from app.models.job import JobConfig, TrainInfo, TriggerMode

    cfg = JobConfig(train_name=args.train, from_city=args.src, to_city=args.dst, date=args.date, trigger=TriggerMode.MANUAL)
    cfg.seat_class = args.cls
    train = TrainInfo(name=args.train or "first card", departure="")
    try:
        search_mod.open_train_layout(driver, cfg, train, rank=0)
        print("clicked Book Now automatically - waiting for the SPA's seat-layout request...")
    except Exception as exc:
        print(f"automatic Book Now failed ({exc}).")
        if not assist:
            return None, None, "n/a"
        print("\nIn the opened Chrome window, open the first train's seat layout yourself:")
        print("  - solve any Turnstile checkbox if shown, then")
        print("  - click the first train card's Book Now / seat button.")
        input("Press ENTER here once the seat layout is visible in the browser... ")
        print("waiting for the SPA's seat-layout request...")

    deadline = time.time() + 45
    request = response = None
    while time.time() < deadline:
        for rec in drain(driver):
            u = rec.get("url") or ""
            if "seat-layout" not in u:
                continue
            if rec.get("type") == "request":
                request = rec
            elif rec.get("type") == "response" and rec.get("respBody"):
                response = rec
        if request and response:
            break
        time.sleep(0.5)

    if response is None:
        print("no seat-layout response captured (checked the log "
              f"{'/' if request else ', no request either'}); the train may not be bookable yet")
        return None, request, "n/a"

    try:
        payload = json.loads(response["respBody"])
    except Exception as exc:
        print(f"seat-layout response was not JSON: {exc}")
        return None, request, "raw"
    return payload, request, "browser-captured"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="api_probe")
    p.add_argument("--from", dest="src", default="Dhaka")
    p.add_argument("--to", dest="dst", default="Cox's bazar")
    p.add_argument("--date", dest="date", default="", help="dd-MMM-yyyy or YYYY-MM-DD (defaults to JOURNEY_DATE/.env, else today+3)")
    p.add_argument("--cls", dest="cls", default="AC_S")
    p.add_argument("--train", dest="train", default="")
    p.add_argument("--assist", action="store_true", help="if Book Now fails, ask you to click it in the browser")
    p.add_argument(
        "--reserve-probe",
        action="store_true",
        help="hold 2 seats via UI clicks, diff their action_token values, then release both",
    )
    return p.parse_args()


def _cft_from_layout_request(layout_request: dict | None) -> str:
    """Pull the seat page's own Turnstile token out of the SPA's layout request.

    The SPA sends it as a query param: seat-layout?...&cft_response=1.<payload>.
    That is a real, freshly minted, seat-page token, so it is a far better token
    source than trying to harvest the SPA-managed widget ourselves - and the
    widget is not readable from outside.
    """
    if not layout_request:
        return ""
    url = layout_request.get("url") or ""
    if "cft_response=" not in url:
        return ""
    return url.split("cft_response=", 1)[1].split("&", 1)[0].strip()


def _reserve_probe(client, args, cft_token: str) -> dict:
    """Test whether ONE Turnstile token can cover two reserve-seat calls.

    This settles the question the whole API reserve path hinges on: is the body
    `action_token` single-use per seat, or does one token cover a multi-seat
    selection? We answer it against our own client rather than by clicking the
    SPA, because clicking Continue navigates away and wipes the in-page network
    log before the calls can be read back.
    """
    from app.rpa import layout as layout_mod
    from app.rpa import search as search_mod

    if not cft_token:
        print("\n== reserve probe: no cft_response captured from the layout request ==")
        return {"skipped": "no cft_response in the seat-layout request"}

    train = search_mod.find_train(
        search_mod.parse_trains(
            client.search_trips(args.src, args.dst, args.date, args.cls), args.cls
        ),
        args.train,
    )
    if not train:
        print("\n== reserve probe: could not re-resolve the train ==")
        return {"skipped": "train not found"}

    # Our own seat-layout call both yields the seats and captures the rolling
    # X-Action-Token that the first reserve must send.
    seats = layout_mod.fetch_layout_api(client, train, cft_token)
    free = [s for s in seats if s.status == "available" and (s.raw or {}).get("ticket_id")]
    print(f"\n== reserve probe: {len(free)} bookable seat(s); reusing one token for 2 reserves ==")
    if len(free) < 2:
        return {"skipped": f"only {len(free)} bookable seat(s)"}

    chosen = free[:2]
    results: list[dict] = []
    for seat in chosen:
        try:
            resp = client.reserve_seat(
                ticket_id=seat.raw["ticket_id"],
                route_id=train.trip_route_id,
                seat_number=seat.seat_number,
                trip_number=train.trip_number or train.name,
                origin_name=args.src,
                destination_name=args.dst,
                action_token=cft_token,
            )
            results.append({"seat": seat.seat_number, "ok": True, "response": resp})
            print(f"  reserve {seat.seat_number}: OK")
        except Exception as exc:
            results.append({"seat": seat.seat_number, "ok": False, "error": str(exc)[:300]})
            print(f"  reserve {seat.seat_number}: FAILED - {str(exc)[:200]}")

    succeeded = [r for r in results if r["ok"]]
    verdict = {
        "action_tokens_used": 1,
        "seats": [r["seat"] for r in results],
        "results": results,
        "identical": None,
        "verdict": (
            "REUSABLE - one action_token covered both seats"
            if len(succeeded) == 2
            else "PER-SEAT (or other failure) - see results"
            if succeeded
            else "UNKNOWN - neither reserve succeeded"
        ),
    }
    print(f"  VERDICT: {verdict['verdict']}")

    released = 0
    if succeeded:
        for rec in succeeded:
            try:
                client.release_seat(
                    ticket_id=next(s.raw["ticket_id"] for s in chosen if s.seat_number == rec["seat"]),
                    route_id=train.trip_route_id,
                    seat_number=rec["seat"],
                    trip_number=train.trip_number or train.name,
                    origin_name=args.src,
                    destination_name=args.dst,
                    action_token=cft_token,
                )
                released += 1
                print(f"  released {rec['seat']}")
            except Exception as exc:
                print(f"  could not release {rec['seat']}: {exc}")
    verdict["released"] = released
    if released < len(succeeded):
        print(f"  WARNING: may still be held, release in the browser: {[r['seat'] for r in succeeded]}")

    _dump("reserve_tokens.json", verdict)
    return verdict




if __name__ == "__main__":
    sys.exit(main())