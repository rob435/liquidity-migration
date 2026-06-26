#!/usr/bin/env bash
# Daily BTC-beta hedge run for the continuous demo book (WP3 live wiring).
# Dry-run by default; set SUBMIT_HEDGE=1 (and CONFIRM_DEMO_ORDERS=1) to arm the
# gated submit path. Demo only — never with REAL_MONEY=true.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || command -v python)"
VENUE="${HEDGE_VENUE:-bybit}"
PRIMARY_ROOT="${PRIMARY_ROOT:-data/bybit-continuous-demo-event}"
DATA_ROOT="${HEDGE_DATA_ROOT:-data/bybit-continuous-hedge-event}"
case "${CONTINUOUS_HEDGE_TIMER:-off}" in
    on|ON|On|1|true|TRUE|yes|YES) ;;
    *)
        echo "continuous hedge lifecycle is off; refusing to run armed hedge service." >&2
        exit 0
        ;;
esac
args=(--venue "$VENUE" --data-root "$DATA_ROOT" --primary-root "$PRIMARY_ROOT")
if [[ "${SUBMIT_HEDGE:-0}" == "1" ]]; then
    args+=(--submit)
fi
exec "$PYTHON_BIN" scripts/run_continuous_hedge.py "${args[@]}"
