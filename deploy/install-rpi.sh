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

SERIAL_DEV=/dev/serial0
if grep -qa "Raspberry Pi 5" /proc/device-tree/model 2>/dev/null || [ "$(readlink -f /dev/serial0 2>/dev/null)" = /dev/ttyAMA10 ]; then
  # RPi 5: serial0 to złącze debug (ttyAMA10); UART na GPIO14/15 to ttyAMA0
  SERIAL_DEV=/dev/ttyAMA0
  echo "==> Raspberry Pi 5: usługa użyje $SERIAL_DEV (wymaga dtparam=uart0=on w /boot/firmware/config.txt)"
fi
if [ -e "$SERIAL_DEV" ]; then
  echo "==> $SERIAL_DEV istnieje -> $(readlink -f "$SERIAL_DEV")"
  if [ "$(readlink -f "$SERIAL_DEV")" = /dev/ttyS0 ]; then
    echo "UWAGA: to mini-UART (Bluetooth włączony) - nie obsługuje parzystości (8E1). Dla liczników 8E1 dodaj"
    echo "       dtoverlay=disable-bt do /boot/firmware/config.txt, wykonaj: sudo systemctl disable hciuart i restart."
  fi
else
  echo "UWAGA: brak $SERIAL_DEV. Włącz UART: sudo raspi-config -> Interface Options -> Serial Port"
  echo "       (login shell: NIE, hardware serial: TAK), a potem uruchom ponownie Raspberry Pi."
fi

echo "==> Usługa systemd"
sed -e "s#/home/pi/modbus-rpi#$APP_DIR#g" -e "s#^User=pi#User=$USER_NAME#" -e "s#--serial /dev/serial0#--serial $SERIAL_DEV#" \
    "$APP_DIR/deploy/modbus-dash.service" | sudo tee /etc/systemd/system/modbus-dash.service > /dev/null
sudo systemctl daemon-reload
echo
echo "Gotowe. Dostosuj parametry w /etc/systemd/system/modbus-dash.service (ExecStart), potem:"
echo "  sudo systemctl enable --now modbus-dash"
echo "  journalctl -u modbus-dash -f"
