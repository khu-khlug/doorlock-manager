from flask import Flask, jsonify, request, make_response
from gpiozero import OutputDevice
import atexit
import requests
import signal
import subprocess
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
        HttpSyncTransport,
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
                boot_id=recorder.boot_id,
                transport=HttpSyncTransport(endpoint, token),
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


def _record_ble_measurement(raw_identifier: bytes, packet_kind: str, rssi: Optional[int]) -> None:
    """측정 실패가 도어락의 기존 BLE 처리를 중단하지 않도록 격리한다."""
    recorder = _measurement_recorder
    if recorder is None:
        return
    try:
        recorder.observe(
            raw_identifier=raw_identifier,
            packet_kind=packet_kind,
            service_uuid=Ble.Payload.uuid16_to_str(Ble.UUID_REGISTER),
            rssi=rssi,
            pi_phase="continuous",
        )
    except Exception:
        logger.exception("ble measurement callback failed")


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


def _log_callback_exceptions(default_return):
    """콜백에서 예외가 나면 조용히 죽는 대신, 로그 파일에 남기고 계속 돌게 한다."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception:
                logger.exception("ble callback error in %s", func.__name__)
                return default_return
        wrapper.__name__ = func.__name__
        return wrapper
    return decorator


class Ble:
    """블루투스 인증 전체를 소유한다. 상수는 여기(클래스 속성)에 두고, 각 관심사는 아래
    중첩 클래스(Payload/Registry/Advertising/Adapter)가 자기 상태와 함수만 갖는다.

    안쪽 네 클래스는 서로를 모른다 — 여러 클래스를 아우르는 조정 로직(신호 수신 → 파싱 →
    등록 → 광고 갱신)은 이 바깥 클래스의 메서드로만 존재한다. 프로세스당 인스턴스 하나면
    충분하므로(`_ble` 전역 하나) 클래스 속성은 상수, 인스턴스 속성은 상태로 구분한다.

    **UUID 값과 payload 규격은 앱(parksiwoo2/doorlock-frontend) 원격 main의 BleConstants.kt /
    BlePayloadCodec.kt가 기준**이다. 앱을 우리에 맞추는 게 아니라 우리가 앱에 맞춘다.
    """

    UUID_PRESENCE = 0x0312       # Pi -> 폰: 상시 광고 (앱의 TARGET_UUID)
    UUID_REGISTER = 0x1111       # 폰 -> Pi: 등록 요청 / 하트비트 (앱의 RESPONSE_UUID)
    UUID_CONFIRM = 0x2222        # Pi -> 폰: 등록 확인, 학번+세션토큰 (앱의 OPEN_UUID)
    UUID_HEARTBEAT_ACK = 0x3333  # Pi -> 폰: 재실 명단 (앱의 HEARTBEAT_UUID)

    # /etc/door-lock/ble-uuids가 있으면 기동 시 위 UUID 네 개를 덮어쓴다 (아래 _load_uuids_from_file 참고).
    # 코드 재배포 없이 UUID만 바꿔야 할 때를 위함.
    UUID_CONFIG_PATH = "/etc/door-lock/ble-uuids"

    STUDENT_ID_LENGTH = 10                 # 학번 ASCII 자릿수 (디코딩 후 길이)
    ENCODED_STUDENT_ID_LENGTH = 20         # 앱이 학번을 2바이트씩 뒤바꾼 뒤 16진수로 인코딩한 길이
    REGISTRATION_PAYLOAD_LENGTH = ENCODED_STUDENT_ID_LENGTH + 1  # 인코딩 학번 + 공개여부(1바이트)
    HEARTBEAT_PAYLOAD_LENGTH = 2           # 세션토큰(1바이트) + 공개여부(1바이트)

    HEARTBEAT_EXPIRY_SECONDS = 30   # 하트비트가 끊겨 등록 목록에서 제거되는 기준
    REAP_INTERVAL_SECONDS = 5       # 만료된 등록 정리 주기
    PROXIMITY_SCAN_INTERVAL_SECONDS = 1  # 근접 후보를 배치로 모아 판정하는 주기

    # 세션토큰(랜덤ID) 범위. **0은 앱이 명단의 빈 슬롯 표시용으로 예약**했으므로 발급하면 안 된다
    # (앱 BlePayloadCodec.tokenByte의 require(sessionToken in 1..255)).
    RANDOM_ID_MIN = 1
    RANDOM_ID_MAX = 255

    # 재실 명단(HEARTBEAT_ACK) payload는 **정확히 24바이트 고정**이다. 앱의
    # matchesHeartbeatRoster가 payload.size != 24면 무조건 거부하므로 반드시 지켜야 한다.
    # 등록된 토큰을 앞에서부터 채우고 남는 뒤쪽은 0으로 패딩한다.
    ROSTER_PAYLOAD_BYTES = 24

    # 동시에 등록할 수 있는 인원. 토큰 하나가 payload 1바이트를 쓰므로 상한은 위 길이와
    # 같지만, **둘은 별개의 값이다** — 이건 우리 정책이고 위는 앱이 강제하는 전송 규격이다.
    # 정원을 낮춰 시험할 때 이 값만 건드려야 한다. 위 길이를 낮추면 명단 payload가 24바이트가
    # 아니게 되어 앱이 명단을 통째로 거부하고, 폰이 자기 등록 상태를 확인하지 못해 세션을
    # 끊었다가 재등록하면서 문이 주기적으로 다시 열린다.
    ROSTER_CAPACITY = ROSTER_PAYLOAD_BYTES

    # 광고 1건의 ServiceData 상한. legacy 광고 31바이트에서 ServiceData AD 헤더
    # 4바이트(길이+타입+16비트 UUID)를 빼고, BlueZ가 Flags AD(3바이트)를 붙일 여지까지 감안한 값.
    MAX_SERVICE_DATA_BYTES = 24

    # 앱은 트리거의 UUID 존재 여부만 보고 payload 내용은 안 본다. 다만 완전히 빈 바이트는
    # dbus-python이 D-Bus 타입 시그니처를 못 정해 "Failed to parse advertisement"가
    # 나는 것으로 실기에서 확인돼, 더미 1바이트를 넣는다.
    PRESENCE_PAYLOAD = b"\x00"

    # 광고 인스턴스별 송출 제어. 확장 광고 미지원 하드웨어라 커널이 등록된 인스턴스들을
    # round-robin으로 교대 송출하며, Duration이 각 인스턴스의 1회 airtime이다
    # (org.bluez.LEAdvertisement.rst의 Duration = "Rotation duration"). Duration 변경 자체는
    # 거의 공짜다(비확장 광고 컨트롤러에서는 커널이 구조체 값만 갱신하고 HCI 왕복도 없다 —
    # net/bluetooth/hci_core.c의 hci_add_adv_instance 참고). 진짜 병목은 "그 채널의 로테이션
    # 순번이 돌아오기까지의 대기 시간"뿐이라, 평소엔 세 채널을 균등하게 짧은 주기로 돌려
    # 전체 로테이션을 최대한 짧게 유지한다.
    TRIGGER_DURATION_SECONDS = 1   # 상시광고/명단/확인 균등 1초씩 -> 평소 로테이션 주기 3초
    ROSTER_DURATION_SECONDS = 1
    CONFIRM_DURATION_SECONDS = 1   # 확인의 평소(비활성) Duration
    CONFIRM_ACTIVE_DURATION_SECONDS = 5  # 인증 성공 시 이 값으로 잠깐 키운다 (아래 참고)
    CONFIRM_CONTENT_SECONDS = 5    # 인증 성공 후 실제 내용을 보여주는 시간(우리 타이머, BlueZ Timeout 아님).
    # 이 두 값을 같게 잡은 이유: 활성 상태의 로테이션 주기는 1+1+5=7초이고, 확인 채널
    # 자신의 airtime은 그 7초 중 5초를 통째로 차지하는 한 덩어리라 폰이 놓칠 일이 사실상
    # 없다. 순번을 막 놓친 최악의 경우에도 트리거+명단(최대 2초)만 기다리면 확인 채널
    # 차례가 오므로, 앱의 10초 하드 타임아웃(BleRelayService.kt의 openConfirmationTimeoutMillis)
    # 대비 여유가 충분하다.
    # 광고 반복 간격. 짧게 잡을수록 폰이 짧은 스캔 창 안에서도 광고를 받을 확률이 높아진다.
    ADV_MIN_INTERVAL_MS = 100
    ADV_MAX_INTERVAL_MS = 200
    # bluetoothd를 능동 재시작한 뒤 어댑터가 D-Bus에 다시 나타날 때까지 기다리는 시간.
    # 상태를 반복 확인하는 폴링이 아니라, 재시작 직후 한 번만 쉬는 고정 지연이다.
    SERVICE_RESTART_SETTLE_SECONDS = 3

    # BlueZ D-Bus 이름들. BlueZ D-Bus API를 직접 쓰므로 여기서 정의한다.
    BLUEZ_SERVICE = "org.bluez"
    ADAPTER_IFACE = "org.bluez.Adapter1"
    DEVICE_IFACE = "org.bluez.Device1"
    LE_ADVERTISEMENT_IFACE = "org.bluez.LEAdvertisement1"
    LE_ADVERTISING_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"
    OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
    # 어댑터 경로는 자동 탐색하며, BLE_ADAPTER 환경변수로 재정의할 수 있다.
    ADAPTER_PATH_OVERRIDE = os.environ.get("BLE_ADAPTER")
    # 광고 인스턴스 3개의 D-Bus 오브젝트 경로.
    AD_PATH_TRIGGER = "/org/khlug/doorlock/advertisement0"
    AD_PATH_ROSTER = "/org/khlug/doorlock/advertisement1"
    AD_PATH_CONFIRM = "/org/khlug/doorlock/advertisement2"

    @classmethod
    def _load_uuids_from_file(cls) -> None:
        """UUID_CONFIG_PATH가 있으면 UUID 네 개를 덮어쓴다. 코드 재배포 없이 UUID만
        바꿔야 할 때를 위한 것으로, 없으면 위 기본값을 그대로 쓴다."""
        if not os.path.exists(cls.UUID_CONFIG_PATH):
            return
        values = {}
        with open(cls.UUID_CONFIG_PATH) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                # UUID는 관례상 16진수로 적으므로 "0x" 접두사가 없어도 16진수로 해석한다.
                values[key.strip()] = int(value.strip(), 16)
        if "presence" in values:
            cls.UUID_PRESENCE = values["presence"]
        if "register" in values:
            cls.UUID_REGISTER = values["register"]
        if "confirm" in values:
            cls.UUID_CONFIRM = values["confirm"]
        if "heartbeat_ack" in values:
            cls.UUID_HEARTBEAT_ACK = values["heartbeat_ack"]
        logger.info(
            "ble uuids loaded from %s presence=%#06x register=%#06x confirm=%#06x heartbeat_ack=%#06x",
            cls.UUID_CONFIG_PATH, cls.UUID_PRESENCE, cls.UUID_REGISTER,
            cls.UUID_CONFIRM, cls.UUID_HEARTBEAT_ACK,
        )

    class Payload:
        """인코딩/디코딩 순수함수. 상태도, 다른 세 클래스도 참조하지 않는다."""

        @staticmethod
        def uuid16_to_str(uuid16: int) -> str:
            """16비트 UUID를 BlueZ가 쓰는 128비트 표준 UUID 문자열로 확장한다."""
            return f"0000{uuid16:04x}-0000-1000-8000-00805f9b34fb"

        @staticmethod
        def decode_student_id(encoded: bytes) -> Optional[str]:
            """앱의 BlePayloadCodec.encodeStudentId() 인코딩을 복원한다.
            학번 10자리 ASCII를 2바이트씩 맞바꾼 뒤 16진수 20글자로 표현한 것을 원래 학번으로 되돌린다."""
            if len(encoded) != Ble.ENCODED_STUDENT_ID_LENGTH:
                return None
            try:
                swapped = bytes.fromhex(encoded.decode("ascii"))
            except (ValueError, UnicodeDecodeError):
                return None
            if len(swapped) != Ble.STUDENT_ID_LENGTH:
                return None
            unswapped = bytearray(swapped)
            for i in range(0, Ble.STUDENT_ID_LENGTH, 2):
                unswapped[i], unswapped[i + 1] = unswapped[i + 1], unswapped[i]
            try:
                student_id = bytes(unswapped).decode("ascii")
            except UnicodeDecodeError:
                return None
            if len(student_id) != Ble.STUDENT_ID_LENGTH or not student_id.isdigit():
                return None
            return student_id

        @staticmethod
        def encode_student_id(student_id: str) -> bytes:
            """decode_student_id()의 역변환. Pi가 CONFIRM(2222)에 학번을 실을 때 앱과 같은 인코딩으로 맞춘다."""
            swapped = bytearray(student_id.encode("ascii"))
            for i in range(0, Ble.STUDENT_ID_LENGTH, 2):
                swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
            return bytes(swapped).hex().upper().encode("ascii")

        @staticmethod
        def extract_register_payload(service_data: dict, register_uuid16: int) -> Optional[bytes]:
            """ServiceData에서 등록 UUID의 원본 payload를 그대로 꺼낸다.
            UUID를 인자로 받아 Ble.UUID_REGISTER를 직접 참조하지 않는다 — 파일에서 UUID를
            다시 읽어도(Ble._load_uuids_from_file) 이 함수 자체는 손댈 필요가 없다."""
            raw = service_data.get(Ble.Payload.uuid16_to_str(register_uuid16))
            if raw is None:
                return None
            return bytes(raw)

        @staticmethod
        def parse_registration_payload(payload: bytes) -> Optional[tuple]:
            """등록 신호(인코딩 학번 20바이트 + 공개여부 1바이트 = 21바이트)를 파싱한다."""
            if len(payload) != Ble.REGISTRATION_PAYLOAD_LENGTH:
                return None
            student_id = Ble.Payload.decode_student_id(payload[:Ble.ENCODED_STUDENT_ID_LENGTH])
            if student_id is None:
                return None
            visible = payload[Ble.ENCODED_STUDENT_ID_LENGTH] != 0
            return student_id, visible

        @staticmethod
        def parse_heartbeat_payload(payload: bytes) -> Optional[tuple]:
            """하트비트 신호(랜덤ID 1바이트 + 공개여부 1바이트 = 2바이트)를 파싱한다."""
            if len(payload) != Ble.HEARTBEAT_PAYLOAD_LENGTH:
                return None
            return payload[0], payload[1] != 0

        @staticmethod
        def roster_payload(random_ids: list) -> bytes:
            """재실 명단(HEARTBEAT_ACK) payload를 만든다.

            **항상 정확히 Ble.ROSTER_PAYLOAD_BYTES 바이트**여야 한다 — 앱의 matchesHeartbeatRoster가
            payload.size != 24면 무조건 거부한다. 등록된 세션토큰을 앞에서부터 채우고 남는
            뒤쪽은 0으로 패딩한다(0은 앱이 빈 슬롯으로 해석하는 예약값). Registry.register()가
            정원(24명) 초과 등록을 애초에 거부하므로, random_ids는 항상 이 길이 이하로 들어온다.
            """
            ordered = sorted(random_ids)
            return bytes(ordered) + bytes(Ble.ROSTER_PAYLOAD_BYTES - len(ordered))

    class Registry:
        """재실 테이블 · 후보 · 세션토큰 발급. Ble 상수만 참조하고 Advertising/Adapter는 모른다."""

        def __init__(self):
            self._lock = threading.Lock()
            self._registered_devices = {}  # 학번 -> {"random_id", "last_heartbeat_at", "visible"}
            self._candidates = {}          # 1초 배치 동안 쌓인 최신 후보. 학번 -> {...}
            self._random_id_counter = Ble.RANDOM_ID_MIN  # 다음에 발급할 세션토큰. 항상 비어있는 값을 가리킨다.

        def touch_heartbeat(self, random_id: int, visible: bool) -> None:
            """하트비트를 받은 등록 기기의 마지막 수신 시각과 공개 여부를 갱신한다."""
            with self._lock:
                for info in self._registered_devices.values():
                    if info["random_id"] == random_id:
                        info["last_heartbeat_at"] = time.monotonic()
                        info["visible"] = visible
                        return

        def student_id_for_token(self, random_id: int) -> Optional[str]:
            """등록된 세션토큰을 측정용 학번 식별자로 역매핑한다."""
            with self._lock:
                for student_id, info in self._registered_devices.items():
                    if info["random_id"] == random_id:
                        return student_id
            return None

        def add_candidate(self, student_id: str, rssi: int, visible: bool) -> None:
            """1초 배치 윈도우에 등록 후보를 쌓는다. 같은 폰이 여러 번 보내면 최신 것으로
            덮어쓴다.

            이미 등록된 기기인지는 여기서 거르지 않고 꺼낼 때 거른다
            (snapshot_and_clear_candidates 참고) — 이유는 그쪽 주석에 있다."""
            with self._lock:
                if student_id not in self._candidates and student_id not in self._registered_devices:
                    # 폰은 초당 여러 번 같은 신호를 보낸다. 매번 찍으면 24시간 운용에서
                    # 로그가 폭주하니 처음 한 번만 남긴다. 등록을 마친 기기가 확인 광고를
                    # 놓쳐 등록 요청을 계속 보내는 경우도 로그에서 제외한다.
                    logger.info("ble candidate added student_id=%s rssi=%d visible=%s", student_id, rssi, visible)
                self._candidates[student_id] = {
                    "student_id": student_id,
                    "rssi": rssi,
                    "seen_at": time.monotonic(),
                    "visible": visible,
                }

        def snapshot_and_clear_candidates(self) -> list:
            """직전 1초 동안 쌓인 후보를 꺼내고 비운다. **이미 등록된 기기는 여기서 제외한다.**

            거르는 시점이 꺼낼 때인 이유: 후보로 들어간 뒤 실제로 인증되기까지 최대 1초(배치
            주기) + 백엔드 왕복 시간이 걸린다. 넣을 때만 검사하면 그 사이에 등록이 끝난 기기가
            다음 배치에 그대로 남아 재인증되고, 문이 다시 열리면서 세션토큰까지 새로 발급된다
            (폰은 먼저 받은 토큰을 들고 있는데 재실 명단에는 새 토큰이 실려 서로 어긋난다).
            꺼내는 시점에 확인하면 등록 직후의 잔여 후보가 전부 걸러진다.

            _registered_devices와 _candidates를 같은 락 안에서 함께 보므로, register()가
            중간에 끼어들어 생기는 경합은 없다.
            """
            with self._lock:
                candidates = [
                    info for student_id, info in self._candidates.items()
                    if student_id not in self._registered_devices
                ]
                self._candidates.clear()
            return candidates

        def _next_random_id(self, value: int) -> int:
            """세션토큰 순환. RANDOM_ID_MIN..RANDOM_ID_MAX 안에서만 돈다."""
            span = Ble.RANDOM_ID_MAX - Ble.RANDOM_ID_MIN + 1
            return Ble.RANDOM_ID_MIN + ((value - Ble.RANDOM_ID_MIN + 1) % span)

        def _allocate_random_id(self) -> int:
            """등록 기기에 부여할 세션토큰(1바이트)을 순차 발급한다. 호출자가 이미
            self._lock을 쥔 상태에서만 불러야 한다.

            **0은 절대 발급하지 않는다** — 앱이 재실 명단의 빈 슬롯 표시용으로 예약한 값이라,
            0을 주면 그 기기는 명단에서 자기 토큰을 영영 못 찾는다.
            """
            if self._random_id_counter < Ble.RANDOM_ID_MIN:
                self._random_id_counter = Ble.RANDOM_ID_MIN
            issued = self._random_id_counter
            used = {info["random_id"] for info in self._registered_devices.values()}
            used.add(issued)
            next_id = self._next_random_id(issued)
            while next_id in used and next_id != issued:
                next_id = self._next_random_id(next_id)
            self._random_id_counter = next_id
            return issued

        def has_capacity(self) -> bool:
            """정원(Ble.ROSTER_CAPACITY명)에 자리가 남았는지 알려준다.

            **인증을 시도하기 전에 이걸로 먼저 걸러야 한다.** 백엔드 인증에 성공하면 그
            안에서 곧바로 문이 열리므로, 등록 단계에서야 정원 초과를 발견하면 문은 이미
            열린 뒤다. 그러면 정원이 찼는데도 문만 열리고 세션토큰·명단 등록·확인 광고는
            전부 실패하는 상태가 되고, 등록이 안 됐으니 그 폰은 다음 배치에서도 다시
            후보로 뽑혀 매초 문이 열린다."""
            with self._lock:
                return len(self._registered_devices) < Ble.ROSTER_CAPACITY

        def register(self, student_id: str, visible: bool) -> Optional[int]:
            """정원(Ble.ROSTER_CAPACITY명) 안에서만 세션토큰을 발급하고 등록한다.
            꽉 차 있으면 아무것도 하지 않고 None을 반환한다 — 정원을 넘는 등록은 실패로
            처리한다. 그래서 명단 payload는 항상 한 묶음이면 충분하다.

            호출자가 has_capacity()로 미리 걸러도 이 검사는 남겨둔다 — 등록 성립 여부는
            이 메서드 단독으로 보장돼야 한다."""
            with self._lock:
                if len(self._registered_devices) >= Ble.ROSTER_CAPACITY:
                    return None
                random_id = self._allocate_random_id()
                self._registered_devices[student_id] = {
                    "random_id": random_id,
                    "last_heartbeat_at": time.monotonic(),
                    "visible": visible,
                }
                return random_id

        def reap_expired(self) -> list:
            """Ble.HEARTBEAT_EXPIRY_SECONDS 넘게 하트비트 없는 항목을 제거하고
            제거된 학번 목록을 반환한다."""
            now = time.monotonic()
            with self._lock:
                expired = [
                    student_id for student_id, info in self._registered_devices.items()
                    if now - info["last_heartbeat_at"] > Ble.HEARTBEAT_EXPIRY_SECONDS
                ]
                for student_id in expired:
                    del self._registered_devices[student_id]
            for student_id in expired:
                logger.info("ble registration expired student_id=%s", student_id)
            return expired

        def roster_tokens(self) -> list:
            """현재 등록된 세션토큰 전체 목록 (명단 광고 갱신용)."""
            with self._lock:
                return [info["random_id"] for info in self._registered_devices.values()]

        def log_table(self) -> None:
            """재실 인원 테이블(학번 -> 세션토큰)이 바뀔 때마다 전체 스냅샷을 남긴다."""
            with self._lock:
                table = {sid: info["random_id"] for sid, info in self._registered_devices.items()}
            logger.info("ble roster table=%s", table)

    class Advertising:
        """광고 인스턴스 3개(트리거·명단·확인). Ble.Payload만 참조하고 Registry/Adapter는 모른다."""

        def __init__(self):
            self.trigger_ad = None   # 0312, 기동 시 등록 후 상시 유지
            self.roster_ad = None    # 3333, 기동 시 등록 후 상시 유지. 등록 목록이 바뀔 때 payload만 갱신
            self.confirm_ad = None   # 2222, 이것도 기동 시 등록 후 상시 유지. 평소엔 비활성값을 내보내다가
                                      # 인증 성공 시에만 실제 내용 + Duration을 잠깐 키운다(런타임
                                      # register/unregister는 하지 않는다 — show_confirm 참고)
            self.ad_manager_methods = None  # org.bluez.LEAdvertisingManager1 인터페이스 (등록/해제 전용)

        @staticmethod
        def _advertisement_class():
            """org.bluez.LEAdvertisement1을 구현하는 D-Bus 오브젝트 클래스를 만들어 반환한다.

            dbus.service.Object는 상속으로만 쓸 수 있어 클래스가 불가피하고, 그 데코레이터가
            정의 시점에 dbus 모듈을 필요로 한다. 그런데 이 파일은 dbus가 없는 개발 PC에서도
            (상태 전이 단위 테스트 목적으로) import될 수 있어야 하므로, 클래스 정의를 이
            팩토리 안에 넣어 실제로 BLE를 쓸 때만 만들어지게 한다.
            """
            import dbus
            import dbus.exceptions
            import dbus.service

            class DoorLockAdvertisement(dbus.service.Object):
                """광고 인스턴스 하나.

                내용 교체는 unregister/register가 아니라 PropertiesChanged 신호로 한다.
                bluetoothd는 등록에 성공하면 이 오브젝트에 property watch를 걸고
                (BlueZ 5.82 src/advertising.c:1384), ServiceData가 바뀌었다는 신호를 받으면
                refresh_advertisement()로 새 데이터를 컨트롤러에 밀어넣는다(같은 파일 1300-1323).

                Duration/Timeout/MinInterval/MaxInterval은 bluezero의 Advertisement 클래스에는
                없는 속성이라, bluezero로는 송출 시점과 주기를 제어할 수 없다.
                """

                def __init__(self, bus, path, uuid16, label, include_service_uuids=False,
                             duration=None, timeout=None, on_released=None):
                    super().__init__(bus, path)
                    self.path = path
                    self.label = label
                    self.uuid16 = uuid16
                    self._uuid_str = Ble.Payload.uuid16_to_str(uuid16)
                    self._on_released = on_released
                    self._lock = threading.Lock()
                    self._payload = None
                    # "broadcast"(non-connectable)면 커널이 매 등록마다 새 임의 주소(NRPA)를
                    # 요구한다(mgmt-api.txt의 Add Extended Advertising Parameters Command 설명).
                    # 우리는 상시 스캔을 켜두므로 그 요청이 Bluetooth Core Spec Vol 4 Part E
                    # §7.8.4상 "스캐닝 중엔 LE Set Random Address 금지"에 걸려 매번
                    # Command Disallowed(0x0c)로 거부됐다(실기 btmon 캡처로 확인).
                    # "peripheral"(connectable)로 두면 identity(고정 공개 MAC) 주소를 쓰므로
                    # 이 명령 자체가 필요 없어진다. GATT 연결은 안 쓰므로 폰 쪽엔 영향 없다.
                    self._props = {"Type": "peripheral"}
                    # 앱은 트리거만 setServiceUuid(= Service UUID 목록 AD)로 거르고 나머지는
                    # setServiceData로 거른다. UUID 목록은 4바이트를 더 먹으므로 꼭 필요한
                    # 인스턴스에만 싣는다(24바이트 명단에 넣으면 31바이트 한도를 넘는다).
                    if include_service_uuids:
                        self._props["ServiceUUIDs"] = dbus.Array([self._uuid_str], signature="s")
                    self._props["ServiceData"] = dbus.Dictionary({}, signature="sv")
                    self._props["MinInterval"] = dbus.UInt32(Ble.ADV_MIN_INTERVAL_MS)
                    self._props["MaxInterval"] = dbus.UInt32(Ble.ADV_MAX_INTERVAL_MS)
                    if duration is not None:
                        self._props["Duration"] = dbus.UInt16(duration)
                    if timeout is not None:
                        self._props["Timeout"] = dbus.UInt16(timeout)

                def set_payload(self, payload) -> bool:
                    """이 인스턴스의 ServiceData를 교체한다. 내용이 같으면 아무 것도 하지 않는다."""
                    payload = bytes(payload)
                    if len(payload) > Ble.MAX_SERVICE_DATA_BYTES:
                        logger.error(
                            "ble: %s payload too long len=%d (max %d)",
                            self.label, len(payload), Ble.MAX_SERVICE_DATA_BYTES,
                        )
                        return False
                    with self._lock:
                        if self._payload == payload:
                            return False
                        self._payload = payload
                        self._props["ServiceData"] = dbus.Dictionary(
                            {self._uuid_str: dbus.Array(payload, signature="y")}, signature="sv"
                        )
                        changed = dbus.Dictionary(
                            {"ServiceData": self._props["ServiceData"]}, signature="sv"
                        )
                    self.PropertiesChanged(
                        Ble.LE_ADVERTISEMENT_IFACE, changed, dbus.Array([], signature="s")
                    )
                    return True

                def set_duration(self, seconds: int) -> bool:
                    """이 인스턴스의 로테이션 Duration(초)을 바꾼다. 값이 같으면 아무 것도 하지 않는다.

                    변경 자체는 사실상 공짜다 — 확장 광고 미지원 컨트롤러에서는 커널이 구조체
                    필드만 갱신하고 HCI 왕복도 없다(hci_add_adv_instance). 다만 이미 진행 중인
                    로테이션 순번을 끊고 끼어들지는 못하고, 다음에 이 인스턴스 차례가 왔을 때부터
                    새 값이 적용된다."""
                    with self._lock:
                        if self._props.get("Duration") == dbus.UInt16(seconds):
                            return False
                        self._props["Duration"] = dbus.UInt16(seconds)
                        changed = dbus.Dictionary({"Duration": self._props["Duration"]}, signature="sv")
                    self.PropertiesChanged(
                        Ble.LE_ADVERTISEMENT_IFACE, changed, dbus.Array([], signature="s")
                    )
                    return True

                def set_service_uuids_visible(self, visible: bool) -> bool:
                    """이 인스턴스의 Service UUID 목록 AD를 실었다 뺐다 한다.
                    값이 이미 그 상태면 아무 것도 하지 않는다.

                    앱은 트리거 광고를 Service UUID 목록으로 거르므로, 이 목록을 비우면
                    광고 등록을 유지한 채로 폰의 스캔 필터에 안 걸리게 만들 수 있다.
                    ServiceData만 비워서는 앱 필터가 그대로 통과하므로 소용이 없다.

                    bluetoothd는 ServiceData와 마찬가지로 ServiceUUIDs 변경도 감시하다가
                    같은 인스턴스 번호로 광고 데이터를 다시 밀어넣는다. 런타임
                    register/unregister를 쓰지 않고도 송출을 껐다 켤 수 있는 이유다."""
                    with self._lock:
                        already = "ServiceUUIDs" in self._props
                        if already == visible:
                            return False
                        if visible:
                            self._props["ServiceUUIDs"] = dbus.Array(
                                [self._uuid_str], signature="s"
                            )
                        else:
                            self._props["ServiceUUIDs"] = dbus.Array([], signature="s")
                        changed = dbus.Dictionary(
                            {"ServiceUUIDs": self._props["ServiceUUIDs"]}, signature="sv"
                        )
                        if not visible:
                            # 다음 호출에서 "빠져 있음"으로 판정되도록 키 자체를 지운다.
                            # 신호에는 빈 배열을 실어 bluetoothd가 목록을 비우게 한다.
                            del self._props["ServiceUUIDs"]
                    self.PropertiesChanged(
                        Ble.LE_ADVERTISEMENT_IFACE, changed, dbus.Array([], signature="s")
                    )
                    return True

                @dbus.service.signal(dbus.PROPERTIES_IFACE, signature="sa{sv}as")
                def PropertiesChanged(self, interface, changed, invalidated):
                    """bluetoothd가 이 신호를 받아 광고 데이터를 갱신한다. 데코레이터가 신호
                    발신을 담당하므로 본문은 비어 있고, 호출하는 것 자체가 발신이다."""

                @dbus.service.method(dbus.PROPERTIES_IFACE,
                                     in_signature="s", out_signature="a{sv}")
                def GetAll(self, interface):
                    if interface != Ble.LE_ADVERTISEMENT_IFACE:
                        raise dbus.exceptions.DBusException(
                            "unknown interface " + interface,
                            name="org.freedesktop.DBus.Error.InvalidArgs",
                        )
                    return dbus.Dictionary(self._props, signature="sv")

                @dbus.service.method(dbus.PROPERTIES_IFACE,
                                     in_signature="ss", out_signature="v")
                def Get(self, interface, name):
                    return self.GetAll(interface)[name]

                @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ssv")
                def Set(self, interface, name, value):
                    # bluetoothd가 등록 후 Instance 속성을 써 넣는 경우가 있다. 우리가 쓰지
                    # 않는 값이라도 거부하면 안 되므로 그대로 받아둔다.
                    self._props[name] = value

                @dbus.service.method(Ble.LE_ADVERTISEMENT_IFACE,
                                     in_signature="", out_signature="")
                def Release(self):
                    """bluetoothd가 광고를 회수했을 때 호출된다(확인 광고의 Timeout 만료 등).
                    이 시점엔 이미 해제된 뒤라 UnregisterAdvertisement를 부르면 안 된다."""
                    logger.info("ble %s advertisement released by bluetoothd", self.label)
                    if self._on_released is not None:
                        self._on_released()

            return DoorLockAdvertisement

        def init(self, bus, ad_manager_methods) -> None:
            """광고 인스턴스 3개를 만든다. D-Bus 왕복(등록)은 하지 않는다."""
            self.ad_manager_methods = ad_manager_methods
            advertisement_cls = self._advertisement_class()

            # 상시광고: 항상 송출. 앱이 setServiceUuid로 거르므로 UUID 목록을 함께 싣는다.
            # Duration을 길게 잡아 다른 인스턴스로 넘어가는 공백을 짧게 유지한다.
            self.trigger_ad = advertisement_cls(
                bus, Ble.AD_PATH_TRIGGER, Ble.UUID_PRESENCE, "trigger",
                include_service_uuids=True, duration=Ble.TRIGGER_DURATION_SECONDS,
            )
            self.trigger_ad.set_payload(Ble.PRESENCE_PAYLOAD)

            # 명단: 상시 송출. 24바이트라 UUID 목록까지 넣으면 31바이트 한도를 넘으므로 ServiceData만.
            self.roster_ad = advertisement_cls(
                bus, Ble.AD_PATH_ROSTER, Ble.UUID_HEARTBEAT_ACK, "roster",
                duration=Ble.ROSTER_DURATION_SECONDS,
            )
            self.roster_ad.set_payload(Ble.Payload.roster_payload([]))

            # 확인: 이것도 기동 시 상시 등록한다 — 인증 성공 시에만 register/unregister하면
            # 런타임 등록/해제가 AlreadyExists 영구 교착을 만든다(register_persistent 주석
            # 참고). 그래서 등록은 기동 시 한 번뿐이고, 평소엔 비활성값(전부 0)을
            # 싣고, 인증 성공 시에만 잠깐 실제 값으로 갈아끼운다 — 인코딩 학번은 항상
            # ASCII 16진수 문자('0'-'9','A'-'F')라 전부 0인 바이트는 어떤 실제 학번과도
            # 절대 일치하지 않으므로, 이 값이 곧 "아무 신호도 없음"과 동등하다.
            self.confirm_ad = advertisement_cls(
                bus, Ble.AD_PATH_CONFIRM, Ble.UUID_CONFIRM, "confirm",
                duration=Ble.CONFIRM_DURATION_SECONDS,
            )
            self.confirm_ad.set_payload(bytes(Ble.REGISTRATION_PAYLOAD_LENGTH))

        def _register_one(self, ad, on_error=None) -> None:
            """광고를 bluetoothd에 등록한다.

            **반드시 비동기(reply_handler/error_handler)로 부르고, 메인루프가 돌 수 있는
            상태에서 불러야 한다.** RegisterAdvertisement는 즉시 응답하지 않는다 — bluetoothd는
            먼저 우리 광고 오브젝트로 GDBusProxy를 만들어(BlueZ src/advertising.c:1607,1620)
            Introspect/GetAll을 **우리 프로세스로 되돌아 호출**하고, 그 응답을 받아
            client_proxy_added()(1571)를 거친 뒤에야 우리에게 응답을 보낸다(1703에서 return NULL로
            응답을 미룬다). 블로킹으로 부르면 서로 기다리는 데드락이 되어 25초 뒤 NoReply로 끝난다.

            재시도 루프는 두지 않는다. NoReply가 나도 bluetoothd 큐에는 클라이언트가 남아 있어
            (1701) 같은 (owner, path)로 재시도하면 무조건 AlreadyExists가 되고(1733), 한 번
            어긋나면 영구 교착이 된다. 실패는 재시도 대신 로그로 남긴다.
            """
            import dbus

            def _on_ok():
                logger.info("ble %s advertisement registered path=%s", ad.label, ad.path)

            def _on_error(error):
                logger.error("ble: failed to register %s advertisement: %s", ad.label, error)
                if on_error is not None:
                    on_error()

            self.ad_manager_methods.RegisterAdvertisement(
                ad.path,
                dbus.Dictionary({}, signature="sv"),
                reply_handler=_on_ok,
                error_handler=_on_error,
            )

        def _unregister_one(self, ad) -> None:
            """광고를 해제한다. 등록돼 있지 않으면 조용히 넘어간다.

            블로킹으로 불러도 안전하다 — RegisterAdvertisement와 달리 bluetoothd가 우리 쪽으로
            되돌아 호출할 게 없다(Release()는 문서상 noreply). 종료가 매달리지 않게 상한만 둔다.
            """
            if ad is None or self.ad_manager_methods is None:
                return
            try:
                self.ad_manager_methods.UnregisterAdvertisement(ad.path, timeout=5)
                logger.info("ble %s advertisement unregistered", ad.label)
            except Exception as error:
                # 애초에 등록돼 있지 않으면 DoesNotExist가 나는데 정상 경로다.
                logger.debug("ble: unregister %s skipped (%s)", ad.label, error)

        def register_persistent(self) -> None:
            """세 인스턴스(트리거·명단·확인)를 전부 등록한다. 프로세스당 한 번만 호출한다.

            확인 인스턴스도 여기서 함께 등록한다 — 인증 이벤트마다 register/unregister를
            반복하면 AlreadyExists 영구 교착에 빠진다(RegisterAdvertisement가 NoReply로 끝나도
            bluetoothd 큐에는 클라이언트가 남아, 같은 (owner, path) 재시도가 전부 거부된다 —
            _register_one 주석 참고). 세 인스턴스 모두 기동 시 한 번 등록해두고, 이후엔
            내용만 갈아끼운다.

            등록 전에 같은 경로를 먼저 해제해둔다. BlueZ는 소유자(D-Bus 발신자 이름)가 다르면
            남의 광고를 건드리지 못하게 막으므로(advertising.c:1727) 이전 프로세스가 남긴 것은
            어차피 우리가 못 지우지만, 우리 프로세스 안에서 상태가 꼬였을 때를 대비한 방어다.
            """
            for ad in (self.trigger_ad, self.roster_ad, self.confirm_ad):
                self._unregister_one(ad)
                self._register_one(ad)

        def unregister_all(self) -> None:
            for ad in (self.confirm_ad, self.roster_ad, self.trigger_ad):
                self._unregister_one(ad)

        def refresh_roster(self, tokens: list) -> None:
            """등록 목록이 바뀌었을 때 명단 광고 payload를 갱신한다."""
            if self.roster_ad is None:
                return
            self.roster_ad.set_payload(Ble.Payload.roster_payload(tokens))

        def set_presence_enabled(self, enabled: bool) -> None:
            """트리거(PRESENCE) 광고가 폰에 잡히게 할지 말지를 정한다.

            정원이 차면 끄고, 자리가 나면 다시 켠다. 폰이 애초에 진입 신호를 못 보게 해서
            들어올 수 없는 상태에서 등록 요청을 반복하는 걸 막는다.

            **광고를 해제하지 않고 Service UUID 목록만 비운다.** 런타임 해제 후 재등록은
            등록이 한 번만 어긋나도 영구 교착에 빠지는 경로라 쓰지 않는다.

            이미 등록을 마친 사람들에게는 영향이 없다 — 앱은 진입 감시 단계에서만 이 신호를
            보고, 등록 뒤에는 재실 명단 쪽만 본다."""
            if self.trigger_ad is None:
                return
            if self.trigger_ad.set_service_uuids_visible(enabled):
                logger.info("ble presence advertisement %s", "enabled" if enabled else "disabled (roster full)")

        def show_confirm(self, student_id: str, random_id: int) -> None:
            """등록 확인(2222) 내용을 Ble.CONFIRM_CONTENT_SECONDS 동안만 실제 값으로 보여준다.

            확인 인스턴스는 기동 시부터 항상 등록돼 있다 — 여기서 하는 일은 register가 아니라
            set_payload/set_duration 뿐이다. Duration도 이 동안만 CONFIRM_ACTIVE_DURATION_SECONDS로
            키워서, 로테이션 순번이 왔을 때 확인 채널이 훨씬 오래(그리고 훨씬 확실하게) 잡히게 한다.
            시간이 지나면 hide_confirm이 둘 다 평소 값으로 되돌린다."""
            self.confirm_ad.set_payload(Ble.Payload.encode_student_id(student_id) + bytes([random_id]))
            self.confirm_ad.set_duration(Ble.CONFIRM_ACTIVE_DURATION_SECONDS)
            t = threading.Timer(Ble.CONFIRM_CONTENT_SECONDS, self.hide_confirm)
            t.daemon = True
            t.start()

        def hide_confirm(self) -> None:
            """확인 내용과 Duration을 평소값(비활성 payload, 1초)으로 되돌린다."""
            if self.confirm_ad is not None:
                self.confirm_ad.set_payload(bytes(Ble.REGISTRATION_PAYLOAD_LENGTH))
                self.confirm_ad.set_duration(Ble.CONFIRM_DURATION_SECONDS)

    class Adapter:
        """BlueZ 연결·스캔·메인루프. Ble.Payload에는 의존하지만(등록 payload 추출),
        Registry/Advertising은 전혀 모른다 — import조차 하지 않고 콜백으로만 위에 보고한다."""

        @staticmethod
        def _run_privileged(args: list, label: str) -> bool:
            """sudo가 필요한 셋업 명령 하나를 실행하고 성공 여부를 로그와 함께 돌려준다."""
            try:
                result = subprocess.run(args, capture_output=True, text=True, timeout=15)
            except Exception as error:
                logger.error("ble: could not run %s: %s", label, error)
                return False
            if result.returncode != 0:
                logger.error(
                    "ble: %s failed (rc=%d): %s", label, result.returncode, result.stderr.strip()
                )
                return False
            return True

        def prepare(self) -> None:
            """블루투스 어댑터를 능동적으로 살려서 항상 깨끗한 상태에서 시작한다.

            **rfkill 소프트 블록**이 걸려 있으면 Powered 설정이 org.bluez.Error.Failed로
            거부되고, 이어지는 SetDiscoveryFilter도 org.bluez.Error.NotReady로 실패해 BLE
            스레드가 통째로 죽는다. 실기(Pi 4)에서 `rfkill list`의 hci0이 `Soft blocked: yes`,
            `hciconfig -a`의 hci0이 `DOWN`인 상태로 확인된 조합이다. rfkill이 막고 있으면
            bluetoothd를 몇 번 재시작해도 그 아래 인터페이스를 못 올리므로, Powered를
            시도하기 전에 반드시 rfkill부터 풀어야 한다.

            블루투스는 이 데몬 전용이라 재시작·언블록해도 방해받는 다른 프로세스가 없다.
            그래서 "상태를 확인하고 필요하면 조치"가 아니라 **매 기동마다 무조건** rfkill을
            풀고 bluetoothd를 재시작해서 항상 같은 known-good 상태에서 시작한다 — 확인 로직을
            늘리는 대신 환경을 우리가 직접 통제한다.
            """
            if self._run_privileged(["sudo", "rfkill", "unblock", "bluetooth"], "rfkill unblock bluetooth"):
                logger.info("ble: rfkill unblocked")

            if self._run_privileged(["sudo", "systemctl", "restart", "bluetooth"], "bluetooth.service restart"):
                logger.info("ble: bluetooth.service restarted")

            time.sleep(Ble.SERVICE_RESTART_SETTLE_SECONDS)

        def find_adapter_path(self, bus) -> str:
            """org.bluez.Adapter1을 구현한 오브젝트를 찾아 그 경로를 돌려준다.

            경로를 하드코딩하면 USB 동글을 꽂아 어댑터가 늘거나 번호가 바뀌었을 때 엉뚱한(또는
            고장난) 어댑터를 계속 잡는다. BLE_ADAPTER 환경변수로 강제 지정할 수 있다.
            """
            import dbus

            if Ble.ADAPTER_PATH_OVERRIDE:
                return Ble.ADAPTER_PATH_OVERRIDE
            manager = dbus.Interface(bus.get_object(Ble.BLUEZ_SERVICE, "/"), Ble.OBJECT_MANAGER_IFACE)
            for path, interfaces in manager.GetManagedObjects().items():
                if Ble.ADAPTER_IFACE in interfaces:
                    return str(path)
            raise RuntimeError("no org.bluez.Adapter1 found (is the controller present and powered?)")

        def log_capacity(self, bus, adapter_path: str) -> None:
            """기동 시 광고 인스턴스 사용 현황을 남긴다.

            다른 프로세스가 등록한 광고는 BlueZ가 소유자 기준으로 막아 우리가 지울 수 없으므로
            (advertising.c:1727), 최소한 눈에 보이게 로그로 남겨 진단에 쓴다.
            """
            import dbus

            try:
                props = dbus.Interface(
                    bus.get_object(Ble.BLUEZ_SERVICE, adapter_path), dbus.PROPERTIES_IFACE
                )
                active = int(props.Get(Ble.LE_ADVERTISING_MANAGER_IFACE, "ActiveInstances"))
                free = int(props.Get(Ble.LE_ADVERTISING_MANAGER_IFACE, "SupportedInstances"))
                logger.info("ble advertising instances active=%d free=%d", active, free)
                if active:
                    logger.warning(
                        "ble: %d advertising instance(s) already active before our registration", active
                    )
            except Exception:
                logger.debug("ble: could not read advertising instance counts", exc_info=True)

        def connect(self):
            """D-Bus GLib 메인루프를 설정하고 어댑터를 켠 뒤 (bus, adapter_path, adapter_object)를
            반환한다."""
            import dbus
            import dbus.mainloop.glib

            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            bus = dbus.SystemBus()

            adapter_path = self.find_adapter_path(bus)
            adapter_object = bus.get_object(Ble.BLUEZ_SERVICE, adapter_path)
            adapter_props = dbus.Interface(adapter_object, dbus.PROPERTIES_IFACE)
            try:
                address = str(adapter_props.Get(Ble.ADAPTER_IFACE, "Address"))
            except Exception:
                address = "?"
            logger.info("ble adapter path=%s address=%s", adapter_path, address)

            # 어댑터가 꺼져 있으면 광고 등록도 스캔도 실패한다. 다만 일부 USB 동글 펌웨어는
            # SET_POWERED를 지원하지 않는 사례가 보고돼 있으므로, 실패해도 BLE를 통째로
            # 죽이지 말고 경고만 남기고 진행한다(이미 켜져 있으면 문제가 없다).
            try:
                adapter_props.Set(Ble.ADAPTER_IFACE, "Powered", dbus.Boolean(True))
            except Exception as error:
                logger.warning("ble: could not set adapter Powered (continuing): %s", error)

            self.log_capacity(bus, adapter_path)
            return bus, adapter_path, adapter_object

        def run(self, bus, adapter_object, on_register_signal) -> None:
            """시그널 구독 + 스캔 시작 + GLib 메인루프 실행(블로킹).

            원시 ServiceData에서 등록 UUID의 payload만 뽑아 on_register_signal(payload, rssi)로
            위(Ble)에 보고한다 — 그 payload가 하트비트인지 등록 요청인지, Registry에 뭘 하는지는
            Adapter가 전혀 모른다.
            """
            import dbus
            from gi.repository import GLib

            def _extract_and_report(properties) -> None:
                service_data = properties.get("ServiceData")
                if service_data is None:
                    return
                payload = Ble.Payload.extract_register_payload(service_data, Ble.UUID_REGISTER)
                if payload is not None:
                    on_register_signal(payload, int(properties.get("RSSI", 0)))

            @_log_callback_exceptions(None)
            def _on_interfaces_added(path, interfaces) -> None:
                """새로 발견된 기기의 최초 광고를 처리한다.

                시그널 payload에 이미 들어 있는 ServiceData/RSSI를 그대로 읽는다. bluezero의
                adapter._interfaces_added는 이 payload를 버리고 주소로 Device 오브젝트를 다시
                조회하는데, 폰들이 랜덤 MAC을 빠르게 바꾸는 환경에선 조회 전에 기기가 사라져
                "ValueError: Cannot find a device"가 끊임없이 터진다."""
                properties = interfaces.get(Ble.DEVICE_IFACE)
                if properties:
                    _extract_and_report(properties)

            @_log_callback_exceptions(None)
            def _on_properties_changed(interface, changed, invalidated, path) -> None:
                """이미 알려진 기기의 반복 광고(하트비트 등)를 처리한다.
                BlueZ는 같은 주소의 기기가 다시 광고하면 InterfacesAdded가 아니라
                PropertiesChanged를 보내므로 둘 다 구독해야 한다."""
                _extract_and_report(changed)

            bus.add_signal_receiver(
                _on_interfaces_added,
                dbus_interface=Ble.OBJECT_MANAGER_IFACE,
                signal_name="InterfacesAdded",
            )
            bus.add_signal_receiver(
                _on_properties_changed,
                dbus_interface="org.freedesktop.DBus.Properties",
                signal_name="PropertiesChanged",
                arg0=Ble.DEVICE_IFACE,
                path_keyword="path",
            )

            # NOTE: `UUIDs` 필터는 광고의 "Service UUID 목록" AD 구조체만 검사하고
            # "Service Data" AD 구조체는 보지 않는다. 앱은 학번을 Service Data로만
            # 싣고 별도 Service UUID 목록을 붙이지 않으므로, UUIDs 필터를 걸면
            # BlueZ가 이 기기를 아예 걸러버려 우리 콜백에 전달되지 않는다. 그래서 UUID
            # 필터 없이 전체 LE 기기를 받아 extract_register_payload에서 ServiceData
            # 내용으로 직접 걸러낸다.
            adapter_methods = dbus.Interface(adapter_object, Ble.ADAPTER_IFACE)
            adapter_methods.SetDiscoveryFilter(
                {
                    "Transport": dbus.String("le"),
                    # DuplicateData는 반복 광고(하트비트)를 계속 받기 위해 필요하고,
                    # 부수적으로 BlueZ의 RSSI 변화량 임계치 필터링도 비활성화한다.
                    "DuplicateData": dbus.Boolean(True),
                }
            )
            adapter_methods.StartDiscovery()
            logger.info("ble discovery started (continuous)")

            GLib.MainLoop().run()

    def __init__(self):
        self.registry = Ble.Registry()
        self.advertising = Ble.Advertising()
        self.adapter = Ble.Adapter()

    def _on_register_signal(self, payload: bytes, rssi: int) -> None:
        """폰이 보낸 REGISTER 신호 1건을 파싱해 Registry에 위임한다.

        폰은 UUID 하나(REGISTER)로 두 가지를 보내고 **payload 길이로 구분**된다:
        2바이트(세션토큰+공개여부) = 하트비트, 21바이트(인코딩 학번+공개여부) = 등록 요청.
        하트비트는 상시 처리한다 — 놓치면 30초 뒤 만료되고 폰도 세션을 끊어 재등록하면서
        **이미 안에 있는 사람에게 문이 다시 열린다.**
        """
        heartbeat = Ble.Payload.parse_heartbeat_payload(payload)
        if heartbeat is not None:
            random_id, visible = heartbeat
            student_id = self.registry.student_id_for_token(random_id)
            raw_identifier = (
                student_id.encode("ascii")
                if student_id is not None
                else b"heartbeat-token:" + bytes([random_id])
            )
            _record_ble_measurement(raw_identifier, "heartbeat", rssi)
            self.registry.touch_heartbeat(random_id, visible)
            return

        registration = Ble.Payload.parse_registration_payload(payload)
        if registration is None:
            # 우리 규격이 아닌 길이. 주변 기기가 같은 UUID를 쓰는 경우일 수 있어 조용히 무시한다.
            logger.debug("ble register signal ignored len=%d rssi=%d", len(payload), rssi)
            return

        student_id, visible = registration
        _record_ble_measurement(student_id.encode("ascii"), "register", rssi)
        self.registry.add_candidate(student_id, rssi, visible)

    def _proximity_scan_loop(self) -> None:
        """1초마다 그 사이 쌓인 후보를 모아 범위 내 가장 가까운 하나를 인증한다.
        threading.Timer로 스스로 재예약한다 — 이 환경에서 GLib.timeout_add는
        최초 1회 이후 반복 실행되지 않는 것으로 실기에서 확인됐다 (README 참고)."""
        try:
            candidates = self.registry.snapshot_and_clear_candidates()
            # 정원이 찼으면 후보를 비우기만 하고 판정도 인증도 하지 않는다. 정원이 찬 동안엔
            # 0312를 안 내보내므로 후보가 새로 쌓일 일도 거의 없지만, 정원이 차기 직전에
            # 들어온 신호가 남아 있을 수 있다.
            if self.registry.has_capacity():
                candidate = select_closest_candidate_in_range(candidates)
                if candidate is not None:
                    self._confirm_candidate(candidate)
        except Exception:
            logger.exception("ble proximity scan error")
        t = threading.Timer(Ble.PROXIMITY_SCAN_INTERVAL_SECONDS, self._proximity_scan_loop)
        t.daemon = True
        t.start()

    def _confirm_candidate(self, candidate: dict) -> None:
        """선택된 후보 1명을 인증하고, 성공하면 등록 + 확인 광고까지 처리한다.

        **정원 확인이 인증보다 먼저다.** attempt_unlock은 백엔드 인증이 통과하는 즉시 문을
        열기 때문에, 자리가 없는데 인증부터 하면 문만 열리고 등록은 실패하는 상태가 된다.
        정원이 찬 동안에는 블루투스 인증 자체가 성립하지 않으며, 그때는 키패드로 출입한다."""
        student_id = candidate["student_id"]
        if not self.registry.has_capacity():
            logger.info("ble registration rejected (roster full) student_id=%s", student_id)
            return

        result = attempt_unlock(
            "/internal/door-lock/accesses",
            {"number": int(student_id), "roomNumber": ROOM_NUMBER},
            source="bluetooth",
        )
        if result.kind != "ok":
            return

        visible = candidate.get("visible", True)
        random_id = self.registry.register(student_id, visible)
        if random_id is None:
            logger.info("ble registration rejected (roster full) student_id=%s", student_id)
            return

        logger.info("ble registered student_id=%s token=%d visible=%s", student_id, random_id, visible)
        # 명단에 새 토큰을 반영하고, 확인 채널 내용을 잠깐 실제 값으로 보여준다.
        # (확인 인스턴스는 기동 시부터 항상 등록돼 있으므로 여기서 register하지 않는다.)
        self.advertising.refresh_roster(self.registry.roster_tokens())
        # 이번 등록으로 정원이 찼으면 진입 신호를 끊는다.
        self.advertising.set_presence_enabled(self.registry.has_capacity())
        self.registry.log_table()
        self.advertising.show_confirm(student_id, random_id)

    def _reap_loop(self) -> None:
        """30초 이상 하트비트가 없는 등록 기기를 목록에서 제거한다.
        threading.Timer로 스스로 재예약한다."""
        try:
            expired = self.registry.reap_expired()
            if expired:
                self.advertising.refresh_roster(self.registry.roster_tokens())
                # 자리가 났으면 진입 신호를 다시 내보낸다.
                self.advertising.set_presence_enabled(self.registry.has_capacity())
                self.registry.log_table()
        except Exception:
            logger.exception("ble reap error")
        t = threading.Timer(Ble.REAP_INTERVAL_SECONDS, self._reap_loop)
        t.daemon = True
        t.start()

    def start(self) -> None:
        """BLE 전용 스레드 진입점. 어댑터를 연결하고 광고를 등록한 뒤 메인루프를 돌린다."""
        self.adapter.prepare()
        bus, _adapter_path, adapter_object = self.adapter.connect()

        import dbus
        ad_manager_methods = dbus.Interface(adapter_object, Ble.LE_ADVERTISING_MANAGER_IFACE)
        self.advertising.init(bus, ad_manager_methods)

        # 근접 스캔/만료 정리는 GLib.timeout_add 대신 threading.Timer 기반 자기재예약
        # 루프로 돌린다 — 이 환경에서 GLib.timeout_add는 최초 1회 이후 반복 실행되지
        # 않는 것으로 실기에서 확인됐다 (README 참고).
        for loop_fn in (self._proximity_scan_loop, self._reap_loop):
            t = threading.Timer(0.5, loop_fn)
            t.daemon = True
            t.start()

        # 등록은 비동기라 여기서는 메시지만 큐에 넣고 바로 리턴한다. 실제 D-Bus 왕복과
        # bluetoothd가 우리에게 되거는 Introspect/GetAll은 adapter.run()의 메인루프가 처리한다.
        # (블로킹으로 부르면 서로 기다리다 NoReply — Advertising._register_one 주석 참고.)
        self.advertising.register_persistent()

        self.adapter.run(bus, adapter_object, self._on_register_signal)

    @_log_callback_exceptions(None)
    def start_logged(self) -> None:
        """start()를 감싸 예외를 daemon.log에 남긴다. 이게 없으면 BLE 스레드가 죽어도
        stderr에만 트레이스백이 찍히고 로그 파일엔 아무 것도 안 남아, 겉보기엔 데몬이
        멀쩡한데 BLE만 조용히 멈춘 상태가 되어 진단이 불가능해진다."""
        self.start()

    def shutdown(self, *_args) -> None:
        """프로세스 종료 시 등록된 BLE 광고를 확실히 해제한다.

        SIGTERM은 이 모듈이 등록한 핸들러(_handle_shutdown_signal)가 SystemExit을
        던져서 atexit이 정상적으로 실행되지만, SIGKILL(kill -9)은 파이썬이 정리할
        틈도 없이 즉시 죽어서 이 메서드가 아예 호출되지 않는다 — 그러면 bluetoothd의
        자동 정리(D-Bus 연결 끊김 감지)에만 의존하게 되는데, 이게 항상 즉시 이뤄지지는
        않아 다음 프로세스가 뜰 때 광고 등록이 "Already Exists"로 실패할 수 있다.
        그래서 테스트 시 데몬을 죽일 땐 -9 없이 pkill/일반 kill(SIGTERM)을 써야 한다."""
        self.advertising.unregister_all()


Ble._load_uuids_from_file()

# 실측 전에는 기존 동작과 같은 RSSI 점수를 사용한다. 환경별 측정 결과가 모이면
# estimate_proximity_score() 내부를 교체한다.
BLE_PROXIMITY_SCORE_THRESHOLD = -60


def estimate_proximity_score(candidate: dict) -> int:
    """후보의 근접도 점수를 반환한다. 점수가 클수록 가까운 것으로 판정한다.

    현재는 기존 목업 동작을 보존하기 위해 RSSI를 그대로 사용한다. 실측 후에는
    주머니·차폐·실내 위치 등 환경 변수를 고려한 판정으로 이 함수만 교체한다.
    """
    return candidate["rssi"]


def select_closest_candidate_in_range(candidates: list) -> Optional[dict]:
    """문 앞 범위 안의 후보 중 근접도 점수가 가장 높은 후보를 반환한다."""
    closest_candidate = None
    closest_score = None

    for candidate in candidates:
        score = estimate_proximity_score(candidate)
        if score < BLE_PROXIMITY_SCORE_THRESHOLD:
            continue
        if closest_score is None or score > closest_score:
            closest_candidate = candidate
            closest_score = score

    return closest_candidate


_ble = Ble()


def start_reader() -> None:
    """BLE 스캔/광고 스레드를 기동한다. non-blocking으로 즉시 리턴한다."""
    t = threading.Thread(target=_ble.start_logged, daemon=True)
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


def _shutdown_ble(*_args) -> None:
    _ble.shutdown()
    _stop_ble_measurement()


def _handle_shutdown_signal(signum, frame):
    raise SystemExit(0)


atexit.register(_shutdown_ble)
signal.signal(signal.SIGTERM, _handle_shutdown_signal)
signal.signal(signal.SIGINT, _handle_shutdown_signal)


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
    start_reader()
    app.run(host="127.0.0.1", port=PORT, threaded=True)
