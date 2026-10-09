#!/usr/bin/env bash
# One-time setup of Radar's own server as a GitHub runner for Anmol-a/bugradar-radar (Ubuntu 24.04, run as root).
#
#   1. GitHub -> repo Settings -> Actions -> Runners -> "New self-hosted runner" -> Linux (x64 or ARM64):
#      copy ONLY the token shown after "--token" (valid ~1 hour).
#   2. On the server, as root (the repo is private, so copy the file by hand):
#        nano setup-runner.sh      <- paste this whole file (GitHub: cloud/setup-runner.sh on branch cloud-runs, "Raw"),
#                                     save with Ctrl+O, Enter, Ctrl+X
#        bash setup-runner.sh <TOKEN>
#   3. GitHub -> repo Settings -> Secrets and variables -> Actions -> Variables -> New variable:
#        RADAR_RUNNER = radar          (delete the variable to go back to GitHub's machines)
#
# What it does: a non-root user `radar`; Python, git and Chromium's system libraries; the GitHub runner as a
# service that restarts on reboot; a data folder /home/radar/radar-data that keeps every run (incident history);
# automatic security updates; a firewall that only allows SSH in. Nothing listens on the internet: the runner
# only makes outgoing connections to GitHub. Re-running it is safe (it replaces the runner registration).
set -euo pipefail

TOKEN="${1:-}"
REPO_URL="https://github.com/Anmol-a/bugradar-radar"
RUNNER_NAME="${RUNNER_NAME:-radar-$(hostname -s)}"
[ -n "$TOKEN" ] || { echo "usage: bash setup-runner.sh <runner registration token>"; exit 1; }
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -yq python3 python3-venv python3-pip git curl jq tar unattended-upgrades ufw ca-certificates
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "== firewall: SSH in, everything out"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

echo "== user radar + data folder"
id radar >/dev/null 2>&1 || useradd -m -s /bin/bash radar
install -d -o radar -g radar /home/radar/radar-data /home/radar/actions-runner

echo "== Chromium system libraries (the workflow installs Chromium itself, as user radar)"
python3 -m venv /opt/pw-deps
/opt/pw-deps/bin/pip install -q playwright
/opt/pw-deps/bin/python -m playwright install-deps chromium

echo "== GitHub runner"
VER=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r .tag_name | sed 's/^v//')
cd /home/radar/actions-runner
if [ ! -x ./config.sh ]; then
  case "$(uname -m)" in aarch64|arm64) ARCH=arm64 ;; *) ARCH=x64 ;; esac     # Oracle's free Ampere machines are ARM
  curl -fsSL -o runner.tgz "https://github.com/actions/runner/releases/download/v${VER}/actions-runner-linux-${ARCH}-${VER}.tar.gz"
  tar xzf runner.tgz && rm runner.tgz
  chown -R radar:radar /home/radar/actions-runner
  ./bin/installdependencies.sh
fi
if [ -f .runner ]; then             # re-run: drop the old local registration, --replace takes over the name on GitHub
  ./svc.sh stop || true; ./svc.sh uninstall || true
  rm -f .runner .credentials .credentials_rsaparams
fi
sudo -u radar ./config.sh --url "$REPO_URL" --token "$TOKEN" --name "$RUNNER_NAME" --labels radar \
  --work _work --unattended --replace
./svc.sh install radar
./svc.sh start

echo
echo "DONE. Runner '$RUNNER_NAME' (label: radar) is installed and running."
echo "Last step on GitHub: Settings -> Secrets and variables -> Actions -> Variables -> RADAR_RUNNER = radar"
echo "Check: Settings -> Actions -> Runners should show '$RUNNER_NAME' as Idle (green)."
