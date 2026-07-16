#!/usr/bin/env bash
# Staged VPS lifecycle: stopped install -> separate authorization -> activate.
set -euo pipefail

MODE="${1:-${DEPLOY_MODE:-verify}}"
if [ "$#" -gt 0 ]; then shift; fi
if [ "$#" -ne 0 ]; then
    echo "usage: deploy_vps_live.sh {install|activate|verify}" >&2
    exit 2
fi
case "$MODE" in install|activate|verify) ;; *) echo "invalid deploy mode: $MODE" >&2; exit 2 ;; esac

SSH_TARGET="${SSH_TARGET:-root@116.202.15.128}"
SSH_OPTS="${SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=10}"
REPO_URL="${REPO_URL:-https://github.com/rob435/liquidity-migration.git}"
REPO_DIR="${REPO_DIR:-/opt/liquidity-migration}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"
EXPECTED_COMMIT="${EXPECTED_COMMIT:-}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
RMOM_BOOTSTRAP_TIMEOUT_SECONDS="${RMOM_BOOTSTRAP_TIMEOUT_SECONDS:-1800}"
RMOM_BOOTSTRAP_RETRY_SECONDS="${RMOM_BOOTSTRAP_RETRY_SECONDS:-30}"

if [[ ! "$EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
    echo "EXPECTED_COMMIT must be a full lowercase 40-character commit" >&2
    exit 2
fi
if ! git check-ref-format --branch "$BRANCH" >/dev/null 2>&1; then
    echo "BRANCH is not a valid Git branch" >&2
    exit 2
fi
for value in "$RMOM_BOOTSTRAP_TIMEOUT_SECONDS" "$RMOM_BOOTSTRAP_RETRY_SECONDS"; do
    [[ "$value" =~ ^[1-9][0-9]*$ ]] || { echo "RMOM durations must be positive integers" >&2; exit 2; }
done

if [ "$MODE" = install ] && [ -z "$GITHUB_TOKEN" ] \
    && [[ "$REPO_URL" == https://github.com/* ]] && command -v gh >/dev/null 2>&1; then
    GITHUB_TOKEN="$(gh auth token --hostname github.com 2>/dev/null || true)"
fi

read -r -a SSH_ARGS <<< "$SSH_OPTS"
{
    printf 'MODE=%q\n' "$MODE"
    printf 'REPO_URL=%q\n' "$REPO_URL"
    printf 'REPO_DIR=%q\n' "$REPO_DIR"
    printf 'REMOTE=%q\n' "$REMOTE"
    printf 'BRANCH=%q\n' "$BRANCH"
    printf 'EXPECTED_COMMIT=%q\n' "$EXPECTED_COMMIT"
    printf 'GITHUB_TOKEN=%q\n' "$GITHUB_TOKEN"
    printf 'RMOM_BOOTSTRAP_TIMEOUT_SECONDS=%q\n' "$RMOM_BOOTSTRAP_TIMEOUT_SECONDS"
    printf 'RMOM_BOOTSTRAP_RETRY_SECONDS=%q\n' "$RMOM_BOOTSTRAP_RETRY_SECONDS"
    cat <<'REMOTE_SCRIPT'
set -euo pipefail

fail() { echo "deploy failed: $*" >&2; exit 1; }

PAPER_RUNTIME_USER=liquidity-migration-paper
PAPER_RUNTIME_GROUP=liquidity-migration-paper
PAPER_ACCOUNT_ROOT=/opt/liquidity-migration/data/bybit-account-paper
PAPER_INBOX_ROOT=/opt/liquidity-migration/data/bybit-account-paper-intents
PAPER_CAPTURE_ROOT=/opt/liquidity-migration/data/bybit-account-paper-market-capture
PAPER_CONFIG_DIR=/etc/liquidity-migration/account-paper-execution
PAPER_ENVIRONMENT=/etc/liquidity-migration/account-paper-execution.env
PAPER_SYMBOLS_FILE=$PAPER_CONFIG_DIR/symbols.txt
PAPER_RULES_FILE=$PAPER_CONFIG_DIR/demo-rules.json
PAPER_RISK_FILE=$PAPER_CONFIG_DIR/risk-policy.json
LONG_DEMO_ROOT=/opt/liquidity-migration/data/bybit-long-demo-event
CONTINUOUS_DEMO_ROOT=/opt/liquidity-migration/data/bybit-continuous-demo-event
LONG_PAPER_ROOT=/opt/liquidity-migration/data/bybit-long-paper-event
CONTINUOUS_PAPER_ROOT=/opt/liquidity-migration/data/bybit-continuous-paper-event

ensure_paper_runtime_identity() {
    command -v getent >/dev/null 2>&1 || fail "getent is unavailable"
    command -v runuser >/dev/null 2>&1 || fail "runuser is unavailable"
    getent group "$PAPER_RUNTIME_GROUP" >/dev/null \
        || groupadd --system "$PAPER_RUNTIME_GROUP"
    if ! id -u "$PAPER_RUNTIME_USER" >/dev/null 2>&1; then
        useradd --system --gid "$PAPER_RUNTIME_GROUP" --no-create-home \
            --home-dir /nonexistent --shell /usr/sbin/nologin "$PAPER_RUNTIME_USER"
    fi
    [ "$(id -gn "$PAPER_RUNTIME_USER")" = "$PAPER_RUNTIME_GROUP" ] \
        || fail "$PAPER_RUNTIME_USER does not use $PAPER_RUNTIME_GROUP as its primary group"
    paper_shell="$(getent passwd "$PAPER_RUNTIME_USER" | awk -F: '{print $7}')"
    case "$paper_shell" in
        /usr/sbin/nologin|/sbin/nologin|/bin/false) ;;
        *) fail "$PAPER_RUNTIME_USER must be a non-login account" ;;
    esac
}

prepare_paper_runtime_boundary() {
    [ "$REPO_DIR" = /opt/liquidity-migration ] \
        || fail "systemd paper paths require REPO_DIR=/opt/liquidity-migration"
    ensure_paper_runtime_identity
    for path in \
        /etc/liquidity-migration/account-execution.env \
        /etc/liquidity-migration/bybit-demo.env; do
        [ -f "$path" ] && [ ! -L "$path" ] || fail "missing real private config: $path"
        chown root:root "$path"
        chmod 0600 "$path"
    done
    lm_load_private_systemd_environment "$PYTHON" \
        /etc/liquidity-migration/account-execution.env \
        ACCOUNT_SYMBOLS_FILE CANDIDATE_UNIVERSE_FILE \
        ACCOUNT_DEMO_RULES_FILE ACCOUNT_RISK_POLICY_FILE
    demo_symbols="$ACCOUNT_SYMBOLS_FILE"
    demo_candidate="${CANDIDATE_UNIVERSE_FILE:-}"
    demo_rules="$ACCOUNT_DEMO_RULES_FILE"
    demo_risk="$ACCOUNT_RISK_POLICY_FILE"
    for path in "$demo_symbols" "$demo_rules" "$demo_risk"; do
        [ "${path#/}" != "$path" ] || fail "demo account input must be absolute: $path"
        [ -f "$path" ] && [ ! -L "$path" ] || fail "missing real demo account input: $path"
        chown root:root "$path"
        chmod 0600 "$path"
    done
    if [ -z "$demo_candidate" ]; then
        "$PYTHON" - /etc/liquidity-migration/account-execution.env "$demo_symbols" <<'PY'
import os
import shlex
import sys
import tempfile
from pathlib import Path

from liquidity_migration.account_candidate_universe import load_candidate_universe
from liquidity_migration.systemd_environment import load_private_systemd_environment

path = Path(sys.argv[1])
symbols = str(Path(sys.argv[2]))
load_candidate_universe(symbols)
values = load_private_systemd_environment(path)
values["CANDIDATE_UNIVERSE_FILE"] = symbols
descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        for key, value in sorted(values.items()):
            handle.write(f"{key}={shlex.quote(value)}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
except BaseException:
    Path(temporary).unlink(missing_ok=True)
    raise
PY
        demo_candidate="$demo_symbols"
    fi
    [ "$demo_candidate" = "$demo_symbols" ] \
        || fail "demo candidate universe is not the owner symbols file"

    install -d -o root -g "$PAPER_RUNTIME_GROUP" -m 0710 /etc/liquidity-migration
    install -d -o root -g "$PAPER_RUNTIME_GROUP" -m 0750 "$PAPER_CONFIG_DIR"
    install -o "$PAPER_RUNTIME_USER" -g "$PAPER_RUNTIME_GROUP" -m 0600 \
        "$demo_symbols" "$PAPER_SYMBOLS_FILE"
    install -o "$PAPER_RUNTIME_USER" -g "$PAPER_RUNTIME_GROUP" -m 0600 \
        "$demo_rules" "$PAPER_RULES_FILE"
    install -o "$PAPER_RUNTIME_USER" -g "$PAPER_RUNTIME_GROUP" -m 0600 \
        "$demo_risk" "$PAPER_RISK_FILE"

    # Rebuild the non-secret paper route as strict data. An existing file may
    # contribute benign tuning values, but credentials and alternate roots are
    # never migrated across this boundary.
    if [ -e "$PAPER_ENVIRONMENT" ] || [ -L "$PAPER_ENVIRONMENT" ]; then
        [ -f "$PAPER_ENVIRONMENT" ] && [ ! -L "$PAPER_ENVIRONMENT" ] \
            || fail "paper environment must be a real regular file"
        chown root:root "$PAPER_ENVIRONMENT"
        chmod 0600 "$PAPER_ENVIRONMENT"
    else
        install -o root -g root -m 0600 /dev/null "$PAPER_ENVIRONMENT"
    fi
    "$PYTHON" - "$PAPER_ENVIRONMENT" \
        "$PAPER_ACCOUNT_ROOT" "$PAPER_INBOX_ROOT" "$PAPER_CAPTURE_ROOT" \
        "$PAPER_SYMBOLS_FILE" "$PAPER_RULES_FILE" "$PAPER_RISK_FILE" <<'PY'
import os
import shlex
import sys
import tempfile
from pathlib import Path

from liquidity_migration.systemd_environment import (
    load_private_systemd_environment,
)

path = Path(sys.argv[1])
existing = load_private_systemd_environment(path) if path.stat().st_size else {}
for key in (
    "BYBIT_DEMO_API_KEY",
    "BYBIT_DEMO_API_SECRET",
    "BYBIT_REAL_API_KEY",
    "BYBIT_REAL_API_SECRET",
):
    if existing.get(key):
        raise SystemExit(f"paper environment contains forbidden credential {key}")
if existing.get("REAL_MONEY", "").strip().lower() not in {"", "0", "false", "no", "off"}:
    raise SystemExit("paper environment does not explicitly disable REAL_MONEY")
allowed_tuning = {
    "PAPER_EQUITY_USDT",
    "MAX_DEMO_RULE_AGE_HOURS",
    "ACCOUNT_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS",
}
values = {key: value for key, value in existing.items() if key in allowed_tuning}
values.update(
    {
        "ACCOUNT_PAPER_KERNEL_REQUIRED": "1",
        "ACCOUNT_RAW_MARKET_PERSISTENCE": "0",
        "ACCOUNT_EXECUTION_ROOT": sys.argv[2],
        "ACCOUNT_INTENT_INBOX_ROOT": sys.argv[3],
        "ACCOUNT_PAPER_CAPTURE_ROOT": sys.argv[4],
        "ACCOUNT_SYMBOLS_FILE": sys.argv[5],
        "CANDIDATE_UNIVERSE_FILE": sys.argv[5],
        "ACCOUNT_DEMO_RULES_FILE": sys.argv[6],
        "ACCOUNT_RISK_POLICY_FILE": sys.argv[7],
    }
)
descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        for key, value in sorted(values.items()):
            handle.write(f"{key}={shlex.quote(value)}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
except BaseException:
    Path(temporary).unlink(missing_ok=True)
    raise
PY
    chown root:"$PAPER_RUNTIME_GROUP" "$PAPER_ENVIRONMENT"
    chmod 0640 "$PAPER_ENVIRONMENT"
    chown root:"$PAPER_RUNTIME_GROUP" /etc/liquidity-migration/sleeves.resolved.env
    chmod 0640 /etc/liquidity-migration/sleeves.resolved.env

    for root in \
        "$PAPER_ACCOUNT_ROOT" "$PAPER_INBOX_ROOT" "$PAPER_CAPTURE_ROOT" \
        "$LONG_PAPER_ROOT" "$CONTINUOUS_PAPER_ROOT"; do
        [ ! -L "$root" ] || fail "paper runtime root must not be a symlink: $root"
        install -d -o "$PAPER_RUNTIME_USER" -g "$PAPER_RUNTIME_GROUP" -m 0700 "$root"
        chown -R "$PAPER_RUNTIME_USER:$PAPER_RUNTIME_GROUP" "$root"
        find "$root" -type d -exec chmod 0700 {} +
        find "$root" -type f -exec chmod 0600 {} +
    done
    for root in "$LONG_DEMO_ROOT" "$CONTINUOUS_DEMO_ROOT"; do
        [ ! -L "$root" ] || fail "demo market root must not be a symlink: $root"
        install -d -o root -g "$PAPER_RUNTIME_GROUP" -m 2710 "$root"
        install -d -o root -g "$PAPER_RUNTIME_GROUP" -m 2710 "$root/.cache"
        install -d -o root -g "$PAPER_RUNTIME_GROUP" -m 2750 \
            "$root/.cache/ws_klines"
        if [ -e "$root/.cache/ws_klines/store.parquet" ]; then
            [ -f "$root/.cache/ws_klines/store.parquet" ] \
                && [ ! -L "$root/.cache/ws_klines/store.parquet" ] \
                || fail "demo kline snapshot must be a real regular file: $root"
            chown root:"$PAPER_RUNTIME_GROUP" \
                "$root/.cache/ws_klines/store.parquet"
            chmod 0640 "$root/.cache/ws_klines/store.parquet"
        fi
    done
    if [ -e "$CONTINUOUS_DEMO_ROOT/residual_momentum.parquet" ]; then
        [ -f "$CONTINUOUS_DEMO_ROOT/residual_momentum.parquet" ] \
            && [ ! -L "$CONTINUOUS_DEMO_ROOT/residual_momentum.parquet" ] \
            || fail "shared RMOM input must be a real regular file"
        chown root:"$PAPER_RUNTIME_GROUP" \
            "$CONTINUOUS_DEMO_ROOT/residual_momentum.parquet"
        chmod 0640 "$CONTINUOUS_DEMO_ROOT/residual_momentum.parquet"
    fi
    chown root:root \
        /etc/liquidity-migration/account-execution.env \
        /etc/liquidity-migration/bybit-demo.env
    chmod 0600 \
        /etc/liquidity-migration/account-execution.env \
        /etc/liquidity-migration/bybit-demo.env
}

verify_paper_runtime_boundary() {
    ensure_paper_runtime_identity
    for path in \
        "$PAPER_ENVIRONMENT" /etc/liquidity-migration/sleeves.resolved.env \
        "$PAPER_SYMBOLS_FILE" "$PAPER_RULES_FILE" "$PAPER_RISK_FILE"; do
        runuser -u "$PAPER_RUNTIME_USER" -- test -r "$path" \
            || fail "paper runtime cannot read required non-secret input: $path"
    done
    for path in \
        /etc/liquidity-migration/bybit-demo.env \
        /etc/liquidity-migration/account-execution.env; do
        runuser -u "$PAPER_RUNTIME_USER" -- test ! -r "$path" \
            || fail "paper runtime can read forbidden demo config: $path"
    done
    for root in \
        "$PAPER_ACCOUNT_ROOT" "$PAPER_INBOX_ROOT" "$PAPER_CAPTURE_ROOT" \
        "$LONG_PAPER_ROOT" "$CONTINUOUS_PAPER_ROOT"; do
        runuser -u "$PAPER_RUNTIME_USER" -- test -w "$root" \
            || fail "paper runtime cannot write its explicit state root: $root"
    done
    for root in "$LONG_DEMO_ROOT" "$CONTINUOUS_DEMO_ROOT"; do
        runuser -u "$PAPER_RUNTIME_USER" -- \
            test -x "$root/.cache/ws_klines" \
            || fail "paper runtime cannot traverse demo kline cache: $root"
        snapshot="$root/.cache/ws_klines/store.parquet"
        if [ -e "$snapshot" ]; then
            runuser -u "$PAPER_RUNTIME_USER" -- test -r "$snapshot" \
                || fail "paper runtime cannot read demo kline snapshot: $snapshot"
        fi
        runuser -u "$PAPER_RUNTIME_USER" -- test ! -w "$root" \
            || fail "paper runtime can write demo market root: $root"
    done
    if [ -e "$CONTINUOUS_DEMO_ROOT/residual_momentum.parquet" ]; then
        runuser -u "$PAPER_RUNTIME_USER" -- \
            test -r "$CONTINUOUS_DEMO_ROOT/residual_momentum.parquet" \
            || fail "paper runtime cannot read the shared RMOM input"
    fi
    runuser -u "$PAPER_RUNTIME_USER" -- test ! -w "$REPO_DIR/liquidity_migration" \
        || fail "paper runtime can write repository code"
    if [ -e /etc/liquidity-migration/account-execution-operational-ready ]; then
        runuser -u "$PAPER_RUNTIME_USER" -- \
            test -r /etc/liquidity-migration/account-execution-operational-ready \
            || fail "paper runtime cannot read the operational receipt"
    fi
}

require_checkout() {
    [ -d "$REPO_DIR/.git" ] || fail "missing Git checkout: $REPO_DIR"
    cd "$REPO_DIR"
}

require_clean_head() {
    [ "$(git rev-parse HEAD)" = "$EXPECTED_COMMIT" ] \
        || fail "checkout is not at expected commit $EXPECTED_COMMIT"
    [ -z "$(git status --porcelain)" ] || fail "checkout is dirty"
}

require_quiescent() {
    command -v systemctl >/dev/null 2>&1 || fail "systemctl is unavailable"
    local rows running
    rows="$(systemctl list-units 'liquidity-migration-*' --all --no-legend --no-pager --plain 2>/dev/null)" \
        || fail "cannot inspect liquidity-migration units"
    running="$(printf '%s\n' "$rows" | awk 'NF >= 3 && $3 != "inactive" && $3 != "failed" {print $1 " (" $3 ")"}')"
    [ -z "$running" ] || { printf 'quiesce these units first:\n%s\n' "$running" >&2; exit 1; }
}

git_fetch() {
    if [ -n "$GITHUB_TOKEN" ] && [[ "$REPO_URL" == https://github.com/* ]]; then
        local auth
        auth="$(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 | tr -d '\n')"
        GIT_CONFIG_COUNT=1 \
        GIT_CONFIG_KEY_0=http.https://github.com/.extraheader \
        GIT_CONFIG_VALUE_0="AUTHORIZATION: Basic $auth" \
        GIT_TERMINAL_PROMPT=0 git "$@"
    else
        GIT_TERMINAL_PROMPT=0 git "$@"
    fi
}

invalidate_operational_authorization() {
    local path=/etc/liquidity-migration/account-execution-operational-ready archive stamp
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    [ -e "$path" ] || [ -L "$path" ] || return 0
    archive="/var/lib/liquidity-migration/retired-authority/$stamp"
    install -d -m 0700 "$archive"
    mv "$path" "$archive/$(basename "$path")"
    echo "invalidated prior operational authorization: $archive"
}

install_mode() {
    require_checkout
    require_quiescent
    [ -z "$(git status --porcelain)" ] || fail "checkout is dirty before install"

    if git remote get-url "$REMOTE" >/dev/null 2>&1; then
        git remote set-url "$REMOTE" "$REPO_URL"
    else
        git remote add "$REMOTE" "$REPO_URL"
    fi
    git_fetch fetch "$REMOTE" "refs/heads/$BRANCH:refs/remotes/$REMOTE/$BRANCH"
    git cat-file -e "$EXPECTED_COMMIT^{commit}" 2>/dev/null \
        || fail "expected commit is unavailable"
    git merge-base --is-ancestor "$EXPECTED_COMMIT" "$REMOTE/$BRANCH" \
        || fail "expected commit is not on $REMOTE/$BRANCH"
    git checkout -B "$BRANCH" "$EXPECTED_COMMIT"
    require_clean_head
    unset GITHUB_TOKEN

    [ -x .venv/bin/python ] || python3 -m venv .venv
    PYTHON=.venv/bin/python
    "$PYTHON" -m pip install --disable-pip-version-check --no-deps \
        --only-binary=:all: -r requirements.lock
    "$PYTHON" -m ruff check liquidity_migration scripts tests
    "$PYTHON" -m mypy liquidity_migration
    "$PYTHON" -m pytest -q \
        tests/test_operational_runtime_authority.py \
        tests/test_runtime_scripts.py

    . deploy/lib_sleeves.sh
    . deploy/lib_systemd_environment.sh
    ensure_paper_runtime_identity
    lm_install_current_systemd_units
    for unit in $(lm_expected_systemd_units); do
        systemctl disable --now "$unit" 2>/dev/null || true
    done
    require_quiescent
    invalidate_operational_authorization

    lm_load_sleeve_toggles
    if sleeve_on "$CONTINUOUS_SLEEVE"; then
        CONTINUOUS_HEDGE_TIMER=on
    else
        lm_load_private_systemd_environment "$PYTHON" \
            /etc/liquidity-migration/account-execution.env ACCOUNT_EXECUTION_ROOT
        hedge_open="$(ACCOUNT_ROOT="$ACCOUNT_EXECUTION_ROOT" "$PYTHON" - <<'PY' 2>/dev/null || echo unknown
import os
from pathlib import Path
from liquidity_migration.account_service import SleeveAdapterKind
from liquidity_migration.account_strategy_state import canonical_strategy_trade_rows

try:
    rows = canonical_strategy_trade_rows(
        Path(os.environ["ACCOUNT_ROOT"]), sleeve=SleeveAdapterKind.HEDGE.value
    )
    print(int((rows["status"] == "open").sum()) if not rows.is_empty() else 0)
except Exception:
    print("unknown")
PY
)"
        if [ "$hedge_open" = 0 ]; then CONTINUOUS_HEDGE_TIMER=off; else CONTINUOUS_HEDGE_TIMER=on; fi
    fi
    export CONTINUOUS_HEDGE_TIMER
    lm_write_resolved_sleeve_toggles
    prepare_paper_runtime_boundary
    lm_verify_resolved_sleeve_toggles
    verify_paper_runtime_boundary
    require_clean_head
    echo "install-ok commit=$EXPECTED_COMMIT units_started=0"
    echo "next: issue a new operational authorization for this stopped exact checkout, then run activate"
}

load_authorization() {
    require_checkout
    require_clean_head
    PYTHON=.venv/bin/python
    [ -x "$PYTHON" ] || fail "missing deployed Python environment"
    AUTH_JSON="$("$PYTHON" -m liquidity_migration.operational_runtime_authority verify \
        --receipt /etc/liquidity-migration/account-execution-operational-ready \
        --repo-root "$REPO_DIR")" || fail "operational authorization verification failed"
    AUTH_PROFILE="$(printf '%s' "$AUTH_JSON" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["profile"])')"
    AUTH_COMMIT="$(printf '%s' "$AUTH_JSON" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["authorized_commit"])')"
    [ "$AUTH_COMMIT" = "$EXPECTED_COMMIT" ] || fail "authorization is for another commit"
    case "$AUTH_PROFILE" in demo-operational|operational) ;; *) fail "unsupported profile $AUTH_PROFILE" ;; esac
    if [ "$AUTH_PROFILE" = operational ]; then
        verify_paper_runtime_boundary
    fi

    . deploy/lib_sleeves.sh
    . deploy/lib_systemd_environment.sh
    lm_load_group_systemd_environment "$PYTHON" \
        /etc/liquidity-migration/sleeves.resolved.env "$PAPER_RUNTIME_GROUP" \
        LONG_SLEEVE CONTINUOUS_SLEEVE CONTINUOUS_PAPER_SLEEVE CONTINUOUS_HEDGE_TIMER
    for value in "$LONG_SLEEVE" "$CONTINUOUS_SLEEVE" "$CONTINUOUS_PAPER_SLEEVE" "$CONTINUOUS_HEDGE_TIMER"; do
        case "$value" in on|off) ;; *) fail "invalid resolved sleeve value" ;; esac
    done
    if [ "$AUTH_PROFILE" = demo-operational ] && sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; then
        fail "demo-operational authorization cannot run the paper continuous sleeve"
    fi
    lm_verify_resolved_sleeve_toggles
    lm_verify_no_unknown_liqmig_units
    lm_verify_guarded_unit_surfaces
}

unit_on() {
    systemctl is-active --quiet "$1" && systemctl is-enabled --quiet "$1"
}

unit_off() {
    ! systemctl is-active --quiet "$1" 2>/dev/null \
        && ! systemctl is-enabled --quiet "$1" 2>/dev/null
}

timer_on() { unit_on "$1"; }
timer_off() { unit_off "$1"; }

validate_hedge_model_prior() {
    "$PYTHON" scripts/run_continuous_hedge.py \
        --execution-environment demo \
        --validate-model-prior-only
}

check_demo_order_permissions() {
    local context="$1" status=0
    unset BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET REAL_MONEY DEMO
    lm_load_private_systemd_environment "$PYTHON" \
        /etc/liquidity-migration/bybit-demo.env \
        BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET REAL_MONEY
    "$PYTHON" scripts/check_bybit_order_permissions.py --context "$context" || status=$?
    unset BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET REAL_MONEY
    return "$status"
}

verify_topology() {
    unit_on liquidity-migration-account-execution.service || fail "demo owner is not active and enabled"
    if [ "$AUTH_PROFILE" = operational ]; then
        unit_on liquidity-migration-account-paper-execution.service || fail "paper owner is not active and enabled"
    else
        unit_off liquidity-migration-account-paper-execution.service || fail "paper owner is active under demo-only authorization"
    fi

    if sleeve_on "$LONG_SLEEVE"; then
        unit_on liquidity-migration-bybit-long-demo.service || fail "LONG demo producer is not active"
    else
        unit_off liquidity-migration-bybit-long-demo.service || fail "LONG demo producer is not off"
    fi
    if [ "$AUTH_PROFILE" = operational ] && sleeve_on "$LONG_SLEEVE"; then
        unit_on liquidity-migration-bybit-long-paper.service || fail "LONG paper producer is not active"
    else
        unit_off liquidity-migration-bybit-long-paper.service || fail "LONG paper producer is not off"
    fi
    if sleeve_on "$CONTINUOUS_SLEEVE"; then
        unit_on liquidity-migration-bybit-continuous-demo.service || fail "continuous demo producer is not active"
    else
        unit_off liquidity-migration-bybit-continuous-demo.service || fail "continuous demo producer is not off"
    fi
    if [ "$AUTH_PROFILE" = operational ] && sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; then
        unit_on liquidity-migration-bybit-continuous-paper.service || fail "continuous paper producer is not active"
    else
        unit_off liquidity-migration-bybit-continuous-paper.service || fail "continuous paper producer is not off"
    fi

    if sleeve_on "$CONTINUOUS_SLEEVE" \
        || { [ "$AUTH_PROFILE" = operational ] && sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; }; then
        timer_on liquidity-migration-continuous-rmom-refresh.timer || fail "RMOM timer is not active"
    else
        timer_off liquidity-migration-continuous-rmom-refresh.timer || fail "RMOM timer is not off"
    fi
    if sleeve_on "$CONTINUOUS_HEDGE_TIMER"; then
        validate_hedge_model_prior
        timer_on liquidity-migration-continuous-hedge.timer || fail "hedge timer is not active"
    else
        timer_off liquidity-migration-continuous-hedge.timer || fail "hedge timer is not off"
    fi
    timer_on liquidity-migration-demo-liveness.timer || fail "liveness timer is not active"
    for oneshot in \
        liquidity-migration-continuous-rmom-refresh.service \
        liquidity-migration-continuous-hedge.service \
        liquidity-migration-demo-liveness.service; do
        ! systemctl is-failed --quiet "$oneshot" || fail "$oneshot is failed"
    done
    check_demo_order_permissions verify
    echo "verify-ok commit=$EXPECTED_COMMIT profile=$AUTH_PROFILE"
}

start_if() {
    local state="$1" unit="$2"
    if sleeve_on "$state"; then
        systemctl enable "$unit"
        systemctl start "$unit"
    else
        systemctl disable --now "$unit" 2>/dev/null || true
    fi
}

seed_rmom() {
    local deadline ok
    deadline=$(( $(date +%s) + RMOM_BOOTSTRAP_TIMEOUT_SECONDS ))
    while true; do
        ok=1
        systemctl reset-failed liquidity-migration-continuous-rmom-refresh.service 2>/dev/null || true
        systemctl start liquidity-migration-continuous-rmom-refresh.service || ok=0
        if sleeve_on "$CONTINUOUS_SLEEVE" \
            || { [ "$AUTH_PROFILE" = operational ] && sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; }; then
            "$PYTHON" scripts/check_residual_momentum_gate.py \
                --path data/bybit-continuous-demo-event/residual_momentum.parquet || ok=0
        fi
        [ "$ok" -eq 0 ] || return 0
        [ "$(date +%s)" -lt "$deadline" ] || fail "RMOM bootstrap timed out"
        sleep "$RMOM_BOOTSTRAP_RETRY_SECONDS"
    done
}

activate_mode() {
    load_authorization
    require_quiescent
    check_demo_order_permissions deploy
    if sleeve_on "$CONTINUOUS_HEDGE_TIMER"; then
        validate_hedge_model_prior
    fi

    for unit in $(lm_expected_systemd_units); do
        systemctl disable --now "$unit" 2>/dev/null || true
    done
    systemctl enable liquidity-migration-account-execution.service
    systemctl start liquidity-migration-account-execution.service
    if [ "$AUTH_PROFILE" = operational ]; then
        systemctl enable liquidity-migration-account-paper-execution.service
        systemctl start liquidity-migration-account-paper-execution.service
    fi

    start_if "$LONG_SLEEVE" liquidity-migration-bybit-long-demo.service
    if [ "$AUTH_PROFILE" = operational ]; then
        start_if "$LONG_SLEEVE" liquidity-migration-bybit-long-paper.service
    fi
    start_if "$CONTINUOUS_SLEEVE" liquidity-migration-bybit-continuous-demo.service
    if [ "$AUTH_PROFILE" = operational ]; then
        start_if "$CONTINUOUS_PAPER_SLEEVE" liquidity-migration-bybit-continuous-paper.service
    fi

    if sleeve_on "$CONTINUOUS_SLEEVE" \
        || { [ "$AUTH_PROFILE" = operational ] && sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; }; then
        seed_rmom
        systemctl enable --now liquidity-migration-continuous-rmom-refresh.timer
    fi
    if sleeve_on "$CONTINUOUS_HEDGE_TIMER"; then
        systemctl enable --now liquidity-migration-continuous-hedge.timer
    fi
    systemctl enable --now liquidity-migration-demo-liveness.timer
    verify_topology
}

case "$MODE" in
    install) install_mode ;;
    activate) activate_mode ;;
    verify) load_authorization; verify_topology ;;
esac
REMOTE_SCRIPT
} | ssh "${SSH_ARGS[@]}" -- "$SSH_TARGET" bash -s
