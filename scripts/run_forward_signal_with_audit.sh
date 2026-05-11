#!/usr/bin/env bash
set -u -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
DATA_ROOT="${DATA_ROOT:-data/forward-paper}"
CONFIG_PATH="${CONFIG_PATH:-configs/volume_alpha.default.yaml}"
FORWARD_WORKERS="${FORWARD_WORKERS:-}"
FORWARD_SIGNAL_SLEEVES="${FORWARD_SIGNAL_SLEEVES:-stage4_selected}"
DEMO_ENTRY_SLEEVES="${DEMO_ENTRY_SLEEVES:-stage4_selected}"
DEMO_ENTRY_LEVERAGE="${DEMO_ENTRY_LEVERAGE:-1}"
DEMO_USE_WALLET_BALANCE="${DEMO_USE_WALLET_BALANCE:-1}"
DEMO_MAX_ORDER_NOTIONAL="${DEMO_MAX_ORDER_NOTIONAL:-0}"
DEMO_MAX_TOTAL_NEW_NOTIONAL="${DEMO_MAX_TOTAL_NEW_NOTIONAL:-0}"
DEMO_MAX_ORDER_NOTIONAL_PCT_EQUITY="${DEMO_MAX_ORDER_NOTIONAL_PCT_EQUITY:-0.10}"
DEMO_MAX_TOTAL_NEW_NOTIONAL_PCT_EQUITY="${DEMO_MAX_TOTAL_NEW_NOTIONAL_PCT_EQUITY:-1.0}"
FAST_PROTECTION_SECONDS=55

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

cycle_args=(
  --data-root "$DATA_ROOT"
  --config "$CONFIG_PATH"
  bybit-demo-cycle
  --submit-orders
  --i-understand-demo-sync
  --demo-entry-sleeves "$DEMO_ENTRY_SLEEVES"
  --entry-leverage "$DEMO_ENTRY_LEVERAGE"
  --max-order-notional "$DEMO_MAX_ORDER_NOTIONAL"
  --max-total-new-notional "$DEMO_MAX_TOTAL_NEW_NOTIONAL"
  --max-order-notional-pct-equity "$DEMO_MAX_ORDER_NOTIONAL_PCT_EQUITY"
  --max-total-new-notional-pct-equity "$DEMO_MAX_TOTAL_NEW_NOTIONAL_PCT_EQUITY"
  --forward-mode open-from-scan
  --require-first-slice
  --fast-protection-seconds "$FAST_PROTECTION_SECONDS"
)
if [[ "$DEMO_USE_WALLET_BALANCE" == "1" || "$DEMO_USE_WALLET_BALANCE" == "true" || "$DEMO_USE_WALLET_BALANCE" == "yes" ]]; then
  cycle_args+=(--use-wallet-balance)
fi

audit_args=(
  --data-root "$DATA_ROOT"
  --config "$CONFIG_PATH"
  forward-audit
  --telegram
)

"$PYTHON_BIN" -m aggression_carry "${signal_args[@]}"
signal_status=$?

cycle_status=0
if [[ "$signal_status" -eq 0 ]]; then
  "$PYTHON_BIN" -m aggression_carry "${cycle_args[@]}"
  cycle_status=$?
fi

"$PYTHON_BIN" -m aggression_carry "${audit_args[@]}"
audit_status=$?

if [[ "$signal_status" -ne 0 ]]; then
  exit "$signal_status"
fi
if [[ "$cycle_status" -ne 0 ]]; then
  exit "$cycle_status"
fi
exit "$audit_status"
