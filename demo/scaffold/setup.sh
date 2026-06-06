#!/usr/bin/env bash
# Run this once per fresh checkout. Installs npm deps for the ToolForge demo scaffold.
set -euo pipefail

SCAFFOLD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCAFFOLD_DIR"

echo "Installing demo scaffold dependencies in $SCAFFOLD_DIR"
if ! npm install; then
  echo "Setup failed. Check that Node.js and npm are on PATH, then re-run setup.sh." >&2
  exit 1
fi

echo "Scaffold ready. Run 'npm run dev' to start the demo at http://localhost:5173"
