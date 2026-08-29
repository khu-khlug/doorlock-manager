from flask import Flask, jsonify, request, make_response
from gpiozero import OutputDevice
import argparse
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

# 블루투스 프로토콜 설정.
# UUID 값과 payload 규격은 **앱(parksiwoo2/doorlock-frontend) 원격 main의 BleConstants.kt /
# BlePayloadCodec.kt가 기준**이다. 앱을 우리에 맞추는 게 아니라 우리가 앱에 맞춘다.
BLE_UUID_PRESENCE = 0x0312       # Pi -> 폰: 트리거 (앱의 TARGET_UUID)
BLE_UUID_REGISTER = 0x1111       # 폰 -> Pi: 등록 요청 / 하트비트 (앱의 RESPONSE_UUID)
BLE_UUID_CONFIRM = 0x2222        # Pi -> 폰: 등록 확인, 학번+세션토큰 (앱의 OPEN_UUID)
BLE_UUID_HEARTBEAT_ACK = 0x3333  # Pi -> 폰: 재실 명단 (앱의 HEARTBEAT_UUID)

BLE_STUDENT_ID_LENGTH = 10                 # 학번 ASCII 자릿수 (디코딩 후 길이)
BLE_ENCODED_STUDENT_ID_LENGTH = 20         # 앱이 학번을 2바이트씩 뒤바꾼 뒤 16진수로 인코딩한 길이
BLE_REGISTRATION_PAYLOAD_LENGTH = BLE_ENCODED_STUDENT_ID_LENGTH + 1  # 인코딩 학번 + 공개여부(1바이트)
BLE_HEARTBEAT_PAYLOAD_LENGTH = 2           # 세션토큰(1바이트) + 공개여부(1바이트)
BLE_TRIGGER_WINDOW_SECONDS = 5      # 등록 요청을 새 후보로 받아들이는 수집 윈도우 길이
BLE_HEARTBEAT_EXPIRY_SECONDS = 30   # 하트비트가 끊겨 등록 목록에서 제거되는 기준
BLE_REAP_INTERVAL_SECONDS = 5       # 만료된 등록 정리 주기
BLE_TRIGGER_POLL_INTERVAL_MS = 500  # check_trigger() 폴링 주기

# 세션토큰(랜덤ID) 범위. **0은 앱이 명단의 빈 슬롯 표시용으로 예약**했으므로 발급하면 안 된다
# (앱 BlePayloadCodec.tokenByte의 require(sessionToken in 1..255)).
BLE_RANDOM_ID_MIN = 1
BLE_RANDOM_ID_MAX = 255

# 재실 명단(HEARTBEAT_ACK) payload는 **정확히 24바이트 고정**이다. 앱의
# matchesHeartbeatRoster가 payload.size != 24면 무조건 거부하므로 반드시 지켜야 한다.
# 등록된 토큰을 앞에서부터 채우고 남는 뒤쪽은 0으로 패딩한다.
BLE_ROSTER_LENGTH = 24
# 등록 인원이 한 패킷에 다 안 들어갈 때만 다음 묶음으로 넘어가는 주기.
# 정상 규모(24명 이하)에서는 묶음이 하나뿐이라 명단 내용이 바뀌지 않는다.
BLE_ROSTER_CHUNK_INTERVAL_SECONDS = 2

# 광고 1건의 ServiceData 상한. legacy 광고 31바이트에서 ServiceData AD 헤더
# 4바이트(길이+타입+16비트 UUID)를 빼고, BlueZ가 Flags AD(3바이트)를 붙일 여지까지 감안한 값.
BLE_MAX_SERVICE_DATA_BYTES = 24

# 앱은 트리거의 UUID 존재 여부만 보고 payload 내용은 안 본다. 다만 완전히 빈 바이트는
# dbus-python이 D-Bus 타입 시그니처를 못 정해 "Failed to parse advertisement"가
# 나는 것으로 실기에서 확인돼, 더미 1바이트를 넣는다.
BLE_PRESENCE_PAYLOAD = b"\x00"

# 광고 인스턴스별 송출 제어. 확장 광고 미지원 하드웨어라 커널이 등록된 인스턴스들을
# round-robin으로 교대 송출하며, Duration이 각 인스턴스의 1회 airtime이다
# (org.bluez.LEAdvertisement.rst의 Duration = "Rotation duration"). Duration 변경 자체는
# 거의 공짜다(비확장 광고 컨트롤러에서는 커널이 구조체 값만 갱신하고 HCI 왕복도 없다 —
# net/bluetooth/hci_core.c의 hci_add_adv_instance 참고). 진짜 병목은 "그 채널의 로테이션
# 순번이 돌아오기까지의 대기 시간"뿐이라, 평소엔 세 채널을 균등하게 짧은 주기로 돌려
# 전체 로테이션을 최대한 짧게 유지한다.
BLE_TRIGGER_DURATION_SECONDS = 1   # 트리거/명단/확인 균등 1초씩 -> 평소 로테이션 주기 3초
BLE_ROSTER_DURATION_SECONDS = 1
BLE_CONFIRM_DURATION_SECONDS = 1   # 확인의 평소(비활성) Duration
BLE_CONFIRM_ACTIVE_DURATION_SECONDS = 5  # 인증 성공 시 이 값으로 잠깐 키운다 (아래 참고)
BLE_CONFIRM_CONTENT_SECONDS = 5    # 인증 성공 후 실제 내용을 보여주는 시간(우리 타이머, BlueZ Timeout 아님).
# 이 두 값을 같게 잡은 이유: 활성 상태의 로테이션 주기는 1+1+5=7초이고, 확인 채널
# 자신의 airtime은 그 7초 중 5초를 통째로 차지하는 한 덩어리라 폰이 놓칠 일이 사실상
# 없다. 순번을 막 놓친 최악의 경우에도 트리거+명단(최대 2초)만 기다리면 확인 채널
# 차례가 오므로, 앱의 10초 하드 타임아웃(BleRelayService.kt의 openConfirmationTimeoutMillis)
# 대비 여유가 충분하다.
# 광고 반복 간격. 짧게 잡을수록 폰이 짧은 스캔 창 안에서도 광고를 받을 확률이 높아진다.
BLE_ADV_MIN_INTERVAL_MS = 100
BLE_ADV_MAX_INTERVAL_MS = 200
# bluetoothd를 능동 재시작한 뒤 어댑터가 D-Bus에 다시 나타날 때까지 기다리는 시간.
# 상태를 반복 확인하는 폴링이 아니라, 재시작 직후 한 번만 쉬는 고정 지연이다.
BLE_SERVICE_RESTART_SETTLE_SECONDS = 3

# BlueZ D-Bus 이름들. bluezero를 걷어냈으므로 직접 정의한다.
BLUEZ_SERVICE = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
LE_ADVERTISEMENT_IFACE = "org.bluez.LEAdvertisement1"
LE_ADVERTISING_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
# 어댑터 경로는 자동 탐색하며, BLE_ADAPTER 환경변수로 재정의할 수 있다.
BLE_ADAPTER_PATH_OVERRIDE = os.environ.get("BLE_ADAPTER")
# 광고 인스턴스 3개의 D-Bus 오브젝트 경로.
BLE_AD_PATH_TRIGGER = "/org/khlug/doorlock/advertisement0"
BLE_AD_PATH_ROSTER = "/org/khlug/doorlock/advertisement1"
BLE_AD_PATH_CONFIRM = "/org/khlug/doorlock/advertisement2"

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


# 블루투스 인증 상태 (모듈 전역). 트리거 폴링/하트비트 갱신/만료 정리가
# 각자 별도 threading.Timer 스레드에서 돌기 때문에 락으로 보호한다.
_ble_lock = threading.Lock()
_ble_mode = "idle"  # "idle" | "triggered"
_registered_devices = {}  # 학번 -> {"random_id": int, "last_heartbeat_at": float, "visible": bool}
_candidates = {}          # triggered 중 수집: 학번 -> {"student_id", "rssi", "seen_at", "visible"}
_roster_chunk_index = 0  # 등록 기기가 한 패킷에 안 들어갈 때만 쓰는 순환 인덱스
_random_id_counter = BLE_RANDOM_ID_MIN  # 다음에 발급할 세션토큰. 항상 비어있는 값을 가리킨다.

# BLE 광고. 송출 신호마다 요구되는 주기가 달라서, 하나의 광고 내용을 갈아끼우는 대신
# **인스턴스 3개를 따로 등록**하고 Duration/Timeout으로 시점과 지속을 제어한다.
# 이 하드웨어는 확장 광고 미지원이 확인됐으므로 커널이 이들을 round-robin으로 교대
# 송출한다(동시 송출이 아니다 — mgmt-api.txt의 Add Advertising 설명 참고).
_trigger_ad = None   # 0312, 기동 시 등록 후 상시 유지
_roster_ad = None    # 3333, 기동 시 등록 후 상시 유지. 등록 목록이 바뀔 때 payload만 갱신
_confirm_ad = None   # 2222, 이것도 기동 시 등록 후 상시 유지. 평소엔 비활성값을 내보내다가
                      # 인증 성공 시에만 실제 내용 + Duration을 잠깐 키운다(런타임
                      # register/unregister는 이번 설계에서 금지 — _show_confirm_content 참고)
_ad_manager_methods = None  # org.bluez.LEAdvertisingManager1 인터페이스 (등록/해제 전용)


def _uuid16_to_str(uuid16: int) -> str:
    """16비트 UUID를 BlueZ가 쓰는 128비트 표준 UUID 문자열로 확장한다."""
    return f"0000{uuid16:04x}-0000-1000-8000-00805f9b34fb"


def _decode_student_id(encoded: bytes) -> Optional[str]:
    """앱의 BlePayloadCodec.encodeStudentId() 인코딩을 복원한다.
    학번 10자리 ASCII를 2바이트씩 맞바꾼 뒤 16진수 20글자로 표현한 것을 원래 학번으로 되돌린다."""
    if len(encoded) != BLE_ENCODED_STUDENT_ID_LENGTH:
        return None
    try:
        swapped = bytes.fromhex(encoded.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return None
    if len(swapped) != BLE_STUDENT_ID_LENGTH:
        return None
    unswapped = bytearray(swapped)
    for i in range(0, BLE_STUDENT_ID_LENGTH, 2):
        unswapped[i], unswapped[i + 1] = unswapped[i + 1], unswapped[i]
    try:
        student_id = bytes(unswapped).decode("ascii")
    except UnicodeDecodeError:
        return None
    if len(student_id) != BLE_STUDENT_ID_LENGTH or not student_id.isdigit():
        return None
    return student_id


def _encode_student_id(student_id: str) -> bytes:
    """_decode_student_id()의 역변환. Pi가 CONFIRM(3333)에 학번을 실을 때 앱과 같은 인코딩으로 맞춘다."""
    swapped = bytearray(student_id.encode("ascii"))
    for i in range(0, BLE_STUDENT_ID_LENGTH, 2):
        swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
    return bytes(swapped).hex().upper().encode("ascii")


def _extract_register_payload(service_data: dict) -> Optional[bytes]:
    """ServiceData에서 등록 UUID(BLE_UUID_REGISTER)의 원본 payload를 그대로 꺼낸다."""
    raw = service_data.get(_uuid16_to_str(BLE_UUID_REGISTER))
    if raw is None:
        return None
    return bytes(raw)


def _parse_registration_payload(payload: bytes) -> Optional[tuple]:
    """등록 신호(triggered 중, 인코딩 학번 20바이트 + 공개여부 1바이트 = 21바이트)를 파싱한다."""
    if len(payload) != BLE_REGISTRATION_PAYLOAD_LENGTH:
        return None
    student_id = _decode_student_id(payload[:BLE_ENCODED_STUDENT_ID_LENGTH])
    if student_id is None:
        return None
    visible = payload[BLE_ENCODED_STUDENT_ID_LENGTH] != 0
    return student_id, visible


def _parse_heartbeat_payload(payload: bytes) -> Optional[tuple]:
    """하트비트 신호(idle 중, 랜덤ID 1바이트 + 공개여부 1바이트 = 2바이트)를 파싱한다."""
    if len(payload) != BLE_HEARTBEAT_PAYLOAD_LENGTH:
        return None
    return payload[0], payload[1] != 0


def _next_random_id(value: int) -> int:
    """세션토큰 순환. BLE_RANDOM_ID_MIN..BLE_RANDOM_ID_MAX 안에서만 돈다."""
    span = BLE_RANDOM_ID_MAX - BLE_RANDOM_ID_MIN + 1
    return BLE_RANDOM_ID_MIN + ((value - BLE_RANDOM_ID_MIN + 1) % span)


def _allocate_random_id() -> int:
    """등록 기기에 부여할 세션토큰(1바이트)을 순차 발급한다.

    **0은 절대 발급하지 않는다** — 앱이 재실 명단의 빈 슬롯 표시용으로 예약한 값이라
    (BlePayloadCodec.tokenByte의 require(sessionToken in 1..255)), 0을 주면 그 기기는
    명단에서 자기 토큰을 영영 못 찾는다.

    카운터가 항상 다음에 내줄 빈 값을 가리키고 있다가, 발급 시 그 값을 반환하고
    카운터를 그 다음 빈 값으로 옮겨둔다.
    """
    global _random_id_counter
    if _random_id_counter < BLE_RANDOM_ID_MIN:
        _random_id_counter = BLE_RANDOM_ID_MIN
    issued = _random_id_counter
    used = {info["random_id"] for info in _registered_devices.values()}
    used.add(issued)
    next_id = _next_random_id(issued)
    while next_id in used and next_id != issued:
        next_id = _next_random_id(next_id)
    _random_id_counter = next_id
    return issued


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
            self._uuid_str = _uuid16_to_str(uuid16)
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
            self._props["MinInterval"] = dbus.UInt32(BLE_ADV_MIN_INTERVAL_MS)
            self._props["MaxInterval"] = dbus.UInt32(BLE_ADV_MAX_INTERVAL_MS)
            if duration is not None:
                self._props["Duration"] = dbus.UInt16(duration)
            if timeout is not None:
                self._props["Timeout"] = dbus.UInt16(timeout)

        def set_payload(self, payload) -> bool:
            """이 인스턴스의 ServiceData를 교체한다. 내용이 같으면 아무 것도 하지 않는다."""
            payload = bytes(payload)
            if len(payload) > BLE_MAX_SERVICE_DATA_BYTES:
                logger.error(
                    "ble: %s payload too long len=%d (max %d)",
                    self.label, len(payload), BLE_MAX_SERVICE_DATA_BYTES,
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
                LE_ADVERTISEMENT_IFACE, changed, dbus.Array([], signature="s")
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
                LE_ADVERTISEMENT_IFACE, changed, dbus.Array([], signature="s")
            )
            return True

        @dbus.service.signal(dbus.PROPERTIES_IFACE, signature="sa{sv}as")
        def PropertiesChanged(self, interface, changed, invalidated):
            """bluetoothd가 이 신호를 받아 광고 데이터를 갱신한다. 데코레이터가 신호
            발신을 담당하므로 본문은 비어 있고, 호출하는 것 자체가 발신이다."""

        @dbus.service.method(dbus.PROPERTIES_IFACE,
                             in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):
            if interface != LE_ADVERTISEMENT_IFACE:
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

        @dbus.service.method(LE_ADVERTISEMENT_IFACE,
                             in_signature="", out_signature="")
        def Release(self):
            """bluetoothd가 광고를 회수했을 때 호출된다(확인 광고의 Timeout 만료 등).
            이 시점엔 이미 해제된 뒤라 UnregisterAdvertisement를 부르면 안 된다."""
            logger.info("ble %s advertisement released by bluetoothd", self.label)
            if self._on_released is not None:
                self._on_released()

    return DoorLockAdvertisement


def roster_payload(random_ids: list, chunk_index: int = 0) -> bytes:
    """재실 명단(HEARTBEAT_ACK) payload를 만든다.

    **항상 정확히 BLE_ROSTER_LENGTH 바이트**여야 한다 — 앱의 matchesHeartbeatRoster가
    payload.size != 24면 무조건 거부한다. 등록된 세션토큰을 앞에서부터 채우고 남는
    뒤쪽은 0으로 패딩한다(0은 앱이 빈 슬롯으로 해석하는 예약값).

    인원이 한 패킷에 다 안 들어가면 묶음으로 잘라 chunk_index로 순환한다.
    """
    ordered = sorted(random_ids)
    size = BLE_ROSTER_LENGTH
    if len(ordered) > size:
        chunk_count = (len(ordered) + size - 1) // size
        start = (chunk_index % chunk_count) * size
        ordered = ordered[start:start + size]
    return bytes(ordered) + bytes(size - len(ordered))


def _init_advertisements(bus) -> None:
    """광고 인스턴스 3개를 만든다. D-Bus 왕복(등록)은 하지 않는다."""
    global _trigger_ad, _roster_ad, _confirm_ad

    advertisement_cls = _advertisement_class()
    # 트리거: 상시 송출. 앱이 setServiceUuid로 거르므로 UUID 목록을 함께 싣는다.
    # Duration을 길게 잡아 다른 인스턴스로 넘어가는 공백을 짧게 유지한다.
    _trigger_ad = advertisement_cls(
        bus, BLE_AD_PATH_TRIGGER, BLE_UUID_PRESENCE, "trigger",
        include_service_uuids=True, duration=BLE_TRIGGER_DURATION_SECONDS,
    )
    _trigger_ad.set_payload(BLE_PRESENCE_PAYLOAD)
    # 명단: 상시 송출. 24바이트라 UUID 목록까지 넣으면 31바이트 한도를 넘으므로 ServiceData만.
    _roster_ad = advertisement_cls(
        bus, BLE_AD_PATH_ROSTER, BLE_UUID_HEARTBEAT_ACK, "roster",
        duration=BLE_ROSTER_DURATION_SECONDS,
    )
    _roster_ad.set_payload(roster_payload([]))
    # 확인: 이것도 기동 시 상시 등록한다 — 인증 성공 시에만 register/unregister하는
    # 방식은 우리가 이번 재설계에서 금지한 바로 그 패턴이다(런타임 등록/해제가
    # AlreadyExists 영구 교착을 만든 전례가 있다). 대신 평소엔 비활성값(전부 0)을
    # 싣고, 인증 성공 시에만 잠깐 실제 값으로 갈아끼운다 — 인코딩 학번은 항상
    # ASCII 16진수 문자('0'-'9','A'-'F')라 전부 0인 바이트는 어떤 실제 학번과도
    # 절대 일치하지 않으므로, 이 값이 곧 "아무 신호도 없음"과 동등하다.
    _confirm_ad = advertisement_cls(
        bus, BLE_AD_PATH_CONFIRM, BLE_UUID_CONFIRM, "confirm",
        duration=BLE_CONFIRM_DURATION_SECONDS,
    )
    _confirm_ad.set_payload(bytes(BLE_REGISTRATION_PAYLOAD_LENGTH))


def _register_advertisement(ad, on_error=None) -> None:
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

    _ad_manager_methods.RegisterAdvertisement(
        ad.path,
        dbus.Dictionary({}, signature="sv"),
        reply_handler=_on_ok,
        error_handler=_on_error,
    )


def _unregister_advertisement(ad) -> None:
    """광고를 해제한다. 등록돼 있지 않으면 조용히 넘어간다.

    블로킹으로 불러도 안전하다 — RegisterAdvertisement와 달리 bluetoothd가 우리 쪽으로
    되돌아 호출할 게 없다(Release()는 문서상 noreply). 종료가 매달리지 않게 상한만 둔다.
    """
    if ad is None or _ad_manager_methods is None:
        return
    try:
        _ad_manager_methods.UnregisterAdvertisement(ad.path, timeout=5)
        logger.info("ble %s advertisement unregistered", ad.label)
    except Exception as error:
        # 애초에 등록돼 있지 않으면 DoesNotExist가 나는데 정상 경로다.
        logger.debug("ble: unregister %s skipped (%s)", ad.label, error)


def _register_persistent_advertisements() -> None:
    """세 인스턴스(트리거·명단·확인)를 전부 등록한다. 프로세스당 한 번만 호출한다.

    확인 인스턴스도 여기서 함께 등록한다 — 인증 이벤트가 날 때만 register/unregister를
    반복하는 방식은 쓰지 않는다(런타임 등록/해제가 AlreadyExists 영구 교착을 만든 전례가
    있다). 세 인스턴스 모두 기동 시 한 번 등록해두고, 이후엔 내용만 갈아끼운다.

    등록 전에 같은 경로를 먼저 해제해둔다. BlueZ는 소유자(D-Bus 발신자 이름)가 다르면
    남의 광고를 건드리지 못하게 막으므로(advertising.c:1727) 이전 프로세스가 남긴 것은
    어차피 우리가 못 지우지만, 우리 프로세스 안에서 상태가 꼬였을 때를 대비한 방어다.
    """
    for ad in (_trigger_ad, _roster_ad, _confirm_ad):
        _unregister_advertisement(ad)
        _register_advertisement(ad)


def _show_confirm_content(student_id: str, random_id: int) -> None:
    """등록 확인(2222) 내용을 BLE_CONFIRM_CONTENT_SECONDS 동안만 실제 값으로 보여준다.

    확인 인스턴스는 기동 시부터 항상 등록돼 있다 — 여기서 하는 일은 register가 아니라
    set_payload/set_duration 뿐이다. Duration도 이 동안만 BLE_CONFIRM_ACTIVE_DURATION_SECONDS로
    키워서, 로테이션 순번이 왔을 때 확인 채널이 훨씬 오래(그리고 훨씬 확실하게) 잡히게 한다.
    시간이 지나면 _hide_confirm_content가 둘 다 평소 값으로 되돌린다."""
    _confirm_ad.set_payload(_encode_student_id(student_id) + bytes([random_id]))
    _confirm_ad.set_duration(BLE_CONFIRM_ACTIVE_DURATION_SECONDS)
    t = threading.Timer(BLE_CONFIRM_CONTENT_SECONDS, _hide_confirm_content)
    t.daemon = True
    t.start()


def _hide_confirm_content() -> None:
    """확인 내용과 Duration을 평소값(비활성 payload, 1초)으로 되돌린다."""
    if _confirm_ad is not None:
        _confirm_ad.set_payload(bytes(BLE_REGISTRATION_PAYLOAD_LENGTH))
        _confirm_ad.set_duration(BLE_CONFIRM_DURATION_SECONDS)


def _refresh_roster_advertisement() -> None:
    """등록 목록이 바뀌었을 때 명단 광고 payload를 갱신한다."""
    if _roster_ad is None:
        return
    with _ble_lock:
        random_ids = [info["random_id"] for info in _registered_devices.values()]
        chunk_index = _roster_chunk_index
    _roster_ad.set_payload(roster_payload(random_ids, chunk_index))


def _log_roster_table() -> None:
    """재실 인원 테이블(학번 -> 세션토큰)이 바뀔 때마다 전체 스냅샷을 남긴다."""
    with _ble_lock:
        table = {sid: info["random_id"] for sid, info in _registered_devices.items()}
    logger.info("ble roster table=%s", table)


def _trigger_none() -> bool:
    """기본 트리거 전략: 실제 트리거 신호(버튼/모션센서 등)가 아직 정해지지 않아 항상 False."""
    return False


def _trigger_test() -> bool:
    """테스트용 트리거 전략: 항상 True를 반환해 트리거 윈도우가 계속 반복되게 한다."""
    return True


TRIGGER_STRATEGIES = {
    "none": _trigger_none,
    "test": _trigger_test,
}

_trigger_strategy = _trigger_none  # --trigger CLI 인자로 __main__에서 교체된다.


def check_trigger() -> bool:
    """TRIGGERED 진입 조건을 판단한다. 실제 판단은 --trigger로 주입된 전략에 위임한다."""
    return _trigger_strategy()


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


def _handle_ble_register_signal(payload: bytes, rssi: int) -> None:
    """폰이 보낸 REGISTER 신호 1건을 처리한다.

    폰은 UUID 하나(REGISTER)로 두 가지를 보내고 **payload 길이로 구분**된다:
    2바이트(세션토큰+공개여부) = 하트비트, 21바이트(인코딩 학번+공개여부) = 등록 요청.

    하트비트는 길이로 이미 명확히 구분되므로 **수집 윈도우 상태와 무관하게 항상 처리한다.**
    수집 중(triggered)이라고 하트비트를 건너뛰면, 트리거가 상시 발생하는 운용에서는 거의 항상
    수집 중이라 등록 기기의 하트비트가 갱신되지 않는다. 그러면 30초 뒤 만료되고 폰도 세션을
    끊어 재등록하면서 **이미 안에 있는 사람에게 문이 다시 열린다.**
    """
    heartbeat = _parse_heartbeat_payload(payload)
    if heartbeat is not None:
        random_id, visible = heartbeat
        _touch_registered_device(random_id, visible)
        return

    registration = _parse_registration_payload(payload)
    if registration is None:
        # 우리 규격이 아닌 길이. 주변 기기가 같은 UUID를 쓰는 경우일 수 있어 조용히 무시한다.
        logger.debug("ble register signal ignored len=%d rssi=%d", len(payload), rssi)
        return

    student_id, visible = registration
    with _ble_lock:
        collecting = _ble_mode == "triggered"
        already_registered = student_id in _registered_devices
    if not collecting:
        return
    # 이미 등록된 기기가 (하트비트를 못 받아) 등록 요청을 다시 보내는 경우가 있는데,
    # 새 후보로 받아들이면 재인증·재개방이 일어나므로 제외한다.
    if already_registered:
        return

    if student_id not in _candidates:
        # 폰은 수집 윈도우 동안 같은 신호를 초당 여러 번 보낸다. 매번 찍으면 24시간
        # 운용에서 로그가 폭주하고 SD 카드 수명에도 해로우므로 처음 한 번만 남긴다.
        logger.info("ble candidate added student_id=%s rssi=%d visible=%s", student_id, rssi, visible)
    _candidates[student_id] = {
        "student_id": student_id,
        "rssi": rssi,
        "seen_at": time.monotonic(),
        "visible": visible,
    }


def _touch_registered_device(random_id: int, visible: bool) -> None:
    """하트비트를 받은 등록 기기의 마지막 수신 시각과 공개 여부를 갱신한다."""
    with _ble_lock:
        for info in _registered_devices.values():
            if info["random_id"] == random_id:
                info["last_heartbeat_at"] = time.monotonic()
                info["visible"] = visible
                return


def select_closest_candidate(candidates: list) -> Optional[dict]:
    """가장 가까운(RSSI가 가장 강한) 후보 하나를 고른다.

    TODO: 정확한 판별 알고리즘은 아직 미정 — 현재는 RSSI 최댓값으로 임시 구현.
    """
    if not candidates:
        return None
    return max(candidates, key=lambda c: c["rssi"])


def _confirm_candidate(candidate: dict) -> None:
    """선택된 후보 1명의 학번으로 백엔드에 인증을 시도한다 (성공하면 attempt_unlock 내부에서 문이 열린다).
    인증에 성공한 경우에만 세션토큰을 발급해 등록하고 확인 광고를 켠다.
    실패하면 등록하지 않고 수집 윈도우만 닫는다."""
    student_id = candidate["student_id"]
    result = attempt_unlock(
        "/internal/door-lock/accesses",
        {"number": int(student_id), "roomNumber": ROOM_NUMBER},
        source="bluetooth",
    )
    if result.kind != "ok":
        _close_collect_window()
        return

    random_id = _allocate_random_id()
    visible = candidate.get("visible", True)
    with _ble_lock:
        _registered_devices[student_id] = {
            "random_id": random_id,
            "last_heartbeat_at": time.monotonic(),
            "visible": visible,
        }

    logger.info("ble registered student_id=%s token=%d visible=%s", student_id, random_id, visible)
    # 명단에 새 토큰을 반영하고, 확인 채널 내용을 잠깐 실제 값으로 보여준다.
    # (확인 인스턴스는 기동 시부터 항상 등록돼 있으므로 여기서 register하지 않는다.)
    _refresh_roster_advertisement()
    _log_roster_table()
    _show_confirm_content(student_id, random_id)
    _close_collect_window()


def _close_collect_window() -> None:
    """수집 윈도우를 닫는다(-> idle). 광고는 건드리지 않는다 — 트리거와 명단 인스턴스는
    상시 등록돼 있고, 확인 광고는 Timeout이 알아서 회수한다."""
    global _ble_mode
    with _ble_lock:
        _ble_mode = "idle"
        _candidates.clear()
    logger.debug("ble collect window closed")


@_log_callback_exceptions(None)
def _end_collect_window() -> None:
    """수집 윈도우 종료. 후보가 있으면 가장 가까운 하나를 인증하고, 없으면 그냥 닫는다."""
    candidate = select_closest_candidate(list(_candidates.values()))
    if candidate is not None:
        _confirm_candidate(candidate)
    else:
        _close_collect_window()


def _open_collect_window() -> None:
    """idle -> 수집. 후보 목록을 비우고 수집 윈도우 타이머를 예약한다.
    트리거 광고는 상시 나가고 있으므로 여기서 광고를 건드릴 게 없다."""
    global _ble_mode
    with _ble_lock:
        _ble_mode = "triggered"
        _candidates.clear()
    logger.debug("ble collect window opened")
    t = threading.Timer(BLE_TRIGGER_WINDOW_SECONDS, _end_collect_window)
    t.daemon = True
    t.start()


def _trigger_poll_loop() -> None:
    """check_trigger()를 주기적으로 확인해 수집 윈도우를 연다.
    threading.Timer로 스스로 재예약한다 — 이 환경에서 GLib.timeout_add는
    최초 1회 이후 반복 실행되지 않는 것으로 실기에서 확인됐다 (README 참고)."""
    try:
        with _ble_lock:
            mode = _ble_mode
        if mode == "idle" and check_trigger():
            _open_collect_window()
    except Exception:
        logger.exception("ble trigger poll error")
    t = threading.Timer(BLE_TRIGGER_POLL_INTERVAL_MS / 1000, _trigger_poll_loop)
    t.daemon = True
    t.start()


def _roster_chunk_loop() -> None:
    """등록 인원이 한 패킷에 안 들어갈 때만 다음 묶음으로 넘긴다.
    정상 규모(BLE_ROSTER_LENGTH 이하)에서는 묶음이 하나뿐이라 명단 내용이 바뀌지 않고,
    이 루프는 사실상 아무 일도 하지 않는다. threading.Timer로 스스로 재예약한다."""
    global _roster_chunk_index
    try:
        with _ble_lock:
            needs_chunking = len(_registered_devices) > BLE_ROSTER_LENGTH
            if needs_chunking:
                _roster_chunk_index += 1
            else:
                _roster_chunk_index = 0
        if needs_chunking:
            _refresh_roster_advertisement()
    except Exception:
        logger.exception("ble roster chunk error")
    t = threading.Timer(BLE_ROSTER_CHUNK_INTERVAL_SECONDS, _roster_chunk_loop)
    t.daemon = True
    t.start()


def _reap_loop() -> None:
    """30초 이상 하트비트가 없는 등록 기기를 목록에서 제거한다.
    threading.Timer로 스스로 재예약한다."""
    try:
        _reap_expired_registrations_once()
    except Exception:
        logger.exception("ble reap error")
    t = threading.Timer(BLE_REAP_INTERVAL_SECONDS, _reap_loop)
    t.daemon = True
    t.start()


def _reap_expired_registrations_once() -> None:
    """30초 이상 하트비트가 없는 등록 기기를 목록에서 제거한다.
    목록이 바뀌었으면 4444 광고 내용도 그에 맞춰 갱신한다."""
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
    if expired:
        _refresh_roster_advertisement()
        _log_roster_table()


@_log_callback_exceptions(None)
def _on_bluez_interfaces_added(path, interfaces) -> None:
    """새로 발견된 기기의 최초 광고를 처리한다.

    시그널 payload에 이미 들어 있는 ServiceData/RSSI를 그대로 읽는다. bluezero의
    adapter._interfaces_added는 이 payload를 버리고 주소로 Device 오브젝트를 다시
    조회하는데, 폰들이 랜덤 MAC을 빠르게 바꾸는 환경에선 조회 전에 기기가 사라져
    "ValueError: Cannot find a device"가 끊임없이 터진다."""
    properties = interfaces.get(DEVICE_IFACE)
    if not properties:
        return
    _handle_ble_device_properties(properties)


@_log_callback_exceptions(None)
def _on_bluez_properties_changed(interface, changed, invalidated, path) -> None:
    """이미 알려진 기기의 반복 광고(하트비트 등)를 처리한다.
    BlueZ는 같은 주소의 기기가 다시 광고하면 InterfacesAdded가 아니라
    PropertiesChanged를 보내므로 둘 다 구독해야 한다."""
    _handle_ble_device_properties(changed)


def _handle_ble_device_properties(properties) -> None:
    """org.bluez.Device1 속성 묶음에서 우리 등록 UUID(2222) 신호만 추려 처리한다."""
    service_data = properties.get("ServiceData")
    if service_data is None:
        return
    payload = _extract_register_payload(service_data)
    if payload is not None:
        _handle_ble_register_signal(payload, int(properties.get("RSSI", 0)))


def _start_continuous_ble_discovery(adapter_methods, dbus_module) -> None:
    """중복 억제 없이 계속 스캔한다.

    NOTE: `UUIDs` 필터는 광고의 "Service UUID 목록" AD 구조체만 검사하고
    "Service Data" AD 구조체는 보지 않는다. 앱은 학번을 Service Data로만
    싣고 별도 Service UUID 목록을 붙이지 않으므로, UUIDs 필터를 걸면
    BlueZ가 이 기기를 아예 걸러버려 우리 콜백에 전달되지 않는다
    (bluetoothctl scan on은 필터 없이 전역 캐시를 채워서 잡히는 것처럼 보였을 뿐).
    그래서 UUID 필터 없이 전체 LE 기기를 받아 _extract_register_payload에서
    ServiceData 내용으로 직접 걸러낸다.
    """
    adapter_methods.SetDiscoveryFilter(
        {
            "Transport": dbus_module.String("le"),
            # DuplicateData는 반복 광고(하트비트)를 계속 받기 위해 필요하고,
            # 부수적으로 BlueZ의 RSSI 변화량 임계치 필터링도 비활성화한다.
            "DuplicateData": dbus_module.Boolean(True),
        }
    )
    adapter_methods.StartDiscovery()


def _find_adapter_path(bus) -> str:
    """org.bluez.Adapter1을 구현한 오브젝트를 찾아 그 경로를 돌려준다.

    경로를 하드코딩하면 USB 동글을 꽂아 어댑터가 늘거나 번호가 바뀌었을 때 엉뚱한(또는
    고장난) 어댑터를 계속 잡는다. BLE_ADAPTER 환경변수로 강제 지정할 수 있다.
    """
    import dbus

    if BLE_ADAPTER_PATH_OVERRIDE:
        return BLE_ADAPTER_PATH_OVERRIDE
    manager = dbus.Interface(bus.get_object(BLUEZ_SERVICE, "/"), OBJECT_MANAGER_IFACE)
    for path, interfaces in manager.GetManagedObjects().items():
        if ADAPTER_IFACE in interfaces:
            return str(path)
    raise RuntimeError("no org.bluez.Adapter1 found (is the controller present and powered?)")


def _log_advertising_capacity(bus, adapter_path: str) -> None:
    """기동 시 광고 인스턴스 사용 현황을 남긴다.

    다른 프로세스가 등록한 광고는 BlueZ가 소유자 기준으로 막아 우리가 지울 수 없으므로
    (advertising.c:1727), 최소한 눈에 보이게 로그로 남겨 진단에 쓴다.
    """
    import dbus

    try:
        props = dbus.Interface(
            bus.get_object(BLUEZ_SERVICE, adapter_path), dbus.PROPERTIES_IFACE
        )
        active = int(props.Get(LE_ADVERTISING_MANAGER_IFACE, "ActiveInstances"))
        free = int(props.Get(LE_ADVERTISING_MANAGER_IFACE, "SupportedInstances"))
        logger.info("ble advertising instances active=%d free=%d", active, free)
        if active:
            logger.warning(
                "ble: %d advertising instance(s) already active before our registration", active
            )
    except Exception:
        logger.debug("ble: could not read advertising instance counts", exc_info=True)


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


def _prepare_bluetooth_adapter() -> None:
    """블루투스 어댑터를 능동적으로 살려서 항상 깨끗한 상태에서 시작한다.

    실기에서 확인된 실패( Powered 설정이 org.bluez.Error.Failed로 거부, 이어서
    SetDiscoveryFilter가 NotReady로 실패 )는 처음엔 bluetoothd 내부 mgmt 연결이
    깨진 것으로 의심했지만(BlueZ 5.82 src/adapter.c의 property_set_mode() — mgmt_send()가
    큐잉에 실패하면 커널 응답도 기다리지 않고 곧장 ERROR_INTERFACE ".Failed"를 반환하는
    분기), bluetooth.service를 재시작해도 재현돼 그 가설은 반증됐다.

    실제 원인은 **rfkill 소프트 블록**이었다 — `rfkill list`에서 hci0이
    `Soft blocked: yes`, `hciconfig -a`에서 `hci0`이 `DOWN`으로 확인됐다. rfkill이
    막고 있으면 bluetoothd가 몇 번을 재시작해도 그 아래 인터페이스를 못 올리므로,
    Powered를 시도하기 전에 반드시 rfkill부터 풀어야 한다.

    블루투스는 이 데몬 전용이라 재시작·언블록해도 방해받는 다른 프로세스가 없다.
    그래서 "상태를 확인하고 필요하면 조치"가 아니라 **매 기동마다 무조건** rfkill을
    풀고 bluetoothd를 재시작해서 항상 같은 known-good 상태에서 시작한다 — 확인 로직을
    늘리는 대신 환경을 우리가 직접 통제한다.
    """
    if _run_privileged(["sudo", "rfkill", "unblock", "bluetooth"], "rfkill unblock bluetooth"):
        logger.info("ble: rfkill unblocked")

    if _run_privileged(["sudo", "systemctl", "restart", "bluetooth"], "bluetooth.service restart"):
        logger.info("ble: bluetooth.service restarted")

    time.sleep(BLE_SERVICE_RESTART_SETTLE_SECONDS)


def _ble_main() -> None:
    """BLE 전용 스레드 진입점. D-Bus GLib 메인루프를 설정하고 계속 실행한다.

    광고/스캔 모두 bluezero 없이 BlueZ D-Bus API를 직접 쓴다 (bluezero를 걷어낸
    이유는 DoorLockAdvertisement와 _on_bluez_interfaces_added의 주석 참고).
    """
    global _ad_manager_methods

    _prepare_bluetooth_adapter()

    import dbus
    import dbus.mainloop.glib
    from gi.repository import GLib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    bus.add_signal_receiver(
        _on_bluez_interfaces_added,
        dbus_interface=OBJECT_MANAGER_IFACE,
        signal_name="InterfacesAdded",
    )
    bus.add_signal_receiver(
        _on_bluez_properties_changed,
        dbus_interface="org.freedesktop.DBus.Properties",
        signal_name="PropertiesChanged",
        arg0=DEVICE_IFACE,
        path_keyword="path",
    )

    adapter_path = _find_adapter_path(bus)
    adapter_object = bus.get_object(BLUEZ_SERVICE, adapter_path)
    adapter_props = dbus.Interface(adapter_object, dbus.PROPERTIES_IFACE)
    try:
        address = str(adapter_props.Get(ADAPTER_IFACE, "Address"))
    except Exception:
        address = "?"
    logger.info("ble adapter path=%s address=%s", adapter_path, address)

    # 어댑터가 꺼져 있으면 광고 등록도 스캔도 실패한다. 다만 일부 USB 동글 펌웨어는
    # SET_POWERED를 지원하지 않는 사례가 보고돼 있으므로, 실패해도 BLE를 통째로
    # 죽이지 말고 경고만 남기고 진행한다(이미 켜져 있으면 문제가 없다).
    try:
        adapter_props.Set(ADAPTER_IFACE, "Powered", dbus.Boolean(True))
    except Exception as error:
        logger.warning("ble: could not set adapter Powered (continuing): %s", error)

    _log_advertising_capacity(bus, adapter_path)

    _ad_manager_methods = dbus.Interface(adapter_object, LE_ADVERTISING_MANAGER_IFACE)
    _init_advertisements(bus)

    _start_continuous_ble_discovery(dbus.Interface(adapter_object, ADAPTER_IFACE), dbus)
    logger.info("ble discovery started (continuous)")

    # 트리거 폴링/명단 묶음 순환/만료 정리는 GLib.timeout_add 대신 threading.Timer
    # 기반 자기재예약 루프로 돌린다 — 이 환경에서 GLib.timeout_add는 최초 1회 이후
    # 반복 실행되지 않는 것으로 실기에서 확인됐다 (README 참고).
    for loop_fn in (_trigger_poll_loop, _roster_chunk_loop, _reap_loop):
        t = threading.Timer(0.5, loop_fn)
        t.daemon = True
        t.start()

    # 등록은 비동기라 여기서는 메시지만 큐에 넣고 바로 리턴한다. 실제 D-Bus 왕복과
    # bluetoothd가 우리에게 되거는 Introspect/GetAll은 아래 메인루프가 처리한다.
    # (블로킹으로 부르면 서로 기다리다 NoReply — _register_advertisement 주석 참고.)
    _register_persistent_advertisements()

    GLib.MainLoop().run()


@_log_callback_exceptions(None)
def _ble_main_logged() -> None:
    """_ble_main()을 감싸 예외를 daemon.log에 남긴다. 이게 없으면 BLE 스레드가 죽어도
    stderr에만 트레이스백이 찍히고 로그 파일엔 아무 것도 안 남아, 겉보기엔 데몬이
    멀쩡한데 BLE만 조용히 멈춘 상태가 된다 (실기에서 그렇게 한참 헤맸다)."""
    _ble_main()


def start_reader() -> None:
    """BLE 스캔/광고 스레드를 기동한다. non-blocking으로 즉시 리턴한다."""
    t = threading.Thread(target=_ble_main_logged, daemon=True)
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


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--trigger",
        choices=list(TRIGGER_STRATEGIES),
        default="none",
        help="triggered 진입 조건 전략 (기본값: none, 테스트 시 test)",
    )
    return parser.parse_args()


def _shutdown_ble(*_args) -> None:
    """프로세스 종료 시 등록된 BLE 광고를 확실히 해제한다.

    SIGTERM은 이 모듈이 등록한 핸들러(_handle_shutdown_signal)가 SystemExit을
    던져서 atexit이 정상적으로 실행되지만, SIGKILL(kill -9)은 파이썬이 정리할
    틈도 없이 즉시 죽어서 이 함수가 아예 호출되지 않는다 — 그러면 bluetoothd의
    자동 정리(D-Bus 연결 끊김 감지)에만 의존하게 되는데, 실기에서 이게 항상
    즉시/확실히 되는 건 아닌 것으로 보였다(다음 프로세스가 뜰 때 "Already Exists"로
    계속 실패하는 원인 중 하나였다). 그래서 테스트 시 데몬을 죽일 땐 -9 없이
    pkill/일반 kill(SIGTERM)을 써야 이 정리 로직이 실행된다."""
    for ad in (_confirm_ad, _roster_ad, _trigger_ad):
        _unregister_advertisement(ad)


def _handle_shutdown_signal(signum, frame):
    raise SystemExit(0)


atexit.register(_shutdown_ble)
signal.signal(signal.SIGTERM, _handle_shutdown_signal)
signal.signal(signal.SIGINT, _handle_shutdown_signal)


if __name__ == "__main__":
    args = _parse_args()
    _trigger_strategy = TRIGGER_STRATEGIES[args.trigger]
    logger.info("trigger strategy=%s", args.trigger)

    fetched_at = _load_cache_from_file()
    elapsed = datetime.now(timezone.utc).timestamp() - fetched_at
    if elapsed >= SCHEDULE_REFRESH_INTERVAL:
        _refresh_schedules()
    else:
        t = threading.Timer(SCHEDULE_REFRESH_INTERVAL - elapsed, _refresh_schedules)
        t.daemon = True
        t.start()
        logger.info("schedules cache valid, next refresh in %.0fs", SCHEDULE_REFRESH_INTERVAL - elapsed)
    start_reader()
    app.run(host="127.0.0.1", port=PORT, threaded=True)
