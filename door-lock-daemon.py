from flask import Flask, jsonify, request, make_response
from gpiozero import OutputDevice
import requests
import threading
import time
import os
import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

GPIO_PIN = 18
UNLOCK_DURATION = 0.1  # 초 (레거시 open.py 기준)
PORT = 8080
LOG_FILE = "/var/log/door-lock/daemon.log"
SCHEDULE_CACHE_FILE = "/var/cache/door-lock/schedules.json"
SCHEDULE_REFRESH_INTERVAL = 3600  # 1시간
SCHEDULE_RETRY_INTERVAL = 600    # 실패 시 10분 후 재시도

with open("/etc/door-lock/api-key") as f:
    INTERNAL_API_KEY = f.read().strip()

BACKEND_URL = os.environ["BACKEND_URL"]
ROOM_NUMBER = int(os.environ["ROOM_NUMBER"])

relay = OutputDevice(GPIO_PIN, active_high=True, initial_value=False)
app = Flask(__name__)

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger = logging.getLogger("door-lock")
logger.setLevel(logging.INFO)
logger.addHandler(handler)

_schedule_cache = []
_schedule_lock = threading.Lock()


def _parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _load_cache_from_file():
    try:
        with open(SCHEDULE_CACHE_FILE) as f:
            data = json.load(f)
        schedules = data.get("schedules", [])
        fetched_at = data.get("fetchedAt", 0)
        with _schedule_lock:
            _schedule_cache[:] = schedules
        logger.info("schedules loaded from file count=%d", len(schedules))
        return fetched_at
    except Exception:
        return 0


def _save_cache_to_file(schedules):
    try:
        os.makedirs(os.path.dirname(SCHEDULE_CACHE_FILE), exist_ok=True)
        with open(SCHEDULE_CACHE_FILE, "w") as f:
            json.dump({"schedules": schedules, "fetchedAt": datetime.now(timezone.utc).timestamp()}, f)
    except Exception as e:
        logger.error("schedule cache write error: %s", e)


def _refresh_schedules():
    now = datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    success = False
    try:
        resp = requests.get(
            f"{BACKEND_URL}/schedules",
            params={"from": start_of_day.isoformat(), "limit": 50},
            headers={"x-api-key": INTERNAL_API_KEY},
            timeout=10,
        )
        if resp.status_code == 200:
            schedules = resp.json().get("schedules", [])
            with _schedule_lock:
                _schedule_cache[:] = schedules
            _save_cache_to_file(schedules)
            logger.info("schedules refreshed count=%d", len(schedules))
            success = True
        else:
            logger.warning("schedule refresh failed status=%d", resp.status_code)
    except Exception as e:
        logger.error("schedule refresh error: %s", e)

    next_interval = SCHEDULE_REFRESH_INTERVAL if success else SCHEDULE_RETRY_INTERVAL
    t = threading.Timer(next_interval, _refresh_schedules)
    t.daemon = True
    t.start()


def cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


@app.route("/health", methods=["GET", "OPTIONS"])
def health():
    if request.method == "OPTIONS":
        return cors(make_response("", 204))
    return cors(jsonify({"status": "ok"}))


@app.route("/schedules/now", methods=["GET", "OPTIONS"])
def schedule_now():
    if request.method == "OPTIONS":
        return cors(make_response("", 204))
    now = datetime.now(timezone.utc)
    with _schedule_lock:
        current = next(
            (s for s in _schedule_cache
             if _parse_iso(s["scheduledAt"]) <= now
             and (s["endAt"] is None or _parse_iso(s["endAt"]) >= now)),
            None,
        )
    return cors(jsonify(current))


@app.route("/schedules/next", methods=["GET", "OPTIONS"])
def schedule_next():
    if request.method == "OPTIONS":
        return cors(make_response("", 204))
    now = datetime.now(timezone.utc)
    with _schedule_lock:
        nxt = next(
            (s for s in _schedule_cache if _parse_iso(s["scheduledAt"]) > now),
            None,
        )
    return cors(jsonify(nxt))


@app.route("/unlock", methods=["POST", "OPTIONS"])
def unlock():
    if request.method == "OPTIONS":
        return cors(make_response("", 204))

    if request.remote_addr != "127.0.0.1":
        return cors(jsonify({"message": "forbidden"})), 403

    data = request.get_json(silent=True) or {}
    student_id = data.get("studentId")
    if student_id is None:
        return cors(jsonify({"message": "studentId required"})), 400

    logger.info("unlock attempt student_id=%s", student_id)
    try:
        resp = requests.post(
            f"{BACKEND_URL}/internal/door-lock/accesses",
            json={"number": int(student_id), "roomNumber": ROOM_NUMBER},
            headers={"x-api-key": INTERNAL_API_KEY},
            timeout=5,
        )
    except requests.exceptions.Timeout:
        logger.warning("backend timeout student_id=%s", student_id)
        return cors(jsonify({"message": "timeout"})), 504
    except requests.exceptions.RequestException as e:
        logger.error("backend network error student_id=%s: %s", student_id, e)
        return cors(jsonify({"message": "network"})), 502

    if resp.status_code != 200:
        logger.info("unauthorized student_id=%s status=%s", student_id, resp.status_code)
        return cors(jsonify({"message": "unauthorized"})), 403

    relay.on()
    time.sleep(UNLOCK_DURATION)
    relay.off()
    name = (resp.json().get("name") or "") if resp.content else ""
    logger.info("unlocked student_id=%s name=%s", student_id, name)
    return cors(jsonify({"message": "ok", "name": name}))


if __name__ == "__main__":
    fetched_at = _load_cache_from_file()
    elapsed = datetime.now(timezone.utc).timestamp() - fetched_at
    if elapsed >= SCHEDULE_REFRESH_INTERVAL:
        _refresh_schedules()
    else:
        t = threading.Timer(SCHEDULE_REFRESH_INTERVAL - elapsed, _refresh_schedules)
        t.daemon = True
        t.start()
        logger.info("schedules cache valid, next refresh in %.0fs", SCHEDULE_REFRESH_INTERVAL - elapsed)
    app.run(host="127.0.0.1", port=PORT, threaded=True)
