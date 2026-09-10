#!/usr/bin/env bash
# Install or update TMT Regulatory Radar on an Ubuntu VM (22.04 / 24.04) as a systemd service
# behind nginx. Idempotent: run it again to update.
#
#   sudo bash server/deploy/install.sh            # from a checkout at /opt/tmt-radar
#
# What it sets up, and why each piece is there:
#   /opt/tmt-radar          the checkout — the same files the pipeline has always used
#   /var/lib/tmt-radar/settings.env   the ONLY place secrets live (0600, owned by the service, which writes it from /setup and /admin)
#   tmt-radar.service       uvicorn on 127.0.0.1:8080 as the unprivileged `tmt-radar` user
#   nginx                   TLS termination + reverse proxy; the app does the Basic Auth itself,
#                           so the same credentials work whether or not nginx is in front
# Nothing is scheduled. The service runs a job when a person presses a button on the page.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/tmt-radar}"
SVC_USER="${SVC_USER:-tmt-radar}"
STATE_DIR="/var/lib/$SVC_USER"            # the service's own directory — writable under ProtectSystem
ENV_FILE="$STATE_DIR/settings.env"        # written by the service's /setup and /admin pages
LEGACY_ENV="/etc/tmt-radar.env"           # where the first installs put it; moved on upgrade
DOMAIN="${DOMAIN:-}"           # e.g. radar.trilegal.com — leave empty for plain HTTP on the VM's IP

say() { printf '\n== %s\n' "$*"; }

say "system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git nginx >/dev/null

say "service user"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "/var/lib/$SVC_USER" --shell /usr/sbin/nologin "$SVC_USER"

say "checkout at $APP_DIR"
if [ ! -d "$APP_DIR/.git" ]; then
  echo "   $APP_DIR is not a git checkout. Copy the repository there first (git clone, or rsync from your machine), then re-run." >&2
  exit 1
fi
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR"

say "python environment (engine/.venv — the path every script already expects)"
sudo -u "$SVC_USER" bash -c "cd '$APP_DIR' && python3 -m venv engine/.venv && engine/.venv/bin/pip install -q --upgrade pip && engine/.venv/bin/pip install -q -r requirements.txt"

say "settings file $ENV_FILE"
SETUP_CODE=""
mkdir -p "$STATE_DIR"; chown "$SVC_USER:$SVC_USER" "$STATE_DIR"; chmod 0750 "$STATE_DIR"
if [ -f "$LEGACY_ENV" ] && [ ! -f "$ENV_FILE" ]; then
  mv "$LEGACY_ENV" "$ENV_FILE"; echo "   moved from $LEGACY_ENV (the service must be able to write it; /etc is read-only to it)"
fi
if [ ! -f "$ENV_FILE" ]; then
  # No login and no key yet: a one-time setup code lets the first person configure the service
  # from the browser at /setup (it is deleted once used). There is no starter password on purpose.
  SETUP_CODE="$(openssl rand -hex 12)"
  cat > "$ENV_FILE" <<EOF
# TMT Regulatory Radar — service environment. Written by the service's setup and admin pages;
# editing by hand also works (then: systemctl restart tmt-radar). Owned by the service user, mode 0600.
OPENAI_API_KEY=
AUTH_USERS=""
TMT_ADMIN_USER=
TMT_SETUP_TOKEN=$SETUP_CODE
TMT_SCAN_MODEL=
TMT_SCAN_MODEL_STRONG=
TMT_ASK_MODEL=
TMT_NO_COMMIT=0
EOF
  echo "   written; no login exists yet — the first person configures it at /setup with the setup code below."
else
  echo "   exists, left untouched"
  SETUP_CODE="$(sed -n 's/^TMT_SETUP_TOKEN=//p' "$ENV_FILE" | tr -d '"')"
fi
# The service writes this file itself (setup and admin pages), so it must own it.
chown "$SVC_USER:$SVC_USER" "$ENV_FILE"; chmod 0600 "$ENV_FILE"

say "git identity for the audit-trail commits"
sudo -u "$SVC_USER" git -C "$APP_DIR" config user.name "tmt-radar" || true
sudo -u "$SVC_USER" git -C "$APP_DIR" config user.email "tmt-radar@localhost" || true

say "systemd unit"
sed -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__USER__#$SVC_USER#g" -e "s#__ENV__#$ENV_FILE#g" -e "s#__STATE_DIR__#$STATE_DIR#g" \
    "$APP_DIR/server/deploy/tmt-radar.service" > /etc/systemd/system/tmt-radar.service
systemctl daemon-reload
systemctl enable --now tmt-radar
sleep 2
systemctl --no-pager --lines=5 status tmt-radar || true

say "nginx"
sed -e "s#__DOMAIN__#${DOMAIN:-_}#g" "$APP_DIR/server/deploy/nginx.conf" > /etc/nginx/sites-available/tmt-radar
ln -sf /etc/nginx/sites-available/tmt-radar /etc/nginx/sites-enabled/tmt-radar
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

if [ -n "$DOMAIN" ]; then
  say "TLS for $DOMAIN (certbot)"
  apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect || \
    echo "   certbot did not finish — check that $DOMAIN points at this VM and port 80 is open, then: certbot --nginx -d $DOMAIN"
fi

say "done"
if [ -n "$SETUP_CODE" ]; then
  echo "   SETUP CODE (one-time, for the /setup page): $SETUP_CODE"
fi
echo "   health:  curl -s http://127.0.0.1:8080/api/health"
echo "   logs:    journalctl -u tmt-radar -f"
echo "   update:  cd $APP_DIR && sudo -u $SVC_USER git pull && sudo bash server/deploy/install.sh"
