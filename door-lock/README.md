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
curl -fsSL https://raw.githubusercontent.com/khu-khlug/sight-infra/main/door-lock/setup-door-lock.sh -o setup-door-lock.sh
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
| `door-lock-svc` | Flask 데몬 실행 | `door-lock`, `gpio` 그룹 |

API 키는 `/etc/door-lock/api-key`에 저장되며 `door-lock-svc`만 읽을 수 있다.

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
└── door-lock-daemon.py   # 4. 데몬: Flask HTTP 서버 + GPIO 제어 (systemd 관리)
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
