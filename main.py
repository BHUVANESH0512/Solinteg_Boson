import os
import re
import time
import smtplib
import ssl
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

load_dotenv()

# -- Global defaults (used as fallback for each site) -------------------------
DEFAULT_THRESHOLD    = float(os.getenv("SOC_THRESHOLD", "40"))
DEFAULT_REARM_MARGIN = float(os.getenv("SOC_REARM_MARGIN", "2"))
CHECK_INTERVAL       = max(10, int(os.getenv("CHECK_INTERVAL_SECONDS", "60")))
RELOAD_INTERVAL      = max(60, int(os.getenv("PAGE_RELOAD_INTERVAL_SECONDS", "300")))
MAX_FAILURES         = 3
HEADLESS             = os.getenv("HEADLESS", "false").lower() == "true"

# -- Login selectors (shared across all sites, same Solinteg account) ---------
USERNAME           = os.getenv("SOLINTEG_USERNAME", "")
PASSWORD           = os.getenv("SOLINTEG_PASSWORD", "")
USERNAME_SELECTOR  = os.getenv("LOGIN_USERNAME_SELECTOR", "").strip()
PASSWORD_SELECTOR  = os.getenv("LOGIN_PASSWORD_SELECTOR", "").strip()
LOGIN_BTN_SELECTOR = os.getenv("LOGIN_BUTTON_SELECTOR", "").strip()

# -- Email settings -----------------------------------------------------------
ALERT_TO_EMAIL_RAW = os.getenv("ALERT_TO_EMAIL", "")
ALERT_TO_EMAILS    = [e.strip() for e in ALERT_TO_EMAIL_RAW.split(",") if e.strip()]
GMAIL_FROM_EMAIL   = os.getenv("GMAIL_FROM_EMAIL", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

# -- Browser profile ----------------------------------------------------------
PROFILE_DIR = Path("solinteg_browser_profile")
PROFILE_DIR.mkdir(exist_ok=True)
Path("logs").mkdir(exist_ok=True)

# Battery SOC pattern -- matches "Battery 93%" style text
BATTERY_PATTERN = re.compile(
    r"\bBattery\b[\s:]*([0-9]{1,3}(?:\.[0-9]+)?)\s*%", re.IGNORECASE
)


# -- Background Health Server for Cloud / Fly.io on Port 8080 -----------------
LATEST_STATUS = {"status": "starting", "time": "none", "sites": {}}

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        html = f"""<html><head><title>Solinteg Battery Monitor</title></head>
        <body style='font-family:sans-serif;max-width:600px;margin:40px auto;padding:20px;background:#f8f9fa;'>
        <h2>Solinteg Battery Monitor Status</h2>
        <p><b>Status:</b> {LATEST_STATUS.get('status', 'running')}</p>
        <p><b>Last Update:</b> {LATEST_STATUS.get('time', 'waiting...')}</p>
        <h3>Monitored Sites:</h3><ul>"""
        for sname, sdata in LATEST_STATUS.get("sites", {}).items():
            html += f"<li><b>{sname}:</b> SOC {sdata.get('soc', 'N/A')}% (Updated: {sdata.get('time')})</li>"
        html += "</ul></body></html>"
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        pass # Suppress noisy HTTP access logs in console

def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        print(f"Health status server listening on port {port}", flush=True)
    except Exception as e:
        print(f"Health server note: {e}", flush=True)


# -- Load sites from .env (SITE_1_*, SITE_2_*, ...) ---------------------------
def load_sites() -> list[dict]:
    """
    Read sites from numbered env vars. Example .env:
        SITE_1_NAME=Rishi Valley 100 kW
        SITE_1_URL=https://...
        SITE_1_THRESHOLD=40
        SITE_1_REARM_MARGIN=2

    Add SITE_2_*, SITE_3_* for more sites -- no code changes needed.
    Falls back to legacy DASHBOARD_URL if no SITE_1_URL is found.
    """
    sites = []
    i = 1
    while True:
        url = os.getenv(f"SITE_{i}_URL", "").strip()
        if not url:
            break
        sites.append({
            "name":         os.getenv(f"SITE_{i}_NAME", f"Site {i}"),
            "url":          url,
            "threshold":    float(os.getenv(f"SITE_{i}_THRESHOLD",    str(DEFAULT_THRESHOLD))),
            "rearm_margin": float(os.getenv(f"SITE_{i}_REARM_MARGIN", str(DEFAULT_REARM_MARGIN))),
            "alert_active": False,
            "failures":     0,
            "last_reload":  0.0,
        })
        i += 1

    # Backward-compat: fall back to legacy single DASHBOARD_URL
    if not sites:
        legacy_url = os.getenv("DASHBOARD_URL", "").strip()
        if legacy_url:
            sites.append({
                "name":         "Dashboard",
                "url":          legacy_url,
                "threshold":    DEFAULT_THRESHOLD,
                "rearm_margin": DEFAULT_REARM_MARGIN,
                "alert_active": False,
                "failures":     0,
                "last_reload":  0.0,
            })

    return sites


# -- Logging ------------------------------------------------------------------
def log(message: str, site_name: str = "") -> None:
    prefix = f"[{site_name}] " if site_name else ""
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {prefix}{message}", flush=True)


# -- Email --------------------------------------------------------------------
def send_email_alert(soc: float, site: dict) -> None:
    """Send a plain-text + HTML email alert for a site."""
    if not GMAIL_FROM_EMAIL or not GMAIL_APP_PASSWORD:
        log("Email not configured (GMAIL_FROM_EMAIL / GMAIL_APP_PASSWORD blank). Skipping.", site["name"])
        return
    if not ALERT_TO_EMAILS:
        log("ALERT_TO_EMAIL is blank. Skipping email.", site["name"])
        return

    now_str   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    threshold = site["threshold"]
    rearm     = threshold + site["rearm_margin"]
    name      = site["name"]
    url       = site["url"]

    # ASCII-only subject -- no special dashes or arrows
    subject = f"[ALERT] Battery SOC {soc:.1f}% - Below {threshold:.0f}% | {name}"

    # Plain-text body -- ASCII only
    plain = (
        f"BATTERY LOW - IMMEDIATE ATTENTION REQUIRED\n"
        f"==========================================\n"
        f"Site        : {name}\n"
        f"Current SOC : {soc:.1f}%\n"
        f"Threshold   : {threshold:.1f}%\n"
        f"Time        : {now_str}\n"
        f"\n"
        f"Action Required:\n"
        f"  Battery SOC has dropped below the configured threshold.\n"
        f"  Please check the Solinteg dashboard and take appropriate action.\n"
        f"\n"
        f"Dashboard URL:\n"
        f"  {url}\n"
        f"\n"
        f"---\n"
        f"This alert re-arms only after SOC rises above {rearm:.1f}%\n"
        f"and drops below {threshold:.1f}% again.\n"
        f"\n"
        f"Sent by: Solinteg Battery SOC Monitor (automated)\n"
        f"Do not reply to this email.\n"
    )

    # HTML body -- uses only HTML entities, no raw Unicode special chars
    html = (
        "<!DOCTYPE html>"
        "<html>"
        "<body style='font-family:Arial,sans-serif;max-width:600px;margin:auto;background:#f5f5f5;padding:20px;'>"
        "<div style='background:#c0392b;color:white;padding:20px 30px;border-radius:8px 8px 0 0;'>"
        "<h2 style='margin:0;'>BATTERY LOW ALERT</h2>"
        f"<p style='margin:5px 0 0;opacity:0.9;'>{name}</p>"
        "</div>"
        "<div style='background:white;padding:30px;border-radius:0 0 8px 8px;border:1px solid #ddd;'>"
        "<table style='width:100%;border-collapse:collapse;'>"
        "<tr>"
        "<td style='padding:10px;color:#555;width:140px;'><b>Current SOC</b></td>"
        f"<td style='padding:10px;'><span style='font-size:2em;font-weight:bold;color:#c0392b;'>{soc:.1f}%</span></td>"
        "</tr>"
        "<tr style='background:#fef9f9;'>"
        "<td style='padding:10px;color:#555;'><b>Alert Threshold</b></td>"
        f"<td style='padding:10px;color:#333;'>{threshold:.1f}%</td>"
        "</tr>"
        "<tr>"
        "<td style='padding:10px;color:#555;'><b>Time</b></td>"
        f"<td style='padding:10px;color:#333;'>{now_str}</td>"
        "</tr>"
        "<tr style='background:#fef9f9;'>"
        "<td style='padding:10px;color:#555;'><b>Site</b></td>"
        f"<td style='padding:10px;color:#333;'>{name}</td>"
        "</tr>"
        "</table>"
        "<div style='margin:25px 0;padding:15px;background:#fff3cd;border-left:4px solid #f0ad4e;border-radius:4px;'>"
        "<b>Action Required:</b> Battery SOC has dropped below the configured threshold. "
        "Please check the Solinteg dashboard and take appropriate action."
        "</div>"
        f"<a href='{url}' style='display:inline-block;padding:12px 24px;background:#2980b9;color:white;"
        "text-decoration:none;border-radius:5px;font-weight:bold;'>Open Dashboard</a>"
        "<hr style='margin:25px 0;border:none;border-top:1px solid #eee;'>"
        "<p style='font-size:12px;color:#999;margin:0;'>"
        f"This alert re-arms after SOC recovers above {rearm:.1f}%.<br>"
        "Sent by: Solinteg Battery SOC Monitor (automated) - Do not reply."
        "</p>"
        "</div>"
        "</body>"
        "</html>"
    )

    try:
        to_header = ", ".join(ALERT_TO_EMAILS)
        msg = EmailMessage()
        msg["From"]    = GMAIL_FROM_EMAIL
        msg["To"]      = to_header
        msg["Subject"] = subject
        msg.set_content(plain, charset="utf-8")
        msg.add_alternative(html, subtype="html", charset="utf-8")

        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
            server.login(GMAIL_FROM_EMAIL, GMAIL_APP_PASSWORD)
            server.send_message(msg)

        log(f"Email alert sent to {to_header}.", site["name"])
    except Exception as e:
        log(f"Failed to send email: {e}", site["name"])


# -- Browser helpers ----------------------------------------------------------
def soc_visible(page, timeout=15000) -> bool:
    try:
        page.wait_for_function(
            "(pattern) => new RegExp(pattern, 'i').test(document.body.innerText)",
            arg=BATTERY_PATTERN.pattern,
            timeout=timeout,
        )
        return True
    except PlaywrightTimeoutError:
        return False


def try_automatic_login(page) -> bool:
    if not all([USERNAME_SELECTOR, PASSWORD_SELECTOR, LOGIN_BTN_SELECTOR]):
        log("Automatic login selectors not fully configured.")
        return False
    if not USERNAME or not PASSWORD:
        log("Username or password env variable is missing.")
        return False
    try:
        # If we are not on the login page, navigate to the login page first!
        if "/login" not in page.url and not page.locator(USERNAME_SELECTOR).is_visible():
            log(f"Navigating to login page. Current was: {page.url}")
            page.goto("https://www.solinteg-cloud.com/login", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

        # Ensure the username box is actually present before trying to click it
        if not page.locator(USERNAME_SELECTOR).is_visible():
            log(f"Login input {USERNAME_SELECTOR} not visible on page: {page.url}")
            return False

        log("Entering login credentials...")
        u_box = page.locator(USERNAME_SELECTOR)
        u_box.click()
        u_box.fill("")
        u_box.press_sequentially(USERNAME, delay=50)

        p_box = page.locator(PASSWORD_SELECTOR)
        p_box.click()
        p_box.fill("")
        p_box.press_sequentially(PASSWORD, delay=50)

        # Check terms and agreement checkbox if present and not already checked
        try:
            agreement_cb = page.locator("input[type='checkbox']").last
            if agreement_cb.is_visible() and not agreement_cb.is_checked():
                agreement_cb.check(force=True)
                log("Checked user agreement terms checkbox.")
        except Exception:
            try:
                page.locator("text=/agree|agreement/i").first.click(timeout=2000)
                log("Clicked agreement label.")
            except Exception:
                pass

        page.locator(LOGIN_BTN_SELECTOR).click(timeout=5000)
        log("Submitted login form. Waiting for authentication and redirect...")

        # Wait up to 15s for the URL to leave /login
        try:
            page.wait_for_url(lambda u: "/login" not in u, timeout=15000)
            log(f"Login successful! Redirected to: {page.url}")
            return True
        except Exception:
            current_url = page.url
            body_snippet = page.locator("body").inner_text()[:400].replace("\n", " ")
            log(f"Login did not redirect! Still at: {current_url} | Visible text: {body_snippet}")
            return False
    except Exception as e:
        log(f"Login error: {e}")
        return False


def ensure_site(page, site: dict) -> bool:
    """Navigate to a site dashboard; re-login if session expired."""
    # 1. If currently on login page or not logged in, perform login first
    if "/login" in page.url or page.locator(USERNAME_SELECTOR).is_visible():
        log("On login page. Performing login first...", site["name"])
        try_automatic_login(page)

    # 2. Navigate to target site dashboard
    page.goto(site["url"], wait_until="domcontentloaded", timeout=60000)

    # 3. If Solinteg redirected us to /login, log in and reload dashboard
    if "/login" in page.url or page.locator(USERNAME_SELECTOR).is_visible():
        log("Redirected to login. Logging in...", site["name"])
        if try_automatic_login(page):
            page.goto(site["url"], wait_until="domcontentloaded", timeout=60000)

    # 4. Wait up to 35s for live battery data to populate
    if soc_visible(page, timeout=35000):
        return True

    # 5. If data didn't appear, try one refresh with 35s wait
    log("SOC not visible yet. Reloading dashboard...", site["name"])
    page.reload(wait_until="domcontentloaded", timeout=60000)
    return soc_visible(page, timeout=35000)


def extract_battery_soc(page) -> float:
    text = page.locator("body").inner_text(timeout=15000)
    match = BATTERY_PATTERN.search(text)
    if not match:
        raise ValueError("Could not find 'Battery xx%' on the page.")
    soc = float(match.group(1))
    if not 0 <= soc <= 100:
        raise ValueError(f"SOC out of range: {soc}")
    return soc


def check_threshold(soc: float, site: dict) -> None:
    """Alert once when SOC drops below threshold; re-arm after recovery."""
    threshold = site["threshold"]
    rearm     = threshold + site["rearm_margin"]

    if soc < threshold and not site["alert_active"]:
        log(f"ALERT: SOC is {soc:.1f}%, below {threshold:.1f}%!", site["name"])
        print("\a", end="", flush=True)
        send_email_alert(soc, site)
        site["alert_active"] = True
    elif soc >= rearm and site["alert_active"]:
        log(f"Recovered to {soc:.1f}%. Alert re-armed.", site["name"])
        site["alert_active"] = False


# -- Main ---------------------------------------------------------------------
def main() -> None:
    start_health_server()
    sites = load_sites()
    if not sites:
        print("ERROR: No sites configured. Add SITE_1_URL to .env and restart.")
        return

    log(f"Monitor started | {len(sites)} site(s) | check every {CHECK_INTERVAL}s"
        f" | refresh every {RELOAD_INTERVAL}s | headless={HEADLESS}")
    for s in sites:
        log(f"  Site: {s['name']} | threshold: {s['threshold']}%")
    log(f"Alert email -> {', '.join(ALERT_TO_EMAILS)}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=HEADLESS,
            viewport={"width": 1440, "height": 900},
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-zygote",
                "--single-process",
            ] if HEADLESS else [],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()

            # Initial load: perform login directly first!
            log("Opening Solinteg to verify/perform login first...")
            page.goto("https://www.solinteg-cloud.com/login", wait_until="domcontentloaded", timeout=60000)
            if "/login" in page.url or page.locator(USERNAME_SELECTOR).is_visible():
                try_automatic_login(page)

            # Establish session and verify first site
            initial_attempts = 0
            while not ensure_site(page, sites[0]):
                initial_attempts += 1
                log(f"Initial connection to {sites[0]['name']} failed (attempt {initial_attempts}). Retrying in 15s...")
                time.sleep(15)
                if initial_attempts >= 5:
                    log("Warning: Could not read first site immediately, but continuing to main monitoring loop.")
                    break

            # Initialise reload timers
            now = time.monotonic()
            for site in sites:
                site["last_reload"] = now

            next_check = time.monotonic()

            while True:
                now = time.monotonic()

                for site in sites:
                    try:
                        # Periodic session refresh
                        if now - site["last_reload"] >= RELOAD_INTERVAL:
                            log(f"Refreshing ({RELOAD_INTERVAL}s cycle)...", site["name"])
                            if not ensure_site(page, site):
                                raise RuntimeError("Dashboard unavailable after refresh.")
                            site["last_reload"] = time.monotonic()
                        else:
                            page.goto(site["url"], wait_until="domcontentloaded", timeout=60000)
                            if not soc_visible(page, timeout=30000):
                                # If session dropped mid-loop, re-login
                                if "/login" in page.url or page.locator(USERNAME_SELECTOR).is_visible():
                                    ensure_site(page, site)
                                else:
                                    raise RuntimeError("SOC not visible after navigation.")

                        soc = extract_battery_soc(page)
                        log(f"Battery SOC: {soc:.1f}%", site["name"])
                        now_h = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        LATEST_STATUS["status"] = "monitoring"
                        LATEST_STATUS["time"] = now_h
                        LATEST_STATUS["sites"][site["name"]] = {"soc": f"{soc:.1f}", "time": now_h}
                        check_threshold(soc, site)
                        site["failures"] = 0

                    except Exception as error:
                        site["failures"] += 1
                        log(f"Error ({site['failures']}/{MAX_FAILURES}): {error}", site["name"])
                        if site["failures"] >= MAX_FAILURES:
                            log("Too many failures - re-establishing session.", site["name"])
                            try:
                                if ensure_site(page, site):
                                    site["failures"] = 0
                                    site["last_reload"] = time.monotonic()
                            except Exception as e:
                                log(f"Recovery failed: {e}", site["name"])

                # Drift-free schedule
                next_check += CHECK_INTERVAL
                time.sleep(max(0, next_check - time.monotonic()))

        except KeyboardInterrupt:
            log("Stopped by user.")
        finally:
            context.close()
if __name__ == "__main__":
    main()