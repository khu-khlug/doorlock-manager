#!/bin/bash
# Assumptions: Fresh Raspbian 64-bit Lite installed via Raspberry Pi Imager
# Usage: Download this script and run it to install all required files

set -e

read -rp "Enter room number (3 digits): " ROOM_NUMBER

REPO_RAW="https://raw.githubusercontent.com/khu-khlug/doorlock-manager/dev"
SETUP_USER="${SUDO_USER:-$(whoami)}"
SETUP_DIR="$(cd "$(dirname "$0")" && pwd)"
KIOSK_USER="kiosk"
KIOSK_HOME="/home/${KIOSK_USER}"
DOOR_LOCK_GROUP="door-lock"
DAEMON_SVC_USER="door-lock-svc"
PWA_ORIGIN="https://feat-door-lock.khlug-dev.pages.dev"
PWA_URL="${PWA_ORIGIN}/door-lock"
BACKEND_URL="https://api.dev.khlugy.app"

# ── 1. Install system packages ────────────────────────────────────────────────
echo "[1/10] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y \
    xserver-xorg \
    xinit \
    x11-xserver-utils \
    xinput \
    chromium \
    unclutter \
    python3-flask \
    python3-gpiozero \
    python3-requests \
    fonts-nanum

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

# ── 2. User and group setup ───────────────────────────────────────────────────
echo "[2/10] Setting up users and groups..."

if ! getent group "$DOOR_LOCK_GROUP" > /dev/null; then
    sudo groupadd "$DOOR_LOCK_GROUP"
    echo "  Group created: ${DOOR_LOCK_GROUP}"
else
    echo "  Group already exists: ${DOOR_LOCK_GROUP}"
fi

if ! id "$KIOSK_USER" > /dev/null 2>&1; then
    sudo useradd \
        --create-home \
        --home-dir "$KIOSK_HOME" \
        --shell /bin/bash \
        --gid "$DOOR_LOCK_GROUP" \
        --no-user-group \
        "$KIOSK_USER"
    echo "  User created: ${KIOSK_USER} (home: ${KIOSK_HOME})"
else
    echo "  User already exists: ${KIOSK_USER}"
fi

for grp in video audio; do
    if getent group "$grp" > /dev/null; then
        sudo usermod -aG "$grp" "$KIOSK_USER"
    fi
done
echo "  ${KIOSK_USER}: added to video, audio groups"

if ! id "$DAEMON_SVC_USER" > /dev/null 2>&1; then
    sudo useradd \
        --system \
        --no-create-home \
        --shell /usr/sbin/nologin \
        "$DAEMON_SVC_USER"
    echo "  User created: ${DAEMON_SVC_USER}"
else
    echo "  User already exists: ${DAEMON_SVC_USER}"
fi

for grp in gpio "$DOOR_LOCK_GROUP"; do
    if getent group "$grp" > /dev/null; then
        sudo usermod -aG "$grp" "$DAEMON_SVC_USER"
    fi
done
echo "  ${DAEMON_SVC_USER}: added to gpio, ${DOOR_LOCK_GROUP} groups"

if ! groups "$SETUP_USER" | grep -q "\b${DOOR_LOCK_GROUP}\b"; then
    sudo usermod -aG "$DOOR_LOCK_GROUP" "$SETUP_USER"
    echo "  ${SETUP_USER}: added to ${DOOR_LOCK_GROUP} group"
else
    echo "  ${SETUP_USER}: already in ${DOOR_LOCK_GROUP} group"
fi

# ── 3. Download scripts ───────────────────────────────────────────────────────
echo "[3/10] Downloading scripts..."

for file in setup-door-lock.sh start-door-lock.sh stop-door-lock.sh door-lock-daemon.py README.md; do
    sudo rm -f "${KIOSK_HOME}/${file}"
    sudo curl -fsSL "${REPO_RAW}/${file}" -o "${KIOSK_HOME}/${file}"
    echo "  Downloaded: ${file}"
done

sudo chown -R "${KIOSK_USER}:${DOOR_LOCK_GROUP}" "$KIOSK_HOME"
sudo chmod 770 "$KIOSK_HOME"
sudo chmod 770 "${KIOSK_HOME}/setup-door-lock.sh"
sudo chmod 770 "${KIOSK_HOME}/start-door-lock.sh"
sudo chmod 770 "${KIOSK_HOME}/stop-door-lock.sh"
sudo chmod 770 "${KIOSK_HOME}/door-lock-daemon.py"
sudo chmod 660 "${KIOSK_HOME}/README.md"

# ── 4. API key setup ──────────────────────────────────────────────────────────
echo "[4/10] Setting up API key..."

KEY_SRC="${SETUP_DIR}/internal-api-key"
API_KEY_DIR="/etc/door-lock"
API_KEY_FILE="${API_KEY_DIR}/api-key"

if [ -f "$KEY_SRC" ]; then
    API_KEY=$(cat "$KEY_SRC")
    echo "  Read from internal-api-key file"
else
    API_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    echo "$API_KEY" > "$KEY_SRC"
    echo "  API key generated and saved: ${KEY_SRC}"
fi

sudo mkdir -p "$API_KEY_DIR"
sudo chown root:"$DAEMON_SVC_USER" "$API_KEY_DIR"
sudo chmod 750 "$API_KEY_DIR"
echo "$API_KEY" | sudo tee "$API_KEY_FILE" > /dev/null
sudo chown root:"$DAEMON_SVC_USER" "$API_KEY_FILE"
sudo chmod 640 "$API_KEY_FILE"
echo "  API key saved: ${API_KEY_FILE}"
echo ""
echo "  !! Set the backend INTERNAL_API_KEY env var to:"
echo "     $(cat "$KEY_SRC")"
echo ""

# ── 5. PWA install policy ─────────────────────────────────────────────────────
echo "[5/10] Configuring PWA install policy..."
sudo rm -rf "${KIOSK_HOME}/.config/chromium"
echo "  Chromium profile reset (PWA cache and local storage cleared)"
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

# ── 6. Auto-login setup ───────────────────────────────────────────────────────
echo "[6/10] Configuring auto-login..."
GETTY_CONF="/etc/systemd/system/getty@tty1.service.d/autologin.conf"
sudo mkdir -p "$(dirname "$GETTY_CONF")"
sudo tee "$GETTY_CONF" > /dev/null << EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin ${KIOSK_USER} --noclear %I \$TERM
EOF
sudo systemctl daemon-reload
sudo systemctl enable getty@tty1.service

# ── 7. X session auto-start ───────────────────────────────────────────────────
echo "[7/10] Configuring X session auto-start..."

BASHRC_MARK="# door-lock: auto startx"
if ! sudo grep -qF "$BASHRC_MARK" "${KIOSK_HOME}/.bashrc" 2>/dev/null; then
    sudo tee -a "${KIOSK_HOME}/.bashrc" > /dev/null << 'EOF'

# door-lock: auto startx
if [ "$(tty)" = "/dev/tty1" ]; then
    /home/kiosk/start-door-lock.sh
fi
EOF
    echo "  Added startx to .bashrc"
else
    echo "  .bashrc already configured, skipping"
fi

sudo tee "${KIOSK_HOME}/.xinitrc" > /dev/null << EOF
#!/bin/bash
exec "${KIOSK_HOME}/start-door-lock.sh"
EOF
sudo chmod +x "${KIOSK_HOME}/.xinitrc"
sudo chown "${KIOSK_USER}:${DOOR_LOCK_GROUP}" "${KIOSK_HOME}/.bashrc" "${KIOSK_HOME}/.xinitrc"
echo "  .xinitrc configured"

# ── 8. Display sleep cron ─────────────────────────────────────────────────────
echo "[8/10] Registering display sleep cron..."
CRON_MARK="# door-lock: display power"
if ! sudo crontab -u "$KIOSK_USER" -l 2>/dev/null | grep -qF "$CRON_MARK"; then
    (sudo crontab -u "$KIOSK_USER" -l 2>/dev/null; \
     echo "$CRON_MARK"; \
     echo "0 9  * * * DISPLAY=:0 xset s off; DISPLAY=:0 xset -dpms"; \
     echo "0 21 * * * DISPLAY=:0 xset s 300 300; DISPLAY=:0 xset dpms 300 300 300") \
    | sudo crontab -u "$KIOSK_USER" -
    echo "  Cron registered (sleep off at 09:00 / sleep on at 21:00)"
else
    echo "  Cron already registered, skipping"
fi

# ── 9. Register daemon systemd service ────────────────────────────────────────
echo "[9/10] Registering daemon systemd service..."
SERVICE_FILE="/etc/systemd/system/door-lock-daemon.service"
sudo tee "$SERVICE_FILE" > /dev/null << EOF
[Unit]
Description=Door Lock Daemon
After=network.target

[Service]
Type=simple
User=${DAEMON_SVC_USER}
Environment=BACKEND_URL=${BACKEND_URL}
Environment=ROOM_NUMBER=${ROOM_NUMBER}
ExecStart=python3 ${KIOSK_HOME}/door-lock-daemon.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable door-lock-daemon.service
sudo systemctl restart door-lock-daemon.service
echo "  door-lock-daemon.service registered and started"

# ── 10. Check GPIO permissions ────────────────────────────────────────────────
echo "[10/10] Checking GPIO permissions..."
if ! groups "$DAEMON_SVC_USER" | grep -q '\bgpio\b'; then
    echo "  WARNING: ${DAEMON_SVC_USER} is not in the gpio group" >&2
else
    echo "  GPIO permissions OK"
fi

echo ""
echo "Setup complete. Reboot to start the door lock kiosk."
echo "  Reboot: sudo reboot"
