#!/bin/bash

# If run outside an X session, launch startx and exit
# (.xinitrc calls this script again with DISPLAY set)
if [ -z "$DISPLAY" ]; then
    startx
    exit
fi

DAEMON_DIR="$(cd "$(dirname "$0")" && pwd)"
PWA_URL="https://feat-door-lock.khlug-dev.pages.dev/door-lock"
BLANK_TIMEOUT=300          # 야간 화면 절전 시간 (초, 21:00~09:00)
TOUCH_CHIP="ft5x06"        # 터치스크린 칩 모델명 (xinput 장치 탐색용)

# ── 1. Stop existing processes ───────────────────────────────────────────────
echo "[1/3] Stopping existing processes..."
DAEMON_DIR="$DAEMON_DIR" bash "$DAEMON_DIR/stop-door-lock.sh"

# ── 2. Display setup ─────────────────────────────────────────────────────────
echo "[2/3] Configuring display..."
hour=$(date +%H)
if [ "$hour" -ge 9 ] && [ "$hour" -lt 21 ]; then
    xset s off
    xset -dpms
else
    xset s "$BLANK_TIMEOUT" "$BLANK_TIMEOUT"
    xset dpms "$BLANK_TIMEOUT" "$BLANK_TIMEOUT" "$BLANK_TIMEOUT"
fi
xrandr --output DSI-1 --rotate inverted
TOUCH_ID=$(xinput list 2>/dev/null | grep -i "$TOUCH_CHIP" | grep -oP 'id=\K[0-9]+' | head -1)
if [ -n "$TOUCH_ID" ]; then
    xinput set-prop "$TOUCH_ID" "Coordinate Transformation Matrix" -1 0 1 0 -1 1 0 0 1
fi
unclutter -idle 0 &  # hide mouse cursor

# ── 3. Launch Chromium kiosk ──────────────────────────────────────────────────
echo "[3/3] Launching Chromium..."
chromium \
    --kiosk \
    --disable-dev-shm-usage \
    --disable-extensions \
    --disable-background-networking \
    --no-first-run \
    --disable-translate \
    --noerrdialogs \
    --disable-infobars \
    --disable-features=PrivateNetworkAccessPermissionPrompt \
    --app="$PWA_URL"
