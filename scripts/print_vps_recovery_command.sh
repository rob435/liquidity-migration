#!/usr/bin/env bash
# Print credential-free, exact-commit SSH recovery material and staged next steps.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mode=normal
if [ "${1:-}" = --rescue-only ]; then mode=rescue; shift; fi
commit="$(git rev-parse "${1:-HEAD}^{commit}")"

encode() { git show "$commit:$1" | base64 | tr -d '\n'; }
embedded() {
    local payload="$1" name="$2"
    cat <<EOF
umask 077
_script=\$(mktemp "/root/${name}.XXXXXX")
trap 'rm -f "\$_script"' EXIT
printf '%s' '$payload' | base64 --decode > "\$_script"
chmod 0700 "\$_script"
"\$_script"
EOF
}

restore="$(embedded "$(encode scripts/vps_restore_ssh_access.sh)" liquidity-migration-ssh-restore)"
rescue="$(embedded "$(encode scripts/vps_rescue_restore_ssh_access.sh)" liquidity-migration-rescue-restore)"

if [ "$mode" = rescue ]; then
    printf '%s\n' "$rescue"
    exit 0
fi

cat <<EOF
# Generated from exact commit $commit. Inspect before using a provider console.

# Installed OS console, as root:
$restore

# Rescue system, as root:
$rescue

# After SSH is restored, run from this trusted local checkout. Install requires
# the whole liquidity-migration fleet to be stopped and never starts a unit.
EXPECTED_COMMIT="$commit" scripts/deploy_vps_live.sh install

# Issue a new exact-head authorization only after configuring and reviewing the
# stopped host. Then activate and verify without another checkout/config edit:
EXPECTED_COMMIT="$commit" scripts/deploy_vps_live.sh activate
EXPECTED_COMMIT="$commit" scripts/deploy_vps_live.sh verify
EOF
