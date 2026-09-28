#!/usr/bin/env python
"""Live request-header capture: record railspaapi requests the SPA makes.

Boots the warm (logged-in) profile, installs an XHR/fetch logger that survives
navigation, then lets YOU search in the opened browser. Every railspaapi call
(URL, method, request headers, request body, response status) is written to
`data/discovery/captured_requests.json` so we can mirror the exact headers the
API needs (the 401 "Please login first" on search-trips-v2 is our target).

Usage:  ./.venv/bin/python bin/cap_request.py
Search once in the opened browser, then wait - the script dumps the captures.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

from app.config import settings  # noqa: E402
from app.models.job import BookingJob, JobConfig  # noqa: E402
from app.rpa import driver as dv  # noqa: E402
from app.rpa import session as session_mod  # noqa: E402

LOGGER_JS = r"""
(function () {
  if (window.__rpaCapInstalled) return;
  window.__rpaCapInstalled = true;
  var LS = '__rpa_cap';
  var flush = function (entry) {
    var arr = [];
    try { arr = JSON.parse(sessionStorage.getItem(LS)) || []; } catch (e) {}
    arr.push(entry);
    if (arr.length > 60) arr = arr.slice(-60);
    try { sessionStorage.setItem(LS, JSON.stringify(arr)); } catch (e) {}
  };
  var wants = function (url) {
    return typeof url === 'string' && url.indexOf('railspaapi.shohoz.com') !== -1;
  };
  var collectHeaders = function (h) {
    var out = {};
    if (!h) return out;
    if (typeof Headers !== 'undefined' && h instanceof Headers) {
      h.forEach(function (v, k) { out[k] = v; });
    } else if (Array.isArray(h)) {
      h.forEach(function (kv) { out[kv[0]] = kv[1]; });
    } else if (typeof h === 'object') {
      Object.keys(h).forEach(function (k) { out[k] = h[k]; });
    }
    return out;
  };
  var XHR = XMLHttpRequest.prototype;
  var oOpen = XHR.open, oSet = XHR.setRequestHeader, oSend = XHR.send;
  XHR.open = function (m, u) {
    var x = this;
    x.__rpa = { m: m, u: u, h: {} };
    return oOpen.apply(x, arguments);
  };
  XHR.setRequestHeader = function (k, v) {
    var x = this;
    if (x.__rpa) x.__rpa.h[k] = v;
    return oSet.apply(x, arguments);
  };
  XHR.send = function (b) {
    var x = this;
    x.addEventListener('loadend', function () {
      if (x.__rpa && wants(x.__rpa.u)) {
        flush({ kind: 'xhr', status: x.status, method: x.__rpa.m, url: x.__rpa.u,
                requestHeaders: x.__rpa.h, requestBody: typeof b === 'string' ? b : '' });
      }
    });
    return oSend.apply(x, arguments);
  };
  if (window.fetch && !window.__rpaFetchWrap) {
    var oF = window.fetch;
    window.__rpaFetchWrap = true;
    window.fetch = function (input, init) {
      var u = typeof input === 'string' ? input : (input && input.url);
      return oF.apply(this, arguments).then(function (r) {
        if (wants(u)) {
          var h = collectHeaders(init && init.headers);
          var method = (init && init.method) || 'GET';
          var body = (init && typeof init.body === 'string') ? init.body : '';
          var clone = r.clone();
          clone.text().then(function (text) {
            flush({ kind: 'fetch', status: r.status, method: method, url: u,
                    requestHeaders: h, requestBody: body, responseBody: (text || '').slice(0, 4000) });
          }).catch(function () {});
        }
        return r;
      });
    };
  }
})();
"""

SAVE_PATH = "data/discovery/captured_requests.json"


def _dump(log: logging.Logger, entries: list[dict]) -> int:
    out = Path(SAVE_PATH)
    rpa = [e for e in entries if "railspaapi.shohoz.com" in e.get("url", "")]
    log.info("captured %d railspaapi request(s)", len(rpa))
    for e in rpa:
        log.info(
            "  %s %s -> %s",
            e.get("method", "?"),
            e.get("url", "?")[:110],
            e.get("status"),
        )
        for k, v in (e.get("requestHeaders") or {}).items():
            pretty = str(v)
            log.info("      %s: %s", k, pretty[:90] + ("..." if len(pretty) > 90 else ""))
    out.write_text(json.dumps(rpa, indent=2))
    log.info("saved to %s", out)
    return 0 if rpa else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wait-secs", type=int, default=120, help="how long to keep watching after the first entry")
    args = parser.parse_args()

    log = logging.getLogger("cap_request")
    drv = dv.create_driver()
    try:
        log.info("loading %s", settings.base_url)
        drv.get(settings.base_url)
        dv.wait_for_cloudflare(drv, timeout=45)
        time.sleep(2)

        if not session_mod.is_logged_in(drv):
            log.info("profile not logged in - running automated login")
            job = BookingJob(
                JobConfig(
                    train_name="x",
                    from_city=settings.default_from,
                    to_city=settings.default_to,
                    date=settings.journey_date or "04-Oct-2026",
                )
            )
            if not session_mod.ensure_session(drv, job):
                log.error("could not establish a session - aborting")
                return 2
            drv.get(settings.base_url)
            dv.wait_for_cloudflare(drv, timeout=45)
        log.info("logged in - installing request logger")
        try:
            drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": LOGGER_JS})
        except Exception as exc:  # uc may reject the CDP call; install live only
            log.warning("CDP new-document hook failed (%s) - live-page logger only", exc)
        drv.execute_script(LOGGER_JS)
        drv.execute_script("try { sessionStorage.removeItem('__rpa_cap'); } catch (e) {}")

        log.info(">>> SEARCH NOW in the opened browser (Dhaka -> Cox's Bazar, AC_S, any date)")
        log.info(">>> any railspaapi request counts; one search is enough")

        first_seen = None
        last_count = -1
        deadline = time.time() + args.wait_secs + 60
        entries = []
        while time.time() < deadline:
            try:
                entries = drv.execute_script(
                    "try { return JSON.parse(sessionStorage.getItem('__rpa_cap')) || []; } catch (e) { return []; }"
                ) or []
            except Exception:
                entries = []
            if entries:
                if first_seen is None:
                    first_seen = time.time()
                if len(entries) != last_count:
                    last_count = len(entries)
                    log.info("... now %d request(s)", len(entries))
                if time.time() - first_seen > args.wait_secs:
                    break
            else:
                time.sleep(1)
        return _dump(log, entries)
    finally:
        drv.quit()


if __name__ == "__main__":
    sys.exit(main())