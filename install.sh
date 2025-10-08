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
  python3-pil python3-pil.imagetk python3-tk \
  python3-rpi.gpio python3-smbus i2c-tools \
  libopencv-dev python3-numpy rsync xauth

echo "=== Configure SSH ==="
sed -i 's/^#\?X11Forwarding.*/X11Forwarding yes/' /etc/ssh/sshd_config
sed -i 's/^#\?X11UseLocalhost.*/X11UseLocalhost yes/' /etc/ssh/sshd_config
systemctl restart ssh

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
pip3 install Pillow --break-system-packages
pip3 install pandas --break-system-packages
pip3 install openpyxl --break-system-packages
pip3 install opencv-python --break-system-packages
pip3 install opencv-contrib-python --break-system-packages

echo "=== Done ==="
echo "-> Logs: ${LOG_DIR}/app.log"
echo "-> State: ${STATE_DIR}"
#echo "-> Service: systemctl status ${APP_NAME}.service"

