#!/bin/bash

# X 세션 밖에서 실행된 경우 startx 실행 후 종료
# (.xinitrc가 DISPLAY 세팅된 채로 이 스크립트를 다시 호출함)
if [ -z "$DISPLAY" ]; then
    startx
    exit
fi

DAEMON_DIR="$(cd "$(dirname "$0")" && pwd)"
PWA_URL="https://app.khlug.org/door-lock"
BLANK_TIMEOUT=300          # 야간 화면 절전 시간 (초, 21:00~09:00)

# ── 1. 기존 프로세스 종료 ─────────────────────────────────────────────────────
echo "[1/3] 기존 프로세스 종료 중..."
DAEMON_DIR="$DAEMON_DIR" bash "$DAEMON_DIR/stop-door-lock.sh"

# ── 2. 디스플레이 설정 ────────────────────────────────────────────────────────
echo "[2/3] 디스플레이 설정 중..."
hour=$(date +%H)
if [ "$hour" -ge 9 ] && [ "$hour" -lt 21 ]; then
    xset s off
    xset -dpms
else
    xset s "$BLANK_TIMEOUT" "$BLANK_TIMEOUT"
    xset dpms "$BLANK_TIMEOUT" "$BLANK_TIMEOUT" "$BLANK_TIMEOUT"
fi
xrandr --output DSI-1 --rotate inverted
TOUCH_ID=$(xinput list | grep -i "ft5x06" | grep -oP 'id=\K[0-9]+' | head -1)
if [ -n "$TOUCH_ID" ]; then
    xinput set-prop "$TOUCH_ID" "Coordinate Transformation Matrix" -1 0 1 0 -1 1 0 0 1
fi
unclutter -idle 0 &  # 마우스 커서 끔

# ── 3. Chromium 키오스크 실행 ─────────────────────────────────────────────────
echo "[3/3] Chromium 실행 중..."
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
