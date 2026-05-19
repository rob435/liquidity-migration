#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

commit_ref="${1:-HEAD}"
commit_sha="$(git rev-parse "${commit_ref}^{commit}")"
raw_base="${RAW_BASE:-https://raw.githubusercontent.com/rob435/MODEL05042026}"
script_url="$raw_base/$commit_sha/scripts/vps_console_recover_and_deploy.sh"
ssh_script_url="$raw_base/$commit_sha/scripts/vps_restore_ssh_access.sh"

cat <<EOF
# Minimal SSH-only recovery, as root:
apt-get update && apt-get install -y ca-certificates curl
curl -fsSL $ssh_script_url | bash

# VPS provider console recovery, as root:
apt-get update && apt-get install -y ca-certificates curl
curl -fsSL $script_url | EXPECTED_COMMIT="$commit_sha" bash

# If /opt/MODEL050426 is dirty and should be reset after saving a patch:
apt-get update && apt-get install -y ca-certificates curl
curl -fsSL $script_url | EXPECTED_COMMIT="$commit_sha" CLEAN_DIRTY_CHECKOUT=1 bash

# Read-only verification from this checkout after SSH is restored:
EXPECTED_COMMIT="$commit_sha" scripts/verify_vps_live.sh
EOF
