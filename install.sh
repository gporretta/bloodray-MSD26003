#!/bin/bash
set -euo pipefail

APP_NAME="tool-test"
APP_USER="${SUDO_USER:-${USER}}"
APP_DIR="/opt/${APP_NAME}"
LOG_DIR="/var/log/${APP_NAME}"
STATE_DIR="/var/lib/${APP_NAME}"
PYTHON="/usr/bin/python3"
PIP="${PYTHON} -m pip"

# Require root
if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: This script must be run as root (use: sudo ./install.sh)" >&2
  exit 1
fi

echo "=== Using user: ${APP_USER} ==="
echo "=== App dir: ${APP_DIR} ==="
echo "=== Log dir: ${LOG_DIR} ==="
echo "=== State dir: ${STATE_DIR} ==="

echo "=== Updating package list ==="
apt-get update

echo "=== Enabling I2C interface ==="
raspi-config nonint do_i2c 0 || true

echo "=== Installing system dependencies ==="
apt-get install -y \
  python3 python3-pip python3-tk \
  python3-rpi.gpio python3-smbus i2c-tools \
  rsync

echo "=== Ensuring user is in i2c/gpio groups ==="
usermod -aG i2c "${APP_USER}" || true
usermod -aG gpio "${APP_USER}" || true

echo "=== Creating application, log, and state directories ==="
mkdir -p "${APP_DIR}" "${LOG_DIR}" "${STATE_DIR}"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}" "${LOG_DIR}" "${STATE_DIR}"
chmod -R 775 "${LOG_DIR}" "${STATE_DIR}"

echo "=== Copying project files to ${APP_DIR} ==="
rsync -a --delete --exclude ".git" ./ "${APP_DIR}/"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

echo "=== Installing Python packages ==="
if [[ -f "${APP_DIR}/requirements.txt" ]]; then
  sudo -u "${APP_USER}" bash -lc "${PIP} install --break-system-packages -r '${APP_DIR}/requirements.txt'"
else
  echo "No requirements.txt found at ${APP_DIR}/requirements.txt (skipping)."
fi

echo "=== Creating systemd service ==="
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Tool Test GUI (Tkinter) with ADC + Stepper
After=network-online.target graphical.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${PYTHON} ${APP_DIR}/run.py
Restart=on-failure
RestartSec=3
Environment=DISPLAY=:0
UMask=002

NoNewPrivileges=true

[Install]
WantedBy=graphical.target
EOF

echo "=== Reloading systemd and enabling service ==="
systemctl daemon-reload
systemctl enable "${APP_NAME}.service"

echo "=== Starting service now ==="
systemctl restart "${APP_NAME}.service"

echo "=== Done ==="
echo "-> Logs: ${LOG_DIR}/app.log"
echo "-> State: ${STATE_DIR}"
echo "-> Service: systemctl status ${APP_NAME}.service"

