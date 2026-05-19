#!/usr/bin/env bash
set -euo pipefail

LOCAL_SSH_PUBLIC_KEY="${SSH_PUBLIC_KEY:-ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFwJNtc1cVhkzNKmxmq6mogten+Q/5yfLulf9wxZxMNp hetzner}"
GITHUB_ACTIONS_SSH_PUBLIC_KEY="${GITHUB_ACTIONS_SSH_PUBLIC_KEY:-ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKykZKBc1KapzJXdFORWMhjaNFC4zPeEZkOAbu32aTXX model050426-github-actions-20260519}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this from the VPS provider console as root." >&2
  exit 1
fi

if command -v apt-get >/dev/null 2>&1 && ! command -v sshd >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y openssh-server
fi

chown root:root /root
chmod 700 /root
usermod -U root 2>/dev/null || true

mkdir -p /root/.ssh
chmod 700 /root/.ssh
touch /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
for public_key in "$LOCAL_SSH_PUBLIC_KEY" "$GITHUB_ACTIONS_SSH_PUBLIC_KEY"; do
  if ! grep -Fxq "$public_key" /root/.ssh/authorized_keys; then
    printf '%s\n' "$public_key" >> /root/.ssh/authorized_keys
  fi
done
chown -R root:root /root/.ssh
if command -v ssh-keygen >/dev/null 2>&1; then
  echo "Restored authorized key fingerprints:"
  for public_key in "$LOCAL_SSH_PUBLIC_KEY" "$GITHUB_ACTIONS_SSH_PUBLIC_KEY"; do
    tmp_public_key="$(mktemp)"
    printf '%s\n' "$public_key" > "$tmp_public_key"
    ssh-keygen -lf "$tmp_public_key" -E sha256
    rm -f "$tmp_public_key"
  done
fi

if [ -d /etc/ssh ]; then
  mkdir -p /etc/ssh/sshd_config.d
  cat >/etc/ssh/sshd_config.d/99-model050426-recovery.conf <<'SSH_CONFIG'
PubkeyAuthentication yes
PermitRootLogin prohibit-password
AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys2
AuthenticationMethods publickey
SSH_CONFIG
  if [ -f /etc/ssh/sshd_config ] && ! grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config; then
    cp /etc/ssh/sshd_config "/etc/ssh/sshd_config.model050426-backup.$(date -u +%Y%m%dT%H%M%SZ)"
    tmp_sshd_config="$(mktemp)"
    printf '%s\n' 'Include /etc/ssh/sshd_config.d/*.conf' > "$tmp_sshd_config"
    cat /etc/ssh/sshd_config >> "$tmp_sshd_config"
    cat "$tmp_sshd_config" > /etc/ssh/sshd_config
    rm -f "$tmp_sshd_config"
  fi
fi

if command -v sshd >/dev/null 2>&1; then
  sshd -t
  sshd_root_context="user=root,host=localhost,addr=127.0.0.1"
  effective_sshd_config="$(sshd -T -C "$sshd_root_context")"
  printf '%s\n' "$effective_sshd_config" | grep -E '^(pubkeyauthentication|permitrootlogin|authorizedkeysfile|authenticationmethods) '
  printf '%s\n' "$effective_sshd_config" | grep -Eq '^pubkeyauthentication yes$'
  printf '%s\n' "$effective_sshd_config" | grep -Eq '^permitrootlogin (yes|without-password|prohibit-password)$'
  printf '%s\n' "$effective_sshd_config" | grep -Eq '^authorizedkeysfile .*[.]ssh/authorized_keys'
  printf '%s\n' "$effective_sshd_config" | grep -Eq '^authenticationmethods publickey$'
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl restart ssh.service || systemctl restart sshd.service || true
else
  service ssh restart || service sshd restart || true
fi

echo "ssh-restore-ok"
