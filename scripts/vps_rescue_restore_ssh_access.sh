#!/usr/bin/env bash
set -euo pipefail

LOCAL_SSH_PUBLIC_KEY="${SSH_PUBLIC_KEY:-ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFwJNtc1cVhkzNKmxmq6mogten+Q/5yfLulf9wxZxMNp hetzner}"
GITHUB_ACTIONS_SSH_PUBLIC_KEY="${GITHUB_ACTIONS_SSH_PUBLIC_KEY:-ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKykZKBc1KapzJXdFORWMhjaNFC4zPeEZkOAbu32aTXX model050426-github-actions-20260519}"
TARGET_ROOT="${TARGET_ROOT:-}"
MOUNT_ROOT="${MOUNT_ROOT:-/mnt/model050426-root}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this from the VPS provider rescue console as root." >&2
  exit 1
fi

is_installed_root() {
  [ -d "$1/etc" ] && [ -f "$1/etc/passwd" ] && [ -d "$1/root" ]
}

mounted_here=0
target_root=""

if [ -n "$TARGET_ROOT" ]; then
  if is_installed_root "$TARGET_ROOT"; then
    target_root="$TARGET_ROOT"
  else
    echo "TARGET_ROOT does not look like an installed Linux root: $TARGET_ROOT" >&2
    exit 1
  fi
elif is_installed_root /mnt; then
  target_root="/mnt"
else
  command -v vgchange >/dev/null 2>&1 && vgchange -ay || true
  mkdir -p "$MOUNT_ROOT"
  while IFS= read -r device; do
    if mount "$device" "$MOUNT_ROOT" 2>/dev/null; then
      if is_installed_root "$MOUNT_ROOT"; then
        target_root="$MOUNT_ROOT"
        mounted_here=1
        echo "Mounted installed root from $device at $target_root"
        break
      fi
      umount "$MOUNT_ROOT" 2>/dev/null || true
    fi
  done < <(
    lsblk -rpno NAME,FSTYPE,TYPE,MOUNTPOINT |
      awk '$4 == "" && ($3 == "part" || $3 == "lvm") && $2 ~ /^(ext2|ext3|ext4|xfs|btrfs)$/ {print $1}'
  )
fi

if [ -z "$target_root" ]; then
  echo "Could not auto-detect the installed root filesystem." >&2
  echo "Mount it manually, then rerun with TARGET_ROOT=/mounted/root." >&2
  exit 1
fi

chown root:root "$target_root/root"
chmod 700 "$target_root/root"

mkdir -p "$target_root/root/.ssh"
chmod 700 "$target_root/root/.ssh"
touch "$target_root/root/.ssh/authorized_keys"
chmod 600 "$target_root/root/.ssh/authorized_keys"
for public_key in "$LOCAL_SSH_PUBLIC_KEY" "$GITHUB_ACTIONS_SSH_PUBLIC_KEY"; do
  if ! grep -Fxq "$public_key" "$target_root/root/.ssh/authorized_keys"; then
    printf '%s\n' "$public_key" >> "$target_root/root/.ssh/authorized_keys"
  fi
done
chown -R root:root "$target_root/root/.ssh"

if command -v chroot >/dev/null 2>&1; then
  chroot "$target_root" usermod -U root 2>/dev/null || true
fi

if [ -d "$target_root/etc/ssh" ]; then
  mkdir -p "$target_root/etc/ssh/sshd_config.d"
  cat >"$target_root/etc/ssh/sshd_config.d/99-model050426-recovery.conf" <<'SSH_CONFIG'
PubkeyAuthentication yes
PermitRootLogin prohibit-password
AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys2
AuthenticationMethods publickey
SSH_CONFIG
  if [ -f "$target_root/etc/ssh/sshd_config" ] &&
    ! grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' "$target_root/etc/ssh/sshd_config"; then
    cp "$target_root/etc/ssh/sshd_config" "$target_root/etc/ssh/sshd_config.model050426-backup.$(date -u +%Y%m%dT%H%M%SZ)"
    tmp_sshd_config="$(mktemp)"
    printf '%s\n' 'Include /etc/ssh/sshd_config.d/*.conf' > "$tmp_sshd_config"
    cat "$target_root/etc/ssh/sshd_config" >> "$tmp_sshd_config"
    cat "$tmp_sshd_config" > "$target_root/etc/ssh/sshd_config"
    rm -f "$tmp_sshd_config"
  fi
fi

if command -v ssh-keygen >/dev/null 2>&1; then
  echo "Restored authorized key fingerprints in $target_root/root/.ssh/authorized_keys:"
  for public_key in "$LOCAL_SSH_PUBLIC_KEY" "$GITHUB_ACTIONS_SSH_PUBLIC_KEY"; do
    tmp_public_key="$(mktemp)"
    printf '%s\n' "$public_key" > "$tmp_public_key"
    ssh-keygen -lf "$tmp_public_key" -E sha256
    rm -f "$tmp_public_key"
  done
fi

sync
if [ "$mounted_here" = "1" ]; then
  umount "$target_root"
fi

echo "rescue-ssh-restore-ok"
echo "Reboot the VPS from local disk, then run the checked deploy from your local checkout:"
echo 'EXPECTED_COMMIT="$(git rev-parse HEAD)" scripts/deploy_vps_live.sh'
echo 'EXPECTED_COMMIT="$(git rev-parse HEAD)" scripts/verify_vps_live.sh'
