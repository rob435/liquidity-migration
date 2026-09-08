#!/usr/bin/env bash
# One-command VPS deploy, rollback, read-only verify, and the funded safety stops.
#
# deploy: verify its release binaries, fetch the exact commit, install, restart the fleet. A realm
#   that does not publish a fresh heartbeat is rolled back only when the
#   predecessor uses identical runtime inputs; otherwise repair forward.
# rollback: deploy the last commit whose deploy finished (or, when the current
#   one finished, the one before it).
# verify: read-only fleet summary.
# stop-mainnet, stop-mexc: stop that funded realm's units; exposure is unchanged.
# disarm-mainnet, disarm-mexc: stop that realm's units and set REAL_MONEY=false
#   in its own credential file.
#
# Units the manifest marks independent (the market recorder, its upload, the
# state backup, the host watchdog) are never stopped by any mode here; deploy
# restarts the recorder only when its own inputs changed.
set -euo pipefail

deploy_usage() {
    cat >&2 <<'USAGE'
usage: deploy_vps_live.sh {deploy|rollback|verify|stop-mainnet|disarm-mainnet|stop-mexc|disarm-mexc}
  EXPECTED_COMMIT=<40-hex>   exact commit to deploy (default: origin/main tip)
USAGE
    exit 2
}

MODE="${1:-verify}"
[ "$#" -le 1 ] || deploy_usage
case "$MODE" in
    deploy|rollback|verify|stop-mainnet|disarm-mainnet|stop-mexc|disarm-mexc) ;;
    *) deploy_usage ;;
esac

SSH_TARGET="${SSH_TARGET:-root@208.84.103.4}"
SSH_OPTS="${SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3}"
REPO_URL="${REPO_URL:-https://github.com/rob435/liquidity-migration.git}"
REPO_DIR="${REPO_DIR:-/opt/liquidity-migration}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"
EXPECTED_COMMIT="${EXPECTED_COMMIT:-}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"

if [ -n "$EXPECTED_COMMIT" ] && [[ ! "$EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
    echo "EXPECTED_COMMIT must be a full lowercase 40-character commit" >&2
    exit 2
fi

SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
LOCAL_REPOSITORY="$(cd -P -- "$SCRIPT_DIRECTORY/.." && pwd)"
if [ -z "$EXPECTED_COMMIT" ]; then
    EXPECTED_COMMIT="$(
        git -C "$LOCAL_REPOSITORY" rev-parse --verify --quiet \
            "refs/remotes/$REMOTE/$BRANCH^{commit}" 2>/dev/null \
        || git -C "$LOCAL_REPOSITORY" rev-parse --verify 'HEAD^{commit}'
    )" || { echo "cannot resolve a default EXPECTED_COMMIT" >&2; exit 2; }
    echo "EXPECTED_COMMIT defaulted to $EXPECTED_COMMIT" >&2
fi

if { [ "$MODE" = deploy ] || [ "$MODE" = rollback ]; } && [ -z "$GITHUB_TOKEN" ] \
    && [[ "$REPO_URL" == https://github.com/* ]] && command -v gh >/dev/null 2>&1; then
    GITHUB_TOKEN="$(gh auth token --hostname github.com 2>/dev/null || true)"
fi

stage_release_binaries() {
    local commit="$1" target="$2"
    local stage_target="/opt/liquidity-migration-engine/staged/${commit}.tar.gz"
    if ssh "${SSH_ARGS[@]}" "$target" "test -f '$stage_target'" 2>/dev/null; then
        echo "deploy: release artifact already staged; remote verification follows ($stage_target)" >&2
        return 0
    fi
    command -v gh >/dev/null 2>&1 \
        || { echo "deploy: stage a release artifact for $commit; gh is unavailable" >&2; return 1; }
    local artifact_name="engine-binaries-${commit}"
    local run_id=""
    run_id="$(gh api "/repos/rob435/liquidity-migration/actions/artifacts?name=${artifact_name}" --jq 'first(.artifacts[] | select(.expired == false)) | .workflow_run.id' 2>/dev/null || true)"
    if [ -z "$run_id" ] || [ "$run_id" = "null" ]; then
        echo "deploy: no release artifact found for $commit; dispatch deploy to build the release artifact" >&2
        return 1
    fi
    local run_identity
    run_identity="$(gh run view "$run_id" --repo rob435/liquidity-migration --json headSha,conclusion --jq '[.headSha,.conclusion] | join(" ")' 2>/dev/null || true)"
    [ "$run_identity" = "$commit success" ] \
        || { echo "deploy: artifact run $run_id is not successful for $commit" >&2; return 1; }
    echo "deploy: downloading release binaries from GitHub Actions (run $run_id)..." >&2
    local tmp_dir
    tmp_dir="$(mktemp -d)" || return 1
    local staged=1
    if gh run download "$run_id" --repo rob435/liquidity-migration -n "$artifact_name" -D "$tmp_dir" >/dev/null 2>&1; then
        local tarball="$tmp_dir/$artifact_name.tar.gz"
        if python3 "$LOCAL_REPOSITORY/scripts/release_artifact.py" verify \
            --commit "$commit" --artifact "$tarball" >/dev/null; then
            echo "deploy: staging release binaries onto VPS ($target:$stage_target)..." >&2
            if ssh "${SSH_ARGS[@]}" "$target" "mkdir -p /opt/liquidity-migration-engine/staged" \
                && scp "${SSH_ARGS[@]}" "$tarball" "$target:$stage_target.partial" \
                && ssh "${SSH_ARGS[@]}" "$target" "mv -f '$stage_target.partial' '$stage_target'"; then
                staged=0
            else
                ssh "${SSH_ARGS[@]}" "$target" "rm -f '$stage_target.partial'" 2>/dev/null || true
            fi
        fi
    fi
    rm -rf "$tmp_dir"
    [ "$staged" = 0 ] || echo "deploy: could not stage a release artifact for $commit" >&2
    return "$staged"
}

read -r -a SSH_ARGS <<< "$SSH_OPTS"
if [ "$MODE" = deploy ]; then
    stage_release_binaries "$EXPECTED_COMMIT" "$SSH_TARGET"
fi
{
    printf 'MODE=%q\n' "$MODE"
    printf 'REPO_URL=%q\n' "$REPO_URL"
    printf 'REPO_DIR=%q\n' "$REPO_DIR"
    printf 'REMOTE=%q\n' "$REMOTE"
    printf 'BRANCH=%q\n' "$BRANCH"
    printf 'EXPECTED_COMMIT=%q\n' "$EXPECTED_COMMIT"
    printf 'GITHUB_TOKEN=%q\n' "$GITHUB_TOKEN"
    if [ "$MODE" = deploy ] || [ "$MODE" = rollback ]; then
        # The verifier must survive a rollback to a checkout that predates it.
        printf 'RELEASE_ARTIFACT_PY=%q\n' "$(cat "$LOCAL_REPOSITORY/scripts/release_artifact.py")"
    fi
    cat "$LOCAL_REPOSITORY/scripts/vps/deploy_remote.sh"
} | ssh "${SSH_ARGS[@]}" -- "$SSH_TARGET" bash -s
