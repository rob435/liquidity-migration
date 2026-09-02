#!/usr/bin/env bash
# Off-box copy of the state that cannot be rebuilt: the engines' logs, closed
# trades, and heartbeats, the workers' checkpoints, the target books and
# spools, the retired producers' takeover sources, and the two rendered engine
# configs whose hashes the logs name. Code lives in git and market data is
# re-captured; the WAL is the account's own memory, and without this copy it
# exists on exactly one disk.
#
# Two hops. First a local snapshot, because the engines append to their logs
# while this runs and a cloud upload of a growing file fails its size check.
# Then rclone mirrors the snapshot to BACKUP_REMOTE/latest. A file that
# changed or vanished since the last run is moved to
# BACKUP_REMOTE/history/<run stamp>/ instead of being overwritten, so one bad
# run cannot destroy the only copy of a good one; history older than
# BACKUP_HISTORY_DAYS is deleted.
#
# A WAL copied mid-append lands with a torn last frame. That is the shape the
# reader already tolerates — replay cuts a torn tail — so the copy is a valid
# log as of a moment ago, not a corrupt one.
#
# No credential file is ever a source here: a *.env path is refused by name.
set -euo pipefail

REMOTE="${BACKUP_REMOTE:-gdrive:LiquidityMigration/engine-state}"
STAMP="${BACKUP_STAMP_FILE:-/var/lib/liquidity-migration/receipts/backup.last-success}"
STAGE="${BACKUP_STAGE_DIR:-/var/lib/liquidity-migration/backup/stage}"
HISTORY_DAYS="${BACKUP_HISTORY_DAYS:-60}"
CONFIG="${RCLONE_CONFIG:-/var/lib/liquidity-migration/backup/rclone.conf}"
CONFIG_SEED="${RCLONE_CONFIG_SEED:-}"
RCLONE="${RCLONE_BIN:-/usr/bin/rclone}"
RSYNC="${RSYNC_BIN:-rsync}"
DEFAULT_SOURCES="/var/lib/liquidity-migration-engine \
/var/lib/liquidity-migration-engine-mainnet \
/var/lib/liquidity-migration-signal-worker-demo \
/var/lib/liquidity-migration-signal-worker-mainnet \
/var/lib/liquidity-migration/targets \
/var/lib/liquidity-migration/signals \
/var/lib/liquidity-migration/controls \
/etc/liquidity-migration/engine.toml \
/etc/liquidity-migration/engine-mainnet.toml"
SOURCES="${BACKUP_SOURCES:-$DEFAULT_SOURCES}"

case "$REMOTE" in
    *:*) ;;
    *) echo "backup: BACKUP_REMOTE must be an rclone remote (remote:path): $REMOTE" >&2; exit 2 ;;
esac
[ -x "$RCLONE" ] || { echo "backup: rclone is not executable: $RCLONE" >&2; exit 2; }
command -v "$RSYNC" >/dev/null 2>&1 || { echo "backup: rsync is not installed" >&2; exit 2; }
[[ "$HISTORY_DAYS" =~ ^[1-9][0-9]*$ ]] || { echo "backup: BACKUP_HISTORY_DAYS must be a positive integer" >&2; exit 2; }

present=()
# shellcheck disable=SC2086 # the env var is a deliberate space-separated list
for source in $SOURCES; do
    case "$source" in
        *.env|*.env.*) echo "backup: refusing to copy a credential file: $source" >&2; exit 2 ;;
        /*) ;;
        *) echo "backup: sources must be absolute paths: $source" >&2; exit 2 ;;
    esac
    [ -e "$source" ] && present+=("$source")
done
[ "${#present[@]}" -gt 0 ] || { echo "backup: none of the sources exist on this host" >&2; exit 2; }

umask 077
install -d -m 0700 "$STAGE" "$(dirname "$CONFIG")"
# The watchdog runs as another user and reads only the stamp's age.
install -d -m 0755 "$(dirname "$STAMP")"
if command -v flock >/dev/null 2>&1; then
    exec 9>"$(dirname "$STAGE")/backup.lock"
    if ! flock -n 9; then
        echo "backup: another run owns the backup lock"
        exit 0
    fi
fi
if [ -n "$CONFIG_SEED" ]; then
    [ -f "$CONFIG_SEED" ] || { echo "backup: rclone config seed is missing: $CONFIG_SEED" >&2; exit 2; }
    if [ ! -f "$CONFIG" ] || [ "$CONFIG_SEED" -nt "$CONFIG" ]; then
        CONFIG_TEMP="$(dirname "$CONFIG")/.rclone.conf.seed.$$"
        install -m 0600 "$CONFIG_SEED" "$CONFIG_TEMP"
        mv "$CONFIG_TEMP" "$CONFIG"
    fi
fi
[ -f "$CONFIG" ] || { echo "backup: rclone config is missing: $CONFIG" >&2; exit 2; }

# --relative keeps each source's full path under the stage, so two engines'
# files cannot collide; --delete drops what the host no longer has, and the
# remote history keeps what that removed.
"$RSYNC" -a --relative --delete --exclude='*.env' --exclude='*.env.*' \
    --exclude='*.tmp' --exclude='*.tmp.*' \
    --exclude='*.sock' --exclude='.target-book-objects' --exclude='archive' \
    "${present[@]}" "$STAGE/"

REMOTE="${REMOTE%/}"
RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
"$RCLONE" mkdir "$REMOTE/history" --config "$CONFIG"
"$RCLONE" sync "$STAGE" "$REMOTE/latest" \
    --config "$CONFIG" \
    --backup-dir "$REMOTE/history/$RUN_STAMP" \
    --transfers 4 \
    --checkers 8 \
    --drive-chunk-size 32M \
    --retries 5 \
    --low-level-retries 10
"$RCLONE" check "$STAGE" "$REMOTE/latest" \
    --config "$CONFIG" \
    --one-way
"$RCLONE" delete "$REMOTE/history" \
    --config "$CONFIG" \
    --min-age "${HISTORY_DAYS}d" \
    --rmdirs

FILES="$(find "$STAGE" -type f | wc -l | tr -d ' ')"
KILOBYTES="$(du -sk "$STAGE" | cut -f1)"
FREE="$(
    "$RCLONE" about "${REMOTE%%:*}:" --config "$CONFIG" --json 2>/dev/null \
        | sed -n 's/.*"free":[[:space:]]*\([0-9]*\).*/\1/p' | head -1
)"

# The stamp is the receipt the watchdog reads the age of. Written only after
# every step above returned success, so its mtime is the time of the last copy
# that actually landed and was checked, not the last one attempted.
STAMP_TEMP="$(dirname "$STAMP")/.$(basename "$STAMP").$$"
{
    printf 'backed_up_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'run_stamp=%s\n' "$RUN_STAMP"
    printf 'file_count=%s\n' "$FILES"
    printf 'bytes=%s\n' "$((KILOBYTES * 1024))"
    printf 'destination=%s\n' "$REMOTE"
    printf 'remote_free_bytes=%s\n' "${FREE:-}"
} > "$STAMP_TEMP"
chmod 0644 "$STAMP_TEMP"
mv "$STAMP_TEMP" "$STAMP"
echo "backup: landed files=$FILES bytes=$((KILOBYTES * 1024)) destination=$REMOTE/latest history=$RUN_STAMP"
