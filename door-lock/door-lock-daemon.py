from flask import Flask, jsonify, request
from gpiozero import OutputDevice
import time

GPIO_PIN = 18
UNLOCK_DURATION = 0.1  # 초 (레거시 open.py 기준)
PORT = 8080

relay = OutputDevice(GPIO_PIN, active_high=True, initial_value=False)
app = Flask(__name__)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/unlock", methods=["POST"])
def unlock():
    if request.remote_addr != "127.0.0.1":
        return jsonify({"message": "forbidden"}), 403
    relay.on()
    time.sleep(UNLOCK_DURATION)
    relay.off()
    return jsonify({"message": "ok"})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT)
