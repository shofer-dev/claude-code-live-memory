#!/usr/bin/env bash
# Live Memory test gate: strict typecheck + the full pytest suite (unit +
# integration). A single failure exits non-zero. Used by the pre-push git hook to
# block pushes when anything fails (see git-hooks/), and runnable by hand.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SERVER="$HERE/server"
VENV="$SERVER/.venv"
PY="$VENV/bin/python"

# Bootstrap a venv with the dev deps (mypy/pytest) if needed — self-contained.
[ -x "$PY" ] || { python3 -m venv "$VENV"; PY="$VENV/bin/python"; }
if ! "$PY" -c "import mypy, pytest" >/dev/null 2>&1; then
    echo "[run-tests] installing dev deps into $VENV …"
    PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple/}" "$VENV/bin/pip" install -q -e "$SERVER[dev]"
fi

cd "$SERVER"
echo "=== mypy --strict ==="
"$PY" -m mypy live_memory/
echo "=== pytest (unit + integration) ==="
"$PY" -m pytest -q
echo "=== Live Memory tests passed ==="
