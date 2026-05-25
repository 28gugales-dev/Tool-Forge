#!/usr/bin/env bash
# Resets demo state between rehearsals.
# Backs up and clears ~/.claude/toolforge.db, restores the empty PricingCard stub.
# Pass -y to skip the confirmation prompt.
set -euo pipefail

FORCE=0
if [[ "${1:-}" == "-y" ]]; then FORCE=1; fi

SCAFFOLD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_PATH="$HOME/.claude/toolforge.db"
STUB="$SCAFFOLD_DIR/src/PricingCard.template.jsx"
TARGET="$SCAFFOLD_DIR/src/PricingCard.jsx"

if [[ -f "$DB_PATH" ]]; then
  if [[ "$FORCE" -ne 1 ]]; then
    read -r -p "About to back up and delete $DB_PATH. This contains your real ToolForge data. Continue? (y/N) " answer
    if [[ ! "$answer" =~ ^[yY]$ ]]; then
      echo "Aborted. No changes made."
      exit 1
    fi
  fi
  STAMP=$(date +'%Y%m%d-%H%M%S')
  BACKUP="$DB_PATH.bak-$STAMP"
  cp "$DB_PATH" "$BACKUP"
  rm "$DB_PATH"
  echo "Backed up DB to $BACKUP and cleared $DB_PATH"
else
  echo "No toolforge.db found at $DB_PATH, skipping DB reset."
fi

if [[ ! -f "$STUB" ]]; then
  echo "Template stub missing at $STUB. Cannot restore PricingCard.jsx." >&2
  exit 1
fi
cp "$STUB" "$TARGET"
echo "Restored PricingCard.jsx to empty stub."
echo "Demo reset complete."
