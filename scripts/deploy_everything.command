#!/usr/bin/env bash
# One-click full redeploy, run by the owner. Double-click in Finder or run
# from a terminal. Clicking this IS the decision to restart the fleet on the
# current GitHub main — including the funded real-money units whenever the
# host's REAL_MONEY switch is armed. There are no prompts.
#
# Positions held at the stop stay under their venue-native stops for the
# stopped window.

set -euo pipefail
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

ssh_target="${SSH_TARGET:-root@208.84.103.4}"
[ -n "$ssh_target" ] || { echo "ERROR: SSH_TARGET is empty" >&2; exit 2; }

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

echo
SSH_TARGET="$ssh_target" EXPECTED_COMMIT="$deploy_commit" scripts/ops.sh deploy

echo
echo "== Done. The verify table above is the receipt: every unit in its =="
echo "== expected state, and real-money armed means the funded fleet is live. =="
