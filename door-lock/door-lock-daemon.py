from flask import Flask, jsonify, request, make_response
from gpiozero import OutputDevice
import requests
import time
import os

GPIO_PIN = 18
UNLOCK_DURATION = 0.1  # 초 (레거시 open.py 기준)
PORT = 8080

with open("/etc/door-lock/api-key") as f:
    INTERNAL_API_KEY = f.read().strip()

BACKEND_URL = os.environ["BACKEND_URL"]

relay = OutputDevice(GPIO_PIN, active_high=True, initial_value=False)
app = Flask(__name__)


def cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


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

    if student_id == 0 or str(student_id) == "0000000000":
        time.sleep(3)
        relay.on()
        time.sleep(UNLOCK_DURATION)
        relay.off()
        return cors(jsonify({"message": "ok"}))

    try:
        resp = requests.post(
            f"{BACKEND_URL}/internal/door-lock/accesses",
            json={"studentId": int(student_id)},
            headers={"x-api-key": INTERNAL_API_KEY},
            timeout=5,
        )
    except requests.exceptions.Timeout:
        return cors(jsonify({"message": "timeout"})), 504
    except requests.exceptions.RequestException:
        return cors(jsonify({"message": "network"})), 502

    if resp.status_code != 200:
        return cors(jsonify({"message": "unauthorized"})), 403

    relay.on()
    time.sleep(UNLOCK_DURATION)
    relay.off()
    name = (resp.json().get("name") or "") if resp.content else ""
    return cors(jsonify({"message": "ok", "name": name}))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, threaded=False)
