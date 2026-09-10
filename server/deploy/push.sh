#!/usr/bin/env bash
# Push this checkout to the Azure VM and (re)install there, from a machine that has done `az login`.
#   bash server/deploy/push.sh <vm-name> <resource-group> [domain]
# e.g. bash server/deploy/push.sh Work WORK_GROUP tmt-radar.centralindia.cloudapp.azure.com
# The VM needs ports 22/80/443 open and the deploy key at ~/.ssh/tmt-radar-vm (ssh-keygen -t ed25519 -f ~/.ssh/tmt-radar-vm).
set -euo pipefail
VM="$1"; RG="$2"; DOMAIN="${3:-}"; KEY="$HOME/.ssh/tmt-radar-vm"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
say(){ printf '\n== %s\n' "$*"; }

say "who is the admin user, where is the VM"
read -r USER_ IP OS <<<"$(az vm show -d -n "$VM" -g "$RG" -o tsv --query '[osProfile.adminUsername, publicIps, storageProfile.osDisk.osType]' | tr '\n' ' ')"
echo "   $USER_@$IP ($OS)"
[ "$OS" = "Linux" ] || { echo "   the installer targets Ubuntu; this VM reports $OS" >&2; exit 1; }

say "put the deploy key on the VM (Azure adds it to the admin user's authorized_keys)"
az vm user update -n "$VM" -g "$RG" -u "$USER_" --ssh-key-value "$(cat "$KEY.pub")" -o none
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 $USER_@$IP"
$SSH 'echo "   ssh ok: $(hostname) $(lsb_release -ds 2>/dev/null || cat /etc/os-release | head -1)"'

say "copy the checkout (no venv, no built pages, no job logs — the installer makes those)"
$SSH 'sudo mkdir -p /opt/tmt-radar'
# The installer hands /opt/tmt-radar to the service user, so rsync runs as root on the far side.
rsync -az --delete -e "ssh -i $KEY" --rsync-path="sudo rsync" --no-owner --no-group \
  --exclude '.venv' --exclude 'dist/' --exclude 'server/jobs.db' --exclude 'server/logs/' --exclude '__pycache__' \
  "$REPO/" "$USER_@$IP:/opt/tmt-radar/"

say "install (idempotent)"
$SSH "cd /opt/tmt-radar && sudo DOMAIN='$DOMAIN' bash server/deploy/install.sh"

say "health"
$SSH 'curl -s http://127.0.0.1:8080/api/health; echo; sudo systemctl is-active tmt-radar'
echo
echo "NEXT: first install only — open /setup in the browser with the setup code printed above; afterwards logins and the key are managed at /admin."
[ -n "$DOMAIN" ] && echo "      then open https://$DOMAIN/" || echo "      then open http://$IP/  (plain HTTP — give it a DOMAIN for TLS)"
