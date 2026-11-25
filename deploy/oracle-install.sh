#!/bin/bash
# One-shot installer for an Ubuntu 22.04/24.04 server (Oracle Cloud Always Free, ARM or AMD).
# Usage (as root, e.g. from cloud-init):
#   curl -fsSL <raw url of this file> | APP_TOKEN=<access code> bash
# Installs the 4-bot system as a systemd service behind Caddy with automatic HTTPS on
# https://<public-ip-with-dashes>.sslip.io . Log: /var/log/bots-install.log
set -euo pipefail
exec > >(tee -a /var/log/bots-install.log) 2>&1
echo "=== bots install $(date -u) ==="

REPO_TARBALL="${REPO_TARBALL:-https://github.com/meirvbb-png/cryptppilot-ai/archive/refs/heads/claude/computer-check-optimization-l2lweg.tar.gz}"
APP_TOKEN="${APP_TOKEN:-$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)}"
DIR=/opt/bots
export DEBIAN_FRONTEND=noninteractive

# --- packages ---
apt-get update -y
apt-get install -y python3-venv python3-pip curl ca-certificates caddy iptables-persistent || \
  apt-get install -y python3-venv python3-pip curl ca-certificates caddy

# --- swap (small instances, e.g. VM.Standard.E2.1.Micro with 1 GB) ---
if [ "$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)" -lt 3000 ] && ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# --- code ---
id -u bots >/dev/null 2>&1 || useradd -r -m -d $DIR -s /usr/sbin/nologin bots
mkdir -p $DIR
curl -fsSL "$REPO_TARBALL" | tar xz -C $DIR --strip-components=1
cd $DIR
python3 -m venv .venv
.venv/bin/pip install --upgrade pip wheel
.venv/bin/pip install -r requirements.txt || {
  echo "pinned install failed - retrying with latest compatible versions"
  sed 's/==.*//' requirements.txt > /tmp/req-unpinned.txt
  .venv/bin/pip install -r /tmp/req-unpinned.txt
}

cat > $DIR/.env <<EOF
APP_TOKEN=$APP_TOKEN
HOST=127.0.0.1
PORT=8000
HISTORY_DAYS=130
ARCHIVE_DAYS=7
BYBIT_LIVE_ENABLED=false
EOF
chmod 600 $DIR/.env
chown -R bots:bots $DIR

# --- service ---
cat > /etc/systemd/system/bots.service <<EOF
[Unit]
Description=Bybit 4-bot demo system
After=network-online.target
Wants=network-online.target

[Service]
User=bots
WorkingDirectory=$DIR
Environment=MALLOC_ARENA_MAX=2
ExecStart=$DIR/.venv/bin/python -m app
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now bots

# --- firewall (Oracle Ubuntu images REJECT everything except SSH by default) ---
for p in 80 443; do
  iptables -C INPUT -p tcp --dport $p -j ACCEPT 2>/dev/null || iptables -I INPUT 5 -p tcp --dport $p -m state --state NEW -j ACCEPT
done
command -v netfilter-persistent >/dev/null && netfilter-persistent save || true

# --- HTTPS via Caddy + sslip.io ---
PUBIP=$(curl -fsS https://api.ipify.org || curl -fsS https://ifconfig.me)
HOSTNAME_PUB="$(echo "$PUBIP" | tr . -).sslip.io"
cat > /etc/caddy/Caddyfile <<EOF
$HOSTNAME_PUB {
    encode gzip
    reverse_proxy 127.0.0.1:8000
}
EOF
systemctl restart caddy

cat > /root/bots-info.txt <<EOF
URL:   https://$HOSTNAME_PUB
Code:  $APP_TOKEN
Logs:  journalctl -u bots -f
EOF
echo "=== done ==="
cat /root/bots-info.txt
