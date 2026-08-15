#!/usr/bin/env bash
# Install the trader onto an always-on Linux box (mock phase).
# Usage: sudo bash deploy/install.sh [install_dir]  (default /opt/trader)
set -euo pipefail

INSTALL_DIR="${1:-/opt/trader}"
UNIT_DIR="/etc/systemd/system"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v python3 >/dev/null || { echo "python3 required"; exit 1; }
python3 - <<'EOF'
import sys
assert sys.version_info >= (3, 11), f"need python 3.11+, have {sys.version}"
EOF

echo "installing from $SRC_DIR to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
# The desk's one runtime dependency (Schwab Streamer websocket). Failure is
# non-fatal: without it the desk runs REST-polling only.
pip3 install --quiet -r "$SRC_DIR/requirements.txt" 2>/dev/null \
  || pip3 install --quiet --break-system-packages -r "$SRC_DIR/requirements.txt" \
  || echo "WARN: websocket-client not installed; streaming disabled (REST polling still works)"
cp -r "$SRC_DIR/mockschwab" "$SRC_DIR/agent" "$SRC_DIR/SPEC" "$INSTALL_DIR/"
mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/desk_state"
# Never clobber accumulated desk state on reinstall; seed identity only if absent.
if [ ! -f "$INSTALL_DIR/desk_state/identity.md" ]; then
  cp "$SRC_DIR/desk_state/identity.md" "$INSTALL_DIR/desk_state/"
fi

for unit in trader-mock.service trader-poller.service trader-simday.service \
            trader-simday.timer trader-dashboard.service trader-strategist@.service \
            trader-strategist-plan.timer trader-strategist-midday.timer \
            trader-strategist-postmortem.timer; do
  sed "s|@INSTALL_DIR@|$INSTALL_DIR|g" "$SRC_DIR/deploy/$unit" > "$UNIT_DIR/$unit"
done

systemctl daemon-reload
systemctl enable --now trader-mock.service
sleep 2
systemctl enable --now trader-poller.service
systemctl enable --now trader-dashboard.service
systemctl enable --now trader-simday.timer
# Strategist judgment slots — dry-run until an API key is configured (see
# trader-strategist@.service comments): prompts and estimated costs only.
systemctl enable --now trader-strategist-plan.timer \
                       trader-strategist-midday.timer \
                       trader-strategist-postmortem.timer

# Seed the gut with simulated day-shape priors if it's empty.
if [ ! -s "$INSTALL_DIR/desk_state/day_memory.jsonl" ]; then
  echo "backfilling 500 simulated days into gut memory..."
  (cd "$INSTALL_DIR" && python3 -m agent.backfill --days 500 --desk-dir "$INSTALL_DIR/desk_state")
fi

echo
echo "installed. check on it with:"
echo "  systemctl status trader-mock trader-poller"
echo "  systemctl list-timers trader-simday.timer"
echo "  python3 -c \"import sys; sys.path.insert(0,'$INSTALL_DIR'); from agent.desk import Desk; print(Desk('$INSTALL_DIR/desk_state').wake_summary())\""
echo "kill switch: touch $INSTALL_DIR/data/HALT"
