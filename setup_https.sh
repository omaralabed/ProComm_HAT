#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# ProComm HTTPS Setup — Free trusted certificate via DuckDNS + Let's Encrypt
#
# Run this ONCE on the Raspberry Pi:
#   chmod +x setup_https.sh && sudo ./setup_https.sh
#
# What it does:
#   1. Asks for your DuckDNS subdomain and token (free at duckdns.org)
#   2. Points your DuckDNS domain to this Pi's LAN IP
#   3. Gets a trusted Let's Encrypt certificate (no warnings in Safari)
#   4. Copies the cert into ./certs/ so ProComm uses it automatically
#   5. Sets up auto-renewal so the cert never expires
# ─────────────────────────────────────────────────────────────────────────────

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CERT_DIR="$SCRIPT_DIR/certs"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  ProComm HTTPS — Free Trusted Certificate Setup"
echo "═══════════════════════════════════════════════════"
echo ""
echo "You need a FREE DuckDNS account:"
echo "  1. Go to https://www.duckdns.org"
echo "  2. Sign in with Google or GitHub"
echo "  3. Create a subdomain e.g.  procomm"
echo "     → your domain will be    procomm.duckdns.org"
echo "  4. Copy your token from the top of the page"
echo ""

# ── Collect user input ────────────────────────────────────────────────────────
read -rp "Enter your DuckDNS subdomain (without .duckdns.org): " DUCK_SUB
read -rp "Enter your DuckDNS token: " DUCK_TOKEN
DUCK_DOMAIN="${DUCK_SUB}.duckdns.org"

# ── Get this Pi's LAN IP ──────────────────────────────────────────────────────
LAN_IP=$(hostname -I | awk '{print $1}')
echo ""
echo "Pi LAN IP detected: $LAN_IP"

# ── Point DuckDNS domain to this Pi ──────────────────────────────────────────
echo "Updating DuckDNS → $DUCK_DOMAIN = $LAN_IP ..."
DUCK_RESP=$(curl -s "https://www.duckdns.org/update?domains=${DUCK_SUB}&token=${DUCK_TOKEN}&ip=${LAN_IP}")
if [[ "$DUCK_RESP" != "OK" ]]; then
    echo "ERROR: DuckDNS update failed (response: $DUCK_RESP)"
    echo "Check your subdomain and token and try again."
    exit 1
fi
echo "DuckDNS updated OK — $DUCK_DOMAIN → $LAN_IP"

# ── Install certbot if needed ─────────────────────────────────────────────────
echo ""
echo "Installing certbot..."
apt-get update -qq
apt-get install -y -qq certbot curl

# Install the DuckDNS certbot plugin
if ! python3 -m pip show certbot-dns-duckdns &>/dev/null; then
    pip3 install certbot-dns-duckdns --break-system-packages 2>/dev/null || \
    pip3 install certbot-dns-duckdns
fi

# ── Write DuckDNS credentials file ───────────────────────────────────────────
CREDS_FILE="/etc/duckdns-certbot.ini"
cat > "$CREDS_FILE" <<EOF
dns_duckdns_token = ${DUCK_TOKEN}
EOF
chmod 600 "$CREDS_FILE"

# ── Get the Let's Encrypt certificate ─────────────────────────────────────────
echo ""
echo "Requesting Let's Encrypt certificate for $DUCK_DOMAIN ..."
echo "(This may take up to 2 minutes for DNS propagation)"
echo ""

certbot certonly \
    --authenticator dns-duckdns \
    --dns-duckdns-credentials "$CREDS_FILE" \
    --dns-duckdns-propagation-seconds 60 \
    --non-interactive \
    --agree-tos \
    --register-unsafely-without-email \
    -d "$DUCK_DOMAIN"

# ── Copy certs into ProComm certs/ directory ──────────────────────────────────
echo ""
mkdir -p "$CERT_DIR"
LE_DIR="/etc/letsencrypt/live/${DUCK_DOMAIN}"
cp "${LE_DIR}/fullchain.pem" "${CERT_DIR}/cert.pem"
cp "${LE_DIR}/privkey.pem"   "${CERT_DIR}/key.pem"
chmod 644 "${CERT_DIR}/cert.pem"
chmod 600 "${CERT_DIR}/key.pem"
echo "Certificates copied to $CERT_DIR"

# ── Save config for auto-renewal hook ────────────────────────────────────────
CONFIG_FILE="/etc/procomm-cert.conf"
cat > "$CONFIG_FILE" <<EOF
DUCK_DOMAIN=${DUCK_DOMAIN}
CERT_DIR=${CERT_DIR}
EOF

# ── Install renewal deploy hook ───────────────────────────────────────────────
HOOK_FILE="/etc/letsencrypt/renewal-hooks/deploy/procomm.sh"
cat > "$HOOK_FILE" <<'HOOK'
#!/bin/bash
source /etc/procomm-cert.conf
cp "/etc/letsencrypt/live/${DUCK_DOMAIN}/fullchain.pem" "${CERT_DIR}/cert.pem"
cp "/etc/letsencrypt/live/${DUCK_DOMAIN}/privkey.pem"   "${CERT_DIR}/key.pem"
chmod 644 "${CERT_DIR}/cert.pem"
chmod 600 "${CERT_DIR}/key.pem"
# Restart ProComm if running as a service
systemctl restart procomm 2>/dev/null || true
echo "ProComm cert renewed and deployed."
HOOK
chmod +x "$HOOK_FILE"

# ── Update DuckDNS IP daily (in case LAN IP changes) ─────────────────────────
CRON_FILE="/etc/cron.daily/duckdns-update"
cat > "$CRON_FILE" <<CRON
#!/bin/bash
curl -s "https://www.duckdns.org/update?domains=${DUCK_SUB}&token=${DUCK_TOKEN}&ip=$(hostname -I | awk '{print \$1}')" > /dev/null
CRON
chmod +x "$CRON_FILE"

# ── Update the QR code URL in app.py ─────────────────────────────────────────
APP_PY="$SCRIPT_DIR/app.py"
if grep -q "procomm.local:5443" "$APP_PY"; then
    sed -i "s|https://procomm.local:5443/phone|https://${DUCK_DOMAIN}:5443/phone|g" "$APP_PY"
    echo "Updated QR URL in app.py → https://${DUCK_DOMAIN}:5443/phone"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════"
echo "  ✓ Setup complete!"
echo ""
echo "  Your HTTPS address:"
echo "  https://${DUCK_DOMAIN}:5443/phone"
echo ""
echo "  Safari will now open it with NO warnings."
echo "  Certificate auto-renews every 90 days."
echo ""
echo "  Restart ProComm for changes to take effect."
echo "═══════════════════════════════════════════════════"
echo ""
