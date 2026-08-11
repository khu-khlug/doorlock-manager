from flask import Flask, jsonify, request, make_response
from gpiozero import OutputDevice
import atexit
import requests
import threading
import time
import os
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

GPIO_PIN = 18
UNLOCK_DURATION = 0.1  # 초 (레거시 open.py 기준)
PORT = 8080
LOG_FILE = "/var/log/door-lock/daemon.log"
SCHEDULE_CACHE_FILE = "/var/cache/door-lock/schedules.json"
SCHEDULE_REFRESH_INTERVAL = 3600  # 1시간
SCHEDULE_RETRY_INTERVAL = 600    # 실패 시 10분 후 재시도

# 블루투스 인증 설정. UUID는 16비트(2바이트)를 쓰며, 아직 확정되지 않아 TBD 값이다.
# 실제 값이 정해지면 아래 4개만 교체한다.
BLE_UUID_PRESENCE = 0x1111       # Pi -> 미등록 기기: 트리거 알림
BLE_UUID_REGISTER = 0x2222       # 기기 -> Pi: 학번 (등록 응답 / 하트비트)
BLE_UUID_CONFIRM = 0x3333        # Pi -> 기기: 등록 확인 (학번+랜덤ID)
BLE_UUID_HEARTBEAT_ACK = 0x4444  # Pi -> 기기: 하트비트 응답 (랜덤ID)

BLE_STUDENT_ID_LENGTH = 10          # 학번 ASCII 자릿수
BLE_TRIGGER_WINDOW_SECONDS = 5      # 앱이 등록 신호를 빠르게 반복 송신하는 시간과 동일하게 가정
BLE_CONFIRM_DURATION_SECONDS = 5    # 등록 확인 광고 유지 시간
BLE_HEARTBEAT_EXPIRY_SECONDS = 30   # 하트비트가 끊겨 등록 목록에서 제거되는 기준
BLE_HEARTBEAT_ACK_SLOT_MS = 400     # 등록 기기 1개당 하트비트 응답 광고 지속 시간
BLE_REAP_INTERVAL_SECONDS = 5       # 만료된 등록 정리 주기
BLE_TRIGGER_POLL_INTERVAL_MS = 500  # check_trigger() 폴링 주기
BLE_RANDOM_ID_MAX = 255             # 동시 등록 인원이 255명을 넘지 않는다는 가정하에 랜덤ID를 1바이트로 관리

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
_measurement_recorder = None
_measurement_uploader = None


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
            params={"from": start_of_day.strftime("%Y-%m-%dT%H:%M:%S"), "limit": 50},
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


@dataclass
class BackendResult:
    kind: str  # "ok" | "denied" | "timeout" | "network_error"
    status_code: Optional[int]
    body: dict


def request_backend_authorization(endpoint: str, payload: dict) -> BackendResult:
    try:
        resp = requests.post(
            f"{BACKEND_URL}{endpoint}",
            json=payload,
            headers={"x-api-key": INTERNAL_API_KEY},
            timeout=5,
        )
    except requests.exceptions.Timeout:
        return BackendResult("timeout", None, {})
    except requests.exceptions.RequestException as e:
        logger.error("backend network error: %s", e)
        return BackendResult("network_error", None, {})

    body = resp.json() if resp.content else {}
    kind = "ok" if resp.status_code == 200 else "denied"
    return BackendResult(kind, resp.status_code, body)


def attempt_unlock(endpoint: str, payload: dict, source: str) -> BackendResult:
    """백엔드 인증 후 성공하면 릴레이를 연다. HTTP 라우트와 블루투스 인증 로직이 공통으로 호출한다."""
    result = request_backend_authorization(endpoint, payload)
    if result.kind == "ok":
        relay.on()
        time.sleep(UNLOCK_DURATION)
        relay.off()
        logger.info("unlocked source=%s name=%s", source, result.body.get("name"))
    else:
        logger.info("denied source=%s kind=%s status=%s", source, result.kind, result.status_code)
    return result


# 블루투스 인증 상태 (모듈 전역, GLib 메인루프 스레드에서만 변경됨).
_ble_lock = threading.Lock()
_ble_mode = "idle"  # "idle" | "triggered"
_registered_devices = {}  # 학번 -> {"random_id": int, "last_heartbeat_at": float}
_candidates = {}          # triggered 중 수집: 학번 -> {"student_id", "rssi", "seen_at"}
_heartbeat_rotation_index = 0
_active_beacon = None
_random_id_counter = 0  # 다음에 발급할 랜덤ID. 항상 현재 비어있는 값을 가리킨다.


def _uuid16_to_str(uuid16: int) -> str:
    """16비트 UUID를 BlueZ가 쓰는 128비트 표준 UUID 문자열로 확장한다."""
    return f"0000{uuid16:04x}-0000-1000-8000-00805f9b34fb"


def _parse_service_data(service_data: dict) -> Optional[str]:
    """ServiceData에서 등록 UUID(BLE_UUID_REGISTER)의 학번을 추출한다."""
    raw = service_data.get(_uuid16_to_str(BLE_UUID_REGISTER))
    if raw is None:
        return None
    try:
        student_id = bytes(raw).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return None
    if len(student_id) != BLE_STUDENT_ID_LENGTH or not student_id.isdigit():
        return None
    return student_id


def _start_ble_measurement() -> None:
    """설정된 경우에만 BLE 측정 writer와 선택형 uploader를 시작한다."""
    database_path = os.environ.get("BLE_MEASUREMENT_DB")
    if not database_path:
        return
    hash_key = os.environ.get("BLE_MEASUREMENT_HASH_KEY")
    if not hash_key:
        logger.error("BLE_MEASUREMENT_DB is set but BLE_MEASUREMENT_HASH_KEY is missing")
        return

    from ble_measurement import (
        BleMeasurementRecorder,
        HttpBatchTransport,
        MeasurementUploader,
        SQLiteMeasurementStore,
    )

    global _measurement_recorder, _measurement_uploader
    try:
        pi_id = os.environ.get("BLE_MEASUREMENT_PI_ID", f"door-lock-pi-{ROOM_NUMBER}")
        store = SQLiteMeasurementStore(database_path)
        recorder = BleMeasurementRecorder(
            store,
            pi_id=pi_id,
            hash_key=hash_key.encode("utf-8"),
            logger=logger,
        )
        recorder.start()
        _measurement_recorder = recorder

        endpoint = os.environ.get("BLE_MEASUREMENT_ENDPOINT")
        token = os.environ.get("BLE_MEASUREMENT_TOKEN")
        if endpoint and token:
            uploader = MeasurementUploader(
                store,
                pi_id=pi_id,
                transport=HttpBatchTransport(endpoint, token),
                logger=logger,
            )
            uploader.start()
            _measurement_uploader = uploader
        elif endpoint:
            logger.error("BLE_MEASUREMENT_ENDPOINT is set but BLE_MEASUREMENT_TOKEN is missing")
        logger.info("ble measurement enabled database=%s pi_id=%s", database_path, pi_id)
    except Exception as error:
        logger.error("ble measurement initialization failed: %s", error)


def _stop_ble_measurement() -> None:
    if _measurement_uploader is not None:
        _measurement_uploader.close()
    if _measurement_recorder is not None:
        _measurement_recorder.close()


def _record_ble_measurement(student_id: str, rssi: Optional[int]) -> None:
    recorder = _measurement_recorder
    if recorder is None:
        return
    with _ble_lock:
        phase = _ble_mode
        packet_kind = "heartbeat" if student_id in _registered_devices else "register"
    recorder.observe(
        raw_identifier=student_id.encode("ascii"),
        packet_kind=packet_kind,
        service_uuid=_uuid16_to_str(BLE_UUID_REGISTER),
        rssi=int(rssi) if rssi is not None else None,
        pi_phase=phase,
    )


def _allocate_random_id() -> int:
    """등록 기기에 부여할 임의 ID(1바이트, 0~255)를 순차 발급한다.
    카운터가 항상 다음에 내줄 빈 값을 가리키고 있다가, 발급 시 그 값을 반환하고
    카운터를 그 다음 빈 값으로 옮겨둔다 (255 다음은 0으로 순환)."""
    global _random_id_counter
    issued = _random_id_counter
    used = {info["random_id"] for info in _registered_devices.values()}
    used.add(issued)
    next_id = (issued + 1) % (BLE_RANDOM_ID_MAX + 1)
    while next_id in used:
        next_id = (next_id + 1) % (BLE_RANDOM_ID_MAX + 1)
    _random_id_counter = next_id
    return issued


def _advertise_start(uuid16: int, payload: bytes) -> None:
    """주어진 UUID+payload로 BLE 광고를 시작한다 (기존 광고가 있으면 먼저 내린다).

    NOTE: bluezero.broadcaster.Beacon에 공개 stop_beacon()이 있는지 실기에서
    확인되지 않았다 — 없다면 D-Bus LEAdvertisingManager1.UnregisterAdvertisement()를
    직접 호출하는 방식으로 바뀔 수 있다 (README 열린 이슈 참고).
    """
    from bluezero import broadcaster

    global _active_beacon
    _advertise_stop()
    beacon = broadcaster.Beacon()
    beacon.add_service_data(_uuid16_to_str(uuid16), list(payload))
    beacon.start_beacon()
    _active_beacon = beacon


def _advertise_stop() -> None:
    """현재 활성 광고를 내린다. 활성 광고가 없으면 아무 것도 하지 않는다."""
    global _active_beacon
    if _active_beacon is None:
        return
    try:
        _active_beacon.stop_beacon()
    except AttributeError:
        logger.error("ble: Beacon.stop_beacon() unavailable, advertisement left running")
    _active_beacon = None


def check_trigger() -> bool:
    """TRIGGERED 진입 조건을 판단한다.

    TODO: 트리거 신호(버튼/모션센서 등)가 아직 정해지지 않아 항상 False를 반환한다.
    """
    return False


def _handle_ble_scan_result(student_id: str, rssi: int) -> None:
    """등록 UUID 신호 1건을 현재 모드(idle/triggered)에 맞게 처리한다."""
    with _ble_lock:
        mode = _ble_mode
        already_registered = student_id in _registered_devices
    if mode == "triggered":
        # 이미 등록된 기기가 저전력 하트비트를 계속 보내는 중일 수 있으므로,
        # 트리거 윈도우 동안 그 신호를 새 후보로 착각해 재인증/재개방하지 않도록 제외한다.
        if not already_registered:
            _candidates[student_id] = {"student_id": student_id, "rssi": rssi, "seen_at": time.monotonic()}
        return
    with _ble_lock:
        device = _registered_devices.get(student_id)
        if device is not None:
            device["last_heartbeat_at"] = time.monotonic()


def select_closest_candidate(candidates: list) -> Optional[dict]:
    """가장 가까운(RSSI가 가장 강한) 후보 하나를 고른다.

    TODO: 정확한 판별 알고리즘은 아직 미정 — 현재는 RSSI 최댓값으로 임시 구현.
    """
    if not candidates:
        return None
    return max(candidates, key=lambda c: c["rssi"])


def _confirm_candidate(candidate: dict) -> None:
    """선택된 후보 1명의 학번으로 백엔드에 인증을 시도한다 (성공하면 attempt_unlock 내부에서 문이 열린다).
    인증에 성공한 경우에만 랜덤ID를 발급해 등록하고 3333으로 확인 응답을 보낸다.
    실패하면 등록하지 않고 idle로 복귀한다."""
    from gi.repository import GLib

    student_id = candidate["student_id"]
    result = attempt_unlock(
        "/internal/door-lock/accesses",
        {"number": int(student_id), "roomNumber": ROOM_NUMBER},
        source="bluetooth",
    )
    if result.kind != "ok":
        _return_to_idle()
        return

    random_id = _allocate_random_id()
    with _ble_lock:
        _registered_devices[student_id] = {"random_id": random_id, "last_heartbeat_at": time.monotonic()}

    logger.info("ble registered student_id=%s random_id=%d", student_id, random_id)
    _advertise_start(BLE_UUID_CONFIRM, student_id.encode("ascii") + bytes([random_id]))
    GLib.timeout_add(BLE_CONFIRM_DURATION_SECONDS * 1000, _return_to_idle_tick)


def _return_to_idle() -> None:
    """triggered -> idle 복귀. 광고를 내리고 후보 목록을 비운다."""
    global _ble_mode
    _advertise_stop()
    with _ble_lock:
        _ble_mode = "idle"
        _candidates.clear()
    logger.info("ble returning to idle")


def _return_to_idle_tick() -> bool:
    """GLib.timeout_add용 래퍼. 등록 확인 광고 종료 후 idle로 복귀한다."""
    _return_to_idle()
    return False


def _end_triggered_window() -> bool:
    """트리거 윈도우 종료. 1111 광고를 내리고 후보를 선택하거나 idle로 복귀한다."""
    _advertise_stop()
    candidate = select_closest_candidate(list(_candidates.values()))
    if candidate is not None:
        _confirm_candidate(candidate)
    else:
        logger.info("ble triggered: no candidates found")
        _return_to_idle()
    return False  # one-shot 타이머, 반복하지 않음


def _enter_triggered_state() -> None:
    """idle -> triggered 전이. 후보 목록을 비우고 1111을 광고한 뒤 트리거 윈도우 타이머를 예약한다."""
    from gi.repository import GLib

    global _ble_mode
    with _ble_lock:
        _ble_mode = "triggered"
        _candidates.clear()
    logger.info("ble triggered: entering registration window")
    _advertise_start(BLE_UUID_PRESENCE, b"")
    GLib.timeout_add(BLE_TRIGGER_WINDOW_SECONDS * 1000, _end_triggered_window)


def _trigger_poll_tick() -> bool:
    """check_trigger()를 주기적으로 확인해 idle -> triggered 전이를 시작한다."""
    with _ble_lock:
        mode = _ble_mode
    if mode == "idle" and check_trigger():
        _enter_triggered_state()
    return True


def _send_heartbeat_ack(student_id: str, random_id: int) -> None:
    """등록 기기 하나에게 하트비트 응답(랜덤ID)을 짧게 광고한다."""
    _advertise_start(BLE_UUID_HEARTBEAT_ACK, bytes([random_id]))


def _heartbeat_rotation_tick() -> bool:
    """idle 상태에서 등록된 기기들을 순서대로 순회하며 하트비트 응답을 광고한다."""
    global _heartbeat_rotation_index
    with _ble_lock:
        mode = _ble_mode
        devices = list(_registered_devices.items())
    if mode != "idle" or not devices:
        return True
    _heartbeat_rotation_index %= len(devices)
    student_id, info = devices[_heartbeat_rotation_index]
    _heartbeat_rotation_index += 1
    _send_heartbeat_ack(student_id, info["random_id"])
    return True


def _reap_expired_registrations() -> bool:
    """30초 이상 하트비트가 없는 등록 기기를 목록에서 제거한다."""
    now = time.monotonic()
    with _ble_lock:
        expired = [
            student_id for student_id, info in _registered_devices.items()
            if now - info["last_heartbeat_at"] > BLE_HEARTBEAT_EXPIRY_SECONDS
        ]
        for student_id in expired:
            del _registered_devices[student_id]
    for student_id in expired:
        logger.info("ble registration expired student_id=%s", student_id)
    return True


def _handle_ble_scan_result_from_device(dev) -> None:
    """새로 발견된 BLE 기기(bluezero Device)의 최초 ServiceData를 처리한다."""
    student_id = _parse_service_data(dev.service_data or {})
    if student_id is not None:
        rssi = dev.RSSI
        _record_ble_measurement(student_id, rssi)
        _handle_ble_scan_result(student_id, rssi or 0)


def _on_bluez_properties_changed(interface, changed, invalidated, path) -> None:
    """이미 알려진 기기의 반복 광고(하트비트 등)를 처리한다.
    BlueZ는 같은 주소의 기기가 다시 광고하면 InterfacesAdded가 아니라
    PropertiesChanged를 보내므로 별도로 구독해야 한다."""
    service_data = changed.get("ServiceData")
    if service_data is None:
        return
    student_id = _parse_service_data(service_data)
    if student_id is not None:
        rssi = changed.get("RSSI")
        _record_ble_measurement(student_id, rssi)
        _handle_ble_scan_result(student_id, rssi or 0)


def _start_continuous_ble_discovery(dongle, dbus_module) -> None:
    """Continuously discover target advertisements without duplicate suppression."""
    dongle.adapter_methods.SetDiscoveryFilter(
        {
            "Transport": dbus_module.String("le"),
            "UUIDs": dbus_module.Array(
                [_uuid16_to_str(BLE_UUID_REGISTER)], signature="s"
            ),
            # DuplicateData also disables BlueZ's RSSI delta threshold.
            "DuplicateData": dbus_module.Boolean(True),
        }
    )
    dongle.start_discovery()


def _ble_main() -> None:
    """BLE 전용 스레드 진입점. D-Bus GLib 메인루프를 설정하고 계속 실행한다.

    NOTE: adapter.Adapter(discovery) + 원시 D-Bus PropertiesChanged 구독을 조합하는
    이 방식은 문서화된 예제가 없는 추정 코드이며, 실기 검증이 필요하다
    (README 열린 이슈 참고). 광고+스캔 동시 운용 자체도 미검증이다.
    """
    import dbus
    import dbus.mainloop.glib
    from gi.repository import GLib
    from bluezero import adapter

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    dongle = adapter.Adapter()
    dongle.on_device_found = _handle_ble_scan_result_from_device

    bus = dbus.SystemBus()
    bus.add_signal_receiver(
        _on_bluez_properties_changed,
        dbus_interface="org.freedesktop.DBus.Properties",
        signal_name="PropertiesChanged",
        arg0="org.bluez.Device1",
        path_keyword="path",
    )

    _start_continuous_ble_discovery(dongle, dbus)

    GLib.timeout_add(BLE_TRIGGER_POLL_INTERVAL_MS, _trigger_poll_tick)
    GLib.timeout_add(BLE_HEARTBEAT_ACK_SLOT_MS, _heartbeat_rotation_tick)
    GLib.timeout_add(BLE_REAP_INTERVAL_SECONDS * 1000, _reap_expired_registrations)

    GLib.MainLoop().run()


def start_reader() -> None:
    """BLE 스캔/광고 스레드를 기동한다. non-blocking으로 즉시 리턴한다."""
    t = threading.Thread(target=_ble_main, daemon=True)
    t.start()


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
    result = attempt_unlock(
        "/internal/door-lock/accesses",
        {"number": int(student_id), "roomNumber": ROOM_NUMBER},
        source="keypad",
    )

    if result.kind == "timeout":
        return cors(jsonify({"message": "timeout"})), 504
    if result.kind == "network_error":
        return cors(jsonify({"message": "network"})), 502
    if result.kind == "denied":
        return cors(jsonify({"message": "unauthorized"})), 403

    return cors(jsonify({"message": "ok", "name": result.body.get("name") or ""}))


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
    _start_ble_measurement()
    atexit.register(_stop_ble_measurement)
    start_reader()
    app.run(host="127.0.0.1", port=PORT, threaded=True)
