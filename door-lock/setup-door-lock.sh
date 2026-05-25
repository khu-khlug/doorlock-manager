#!/bin/bash
# 초기 가정: Raspberry Pi Imager로 Raspbian 64-bit Lite 설치 직후 상태
# 실행 방법: 이 스크립트를 내려받아 실행하면 나머지 파일을 자동으로 설치

set -e

REPO_RAW="https://raw.githubusercontent.com/khu-khlug/sight-infra/main/door-lock"
SETUP_USER="${SUDO_USER:-$(whoami)}"
SETUP_DIR="$(cd "$(dirname "$0")" && pwd)"
KIOSK_USER="kiosk"
KIOSK_HOME="/home/${KIOSK_USER}"
DOOR_LOCK_GROUP="door-lock"
DAEMON_SVC_USER="door-lock-svc"
PWA_ORIGIN="https://app.khlug.org"
PWA_URL="${PWA_ORIGIN}/door-lock"
BACKEND_URL="https://api-v2.khlug.org"

# ── 1. 시스템 패키지 설치 ─────────────────────────────────────────────────────
echo "[1/10] 시스템 패키지 설치 중..."
sudo apt-get update -qq
sudo apt-get install -y \
    xserver-xorg \
    xinit \
    x11-xserver-utils \
    chromium \
    unclutter \
    python3-flask \
    python3-gpiozero \
    python3-requests \
    fonts-nanum \
    locales

sudo sed -i 's/^# *ko_KR.UTF-8/ko_KR.UTF-8/' /etc/locale.gen
sudo locale-gen ko_KR.UTF-8
sudo update-locale LANG=ko_KR.UTF-8 LC_ALL=ko_KR.UTF-8

sudo tee /etc/fonts/conf.d/99-nanum-default.conf > /dev/null << 'EOF'
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <alias>
    <family>sans-serif</family>
    <prefer>
      <family>NanumGothic</family>
    </prefer>
  </alias>
</fontconfig>
EOF
sudo fc-cache -f

# ── 2. 사용자 및 그룹 설정 ────────────────────────────────────────────────────
echo "[2/10] 사용자 및 그룹 설정 중..."

# door-lock 그룹 생성
if ! getent group "$DOOR_LOCK_GROUP" > /dev/null; then
    sudo groupadd "$DOOR_LOCK_GROUP"
    echo "  그룹 생성: ${DOOR_LOCK_GROUP}"
else
    echo "  그룹 이미 존재: ${DOOR_LOCK_GROUP}"
fi

# kiosk 사용자 생성 (sudo 없음, door-lock 기본 그룹, 홈 디렉토리 = 파일 디렉토리)
if ! id "$KIOSK_USER" > /dev/null 2>&1; then
    sudo useradd \
        --create-home \
        --home-dir "$KIOSK_HOME" \
        --shell /bin/bash \
        --gid "$DOOR_LOCK_GROUP" \
        --no-user-group \
        "$KIOSK_USER"
    echo "  사용자 생성: ${KIOSK_USER} (홈: ${KIOSK_HOME})"
else
    echo "  사용자 이미 존재: ${KIOSK_USER}"
fi

# kiosk에 필요한 그룹 추가 (video, audio)
for grp in video audio; do
    if getent group "$grp" > /dev/null; then
        sudo usermod -aG "$grp" "$KIOSK_USER"
    fi
done
echo "  ${KIOSK_USER}: video, audio 그룹 추가"

# door-lock-svc 사용자 생성 (시스템 계정, 데몬 전용)
if ! id "$DAEMON_SVC_USER" > /dev/null 2>&1; then
    sudo useradd \
        --system \
        --no-create-home \
        --shell /usr/sbin/nologin \
        "$DAEMON_SVC_USER"
    echo "  사용자 생성: ${DAEMON_SVC_USER}"
else
    echo "  사용자 이미 존재: ${DAEMON_SVC_USER}"
fi

# door-lock-svc에 필요한 그룹 추가 (gpio, door-lock)
for grp in gpio "$DOOR_LOCK_GROUP"; do
    if getent group "$grp" > /dev/null; then
        sudo usermod -aG "$grp" "$DAEMON_SVC_USER"
    fi
done
echo "  ${DAEMON_SVC_USER}: gpio, ${DOOR_LOCK_GROUP} 그룹 추가"

# setup 실행 사용자에게도 door-lock 그룹 부여
if ! groups "$SETUP_USER" | grep -q "\b${DOOR_LOCK_GROUP}\b"; then
    sudo usermod -aG "$DOOR_LOCK_GROUP" "$SETUP_USER"
    echo "  ${SETUP_USER}: ${DOOR_LOCK_GROUP} 그룹 추가"
else
    echo "  ${SETUP_USER}: ${DOOR_LOCK_GROUP} 그룹 이미 설정됨"
fi

# ── 3. 스크립트 다운로드 (kiosk 홈 = 파일 디렉토리) ──────────────────────────
echo "[3/10] 스크립트 다운로드 중..."

for file in setup-door-lock.sh start-door-lock.sh stop-door-lock.sh door-lock-daemon.py README.md; do
    sudo rm -f "${KIOSK_HOME}/${file}"
    sudo curl -fsSL "${REPO_RAW}/${file}" -o "${KIOSK_HOME}/${file}"
    echo "  다운로드 완료: ${file}"
done

sudo chown -R "${KIOSK_USER}:${DOOR_LOCK_GROUP}" "$KIOSK_HOME"
sudo chmod 770 "$KIOSK_HOME"
sudo chmod 770 "${KIOSK_HOME}/setup-door-lock.sh"
sudo chmod 770 "${KIOSK_HOME}/start-door-lock.sh"
sudo chmod 770 "${KIOSK_HOME}/stop-door-lock.sh"
sudo chmod 770 "${KIOSK_HOME}/door-lock-daemon.py"
sudo chmod 660 "${KIOSK_HOME}/README.md"

# ── 4. API 키 설정 ────────────────────────────────────────────────────────────
echo "[4/10] API 키 설정 중..."

KEY_SRC="${SETUP_DIR}/internal-api-key"
API_KEY_DIR="/etc/door-lock"
API_KEY_FILE="${API_KEY_DIR}/api-key"

if [ -f "$KEY_SRC" ]; then
    API_KEY=$(cat "$KEY_SRC")
    echo "  internal-api-key 파일에서 읽음"
else
    API_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    echo "$API_KEY" > "$KEY_SRC"
    echo "  API 키 생성 및 저장: ${KEY_SRC}"
fi

sudo mkdir -p "$API_KEY_DIR"
sudo chown root:"$DAEMON_SVC_USER" "$API_KEY_DIR"
sudo chmod 750 "$API_KEY_DIR"
echo "$API_KEY" | sudo tee "$API_KEY_FILE" > /dev/null
sudo chown root:"$DAEMON_SVC_USER" "$API_KEY_FILE"
sudo chmod 640 "$API_KEY_FILE"
echo "  API 키 설정 완료: ${API_KEY_FILE}"
echo ""
echo "  !! 백엔드 서버의 INTERNAL_API_KEY 환경변수를 아래 값으로 설정하세요:"
echo "     $(cat "$KEY_SRC")"
echo ""

# ── 5. PWA 설치 정책 설정 ─────────────────────────────────────────────────────
echo "[5/10] PWA 설치 정책 설정 중..."
sudo rm -rf "${KIOSK_HOME}/.config/chromium"
echo "  Chromium 프로필 초기화 (PWA 캐시 및 로컬 스토리지 삭제)"
sudo mkdir -p /etc/chromium/policies/managed
sudo tee /etc/chromium/policies/managed/pwa_install.json > /dev/null << EOF
{
  "WebAppInstallForceList": [
    {
      "url": "${PWA_URL}",
      "default_launch_container": "window"
    }
  ],
  "InsecurePrivateNetworkRequestsAllowedForUrls": [
    "${PWA_ORIGIN}"
  ],
  "LocalNetworkAccessAllowedForUrls": [
    "${PWA_ORIGIN}"
  ],
  "PrivateNetworkAccessRestrictionsEnabled": false,
  "DeveloperToolsAvailability": 2,
  "TranslateEnabled": false
}
EOF

# ── 6. 자동 로그인 설정 (kiosk 사용자) ───────────────────────────────────────
echo "[6/10] 자동 로그인 설정 중..."
GETTY_CONF="/etc/systemd/system/getty@tty1.service.d/autologin.conf"
sudo mkdir -p "$(dirname "$GETTY_CONF")"
sudo tee "$GETTY_CONF" > /dev/null << EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin ${KIOSK_USER} --noclear %I \$TERM
EOF
sudo systemctl daemon-reload
sudo systemctl enable getty@tty1.service

# ── 7. X 세션 자동 시작 설정 (kiosk 홈 디렉토리) ─────────────────────────────
echo "[7/10] X 세션 자동 시작 설정 중..."

# kiosk의 .bashrc: tty1 로그인 시 startx 실행
BASHRC_MARK="# door-lock: auto startx"
if ! sudo grep -qF "$BASHRC_MARK" "${KIOSK_HOME}/.bashrc" 2>/dev/null; then
    sudo tee -a "${KIOSK_HOME}/.bashrc" > /dev/null << 'EOF'

# door-lock: auto startx
if [ "$(tty)" = "/dev/tty1" ]; then
    /home/kiosk/start-door-lock.sh
fi
EOF
    echo "  .bashrc에 startx 설정 추가"
else
    echo "  .bashrc 이미 설정됨, 건너뜀"
fi

# kiosk의 .xinitrc: X 세션 시작 시 start-door-lock.sh 실행
sudo tee "${KIOSK_HOME}/.xinitrc" > /dev/null << EOF
#!/bin/bash
exec "${KIOSK_HOME}/start-door-lock.sh"
EOF
sudo chmod +x "${KIOSK_HOME}/.xinitrc"
sudo chown "${KIOSK_USER}:${DOOR_LOCK_GROUP}" "${KIOSK_HOME}/.bashrc" "${KIOSK_HOME}/.xinitrc"
echo "  .xinitrc 설정 완료"

# ── 8. 디스플레이 절전 cron 등록 ─────────────────────────────────────────────
echo "[8/10] 디스플레이 절전 cron 등록 중..."
CRON_MARK="# door-lock: display power"
if ! sudo crontab -u "$KIOSK_USER" -l 2>/dev/null | grep -qF "$CRON_MARK"; then
    (sudo crontab -u "$KIOSK_USER" -l 2>/dev/null; \
     echo "$CRON_MARK"; \
     echo "0 9  * * * DISPLAY=:0 xset s off; DISPLAY=:0 xset -dpms"; \
     echo "0 21 * * * DISPLAY=:0 xset s 300 300; DISPLAY=:0 xset dpms 300 300 300") \
    | sudo crontab -u "$KIOSK_USER" -
    echo "  cron 등록 완료 (09:00 절전 해제 / 21:00 절전 활성화)"
else
    echo "  cron 이미 등록됨, 건너뜀"
fi

# ── 9. 데몬 systemd 서비스 등록 ──────────────────────────────────────────────
echo "[9/10] 데몬 systemd 서비스 등록 중..."
SERVICE_FILE="/etc/systemd/system/door-lock-daemon.service"
sudo tee "$SERVICE_FILE" > /dev/null << EOF
[Unit]
Description=Door Lock Daemon
After=network.target

[Service]
Type=simple
User=${DAEMON_SVC_USER}
Environment=BACKEND_URL=${BACKEND_URL}
ExecStart=python3 ${KIOSK_HOME}/door-lock-daemon.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable door-lock-daemon.service
sudo systemctl restart door-lock-daemon.service
echo "  door-lock-daemon.service 등록 및 시작 완료"

# ── 10. GPIO 권한 확인 ────────────────────────────────────────────────────────
echo "[10/10] GPIO 권한 확인 중..."
if ! groups "$DAEMON_SVC_USER" | grep -q '\bgpio\b'; then
    echo "  경고: ${DAEMON_SVC_USER}에 gpio 그룹이 없습니다." >&2
else
    echo "  gpio 권한 정상"
fi

echo ""
echo "설치 완료. 재부팅하면 도어락 키오스크가 자동 시작됩니다."
echo "  재부팅: sudo reboot"
