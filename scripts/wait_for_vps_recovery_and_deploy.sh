#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SSH_TARGET="${SSH_TARGET:-root@116.202.15.128}"
SSH_OPTS="${SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=10}"
EXPECTED_COMMIT="${EXPECTED_COMMIT:-$(git rev-parse HEAD)}"
EXPECTED_TELEGRAM_CHAT_ID="${EXPECTED_TELEGRAM_CHAT_ID:-8388367561}"
SYSTEMD_SETTLE_SECONDS="${SYSTEMD_SETTLE_SECONDS:-15}"
WAIT_TIMEOUT_SECONDS="${WAIT_TIMEOUT_SECONDS:-1800}"
WAIT_INTERVAL_SECONDS="${WAIT_INTERVAL_SECONDS:-10}"

for numeric_var in WAIT_TIMEOUT_SECONDS WAIT_INTERVAL_SECONDS SYSTEMD_SETTLE_SECONDS; do
  numeric_value="${!numeric_var}"
  if ! [[ "$numeric_value" =~ ^[0-9]+$ ]]; then
    echo "$numeric_var must be a non-negative integer number of seconds." >&2
    exit 2
  fi
done

echo "waiting for VPS SSH recovery"
echo "ssh_target=$SSH_TARGET expected_commit=$EXPECTED_COMMIT timeout=${WAIT_TIMEOUT_SECONDS}s interval=${WAIT_INTERVAL_SECONDS}s"

start_epoch="$(date +%s)"
deadline_epoch=$((start_epoch + WAIT_TIMEOUT_SECONDS))
attempt=0

while true; do
  attempt=$((attempt + 1))
  # shellcheck disable=SC2086
  if ssh $SSH_OPTS "$SSH_TARGET" "printf 'ssh-ok\n'" >/dev/null 2>&1; then
    echo "ssh-ready attempt=$attempt elapsed=$(($(date +%s) - start_epoch))s"
    break
  fi

  now_epoch="$(date +%s)"
  if [ "$now_epoch" -ge "$deadline_epoch" ]; then
    echo "Timed out waiting for $SSH_TARGET to accept SSH public-key auth." >&2
    echo "Run scripts/print_vps_recovery_command.sh --rescue-only or --recommended-only, then retry." >&2
    exit 255
  fi

  remaining_seconds=$((deadline_epoch - now_epoch))
  sleep_seconds="$WAIT_INTERVAL_SECONDS"
  if [ "$sleep_seconds" -gt "$remaining_seconds" ]; then
    sleep_seconds="$remaining_seconds"
  fi
  echo "ssh-not-ready attempt=$attempt; sleeping ${sleep_seconds}s"
  sleep "$sleep_seconds"
done

EXPECTED_COMMIT="$EXPECTED_COMMIT" \
EXPECTED_TELEGRAM_CHAT_ID="$EXPECTED_TELEGRAM_CHAT_ID" \
SYSTEMD_SETTLE_SECONDS="$SYSTEMD_SETTLE_SECONDS" \
SSH_TARGET="$SSH_TARGET" \
SSH_OPTS="$SSH_OPTS" \
scripts/deploy_vps_live.sh

EXPECTED_COMMIT="$EXPECTED_COMMIT" \
EXPECTED_TELEGRAM_CHAT_ID="$EXPECTED_TELEGRAM_CHAT_ID" \
SYSTEMD_SETTLE_SECONDS="$SYSTEMD_SETTLE_SECONDS" \
SSH_TARGET="$SSH_TARGET" \
SSH_OPTS="$SSH_OPTS" \
scripts/verify_vps_live.sh

echo "wait-deploy-verify-ok commit=${EXPECTED_COMMIT:0:12}"
