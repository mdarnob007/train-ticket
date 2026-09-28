"""UI locators for eticket.railway.gov.bd.

Captured from the live rendered DOM (login: 2026-09-24 via bin/discover_login.py;
search/seats: Phase 3 discovery run 2026-09-23). Normal operation reuses the
persistent logged-in session and only touches the login form when it expires.

Locator prefixes:
  "id#", "css.", "xpath//", "name=", "tag="
"""

# --- Login (captured from live DOM 2026-09-24 via bin/discover_login.py) ---
LOGIN_AUTH_FORM = "xpath//app-login"
LOGIN_MOBILE_INPUT = "xpath//input[@formcontrolname='mobile_number']"
LOGIN_PASSWORD_INPUT = "xpath//input[@formcontrolname='password']"
LOGIN_SUBMIT_BUTTON = "css.login-form-submit-btn"
# No login-OTP: the railway login never asks for a SMS code on this account.

# --- Seat-confirm OTP (separate modal, every booking, 4 split digit boxes) ---
CONFIRM_OTP_INPUT = "css.confirm-ticket-otp .otp-input-div input"
CONFIRM_OTP_SUBMIT_BUTTON = "css.confirm-ticket-otp .next-button"

# --- Search results (Angular train cards, seat layout = modal in same tab) ---
SEARCH_TRAIN_CARD = "css.trip-collapsible"
SEARCH_TRAIN_NAME = "css.trip-left-info"
SEARCH_BOOK_NOW_BUTTON = "cssbutton.book-now-btn"

# --- Seat layout modal (opens on the search page, no new tab) ---
SEAT_GRID_CONTAINER = "css.seat-layout-view"
SEAT_AVAILABLE = "cssbutton.btn-seat.seat-available"
SEAT_BOOKED = "cssbutton.btn-seat.seat-booked"
SEAT_HOLD = "cssbutton.btn-seat.seat-in-progress"
SEAT_COACH_PICKER = "cssapp-seat-layout select.form-control"
SEAT_CONTINUE_BUTTON = "css#confirmbooking .continue-btn"

# --- Passenger form (trip-info page) ---
PASSENGER_NAME_INPUT = "id#pname0"
PASSENGER_AGE_INPUT = None
PASSENGER_GENDER_SELECT = None
PASSENGER_MOBILE_INPUT = "id#pmobile"
PASSENGER_EMAIL_INPUT = "id#pemail"
PASSENGER_ADD_BUTTON = None
PASSENGER_SUBMIT_BUTTON = "id#confirm_button"

# --- Payment ---
PAYMENT_GATEWAY_LOCATOR = "id#bKash_button"
PAYMENT_CONFIRMATION_SELECTOR = None  # refine on the first real completed booking

# Session validity: the logged-in header shows the user dropdown + name
# (captured 2026-09-24 from login_after_login.html); anonymous headers show
# only a "Login | Register" link.
LOGGED_IN_INDICATOR = "css.railway-logged-user"


class Selectors:
    """Plain attribute container so modules read `sel.LOGIN_MOBILE_INPUT`."""

    LOGIN_AUTH_FORM = LOGIN_AUTH_FORM
    LOGIN_MOBILE_INPUT = LOGIN_MOBILE_INPUT
    LOGIN_PASSWORD_INPUT = LOGIN_PASSWORD_INPUT
    LOGIN_SUBMIT_BUTTON = LOGIN_SUBMIT_BUTTON
    CONFIRM_OTP_INPUT = CONFIRM_OTP_INPUT
    CONFIRM_OTP_SUBMIT_BUTTON = CONFIRM_OTP_SUBMIT_BUTTON
    SEARCH_TRAIN_CARD = SEARCH_TRAIN_CARD
    SEARCH_TRAIN_NAME = SEARCH_TRAIN_NAME
    SEARCH_BOOK_NOW_BUTTON = SEARCH_BOOK_NOW_BUTTON
    SEAT_GRID_CONTAINER = SEAT_GRID_CONTAINER
    SEAT_AVAILABLE = SEAT_AVAILABLE
    SEAT_BOOKED = SEAT_BOOKED
    SEAT_HOLD = SEAT_HOLD
    SEAT_COACH_PICKER = SEAT_COACH_PICKER
    SEAT_CONTINUE_BUTTON = SEAT_CONTINUE_BUTTON
    PASSENGER_NAME_INPUT = PASSENGER_NAME_INPUT
    PASSENGER_AGE_INPUT = PASSENGER_AGE_INPUT
    PASSENGER_GENDER_SELECT = PASSENGER_GENDER_SELECT
    PASSENGER_MOBILE_INPUT = PASSENGER_MOBILE_INPUT
    PASSENGER_EMAIL_INPUT = PASSENGER_EMAIL_INPUT
    PASSENGER_ADD_BUTTON = PASSENGER_ADD_BUTTON
    PASSENGER_SUBMIT_BUTTON = PASSENGER_SUBMIT_BUTTON
    PAYMENT_GATEWAY_LOCATOR = PAYMENT_GATEWAY_LOCATOR
    PAYMENT_CONFIRMATION_SELECTOR = PAYMENT_CONFIRMATION_SELECTOR
    LOGGED_IN_INDICATOR = LOGGED_IN_INDICATOR


sel = Selectors()
