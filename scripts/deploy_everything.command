#!/usr/bin/env bash
# One-click full redeploy, run by the owner. Double-click in Finder or run
# from a terminal. Clicking this IS the decision to restart the fleet on the
# current GitHub main — including the funded real-money units whenever the
# host's REAL_MONEY switch is armed. There are no prompts.
#
# What it does, in order (all existing deploy tooling, nothing new):
#   1. resolve and pin the exact tip of GitHub main
#   2. stop every running fleet unit, funded ones included
#   3. install that exact commit on the host
#   4. activate: start demo and, if armed, the funded fleet
#   5. verify: unit states, installed commit, mainnet on/off
#
# Positions held at the stop stay under their venue-native stops for the
# stopped window, the same posture every staged deploy uses.

set -euo pipefail
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

# Run the controller from the same immutable commit sent to the host. A dirty
# local checkout may contain useful work, but it cannot silently control a
# different deployment. The host is resolved once and passed explicitly.
ssh_target="${SSH_TARGET:-root@208.84.103.4}"
[ -n "$ssh_target" ] || { echo "ERROR: SSH_TARGET is empty" >&2; exit 2; }
controller_parent="$(mktemp -d "${TMPDIR:-/tmp}/liquidity-migration-deploy.XXXXXX")"
controller_root="$controller_parent/controller"
controller_added=0
cleanup() {
    if [ "$controller_added" -eq 1 ]; then
        git -C "$repo_root" worktree remove --force "$controller_root" >/dev/null 2>&1 || true
    fi
    rmdir "$controller_parent" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

echo "== Deploy everything =="
echo "Host gets the tip of GitHub main (not your local checkout)."

git fetch --quiet origin main
deploy_commit="$(git rev-parse 'origin/main^{commit}')"
echo "Deploying: $(git log --oneline -1 "$deploy_commit")"
echo "Target: $ssh_target"
ahead="$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)"
if [ "${ahead}" != 0 ]; then
    echo "note: your local main is ${ahead} commit(s) ahead of GitHub — those are NOT in this deploy. Push first if you want them."
fi

git worktree add --quiet --detach "$controller_root" "$deploy_commit"
controller_added=1
[ "$(git -C "$controller_root" rev-parse HEAD)" = "$deploy_commit" ] || {
    echo "ERROR: detached deploy controller does not match $deploy_commit" >&2
    exit 1
}

echo
(
    cd "$controller_root"
    SSH_TARGET="$ssh_target" EXPECTED_COMMIT="$deploy_commit" \
        scripts/ops.sh deploy rollout --profile operational
)

echo
echo "== Done. The verify table above is the receipt: every unit in its =="
echo "== expected state, and mainnet=on means the funded fleet is live. =="
