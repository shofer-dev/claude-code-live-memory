#!/usr/bin/env bash
# Install the Live Memory pre-push test gate into this repo's .git/hooks.
# Copies (not symlinks) so it survives branch switches; re-run after editing the
# committed hook. Idempotent.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
SRC="$ROOT/claude-code/live-memory/git-hooks/pre-push"
DST="$ROOT/.git/hooks/pre-push"

if [ -e "$DST" ] && ! grep -q "claude-code/live-memory/run-tests.sh" "$DST" 2>/dev/null; then
    echo "WARNING: $DST already exists and isn't ours — backing it up to $DST.bak"
    mv "$DST" "$DST.bak"
fi
install -m 0755 "$SRC" "$DST"
echo "Installed pre-push hook → $DST"
echo "Pushes touching claude-code/live-memory/ now run: claude-code/live-memory/run-tests.sh"
