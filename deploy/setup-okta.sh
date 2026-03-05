#!/bin/bash
# Run on EC2 to install and configure oauth2-proxy for Okta
# Usage: bash setup-okta.sh <OKTA_DOMAIN> <CLIENT_ID> <CLIENT_SECRET>
# Example: bash setup-okta.sh alkira.okta.com 0oa1abc... secret123

set -e

OKTA_DOMAIN=$1
CLIENT_ID=$2
CLIENT_SECRET=$3

if [ -z "$OKTA_DOMAIN" ] || [ -z "$CLIENT_ID" ] || [ -z "$CLIENT_SECRET" ]; then
    echo "Usage: bash setup-okta.sh <OKTA_DOMAIN> <CLIENT_ID> <CLIENT_SECRET>"
    exit 1
fi

echo "=== Installing oauth2-proxy ==="
VERSION="7.6.0"
wget -q "https://github.com/oauth2-proxy/oauth2-proxy/releases/download/v${VERSION}/oauth2-proxy-v${VERSION}.linux-amd64.tar.gz" -O /tmp/oauth2-proxy.tar.gz
tar -xzf /tmp/oauth2-proxy.tar.gz -C /tmp
sudo mv /tmp/oauth2-proxy-v${VERSION}.linux-amd64/oauth2-proxy /usr/local/bin/oauth2-proxy
sudo chmod +x /usr/local/bin/oauth2-proxy
echo "  Installed oauth2-proxy v${VERSION}"

echo "=== Generating cookie secret ==="
COOKIE_SECRET=$(python3 -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())")

echo "=== Writing config ==="
sudo mkdir -p /etc/oauth2-proxy
sudo tee /etc/oauth2-proxy/oauth2-proxy.cfg > /dev/null <<EOF
provider = "oidc"
oidc-issuer-url = "https://${OKTA_DOMAIN}/oauth2/default"
client-id = "${CLIENT_ID}"
client-secret = "${CLIENT_SECRET}"
cookie-secret = "${COOKIE_SECRET}"
cookie-secure = false
cookie-name = "_alkira_oauth2"
email-domain = "alkira.net"
upstreams = ["http://localhost:80/"]
http-address = "127.0.0.1:4180"
redirect-url = "http://alkira-dashboard.duckdns.org/oauth2/callback"
skip-provider-button = true
silence-ping-logging = true
EOF
sudo chmod 600 /etc/oauth2-proxy/oauth2-proxy.cfg
echo "  Config written to /etc/oauth2-proxy/oauth2-proxy.cfg"

echo "=== Deploying nginx config with dual auth support ==="
sudo cp /tmp/dashboard.conf /etc/nginx/conf.d/dashboard.conf 2>/dev/null || \
  echo "  Note: copy /Users/.../nginx/dashboard.conf to EC2 first"
sudo nginx -t && sudo systemctl reload nginx
echo "  nginx reloaded"

echo "=== Installing systemd service ==="
sudo tee /etc/systemd/system/oauth2-proxy.service > /dev/null <<'EOF'
[Unit]
Description=OAuth2 Proxy (Okta)
After=network.target

[Service]
User=ubuntu
ExecStart=/usr/local/bin/oauth2-proxy --config=/etc/oauth2-proxy/oauth2-proxy.cfg
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable oauth2-proxy
sudo systemctl start oauth2-proxy
echo "  oauth2-proxy service started"

echo ""
echo "=== Done! ==="
echo "Dashboard now requires Okta login at: http://alkira-dashboard.duckdns.org"
echo "Verify status: sudo systemctl status oauth2-proxy"
