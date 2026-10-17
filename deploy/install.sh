#!/usr/bin/env bash
# Troupe installer for Debian 12 / Ubuntu 22.04+ (amd64 or arm64). Run as root.
#
#   curl -fsSL https://raw.githubusercontent.com/elysianoracle/Troupe/main/deploy/install.sh | sudo bash
#   sudo ./deploy/install.sh                      # from a copy of the repository
#
# Options (environment variables):
#   TROUPE_DOMAIN=troupe.example.com   your domain (DNS pointed at this server); default <ip>.sslip.io
#   TROUPE_REPO=https://github.com/elysianoracle/Troupe   where to fetch Troupe (default shown)
#   TROUPE_HARDEN_SSH=0                leave SSH settings alone
# Re-run the same command to update; your data and settings are kept.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Please run as root (sudo)."; exit 1; }
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# --- where is Troupe? ---------------------------------------------------------------------------------
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /nonexistent)"
if [[ -f "$SELF_DIR/../pyproject.toml" ]]; then
  REPO="$(cd "$SELF_DIR/.." && pwd)"
else
  TROUPE_REPO="${TROUPE_REPO:-https://github.com/elysianoracle/Troupe.git}"
  say "Fetching Troupe"
  apt-get update -qq && apt-get install -y -qq git >/dev/null
  rm -rf /opt/troupe-src && git clone -q --depth 1 "$TROUPE_REPO" /opt/troupe-src
  REPO=/opt/troupe-src
fi

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip docker.io git curl rsync fail2ban >/dev/null
apt-get install -y -qq caddy >/dev/null 2>&1 || echo "  Caddy isn't in this distribution's repositories; HTTPS will be skipped (see https://caddyserver.com/docs/install)."
systemctl enable --now docker >/dev/null

say "Adding 2 GB of swap (keeps browser sandboxes from exhausting memory)"
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
  sysctl -qw vm.swappiness=10 && echo "vm.swappiness=10" > /etc/sysctl.d/99-troupe.conf
else
  echo "  Swap already present."
fi

say "Securing SSH"
INVOKER="${SUDO_USER:-root}"
INVOKER_HOME="$(getent passwd "$INVOKER" | cut -d: -f6)"
if [[ "${TROUPE_HARDEN_SSH:-1}" == "0" ]]; then
  echo "  Skipped (TROUPE_HARDEN_SSH=0)."
elif [[ -s "$INVOKER_HOME/.ssh/authorized_keys" ]]; then
  cat > /etc/ssh/sshd_config.d/01-troupe-hardening.conf <<'CONF'
# Troupe: SSH accepts keys only, so it can safely stay reachable from any address.
# Named 01- so it wins over cloud-init's files (sshd uses the first value it reads).
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
  if sshd -t 2>/dev/null; then systemctl reload ssh 2>/dev/null || systemctl reload sshd; echo "  Key-only SSH is on."
  else rm -f /etc/ssh/sshd_config.d/01-troupe-hardening.conf; echo "  sshd rejected the settings; SSH left unchanged."; fi
else
  echo "  $INVOKER has no SSH keys, so password logins were left on (you'd be locked out otherwise)."
fi
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

say "Creating the troupe service account"
id troupe >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/troupe --shell /usr/sbin/nologin troupe
# The core drives Docker to create the agents' sandboxes. Docker access is root-equivalent:
# keep this server single-purpose, or use rootless Docker / gVisor (README, "Hardening").
usermod -aG docker troupe
mkdir -p /etc/troupe /var/lib/troupe
chown troupe:troupe /var/lib/troupe

# --- address ------------------------------------------------------------------------------------------------
PUBLIC_IP="$(curl -fsS -m 10 https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || true)"
HOST="${TROUPE_DOMAIN:-}"
[[ -z "$HOST" && -n "$PUBLIC_IP" ]] && HOST="${PUBLIC_IP//./-}.sslip.io"
if command -v caddy >/dev/null && [[ -n "$HOST" ]]; then URL="https://$HOST"; else URL="http://${PUBLIC_IP:-localhost}:8080"; fi

ENV=/etc/troupe/troupe.env
if [[ ! -f "$ENV" ]]; then
  cp "$REPO/.env.example" "$ENV"
  chmod 640 "$ENV"; chgrp troupe "$ENV"
fi
if grep -q '^TROUPE_PUBLIC_URL=' "$ENV"; then sed -i "s|^TROUPE_PUBLIC_URL=.*|TROUPE_PUBLIC_URL=$URL|" "$ENV"; else echo "TROUPE_PUBLIC_URL=$URL" >> "$ENV"; fi
if ! command -v caddy >/dev/null; then sed -i "s|^TROUPE_HOST=.*|TROUPE_HOST=0.0.0.0|" "$ENV"; fi

say "Installing Troupe"
rsync -a --delete --exclude .git --exclude .venv --exclude data "$REPO/" /opt/troupe/
python3 -m venv /opt/troupe/.venv
/opt/troupe/.venv/bin/pip install -q --upgrade pip
/opt/troupe/.venv/bin/pip install -q --force-reinstall --no-deps /opt/troupe
/opt/troupe/.venv/bin/pip install -q /opt/troupe
chown -R root:root /opt/troupe
install -m 755 /opt/troupe/deploy/troupe-setkey /usr/local/bin/troupe-setkey
install -m 755 /opt/troupe/deploy/troupe-password /usr/local/bin/troupe-password

say "Building the agents' sandbox image (a few minutes the first time)"
docker build -q -t troupe-sandbox:latest /opt/troupe/sandbox >/dev/null

if command -v caddy >/dev/null && [[ -n "$HOST" ]]; then
  say "Setting up HTTPS for $HOST"
  if [[ ! -f /etc/caddy/Caddyfile ]] || ! grep -q "reverse_proxy 127.0.0.1:8080" /etc/caddy/Caddyfile; then
    sed "s/troupe.example.com/$HOST/" /opt/troupe/deploy/Caddyfile > /etc/caddy/Caddyfile
  fi
  systemctl enable caddy >/dev/null 2>&1; systemctl reload caddy 2>/dev/null || systemctl restart caddy
fi

say "Starting Troupe"
cp /opt/troupe/deploy/troupe.service /etc/systemd/system/troupe.service
systemctl daemon-reload
systemctl enable troupe >/dev/null 2>&1
systemctl restart troupe
for _ in $(seq 1 30); do curl -fsS -m 2 http://127.0.0.1:8080/healthz >/dev/null 2>&1 && break; sleep 1; done
TOKEN="$(cat /var/lib/troupe/admin_token 2>/dev/null || true)"

cat <<MSG

  Troupe is running.

  Finish setting up in your browser (this link signs you in once; keep it private):

      $URL/?token=$TOKEN

  Make sure your cloud firewall allows ports 80 and 443 (and 22 for SSH).
  Update later by running this installer again. Logs: journalctl -u troupe -f

MSG
