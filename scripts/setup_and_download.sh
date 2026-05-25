#!/usr/bin/env bash
# Bootstrap + run the full-PIT rebuild on a fresh machine.
#
# Does, in order:
#   1. clone the repo (or git pull if already present)
#   2. build .venv + install -e .[dev]
#   3. run the pre-push CI gate (ruff + pytest) so nothing crashes mid-download
#   4. launch scripts/build_full_pit_roots.sh in the background, logging to disk
#   5. print monitor commands and resource expectations
#
# Idempotent — every stage skips work already done. Safe to re-run on crash.
#
# Usage on a fresh PC:
#
#     curl -fsSL https://raw.githubusercontent.com/rob435/liquidity-migration/main/scripts/setup_and_download.sh -o /tmp/setup_and_download.sh
#     bash /tmp/setup_and_download.sh
#
# Or after a manual git clone:
#
#     cd liquidity-migration && bash scripts/setup_and_download.sh
#
# Overridable env:
#   REPO_URL=...          (default https://github.com/rob435/liquidity-migration.git)
#   REPO_DIR=...          (default $HOME/liquidity-migration)
#   DATA_ROOT_PARENT=...  (default $HOME/SHARED_DATA — bybit_full_pit and binance_full_pit
#                          live under here)
#   MANIFEST_WORKERS=16   (lower if RAM-constrained; 8 is conservative)
#   KLINE_WORKERS=8       (lower if RAM-constrained; 4 is conservative)
#   ANCILLARY_WORKERS=4   (Bybit REST rate-limit-aware; do not raise above 4)
#   BINANCE_WORKERS=24    (data.binance.vision S3 — can be raised on fast pipes)
#   SKIP_CI=0             (set 1 to skip the ruff+pytest gate before launch)
#   SKIP_LAUNCH=0         (set 1 to set up everything but not start the rebuild)
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/rob435/liquidity-migration.git}"
REPO_DIR="${REPO_DIR:-$HOME/liquidity-migration}"
DATA_ROOT_PARENT="${DATA_ROOT_PARENT:-$HOME/SHARED_DATA}"
MANIFEST_WORKERS="${MANIFEST_WORKERS:-16}"
KLINE_WORKERS="${KLINE_WORKERS:-8}"
ANCILLARY_WORKERS="${ANCILLARY_WORKERS:-4}"
BINANCE_WORKERS="${BINANCE_WORKERS:-24}"
SKIP_CI="${SKIP_CI:-0}"
SKIP_LAUNCH="${SKIP_LAUNCH:-0}"

banner() {
  echo
  echo "=============================================================="
  echo "$*"
  echo "=============================================================="
}

banner "[1/5] Repo"
if [ ! -d "$REPO_DIR/.git" ]; then
  echo "Cloning $REPO_URL -> $REPO_DIR"
  git clone "$REPO_URL" "$REPO_DIR"
else
  echo "Repo present at $REPO_DIR — git fetch + pull"
  git -C "$REPO_DIR" fetch origin --prune
  if ! git -C "$REPO_DIR" diff-index --quiet HEAD --; then
    echo "WARNING: local changes in $REPO_DIR — leaving them alone, not pulling"
  else
    git -C "$REPO_DIR" pull --ff-only origin main
  fi
fi
cd "$REPO_DIR"
echo "Repo HEAD: $(git rev-parse --short HEAD) — $(git log -1 --pretty='%s')"

banner "[2/5] Python venv"
PYTHON_BOOTSTRAP=""
for cand in python3.12 python3.11 python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    PYTHON_BOOTSTRAP="$cand"
    break
  fi
done
if [ -z "$PYTHON_BOOTSTRAP" ]; then
  echo "FATAL: no python3.11+ executable found on PATH" >&2
  exit 2
fi
echo "Bootstrap Python: $PYTHON_BOOTSTRAP ($($PYTHON_BOOTSTRAP --version 2>&1))"

if [ ! -d ".venv" ]; then
  "$PYTHON_BOOTSTRAP" -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
python -m pip install -e ".[dev]" >/dev/null
echo "Installed editable + dev extras"

banner "[3/5] Pre-launch CI gate (ruff + pytest)"
if [ "$SKIP_CI" = "1" ]; then
  echo "SKIPPED (SKIP_CI=1)"
else
  python -m ruff check liquidity_migration tests
  python -m pytest -q
fi

banner "[4/5] Disk space + free RAM check"
need_gb=40
df_root="$DATA_ROOT_PARENT"
mkdir -p "$DATA_ROOT_PARENT"
if command -v df >/dev/null; then
  free_gb=$(df -BG "$df_root" 2>/dev/null | awk 'NR==2 {gsub("G",""); print $4}' \
            || df -g "$df_root" 2>/dev/null | awk 'NR==2 {print $4}')
  echo "Free at $df_root: ${free_gb:-?} GB (need ≥${need_gb} GB)"
fi
echo "Worker counts: manifest=$MANIFEST_WORKERS kline=$KLINE_WORKERS ancillary=$ANCILLARY_WORKERS binance=$BINANCE_WORKERS"

banner "[5/5] Launch"
LOG="$DATA_ROOT_PARENT/rebuild.log"
if [ "$SKIP_LAUNCH" = "1" ]; then
  echo "SKIPPED (SKIP_LAUNCH=1) — to launch later:"
  echo
  echo "  cd $REPO_DIR"
  echo "  BYBIT_FULL_ROOT=$DATA_ROOT_PARENT/bybit_full_pit \\"
  echo "  BINANCE_FULL_ROOT=$DATA_ROOT_PARENT/binance_full_pit \\"
  echo "  MANIFEST_WORKERS=$MANIFEST_WORKERS KLINE_WORKERS=$KLINE_WORKERS \\"
  echo "  ANCILLARY_WORKERS=$ANCILLARY_WORKERS BINANCE_WORKERS=$BINANCE_WORKERS \\"
  echo "  SKIP_ARCHIVE=1 PYTHON_BIN=.venv/bin/python \\"
  echo "  bash scripts/build_full_pit_roots.sh >> $LOG 2>&1 &"
  exit 0
fi

# Launch detached. nohup + setsid (where available) ensures the process
# survives terminal close. Output goes to $LOG so we can tail it.
echo "Launching rebuild — log: $LOG"
if command -v setsid >/dev/null 2>&1; then
  setsid nohup env \
    BYBIT_FULL_ROOT="$DATA_ROOT_PARENT/bybit_full_pit" \
    BINANCE_FULL_ROOT="$DATA_ROOT_PARENT/binance_full_pit" \
    MANIFEST_WORKERS="$MANIFEST_WORKERS" \
    KLINE_WORKERS="$KLINE_WORKERS" \
    ANCILLARY_WORKERS="$ANCILLARY_WORKERS" \
    BINANCE_WORKERS="$BINANCE_WORKERS" \
    SKIP_ARCHIVE=1 \
    PYTHON_BIN=".venv/bin/python" \
    bash scripts/build_full_pit_roots.sh >> "$LOG" 2>&1 < /dev/null &
else
  nohup env \
    BYBIT_FULL_ROOT="$DATA_ROOT_PARENT/bybit_full_pit" \
    BINANCE_FULL_ROOT="$DATA_ROOT_PARENT/binance_full_pit" \
    MANIFEST_WORKERS="$MANIFEST_WORKERS" \
    KLINE_WORKERS="$KLINE_WORKERS" \
    ANCILLARY_WORKERS="$ANCILLARY_WORKERS" \
    BINANCE_WORKERS="$BINANCE_WORKERS" \
    SKIP_ARCHIVE=1 \
    PYTHON_BIN=".venv/bin/python" \
    bash scripts/build_full_pit_roots.sh >> "$LOG" 2>&1 < /dev/null &
fi
LAUNCHED_PID=$!
disown 2>/dev/null || true
sleep 2
if kill -0 "$LAUNCHED_PID" 2>/dev/null; then
  echo "Rebuild running. PID=$LAUNCHED_PID"
else
  echo "WARNING: launched process not visible at PID=$LAUNCHED_PID — check $LOG"
fi

cat <<EOF

Monitor:
    tail -f $LOG                                          # live log
    ps -p $LAUNCHED_PID -o pid,etime,command              # is it alive?
    du -sh $DATA_ROOT_PARENT/bybit_full_pit                # disk growth
    du -sh $DATA_ROOT_PARENT/binance_full_pit
    df -h $DATA_ROOT_PARENT                                # free space

Resume on crash:
    cd $REPO_DIR && SKIP_ARCHIVE=1 PYTHON_BIN=.venv/bin/python \\
        bash scripts/build_full_pit_roots.sh >> $LOG 2>&1 &

Expected runtime: 17-31 hours unattended (Bybit ~10-18h, Binance ~7-13h, verify ~30m).
Expected disk: 25-32 GB across the two new roots.
EOF
