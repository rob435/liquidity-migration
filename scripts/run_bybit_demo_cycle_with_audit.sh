#!/usr/bin/env bash
set -u -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
DATA_ROOT="${DATA_ROOT:-data/forward-paper}"
CONFIG_PATH="${CONFIG_PATH:-configs/volume_alpha.default.yaml}"
DEMO_ENTRY_SLEEVES="${DEMO_ENTRY_SLEEVES:-rank_31_plus}"
DEMO_ENTRY_LEVERAGE="${DEMO_ENTRY_LEVERAGE:-1}"
FAST_PROTECTION_SECONDS=55

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python not found or not executable: $PYTHON_BIN" >&2
  exit 1
fi

cycle_args=(
  --data-root "$DATA_ROOT"
  --config "$CONFIG_PATH"
  bybit-demo-cycle
  --submit-orders
  --i-understand-demo-sync
  --demo-entry-sleeves "$DEMO_ENTRY_SLEEVES"
  --entry-leverage "$DEMO_ENTRY_LEVERAGE"
  --forward-mode open-from-scan
  --require-first-slice
  --fast-protection-seconds "$FAST_PROTECTION_SECONDS"
)

audit_args=(
  --data-root "$DATA_ROOT"
  --config "$CONFIG_PATH"
  forward-audit
  --telegram
)

"$PYTHON_BIN" -m aggression_carry "${cycle_args[@]}"
cycle_status=$?

"$PYTHON_BIN" -m aggression_carry "${audit_args[@]}"
audit_status=$?

if [[ "$cycle_status" -ne 0 ]]; then
  exit "$cycle_status"
fi
exit "$audit_status"
