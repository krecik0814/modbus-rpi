#!/usr/bin/env bash
# Instalacja Modbus Dash na Raspberry Pi OS (Bookworm i nowsze).
# Użycie:  bash deploy/install-rpi.sh            (z katalogu repozytorium)
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="${SUDO_USER:-$USER}"

echo "==> Katalog aplikacji: $APP_DIR (użytkownik: $USER_NAME)"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip

echo "==> Środowisko wirtualne Pythona (.venv)"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" waitress paho-mqtt

echo "==> Uprawnienia do portu szeregowego (grupa dialout)"
sudo usermod -a -G dialout "$USER_NAME"

if [ -e /dev/serial0 ]; then
  echo "==> /dev/serial0 istnieje -> $(readlink -f /dev/serial0)"
else
  echo "UWAGA: brak /dev/serial0. Włącz UART: sudo raspi-config -> Interface Options -> Serial Port"
  echo "       (login shell: NIE, hardware serial: TAK), a potem uruchom ponownie Raspberry Pi."
fi

echo "==> Usługa systemd"
sed -e "s#/home/pi/modbus-rpi#$APP_DIR#g" -e "s#^User=pi#User=$USER_NAME#" \
    "$APP_DIR/deploy/modbus-dash.service" | sudo tee /etc/systemd/system/modbus-dash.service > /dev/null
sudo systemctl daemon-reload
echo
echo "Gotowe. Dostosuj parametry w /etc/systemd/system/modbus-dash.service (ExecStart), potem:"
echo "  sudo systemctl enable --now modbus-dash"
echo "  journalctl -u modbus-dash -f"
