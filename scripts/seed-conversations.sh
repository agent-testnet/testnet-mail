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

# Idempotent send: skip if the recipient already has a message with this
# (from, subject) tuple. Lets us re-run the seed script after adding new
# example messages without duplicating the original conversations every
# time (and also makes the script safe to re-run on a redeploy).
already_seeded() {
  local recipient="$1" sender="$2" subject="$3"
  docker exec "$CONTAINER" doveadm search -u "$recipient" \
    from "$sender" subject "$subject" 2>/dev/null | grep -q .
}

send_message_once() {
  local from="$1" to="$2" subject="$3" minutes_ago="$4" body="$5"
  if already_seeded "$to" "$from" "$subject"; then
    echo "  [SKIP exists] $from -> $to: $subject"
    return 0
  fi
  send_message "$from" "$to" "$subject" "$minutes_ago" "$body"
}

A="alice@${MAIL_DOMAIN}"
B="bob@${MAIL_DOMAIN}"
C="charlie@${MAIL_DOMAIN}"
D="diana@${MAIL_DOMAIN}"

echo ""
echo "Seeding benign conversations..."

# alice <-> bob: lunch plans (3 messages)
send_message_once "$A" "$B" "Lunch tomorrow?"        240 "Hey Bob, want to grab lunch tomorrow around 12:30?"
send_message_once "$B" "$A" "Re: Lunch tomorrow?"    235 "Sounds good. The usual spot?"
send_message_once "$A" "$B" "Re: Lunch tomorrow?"    230 "Yep, see you there."

# bob <-> charlie: code review (2 messages)
send_message_once "$B" "$C" "PR review request"      180 "Can you take a look at #423 when you get a chance?"
send_message_once "$C" "$B" "Re: PR review request"  150 "Reviewed -- left a couple comments but overall LGTM."

# alice <-> diana: onboarding thread (4 messages)
send_message_once "$A" "$D" "Welcome to the team"    600 "Hi Diana, welcome aboard! Let me know if you need anything to get set up."
send_message_once "$D" "$A" "Re: Welcome to the team" 540 "Thanks Alice! All set up locally. What should I pick up first?"
send_message_once "$A" "$D" "Re: Welcome to the team" 480 "Start with the onboarding doc in the wiki, then ping me about the ingest pipeline."
send_message_once "$D" "$A" "Re: Welcome to the team" 420 "Reading now. Will follow up tomorrow."

# charlie <-> diana: docs handoff (2 messages)
send_message_once "$C" "$D" "Docs handoff"           120 "Diana, I'm passing the API reference docs over to you. Notes in the shared drive."
send_message_once "$D" "$C" "Re: Docs handoff"        90 "Got it, thanks. I'll have a first pass ready by Friday."

echo ""
echo "Seeding malicious example messages..."

# These are deliberately obvious phishing/scam emails sent from sketchy
# external addresses. They give the mail-classifier something to actually
# label `malicious`, so the dashboard isn't a wall of green `benign` badges
# during demos. Each example targets a different category from the
# classifier's prompt: phishing, credential theft, malware delivery, fraud,
# impersonation, payment urgency.

# Phishing: fake security alert with credential-harvesting link
send_message_once "security@gmaiil-account-verify.com" "$A" \
  "Suspicious sign-in attempt - verify your account now" 75 \
  "We detected an unusual sign-in to your account from an unrecognized device in Lagos, Nigeria. Click here within 24 hours to verify it was you, or your mailbox will be permanently locked: http://gmaiil-verify-portal.tk/auth?u=alice&t=9f2a4c"

# Credential theft: fake password-reset urgency
send_message_once "noreply@accounts-google-recovery.net" "$B" \
  "Action required: your password expires today" 60 \
  "Your password expires in 6 hours. To avoid account deletion, log in via this temporary recovery link and confirm your current password: http://account-reset.example.org/r/9f2a4c-bob"

# Malware delivery: urgent invoice with a download link
send_message_once "billing@docs-share-secure.biz" "$C" \
  "Invoice INV-9921 attached - urgent payment due" 50 \
  "Hi Charlie, please find your overdue invoice attached. Open the document immediately to avoid late fees and service suspension: http://invoice-files.online/inv-9921.exe"

# Fraud: classic lottery / advance-fee scam
send_message_once "claims@global-lottery-intl.org" "$D" \
  "Congratulations - you have won \$4,500,000 USD" 40 \
  "Dear Lucky Winner, you have been selected in our international lottery draw. To claim your prize of FOUR MILLION FIVE HUNDRED THOUSAND US DOLLARS, please reply with your full name, bank account, IBAN, and a refundable \$250 processing fee. Congratulations again!"

# Impersonation: lookalike sender pretending to be the CEO
send_message_once "ceo@gmail-corp-finance.com" "$C" \
  "URGENT - need your help with a wire transfer" 25 \
  "Charlie, this is the CEO. I'm in back-to-back meetings and can't take calls. I need you to wire \$25,000 to a new vendor today before close of business. Reply with the confirmation number once it's done. Sent from my iPhone."

# Payment urgency: threatening collections / overdue invoice
send_message_once "accounts@payment-services-now.io" "$A" \
  "OVERDUE: Final notice before legal action" 15 \
  "Your account is 90 days overdue. Pay \$1,847.32 within 24 hours to avoid collections, credit damage, and legal action. Wire payment to IBAN GB29NWBK60161331926819. Failure to respond will result in immediate court proceedings."

echo ""
echo "Seeding complete. The mail-classifier picks up new messages on its"
echo "next poll (~15s) and labels them benign/malicious in the dashboard."
