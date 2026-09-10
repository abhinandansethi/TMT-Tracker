#!/usr/bin/env bash
# Install or update TMT Regulatory Radar on an Ubuntu VM (22.04 / 24.04) as a systemd service
# behind nginx. Idempotent: run it again to update.
#
#   sudo bash server/deploy/install.sh            # from a checkout at /opt/tmt-radar
#
# What it sets up, and why each piece is there:
#   /opt/tmt-radar          the checkout — the same files the pipeline has always used
#   /etc/tmt-radar.env      the ONLY place secrets live (0600, root:tmt-radar)
#   tmt-radar.service       uvicorn on 127.0.0.1:8080 as the unprivileged `tmt-radar` user
#   nginx                   TLS termination + reverse proxy; the app does the Basic Auth itself,
#                           so the same credentials work whether or not nginx is in front
# Nothing is scheduled. The service runs a job when a person presses a button on the page.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/tmt-radar}"
SVC_USER="${SVC_USER:-tmt-radar}"
ENV_FILE="/etc/tmt-radar.env"
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

say "secrets file $ENV_FILE"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'EOF'
# TMT Regulatory Radar — service environment. Root-owned, mode 0600. Edit, then: systemctl restart tmt-radar
OPENAI_API_KEY=
# One login per partner: user:password, one per line (a password may contain colons). Keep the
# quotes so newlines survive systemd's parser.
AUTH_USERS="abhi:CHANGE-ME"
# Models. Empty means the code's default (gpt-5.6-luna).
TMT_SCAN_MODEL=
TMT_SCAN_MODEL_STRONG=
TMT_ASK_MODEL=
# Set to 1 to stop the service committing each job to the local git repo (the audit trail).
TMT_NO_COMMIT=0
EOF
  chmod 0600 "$ENV_FILE"; chown "root:$SVC_USER" "$ENV_FILE"; chmod 0640 "$ENV_FILE"
  echo "   written with placeholders — put the real OPENAI_API_KEY and AUTH_USERS in it before partners use this."
else
  echo "   exists, left untouched"
fi

say "git identity for the audit-trail commits"
sudo -u "$SVC_USER" git -C "$APP_DIR" config user.name "tmt-radar" || true
sudo -u "$SVC_USER" git -C "$APP_DIR" config user.email "tmt-radar@localhost" || true

say "systemd unit"
sed -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__USER__#$SVC_USER#g" -e "s#__ENV__#$ENV_FILE#g" \
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
echo "   health:  curl -s http://127.0.0.1:8080/api/health"
echo "   logs:    journalctl -u tmt-radar -f"
echo "   update:  cd $APP_DIR && sudo -u $SVC_USER git pull && sudo bash server/deploy/install.sh"
