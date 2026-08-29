# door-lock

동방 도어락에 설치되어야 할 스크립트들. 라즈베리 파이에서 실행되며 PWA → 로컬 데몬 → 백엔드 인증 → GPIO → 릴레이 → 도어락 구조로 동작.

---

## 사용법 (Pi에서 실행)

라즈베리 파이 이미저로 Raspberry Pi OS 64-bit Lite를 설치 후
curl을 통해 깃허브에서 셋업 스크립트를 다운로드하여 실행한다.

백엔드 서버에 등록된 `UserRole.SYSTEM` API 키를 `internal-api-key` 파일에 미리 작성한다.

```bash
echo "백엔드_SYSTEM_API_키값" > ./internal-api-key
```

그 다음 셋업 스크립트를 같은 폴더에서 실행한다. 실행 시 방 번호(숫자 3자리)를 대화식으로 입력한다.

```bash
curl -fsSL https://raw.githubusercontent.com/khu-khlug/doorlock-manager/main/setup-door-lock.sh -o setup-door-lock.sh
chmod +x setup-door-lock.sh
sudo ./setup-door-lock.sh
```

> `internal-api-key`가 없으면 셋업 스크립트가 새 키를 자동 생성하지만, 이 경우 백엔드 서버의 `INTERNAL_API_KEY` 환경변수를 생성된 값으로 별도 업데이트해야 한다.

이후 재부팅 시 자동으로 도어락 페이지가 나타난다.

---

## 사용자 및 권한 구조

보안을 위해 두 시스템 계정이 역할별로 분리되어 있다.

| 계정 | 역할 | 권한 |
|------|------|------|
| `kiosk` | X 세션 및 Chromium 키오스크 실행 | `door-lock`, `video`, `audio` 그룹 |
| `door-lock-svc` | Flask 데몬 실행 | `door-lock`, `gpio`, `bluetooth` 그룹 + `rfkill unblock bluetooth`/`systemctl restart bluetooth` NOPASSWD sudo |

API 키는 `/etc/door-lock/api-key`에 저장되며 `door-lock-svc`만 읽을 수 있다. sudo 권한은
`/etc/sudoers.d/door-lock-bluetooth`에 `systemctl restart bluetooth` 한 줄로만 좁혀 부여한다 —
이유는 아래 "BLE 계층별 한계"의 bluetoothd 능동 재시작 항목 참고.

---

## 제약 사항

- 네 파일(`setup-door-lock.sh`, `start-door-lock.sh`, `stop-door-lock.sh`, `door-lock-daemon.py`)은 반드시 같은 폴더에 있어야 한다. `start-door-lock.sh`가 같은 디렉토리를 기준으로 나머지 파일을 참조하기 때문.
- Raspberry Pi OS 64-bit Lite 기반 (KMS 드라이버 `vc4-kms-v3d` 사용)
- DSI 디스플레이 사용 시 디스플레이 회전은 `xrandr`로 처리한다. `lcd_rotate` 설정은 KMS 드라이버에서 작동하지 않는다.
- `/etc/door-lock/api-key`에 저장된 키가 백엔드에서 유효한 `UserRole.SYSTEM` 키여야 한다. 값이 반드시 동일할 필요는 없으며, 백엔드가 비대칭 키 방식을 사용하는 경우 그에 맞는 값을 저장하면 된다.

---

## 파일 구성 및 실행 순서

```
some-dir/
├── setup-door-lock.sh    # 1. 최초 1회: 패키지 설치 + 파일 다운로드 + 환경 구성
├── start-door-lock.sh    # 2. 부팅마다: X 세션 시작 + Chromium 키오스크 실행
├── stop-door-lock.sh     # 3. 필요 시: 모든 프로세스 정지
├── door-lock-daemon.py   # 4. 데몬: Flask HTTP 서버 + GPIO 제어 (systemd 관리)
└── setup-door-lock-test.sh  # (테스트용) 자동 실행 없이 데몬만 수동으로 띄우는 셋업
```

### 1. `setup-door-lock.sh` — 최초 설치

Pi에서 한 번만 실행.

1. 시스템 패키지 설치 (X11, Chromium, Python Flask, gpiozero, unclutter, fonts-nanum, locales)
2. 한글 로케일(ko_KR.UTF-8) 및 NanumGothic 기본 폰트 설정
3. `door-lock` 그룹 및 `kiosk`, `door-lock-svc` 사용자 생성, 그룹 권한 설정
4. GitHub에서 `start-door-lock.sh`, `stop-door-lock.sh`, `door-lock-daemon.py` 다운로드
5. API 키 생성 및 `/etc/door-lock/api-key`에 저장 (백엔드의 `INTERNAL_API_KEY`와 동기화 필요)
6. Chromium 프로필 초기화 및 정책 설정 (`/etc/chromium/policies/managed/pwa_install.json`)
   - PWA 강제 설치
   - Private Network Access 허용 (`LocalNetworkAccessAllowedForUrls`)
   - 개발자 도구 비활성화 (`DeveloperToolsAvailability: 2`)
   - 번역 팝업 비활성화 (`TranslateEnabled: false`)
7. `kiosk` 사용자로 tty1 자동 로그인 설정
8. `kiosk`의 `~/.bashrc`에서 tty1 진입 시 `start-door-lock.sh` 직접 호출
9. `kiosk`의 `~/.xinitrc`에서 `start-door-lock.sh` 실행 (X 세션 진입점)
10. 디스플레이 절전 cron 등록 (09:00 해제 / 21:00 활성화)
11. `door-lock-daemon.service` systemd 서비스 등록 및 자동 시작

### 2. `start-door-lock.sh` — 부팅 진입점

`~/.bashrc` → `startx` → `.xinitrc` 순으로 자동 실행된다.

> `DISPLAY` 환경변수가 없으면 `startx`를 호출하고 종료한다. `.xinitrc`가 `DISPLAY`를 설정한 채로 이 스크립트를 다시 호출한다.

1. `stop-door-lock.sh` 호출 — 이전 프로세스 정리
2. 시간대에 따라 화면 절전 설정 (09:00~21:00 절전 해제, 그 외 5분 절전)
3. DSI 디스플레이 180도 회전 (`xrandr --output DSI-1 --rotate inverted`)
4. 터치 입력 좌표 변환 (`xinput` ft5x06 장치 Coordinate Transformation Matrix 설정)
5. `unclutter`로 마우스 커서 숨김
6. Chromium `--app` 플래그로 PWA 키오스크 실행

> 데몬(`door-lock-daemon.py`)은 systemd가 관리하므로 이 스크립트에서 직접 실행하지 않는다.

### 3. `stop-door-lock.sh` — 프로세스 정지

`start-door-lock.sh`에서 자동 호출되며, 수동으로 실행해도 된다.

- Chromium, unclutter를 순서대로 종료
- SIGTERM → 10초 대기 → SIGKILL 순으로 처리
- `DAEMON_DIR` 환경변수로 데몬 경로를 받음 (기본값: 스크립트 자신의 위치)

### 4. `door-lock-daemon.py` — Python 데몬

Flask HTTP 서버로 `127.0.0.1:8080`에서 수신. systemd `door-lock-daemon.service`가 관리하며 실패 시 자동 재시작.

- `GET /health` — 데몬 상태 확인
- `POST /unlock` — 학번과 방 번호를 백엔드에 전달해 인증 후 GPIO 릴레이 개방, 인증된 회원 이름 반환
  - `127.0.0.1`에서만 요청 수락
  - 백엔드 타임아웃 5초, 실패 시 504/502 반환
- 출입 시도·성공·실패를 `/var/log/door-lock/daemon.log`에 기록 (5MB × 3개 순환)
- 백엔드 인증 + 릴레이 개방 로직은 `attempt_unlock(endpoint, payload, source)` 함수로 공용화되어 있다. `/unlock`(`source="keypad"`, 학번 직접 입력)과 블루투스 인증(`source="bluetooth"`, 아래 참고) 양쪽이 이 함수를 공통으로 호출한다.
- `start_reader()`는 블루투스 인증을 처리한다. 앱이 BLE 광고로 학번을 보내면 이를 감지해 `attempt_unlock(..., source="bluetooth")`을 호출하고, 성공하면 문이 열린다. BLE 관련 상태/함수는 전부 `Ble` 클래스(및 그 안에 중첩된 `Ble.Payload`/`Ble.Registry`/`Ble.Advertising`/`Ble.Adapter`)에 있다 — 자세한 구조는 "BLE 클래스 구조" 절 참고. 프로토콜 요약:
  - **UUID와 payload 규격은 앱이 기준이다.** 안드로이드 앱(`parksiwoo2/doorlock-frontend`) 원격 `main`의 `BleConstants.kt` / `BlePayloadCodec.kt`에 맞춰 Pi를 고친다. UUID는 코드 상수(`Ble.UUID_*`)가 기본값이고, 재배포 없이 바꾸려면 `/etc/door-lock/ble-uuids` 파일로 덮어쓸 수 있다(아래 "BLE UUID 설정 파일" 참고).

    | UUID | 방향 | payload |
    |---|---|---|
    | `0312` PRESENCE | Pi → 폰 | 더미 1바이트 (앱은 UUID 존재만 확인) |
    | `1111` REGISTER | **폰 → Pi** | 21바이트 = 인코딩 학번(20) + 공개여부(1) / **2바이트** = 세션토큰(1) + 공개여부(1) |
    | `2222` CONFIRM | Pi → 폰 | 21바이트 = 인코딩 학번(20) + 세션토큰(1) |
    | `3333` HEARTBEAT_ACK | Pi → 폰 | **정확히 24바이트** 재실 명단 |

  - **폰이 보내는 신호는 `1111` 하나뿐이고 payload 길이로 의미가 갈린다** — 21바이트는 등록 요청, 2바이트는 하트비트. 길이로 명확히 구분되며, **하트비트는 항상 즉시 처리한다**(`Ble._on_register_signal`). 이걸 어기면 등록 기기가 30초 뒤 만료되고 재등록하며 **이미 안에 있는 사람에게 문이 다시 열리는 버그**가 생긴다.
  - **상시 근접 스트림**: 트리거로 수집 윈도우를 열고 닫는 방식이 아니라, Pi는 항상 스캔하며 **1초마다 그 사이 들어온 등록 요청을 배치로 모은다**(같은 폰이 여러 번 보내면 최신 것만 남긴다, `Ble.Registry.add_candidate`/`snapshot_and_clear_candidates`). 그 1초치 후보 목록을 `select_closest_candidate_in_range()`에 넘겨 임계 거리 이내에 있는 후보 중 가장 가까운 것 하나를 고른다 — 범위 안에 아무도 없으면 아무 일도 일어나지 않는다. **거리 계산과 임계값은 목업이다**(현재는 RSSI 최댓값 + 임계값 비교로 임시 구현, `BLE_PROXIMITY_RSSI_THRESHOLD`) — 정확한 알고리즘은 별도로 구현될 예정이다. 선택된 후보에 대해서만 `attempt_unlock` 1회를 호출하고, 성공하면 등록 + 확인 광고를 켠다. **이미 등록된 기기를 후보에서 제외하는 검사는 후보를 넣을 때가 아니라 꺼낼 때(`Ble.Registry.snapshot_and_clear_candidates`) 한다** — 후보 삽입과 실제 인증 사이에는 최대 1초(배치 주기) + 백엔드 왕복 시간이 있어서, 넣을 때만 검사하면 그 사이에 등록이 끝난 기기가 다음 배치에 남아 재인증된다. 그러면 같은 사람에게 문이 다시 열리고 세션토큰까지 새로 발급되어, 폰이 든 토큰과 재실 명단이 어긋난다. 30초 이상 하트비트가 없으면 등록 목록에서 제거한다.
  - **재실 명단(`3333`)은 항상 정확히 24바이트다.** 등록된 세션토큰을 앞에서부터 채우고 뒤는 `0`으로 패딩한다(`Ble.Payload.roster_payload()`). 앱의 `matchesHeartbeatRoster`가 `payload.size != 24`면 무조건 거부하므로 반드시 지켜야 한다. **정원은 24명이 하드 캡이다** — `Ble.Registry.register()`가 24명이 이미 등록된 상태에서의 새 등록 요청을 그냥 실패시킨다(`attempt_unlock`조차 호출하지 않는다). 그래서 명단을 여러 묶음으로 나눠 순환할 필요가 없다.
  - **세션토큰은 1~255이며 `0`은 발급하지 않는다**(`Ble.Registry._allocate_random_id`). `0`은 앱이 명단의 빈 슬롯으로 해석하는 예약값이라, 0을 주면 그 기기는 명단에서 자기 토큰을 영영 못 찾는다.
  - 학번은 앱의 `BlePayloadCodec.encodeStudentId()`와 동일한 방식(2바이트씩 맞바꾼 뒤 16진수 20자로 인코딩)으로 주고받는다 — `Ble.Payload.decode_student_id()`/`encode_student_id()`가 이 인코딩을 처리한다.
  - **송출은 광고 인스턴스 3개로 나눠 등록한다**(아래 "BLE 계층별 한계" 참고). 신호마다 요구되는 주기가 다르기 때문이다. **세 인스턴스 모두 기동 시 상시 등록해두고, 운영 중엔 절대 register/unregister를 다시 하지 않는다** — 이벤트 시에만 register하는 방식은 AlreadyExists 영구 교착을 만든다. "송출 여부"는 등록 여부가 아니라 **내용을 바꾸는 것**으로 조절한다.

    | 인스턴스 | 평소(비활성) | 인증 성공 시 |
    |---|---|---|
    | 트리거 `0312` | `Duration=1s`, ServiceUUIDs+ServiceData 실음(앱이 `setServiceUuid`로 거름) | 변화 없음 |
    | 명단 `3333` | `Duration=1s`, 등록된 세션토큰 목록(24바이트 고정) | 새 토큰 반영 시에만 payload 갱신 |
    | 확인 `2222` | `Duration=1s`, payload는 **21바이트 전부 0**(어떤 실제 학번과도 절대 안 겹침 — 앱 필터가 라디오 단계에서 걸러줌) | `Duration=5s`로 잠깐 키우고 payload를 실제 값(학번+토큰)으로 채운 뒤, `BLE_CONFIRM_CONTENT_SECONDS`(5초) 뒤 둘 다 원복 |

    평소엔 세 인스턴스가 1+1+1=3초 주기로 균등 순환하다가, 확인이 활성화되면 그 구간만 1+1+5=7초 주기가 되고 확인 채널이 그중 5초(약 71%)를 차지한다. `Duration` 변경 자체는 커널이 구조체 값만 갱신해 사실상 공짜지만(비확장 광고 컨트롤러는 HCI 왕복도 없음), 이미 진행 중인 순번을 끊고 끼어들진 못하므로 로테이션 순번이 돌아오는 대기 시간(최악 ~2초)은 그대로 남는다. 그래도 앱의 확인 대기 하드 타임아웃(`BleRelayService.kt`의 `openConfirmationTimeoutMillis = 10_000L`) 대비 여유가 충분하다.

  - 광고와 스캔 모두 `bluezero` 없이 BlueZ D-Bus API를 직접 쓴다. 내용 교체는 unregister/register가 아니라 광고 오브젝트의 `PropertiesChanged` 신호로 한다. 스캔은 `InterfacesAdded`/`PropertiesChanged` 시그널 payload에서 `ServiceData`/`RSSI`를 직접 읽는다.
  - 어댑터 경로는 하드코딩하지 않고 `org.bluez.Adapter1`을 구현한 오브젝트를 찾아 쓴다(`_find_adapter_path`). `BLE_ADAPTER` 환경변수로 강제 지정할 수 있다 — USB 동글을 꽂아 어댑터가 늘었을 때 필요하다.
  - 트리거 폴링/명단 묶음 순환/만료 정리 같은 주기 작업은 `GLib.timeout_add`가 아니라 `threading.Timer` 자기재예약 방식(`_refresh_schedules`와 같은 패턴)으로 돈다 — 실기에서 이 환경의 `dbus-python`+PyGObject 조합에서 `GLib.timeout_add`가 최초 1회 이후 반복 실행되지 않는 문제가 확인돼서 우회했다. D-Bus 시그널 콜백(`_on_bluez_properties_changed` 등)과 `GLib.MainLoop().run()`은 그대로 GLib 쪽에 남아있다.

---

## 유지보수 규칙

### `setup-door-lock.sh`

- **멱등성 필수**: 여러 번 실행해도 부작용이 없어야 한다.
  - `apt-get install -y`는 이미 설치된 경우 건너뛴다.
  - `usermod -aG`는 `groups`로 사전 확인 후 실행한다.
  - `~/.bashrc` 추가는 마커 문자열(`# door-lock: auto startx`)로 중복 방지한다.
  - `~/.xinitrc`, `autologin.conf`, `pwa_install.json`은 덮어쓰기 방식으로 항상 최신 상태를 유지한다.
  - Chromium 프로필은 매번 초기화된다 (`~/.config/chromium` 삭제). 로컬 스토리지도 함께 삭제되므로 주의.
- **파일 다운로드**: `REPO_RAW` 변수 하나만 수정하면 브랜치/포크 전환이 가능하다.
- **API 키**: `/etc/door-lock/api-key`에 저장된 키가 백엔드에서 유효한 `UserRole.SYSTEM` 키여야 한다. `internal-api-key`를 미리 준비하지 않고 설치한 경우, 설치 후 출력되는 키 값을 백엔드가 인증할 수 있도록 등록해야 한다.
- **블루투스 의존성**: `bluez`/`python3-dbus`/`python3-gi`를 apt로 설치하면 된다 (BLE는 BlueZ D-Bus API를 직접 쓰므로 `bluezero` 같은 추가 패키지가 필요 없다). `door-lock-svc` 계정은 `gpio`뿐 아니라 `bluetooth` 그룹에도 속해야 BlueZ D-Bus API를 쓸 수 있다.

### `start-door-lock.sh`

- `DAEMON_DIR`은 스크립트 자신의 위치(`$(dirname "$0")`)를 기반으로 결정된다. 폴더를 이동해도 경로 수정 없이 동작한다.
- 터치 장치 탐색에 사용하는 칩 모델명은 `start-door-lock.sh` 상단의 `TOUCH_CHIP` 변수로 관리한다. 같은 DSI 터치스크린 하드웨어를 사용하는 한 모든 기기에서 동일한 이름이 나온다. 다른 터치스크린 하드웨어를 섞어 운영할 경우 해당 기기의 `TOUCH_CHIP` 값을 칩 이름에 맞게 수정한다. xinput 숫자 ID는 X 서버가 매 세션마다 동적으로 할당하므로 스크립트가 자동으로 탐색한다.

### `stop-door-lock.sh`

- `pgrep` 패턴은 프로세스를 정확히 식별할 수 있도록 충분히 구체적으로 작성한다.

### `door-lock-daemon.py`

- `/health` 엔드포인트는 반드시 유지한다. 키오스크 프론트엔드가 데몬 생존 여부를 이 엔드포인트로 확인한다.
- GPIO 핀 번호 변경 시 `GPIO_PIN` 상수만 수정하면 된다.
- 백엔드 URL은 `BACKEND_URL` 환경변수로 주입되며 `setup-door-lock.sh`가 자동으로 설정한다.
- 방 번호는 `ROOM_NUMBER` 환경변수로 주입되며 `setup-door-lock.sh` 실행 시 대화식으로 입력받아 설정한다.
- 로그 파일 경로는 `LOG_FILE` 상수로 지정되어 있으며 디렉토리가 없으면 자동 생성된다.
- 블루투스 인증은 `start_reader()`가 기동하는 전용 스레드(`_ble.start_logged`)에서 처리한다. BLE 관련 상태와 함수는 모두 `Ble` 클래스 아래 중첩돼 있다 — 구조는 바로 아래 "BLE 클래스 구조" 참고.
- 백엔드 API 스펙이 블루투스 인증을 위해 바뀌면(새 엔드포인트 또는 기존 엔드포인트의 payload 확장) `Ble._confirm_candidate()`가 `attempt_unlock`에 넘기는 `endpoint`/`payload` 값만 그에 맞게 구성하면 되고, `attempt_unlock`/`request_backend_authorization` 자체는 손댈 필요 없다. 현재는 `/unlock`과 동일한 `/internal/door-lock/accesses` + `{"number", "roomNumber"}`를 그대로 재사용한다는 가정이며, 백엔드팀 확인이 필요하다.
- **근접 판정은 목업이다.** `select_closest_candidate_in_range()`(모듈 최상위 자유 함수, `Ble` 클래스 밖에 있다 — 알고리즘을 통째로 갈아끼울 사람이 클래스 구조를 몰라도 바로 찾도록)와 임계값 상수 `BLE_PROXIMITY_RSSI_THRESHOLD`는 현재 RSSI 최댓값 + 임계값 비교로 임시 구현돼 있다. 실제 거리 판별 알고리즘으로 교체될 예정이다.
- **BLE UUID와 payload 규격은 앱이 기준이다.** 앱(`parksiwoo2/doorlock-frontend`) 원격 `main`의 `BleConstants.kt`/`BlePayloadCodec.kt`를 근거로 삼고, 어긋나면 Pi를 고친다. 코드 상수 `Ble.UUID_PRESENCE`/`UUID_REGISTER`/`UUID_CONFIRM`/`UUID_HEARTBEAT_ACK`가 기본값이고, 재배포 없이 바꾸려면 `/etc/door-lock/ble-uuids` 파일을 쓴다(아래 "BLE UUID 설정 파일" 참고). **세션토큰은 `Ble.RANDOM_ID_MIN`(=1)~`Ble.RANDOM_ID_MAX`(=255)이며 `0`은 앱이 명단의 빈 슬롯으로 쓰는 예약값이라 절대 발급하지 않는다.**
- **재실 명단 payload는 항상 정확히 `Ble.ROSTER_LENGTH`(=24)바이트여야 한다**(`Ble.Payload.roster_payload()`). 앱의 `matchesHeartbeatRoster`가 길이를 엄격히 검사하므로 인원수만큼만 보내면 폰이 통째로 무시한다. **정원(24명)을 넘는 등록 요청은 `Ble.Registry.register()`가 그냥 실패시킨다** — 그래서 명단은 항상 한 묶음이면 충분하다. 이 계약들(24바이트 고정, 토큰 0 금지, 정원 초과 시 실패)은 `test_door_lock_daemon.py`가 테스트로 못박고 있다.
- 학번은 원문이 아니라 앱과 동일한 인코딩(2바이트씩 맞바꾼 뒤 16진수 20자, `Ble.Payload.decode_student_id`/`encode_student_id`)으로 주고받는다. 등록 요청(21바이트)과 하트비트(2바이트)는 같은 REGISTER UUID로 오지만 payload 길이로 구분한다(`Ble._on_register_signal`). **하트비트는 항상 즉시 처리해야 한다** — 이걸 어기면 이미 안에 있는 사람에게 문이 반복해서 열린다.
- 근접 스캔(`Ble._proximity_scan_loop`)/만료 정리(`Ble._reap_loop`)는 각각 독립된 `threading.Timer` 체인으로 자기 자신을 재예약한다. 콜백 안에서 예외가 나도 재예약 자체는 계속되도록 각 루프가 자체적으로 try/except를 감싸거나 `_log_callback_exceptions` 데코레이터를 쓴다.
- **24시간 운용을 전제로 로그를 아낀다.** 수신 신호마다 로그를 남기면 초당 수십 줄이 쌓여 SD 카드 수명과 로그 가독성을 모두 해친다. 후보는 처음 잡혔을 때만 `INFO`로 남기고, 그 외 `INFO`에는 실제 사건(개방·등록·만료·광고 등록/해제·오류)만 남긴다.
- `dbus`/`gi`(PyGObject) 의존성은 `Ble.Adapter`/`Ble.Advertising` 안에서만 지연 import한다 — 나머지 로직(상태 전이, 후보 선택, payload 생성 등)은 이 라이브러리들이 없는 개발 환경에서도 import/테스트 가능해야 하기 때문이다(`test_door_lock_daemon.py`). `org.bluez.LEAdvertisement1`을 구현하는 클래스만 `dbus.service.Object` 상속이 필수라 클래스로 두되, 정의 자체를 `Ble.Advertising._advertisement_class()` 팩토리 안에 넣어 이 규칙을 지킨다.

#### BLE 클래스 구조

BLE 관련 상태/함수는 전부 `Ble` 클래스 하나에 있다. **상수는 `Ble`(바깥 클래스)에 두고, 관심사별로
네 개의 작은 중첩 클래스가 각자의 상태와 함수만 갖는다** — 안쪽 네 클래스는 서로를 모르고, 여러
클래스를 아우르는 조정 로직만 `Ble`(바깥) 메서드로 존재한다.

```
Ble (상수 + 인스턴스 보유 + 조정 로직)
 ├── Ble.Payload       — 인코딩/디코딩 순수함수. 아무것도 의존 안 함
 ├── Ble.Registry      — 재실 테이블/후보/토큰 발급. Ble 상수만 의존
 ├── Ble.Advertising   — 광고 인스턴스 3개. Ble.Payload만 의존
 └── Ble.Adapter       — BlueZ 연결/스캔/메인루프. Ble.Payload만 의존(payload 추출용), 콜백으로 위에 보고
```

프로세스당 인스턴스 하나(`_ble = Ble()`, 모듈 전역)면 충분하다. 조정 메서드
(`_on_register_signal`/`_proximity_scan_loop`/`_confirm_candidate`/`_reap_loop`/`start`/`shutdown`)는
여러 안쪽 클래스와 `attempt_unlock`(백엔드 인증)을 가로지르므로 바깥 `Ble`에 있다. 예를 들어
`_confirm_candidate`는 `attempt_unlock` 호출 → 성공 시 `self.registry.register()` → 토큰이 나오면
`self.advertising.refresh_roster()`/`show_confirm()` 순서로 여러 클래스를 조정하는데, 이런 흐름은
`Registry`나 `Advertising` 어느 한쪽에 넣으면 그 클래스가 상대방을 알아야 해서 결합이 생긴다.

`select_closest_candidate_in_range()`(근접 판정 목업)와 그 임계값 상수는 의도적으로 `Ble` 밖의
모듈 최상위에 둔다 — 다른 팀원이 실제 알고리즘으로 교체할 자리라, 클래스 구조를 몰라도 바로
찾아 고칠 수 있어야 하기 때문이다.

#### BLE UUID 설정 파일

`/etc/door-lock/ble-uuids`가 있으면 데몬 기동 시 `Ble._load_uuids_from_file()`이 읽어 코드
상수(`Ble.UUID_PRESENCE` 등 4개)를 덮어쓴다. `key=value` 한 줄씩(`#`로 주석), 값은 16진수로
적는다(`0x` 접두사 없이도 16진수로 해석된다):

```
presence=0312
register=1111
confirm=2222
heartbeat_ack=3333
```

두 setup 스크립트(`setup-door-lock.sh`/`setup-door-lock-test.sh`) 모두 파일이 없을 때만 위 기본값으로
생성한다(있으면 건드리지 않는다 — `/etc/door-lock/api-key`와 같은 멱등성 패턴). 앱과 UUID가
달라지면 이 파일을 고치고 데몬을 재시작하면 되고, 코드 재배포는 필요 없다.

### `setup-door-lock-test.sh` — 테스트 전용 셋업

실기에서 BLE를 수동으로 검증할 때 쓴다. 운영용 `setup-door-lock.sh`와 달리 **systemd 서비스를 등록하지 않는다** — 전원만 켜도 데몬이 자동 실행되면 수동 실행분과 포트·어댑터를 두고 경합해서 테스트가 불가능하다. 키오스크 UI(X11/Chromium/폰트/PWA 정책/자동 로그인/디스플레이 cron)도 설치하지 않고, GitHub에서 받지 않고 **스크립트와 같은 디렉터리의 `door-lock-daemon.py`를 쓴다**(개발 중인 파일을 scp로 올려 테스트하므로). 데몬은 `/opt/door-lock/`에 설치되고, 마지막에 실행 명령을 그대로 출력한다.

### BLE 계층별 한계 (실기 + 공식 소스로 확인한 것)

이 부분은 오래 헤맨 곳이라, 왜 지금 구조인지 근거를 남긴다. 조사에 쓴 BlueZ 5.82(파이에 깔린 그 버전) 소스와 mgmt-api 문서, bluezero 소스는 `ble-research/`에 받아뒀다.

- **광고 내용은 등록을 유지한 채 교체할 수 있다.** bluetoothd는 등록에 성공하면 우리 광고 오브젝트에 property watch를 걸고(`src/advertising.c:1384`), `ServiceData` 등이 바뀌었다는 `PropertiesChanged`를 받으면 `refresh_advertisement()`로 컨트롤러에 새 데이터를 밀어넣는다(같은 파일 1300–1323). 그래서 인스턴스는 기동 시 한 번만 등록하면 되고, 이후엔 payload만 갱신한다.
- **그런데 `bluezero`로는 그게 불가능하다.** `bluezero/advertisement.py:265`의 `Set()`은 파이썬 dict만 조용히 바꾸고 `PropertiesChanged`를 절대 emit하지 않는다. 그래서 예전 구현은 내용을 바꿀 때마다 unregister→register를 반복해야 했다. `Duration`/`Timeout`/`MinInterval`/`MaxInterval`도 `bluezero`의 `props`에 아예 없어 송출 시점·주기를 제어할 방법이 없었다 — 지금 구조가 이 속성들을 쓰므로 `bluezero`로는 되돌아갈 수 없다.
- **`RegisterAdvertisement`는 반드시 비동기(`reply_handler`/`error_handler`)로 불러야 한다.** 이 메서드는 즉시 응답하지 않는다 — bluetoothd가 먼저 우리 광고 오브젝트로 프록시를 만들어(`advertising.c:1607,1620`) **우리 프로세스로 `Introspect`/`GetAll`을 되돌아 호출**하고, 그 응답을 받아 `client_proxy_added()`(1571) → `parse_advertisement()`(1581)를 거친 뒤에야 응답을 보낸다(등록 함수 자체는 1703에서 `return NULL`로 응답을 미룬다). 그래서 블로킹으로 부르면 **우리 스레드는 응답을 기다리고 bluetoothd는 우리 `GetAll` 응답을 기다리는 데드락**이 되어 25초 뒤 `NoReply`로 끝난다. 등록은 메인루프가 뜬 뒤 처리되도록 `GLib.MainLoop().run()` 직전에 비동기로 건다. **되돌아 호출이 없는 메서드**(`SetDiscoveryFilter`/`StartDiscovery`/`Powered` 설정/`UnregisterAdvertisement` — `Release()`는 문서상 `[noreply]`)는 블로킹이어도 안전하다.
- **그 `NoReply`가 `org.bluez.Error.AlreadyExists` 영구 교착으로 이어졌다.** `RegisterAdvertisement`는 mgmt 명령이 끝나기 전에 클라이언트를 큐에 넣으므로(`advertising.c:1701`), 타임아웃이 나도 bluetoothd에는 등록이 남는다. 그 뒤 같은 (owner, object path)로 재시도하면 무조건 `AlreadyExists`다(`advertising.c:1733`의 `match_client`). **그래서 등록 실패 시 재시도 루프를 두지 않는다** — 예전에 넣었던 재시도가 일시적 실패를 영구 장애로 승격시켰다. 실패는 재시도 대신 `daemon.log`에 남긴다.
- **다른 프로세스가 등록한 광고는 우리가 지울 수 없다.** `UnregisterAdvertisement`는 (owner, object path)가 모두 일치해야 동작한다(`advertising.c:1727`). 그래서 기동 시 우리 경로만 정리하고, `ActiveInstances`가 0이 아니면 로그로 남겨 진단에 쓴다. 완전 초기화가 필요하면 `systemctl restart bluetooth`(bluetoothd가 기동 시 커널 인스턴스를 리셋한다)를 쓴다.
- **동시 광고는 안 된다 — 커널이 교대로 내보낸다.** `sudo btmgmt advinfo`의 supported flags에 Secondary Channel 비트(LE 1M/2M/Coded)가 없어 LE Extended Advertising 미지원이고, 그 경우 커널이 인스턴스들을 *소프트웨어 라운드로빈으로 시분할*한다(`mgmt-api.txt`의 Add Advertising 설명). 즉 `Max instances: 5`는 "동시 5개"가 아니라 "로테이션 큐 5개"다. 인스턴스 3개를 등록하되 `Duration`으로 각자의 airtime을 배분하는 이유가 이것이다.
- **payload 한도는 24바이트다.** legacy 광고 31바이트에서 ServiceData AD 헤더 4바이트를 빼고 BlueZ가 Flags AD(3바이트)를 붙일 여지까지 감안한 값이며 `Ble.MAX_SERVICE_DATA_BYTES`로 상수화했다. **Service UUID 목록 AD는 4바이트를 더 먹으므로 꼭 필요한 인스턴스에만 싣는다** — 트리거(상시광고, `0312`)는 앱이 `setServiceUuid`로 거르므로 필수이고, 24바이트짜리 명단에 넣으면 31바이트를 넘긴다.
- **스캔도 `bluezero`를 안 쓴다.** `bluezero/adapter.py:304-317`의 `_interfaces_added`는 시그널에 이미 들어 있는 `ServiceData`를 버리고 주소로 `Device` 오브젝트를 다시 조회하는데, 폰들이 랜덤 MAC을 빠르게 바꾸는 환경에선 조회 전에 기기가 사라져 `ValueError: Cannot find a device`가 끊임없이 터진다. 그래서 시그널 payload에서 직접 읽는다.
- **기동마다 rfkill을 풀고 bluetoothd를 능동적으로 재시작한다(`Ble.Adapter.prepare()`).** 실기(Pi 4)에서 `Powered` 설정이 `org.bluez.Error.Failed`로 거부되고, 이어진 `SetDiscoveryFilter`도 `org.bluez.Error.NotReady`로 실패해 BLE 스레드 전체가 죽는 문제가 있었다. 처음엔 `property_set_mode()`(`src/adapter.c:3116` 부근 — `mgmt_send()`가 큐잉에 실패하면 커널 응답도 없이 곧장 `.Failed`를 돌려주는 분기)를 근거로 "bluetoothd 내부 mgmt 연결이 깨졌다"고 추정했지만, **bluetoothd만 재시작해서는 재현이 그대로 반복돼 그 가설은 반증됐다.** 실제 원인은 **rfkill 소프트 블록**이었다 — `rfkill list`에서 hci0이 `Soft blocked: yes`, `hciconfig -a`에서 `hci0`이 `DOWN`으로 확인됐고, `rfkill unblock bluetooth` 후 `hciconfig hci0 up`이 곧바로 `UP RUNNING`으로 전환되는 것으로 재현·해소를 직접 확인했다. rfkill이 막고 있으면 bluetoothd를 몇 번 재시작해도 그 아래 인터페이스를 못 올리므로 기다려도 저절로 안 풀린다. 블루투스는 이 데몬 전용이라 언블록·재시작해도 방해받는 다른 프로세스가 없으므로, 상태를 확인하고 필요할 때만 조치하는 대신 **매 기동마다 무조건** rfkill 해제 + bluetoothd 재시작을 실행해 항상 같은 known-good 상태에서 시작한다. `door-lock-svc`에게 `rfkill unblock bluetooth`/`systemctl restart bluetooth` NOPASSWD sudo만 최소로 내준다(`/etc/sudoers.d/door-lock-bluetooth`, 두 setup 스크립트가 설치).
- **광고 인스턴스는 `Type: "peripheral"`(connectable)로 등록한다 — `"broadcast"`가 아니다.** rfkill을 풀고
  discovery까지 정상 시작된 뒤에도 `org.bluez.Error.Failed: Failed to register advertisement`가 났다.
  `btmon` 캡처로 확정했다: `LE Set Advertising Data`는 성공하는데 곧바로 이어지는
  `LE Set Random Address`가 `Command Disallowed (0x0c)`로 거부되어 `Add Extended Advertising Data`가
  `Failed (0x03)`로 끝난다. 근거는 `mgmt-api.txt:3593`(Add Extended Advertising Parameters Command) —
  *"When using non-connectable or scannable advertising, the controller will be programmed with a
  non-resolvable random address. When the system is connectable, then the identity address ... will
  be used."* `Type: "broadcast"`(non-connectable)를 쓰면 등록마다 새 NRPA를 요구하는데, 우리는 상시
  스캔을 켜두고 있어 Bluetooth Core Spec Vol 4 Part E §7.8.4("스캐닝 중엔 `LE Set Random Address`
  금지")에 걸려 매번 거부됐다. `advertising.c:937`(`get_adv_flags`)에서 `Type == "peripheral"`일 때만
  `MGMT_ADV_FLAG_CONNECTABLE`이 켜지고, 그 경우 identity(고정 공개 MAC) 주소를 써서 이 명령 자체가
  필요 없어진다. 프로토콜은 GATT 연결 없이 ServiceData 브로드캐스트만 쓰므로 `peripheral`로 바꿔도
  폰 쪽 감지 로직에는 영향이 없다 — 광고 패킷이 `ADV_NONCONN_IND`에서 `ADV_IND`로, MAC이 랜덤에서
  고정 공개 주소로 바뀔 뿐이다.

### 하드웨어: Pi 3B에서는 BT가 죽는다

**Pi 3B(Rev 1.2)는 블루투스 UART에 하드웨어 흐름제어선이 연결돼 있지 않다.** 장비에서 직접 확인한 결과다:

```
$ pinctrl get 30-33     # Pi 3B
30: ip  // CTS0  ← 흐름제어선: 미할당
31: ip  // RTS0  ← 흐름제어선: 미할당
32: a3  // TXD0  ← 데이터선: 할당됨
33: a3  // RXD0  ← 데이터선: 할당됨
```

라이브 디바이스 트리(`/proc/device-tree/soc/serial@7e201000/`)에도 `uart-has-rtscts`가 없다. 공식 커널 DT를 비교하면 Pi 4(`bcm2711-rpi-4-b.dts:226`)에는 `uart0_ctsrts_gpio30` + `uart-has-rtscts`가 있지만 Pi 3B(`bcm2837-rpi-3-b.dts:131`)에는 둘 다 없다 — 제조사도 인지하고 다음 모델에서 보완한 하드웨어 한계다.

상시 LE 스캔으로 UART 인바운드 부하가 최악인데 흐름제어가 없어 CPU가 밀리면 바이트가 유실된다 → `Frame reassembly failed (-84)`(EILSEQ) → 커널 6.1+는 재동기화를 못 해 **재부팅 전까지 BT가 완전히 죽는다.** 알려진 미해결 업스트림 버그다([raspberrypi/linux#5460](https://github.com/raspberrypi/linux/issues/5460), 2023년 등록·현재까지 open). 부팅 직후엔 멀쩡하다가 한 시간쯤 뒤 터지는 게 특징이다.

**소프트웨어로 해결할 수 없다.** Pi 4로 교체하거나(흐름제어가 있다), USB 블루투스 동글로 UART 경로 자체를 우회해야 한다(동글은 BT 4.0 이상이면 충분하고, `dtoverlay=disable-bt`로 내장 BT를 꺼야 동글이 유일한 어댑터가 된다).

확인 명령:

```bash
pinctrl get 30-33       # 30/31이 a3면 흐름제어 활성
sudo btmgmt advinfo     # Secondary Channel 항목이 있으면 동시 광고 가능
dmesg | grep -i blue    # Frame reassembly failed 가 없어야 정상
```

`setup-door-lock.sh`의 BLE 의존성은 `bluez`/`python3-dbus`/`python3-gi`면 충분하다(BLE는 BlueZ D-Bus API를 직접 쓰므로 `bluezero`가 필요 없다). 데몬은 CLI 인자 없이 항상 상시 근접 스트림으로 동작한다 — 테스트 시엔 `setup-door-lock-test.sh`로 셋업하고 수동 실행한다.
