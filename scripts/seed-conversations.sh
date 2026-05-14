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

if [ "${running:-false}" != "true" ]; then
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

echo "Creating dashboard test accounts..."
create_account "alice@${MAIL_DOMAIN}"   "alice-password"
create_account "bob@${MAIL_DOMAIN}"     "bob-password"
create_account "charlie@${MAIL_DOMAIN}" "charlie-password"
create_account "diana@${MAIL_DOMAIN}"   "diana-password"

# Give Dovecot a moment to provision mailboxes before delivery.
sleep 3

# RFC 2822 timestamp N minutes in the past (uses GNU date inside the container).
rfc_date() {
  docker exec "$CONTAINER" date -R -d "$1 minutes ago"
}

send_message() {
  local from="$1" to="$2" subject="$3" minutes_ago="$4" body="$5"
  local date_hdr
  date_hdr=$(rfc_date "$minutes_ago")
  docker exec -i "$CONTAINER" sendmail -f "$from" "$to" <<EOF
From: $from
To: $to
Subject: $subject
Date: $date_hdr

$body
EOF
  echo "  [$minutes_ago min ago] $from -> $to: $subject"
}

A="alice@${MAIL_DOMAIN}"
B="bob@${MAIL_DOMAIN}"
C="charlie@${MAIL_DOMAIN}"
D="diana@${MAIL_DOMAIN}"

echo ""
echo "Seeding conversations..."

# alice <-> bob: lunch plans (3 messages)
send_message "$A" "$B" "Lunch tomorrow?"        240 "Hey Bob, want to grab lunch tomorrow around 12:30?"
send_message "$B" "$A" "Re: Lunch tomorrow?"    235 "Sounds good. The usual spot?"
send_message "$A" "$B" "Re: Lunch tomorrow?"    230 "Yep, see you there."

# bob <-> charlie: code review (2 messages)
send_message "$B" "$C" "PR review request"      180 "Can you take a look at #423 when you get a chance?"
send_message "$C" "$B" "Re: PR review request"  150 "Reviewed -- left a couple comments but overall LGTM."

# alice <-> diana: onboarding thread (4 messages)
send_message "$A" "$D" "Welcome to the team"    600 "Hi Diana, welcome aboard! Let me know if you need anything to get set up."
send_message "$D" "$A" "Re: Welcome to the team" 540 "Thanks Alice! All set up locally. What should I pick up first?"
send_message "$A" "$D" "Re: Welcome to the team" 480 "Start with the onboarding doc in the wiki, then ping me about the ingest pipeline."
send_message "$D" "$A" "Re: Welcome to the team" 420 "Reading now. Will follow up tomorrow."

# charlie <-> diana: docs handoff (2 messages)
send_message "$C" "$D" "Docs handoff"           120 "Diana, I'm passing the API reference docs over to you. Notes in the shared drive."
send_message "$D" "$C" "Re: Docs handoff"        90 "Got it, thanks. I'll have a first pass ready by Friday."

echo ""
echo "Seeding complete. Open the dashboard at http://localhost:5000"
