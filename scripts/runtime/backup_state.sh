#!/usr/bin/env bash
# Off-box copy of the state that cannot be rebuilt: the engines' logs and the
# closed-trade files. Code lives in git and market data is re-capturable; the
# WAL is the account's own memory, and it exists on exactly one disk until
# this runs.
#
# Unconfigured is not an error. An owner who has not set a destination gets a
# note and exit 0, never a failed unit — the watchdog's backup_stale check
# only arms once LIVENESS_BACKUP_STAMP_FILE is set on the liveness unit, so
# nothing pages about a backup nobody asked for.
#
# A WAL copied mid-append lands with a torn last frame. That is the shape the
# reader already tolerates — replay cuts a torn tail — so the copy is a valid
# log as of a moment ago, not a corrupt one.
set -euo pipefail

DEST="${BACKUP_RSYNC_DEST:-}"
if [ -z "$DEST" ]; then
    echo "backup: BACKUP_RSYNC_DEST is not set; nothing to do"
    exit 0
fi
STAMP="${BACKUP_STAMP_FILE:?BACKUP_STAMP_FILE is required once BACKUP_RSYNC_DEST is set}"
SOURCES="${BACKUP_SOURCES:?BACKUP_SOURCES is required once BACKUP_RSYNC_DEST is set (space-separated absolute paths)}"

# --relative keeps each source's full path under the destination, so two
# fleets' files cannot collide; -az is archive + compress for a WAN hop.
# shellcheck disable=SC2086 # the env var is a deliberate space-separated list
rsync -az --relative $SOURCES "$DEST"

# The stamp is the receipt the watchdog reads the age of. Written only after
# rsync returned success, so its mtime is the time of the last copy that
# actually landed, not the last one attempted.
date -u +%Y-%m-%dT%H:%M:%SZ > "$STAMP"
echo "backup: landed at $DEST"
