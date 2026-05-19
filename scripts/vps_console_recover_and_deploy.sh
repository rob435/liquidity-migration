#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/rob435/MODEL05042026.git}"
REPO_DIR="${REPO_DIR:-/opt/MODEL050426}"
BRANCH="${BRANCH:-main}"
REMOTE="${REMOTE:-origin}"
EXPECTED_COMMIT="${EXPECTED_COMMIT:-}"
EXPECTED_TELEGRAM_CHAT_ID="${EXPECTED_TELEGRAM_CHAT_ID:-8388367561}"
LOCAL_SSH_PUBLIC_KEY="${SSH_PUBLIC_KEY:-ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFwJNtc1cVhkzNKmxmq6mogten+Q/5yfLulf9wxZxMNp hetzner}"
GITHUB_ACTIONS_SSH_PUBLIC_KEY="${GITHUB_ACTIONS_SSH_PUBLIC_KEY:-ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKykZKBc1KapzJXdFORWMhjaNFC4zPeEZkOAbu32aTXX model050426-github-actions-20260519}"
CLEAN_DIRTY_CHECKOUT="${CLEAN_DIRTY_CHECKOUT:-0}"
SYSTEMD_SETTLE_SECONDS="${SYSTEMD_SETTLE_SECONDS:-15}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this from the VPS provider console as root." >&2
  exit 1
fi

missing_prereqs=()
for binary in git python3; do
  if ! command -v "$binary" >/dev/null 2>&1; then
    missing_prereqs+=("$binary")
  fi
done

if [ "${#missing_prereqs[@]}" -gt 0 ] || ! python3 -m venv --help >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y ca-certificates git openssh-server python3 python3-venv python3-pip
  else
    echo "Missing deploy prerequisites and apt-get is unavailable: ${missing_prereqs[*]:-python3-venv}" >&2
    exit 1
  fi
fi

mkdir -p /root/.ssh
chmod 700 /root/.ssh
touch /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
for public_key in "$LOCAL_SSH_PUBLIC_KEY" "$GITHUB_ACTIONS_SSH_PUBLIC_KEY"; do
  if ! grep -Fxq "$public_key" /root/.ssh/authorized_keys; then
    printf '%s\n' "$public_key" >> /root/.ssh/authorized_keys
  fi
done
chown -R root:root /root/.ssh

if [ -d /etc/ssh/sshd_config.d ]; then
  cat >/etc/ssh/sshd_config.d/99-model050426-recovery.conf <<'SSH_CONFIG'
PubkeyAuthentication yes
PermitRootLogin prohibit-password
AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys2
SSH_CONFIG
fi
if command -v sshd >/dev/null 2>&1; then
  sshd -t
fi
if command -v systemctl >/dev/null 2>&1; then
  systemctl restart ssh.service || systemctl restart sshd.service || true
else
  service ssh restart || service sshd restart || true
fi

if [ ! -d "$REPO_DIR/.git" ]; then
  mkdir -p "$(dirname "$REPO_DIR")"
  git clone "$REPO_URL" "$REPO_DIR"
fi

cd "$REPO_DIR"

if [ -n "$(git status --short)" ]; then
  if [ "$CLEAN_DIRTY_CHECKOUT" != "1" ]; then
    echo "Refusing deploy: VPS git checkout is dirty." >&2
    echo "Rerun with CLEAN_DIRTY_CHECKOUT=1 to save a patch, reset tracked files, and clean untracked non-ignored files." >&2
    git status --short >&2
    exit 1
  fi
  backup_dir="/root/model050426-deploy-backups"
  mkdir -p "$backup_dir"
  backup_patch="$backup_dir/dirty-checkout-$(date -u +%Y%m%dT%H%M%SZ).patch"
  git diff > "$backup_patch"
  git status --short > "$backup_patch.status"
  git reset --hard
  git clean -fd
  echo "Cleaned dirty checkout; saved diff/status under $backup_dir"
fi

git fetch "$REMOTE" "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only "$REMOTE" "$BRANCH"

if [ -n "$EXPECTED_COMMIT" ]; then
  actual_commit="$(git rev-parse HEAD)"
  if [ "$actual_commit" != "$EXPECTED_COMMIT" ]; then
    echo "Refusing deploy: expected commit $EXPECTED_COMMIT but VPS has $actual_commit" >&2
    exit 1
  fi
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
PYTHON=.venv/bin/python

"$PYTHON" -m pytest \
  tests/test_runtime_scripts.py \
  tests/test_aggression_carry_champion_challenger.py \
  tests/test_aggression_carry_cli.py::test_cli_volume_events_defaults_to_selected_liquidity_migration \
  tests/test_aggression_carry_event_demo.py::test_demo_relaxed_profile_lowers_gates_for_more_demo_trades \
  tests/test_aggression_carry_event_demo.py::test_observe_alias_maps_to_canonical_demo_relaxed_profile

"$PYTHON" - <<'PY'
from aggression_carry.event_demo import _demo_event_config, _demo_strategy_id
from aggression_carry.volume_events import VolumeEventResearchConfig

promoted = _demo_event_config(VolumeEventResearchConfig(), profile="promoted")
demo = _demo_event_config(VolumeEventResearchConfig(), profile="demo_relaxed")

assert _demo_strategy_id("promoted") == "liqmig_union_q40_h3_tp26_g100_qsqueeze"
assert _demo_strategy_id("demo_relaxed") == "demo_relaxed_liqmig_q40_h3_tp21_g100_qsqueeze_ff6"
assert promoted.take_profit_pcts == (0.26,)
assert demo.take_profit_pcts == (0.21,)
assert demo.failed_fade_exit_hours == 6
assert demo.failed_fade_min_mfe_pct == 0.01
assert demo.failed_fade_loss_pct == 0.04
assert demo.failed_fade_close_location_min == 0.0
print("strategy-settings-ok")
PY

if [ ! -f /etc/model050426/bybit-demo.env ]; then
  echo "Missing /etc/model050426/bybit-demo.env; restore secrets before starting services." >&2
  exit 1
fi

set -a
. /etc/model050426/bybit-demo.env
set +a

if [ "${TELEGRAM_CHAT_ID:-}" != "$EXPECTED_TELEGRAM_CHAT_ID" ]; then
  echo "Refusing deploy: TELEGRAM_CHAT_ID is '${TELEGRAM_CHAT_ID:-unset}', expected '$EXPECTED_TELEGRAM_CHAT_ID'" >&2
  exit 1
fi

cp deploy/systemd/model050426-bybit-demo.service /etc/systemd/system/model050426-bybit-demo.service
cp deploy/systemd/model050426-bybit-risk.service /etc/systemd/system/model050426-bybit-risk.service
systemctl daemon-reload
systemctl enable model050426-bybit-demo.service
systemctl enable model050426-bybit-risk.service
systemctl restart model050426-bybit-demo.service
systemctl restart model050426-bybit-risk.service

if [ "$SYSTEMD_SETTLE_SECONDS" -gt 0 ]; then
  sleep "$SYSTEMD_SETTLE_SECONDS"
fi

systemctl is-active --quiet model050426-bybit-demo.service
systemctl is-active --quiet model050426-bybit-risk.service

systemctl show model050426-bybit-demo.service \
  --property=ActiveState \
  --property=SubState \
  --property=MainPID \
  --property=ExecMainStatus \
  --no-pager
systemctl show model050426-bybit-risk.service \
  --property=ActiveState \
  --property=SubState \
  --property=MainPID \
  --property=ExecMainStatus \
  --no-pager
systemctl cat model050426-bybit-demo.service --no-pager | grep -E 'Environment=(STRATEGY_PROFILE|INTERVAL_SECONDS|UNIVERSE_RANK_END|UNIVERSE_MAX_SYMBOLS)='

echo "deploy-ok commit=$(git rev-parse --short HEAD)"
