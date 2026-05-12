#!/usr/bin/env bash
set -euo pipefail

MAIL_DOMAIN="${MAIL_DOMAIN:?MAIL_DOMAIN is required}"
CONTAINER="mailserver"
MAX_WAIT=120

echo "Waiting for mailserver container to be running..."
elapsed=0
while [ "$elapsed" -lt "$MAX_WAIT" ]; do
  running=$(docker inspect --format='{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo "false")
  if [ "$running" = "true" ]; then
    break
  fi
  sleep 2
  elapsed=$((elapsed + 2))
done

if [ "$running" != "true" ]; then
  echo "ERROR: mailserver not running after ${MAX_WAIT}s" >&2
  exit 1
fi

create_account() {
  local email="$1" password="$2"
  if docker exec "$CONTAINER" setup email list 2>/dev/null | grep -q "${email}"; then
    echo "  Already exists: $email"
  else
    docker exec "$CONTAINER" setup email add "$email" "$password" 2>&1 || true
    echo "  Processed: $email"
  fi
}

echo "Creating email accounts..."

create_account "admin@${MAIL_DOMAIN}" "testnet-admin-password"
create_account "agent@${MAIL_DOMAIN}" "agent-password"
create_account "user@${MAIL_DOMAIN}"  "user-password"
create_account "noreply@${MAIL_DOMAIN}" "noreply-password"

echo ""
echo "Seeding complete. Accounts:"
docker exec "$CONTAINER" setup email list
