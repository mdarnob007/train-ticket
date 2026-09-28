#!/usr/bin/env python
"""Phase 2 discovery harness.

Captures every network request/response the SPA makes to the Shohoz API while a
real booking is performed manually in the browser. Output is used to fill
`app/models/apidefs.py` and `app/models/selectors.py`.

Capture mechanism: an in-page fetch/XHR logger is injected *before* navigation
(Page.addScriptToEvaluateOnNewDocument), so request headers, post bodies and
response bodies are recorded from inside the app's own JS context. This is far
more reliable than ChromeDriver's `performance` log buffer and works with the
undetected-chromedriver engine.

Usage:  ./.venv/bin/python bin/discover.py

Instructions:
  1. A Chrome window opens on the site. Log in manually (enter OTP when asked).
  2. After EACH step (login, search, seat layout, seat picker, the OTP
     confirmation page, passenger page, payment page), press ENTER in this
     terminal so we snapshot the DOM + screenshot.
  3. At the payment page STOP (do not pay). Type `q` + ENTER to finish capture.

Nothing is stored except network metadata and page snapshots under data/discovery/.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("discover")

from app.config import settings  # noqa: E402
from app.rpa import driver as dv  # noqa: E402

OUT_DIR = settings.data_dir / "discovery"
STEPS_DIR = OUT_DIR / "steps"
INTERESTING_HOSTS = ("railspaapi.shohoz.com", "eticket.railway.gov.bd", "shohoz.com")

MAX_BODY_CHARS = 50_000

# Injected before every document load. Wraps XHR + fetch, pushes a log entry with
# a monotonic id so the poller can dedupe across polls.
LOGGER_JS = """
(() => {
  if (window.__railwayLog) return;
  window.__railwayLog = [];
  let seq = 0;
  const trunk = s =>
    typeof s === 'string' && s.length > MAX
      ? s.slice(0, MAX) + '...[truncated]'
      : typeof s === 'string' ? s : (s === null || s === undefined ? '' : String(s));
  const MAX = %MAX_BODY_CHARS%;
  const rec = (type, data) => {
    try {
      window.__railwayLog.push({ id: ++seq, ts: Date.now(), type: type, ...data });
    } catch (e) {}
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
    if (!meta.url) delete meta.url;
    const xhr = this;
    const onLoad = () => {
      rec('response', { id, method: meta.method || 'GET', url: meta.url || '', status: xhr.status, respBody: trunk(xhr.responseText) });
    };
    if (xhr.addEventListener) xhr.addEventListener('load', onLoad);
    return origSend.call(this, body);
  };

  const origFetch = window.fetch;
  if (typeof origFetch === 'function') {
    window.fetch = function (input, init) {
      const url = typeof input === 'string' ? input : (input && (input.url || String(input))) || '';
      const method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
      let body = (init && (init.body || init.data)) || (input && input.body) || '';
      if (body && typeof body === 'object' && !(body instanceof FormData) && !(body instanceof Blob) && !(body instanceof URLSearchParams)) {
        try { body = JSON.stringify(body); } catch (e) {}
      }
      const id = ++seq;
      rec('request', { id, method, url, postData: trunk(body) });
      return origFetch.apply(this, arguments).then(resp => {
        resp.clone().text().then(t => rec('response', { id, method, url, status: resp.status, respBody: trunk(t) })).catch(() => {});
        return resp;
      });
    };
  }
})();
"""

_event_lock = threading.Lock()
_stop = threading.Event()
_seen_ids: set[int] = set()
_events: list[dict] = []


def _interesting(url: str) -> bool:
    return any(h in url for h in INTERESTING_HOSTS)


def poller(drv) -> None:
    """Drain window.__railwayLog (set by the injected logger) into _events."""
    while not _stop.is_set():
        try:
            rows = drv.execute_script("return window.__railwayLog ? window.__railwayLog.slice(0) : []")
        except Exception:
            time.sleep(0.3)
            continue
        fresh = [r for r in rows if r.get("id") not in _seen_ids and _interesting(r.get("url", ""))]
        for r in fresh:
            _seen_ids.add(r.get("id"))
            with _event_lock:
                _events.append(r)
        time.sleep(0.3)


def inject_logger(drv) -> None:
    drv.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": LOGGER_JS.replace("%MAX_BODY_CHARS%", str(MAX_BODY_CHARS))},
    )
    log.info("in-page fetch/XHR logger injected")


def snapshot(drv, label: str) -> None:
    STEPS_DIR.mkdir(parents=True, exist_ok=True)
    idx = len(list(STEPS_DIR.glob(f"{label}_*.html")))
    base = STEPS_DIR / f"{label}_{idx}"
    (base.with_suffix(".html")).write_text(drv.page_source or "", encoding="utf-8", errors="ignore")
    try:
        drv.save_screenshot(str(base.with_suffix(".png")))
    except Exception:
        pass
    with _event_lock:
        count = len(_events)
    log.info("snapshot %s  url=%s  (events so far: %d)", label, drv.current_url, count)


def write_output() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with _event_lock:
        events = list(_events)
    reports = {}
    for e in events:
        key = (e.get("method"), e.get("url"))
        reports.setdefault(key, 0)
        reports[key] += 1
    payload = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "unique_endpoints": [{"method": m, "url": u, "calls": c} for (m, u), c in reports.items()],
        "events": events,
    }
    (OUT_DIR / "network.json").write_text(json.dumps(payload, indent=2, default=str))
    log.info("capture written to %s (%d events)", OUT_DIR / "network.json", len(events))
    log.info("unique endpoints:")
    for e in sorted(payload["unique_endpoints"], key=lambda x: -x["calls"]):
        log.info("  %-6s %s  (x%d)", e["method"] or "", e["url"], e["calls"])


STEPS = [
    ("login_page", "Now log in in the browser (enter OTP if asked). Press ENTER once logged in."),
    ("search_results", "Perform the search (train list visible). Press ENTER."),
    ("seat_layout", "Click Book Now / choose a train so the seat layout opens. Press ENTER."),
    ("seat_selected", "Pick a seat on the seat picker. Then press ENTER (before continuing)."),
    ("otp_confirmation", "Confirm the trip -> a SEPARATE OTP page appears. Enter the SMS OTP and confirm. Press ENTER once back in the flow."),
    ("passenger_page", "Fill passenger details (do NOT confirm submission). Press ENTER."),
    ("payment_page", "Click through to the payment/gateway page, then STOP. Press ENTER."),
]


def main() -> int:
    global drv
    log.info("Chrome will open with in-page network capture. Follow the prompts.")
    drv = dv.create_driver()
    try:
        inject_logger(drv)
        poll_thread = threading.Thread(target=poller, args=(drv,), daemon=True)
        poll_thread.start()
        drv.get(settings.base_url)
        dv.wait_for_cloudflare(drv, timeout=45)
        log.info("Site loaded: %s", drv.title)
        for label, hint in STEPS:
            print("\n=== STEP: %s ===" % label)
            print(hint)
            cmd = input("press ENTER when ready (or 'q' to finish) > ").strip().lower()
            if cmd == "q":
                break
            snapshot(drv, label)
        print("\nFinished capture. Press ENTER to close and save.")
        input()
    finally:
        _stop.set()
        write_output()
        try:
            drv.quit()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())