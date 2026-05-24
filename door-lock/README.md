# door-lock

동방 도어락에 설치되어야 할 스크립트들. 라즈베리 파이에서 실행되며 PWA → 로컬 데몬 → GPIO → 릴레이 → 도어락 구조로 동작.

---

## 사용법 (Pi에서 실행)

라즈베리 파이 이미저로 Raspberry Pi OS 64-bit Lite를 설치 후
curl을 통해 깃허브에서 셋업 스크립트(/sight-infra/door-lock/setup-door-lock.sh)를 다운로드하여 실행한다.

이후 재부팅 시 자동으로 도어락 페이지가 나타난다.

## 제약 사항

- 네 파일(`setup-door-lock.sh`, `start-door-lock.sh`, `stop-door-lock.sh`, `door-lock-daemon.py`)은 반드시 같은 폴더에 있어야 한다. `start-door-lock.sh`가 같은 디렉토리를 기준으로 나머지 파일을 참조하기 때문.
- 사용자 폴더에 새 폴더를 만들어 그곳에서 셋업 스크립트 실행하는것을 권장함.
- Raspberry Pi OS 64-bit Lite 기반

---

## 파일 구성 및 실행 순서

```
some-dir/
├── setup-door-lock.sh    # 1. 최초 1회: 패키지 설치 + 파일 다운로드 + 환경 구성
├── start-door-lock.sh    # 2. 부팅마다: 데몬 + Chromium 키오스크 실행
├── stop-door-lock.sh     # 3. 필요 시: 모든 프로세스 정지
└── door-lock-daemon.py   # 4. 데몬: Flask HTTP 서버 + GPIO 제어
```

### 1. `setup-door-lock.sh` — 최초 설치

Pi에서 한 번만 실행.

1. 시스템 패키지 설치 (X11, Chromium, Python Flask, gpiozero, unclutter)
2. GitHub에서 `start-door-lock.sh`, `stop-door-lock.sh`, `door-lock-daemon.py` 다운로드
3. `door-lock` 그룹 생성 → `kiosk` 사용자 생성 (sudo 없음) → 스크립트 파일 그룹 소유권 설정 (750)
4. Chromium 정책으로 PWA 강제 설치 (`/etc/chromium/policies/managed/pwa_install.json`)
5. `kiosk` 사용자로 tty1 자동 로그인 설정
6. `kiosk`의 `~/.bashrc`에 tty1 진입 시 `startx` 자동 실행 추가
7. `kiosk`의 `~/.xinitrc`에서 `start-door-lock.sh` 실행

### 2. `start-door-lock.sh` — 부팅 진입점

`.xinitrc`에 의해 X 세션 시작 시 자동으로 실행됩니다.

1. `stop-door-lock.sh` 호출 — 이전 프로세스 정리
2. `python3 door-lock-daemon.py` 백그라운드 실행
3. `/health` 엔드포인트 폴링 — 데몬이 준비될 때까지 대기 (최대 30초)
4. `xset`으로 화면 절전 타이머 설정
5. `unclutter`로 마우스 커서 숨김
6. Chromium `--app` 플래그로 PWA 키오스크 실행

### 3. `stop-door-lock.sh` — 프로세스 정지

`start-door-lock.sh`에서 자동 호출되며, 수동으로 실행해도 됩니다.

- Chromium, unclutter, 데몬을 순서대로 종료
- SIGTERM → 10초 대기 → SIGKILL 순으로 처리
- `DAEMON_DIR` 환경변수로 데몬 경로를 받음 (기본값: 스크립트 자신의 위치)

### 4. `door-lock-daemon.py` — Python 데몬

Flask HTTP 서버로 `localhost:port`에서 수신

- `GET /health` — 데몬 상태 확인 (start 스크립트 폴링용)
- `POST /unlock` — GPIO 신호 송출 → 릴레이 → 도어락 개방

---

## 유지보수 규칙

### `setup-door-lock.sh`

- **멱등성 필수**: 여러 번 실행해도 부작용이 없어야 합니다.
  - `apt-get install -y`는 이미 설치된 경우 건너뜁니다.
  - `usermod -aG`는 `groups`로 사전 확인 후 실행합니다.
  - `~/.bashrc` 추가는 마커 문자열(`# door-lock: auto startx`)로 중복 방지합니다.
  - `~/.xinitrc`와 `autologin.conf`는 덮어쓰기 방식(`cat >`, `tee`)으로 항상 최신 상태를 유지합니다.
- **파일 다운로드**: `REPO_RAW` 변수 하나만 수정하면 브랜치/포크 전환이 가능합니다.
- 패키지를 추가할 경우 `apt-get install -y` 목록에 추가하면 됩니다.

### `start-door-lock.sh`

- **이전 태스크 완료 확인 필수**: 각 단계는 이전 단계가 성공한 후에만 진행합니다.
  - 데몬 시작 후 `/health` 폴링 — 응답이 없으면 `exit 1`로 중단합니다.
  - `stop-door-lock.sh` 실패 시 Chromium을 실행하지 않습니다.
- `DAEMON_DIR`은 스크립트 자신의 위치(`$(dirname "$0")`)를 기반으로 결정됩니다. 폴더를 이동해도 경로 수정 없이 동작합니다.
- `DAEMON_PORT` 변수를 변경하면 `door-lock-daemon.py`의 포트도 함께 변경해야 합니다.
- Chromium 플래그 수정 시 주석 없는 줄만 추가합니다 (`\` 연속 사용 시 trailing space 주의).

### `stop-door-lock.sh`

- **독립 실행 가능**: `start-door-lock.sh` 없이도 단독으로 실행할 수 있어야 합니다.
- `DAEMON_DIR` 환경변수로 데몬 경로를 받으므로, 경로가 바뀌어도 이 스크립트 자체는 수정하지 않아도 됩니다.
- `pgrep` 패턴은 프로세스를 정확히 식별할 수 있도록 충분히 구체적으로 작성합니다.

### `door-lock-daemon.py`

- `/health` 엔드포인트는 반드시 유지합니다. `start-door-lock.sh`의 폴링이 이 엔드포인트에 의존합니다.
- GPIO 핀 번호 변경 시 `door-lock-daemon.py` 내 상수만 수정하면 됩니다.
- Flask 포트는 `start-door-lock.sh`의 `DAEMON_PORT`와 항상 동일해야 합니다.
