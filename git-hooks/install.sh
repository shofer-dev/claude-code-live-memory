#!/usr/bin/env bash
# Activate the pre-push test gate for THIS repo (Husky-style, via core.hooksPath).
# Run once after cloning. Scoped to this repo's own git config, so it's safe when
# this repo is a git submodule — it never touches the parent monorepo's hooks.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
chmod +x git-hooks/pre-push run-tests.sh
git config core.hooksPath git-hooks
echo "Installed: core.hooksPath=git-hooks → 'git push' runs ./run-tests.sh and blocks on failure."
