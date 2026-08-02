#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${SKYLEDGER_REPO_DIR:-/home/pi/SkyLedger}"
REMOTE="${SKYLEDGER_REMOTE:-origin}"
BRANCH="${SKYLEDGER_BRANCH:-main}"
APP_USER="${SKYLEDGER_USER:-pi}"
APP_GROUP="${SKYLEDGER_GROUP:-$APP_USER}"

log() {
  printf '[%s] %s\n' "$(date -Is)" "$*"
}

run_as_app_user() {
  if [[ "$(id -u)" -eq 0 ]]; then
    sudo -H -u "$APP_USER" "$@"
  else
    "$@"
  fi
}

if [[ ! -d "$REPO_DIR/.git" ]]; then
  log "Missing Git checkout: $REPO_DIR"
  exit 1
fi

log "Fetching ${REMOTE}/${BRANCH} in $REPO_DIR"
run_as_app_user git -C "$REPO_DIR" fetch --prune "$REMOTE"

current_branch="$(run_as_app_user git -C "$REPO_DIR" branch --show-current)"
if [[ "$current_branch" != "$BRANCH" ]]; then
  log "Checking out $BRANCH"
  run_as_app_user git -C "$REPO_DIR" checkout "$BRANCH"
fi

local_sha="$(run_as_app_user git -C "$REPO_DIR" rev-parse HEAD)"
remote_sha="$(run_as_app_user git -C "$REPO_DIR" rev-parse "${REMOTE}/${BRANCH}")"

if [[ "$local_sha" == "$remote_sha" ]]; then
  log "SkyLedger is already current at $local_sha"
  exit 0
fi

log "Updating SkyLedger from $local_sha to $remote_sha"
run_as_app_user git -C "$REPO_DIR" merge --ff-only "${REMOTE}/${BRANCH}"

log "Installing updated runtime"
cd "$REPO_DIR"
SKYLEDGER_USER="$APP_USER" SKYLEDGER_GROUP="$APP_GROUP" ./install.sh

if systemctl cat skyledger.service >/dev/null 2>&1; then
  log "Restarting skyledger.service"
  # The update unit is ordered before SkyLedger, so a blocking restart here
  # deadlocks waiting for this oneshot unit to finish.
  systemctl --no-block try-restart skyledger.service
fi

log "Update complete"
