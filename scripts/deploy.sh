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
: "${DASHBOARD_PASSWORD:?DASHBOARD_PASSWORD is required (operator password for the /dashboard/ login)}"

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
mkdir -p "$INSTALL_DIR/roundcube" "$INSTALL_DIR/config" "$INSTALL_DIR/signup-api" "$INSTALL_DIR/dashboard" "$INSTALL_DIR/mail-classifier"

cp "$REPO_DIR/docker-compose.yml" "$INSTALL_DIR/"
cp "$REPO_DIR/mailserver.env"     "$INSTALL_DIR/"
cp "$REPO_DIR/roundcube/custom-config.php" "$INSTALL_DIR/roundcube/"
cp -r "$REPO_DIR/roundcube/plugins" "$INSTALL_DIR/roundcube/"
cp "$REPO_DIR/signup-api/main.go"    "$INSTALL_DIR/signup-api/"
cp "$REPO_DIR/signup-api/Dockerfile" "$INSTALL_DIR/signup-api/"
cp "$REPO_DIR/dashboard/Dockerfile"       "$INSTALL_DIR/dashboard/"
cp "$REPO_DIR/dashboard/requirements.txt" "$INSTALL_DIR/dashboard/"
cp "$REPO_DIR/dashboard/app.py"           "$INSTALL_DIR/dashboard/"
cp -r "$REPO_DIR/dashboard/templates"     "$INSTALL_DIR/dashboard/"
cp -r "$REPO_DIR/dashboard/static"        "$INSTALL_DIR/dashboard/"
cp "$REPO_DIR/mail-classifier/Dockerfile" "$INSTALL_DIR/mail-classifier/"
cp "$REPO_DIR/mail-classifier/requirements.txt" "$INSTALL_DIR/mail-classifier/"
cp -r "$REPO_DIR/mail-classifier/src" "$INSTALL_DIR/mail-classifier/"

# Use a sub-domain hostname (mail.<domain>) to keep $myhostname distinct
# from the virtual mailbox domain. See the comment in mailserver.env.
sed -i "s/^OVERRIDE_HOSTNAME=.*/OVERRIDE_HOSTNAME=mail.${MAIL_DOMAIN}/" "$INSTALL_DIR/mailserver.env"

# Persistent Roundcube des_key. The roundcube image regenerates a random
# key on every container recreate when none is provided, which silently
# invalidates all logged-in webmail sessions (Roundcube can no longer
# decrypt the IMAP password from $_SESSION; the empty-decrypted password
# then causes rcube_smtp.php to skip SMTP AUTH, and the submission service
# rejects the unauthenticated client). We pin the key to a stable value
# generated once and persisted on the host outside any container volume.
ROUNDCUBE_DES_KEY_FILE="/etc/testnet/roundcube-des-key"
mkdir -p "$(dirname "$ROUNDCUBE_DES_KEY_FILE")"
if [ ! -s "$ROUNDCUBE_DES_KEY_FILE" ]; then
  # Match the image's own format: 24 base64 characters.
  head -c 18 /dev/urandom | base64 | tr -d '\n=' | head -c 24 > "$ROUNDCUBE_DES_KEY_FILE"
  chmod 600 "$ROUNDCUBE_DES_KEY_FILE"
  echo "    Generated new Roundcube des_key at $ROUNDCUBE_DES_KEY_FILE"
fi
ROUNDCUBEMAIL_DES_KEY=$(cat "$ROUNDCUBE_DES_KEY_FILE")

# Persistent dashboard session-signing key. Same pattern as the Roundcube
# des_key above: generate once, persist on the host outside any container
# volume, reuse on every redeploy. This is what keeps operator login
# sessions valid across container recreates -- without a stable key, every
# redeploy invalidates the session cookie and forces a re-login.
DASHBOARD_SECRET_KEY_FILE="/etc/testnet/dashboard-secret-key"
mkdir -p "$(dirname "$DASHBOARD_SECRET_KEY_FILE")"
if [ ! -s "$DASHBOARD_SECRET_KEY_FILE" ]; then
  # 32 random bytes hex-encoded (64 chars) -- plenty of entropy for HMAC signing.
  head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$DASHBOARD_SECRET_KEY_FILE"
  chmod 600 "$DASHBOARD_SECRET_KEY_FILE"
  echo "    Generated new dashboard secret key at $DASHBOARD_SECRET_KEY_FILE"
fi
DASHBOARD_SECRET_KEY=$(cat "$DASHBOARD_SECRET_KEY_FILE")

# .env feeds docker compose's variable interpolation. TESTNET_MAIL_DOMAINS,
# TESTNET_MAIL_RELAYS, ROUNDCUBEMAIL_DES_KEY, DASHBOARD_PASSWORD,
# DASHBOARD_SECRET_KEY, GEMINI_API_KEY/MODEL, and OPENROUTER_API_KEY/MODEL
# are referenced from docker-compose.yml; pass them through as-is (defaults:
# empty for the testnet vars, generated values for the host-side keys,
# operator-supplied for DASHBOARD_PASSWORD and one of the LLM keys).
#
# Backend selection is automatic: the mail-classifier picks Gemini or
# OpenRouter from whichever of GEMINI_API_KEY / OPENROUTER_API_KEY is set
# (and refuses to start if both are set). Leaving both empty makes the
# classifier container crash-loop loudly while the rest of the stack stays
# up; the dashboard then falls back to "pending" badges.
cat > "$INSTALL_DIR/.env" <<EOF
MAIL_DOMAIN=${MAIL_DOMAIN}
TESTNET_MAIL_DOMAINS=${TESTNET_MAIL_DOMAINS:-}
TESTNET_MAIL_RELAYS=${TESTNET_MAIL_RELAYS:-}
ROUNDCUBEMAIL_DES_KEY=${ROUNDCUBEMAIL_DES_KEY}
DASHBOARD_PASSWORD=${DASHBOARD_PASSWORD}
DASHBOARD_SECRET_KEY=${DASHBOARD_SECRET_KEY}
GEMINI_API_KEY=${GEMINI_API_KEY:-}
GEMINI_MODEL=${GEMINI_MODEL:-gemini-2.5-flash-lite}
OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}
OPENROUTER_MODEL=${OPENROUTER_MODEL:-google/gemini-2.5-flash-lite}
EOF
chmod 600 "$INSTALL_DIR/.env"

if [ -n "${GEMINI_API_KEY:-}" ] && [ -n "${OPENROUTER_API_KEY:-}" ]; then
  echo "    ERROR: GEMINI_API_KEY and OPENROUTER_API_KEY are both set -- pick one" >&2
  exit 1
fi
if [ -z "${GEMINI_API_KEY:-}" ] && [ -z "${OPENROUTER_API_KEY:-}" ]; then
  echo "    WARNING: neither GEMINI_API_KEY nor OPENROUTER_API_KEY set -- mail-classifier will crash-loop until one is provided" >&2
fi

# ── 3a. Render postfix testnet-only outbound policy ─────────────────────────
#
# See postfix/postfix-main.cf for the policy itself. The two .tmpl files are
# rendered into $INSTALL_DIR/config/ which is bind-mounted into the mailserver
# container at /tmp/docker-mailserver/, where docker-mailserver picks up
# postfix-main.cf (appended to /etc/postfix/main.cf) and the recipient/
# transport maps it references.

echo "==> Rendering postfix testnet-only outbound policy..."

# Build the regex alternation:
#   "gmail.com,outlook.com" -> "gmail\.com|outlook\.com"
ALL_TESTNET_DOMAINS="${MAIL_DOMAIN}${TESTNET_MAIL_DOMAINS:+,${TESTNET_MAIL_DOMAINS}}"
TESTNET_DOMAINS_REGEX=$(echo "$ALL_TESTNET_DOMAINS" \
  | tr ',' '\n' \
  | sed 's/[[:space:]]//g; /^$/d; s/\./\\./g' \
  | paste -sd '|' -)

# Build the transport map body, one line per peer mail node:
#   TESTNET_MAIL_RELAYS="outlook.com=1.2.3.4:25,yahoo.com=5.6.7.8:25"
#   ->  outlook.com    smtp:[1.2.3.4]:25
#       yahoo.com      smtp:[5.6.7.8]:25
# The square brackets disable MX lookup so Postfix delivers straight to the
# given IP, matching the design doc's node-to-node SMTP routing pattern.
TESTNET_TRANSPORT_LINES=$(echo "${TESTNET_MAIL_RELAYS:-}" \
  | tr ',' '\n' \
  | awk -F= 'NF==2 {
      gsub(/[[:space:]]/, "", $1)
      gsub(/[[:space:]]/, "", $2)
      n = split($2, hp, ":")
      host = hp[1]
      port = (n >= 2 ? hp[2] : "25")
      printf "%-30s smtp:[%s]:%s\n", $1, host, port
    }')

export TESTNET_DOMAINS_REGEX TESTNET_TRANSPORT_LINES

cp "$REPO_DIR/postfix/postfix-main.cf" "$INSTALL_DIR/config/postfix-main.cf"
envsubst '${TESTNET_DOMAINS_REGEX}' \
  < "$REPO_DIR/postfix/testnet-recipients.pcre.tmpl" \
  > "$INSTALL_DIR/config/testnet-recipients.pcre"
envsubst '${TESTNET_TRANSPORT_LINES}' \
  < "$REPO_DIR/postfix/testnet-transport.tmpl" \
  > "$INSTALL_DIR/config/testnet-transport"

# Compile the transport hash map. We invoke `postmap` through a one-shot
# container of the same docker-mailserver image so the .db file exists
# before the mailserver starts (Postfix would otherwise log lookup errors
# until the next reload).
docker run --rm \
  --entrypoint /usr/sbin/postmap \
  -v "$INSTALL_DIR/config:/tmp/cfg" \
  ghcr.io/docker-mailserver/docker-mailserver:15 \
  /tmp/cfg/testnet-transport

echo "    Allowed testnet mail domains: $ALL_TESTNET_DOMAINS"
if [ -n "${TESTNET_MAIL_RELAYS:-}" ]; then
  echo "    Peer mail node transport routes:"
  echo "$TESTNET_TRANSPORT_LINES" | sed 's/^/      /'
else
  echo "    No peer mail node transport routes configured (TESTNET_MAIL_RELAYS empty)"
fi

# ── 3b. Best-effort validation against the live testnet control plane ──────
#
# Warns (does not fail the deploy) if any configured testnet mail domain is
# not actually registered in the control plane's nodes.yaml. Catches typos
# and stale TESTNET_MAIL_DOMAINS values. Skipped silently if the toolkit is
# not installed or the control plane is briefly unreachable.

if command -v testnet-toolkit >/dev/null 2>&1; then
  echo "==> Validating testnet mail domains against control plane..."
  # `seed domains` requires a client API token; for passive nodes we don't have
  # one, so most of the time this call returns 401 and is silently skipped.
  # When an operator runs deploy.sh with API_TOKEN set, the validation kicks in.
  registered=$(testnet-toolkit seed domains \
    --server-url "$SERVER_URL" \
    --api-token "${API_TOKEN:-}" 2>/dev/null || true)
  if [ -n "$registered" ]; then
    for d in $(echo "$ALL_TESTNET_DOMAINS" | tr ',' ' '); do
      if ! echo "$registered" | grep -qx "$d"; then
        echo "    WARN: '$d' is configured here but not registered in nodes.yaml" >&2
      fi
    done
    echo "    Validation complete"
  else
    echo "    Skipped (no API token, or control plane unreachable)"
  fi
fi

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

# Seed the four dashboard test accounts (alice/bob/charlie/diana) plus a
# handful of sample conversations between them. The dashboard's IMAP scrape
# (dashboard/app.py:TEST_ACCOUNTS) hardcodes these accounts; without them
# the dashboard renders an empty page after login and operators conclude
# "nothing happened".
#
# The seed script is idempotent (each send is gated on doveadm-search of
# the recipient's mailbox for the (from, subject) pair), so we run it on
# every deploy. That way new example messages added to seed-conversations.sh
# automatically land on existing deployments after a redeploy without
# duplicating the conversations that already exist.
echo "==> Seeding dashboard test accounts and conversations (idempotent)..."
MAIL_DOMAIN="$MAIL_DOMAIN" bash "$REPO_DIR/scripts/seed-conversations.sh"

echo "==> Starting Roundcube, signup-api, dashboard, and docker-proxy..."
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
if [ -n "${GEMINI_API_KEY:-}" ]; then
  echo "  Classifier: mail-classifier (Roundcube SQLite + Gemini, model=${GEMINI_MODEL:-gemini-2.5-flash-lite})"
elif [ -n "${OPENROUTER_API_KEY:-}" ]; then
  echo "  Classifier: mail-classifier (Roundcube SQLite + OpenRouter, model=${OPENROUTER_MODEL:-google/gemini-2.5-flash-lite})"
else
  echo "  Classifier: mail-classifier DISABLED (set GEMINI_API_KEY or OPENROUTER_API_KEY to enable)"
fi
echo ""
echo "  Add accounts (CLI):"
echo "    docker exec mailserver setup email add newuser@${MAIL_DOMAIN} password"
echo ""
