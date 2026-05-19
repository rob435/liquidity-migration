#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

mode="all"
case "${1:-}" in
  --recommended-only)
    mode="recommended_only"
    shift
    ;;
  --rescue-only)
    mode="rescue_only"
    shift
    ;;
esac

commit_ref="${1:-HEAD}"
commit_sha="$(git rev-parse "${commit_ref}^{commit}")"
raw_base="${RAW_BASE:-https://raw.githubusercontent.com/rob435/MODEL05042026}"
script_url="$raw_base/$commit_sha/scripts/vps_console_recover_and_deploy.sh"
ssh_script_url="$raw_base/$commit_sha/scripts/vps_restore_ssh_access.sh"
rescue_script_url="$raw_base/$commit_sha/scripts/vps_rescue_restore_ssh_access.sh"

recommended_command="$(cat <<EOF
apt-get update && apt-get install -y ca-certificates curl
curl -fsSL $script_url | EXPECTED_COMMIT="$commit_sha" CLEAN_DIRTY_CHECKOUT=1 bash
EOF
)"

rescue_command="$(cat <<EOF
apt-get update && apt-get install -y ca-certificates curl
curl -fsSL $rescue_script_url | bash
EOF
)"

if [ "$mode" = "recommended_only" ]; then
  printf '%s\n' "$recommended_command"
  exit 0
fi

if [ "$mode" = "rescue_only" ]; then
  printf '%s\n' "$rescue_command"
  exit 0
fi

cat <<EOF
# Minimal SSH-only recovery, as root:
apt-get update && apt-get install -y ca-certificates curl
curl -fsSL $ssh_script_url | bash

# Hetzner Rescue SSH-key restore, as rescue root:
$rescue_command

# Checked deploy from this checkout after SSH-only recovery:
EXPECTED_COMMIT="$commit_sha" scripts/deploy_vps_live.sh
EXPECTED_COMMIT="$commit_sha" scripts/verify_vps_live.sh

# Wait locally for restored SSH access, then deploy and verify:
EXPECTED_COMMIT="$commit_sha" scripts/wait_for_vps_recovery_and_deploy.sh

# Recommended full VPS provider console recovery, as root:
$recommended_command

# Strict full recovery that refuses a dirty /opt/MODEL050426 checkout:
apt-get update && apt-get install -y ca-certificates curl
curl -fsSL $script_url | EXPECTED_COMMIT="$commit_sha" bash

# Read-only verification from this checkout after full console recovery:
EXPECTED_COMMIT="$commit_sha" scripts/verify_vps_live.sh
EOF
