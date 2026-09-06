#!/usr/bin/env bash
# GitHub runners supply the private key and both pinned fingerprints.
set -euo pipefail

test -n "$VPS_SSH_PRIVATE_KEY"
install -d -m 700 ~/.ssh
printf '%s\n' "$VPS_SSH_PRIVATE_KEY" > ~/.ssh/vps_deploy_key
chmod 600 ~/.ssh/vps_deploy_key
ssh-keygen -y -f ~/.ssh/vps_deploy_key > ~/.ssh/vps_deploy_key.pub
ssh-keygen -lf ~/.ssh/vps_deploy_key.pub -E sha256 |
  grep -F "$GITHUB_ACTIONS_DEPLOY_KEY_FINGERPRINT"
ssh-keyscan -T 10 -t ed25519 -- "$VPS_HOST" > ~/.ssh/known_hosts.candidate
test -s ~/.ssh/known_hosts.candidate
host_fingerprints="$(ssh-keygen -lf ~/.ssh/known_hosts.candidate -E sha256 | awk '{print $2}' | sort -u)"
test "$host_fingerprints" = "$VPS_ED25519_FINGERPRINT"
mv ~/.ssh/known_hosts.candidate ~/.ssh/known_hosts
chmod 600 ~/.ssh/known_hosts
