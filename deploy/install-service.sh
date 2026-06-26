#!/usr/bin/env bash
# Register the Live Memory server as a systemd service.
#
# Default: a *user* service (runs as you). This is what lets zero-config Claude
# subscription auth work — the server reads ~/.claude/.credentials.json from
# YOUR home, so it must run as you.
#
#   ./install-service.sh            # user service (recommended; subscription-friendly)
#   ./install-service.sh --system   # system-wide service (best for API-key setups)
#
# Idempotent: re-run to update. Never overwrites an existing env file.
set -euo pipefail

MODE="user"
[[ "${1:-}" == "--system" ]] && MODE="system"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$(cd "$HERE/../server" && pwd)"
VENV="$SERVER_DIR/.venv"
PYTHON="$VENV/bin/python"

echo "▸ Live Memory → systemd ($MODE service)"
echo "  server: $SERVER_DIR"

# 1. venv + install the server package
if [[ ! -x "$PYTHON" ]]; then
  echo "▸ creating venv: $VENV"
  python3 -m venv "$VENV"
fi
echo "▸ installing server deps"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple/}" "$VENV/bin/pip" install -q -e "$SERVER_DIR"

# 2. locations differ between user and system services
if [[ "$MODE" == "user" ]]; then
  UNIT_DIR="$HOME/.config/systemd/user"
  ENV_DIR="$HOME/.config/live-memory"
  SYSTEMCTL=(systemctl --user)
  WANTED_BY="default.target"
  EXTRA_LINES="Environment=PYTHONUNBUFFERED=1"
  SUDO=""
else
  UNIT_DIR="/etc/systemd/system"
  ENV_DIR="/etc/live-memory"
  SYSTEMCTL=(sudo systemctl)
  WANTED_BY="multi-user.target"
  # system service: run as you + set HOME so subscription creds are found
  EXTRA_LINES="User=$USER
Environment=HOME=$HOME
Environment=PYTHONUNBUFFERED=1"
  SUDO="sudo"
fi
ENVFILE="$ENV_DIR/live-memory.env"
UNIT="$UNIT_DIR/live-memory.service"

# 3. env file (never clobber an existing one)
$SUDO mkdir -p "$ENV_DIR" "$UNIT_DIR"
if [[ ! -f "$ENVFILE" ]]; then
  $SUDO cp "$HERE/live-memory.env.example" "$ENVFILE"
  echo "▸ wrote $ENVFILE  (defaults to subscription + Haiku; edit to set a provider/key)"
else
  echo "▸ keeping existing $ENVFILE"
fi

# 4. render + install the unit
UNIT_CONTENT="[Unit]
Description=Live Memory — persistent, read-only codebase Q&A MCP server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PYTHON -m live_memory
EnvironmentFile=-$ENVFILE
$EXTRA_LINES
Restart=on-failure
RestartSec=3

[Install]
WantedBy=$WANTED_BY
"
echo "$UNIT_CONTENT" | $SUDO tee "$UNIT" >/dev/null
echo "▸ installed unit: $UNIT"

# 5. user services need lingering to run without an active login session
if [[ "$MODE" == "user" ]]; then
  loginctl enable-linger "$USER" >/dev/null 2>&1 || true
fi

# 6. enable + (re)start
"${SYSTEMCTL[@]}" daemon-reload
"${SYSTEMCTL[@]}" enable --now live-memory.service
"${SYSTEMCTL[@]}" restart live-memory.service

# 7. health check
HOST="$(sed -nE 's/^LIVE_MEMORY_HOST=//p' "$ENVFILE" | tail -1)"; HOST="${HOST:-127.0.0.1}"
PORT="$(sed -nE 's/^LIVE_MEMORY_PORT=//p' "$ENVFILE" | tail -1)"; PORT="${PORT:-7711}"
echo "▸ waiting for http://$HOST:$PORT/health"
for _ in $(seq 1 10); do
  if curl -fsS -m 3 "http://$HOST:$PORT/health" >/dev/null 2>&1; then
    curl -fsS "http://$HOST:$PORT/health"; echo "  ✓ up"; break
  fi
  sleep 0.5
done

echo
echo "Done. Useful commands:"
if [[ "$MODE" == "user" ]]; then
  echo "  systemctl --user status live-memory"
  echo "  journalctl --user -u live-memory -f"
  echo "  systemctl --user restart live-memory   # after editing $ENVFILE"
else
  echo "  sudo systemctl status live-memory"
  echo "  sudo journalctl -u live-memory -f"
  echo "  sudo systemctl restart live-memory     # after editing $ENVFILE"
fi
