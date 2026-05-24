#!/bin/bash

DAEMON_DIR="$(cd "$(dirname "$0")" && pwd)"
DAEMON_PORT=8080
DAEMON_HEALTH="http://localhost:${DAEMON_PORT}/health"
DAEMON_HEALTH_TIMEOUT=30   # 데몬 준비 대기 최대 시간 (초)
PWA_URL="https://3a603f26.khlug-dev.pages.dev/door-lock"
BLANK_TIMEOUT=300          # 화면 절전 시간 (초)

# ── 1. 기존 프로세스 종료 ─────────────────────────────────────────────────────
echo "[1/4] 기존 프로세스 종료 중..."
DAEMON_DIR="$DAEMON_DIR" bash "$DAEMON_DIR/stop-door-lock.sh"

# ── 2. 데몬 시작 ──────────────────────────────────────────────────────────────
echo "[2/4] 데몬 시작 중..."
python3 "$DAEMON_DIR/door-lock-daemon.py" &

elapsed=0
until curl -sf "$DAEMON_HEALTH" > /dev/null 2>&1; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [ "$elapsed" -ge "$DAEMON_HEALTH_TIMEOUT" ]; then
        echo "데몬이 ${DAEMON_HEALTH_TIMEOUT}초 내에 준비되지 않았습니다." >&2
        exit 1
    fi
done
echo "  데몬 준비 완료 (${elapsed}초)"

# ── 3. 디스플레이 설정 ────────────────────────────────────────────────────────
echo "[3/4] 디스플레이 설정 중..."
xset s "$BLANK_TIMEOUT" "$BLANK_TIMEOUT"
xset dpms "$BLANK_TIMEOUT" "$BLANK_TIMEOUT" "$BLANK_TIMEOUT"
unclutter -idle 0 &  # 마우스 커서 끔

# ── 4. Chromium 키오스크 실행 ─────────────────────────────────────────────────
echo "[4/4] Chromium 실행 중..."
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
