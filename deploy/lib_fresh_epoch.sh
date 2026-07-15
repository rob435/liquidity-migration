# Shared authority-bound fresh-epoch helpers for deploy, verify, and recovery.
#
# Prepare consumes a live, unexpired cutover authorization. Verify and recovery
# instead reopen the immutable activation latch and its exact bound bytes after
# that short-lived authority is spent. No legacy root value is used as a
# fallback after the flat cutover.

LM_CUTOVER_AUTHORIZATION="${LM_CUTOVER_AUTHORIZATION:-/etc/liquidity-migration/account-execution-deploy-ready}"
LM_FRESH_DEPLOY_ENV_DIR="${LM_FRESH_DEPLOY_ENV_DIR:-/etc/liquidity-migration/fresh-deploy}"

lm_prepare_authorized_deploy_epoch() {
    _lfep_python="$1"
    _lfep_repo="$2"
    _lfep_commit="$3"
    GITHUB_TOKEN="${GITHUB_TOKEN:-}" \
    "$_lfep_python" -m liquidity_migration.authorized_deploy_epoch prepare \
        --authorization "$LM_CUTOVER_AUTHORIZATION" \
        --expected-commit "$_lfep_commit" \
        --repo-root "$_lfep_repo" \
        --output-directory "$LM_FRESH_DEPLOY_ENV_DIR"
}

lm_fresh_epoch_phase() {
    _lfes_python="$1"
    "$_lfes_python" -m liquidity_migration.authorized_deploy_epoch phase \
        --authorization "$LM_CUTOVER_AUTHORIZATION" \
        --output-directory "$LM_FRESH_DEPLOY_ENV_DIR" \
        --plain
}

lm_verify_authorized_deploy_epoch() {
    _lfev_python="$1"
    _lfev_repo="$2"
    _lfev_commit="$3"
    "$_lfev_python" -m liquidity_migration.authorized_deploy_epoch verify \
        --authorization "$LM_CUTOVER_AUTHORIZATION" \
        --expected-commit "$_lfev_commit" \
        --repo-root "$_lfev_repo" \
        --output-directory "$LM_FRESH_DEPLOY_ENV_DIR"
}

lm_verify_active_fresh_processes() {
    _lfpv_python="$1"
    _lfpv_repo="$2"
    _lfpv_commit="$3"
    shift 3
    if [ "$#" -eq 0 ]; then
        echo "fresh-epoch process verification requires at least one active unit" >&2
        return 1
    fi
    _lfpv_command=(
        "$_lfpv_python" -m liquidity_migration.authorized_deploy_epoch verify-processes
        --authorization "$LM_CUTOVER_AUTHORIZATION"
        --expected-commit "$_lfpv_commit"
        --repo-root "$_lfpv_repo"
        --output-directory "$LM_FRESH_DEPLOY_ENV_DIR"
    )
    for _lfpv_unit in "$@"; do
        _lfpv_command+=(--unit "$_lfpv_unit")
    done
    "${_lfpv_command[@]}"
}

lm_load_fresh_epoch_roots() {
    if [ "$#" -ne 3 ]; then
        echo "usage: lm_load_fresh_epoch_roots PYTHON REPO_ROOT EXPECTED_COMMIT" >&2
        return 2
    fi
    local _lfer_python="$1"
    local _lfer_repo="$2"
    local _lfer_commit="$3"
    local _lfer_fd _lfer_pid _lfer_key _lfer_value
    local _lfer_error=""
    local _lfer_stream_failed=0
    local -A _lfer_seen=()
    local -A _lfer_values=()

    # EnvironmentFile syntax is not shell syntax. Reopen the immutable
    # activation latch and transfer only the ten registered root values as
    # NUL-delimited data; never evaluate generated EnvironmentFile bytes.
    exec {_lfer_fd}< <(
        "$_lfer_python" - \
            "$LM_CUTOVER_AUTHORIZATION" \
            "$_lfer_commit" \
            "$_lfer_repo" \
            "$LM_FRESH_DEPLOY_ENV_DIR" <<'PY'
from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

from liquidity_migration.authorized_deploy_epoch import verify_authorized_deploy_epoch


ROOT_BINDINGS = (
    ("DEMO_ACCOUNT_EXECUTION_ROOT", "demo_account"),
    ("DEMO_ACCOUNT_INTENT_INBOX_ROOT", "demo_inbox"),
    ("DEMO_ACCOUNT_CAPTURE_ROOT", "demo_capture"),
    ("PAPER_ACCOUNT_EXECUTION_ROOT", "paper_account"),
    ("PAPER_ACCOUNT_INTENT_INBOX_ROOT", "paper_inbox"),
    ("PAPER_ACCOUNT_CAPTURE_ROOT", "paper_capture"),
    ("LONG_DEMO_DATA_ROOT", "long_demo"),
    ("LONG_PAPER_DATA_ROOT", "long_paper"),
    ("CONTINUOUS_DEMO_DATA_ROOT", "continuous_demo"),
    ("CONTINUOUS_PAPER_DATA_ROOT", "continuous_paper"),
)

result = verify_authorized_deploy_epoch(
    authorization_path=sys.argv[1],
    expected_commit=sys.argv[2],
    repo_root=sys.argv[3],
    output_directory=sys.argv[4],
)
roots = result.get("fresh_roots")
expected_roles = {role for _, role in ROOT_BINDINGS}
if not isinstance(roots, Mapping) or set(roots) != expected_roles:
    raise ValueError("authorized fresh epoch returned an unexpected root-role set")

payload = bytearray()
for variable, role in ROOT_BINDINGS:
    value = roots[role]
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError(f"authorized fresh root {role} is not a nonempty path string")
    if not Path(value).is_absolute():
        raise ValueError(f"authorized fresh root {role} is not absolute")
    payload.extend(variable.encode("ascii"))
    payload.append(0)
    payload.extend(value.encode("utf-8"))
    payload.append(0)

written = sys.stdout.buffer.write(payload)
if written != len(payload):
    raise OSError("fresh-root transfer made a partial write")
sys.stdout.buffer.flush()
PY
    )
    _lfer_pid="$!"

    while IFS= read -r -d '' _lfer_key <&"$_lfer_fd"; do
        if ! IFS= read -r -d '' _lfer_value <&"$_lfer_fd"; then
            _lfer_error="fresh-epoch root transfer ended with an incomplete pair"
            break
        fi
        case "$_lfer_key" in
            DEMO_ACCOUNT_EXECUTION_ROOT|DEMO_ACCOUNT_INTENT_INBOX_ROOT|DEMO_ACCOUNT_CAPTURE_ROOT|\
            PAPER_ACCOUNT_EXECUTION_ROOT|PAPER_ACCOUNT_INTENT_INBOX_ROOT|PAPER_ACCOUNT_CAPTURE_ROOT|\
            LONG_DEMO_DATA_ROOT|LONG_PAPER_DATA_ROOT|\
            CONTINUOUS_DEMO_DATA_ROOT|CONTINUOUS_PAPER_DATA_ROOT)
                ;;
            *)
                _lfer_error="fresh-epoch root transfer returned an unknown key: $_lfer_key"
                break
                ;;
        esac
        if [ "${_lfer_seen[$_lfer_key]+present}" = present ]; then
            _lfer_error="fresh-epoch root transfer repeated key: $_lfer_key"
            break
        fi
        case "$_lfer_value" in
            /*) ;;
            *)
                _lfer_error="fresh-epoch root transfer returned a non-absolute value for $_lfer_key"
                break
                ;;
        esac
        _lfer_seen["$_lfer_key"]=1
        _lfer_values["$_lfer_key"]="$_lfer_value"
    done
    exec {_lfer_fd}<&-
    if ! wait "$_lfer_pid"; then
        _lfer_stream_failed=1
    fi
    if [ -n "$_lfer_error" ]; then
        echo "$_lfer_error" >&2
        return 1
    fi
    if [ "$_lfer_stream_failed" -ne 0 ]; then
        echo "fresh-epoch root transfer failed authority verification" >&2
        return 1
    fi
    if [ "${#_lfer_seen[@]}" -ne 10 ]; then
        echo "fresh-epoch root transfer did not return all ten registered roots" >&2
        return 1
    fi

    DEMO_ACCOUNT_EXECUTION_ROOT="${_lfer_values[DEMO_ACCOUNT_EXECUTION_ROOT]}"
    DEMO_ACCOUNT_INTENT_INBOX_ROOT="${_lfer_values[DEMO_ACCOUNT_INTENT_INBOX_ROOT]}"
    DEMO_ACCOUNT_CAPTURE_ROOT="${_lfer_values[DEMO_ACCOUNT_CAPTURE_ROOT]}"
    PAPER_ACCOUNT_EXECUTION_ROOT="${_lfer_values[PAPER_ACCOUNT_EXECUTION_ROOT]}"
    PAPER_ACCOUNT_INTENT_INBOX_ROOT="${_lfer_values[PAPER_ACCOUNT_INTENT_INBOX_ROOT]}"
    PAPER_ACCOUNT_CAPTURE_ROOT="${_lfer_values[PAPER_ACCOUNT_CAPTURE_ROOT]}"
    LONG_DEMO_DATA_ROOT="${_lfer_values[LONG_DEMO_DATA_ROOT]}"
    LONG_PAPER_DATA_ROOT="${_lfer_values[LONG_PAPER_DATA_ROOT]}"
    CONTINUOUS_DEMO_DATA_ROOT="${_lfer_values[CONTINUOUS_DEMO_DATA_ROOT]}"
    CONTINUOUS_PAPER_DATA_ROOT="${_lfer_values[CONTINUOUS_PAPER_DATA_ROOT]}"

    # Preserve the demo route as the ambient route used by hedge-state checks.
    export ACCOUNT_EXECUTION_ROOT="$DEMO_ACCOUNT_EXECUTION_ROOT"
    export ACCOUNT_INTENT_INBOX_ROOT="$DEMO_ACCOUNT_INTENT_INBOX_ROOT"
    export ACCOUNT_CAPTURE_ROOT="$DEMO_ACCOUNT_CAPTURE_ROOT"
    export LONG_DEMO_DATA_ROOT LONG_PAPER_DATA_ROOT
    export CONTINUOUS_DEMO_DATA_ROOT CONTINUOUS_PAPER_DATA_ROOT
}
