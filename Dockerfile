# ── Solinteg Battery SOC Monitor ─────────────────────────────────────────────
# Headless Chromium + Python on Debian slim.
# Build:  docker build -t solinteg-monitor .
# Run:    docker run --env-file .env solinteg-monitor
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# System deps required by Playwright's Chromium
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
    libxkbcommon0 libpango-1.0-0 libcairo2 libasound2 \
    libx11-xcb1 libxcb-dri3-0 libdrm2 \
    fonts-liberation fonts-noto-color-emoji \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright's Chromium browser
RUN python -m playwright install chromium --with-deps

# Copy application code
COPY main.py .

# Browser profile stored in a Docker volume (persists login session)
VOLUME ["/app/solinteg_browser_profile"]

# Logs directory
RUN mkdir -p /app/logs

# HEADLESS must be true inside a container (no display)
ENV HEADLESS=true

CMD ["python", "main.py"]

