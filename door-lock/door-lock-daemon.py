from flask import Flask, jsonify, request, make_response
from gpiozero import OutputDevice
import time

GPIO_PIN = 18
UNLOCK_DURATION = 0.1  # 초 (레거시 open.py 기준)
PORT = 8080

relay = OutputDevice(GPIO_PIN, active_high=True, initial_value=False)
app = Flask(__name__)


def cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
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

    relay.on()
    time.sleep(UNLOCK_DURATION)
    relay.off()
    return cors(jsonify({"message": "ok"}))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, threaded=False)
