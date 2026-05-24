#!/bin/bash
# 초기 가정: Raspberry Pi Imager로 Raspbian 64-bit Lite 설치 직후 상태
# 실행 방법: 이 스크립트를 내려받아 실행하면 나머지 파일을 자동으로 설치

set -e

REPO_RAW="https://raw.githubusercontent.com/khu-khlug/sight-infra/main/door-lock"
SETUP_USER="${SUDO_USER:-$(whoami)}"
KIOSK_USER="kiosk"
KIOSK_HOME="/home/${KIOSK_USER}"
DOOR_LOCK_GROUP="door-lock"
PWA_URL="https://app.khlug.org/door-lock"

# ── 1. 시스템 패키지 설치 ─────────────────────────────────────────────────────
echo "[1/7] 시스템 패키지 설치 중..."
sudo apt-get update -qq
sudo apt-get install -y \
    xserver-xorg \
    xinit \
    x11-xserver-utils \
    chromium \
    unclutter \
    python3-flask \
    python3-gpiozero

# ── 2. 사용자 및 그룹 설정 ────────────────────────────────────────────────────
echo "[2/7] 사용자 및 그룹 설정 중..."

# door-lock 그룹 생성
if ! getent group "$DOOR_LOCK_GROUP" > /dev/null; then
    sudo groupadd "$DOOR_LOCK_GROUP"
    echo "  그룹 생성: ${DOOR_LOCK_GROUP}"
else
    echo "  그룹 이미 존재: ${DOOR_LOCK_GROUP}"
fi

# kiosk 사용자 생성 (sudo 없음, door-lock 기본 그룹, 홈 디렉토리 = 데몬 디렉토리)
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

# kiosk에 필요한 그룹 추가 (gpio, video, audio)
for grp in gpio video audio; do
    if getent group "$grp" > /dev/null; then
        sudo usermod -aG "$grp" "$KIOSK_USER"
    fi
done
echo "  ${KIOSK_USER}: gpio, video, audio 그룹 추가"

# setup 실행 사용자에게도 door-lock 그룹 부여
if ! groups "$SETUP_USER" | grep -q "\b${DOOR_LOCK_GROUP}\b"; then
    sudo usermod -aG "$DOOR_LOCK_GROUP" "$SETUP_USER"
    echo "  ${SETUP_USER}: ${DOOR_LOCK_GROUP} 그룹 추가"
else
    echo "  ${SETUP_USER}: ${DOOR_LOCK_GROUP} 그룹 이미 설정됨"
fi

# ── 3. 스크립트 다운로드 (kiosk 홈 = 데몬 디렉토리) ──────────────────────────
echo "[3/7] 스크립트 다운로드 중..."

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

# ── 4. PWA 설치 정책 설정 ─────────────────────────────────────────────────────
echo "[4/7] PWA 설치 정책 설정 중..."
sudo mkdir -p /etc/chromium/policies/managed
sudo tee /etc/chromium/policies/managed/pwa_install.json > /dev/null << EOF
{
  "WebAppInstallForceList": [
    {
      "url": "${PWA_URL}",
      "default_launch_container": "window"
    }
  ]
}
EOF

# ── 5. 자동 로그인 설정 (kiosk 사용자) ───────────────────────────────────────
echo "[5/7] 자동 로그인 설정 중..."
GETTY_CONF="/etc/systemd/system/getty@tty1.service.d/autologin.conf"
sudo mkdir -p "$(dirname "$GETTY_CONF")"
sudo tee "$GETTY_CONF" > /dev/null << EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin ${KIOSK_USER} --noclear %I \$TERM
EOF
sudo systemctl daemon-reload
sudo systemctl enable getty@tty1.service

# ── 6. X 세션 자동 시작 설정 (kiosk 홈 디렉토리) ─────────────────────────────
echo "[6/7] X 세션 자동 시작 설정 중..."

# kiosk의 .bashrc: tty1 로그인 시 startx 실행
BASHRC_MARK="# door-lock: auto startx"
if ! sudo grep -qF "$BASHRC_MARK" "${KIOSK_HOME}/.bashrc" 2>/dev/null; then
    sudo tee -a "${KIOSK_HOME}/.bashrc" > /dev/null << 'EOF'

# door-lock: auto startx
if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    startx
    sleep infinity
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

# ── 7. 디스플레이 절전 cron 등록 ─────────────────────────────────────────────
echo "[7/8] 디스플레이 절전 cron 등록 중..."
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

# ── 8. GPIO 권한 확인 ─────────────────────────────────────────────────────────
echo "[8/8] GPIO 권한 확인 중..."
if ! groups "$KIOSK_USER" | grep -q '\bgpio\b'; then
    echo "  경고: gpio 그룹이 없습니다. python3-rpi.gpio 또는 python3-gpiozero 설치를 확인하세요." >&2
else
    echo "  gpio 권한 정상"
fi

echo ""
echo "설치 완료. 재부팅하면 도어락 키오스크가 자동 시작됩니다."
echo "  재부팅: sudo reboot"
