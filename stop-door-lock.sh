#!/bin/bash


# ── Process stop helper ───────────────────────────────────────────────────────
# Usage: stop_process <pgrep pattern> <display name>
stop_process() {
    local pattern="$1"
    local name="$2"

    if ! pgrep -f "$pattern" > /dev/null 2>&1; then
        echo "  ${name}: not running, skipping"
        return 0
    fi

    echo "  ${name}: sending SIGTERM..."
    pkill -TERM -f "$pattern" 2>/dev/null || true

    local elapsed=0
    while pgrep -f "$pattern" > /dev/null 2>&1; do
        sleep 1
        elapsed=$((elapsed + 1))
        if [ "$elapsed" -ge 10 ]; then
            echo "  ${name}: not responding, sending SIGKILL..."
            pkill -KILL -f "$pattern" 2>/dev/null || true
            sleep 1
            break
        fi
    done

    if pgrep -f "$pattern" > /dev/null 2>&1; then
        echo "  ${name}: failed to stop" >&2
        return 1
    fi

    echo "  ${name}: stopped"
}

# ── Stop existing processes ───────────────────────────────────────────────────
echo "[stop] Stopping existing processes..."
stop_process "chromium" "Chromium"
stop_process "unclutter" "unclutter"
