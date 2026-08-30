#!/usr/bin/env bash
# Completed segments are immutable. The local ledger makes each scheduled run
# proportional to the new tape, while the Drive-side batch file names exactly
# what that run proved landed.
set -euo pipefail

SOURCE="${FORWARD_CAPTURE_ROOT:-/var/lib/liquidity-migration/forward-market}"
DESTINATION="${FORWARD_CAPTURE_REMOTE:-gdrive:LiquidityMigration/forward-market}"
CONFIG="${RCLONE_CONFIG:-/etc/liquidity-migration/rclone.conf}"
STATE_DIR="${FORWARD_UPLOAD_STATE_DIR:-/var/lib/liquidity-migration/forward-upload}"
RCLONE="${RCLONE_BIN:-/usr/bin/rclone}"

case "$DESTINATION" in
    *:*) ;;
    *) echo "forward upload: destination must be an rclone remote" >&2; exit 2 ;;
esac
[ -d "$SOURCE" ] || { echo "forward upload: capture root is missing: $SOURCE" >&2; exit 2; }
[ -f "$CONFIG" ] || { echo "forward upload: rclone config is missing: $CONFIG" >&2; exit 2; }
[ -x "$RCLONE" ] || { echo "forward upload: rclone is not executable: $RCLONE" >&2; exit 2; }

umask 027
mkdir -p "$STATE_DIR"
if command -v flock >/dev/null 2>&1; then
    exec 9>"$STATE_DIR/upload.lock"
    if ! flock -n 9; then
        echo "forward upload: another run owns the upload lock"
        exit 0
    fi
fi

RUN_DIR="$(mktemp -d "$STATE_DIR/run.XXXXXX")"
trap 'rm -rf -- "$RUN_DIR"' EXIT
LEDGER="$STATE_DIR/uploaded-files.txt"
STAMP="$STATE_DIR/last-success"
ALL="$RUN_DIR/all.txt"
DONE="$RUN_DIR/done.txt"
PENDING="$RUN_DIR/pending.txt"

touch "$LEDGER"
(cd "$SOURCE" && find . -type f -name '*.zst' -print) \
    | sed 's#^\./##' | LC_ALL=C sort -u > "$ALL"
LC_ALL=C sort -u "$LEDGER" > "$DONE"
comm -23 "$ALL" "$DONE" > "$PENDING"

DESTINATION="${DESTINATION%/}"
"$RCLONE" mkdir "$DESTINATION" --config "$CONFIG"

COUNT="$(wc -l < "$PENDING" | tr -d ' ')"
BYTES=0
if [ "$COUNT" -gt 0 ]; then
    while IFS= read -r relative; do
        BYTES=$((BYTES + $(wc -c < "$SOURCE/$relative")))
    done < "$PENDING"

    "$RCLONE" copy "$SOURCE" "$DESTINATION" \
        --config "$CONFIG" \
        --files-from-raw "$PENDING" \
        --immutable \
        --transfers 2 \
        --checkers 4 \
        --drive-chunk-size 16M \
        --retries 5 \
        --low-level-retries 10
    "$RCLONE" check "$SOURCE" "$DESTINATION" \
        --config "$CONFIG" \
        --files-from-raw "$PENDING" \
        --one-way

    BATCH_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
    BATCH_MANIFEST="$RUN_DIR/$BATCH_ID.sha256"
    while IFS= read -r relative; do
        if command -v sha256sum >/dev/null 2>&1; then
            DIGEST="$(sha256sum "$SOURCE/$relative" | cut -c1-64)"
        else
            DIGEST="$(shasum -a 256 "$SOURCE/$relative" | cut -c1-64)"
        fi
        printf '%s  %s\n' "$DIGEST" "$relative"
    done < "$PENDING" > "$BATCH_MANIFEST"
    "$RCLONE" copyto "$BATCH_MANIFEST" "$DESTINATION/_batches/$BATCH_ID.sha256" \
        --config "$CONFIG" \
        --checksum \
        --drive-chunk-size 16M \
        --retries 5 \
        --low-level-retries 10

    LC_ALL=C sort -u "$DONE" "$PENDING" > "$RUN_DIR/uploaded-files.txt"
    chmod 0640 "$RUN_DIR/uploaded-files.txt"
    mv "$RUN_DIR/uploaded-files.txt" "$LEDGER"
else
    "$RCLONE" lsf "$DESTINATION" --config "$CONFIG" --max-depth 1 >/dev/null
    BATCH_ID=none
fi

{
    printf 'uploaded_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'batch_id=%s\n' "$BATCH_ID"
    printf 'file_count=%s\n' "$COUNT"
    printf 'bytes=%s\n' "$BYTES"
    printf 'destination=%s\n' "$DESTINATION"
} > "$RUN_DIR/last-success"
chmod 0640 "$RUN_DIR/last-success"
mv "$RUN_DIR/last-success" "$STAMP"
echo "forward upload: verified files=$COUNT bytes=$BYTES destination=$DESTINATION batch=$BATCH_ID"
