#!/usr/bin/env bash
# Installs deploy/watchdog.sh as a systemd timer (runs every minute, starts at boot).
# Run once on the server, as root:   sudo ./deploy/install-watchdog.sh
#   Logs:    journalctl -u aidocumines-watchdog -n 50
#   Pause:   sudo touch /var/run/aidocumines-watchdog.pause     Resume: sudo rm that file
#   Remove:  sudo systemctl disable --now aidocumines-watchdog.timer
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/deploy/watchdog.sh"
[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }
chmod +x "$SCRIPT"

cat > /etc/systemd/system/aidocumines-watchdog.service <<EOF
[Unit]
Description=aiDocuMines container watchdog
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=$SCRIPT
EOF

cat > /etc/systemd/system/aidocumines-watchdog.timer <<EOF
[Unit]
Description=Run the aiDocuMines container watchdog every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now aidocumines-watchdog.timer
# Docker itself must come back after a reboot or a crash of the daemon.
systemctl enable docker >/dev/null 2>&1 || true
echo "Watchdog installed. Next runs:"
systemctl list-timers aidocumines-watchdog.timer --no-pager || true

# Swap: on a server with no swap, a short memory spike kills containers outright.
if [ "$(swapon --noheadings 2>/dev/null | wc -l)" -eq 0 ]; then
  echo
  echo "NOTE: this server has no swap. A small swap file makes memory spikes survivable:"
  echo "  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile"
  echo "  echo '/swapfile none swap sw 0 0' >> /etc/fstab && sysctl -w vm.swappiness=10"
fi
