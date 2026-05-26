#!/bin/bash


# ── 프로세스 종료 헬퍼 ────────────────────────────────────────────────────────
# 용법: stop_process <pgrep 패턴> <표시 이름>
stop_process() {
    local pattern="$1"
    local name="$2"

    if ! pgrep -f "$pattern" > /dev/null 2>&1; then
        echo "  ${name}: 실행 중 아님, 건너뜀"
        return 0
    fi

    echo "  ${name}: 종료 요청 (SIGTERM)..."
    pkill -TERM -f "$pattern" 2>/dev/null || true

    local elapsed=0
    while pgrep -f "$pattern" > /dev/null 2>&1; do
        sleep 1
        elapsed=$((elapsed + 1))
        if [ "$elapsed" -ge 10 ]; then
            echo "  ${name}: 응답 없음, 강제 종료 (SIGKILL)..."
            pkill -KILL -f "$pattern" 2>/dev/null || true
            sleep 1
            break
        fi
    done

    if pgrep -f "$pattern" > /dev/null 2>&1; then
        echo "  ${name}: 종료 실패" >&2
        return 1
    fi

    echo "  ${name}: 종료 완료"
}

# ── 기존 프로세스 종료 ────────────────────────────────────────────────────────
echo "[stop] 기존 프로세스 종료 중..."
stop_process "chromium" "Chromium"
stop_process "unclutter" "unclutter"
