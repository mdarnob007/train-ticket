"""Unified manual-OTP gate shared by login and the reservation confirm step.

The railway portal sends an SMS OTP on login (occasionally) and on every seat
confirm. Whenever an OTP field appears on the page, we pause the booking thread,
ask the user to type the code into the dashboard (`POST /runs/{id}/otp`), then
fill + submit it and resume.
"""

from __future__ import annotations

import logging
import time

from selenium.webdriver.remote.webdriver import WebDriver

from app.models.job import BookingJob, RunState
from app.rpa import driver as dv

log = logging.getLogger("rpa.otp")


def wait_otp_value(job: BookingJob, label: str = "confirm", otp_timeout: float = 120.0) -> str:
    """Block until the user submits an OTP in the dashboard; return it unsubmitted.

    Used by the API-first path (verify-otp takes the code directly, no UI fill).
    """
    job.set_state(RunState.WAIT_OTP, f"{label} OTP required - enter the SMS code in the dashboard")
    log.info("%s OTP required - waiting for dashboard submission", label)
    otp = job.otp_gate.wait(timeout=otp_timeout)
    if otp is None:
        job.set_state(RunState.FAILED, f"Timed out waiting for {label} OTP")
        raise TimeoutError(f"No {label} OTP submitted within {otp_timeout}s")
    job.otp_gate.reset()
    return otp


def run_otp_gate(
    driver: WebDriver,
    job: BookingJob,
    field_spec: str,
    submit_spec: str | None,
    label: str = "confirm",
    detect_secs: float = 8.0,
    otp_timeout: float = 120.0,
    gate=None,
) -> bool:
    """Pause for a manually-entered OTP if an OTP field appears for `detect_secs`.

    Returns True if the gate ran and was completed, False if no OTP was requested.
    Raises TimeoutError if the user never submits the code.
    `gate` defaults to `job.otp_gate` and only needs `wait(timeout)`/`reset()`.
    """
    if not field_spec:
        return False
    if not dv.field_present(driver, field_spec, timeout=detect_secs):
        return False

    gate = gate or job.otp_gate
    job.set_state(RunState.WAIT_OTP, f"{label} OTP required - enter the SMS code in the dashboard")
    log.info("%s OTP required - waiting for OTP submission", label)
    otp = gate.wait(timeout=otp_timeout)
    if otp is None:
        job.set_state(RunState.FAILED, f"Timed out waiting for {label} OTP")
        raise TimeoutError(f"No {label} OTP submitted within {otp_timeout}s")
    gate.reset()

    job.set_state(RunState.WAIT_OTP, f"{label} OTP received - submitting")
    dv.fill_split_input(driver, field_spec, otp)
    if submit_spec:
        try:
            dv.wait_visible(driver, submit_spec, timeout=10).click()
        except Exception:
            try:
                by, value = dv.locator(field_spec)
                driver.find_elements(by, value)[-1].submit()
            except Exception:
                pass
    else:
        by, value = dv.locator(field_spec)
        driver.find_elements(by, value)[-1].submit()

    # Give the page a moment to consume the OTP before the caller continues.
    time.sleep(1)
    return True