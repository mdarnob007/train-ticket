#!/usr/bin/env python
"""Diagnose why the SPA's search form never becomes valid.

Reuses the stored session (no login, so this costs no Turnstile token), opens
the search form, and prints everything needed to see why the class dropdown and
station autocomplete stay empty: console errors, the page's own network calls,
the reactive-form state, and the submit button's disabled reason.

Usage:
    ./.venv/bin/python bin/diagnose_search.py
    ./.venv/bin/python bin/diagnose_search.py --keep-open   # leave the browser up
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from selenium.webdriver.common.by import By  # noqa: E402

from app.config import settings  # noqa: E402
from app.models.job import BookingJob, JobConfig  # noqa: E402
from app.rpa import driver as dv  # noqa: E402
from app.rpa import session as session_mod  # noqa: E402

log = logging.getLogger("diagnose")

# Installed before the SPA boots so we catch the requests it makes on init.
HOOK = r"""
(function(){
  window.__netlog = [];
  const rec = (m,u,s)=>{try{window.__netlog.push({m:m,u:String(u).slice(0,150),s:s});}catch(e){}};
  const of = window.fetch;
  window.fetch = function(i, init){
    const u = (typeof i === 'string') ? i : (i && i.url);
    return of.apply(this, arguments).then(r=>{rec('fetch',u,r.status);return r;})
      .catch(e=>{rec('fetch-ERR',u,String(e));throw e;});
  };
  const xo = XMLHttpRequest.prototype.open, xs = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m,u){this.__m=m;this.__u=u;return xo.apply(this,arguments);};
  XMLHttpRequest.prototype.send = function(){
    this.addEventListener('loadend',()=>rec(this.__m,this.__u,this.status));
    return xs.apply(this,arguments);
  };
})();
"""


def hr(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def console(drv, label: str) -> None:
    hr(f"console: {label}")
    try:
        entries = drv.get_log("browser")
    except Exception as exc:  # noqa: BLE001
        print("  (no browser log available:", exc, ")")
        return
    severe = [e for e in entries if e.get("level") == "SEVERE"]
    for e in (severe or entries)[-25:]:
        print(f"  [{e.get('level')}] {(e.get('message') or '')[:300]}")
    if not entries:
        print("  (empty)")


def netlog(drv, label: str) -> None:
    hr(f"network: {label}")
    try:
        rows = drv.execute_script("return (window.__netlog||[]).filter(r=>!/google-analytics|g\\/collect/.test(r.u));")
    except Exception as exc:  # noqa: BLE001
        print("  (failed:", exc, ")")
        return
    if not rows:
        print("  NO app requests recorded (only analytics/i18n)")
    for r in rows[-30:]:
        print(f"  {r.get('m'):9s} {r.get('s')}  {r.get('u')}")


def form_state(drv, label: str) -> None:
    hr(f"form state: {label}")
    print("  url:", drv.current_url)
    print("  title:", drv.title[:70])
    rows = drv.execute_script(
        """
        const out = [];
        for (const el of document.querySelectorAll('input,select,button')) {
          if (!el.offsetWidth && !el.offsetHeight && el.type !== 'hidden') continue;
          out.push({
            tag: el.tagName, id: el.id || '', fc: el.getAttribute('formcontrolname') || '',
            type: el.type || '', value: (el.value || '').slice(0,24),
            readonly: !!el.readOnly, disabled: !!el.disabled,
            options: el.tagName === 'SELECT'
              ? Array.from(el.options).map(o => o.value).slice(0,15) : undefined
          });
        }
        return out;
        """
    )
    for r in rows:
        print("  ", json.dumps(r))


def suggestions(drv) -> None:
    hr("station autocomplete")
    for field, query in (("#dest_from", settings.default_from), ("#dest_to", settings.default_to)):
        els = drv.find_elements(By.CSS_SELECTOR, field)
        if not els:
            print(f"  {field}: ABSENT")
            continue
        el = els[0]
        if el.get_attribute("readonly"):
            print(f"  {field}: readonly - cannot type")
            continue
        try:
            el.click()
            el.clear()
            el.send_keys(query)
        except Exception as exc:  # noqa: BLE001
            print(f"  {field}: typing failed ({exc})")
            continue
        time.sleep(2.5)
        items = drv.execute_script(
            """
            const out = [];
            for (const ul of document.querySelectorAll('ul.ui-autocomplete')) {
              const r = ul.getBoundingClientRect();
              if (!r.width || !r.height) continue;
              for (const li of ul.querySelectorAll('li')) out.push((li.innerText||'').trim().slice(0,40));
            }
            return out;
            """
        )
        print(f"  {field} typed {query!r} -> {len(items)} suggestion(s): {items[:8]}")
        if items:
            drv.execute_script(
                """
                const want = arguments[0].toLowerCase();
                for (const ul of document.querySelectorAll('ul.ui-autocomplete')) {
                  const r = ul.getBoundingClientRect();
                  if (!r.width || !r.height) continue;
                  for (const li of ul.querySelectorAll('li')) {
                    if ((li.innerText||'').toLowerCase().includes(want)) { li.click(); return; }
                  }
                }
                """,
                query.lower(),
            )
            time.sleep(1.2)
            print(f"  {field} value after pick: {(drv.find_element(By.CSS_SELECTOR, field).get_attribute('value') or '')[:30]!r}")


def datepicker(drv) -> None:
    hr("datepicker")
    els = drv.find_elements(By.CSS_SELECTOR, "#doj")
    if not els:
        print("  #doj ABSENT")
        return
    el = els[0]
    print("  readonly:", el.get_attribute("readonly"), "| value:", (el.get_attribute("value") or "")[:24])
    try:
        el.click()
    except Exception:  # noqa: BLE001
        drv.execute_script("arguments[0].click();", el)
    time.sleep(1.5)
    info = drv.execute_script(
        """
        const c = document.querySelector('.ui-datepicker-calendar');
        if (!c) return null;
        const tds = Array.from(c.querySelectorAll('td[data-year]'));
        return {
          title: (document.querySelector('.ui-datepicker-title')||{}).innerText,
          cells: tds.length,
          sample: tds.slice(0,3).map(td => ({
            day: (td.innerText||'').trim(), y: td.getAttribute('data-year'), m: td.getAttribute('data-month'),
            anchors: td.querySelectorAll('a').length
          })),
        };
        """
    )
    print("  ", json.dumps(info))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-open", action="store_true", help="leave the browser open afterwards")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    job = BookingJob(
        JobConfig(
            train_name=settings.default_train,
            from_city=settings.default_from,
            to_city=settings.default_to,
            date=settings.journey_date or "06-Oct-2026",
            seat_class=settings.default_class,
            timeout_min=1.0,
        )
    )
    print("config:", {"train": settings.default_train, "date": job.config.date,
                      "class": settings.default_class, "from": settings.default_from,
                      "to": settings.default_to})

    drv = dv.create_driver()
    if dv.is_attached():
        dv.open_own_tab(drv)
    try:
        drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK})
        if not session_mod.ensure_session(drv, job):
            print("!! could not establish a session")
            return 1
        console(drv, "after ensure_session")

        drv.get(settings.base_url)
        dv.wait_for_cloudflare(drv)
        dv.wait_ready(drv)
        time.sleep(5)
        form_state(drv, "homepage")
        netlog(drv, "homepage")

        drv.get(settings.base_url + "/booking/train/search")
        dv.wait_ready(drv)
        time.sleep(6)
        netlog(drv, "results page")

        drv.execute_script("for(const e of document.querySelectorAll('.disclaimer-bottom-sheet')){e.remove();}")
        mods = drv.find_elements(By.CSS_SELECTOR, ".modify_search")
        print(f"\n.modify_search found: {len(mods)} (visible: {[m.is_displayed() for m in mods]})")
        if mods:
            drv.execute_script("arguments[0].click();", mods[0])
            time.sleep(6)

        form_state(drv, "modify-search form")
        netlog(drv, "after MODIFY SEARCH")
        console(drv, "after MODIFY SEARCH")
        suggestions(drv)
        netlog(drv, "after typing station")
        datepicker(drv)
        form_state(drv, "after filling")

        drv.save_screenshot(str(ROOT / "diagnose_search.png"))
        print(f"\nscreenshot: {ROOT / 'diagnose_search.png'}")
        return 0
    finally:
        if args.keep_open:
            print("\n--keep-open: leaving the browser up. Close it yourself.")
        elif dv.is_attached():
            # Only our tab - `quit()` would take down the user's whole browser.
            dv.close_tab_only(drv)
        else:
            try:
                drv.quit()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
