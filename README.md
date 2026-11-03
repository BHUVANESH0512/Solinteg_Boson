# Solinteg Battery SOC Monitor

A small Python tool that watches the battery State of Charge (SOC) of the **Rishi Valley 100 kW** station on Solinteg Cloud and sends an alert when it drops below a set limit (default **40%**).

## How It Works

```
Open Solinteg Cloud (Chromium)  →  Log in (saved session)
            ↓
   Read "Battery xx%" from the dashboard
            ↓
 Every 60s:  check SOC
 Every 300s: refresh page + verify login
            ↓
 SOC < 40%  →  terminal beep + Gmail alert (sent once)
 SOC ≥ 42%  →  alert re-armed
```

## Features

- Checks battery SOC every 60 seconds
- Refreshes the dashboard and re-checks login every 300 seconds
- Sends the alert **once**, with no repeats every minute
- Re-arm margin (hysteresis) to avoid alert spam around 40%
- Email alert through Gmail
- Auto-recovery after repeated errors
- Persistent browser profile, so you don't log in every time
- Runs locally or in Docker (24/7)

## Tech Used

| Tool | Purpose |
|---|---|
| Python 3.11+ | Main language |
| Playwright | Controls Chromium to read the dashboard |
| python-dotenv | Loads settings and secrets from `.env` |
| Docker / Compose | Optional 24/7 deployment |

## Project Structure

```
.
├── main.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env                 # your secrets (never commit this)
├── .dockerignore
├── logs/
└── solinteg_browser_profile/   # saved login session (auto-created)
```

## Setup

### 1. Clone and install

```bash
git clone <your-repo-url>
cd <your-repo-folder>

python -m venv venv
# Windows:
venv\Scripts\activate
# Linux / macOS:
source venv/bin/activate

pip install -r requirements.txt
python -m playwright install chromium
```

`requirements.txt`:
```
playwright
python-dotenv
```

### 2. Create the `.env` file

```env
# Login
SOLINTEG_USERNAME=your_username
SOLINTEG_PASSWORD=your_password

# Optional auto-login selectors (inspect the login page to get them)
LOGIN_USERNAME_SELECTOR=
LOGIN_PASSWORD_SELECTOR=
LOGIN_BUTTON_SELECTOR=

# Monitoring
SOC_THRESHOLD=40
SOC_REARM_MARGIN=2
CHECK_INTERVAL_SECONDS=60
PAGE_RELOAD_INTERVAL_SECONDS=300
HEADLESS=false

# Email alerts (Gmail)
GMAIL_FROM_EMAIL=sender@gmail.com
GMAIL_APP_PASSWORD=your_16_char_app_password
ALERT_TO_EMAIL=receiver@gmail.com
```

### 3. Gmail App Password

1. Turn on **2-Step Verification** in your Google Account.
2. Go to **Google Account → Security → App passwords**.
3. Create one and paste the 16-character code into `GMAIL_APP_PASSWORD`.

> Use the App Password, not your normal Gmail password.

## Run Locally

```bash
python main.py
```

- The first time, a Chromium window opens. If you are not logged in, log in manually in that window.
- Your session is saved in `solinteg_browser_profile/`, so later runs need no login.
- Stop with `Ctrl + C`.

## Run with Docker (24/7)

```bash
# Build the image
docker build -t solinteg-monitor .

# Start in the background
docker compose up -d

# Watch the logs
docker logs -f solinteg-monitor

# Stop
docker compose down
```

Notes:
- The container runs in **headless** mode (no window), so set the three `LOGIN_*_SELECTOR` values for auto-login, or copy a logged-in profile into the volume.
- The login session is stored in the Docker volume `browser_profile`.
- The container restarts automatically after crashes or reboots.

## Settings Reference

| Variable | Default | Meaning |
|---|---|---|
| `SOC_THRESHOLD` | 40 | Alert when SOC is below this % |
| `SOC_REARM_MARGIN` | 2 | SOC must reach threshold + this value before a new alert |
| `CHECK_INTERVAL_SECONDS` | 60 | How often SOC is read |
| `PAGE_RELOAD_INTERVAL_SECONDS` | 300 | How often the page refreshes and login is verified |
| `HEADLESS` | false | Hide the browser window (must be true in Docker) |

## Example Output

```
[2026-11-03 10:00:00] Monitor started | threshold 40.0% | check 60s | refresh 300s
[2026-11-03 10:00:12] Battery SOC: 52.0%
[2026-11-03 10:01:12] Battery SOC: 41.0%
[2026-11-03 10:02:12] Battery SOC: 39.0%
[2026-11-03 10:02:12] ALERT: Battery SOC is 39.0%, below 40.0%!
[2026-11-03 10:02:13] Email alert sent to receiver@gmail.com.
[2026-11-03 10:03:12] Battery SOC: 38.5%        (no repeat alert)
[2026-11-03 10:20:12] Battery recovered to 43.0%. Alert re-armed.
```

## Troubleshooting

| Problem | Fix |
|---|---|
| `Could not find 'Battery xx%'` | You may be logged out or on the wrong page. Log in again and confirm the dashboard shows the battery %. |
| Email not sent | Check `GMAIL_FROM_EMAIL` and the App Password, and that 2-Step Verification is on. |
| Chromium crashes in Docker | Add `shm_size: "1gb"` to the compose service. |
| Asks for manual login in Docker | Headless can't show a login window. Set the login selectors. |
| Browser won't start after a crash | Delete the lock files (`SingletonLock`) in `solinteg_browser_profile/`. |

## Security Notes

- Never commit `.env` or `solinteg_browser_profile/` to Git.
- Add both to `.gitignore` and `.dockerignore`.
- The script only **reads** data from the dashboard and never changes anything on the station.

## Limitations

- Depends on the dashboard showing text like `Battery 93%`. If Solinteg changes its page layout, the extraction pattern may need updating.
- Needs a machine or server that stays on with internet access.
- Web scraping is a workaround. An official Solinteg API, if available, would be more reliable.
