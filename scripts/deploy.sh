#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CERT_DIR="/etc/testnet/certs"
CRED_FILE="/etc/testnet/mail-creds"
INSTALL_DIR="/opt/testnet-mail"
NGINX_AVAILABLE="/etc/nginx/sites-available/mail"
NGINX_ENABLED="/etc/nginx/sites-enabled/mail"

: "${SERVER_URL:?SERVER_URL is required (e.g. https://203.0.113.10:8443)}"
: "${NODE_NAME:?NODE_NAME is required (e.g. mail)}"
: "${NODE_SECRET:?NODE_SECRET is required}"
: "${MAIL_DOMAIN:?MAIL_DOMAIN is required (e.g. gmail.com)}"

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: This script must be run as root" >&2
  exit 1
fi

echo "==> Testnet Mail Deploy"
echo "    Server:  $SERVER_URL"
echo "    Node:    $NODE_NAME"
echo "    Domain:  $MAIL_DOMAIN"
echo ""

# ── 1. Fetch TLS certificates ───────────────────────────────────────────────

echo "==> Fetching TLS certificates..."
mkdir -p "$CERT_DIR"
testnet-toolkit certs fetch \
  --server-url "$SERVER_URL" \
  --name "$NODE_NAME" \
  --secret "$NODE_SECRET" \
  --out-dir "$CERT_DIR"
echo "    Certificates written to $CERT_DIR"

# ── 2. Install nginx config ─────────────────────────────────────────────────

echo "==> Installing nginx config..."

export MAIL_DOMAIN
envsubst '${MAIL_DOMAIN}' < "$REPO_DIR/nginx/mail.conf" > "$NGINX_AVAILABLE"

cp "$REPO_DIR/nginx/rate-limit.conf" /etc/nginx/conf.d/rate-limit.conf

ln -sf "$NGINX_AVAILABLE" "$NGINX_ENABLED"

rm -f /etc/nginx/sites-enabled/default

nginx -t
systemctl reload nginx
echo "    nginx configured and reloaded"

# ── 3. Install project files ────────────────────────────────────────────────

echo "==> Installing project files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR/roundcube" "$INSTALL_DIR/config" "$INSTALL_DIR/signup-api"

cp "$REPO_DIR/docker-compose.yml" "$INSTALL_DIR/"
cp "$REPO_DIR/mailserver.env"     "$INSTALL_DIR/"
cp "$REPO_DIR/roundcube/custom-config.php" "$INSTALL_DIR/roundcube/"
cp -r "$REPO_DIR/roundcube/plugins" "$INSTALL_DIR/roundcube/"
cp "$REPO_DIR/signup-api/main.go"    "$INSTALL_DIR/signup-api/"
cp "$REPO_DIR/signup-api/Dockerfile" "$INSTALL_DIR/signup-api/"

sed -i "s/^OVERRIDE_HOSTNAME=.*/OVERRIDE_HOSTNAME=${MAIL_DOMAIN}/" "$INSTALL_DIR/mailserver.env"

echo "MAIL_DOMAIN=${MAIL_DOMAIN}" > "$INSTALL_DIR/.env"

# ── 4. Store credentials for cron (root-only, not in crontab) ────────────────

echo "==> Writing credentials file..."
mkdir -p "$(dirname "$CRED_FILE")"
cat > "$CRED_FILE" <<EOF
SERVER_URL='${SERVER_URL}'
NODE_NAME='${NODE_NAME}'
NODE_SECRET='${NODE_SECRET}'
CERT_DIR='${CERT_DIR}'
EOF
chmod 600 "$CRED_FILE"

# ── 5. Start mailserver and seed accounts ────────────────────────────────────
#
# docker-mailserver v15 refuses to start Dovecot/Postfix unless at least one
# account exists. Start the mailserver alone, seed accounts during its 120s
# grace window, then bring up Roundcube once it's healthy.

echo "==> Starting Docker Compose..."
cd "$INSTALL_DIR"
docker compose pull --quiet --ignore-buildable
docker compose build --quiet

echo "==> Starting mailserver (accounts required before it becomes healthy)..."
docker compose up -d mailserver

echo "==> Seeding email accounts (within startup grace window)..."
sleep 5
MAIL_DOMAIN="$MAIL_DOMAIN" bash "$REPO_DIR/scripts/seed-mail.sh"

# ── 6. Wait for mailserver health, then start remaining services ─────────────

echo "==> Waiting for mailserver to become healthy..."
MAX_WAIT=120
elapsed=0
while [ "$elapsed" -lt "$MAX_WAIT" ]; do
  status=$(docker inspect --format='{{.State.Health.Status}}' mailserver 2>/dev/null || echo "starting")
  if [ "$status" = "healthy" ]; then
    echo "    Mailserver healthy after ${elapsed}s"
    break
  fi
  sleep 3
  elapsed=$((elapsed + 3))
done

if [ "$status" != "healthy" ]; then
  echo "ERROR: mailserver not healthy after ${MAX_WAIT}s (status: $status)" >&2
  echo "       Check: docker compose -f $INSTALL_DIR/docker-compose.yml logs mailserver" >&2
  exit 1
fi

echo "==> Starting Roundcube, signup-api, and docker-proxy..."
docker compose up -d

# ── 7. Certificate renewal cron ─────────────────────────────────────────────

echo "==> Installing certificate renewal cron job..."
cat > /etc/cron.d/testnet-mail-certs << 'CRON'
0 3 * * * root . /etc/testnet/mail-creds && /usr/local/bin/testnet-toolkit certs fetch --server-url "$SERVER_URL" --name "$NODE_NAME" --secret "$NODE_SECRET" --out-dir "$CERT_DIR" && nginx -s reload
CRON
chmod 600 /etc/cron.d/testnet-mail-certs
echo "    Cron job installed (daily at 03:00, credentials in $CRED_FILE)"

# ── 8. Summary ───────────────────────────────────────────────────────────────

PUBLIC_IP=$(curl -s --max-time 5 http://checkip.amazonaws.com 2>/dev/null || echo "<unknown>")

echo ""
echo "============================================"
echo "  Testnet Mail deployed successfully"
echo "============================================"
echo ""
echo "  Domain:     $MAIL_DOMAIN"
echo "  Public IP:  $PUBLIC_IP"
echo "  Webmail:    https://$MAIL_DOMAIN/"
echo "  Health:     https://$MAIL_DOMAIN/health"
echo "  Signup:     https://$MAIL_DOMAIN/signup"
echo "  SMTP:       $PUBLIC_IP:25 (open for testnet nodes)"
echo ""
echo "  Seeded accounts: admin, agent, user, noreply @${MAIL_DOMAIN}"
echo ""
echo "  Add accounts (CLI):"
echo "    docker exec mailserver setup email add newuser@${MAIL_DOMAIN} password"
echo ""
