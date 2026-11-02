import os
import re
import time
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

load_dotenv()

# ── Dashboard ────────────────────────────────────────────────────────────────
DASHBOARD_URL = os.getenv(
    "DASHBOARD_URL",
    "https://www.solinteg-cloud.com/power-station/detail?id=68907e47c99c9350eaad9377&name=Rishi%20Valley%20100%20kW",
)

# ── Credentials ──────────────────────────────────────────────────────────────
USERNAME = os.getenv("SOLINTEG_USERNAME", "")
PASSWORD = os.getenv("SOLINTEG_PASSWORD", "")

# ── Monitoring settings ───────────────────────────────────────────────────────
THRESHOLD      = float(os.getenv("SOC_THRESHOLD", "40"))
REARM_MARGIN   = float(os.getenv("SOC_REARM_MARGIN", "2"))   # re-arm at THRESHOLD + REARM_MARGIN
CHECK_INTERVAL = max(10,  int(os.getenv("CHECK_INTERVAL_SECONDS", "60")))
RELOAD_INTERVAL= max(60,  int(os.getenv("PAGE_RELOAD_INTERVAL_SECONDS", "300")))
MAX_FAILURES   = 3
HEADLESS       = os.getenv("HEADLESS", "false").lower() == "true"

# ── Login selectors (optional) ────────────────────────────────────────────────
USERNAME_SELECTOR    = os.getenv("LOGIN_USERNAME_SELECTOR", "").strip()
PASSWORD_SELECTOR    = os.getenv("LOGIN_PASSWORD_SELECTOR", "").strip()
LOGIN_BUTTON_SELECTOR= os.getenv("LOGIN_BUTTON_SELECTOR", "").strip()

# ── Email alert settings ──────────────────────────────────────────────────────
ALERT_TO_EMAIL    = os.getenv("ALERT_TO_EMAIL", "bhuvaneshravi1957@gmail.com")
GMAIL_FROM_EMAIL  = os.getenv("GMAIL_FROM_EMAIL", "")          # your Gmail address
GMAIL_APP_PASSWORD= os.getenv("GMAIL_APP_PASSWORD", "")        # 16-char Gmail App Password

# ── Browser profile ───────────────────────────────────────────────────────────
PROFILE_DIR = Path("solinteg_browser_profile")
PROFILE_DIR.mkdir(exist_ok=True)

# Battery SOC pattern — matches "Battery 93%" style text
BATTERY_PATTERN = re.compile(
    r"\bBattery\b[\s:]*([0-9]{1,3}(?:\.[0-9]+)?)\s*%", re.IGNORECASE
)

low_soc_alert_active = False


# ── Logging ───────────────────────────────────────────────────────────────────
def log(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


# ── Email ─────────────────────────────────────────────────────────────────────
def send_email_alert(soc: float) -> None:
    """Send a Gmail alert when SOC drops below threshold."""
    if not GMAIL_FROM_EMAIL or not GMAIL_APP_PASSWORD:
        log("Email not configured (GMAIL_FROM_EMAIL / GMAIL_APP_PASSWORD blank). Skipping email.")
        return

    subject = f"⚠️ Battery SOC Alert: {soc:.1f}% — Below {THRESHOLD:.0f}%"
    body = (
        f"Battery SOC Alert — Rishi Valley 100 kW\n"
        f"{'=' * 45}\n\n"
        f"  Current SOC : {soc:.1f}%\n"
        f"  Threshold   : {THRESHOLD:.1f}%\n"
        f"  Time        : {datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
        f"Dashboard:\n{DASHBOARD_URL}\n\n"
        f"The alert will re-arm once SOC rises above {THRESHOLD + REARM_MARGIN:.1f}%.\n"
    )

    try:
        msg = EmailMessage()
        msg["From"]    = GMAIL_FROM_EMAIL
        msg["To"]      = ALERT_TO_EMAIL
        msg["Subject"] = subject
        msg.set_content(body)

        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
            server.login(GMAIL_FROM_EMAIL, GMAIL_APP_PASSWORD)
            server.send_message(msg)

        log(f"Email alert sent to {ALERT_TO_EMAIL}.")
    except Exception as e:
        log(f"Failed to send email: {e}")


# ── Browser helpers ───────────────────────────────────────────────────────────
def soc_visible(page, timeout=15000) -> bool:
    """Return True if 'Battery xx%' text appears within the timeout."""
    try:
        page.wait_for_function(
            "(pattern) => new RegExp(pattern, 'i').test(document.body.innerText)",
            arg=BATTERY_PATTERN.pattern,   # .pattern is the correct Python attribute
            timeout=timeout,
        )
        return True
    except PlaywrightTimeoutError:
        return False


def try_automatic_login(page) -> bool:
    """Submit login form using selectors from .env. Returns True if submitted."""
    if not all([USERNAME_SELECTOR, PASSWORD_SELECTOR, LOGIN_BUTTON_SELECTOR]):
        return False
    if not USERNAME or not PASSWORD:
        return False
    try:
        page.locator(USERNAME_SELECTOR).fill(USERNAME, timeout=5000)
        page.locator(PASSWORD_SELECTOR).fill(PASSWORD, timeout=5000)
        page.locator(LOGIN_BUTTON_SELECTOR).click(timeout=5000)
        log("Submitted login form.")
        return True
    except PlaywrightTimeoutError:
        log("Login selectors not found on this page.")
        return False


def ensure_dashboard(page) -> bool:
    """Navigate to the dashboard; re-login automatically or prompt manual login."""
    page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=60000)

    if soc_visible(page, timeout=10000):
        return True

    log("Dashboard not visible — session may have expired.")
    if try_automatic_login(page):
        page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=60000)
        if soc_visible(page, timeout=20000):
            return True

    if HEADLESS:
        log("Running headless — cannot prompt for manual login. Retrying later.")
        return False

    log("Please log in manually in the browser window (waiting up to 5 min)...")
    return soc_visible(page, timeout=300000)


def extract_battery_soc(page) -> float:
    """Read visible page text and return battery SOC as a float."""
    text = page.locator("body").inner_text(timeout=15000)
    match = BATTERY_PATTERN.search(text)
    if not match:
        raise ValueError(
            "Could not find 'Battery xx%' on the page. "
            "Confirm the correct dashboard is open."
        )
    soc = float(match.group(1))
    if not 0 <= soc <= 100:
        raise ValueError(f"SOC out of range: {soc}")
    return soc


# ── Alert logic ───────────────────────────────────────────────────────────────
def check_threshold(soc: float) -> None:
    """Alert (terminal + email) once when SOC drops below threshold."""
    global low_soc_alert_active
    if soc < THRESHOLD and not low_soc_alert_active:
        log(f"🔴 ALERT: Battery SOC is {soc:.1f}%, below {THRESHOLD:.1f}%!")
        print("\a", end="", flush=True)
        send_email_alert(soc)
        low_soc_alert_active = True
    elif soc >= THRESHOLD + REARM_MARGIN and low_soc_alert_active:
        log(f"🟢 Battery recovered to {soc:.1f}%. Alert re-armed.")
        low_soc_alert_active = False


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    log(f"Monitor started | threshold {THRESHOLD}% | rearm +{REARM_MARGIN}% "
        f"| check {CHECK_INTERVAL}s | refresh {RELOAD_INTERVAL}s "
        f"| headless={HEADLESS}")
    log(f"Alert email → {ALERT_TO_EMAIL}")
    log(f"Dashboard   → {DASHBOARD_URL}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=HEADLESS,
            viewport={"width": 1440, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()

            if not ensure_dashboard(page):
                log("Could not open the dashboard. Exiting.")
                return

            last_reload = time.monotonic()
            next_check  = time.monotonic()
            failures    = 0

            while True:
                try:
                    # Periodic refresh + session check
                    if time.monotonic() - last_reload >= RELOAD_INTERVAL:
                        log(f"Refreshing dashboard ({RELOAD_INTERVAL}s cycle)...")
                        if not ensure_dashboard(page):
                            raise RuntimeError("Dashboard unavailable after refresh.")
                        last_reload = time.monotonic()

                    soc = extract_battery_soc(page)
                    log(f"Battery SOC: {soc:.1f}%")
                    check_threshold(soc)
                    failures = 0

                except Exception as error:
                    failures += 1
                    log(f"Monitoring error ({failures}/{MAX_FAILURES}): {error}")
                    if failures >= MAX_FAILURES:
                        log("Too many failures — reloading and re-checking login.")
                        try:
                            if ensure_dashboard(page):
                                failures = 0
                                last_reload = time.monotonic()
                        except Exception as e:
                            log(f"Recovery failed: {e}")

                # Drift-free schedule
                next_check += CHECK_INTERVAL
                time.sleep(max(0, next_check - time.monotonic()))

        except KeyboardInterrupt:
            log("Stopped by user.")
        finally:
            context.close()


if __name__ == "__main__":
    main()
