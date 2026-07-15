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

# The repository is private. Provider-console recovery must not pretend an
# anonymous GitHub content URL can bootstrap it. Embed the
# exact bytes already present in this trusted local commit; the generated
# command contains no GitHub credential and writes its temporary script 0600
# before making it executable.
encode_commit_file() {
  git show "$commit_sha:$1" | base64 | tr -d '\n'
}

recovery_payload="$(encode_commit_file scripts/vps_console_recover_and_deploy.sh)"
ssh_payload="$(encode_commit_file scripts/vps_restore_ssh_access.sh)"
rescue_payload="$(encode_commit_file scripts/vps_rescue_restore_ssh_access.sh)"

embedded_command() {
  _payload="$1"
  _name="$2"
  _environment="$3"
  cat <<EOF
umask 077
_liqmig_script=\$(mktemp "/root/${_name}.XXXXXX")
trap 'rm -f "\$_liqmig_script"' EXIT
printf '%s' '$_payload' | base64 --decode > "\$_liqmig_script"
chmod 0700 "\$_liqmig_script"
${_environment}"\$_liqmig_script"
rm -f "\$_liqmig_script"
trap - EXIT
EOF
}

recommended_command="$(embedded_command \
  "$recovery_payload" \
  liquidity-migration-console-recovery \
  "EXPECTED_COMMIT='$commit_sha' CLEAN_DIRTY_CHECKOUT=1 ")"
strict_command="$(embedded_command \
  "$recovery_payload" \
  liquidity-migration-console-recovery \
  "EXPECTED_COMMIT='$commit_sha' ")"
ssh_command="$(embedded_command \
  "$ssh_payload" \
  liquidity-migration-ssh-recovery \
  "")"

rescue_command="$(embedded_command \
  "$rescue_payload" \
  liquidity-migration-rescue-recovery \
  "")"

if [ "$mode" = "recommended_only" ]; then
  printf '%s\n' "$recommended_command"
  exit 0
fi

if [ "$mode" = "rescue_only" ]; then
  printf '%s\n' "$rescue_command"
  exit 0
fi

cat <<EOF
# Generated from exact commit $commit_sha in this trusted local checkout.
# Inspect the command here before pasting it into a provider console.

# Minimal SSH-only recovery, as root:
$ssh_command

# Hetzner Rescue SSH-key restore, as rescue root:
$rescue_command

# Checked deploy from this checkout after SSH-only recovery:
EXPECTED_COMMIT="$commit_sha" scripts/deploy_vps_live.sh
EXPECTED_COMMIT="$commit_sha" scripts/verify_vps_live.sh

# Wait locally for restored SSH access, then deploy and verify:
EXPECTED_COMMIT="$commit_sha" scripts/wait_for_vps_recovery_and_deploy.sh

# Recommended full Hetzner Cloud console recovery, as root:
# Open the Hetzner Cloud web console for 116.202.15.128, then paste this
# into the installed OS shell as root. If that console is unavailable, enable
# Hetzner Rescue and use the rescue command above first.
$recommended_command

# Strict full recovery that refuses a dirty /opt/liquidity-migration checkout:
$strict_command

# Read-only verification from this checkout after full console recovery:
EXPECTED_COMMIT="$commit_sha" scripts/verify_vps_live.sh
EOF
