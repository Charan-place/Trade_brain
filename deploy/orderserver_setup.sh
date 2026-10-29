#!/usr/bin/env bash
# One-time setup of the Groww order server on a fresh Ubuntu 22.04/24.04 VM with a STATIC public IP
# (e.g. AWS Lightsail Mumbai, $7 plan, static IP attached). Run as the default user:
#
#   git clone <your repo> ~/trade_brain_algorithm && cd ~/trade_brain_algorithm
#   bash deploy/orderserver_setup.sh <STATIC_IP>
#
# Then put the three secrets in ~/tbrain-orderserver/.env (the script creates it with placeholders)
# and run:  sudo systemctl restart tbrain-orderserver
set -euo pipefail
IP="${1:?usage: orderserver_setup.sh <STATIC_PUBLIC_IP>}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="$HOME/tbrain-orderserver"
mkdir -p "$HOME_DIR"; chmod 700 "$HOME_DIR"

sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip openssl

python3 -m venv "$HOME_DIR/venv"
"$HOME_DIR/venv/bin/pip" install -q --upgrade pip
"$HOME_DIR/venv/bin/pip" install -q "$REPO" growwapi "uvicorn[standard]"

# Self-signed TLS cert for the static IP (the brain pins this exact cert; no CA needed).
if [ ! -f "$HOME_DIR/cert.pem" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
    -keyout "$HOME_DIR/key.pem" -out "$HOME_DIR/cert.pem" \
    -subj "/CN=$IP" -addext "subjectAltName=IP:$IP"
  chmod 600 "$HOME_DIR/key.pem"
fi

if [ ! -f "$HOME_DIR/.env" ]; then
  umask 077
  cat > "$HOME_DIR/.env" <<ENV
GROWW_API_KEY=
GROWW_API_SECRET=
TBRAIN_ORDER_SERVER_TOKEN=$(openssl rand -hex 32)
ENV
fi

sudo tee /etc/systemd/system/tbrain-orderserver.service >/dev/null <<UNIT
[Unit]
Description=tbrain Groww order server
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$HOME_DIR
EnvironmentFile=$HOME_DIR/.env
ExecStart=$HOME_DIR/venv/bin/tbrain orderserver --host 0.0.0.0 --port 8443 --cert $HOME_DIR/cert.pem --key $HOME_DIR/key.pem
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$HOME_DIR

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable tbrain-orderserver

echo
echo "Done. Next:"
echo "  1. nano $HOME_DIR/.env   -> fill GROWW_API_KEY and GROWW_API_SECRET (token is already generated)"
echo "  2. sudo systemctl restart tbrain-orderserver && sudo systemctl status tbrain-orderserver --no-pager"
echo "  3. Open TCP 8443 in the Lightsail firewall (Networking tab)."
echo "  4. Copy these into the Modal secret 'tbrain-secrets':"
echo "       TBRAIN_ORDER_SERVER_URL=https://$IP:8443"
echo "       TBRAIN_ORDER_SERVER_TOKEN=<the value in $HOME_DIR/.env>"
echo "       TBRAIN_ORDER_SERVER_CERT=<contents of $HOME_DIR/cert.pem, newlines as \\n>"
echo "     Print the cert on one line with:  awk 'NF{printf \"%s\\\\n\", \$0}' $HOME_DIR/cert.pem"
