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

# â”€â”€ Global defaults (used as fallback for each site) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
DEFAULT_THRESHOLD    = float(os.getenv("SOC_THRESHOLD", "40"))
DEFAULT_REARM_MARGIN = float(os.getenv("SOC_REARM_MARGIN", "2"))
CHECK_INTERVAL       = max(10, int(os.getenv("CHECK_INTERVAL_SECONDS", "60")))
RELOAD_INTERVAL      = max(60, int(os.getenv("PAGE_RELOAD_INTERVAL_SECONDS", "300")))
MAX_FAILURES         = 3
HEADLESS             = os.getenv("HEADLESS", "false").lower() == "true"

# â”€â”€ Login selectors (shared across all sites, same Solinteg account) â”€â”€â”€â”€â”€â”€â”€â”€â”€
USERNAME          = os.getenv("SOLINTEG_USERNAME", "")
PASSWORD          = os.getenv("SOLINTEG_PASSWORD", "")
USERNAME_SELECTOR = os.getenv("LOGIN_USERNAME_SELECTOR", "").strip()
PASSWORD_SELECTOR = os.getenv("LOGIN_PASSWORD_SELECTOR", "").strip()
LOGIN_BTN_SELECTOR= os.getenv("LOGIN_BUTTON_SELECTOR", "").strip()

# â”€â”€ Email settings â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
ALERT_TO_EMAIL     = os.getenv("ALERT_TO_EMAIL", "")
GMAIL_FROM_EMAIL   = os.getenv("GMAIL_FROM_EMAIL", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

# â”€â”€ Browser profile â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
PROFILE_DIR = Path("solinteg_browser_profile")
PROFILE_DIR.mkdir(exist_ok=True)
Path("logs").mkdir(exist_ok=True)

# Battery SOC pattern â€” matches "Battery 93%" style text
BATTERY_PATTERN = re.compile(
    r"\bBattery\b[\s:]*([0-9]{1,3}(?:\.[0-9]+)?)\s*%", re.IGNORECASE
)


# â”€â”€ Load sites from .env (SITE_1_*, SITE_2_*, ...) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def load_sites() -> list[dict]:
    """
    Read sites from numbered env vars. Example .env:
        SITE_1_NAME=Rishi Valley 100 kW
        SITE_1_URL=https://...
        SITE_1_THRESHOLD=40          # optional, defaults to SOC_THRESHOLD
        SITE_1_REARM_MARGIN=2        # optional, defaults to SOC_REARM_MARGIN

    Add SITE_2_*, SITE_3_* etc. for more sites â€” no code changes needed.
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
            "alert_active": False,   # in-memory alert state per site
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


# â”€â”€ Logging â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def log(message: str, site_name: str = "") -> None:
    prefix = f"[{site_name}] " if site_name else ""
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {prefix}{message}"
    print(line, flush=True)


# â”€â”€ Email â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def send_email_alert(soc: float, site: dict) -> None:
    """Send a rich HTML + plain-text email alert for a site."""
    if not GMAIL_FROM_EMAIL or not GMAIL_APP_PASSWORD:
        log("Email not configured (GMAIL_FROM_EMAIL / GMAIL_APP_PASSWORD blank). Skipping.", site["name"])
        return
    if not ALERT_TO_EMAIL:
        log("ALERT_TO_EMAIL is blank. Skipping email.", site["name"])
        return

    now_str   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    threshold = site["threshold"]
    rearm     = site["threshold"] + site["rearm_margin"]
    name      = site["name"]
    url       = site["url"]
    subject   = f" ALERT: Battery SOC {soc:.1f}% â€” Below {threshold:.0f}% | {name}"

    plain = f"""
BATTERY LOW â€” IMMEDIATE ATTENTION REQUIRED
===========================================
Site        : {name}
Current SOC : {soc:.1f}%
Threshold   : {threshold:.1f}%
Time        : {now_str}

Action Required:
  Battery SOC has dropped below the configured threshold.
  Please check the Solinteg dashboard and take appropriate action.

Dashboard URL:
  {url}

---
This alert re-arms only after SOC rises above {rearm:.1f}%
and drops below {threshold:.1f}% again.

Sent by: Solinteg Battery SOC Monitor (automated)
Do not reply to this email.
"""

    html = f"""\
<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;max-width:600px;margin:auto;background:#f5f5f5;padding:20px;">
  <div style="background:#c0392b;color:white;padding:20px 30px;border-radius:8px 8px 0 0;">
    <h2 style="margin:0;"> Battery Low Alert</h2>
    <p style="margin:5px 0 0;opacity:0.9;">{name}</p>
  </div>
  <div style="background:white;padding:30px;border-radius:0 0 8px 8px;border:1px solid #ddd;">
    <table style="width:100%;border-collapse:collapse;">
      <tr>
        <td style="padding:10px;color:#555;width:140px;"><b>Current SOC</b></td>
        <td style="padding:10px;">
          <span style="font-size:2em;font-weight:bold;color:#c0392b;">{soc:.1f}%</span>
        </td>
      </tr>
      <tr style="background:#fef9f9;">
        <td style="padding:10px;color:#555;"><b>Alert Threshold</b></td>
        <td style="padding:10px;color:#333;">{threshold:.1f}%</td>
      </tr>
      <tr>
        <td style="padding:10px;color:#555;"><b>Time</b></td>
        <td style="padding:10px;color:#333;">{now_str}</td>
      </tr>
      <tr style="background:#fef9f9;">
        <td style="padding:10px;color:#555;"><b>Site</b></td>
        <td style="padding:10px;color:#333;">{name}</td>
      </tr>
    </table>

    <div style="margin:25px 0;padding:15px;background:#fff3cd;border-left:4px solid #f0ad4e;border-radius:4px;">
      <b>âš ï¸ Action Required:</b> Battery SOC has dropped below the configured threshold.
      Please check the Solinteg dashboard and take appropriate action.
    </div>

    <a href="{url}"
       style="display:inline-block;padding:12px 24px;background:#2980b9;color:white;
              text-decoration:none;border-radius:5px;font-weight:bold;">
      Open Dashboard â†’
    </a>

    <hr style="margin:25px 0;border:none;border-top:1px solid #eee;">
    <p style="font-size:12px;color:#999;margin:0;">
      This alert re-arms after SOC recovers above {rearm:.1f}%.<br>
      Sent by: Solinteg Battery SOC Monitor (automated) â€” Do not reply.
    </p>
  </div>
</body>
</html>"""

    try:
        msg = EmailMessage()
        msg["From"]    = GMAIL_FROM_EMAIL
        msg["To"]      = ALERT_TO_EMAIL
        msg["Subject"] = subject
        msg.set_content(plain)
        msg.add_alternative(html, subtype="html")

        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
            server.login(GMAIL_FROM_EMAIL, GMAIL_APP_PASSWORD)
            server.send_message(msg)

        log(f"âœ… Email alert sent to {ALERT_TO_EMAIL}.", site["name"])
    except Exception as e:
        log(f"âŒ Failed to send email: {e}", site["name"])


# â”€â”€ Browser helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
        return False
    if not USERNAME or not PASSWORD:
        return False
    try:
        page.locator(USERNAME_SELECTOR).fill(USERNAME, timeout=5000)
        page.locator(PASSWORD_SELECTOR).fill(PASSWORD, timeout=5000)
        page.locator(LOGIN_BTN_SELECTOR).click(timeout=5000)
        log("Submitted login form.")
        return True
    except PlaywrightTimeoutError:
        log("Login selectors not found on this page.")
        return False


def ensure_site(page, site: dict) -> bool:
    """Navigate to a site's dashboard; re-login if session expired."""
    page.goto(site["url"], wait_until="domcontentloaded", timeout=60000)
    if soc_visible(page, timeout=10000):
        return True

    log("Dashboard not visible â€” session may have expired.", site["name"])
    if try_automatic_login(page):
        page.goto(site["url"], wait_until="domcontentloaded", timeout=60000)
        if soc_visible(page, timeout=20000):
            return True

    if HEADLESS:
        log("Running headless â€” cannot prompt for manual login. Retrying later.", site["name"])
        return False

    log("Please log in manually in the browser window (waiting up to 5 min)...", site["name"])
    return soc_visible(page, timeout=300000)


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
    """Alert once when SOC drops below site threshold; re-arm after recovery."""
    threshold = site["threshold"]
    rearm     = threshold + site["rearm_margin"]

    if soc < threshold and not site["alert_active"]:
        log(f"ðŸ”´ ALERT: SOC is {soc:.1f}%, below {threshold:.1f}%!", site["name"])
        print("\a", end="", flush=True)
        send_email_alert(soc, site)
        site["alert_active"] = True
    elif soc >= rearm and site["alert_active"]:
        log(f"ðŸŸ¢ Recovered to {soc:.1f}%. Alert re-armed.", site["name"])
        site["alert_active"] = False


# â”€â”€ Main â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def main() -> None:
    sites = load_sites()
    if not sites:
        print("ERROR: No sites configured. Add SITE_1_URL to .env and restart.")
        return

    log(f"Monitor started | {len(sites)} site(s) | check every {CHECK_INTERVAL}s "
        f"| refresh every {RELOAD_INTERVAL}s | headless={HEADLESS}")
    for s in sites:
        log(f"  â€¢ {s['name']} | threshold {s['threshold']}% | url: {s['url']}")
    log(f"Alert email â†’ {ALERT_TO_EMAIL}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=HEADLESS,
            viewport={"width": 1440, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()

            # Initial login â€” navigate to the first site to establish session
            if not ensure_site(page, sites[0]):
                log("Could not open the first dashboard. Exiting.")
                return

            # Initialise reload timers
            now = time.monotonic()
            for site in sites:
                site["last_reload"] = now

            next_check = time.monotonic()

            while True:
                now = time.monotonic()

                for site in sites:
                    try:
                        # Periodic refresh
                        if now - site["last_reload"] >= RELOAD_INTERVAL:
                            log(f"Refreshing ({RELOAD_INTERVAL}s cycle)...", site["name"])
                            if not ensure_site(page, site):
                                raise RuntimeError("Dashboard unavailable after refresh.")
                            site["last_reload"] = time.monotonic()
                        else:
                            # Just navigate to this site's URL
                            page.goto(site["url"], wait_until="domcontentloaded", timeout=60000)
                            if not soc_visible(page, timeout=10000):
                                raise RuntimeError("SOC not visible after navigation.")

                        soc = extract_battery_soc(page)
                        log(f"Battery SOC: {soc:.1f}%", site["name"])
                        check_threshold(soc, site)
                        site["failures"] = 0

                    except Exception as error:
                        site["failures"] += 1
                        log(f"Error ({site['failures']}/{MAX_FAILURES}): {error}", site["name"])
                        if site["failures"] >= MAX_FAILURES:
                            log("Too many failures â€” re-establishing session.", site["name"])
                            try:
                                if ensure_site(page, site):
                                    site["failures"] = 0
                                    site["last_reload"] = time.monotonic()
                            except Exception as e:
                                log(f"Recovery failed: {e}", site["name"])

                # Drift-free schedule â€” advance by CHECK_INTERVAL regardless of elapsed time
                next_check += CHECK_INTERVAL
                time.sleep(max(0, next_check - time.monotonic()))

        except KeyboardInterrupt:
            log("Stopped by user.")
        finally:
            context.close()


if __name__ == "__main__":
    main()
