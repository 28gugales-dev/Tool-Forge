#!/usr/bin/env bash
# demo_reset.sh
# One-command rehearsal reset for the ToolForge demo.
# Backs up ~/.claude/toolforge.db, deletes it, restores PricingCard.jsx.

set -euo pipefail

FORCE=0
for arg in "$@"; do
    case "$arg" in
        --force|-f) FORCE=1 ;;
        *) echo "demo_reset: unknown arg $arg" >&2; exit 2 ;;
    esac
done

fail() {
    echo "demo_reset: $*" >&2
    exit 1
}

# Resolve paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCAFFOLD_RESET="$SCRIPT_DIR/scaffold/reset.sh"
CLAUDE_DIR="$HOME/.claude"
DB_PATH="$CLAUDE_DIR/toolforge.db"

if [ ! -f "$SCAFFOLD_RESET" ]; then
    fail "cannot find scaffold reset script at $SCAFFOLD_RESET"
fi

if [ ! -x "$SCAFFOLD_RESET" ]; then
    chmod +x "$SCAFFOLD_RESET" || fail "cannot chmod +x $SCAFFOLD_RESET"
fi

# Confirmation prompt unless --force
if [ "$FORCE" -ne 1 ]; then
    printf "Reset demo state? [y/n] "
    read -r answer
    case "$answer" in
        y|Y|yes|YES) ;;
        *) echo "demo_reset: aborted by user"; exit 0 ;;
    esac
fi

# 1. Back up the DB if it exists
BACKUP_SUMMARY="no existing toolforge.db to back up"
if [ -f "$DB_PATH" ]; then
    TS="$(date -u +%Y%m%dT%H%M%SZ)"
    BACKUP_PATH="$CLAUDE_DIR/toolforge.db.bak-$TS"
    cp "$DB_PATH" "$BACKUP_PATH" || fail "backup failed"
    if [ ! -f "$BACKUP_PATH" ]; then
        fail "backup did not land at $BACKUP_PATH, refusing to delete original"
    fi
    BACKUP_SUMMARY="backed up toolforge.db to $BACKUP_PATH"

    # 2. Delete the DB only after the backup is verified on disk
    rm -f "$DB_PATH" || fail "delete failed"
fi

# 3. Run the scaffold reset script with -y
"$SCAFFOLD_RESET" -y || fail "scaffold reset.sh exited non-zero"

# 4. Three-line summary
echo ""
echo "demo_reset: $BACKUP_SUMMARY"
echo "demo_reset: deleted ~/.claude/toolforge.db"
echo "demo_reset: restored PricingCard.jsx to empty stub via scaffold/reset.sh"
exit 0
