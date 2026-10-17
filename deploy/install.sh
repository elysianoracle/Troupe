#!/usr/bin/env bash
# One-shot installer for Debian 12 / Ubuntu 22.04+ (run as root from the repo root).
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip docker.io git curl rsync >/dev/null
apt-get install -y -qq caddy >/dev/null 2>&1 || echo "   (caddy not in this distro's repos; install it from https://caddyserver.com/docs/install for HTTPS)"
systemctl enable --now docker

echo "==> swap (2 GB) so Chromium spikes don't trigger the OOM killer"
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
  sysctl -w vm.swappiness=10 >/dev/null && echo "vm.swappiness=10" > /etc/sysctl.d/99-troupe.conf
fi

echo "==> SSH hardening (key-only, so SSH can stay open to any address) + fail2ban"
cat > /etc/ssh/sshd_config.d/01-troupe-hardening.conf <<'CONF'
# Troupe: allow keys only. Named 01- so it wins over cloud-init's files (first value read is used).
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PermitEmptyPasswords no
AuthenticationMethods publickey
MaxAuthTries 3
LoginGraceTime 20
MaxStartups 10:30:60
X11Forwarding no
ClientAliveInterval 60
ClientAliveCountMax 3
CONF
if sshd -t; then systemctl reload ssh; else rm /etc/ssh/sshd_config.d/01-troupe-hardening.conf; echo "   sshd rejected the hardening config; left SSH unchanged"; fi
apt-get install -y -qq fail2ban >/dev/null
cat > /etc/fail2ban/jail.d/troupe-sshd.local <<'CONF'
[sshd]
enabled  = true
backend  = systemd
maxretry = 4
findtime = 10m
bantime  = 1h
bantime.increment = true
bantime.maxtime   = 1w
CONF
systemctl enable --now fail2ban >/dev/null 2>&1 && systemctl restart fail2ban
install -m 755 "$REPO/deploy/troupe-setkey" /usr/local/bin/troupe-setkey

echo "==> user and folders"
id troupe >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/troupe --shell /usr/sbin/nologin troupe
# The core drives Docker to create sandboxes. Docker group access is root-equivalent: keep this
# VPS single-purpose, or switch to rootless Docker / gVisor (see README, "Hardening").
usermod -aG docker troupe
mkdir -p /etc/troupe /var/lib/troupe
chown -R troupe:troupe /var/lib/troupe
[[ -f /etc/troupe/troupe.env ]] || { cp "$REPO/.env.example" /etc/troupe/troupe.env; chmod 640 /etc/troupe/troupe.env; chgrp troupe /etc/troupe/troupe.env; }

echo "==> app"
rsync -a --delete --exclude .git --exclude .venv --exclude data "$REPO/" /opt/troupe/ 2>/dev/null || cp -r "$REPO/." /opt/troupe/
python3 -m venv /opt/troupe/.venv
/opt/troupe/.venv/bin/pip install -q --upgrade pip
/opt/troupe/.venv/bin/pip install -q /opt/troupe
chown -R root:root /opt/troupe

echo "==> sandbox image (takes a few minutes the first time)"
docker build -q -t troupe-sandbox:latest /opt/troupe/sandbox

echo "==> service"
cp /opt/troupe/deploy/troupe.service /etc/systemd/system/troupe.service
systemctl daemon-reload
systemctl enable troupe

cat <<MSG

Installed. Next:
  1. Edit /etc/troupe/troupe.env  (ANTHROPIC_API_KEY, TROUPE_PUBLIC_URL, optional email/Telegram)
  2. Edit /etc/caddy/Caddyfile from /opt/troupe/deploy/Caddyfile, then: systemctl reload caddy
  3. systemctl start troupe && journalctl -u troupe -f
  4. Open \$TROUPE_PUBLIC_URL and paste the token from:
       sudo -u troupe TROUPE_DATA_DIR=/var/lib/troupe /opt/troupe/.venv/bin/troupe token
MSG
