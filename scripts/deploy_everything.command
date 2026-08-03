#!/usr/bin/env bash
# One-click full redeploy, run by the owner. Double-click in Finder or run
# from a terminal. Clicking this IS the decision to restart the fleet on the
# current GitHub main — including the funded real-money units whenever the
# host's REAL_MONEY switch is armed. There are no prompts.
#
# What it does, in order (all existing deploy tooling, nothing new):
#   1. stop every running fleet unit, funded ones included (--stop-first)
#   2. install the tip of GitHub main on the host
#   3. activate: start demo and, if armed, the funded fleet
#   4. verify: unit states, installed commit, mainnet on/off
#
# Positions held at the stop stay under their venue-native stops for the
# stopped window, the same posture every staged deploy uses.

set -euo pipefail
cd "$(dirname "$0")/.."

echo "== Deploy everything =="
echo "Host gets the tip of GitHub main (not your local checkout)."

git fetch --quiet origin main || echo "note: could not reach GitHub to compare; the host install will fail too if GitHub is down"
echo "Deploying: $(git log --oneline -1 origin/main)"
ahead="$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)"
if [ "${ahead}" != 0 ]; then
    echo "note: your local main is ${ahead} commit(s) ahead of GitHub — those are NOT in this deploy. Push first if you want them."
fi

echo
scripts/ops.sh deploy staged --profile operational --stop-first

echo
echo "== Done. The verify table above is the receipt: every unit in its =="
echo "== expected state, and mainnet=on means the funded fleet is live. =="
