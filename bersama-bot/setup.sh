#!/usr/bin/env bash
# BersamaAi event bot — first-boot provisioning for an Ubuntu VM
# (GCP / AWS / Raspberry Pi / any systemd Linux).
#
# Run this ON the VM, from inside the bersama-bot directory, as your normal user:
#
#   git clone https://github.com/pmgwee/BersamaAi-community.git
#   cd BersamaAi-community/bersama-bot
#   ./setup.sh
#
# What it does:
#   1. installs python3 + venv (if missing)
#   2. creates .venv and installs requirements.txt
#   3. writes .env once (asks for the two secrets; never overwrites an existing one)
#   4. installs + enables a systemd service that auto-restarts on crash/reboot
#
# The bot is outbound-only (it connects to Discord's gateway) — NO firewall/ingress
# rules are needed on the VM.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY_USER="$(whoami)"

echo "== BersamaAi bot setup on $(hostname) as $DEPLOY_USER =="

# 1. System packages. On Ubuntu, `import venv` can succeed even when ensurepip is missing
#    (ensurepip lives in the separate python3.X-venv package), which makes
#    `python3 -m venv` fail with "ensurepip is not available". So we check ensurepip and
#    install the versioned venv package + pip + git.
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y python3
  PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  sudo apt-get install -y python3-venv "python${PYVER}-venv" python3-pip git
fi

# 2. Virtualenv + dependencies.
cd "$REPO_DIR"
# Create the venv, or repair it if a previous run left a broken/partial one.
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c 'import ensurepip' >/dev/null 2>&1; then
  python3 -m venv --clear .venv
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# 3. .env (ask once; keep an existing one untouched).
if [ ! -f .env ]; then
  echo "DISCORD_TOKEN = the SAME bot token the discord-mcp jar uses."
  read -r -s -p "DISCORD_TOKEN: " TOKEN; echo
  read -r -s -p "ZAI_API_KEY (blank = disable AI): " ZAI; echo
  cat > .env <<EOF
DISCORD_TOKEN=$TOKEN
GLM_MODEL=glm-5.2
ZAI_BASE_URL=https://api.z.ai/api/coding/paas/v4
ZAI_API_KEY=$ZAI
EOF
  chmod 600 .env
  echo "Wrote .env (chmod 600)."
else
  echo ".env already present — leaving it untouched."
fi

# 4. systemd service with real paths + user; install + enable + start.
if pidof systemd >/dev/null 2>&1; then
  cat > /tmp/bersama.service <<EOF
[Unit]
Description=BersamaAi event bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$DEPLOY_USER
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/.venv/bin/python -u bot.py
EnvironmentFile=$REPO_DIR/.env
Restart=always
RestartSec=10
StandardOutput=append:$REPO_DIR/bersama.log
StandardError=append:$REPO_DIR/bersama.log

[Install]
WantedBy=multi-user.target
EOF
  sudo install -m 644 /tmp/bersama.service /etc/systemd/system/bersama.service
  rm -f /tmp/bersama.service
  sudo systemctl daemon-reload
  sudo systemctl enable --now bersama
  echo
  echo "== Done. Bot is running under systemd (auto-restarts on crash/reboot). =="
  echo "   status:  sudo systemctl status bersama"
  echo "   logs:    tail -f $REPO_DIR/bersama.log"
  echo "   stop:    sudo systemctl stop bersama"
  echo "   restart: sudo systemctl restart bersama"
else
  echo
  echo "== Not a systemd host. Run manually: $REPO_DIR/.venv/bin/python -u bot.py =="
fi
