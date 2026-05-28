#!/usr/bin/env bash
# Pre-reg: docs/preregistration/exploratory/liquidity-capacity-filter-and-filter-tweak-sweep.md
#
# Runs a 15-cell EXPLORATORY parameter sweep against the current promoted
# baseline on both per-venue full-PIT roots. Each cell varies one or more
# filter parameters; the dispatcher script `python scripts/sweep_cells.py`
# does the actual cell iteration + metrics aggregation.
#
# Outputs:
#   ~/SHARED_DATA/{bybit,binance}_full_pit/reports/sweep_2026-05-28/
#     <cell-id>/volume_event_research_report.{json,md}
#     <cell-id>/volume_event_best_*.csv
#   ~/SHARED_DATA/sweep_2026-05-28_summary.csv
#     -- aggregate metrics per (venue, cell) for analysis

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

LOG_DIR="$HOME/SHARED_DATA"
LOG_FILE="$LOG_DIR/sweep_2026-05-28_$(date -u +%Y%m%dT%H%M%SZ).log"

echo "log=$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=============================================================="
echo "Liquidity-capacity + filter-tweak sweep — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "label: EXPLORATORY (not promotion evidence)"
echo "=============================================================="

.venv/bin/python scripts/sweep_cells.py
