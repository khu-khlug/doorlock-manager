#!/bin/bash

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
    --app="$PWA_URL"
