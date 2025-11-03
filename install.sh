#!/usr/bin/env bash
set -euo pipefail

# =============================
# Config
# =============================
APP_NAME="lumiTest"
SERVICE_NAME="lumiTest.service"
APP_USER="lumi"
APP_HOME="/home/${APP_USER}"
APP_DIR="/opt/${APP_NAME}"
LOG_DIR="/var/log/${APP_NAME}"
STATE_DIR="/var/lib/${APP_NAME}"
VENV_DIR="${APP_DIR}/venv"
PYTHON="${VENV_DIR}/bin/python"
PIP="${VENV_DIR}/bin/pip"

# =============================
# Root check
# =============================
if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: Run as root: sudo bash install.sh" >&2
  exit 1
fi

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  echo "ERROR: user '${APP_USER}' does not exist" >&2
  exit 1
fi

echo "=== Installing for user: ${APP_USER} ==="

# =============================
# System packages
# =============================
echo "=== apt-get update ==="
apt-get update -y

echo "=== Installing system dependencies ==="
# python3-rpi.gpio provides RPi.GPIO (what you asked for)
# python3-smbus/i2c-tools for I2C (if you ever use smbus)
# libopencv-dev helps codecs; opencv Python wheels still installed via pip
# sqlite3/git/rsync/xauth for DB/export/deploy; dbus-user-session for reliable user bus
apt-get install -y \
  python3 python3-venv python3-tk \
  python3-pil python3-pil.imagetk \
  python3-rpi.gpio python3-smbus i2c-tools \
  libopencv-dev python3-numpy python3-opencv\
  sqlite3 git rsync xauth dbus-user-session vim

# =============================
# Raspberry Pi config (I2C)
# =============================
echo "=== Enabling I2C (non-interactive) ==="
if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_i2c 0 || true
fi

# =============================
# Groups / permissions
# =============================
echo "=== Ensuring ${APP_USER} is in i2c/gpio groups ==="
usermod -aG i2c "${APP_USER}" || true
usermod -aG gpio "${APP_USER}" || true

# =============================
# Create dirs
# =============================
echo "=== Creating app, log, and state directories ==="
mkdir -p "${APP_DIR}" "${LOG_DIR}" "${STATE_DIR}"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}" "${LOG_DIR}" "${STATE_DIR}"
chmod -R 775 "${LOG_DIR}" "${STATE_DIR}"

# =============================
# Sync project to /opt
# =============================
echo "=== Copying project files to ${APP_DIR} ==="
# Assumes install.sh is run from the repo root
rsync -a --delete --exclude ".git" ./ "${APP_DIR}/"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

# Sanity check
if [[ ! -f "${APP_DIR}/run.py" ]]; then
  echo "ERROR: ${APP_DIR}/run.py not found. Run this from your project root." >&2
  exit 1
fi

# =============================
# Python virtualenv (inherit system site-packages so RPi.GPIO works)
# =============================
echo "=== Creating virtualenv (with system site packages) at ${VENV_DIR} ==="
if [[ ! -d "${VENV_DIR}" ]]; then
  sudo -u "${APP_USER}" python3 -m venv --system-site-packages "${VENV_DIR}"
fi

echo "=== Upgrading pip/setuptools/wheel ==="
sudo -u "${APP_USER}" "${PIP}" install --upgrade pip wheel setuptools

echo "=== Installing Python packages into venv ==="
# RPi.GPIO is provided by apt and visible due to --system-site-packages.
sudo -u "${APP_USER}" "${PIP}" install \
  Pillow pandas openpyxl \
  opencv-python opencv-contrib-python

# =============================
# Remove any old SYSTEM service (avoid confusion)
# =============================
if [[ -f "/etc/systemd/system/${SERVICE_NAME}" ]]; then
  echo "=== Disabling/removing old SYSTEM service ${SERVICE_NAME} ==="
  systemctl stop "${SERVICE_NAME}" || true
  systemctl disable "${SERVICE_NAME}" || true
  rm -f "/etc/systemd/system/${SERVICE_NAME}"
  systemctl daemon-reload
fi

# =============================
# Create USER service for ${APP_USER}
# =============================
echo "=== Enabling linger for ${APP_USER} (boot without login) ==="
loginctl enable-linger "${APP_USER}" || true

USER_SYSTEMD_DIR="${APP_HOME}/.config/systemd/user"
mkdir -p "${USER_SYSTEMD_DIR}"
chown -R "${APP_USER}:${APP_USER}" "${APP_HOME}/.config"

SERVICE_PATH="${USER_SYSTEMD_DIR}/${SERVICE_NAME}"
echo "=== Writing user service to ${SERVICE_PATH} ==="
cat > "${SERVICE_PATH}" << 'UNIT'
[Unit]
Description=lumiTest GUI (local display, user session)
# Start with the user's graphical session. Works for Wayland or X11.
After=graphical-session.target

[Service]
Type=simple
WorkingDirectory=/opt/lumiTest
ExecStart=/opt/lumiTest/venv/bin/python /opt/lumiTest/run.py
Restart=on-failure
RestartSec=2

# ENV for desktop sessions; DISPLAY=:0 is typical kiosk on Pi.
# XDG_RUNTIME_DIR ensures the user bus/socket is found.
Environment=DISPLAY=:0
Environment=XDG_RUNTIME_DIR=/run/user/%U
# If you're definitely on Wayland and need it, uncomment the next line:
# Environment=WAYLAND_DISPLAY=wayland-0

[Install]
WantedBy=default.target
UNIT

chown "${APP_USER}:${APP_USER}" "${SERVICE_PATH}"

# =============================
# Start the USER service (fixes user bus env)
# =============================
echo "=== Enabling and starting user service for ${APP_USER} ==="
UID_NUM="$(id -u "${APP_USER}")"

# Prefer machinectl (clean user-bus access). Fallback injects env vars if machinectl missing.
if command -v machinectl >/dev/null 2>&1; then
  machinectl shell "${APP_USER}@".host /bin/sh -lc \
    "systemctl --user daemon-reload && systemctl --user enable '${SERVICE_NAME}' && systemctl --user restart '${SERVICE_NAME}' && systemctl --user status '${SERVICE_NAME}' --no-pager || true"
else
  echo "machinectl not found; using env-injected systemctl --user"
  sudo -u "${APP_USER}" XDG_RUNTIME_DIR="/run/user/${UID_NUM}" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${UID_NUM}/bus" systemctl --user daemon-reload || true
  sudo -u "${APP_USER}" XDG_RUNTIME_DIR="/run/user/${UID_NUM}" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${UID_NUM}/bus" systemctl --user enable "${SERVICE_NAME}" || true
  sudo -u "${APP_USER}" XDG_RUNTIME_DIR="/run/user/${UID_NUM}" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${UID_NUM}/bus" systemctl --user restart "${SERVICE_NAME}" || true
  sudo -u "${APP_USER}" XDG_RUNTIME_DIR="/run/user/${UID_NUM}" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${UID_NUM}/bus" systemctl --user status "${SERVICE_NAME}" --no-pager || true
fi

# =============================
# Done
# =============================
echo
echo "=== Installation complete ==="
echo "User service: ${SERVICE_PATH}"
echo
echo "Check status (as ${APP_USER}):"
echo "  systemctl --user status ${SERVICE_NAME}"
