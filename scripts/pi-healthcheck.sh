#!/usr/bin/env bash
set -euo pipefail

SKYLEDGER_URL="${SKYLEDGER_URL:-http://127.0.0.1:8000}"
SKYLEDGER_SERVICE="${SKYLEDGER_SERVICE:-skyledger.service}"
READSB_SERVICE="${READSB_SERVICE:-readsb.service}"
ADSB_MAX_AGE_SECONDS="${ADSB_MAX_AGE_SECONDS:-30}"
FALLBACK_ADSB_SOURCE="${ADSB_JSON_PATH:-http://127.0.0.1/tar1090/data/aircraft.json}"

log() {
  printf '[%s] %s\n' "$(date -Is)" "$*"
}

restart_service() {
  local service="$1"
  if systemctl cat "$service" >/dev/null 2>&1; then
    log "Restarting $service"
    systemctl restart "$service"
  else
    log "Service $service is not installed; skipping restart"
  fi
}

service_is_active() {
  local service="$1"
  systemctl is-active --quiet "$service"
}

source_is_healthy() {
  local source="$1"
  if [[ "$source" =~ ^https?:// ]]; then
    curl -fsS --max-time 5 "$source" >/dev/null
    return $?
  fi

  if [[ ! -f "$source" ]]; then
    log "ADS-B JSON file is missing: $source"
    return 1
  fi

  local mtime now age
  mtime="$(stat -c %Y "$source" 2>/dev/null || echo 0)"
  now="$(date +%s)"
  age=$((now - mtime))

  if (( age > ADSB_MAX_AGE_SECONDS )); then
    log "ADS-B JSON file is stale: $source (${age}s old)"
    return 1
  fi

  return 0
}

if ! service_is_active "$SKYLEDGER_SERVICE"; then
  log "$SKYLEDGER_SERVICE is not active"
  restart_service "$SKYLEDGER_SERVICE"
fi

status_json=""
if ! status_json="$(curl -fsS --max-time 5 "${SKYLEDGER_URL}/api/status" 2>/dev/null)"; then
  log "SkyLedger status endpoint is unreachable: ${SKYLEDGER_URL}/api/status"
  restart_service "$SKYLEDGER_SERVICE"
  exit 0
fi

mapfile -t status_fields < <(
  STATUS_JSON="$status_json" python3 - <<'PY'
import json
import os

try:
    status = json.loads(os.environ.get("STATUS_JSON", "{}"))
except Exception:
    status = {}

print("true" if status.get("receiver_online") else "false")
print(status.get("source") or "")
print("true" if status.get("tracker_running") else "false")
print(int(status.get("tracker_consecutive_errors") or 0))
PY
)

receiver_online="${status_fields[0]:-false}"
source="${status_fields[1]:-$FALLBACK_ADSB_SOURCE}"
source="${source:-$FALLBACK_ADSB_SOURCE}"
tracker_running="${status_fields[2]:-false}"
tracker_errors="${status_fields[3]:-0}"

if [[ "$tracker_running" != "true" ]] || (( tracker_errors >= 5 )); then
  log "SkyLedger tracker is unhealthy (running=${tracker_running}, consecutive_errors=${tracker_errors})"
  restart_service "$SKYLEDGER_SERVICE"
  exit 0
fi

if ! service_is_active "$READSB_SERVICE"; then
  log "$READSB_SERVICE is not active"
  restart_service "$READSB_SERVICE"
  exit 0
fi

if ! source_is_healthy "$source"; then
  restart_service "$READSB_SERVICE"
  exit 0
fi

if [[ "$receiver_online" != "true" ]]; then
  log "SkyLedger reports receiver_online=false even though source exists; restarting $SKYLEDGER_SERVICE"
  restart_service "$SKYLEDGER_SERVICE"
fi
