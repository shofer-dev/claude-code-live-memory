#!/usr/bin/env bash
# Recreate a FAITHFUL shofer build-env worktree at the pinned base (cross-package
# edits propagate to tsc; base `pnpm check-types` is clean).
set -euo pipefail
# shofer repo root: override with SHOFER_DIR for reproduction on another machine.
SH=${SHOFER_DIR:-/home/alsterg/Projects/arkware.ai/extensions/shofer}
BASE=${SHOFER_BASE:-32cdefcba07ee9afde9bf65b373a75531f015d96}   # pinned benchmark commit
WT=${1:-/tmp/pilot/shofer}

git -C "$SH" worktree add --detach "$WT" "$BASE" 2>/dev/null || true
cd "$WT"
git checkout -- . && git clean -fd
# real install (correct workspace links to THIS worktree's packages) — reuses global store
pnpm install --offline --config.confirmModulesPurge=false
# build sibling packages so their dist exists
pnpm turbo build --filter='./packages/*' --output-logs errors-only
# link workspace pkgs imported by src/ but not declared as deps (hoisting gap)
ln -sf ../../../packages/vscode-shim src/node_modules/@shofer/vscode-shim
ln -sf ../../packages/vscode-shim     node_modules/@shofer/vscode-shim 2>/dev/null || true
# sanity: base must typecheck clean
( cd src && pnpm check-types ) && echo "OK: faithful worktree at $WT (base typecheck clean)"
