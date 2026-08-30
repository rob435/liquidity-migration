#!/usr/bin/env bash
# One-click full redeploy, run by the owner. Double-click in Finder or run
# from a terminal. Clicking this IS the decision to restart the fleet on the
# current GitHub main — including the funded real-money units whenever the
# host's REAL_MONEY switch is armed. There are no prompts.
#
# What it does, in order (all existing deploy tooling, nothing new):
#   1. resolve and pin the exact tip of GitHub main
#   2. build and fetch that exact commit beside the running fleet
#   3. let rollout quiesce every managed unit, funded ones included
#   4. install, then start demo and, if armed, the funded fleet
#   5. verify unit states, installed commit, and mainnet on/off
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
cleanup() {
    [ -d "$controller_parent" ] && rm -rf -- "$controller_parent"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

echo "== Deploy everything =="
echo "Host gets the tip of GitHub main (not your local checkout)."

git fetch --quiet origin main
deploy_commit="$(git rev-parse 'origin/main^{commit}')"
origin_url="$(git remote get-url origin)"
[ -n "$origin_url" ] || { echo "ERROR: origin URL is empty" >&2; exit 2; }
echo "Deploying: $(git log --oneline -1 "$deploy_commit")"
echo "Target: $ssh_target"
ahead="$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)"
if [ "${ahead}" != 0 ]; then
    echo "note: your local main is ${ahead} commit(s) ahead of GitHub — those are NOT in this deploy. Push first if you want them."
fi

git clone --quiet --no-local --no-checkout "$repo_root" "$controller_root"
git -C "$controller_root" fetch --quiet --no-tags "$repo_root" "$deploy_commit"
git -C "$controller_root" remote set-url origin "$origin_url"
git -C "$controller_root" update-ref refs/remotes/origin/main "$deploy_commit"
git -C "$controller_root" checkout --quiet --detach "$deploy_commit"
[ -d "$controller_root/.git" ] || {
    echo "ERROR: deploy controller has no real .git directory" >&2
    exit 1
}
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
