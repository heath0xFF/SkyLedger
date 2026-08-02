#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${SKYLEDGER_APP_DIR:-/opt/skyledger}"
CONFIG_DIR="${SKYLEDGER_CONFIG_DIR:-/etc/skyledger}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
APP_USER="${SKYLEDGER_USER:-${SUDO_USER:-${USER:-pi}}}"
APP_GROUP="${SKYLEDGER_GROUP:-${APP_USER}}"
SOURCE_DIR="$(pwd -P)"

echo "Installing SkyLedger to ${APP_DIR}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "Python 3 is required."
  exit 1
fi

sudo mkdir -p "${APP_DIR}" "${CONFIG_DIR}"
sudo rsync -a --delete \
  --exclude ".git" \
  --exclude ".venv" \
  --exclude "venv" \
  --exclude "__pycache__" \
  --exclude ".pytest_cache" \
  --exclude ".env" \
  --exclude ".env.*" \
  --exclude "*.log" \
  --exclude "*.db" \
  --exclude "*.sqlite" \
  --exclude "*.sqlite3" \
  --exclude "*.pem" \
  --exclude "*.key" \
  --exclude "*.p12" \
  --exclude "*.pfx" \
  --exclude "*.bak" \
  --exclude "*.backup" \
  --exclude "*.dump" \
  --exclude "config.local.yaml" \
  --exclude "config.*.local.yaml" \
  --exclude "*.private.yaml" \
  ./ "${APP_DIR}/"
sudo chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}"

cd "${APP_DIR}"
"${PYTHON_BIN}" -m venv venv
./venv/bin/python -m pip install --upgrade pip
./venv/bin/python -m pip install -r requirements.txt

if [ ! -f "${CONFIG_DIR}/config.yaml" ]; then
  sudo cp config.example.yaml "${CONFIG_DIR}/config.yaml"
  echo "Created ${CONFIG_DIR}/config.yaml"
fi
sudo chown "${APP_USER}:${APP_GROUP}" "${CONFIG_DIR}" "${CONFIG_DIR}/config.yaml"
sudo chmod 750 "${CONFIG_DIR}"
sudo chmod 640 "${CONFIG_DIR}/config.yaml"

SKYLEDGER_CONFIG="${CONFIG_DIR}/config.yaml" ./venv/bin/python - <<'PY'
from skyledger.config import load_config
from skyledger.db import Database

config = load_config()
Database(config.database_path).init()
print(f"Initialized SQLite database at {config.database_path}")
PY

if [ "${INSTALL_SERVICE:-0}" = "1" ]; then
  sudo cp skyledger.service /etc/systemd/system/skyledger.service
  sudo sed -i \
    -e "s|^User=.*|User=${APP_USER}|" \
    -e "s|^Group=.*|Group=${APP_GROUP}|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=${APP_DIR}|" \
    -e "s|^ExecStart=.*|ExecStart=${APP_DIR}/venv/bin/python -m skyledger --host 0.0.0.0|" \
    /etc/systemd/system/skyledger.service
  sudo systemctl daemon-reload
  sudo systemctl enable skyledger.service
  echo "Installed systemd service. Start it with: sudo systemctl start skyledger"
else
  echo "Service not installed. Run with INSTALL_SERVICE=1 ./install.sh to install it."
fi

if [ "${INSTALL_HEALTHCHECK:-0}" = "1" ]; then
  sudo cp skyledger-healthcheck.service /etc/systemd/system/skyledger-healthcheck.service
  sudo cp skyledger-healthcheck.timer /etc/systemd/system/skyledger-healthcheck.timer
  sudo sed -i \
    -e "s|^ExecStart=.*|ExecStart=/bin/bash ${APP_DIR}/scripts/pi-healthcheck.sh|" \
    /etc/systemd/system/skyledger-healthcheck.service
  sudo systemctl daemon-reload
  sudo systemctl enable --now skyledger-healthcheck.timer
  echo "Installed SkyLedger healthcheck timer."
fi

if [ "${INSTALL_UPDATE_SERVICE:-0}" = "1" ]; then
  sudo cp skyledger-update.service /etc/systemd/system/skyledger-update.service
  sudo cp skyledger-update.timer /etc/systemd/system/skyledger-update.timer
  sudo sed -i \
    -e "s|/home/pi/SkyLedger|${SOURCE_DIR}|g" \
    -e "s|^Environment=SKYLEDGER_USER=.*|Environment=SKYLEDGER_USER=${APP_USER}|" \
    /etc/systemd/system/skyledger-update.service
  sudo systemctl daemon-reload
  sudo systemctl enable skyledger-update.service
  sudo systemctl enable --now skyledger-update.timer
  echo "Installed SkyLedger Git update service and timer."
fi

cat <<EOF

Next steps:
1. Edit ${CONFIG_DIR}/config.yaml with home_lat, home_lon, ADS-B JSON path, and optional Discord webhook.
2. Start manually:
   cd ${APP_DIR}
   SKYLEDGER_CONFIG=${CONFIG_DIR}/config.yaml ./venv/bin/python -m skyledger
3. Open:
   http://localhost:8000
EOF
