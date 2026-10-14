#!/usr/bin/env bash
# One-time setup of the Radar WORKER (the live runner) on our own machine: Ubuntu 22.04/24.04 or Oracle Linux 9,
# run as root. Replaces GitHub Actions for client stores (14 Oct 2026); GitHub keeps running the test suite.
#
#   1. On the machine, as root:
#        curl -fsSL https://raw.githubusercontent.com/Anmol-a/bugradar-radar/cloud-runs/cloud/setup-worker.sh -o setup-worker.sh
#        bash setup-worker.sh
#      (the repo is public today; once private, copy the file by hand and clone with a deploy key)
#   2. Put the three secrets in /home/radar/radar.env (the script creates it with placeholders; nano it):
#        BUGRADAR_SUPABASE_URL=https://onijwpgvpojpvgumvfim.supabase.co
#        BUGRADAR_SERVICE_KEY=<Supabase -> Project settings -> API keys -> service_role>
#        BUGRADAR_KEY=<dashboard -> Connect tools -> upload key brk_...>
#        OPENAI_API_KEY=<optional: LLM triage>
#   3. systemctl restart radar-worker && journalctl -u radar-worker -f
#
# What it does: a non-root user `radar`; Python + Chromium with its system libraries; the repo in /home/radar/radar
# (branch cloud-runs); a venv; /home/radar/radar-data that KEEPS every run (incident history -> pinning works);
# a systemd service that restarts on crash and on reboot; a firewall that only allows SSH in; automatic security
# updates. Nothing listens on the internet. Re-running it is safe (pulls the latest code, keeps the env file).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
REPO_URL="https://github.com/Anmol-a/bugradar-radar"
BRANCH="${RADAR_BRANCH:-cloud-runs}"
HOME_DIR=/home/radar

echo "== packages"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -yq python3 python3-venv python3-pip git curl unattended-upgrades ufw ca-certificates
  dpkg-reconfigure -f noninteractive unattended-upgrades || true
  ufw default deny incoming; ufw default allow outgoing; ufw allow OpenSSH; ufw --force enable
else
  dnf install -y python3 python3-pip git curl ca-certificates          # Oracle Linux: firewalld is on by default
fi

echo "== user radar + folders"
id radar >/dev/null 2>&1 || useradd -m -s /bin/bash radar
install -d -o radar -g radar "$HOME_DIR/radar-data"

echo "== code ($BRANCH)"
if [ -d "$HOME_DIR/radar/.git" ]; then
  sudo -u radar git -C "$HOME_DIR/radar" fetch -q origin "$BRANCH"
  sudo -u radar git -C "$HOME_DIR/radar" checkout -q "$BRANCH"
  sudo -u radar git -C "$HOME_DIR/radar" reset -q --hard "origin/$BRANCH"
else
  sudo -u radar git clone -q --branch "$BRANCH" "$REPO_URL" "$HOME_DIR/radar"
fi

echo "== python + chromium"
sudo -u radar python3 -m venv "$HOME_DIR/radar/.venv"
sudo -u radar "$HOME_DIR/radar/.venv/bin/pip" install -q -r "$HOME_DIR/radar/requirements.txt"
sudo -u radar "$HOME_DIR/radar/.venv/bin/python" -m playwright install chromium
"$HOME_DIR/radar/.venv/bin/python" -m playwright install-deps chromium || true    # needs root; Oracle Linux: see docs

echo "== env file"
if [ ! -f "$HOME_DIR/radar.env" ]; then
  cat > "$HOME_DIR/radar.env" <<EOF
# Radar worker secrets: never commit, never paste into chat. Readable by user radar only.
BUGRADAR_SUPABASE_URL=https://onijwpgvpojpvgumvfim.supabase.co
BUGRADAR_SERVICE_KEY=PASTE_SERVICE_ROLE_KEY
BUGRADAR_KEY=PASTE_UPLOAD_KEY_brk_
OPENAI_API_KEY=
RADAR_DATA_DIR=$HOME_DIR/radar-data
RADAR_WORKER_NAME=$(hostname -s)
EOF
  chown radar:radar "$HOME_DIR/radar.env"; chmod 600 "$HOME_DIR/radar.env"
  echo "   -> edit $HOME_DIR/radar.env and paste the keys"
fi

echo "== service"
cat > /etc/systemd/system/radar-worker.service <<EOF
[Unit]
Description=BugRadar worker (the live runner)
After=network-online.target
Wants=network-online.target

[Service]
User=radar
WorkingDirectory=$HOME_DIR/radar
EnvironmentFile=$HOME_DIR/radar.env
ExecStart=$HOME_DIR/radar/.venv/bin/python -m radar worker --poll 60
Restart=always
RestartSec=30
Nice=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable radar-worker
systemctl restart radar-worker
sleep 3
systemctl --no-pager --lines=5 status radar-worker || true
echo "== done. Logs: journalctl -u radar-worker -f    Update code later: bash setup-worker.sh (re-run)"
