#!/usr/bin/env bash
set -u -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
DATA_ROOT="${DATA_ROOT:-data/forward-paper}"
CONFIG_PATH="${CONFIG_PATH:-configs/volume_alpha.default.yaml}"
FORWARD_WORKERS="${FORWARD_WORKERS:-}"
FORWARD_SIGNAL_SLEEVES="${FORWARD_SIGNAL_SLEEVES:-stage4_selected}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python not found or not executable: $PYTHON_BIN" >&2
  exit 1
fi

signal_args=(
  --data-root "$DATA_ROOT"
  --config "$CONFIG_PATH"
  forward-run-sleeves
  --forward-mode scan
  --sleeves "$FORWARD_SIGNAL_SLEEVES"
)

if [[ -n "$FORWARD_WORKERS" ]]; then
  signal_args+=(--workers "$FORWARD_WORKERS")
fi

audit_args=(
  --data-root "$DATA_ROOT"
  --config "$CONFIG_PATH"
  forward-audit
  --telegram
)

"$PYTHON_BIN" -m aggression_carry "${signal_args[@]}"
signal_status=$?

"$PYTHON_BIN" -m aggression_carry "${audit_args[@]}"
audit_status=$?

if [[ "$signal_status" -ne 0 ]]; then
  exit "$signal_status"
fi
exit "$audit_status"
