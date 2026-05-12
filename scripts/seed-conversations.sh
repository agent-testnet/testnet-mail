#!/usr/bin/env bash
# Unified seed script: cleans data, creates accounts, and seeds email conversations
# Usage: MAIL_DOMAIN=gmail.com ./scripts/seed-conversations.sh

set -euo pipefail

MAIL_DOMAIN="${MAIL_DOMAIN:?MAIL_DOMAIN is required}"
CONTAINER="mailserver"
MAX_WAIT=120

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== Testnet Mail Seeder (Clean + Setup + Conversations) ===${NC}"
echo "Mail domain: $MAIL_DOMAIN"
echo ""

# Wait for mailserver to be running
echo "Waiting for mailserver to be healthy..."
elapsed=0
while [ "$elapsed" -lt "$MAX_WAIT" ]; do
  running=$(docker inspect --format='{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo "false")
  if [ "$running" = "true" ]; then
    # Check if Dovecot/Postfix are actually ready
    if docker exec "$CONTAINER" ss --listening --ipv4 --tcp 2>/dev/null | grep -q ':smtp'; then
      break
    fi
  fi
  sleep 2
  elapsed=$((elapsed + 2))
done

if [ "$running" != "true" ]; then
  echo -e "${RED}ERROR: mailserver not running after ${MAX_WAIT}s${NC}" >&2
  exit 1
fi

echo -e "${GREEN}✓ Mailserver is healthy${NC}"
echo ""

# ==================== STEP 0: CLEAN EXISTING DATA ====================
echo -e "${BLUE}--- Step 0: Cleaning Existing Data ---${NC}"

# Clean all emails from all mailboxes
echo "  Removing all existing emails..."
docker exec "$CONTAINER" bash -c "find /var/mail/${MAIL_DOMAIN}/*/cur -type f -delete 2>/dev/null || true"
echo -e "${GREEN}  ✓ Cleaned mailbox data${NC}"

# Optional: Clean Roundcube preferences/sessions database for fresh start
# Uncomment if you want a completely fresh Roundcube setup
# echo "  Cleaning Roundcube database..."
# rm -f /path/to/roundcube/db.sqlite 2>/dev/null || true
# echo -e "${GREEN}  ✓ Cleaned Roundcube database${NC}"

echo ""

# ==================== STEP 1: CREATE ACCOUNTS ====================
echo -e "${BLUE}--- Step 1: Creating Email Accounts ---${NC}"

create_account() {
  local email="$1"
  local password="$2"
  if docker exec "$CONTAINER" setup email list 2>/dev/null | grep -q "${email}"; then
    echo "  ℹ Already exists: $email"
  else
    docker exec "$CONTAINER" setup email add "$email" "$password" 2>&1 || true
    echo "  ✓ Created: $email"
  fi
}

# Create test accounts
create_account "alice@${MAIL_DOMAIN}" "alice-password"
create_account "bob@${MAIL_DOMAIN}" "bob-password"
create_account "charlie@${MAIL_DOMAIN}" "charlie-password"
create_account "diana@${MAIL_DOMAIN}" "diana-password"

# Also create the original system accounts
create_account "admin@${MAIL_DOMAIN}" "testnet-admin-password"
create_account "agent@${MAIL_DOMAIN}" "agent-password"
create_account "user@${MAIL_DOMAIN}" "user-password"
create_account "noreply@${MAIL_DOMAIN}" "noreply-password"

echo ""
echo -e "${BLUE}Active email accounts:${NC}"
docker exec "$CONTAINER" setup email list | sed 's/^/  /'

echo ""

# ==================== STEP 2: SEED CONVERSATIONS ====================
echo -e "${BLUE}--- Step 2: Creating Email Conversations ---${NC}"

# Create conversation between two users
create_conversation() {
  local user1="$1"
  local user2="$2"
  local user1_email="${user1}@${MAIL_DOMAIN}"
  local user2_email="${user2}@${MAIL_DOMAIN}"
  local user1_maildir="/var/mail/${MAIL_DOMAIN}/${user1}"
  local user2_maildir="/var/mail/${MAIL_DOMAIN}/${user2}"

  echo "  Creating conversation: $user1_email ↔ $user2_email"

  # Generate timestamps using macOS and Linux compatible approach
  local ts_5days=$(date -u -v-5d '+%s' 2>/dev/null || date -u -d '5 days ago' '+%s' 2>/dev/null || echo $(($(date +%s) - 432000)))
  local ts_4days=$(date -u -v-4d '+%s' 2>/dev/null || date -u -d '4 days ago' '+%s' 2>/dev/null || echo $(($(date +%s) - 345600)))
  local ts_3days=$(date -u -v-3d '+%s' 2>/dev/null || date -u -d '3 days ago' '+%s' 2>/dev/null || echo $(($(date +%s) - 259200)))
  local ts_2days=$(date -u -v-2d '+%s' 2>/dev/null || date -u -d '2 days ago' '+%s' 2>/dev/null || echo $(($(date +%s) - 172800)))

  # Generate unique Message-IDs
  local msg1_id="<msg1-${ts_5days}-${RANDOM}@${MAIL_DOMAIN}>"
  local msg2_id="<msg2-${ts_4days}-${RANDOM}@${MAIL_DOMAIN}>"
  local msg3_id="<msg3-${ts_3days}-${RANDOM}@${MAIL_DOMAIN}>"
  local msg4_id="<msg4-${ts_2days}-${RANDOM}@${MAIL_DOMAIN}>"

  # Ensure maildir directories exist
  docker exec "$CONTAINER" bash -c "mkdir -p '$user1_maildir/cur' '$user2_maildir/cur'" 2>/dev/null || true

  # Message 1
  docker exec "$CONTAINER" bash -c "
    DATE_STR=\$(date -u -d @${ts_5days} '+%a, %d %b %Y %H:%M:%S +0000')
    cat > /tmp/msg1.txt << ENDMAIL
Subject: Project update and next steps
From: $user1_email
To: $user2_email
Date: \${DATE_STR}
Message-ID: $msg1_id
MIME-Version: 1.0
Content-Type: text/plain; charset=UTF-8
Content-Transfer-Encoding: 8bit

Hi,

I wanted to reach out regarding the project we discussed last week. I've completed the initial research phase and have some findings to share with you.

Key points:
1. Market analysis looks promising
2. Technical feasibility confirmed
3. Budget estimate: ~\$50K

When would be a good time to discuss further?

Best regards,
$user1
ENDMAIL

    for maildir in '$user1_maildir' '$user2_maildir'; do
      filename=\$(date -d @${ts_5days} +%s%N).M0P\$(printf '%05d' \$RANDOM)S00000V000000000000.localhost
      cp /tmp/msg1.txt \"\$maildir/cur/\$filename\"
      chmod 600 \"\$maildir/cur/\$filename\"
      chown docker:docker \"\$maildir/cur/\$filename\" 2>/dev/null || true
    done
  " 2>/dev/null || true

  # Message 2
  docker exec "$CONTAINER" bash -c "
    DATE_STR=\$(date -u -d @${ts_4days} '+%a, %d %b %Y %H:%M:%S +0000')
    cat > /tmp/msg2.txt << ENDMAIL
Subject: Re: Project update and next steps
From: $user2_email
To: $user1_email
Date: \${DATE_STR}
Message-ID: $msg2_id
In-Reply-To: $msg1_id
References: $msg1_id
MIME-Version: 1.0
Content-Type: text/plain; charset=UTF-8
Content-Transfer-Encoding: 8bit

Thanks for the update! The findings sound great. I'm particularly interested in the market analysis results.

I'm available for a call this Thursday at 2 PM or Friday at 10 AM. Which works better for you?

Also, can you send over the detailed breakdown of that \$50K estimate?

Talk soon,
$user2
ENDMAIL

    for maildir in '$user2_maildir' '$user1_maildir'; do
      filename=\$(date -d @${ts_4days} +%s%N).M0P\$(printf '%05d' \$RANDOM)S00000V000000000000.localhost
      cp /tmp/msg2.txt \"\$maildir/cur/\$filename\"
      chmod 600 \"\$maildir/cur/\$filename\"
      chown docker:docker \"\$maildir/cur/\$filename\" 2>/dev/null || true
    done
  " 2>/dev/null || true

  # Message 3
  docker exec "$CONTAINER" bash -c "
    DATE_STR=\$(date -u -d @${ts_3days} '+%a, %d %b %Y %H:%M:%S +0000')
    cat > /tmp/msg3.txt << ENDMAIL
Subject: Re: Project update and next steps
From: $user1_email
To: $user2_email
Date: \${DATE_STR}
Message-ID: $msg3_id
In-Reply-To: $msg2_id
References: $msg1_id $msg2_id
MIME-Version: 1.0
Content-Type: text/plain; charset=UTF-8
Content-Transfer-Encoding: 8bit

Friday at 10 AM works perfectly for me. Let's do it then.

I'm attaching the detailed budget breakdown. Here's the summary:
- Development: \$30K
- Testing & QA: \$12K
- Deployment & Training: \$8K

Looking forward to our discussion!

$user1
ENDMAIL

    for maildir in '$user1_maildir' '$user2_maildir'; do
      filename=\$(date -d @${ts_3days} +%s%N).M0P\$(printf '%05d' \$RANDOM)S00000V000000000000.localhost
      cp /tmp/msg3.txt \"\$maildir/cur/\$filename\"
      chmod 600 \"\$maildir/cur/\$filename\"
      chown docker:docker \"\$maildir/cur/\$filename\" 2>/dev/null || true
    done
  " 2>/dev/null || true

  # Message 4
  docker exec "$CONTAINER" bash -c "
    DATE_STR=\$(date -u -d @${ts_2days} '+%a, %d %b %Y %H:%M:%S +0000')
    cat > /tmp/msg4.txt << ENDMAIL
Subject: Re: Project update and next steps
From: $user2_email
To: $user1_email
Date: \${DATE_STR}
Message-ID: $msg4_id
In-Reply-To: $msg3_id
References: $msg1_id $msg2_id $msg3_id
MIME-Version: 1.0
Content-Type: text/plain; charset=UTF-8
Content-Transfer-Encoding: 8bit

Perfect! I've received the budget breakdown and it looks reasonable. I've also reviewed it with my team and everyone is on board.

See you Friday at 10 AM. I'll send a calendar invite shortly.

Thanks,
$user2
ENDMAIL

    for maildir in '$user2_maildir' '$user1_maildir'; do
      filename=\$(date -d @${ts_2days} +%s%N).M0P\$(printf '%05d' \$RANDOM)S00000V000000000000.localhost
      cp /tmp/msg4.txt \"\$maildir/cur/\$filename\"
      chmod 600 \"\$maildir/cur/\$filename\"
      chown docker:docker \"\$maildir/cur/\$filename\" 2>/dev/null || true
    done
  " 2>/dev/null || true
}

# Create sample conversations
create_conversation "alice" "bob"
create_conversation "charlie" "diana"

echo ""
echo -e "${GREEN}✓ Conversations created${NC}"
echo ""

# ==================== VERIFICATION ====================
echo -e "${BLUE}--- Verification ---${NC}"

echo "Email count per mailbox:"
docker exec "$CONTAINER" bash -c "
  for user in alice bob charlie diana admin agent user noreply; do
    count=\$(find /var/mail/${MAIL_DOMAIN}/\$user/cur -type f 2>/dev/null | wc -l)
    echo \"  \$user: \$count emails\"
  done
" 2>/dev/null || echo "  (error accessing mailboxes)"

echo ""
echo -e "${GREEN}=== Seeding Complete ===${NC}"
echo ""
echo "Next steps:"
echo "  1. Access webmail at http://localhost:8080"
echo "  2. Login with any account (e.g., alice@${MAIL_DOMAIN} / alice-password)"
echo "  3. Check Inbox to see the seeded conversations"
echo ""
