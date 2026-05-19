import os
import sys
import hmac
import secrets
import sqlite3
from functools import wraps
from flask import (
    Flask,
    render_template,
    jsonify,
    request,
    redirect,
    url_for,
    session,
    make_response,
    abort,
    flash,
    get_flashed_messages,
)
from datetime import datetime, timedelta
import imaplib
import email
from email.header import decode_header
from collections import defaultdict
import re
from email.utils import parsedate_to_datetime
import time

# ── Auth configuration (read at module import time) ──────────────────────────
#
# Read at import time, not inside `if __name__ == '__main__'`, because the
# production entrypoint is gunicorn, which imports `app:app` directly and
# never executes the __main__ block. A misconfigured deploy must fail the
# worker boot loudly rather than silently serve unauthenticated.

DASHBOARD_PASSWORD = os.getenv('DASHBOARD_PASSWORD', '')
DASHBOARD_SECRET_KEY = os.getenv('DASHBOARD_SECRET_KEY', '')
DASHBOARD_SESSION_HOURS = int(os.getenv('DASHBOARD_SESSION_HOURS', '12'))
# DASHBOARD_ALLOW_PLAIN_HTTP=1 is a local-dev escape hatch that drops the
# /dashboard URL prefix and disables Secure cookie flags so the app is reachable
# at http://127.0.0.1:5000/login without nginx in front. It is intentionally
# named for the security trade-off (plain HTTP) rather than the use case
# ("dev"), so an operator skimming a .env file can see the cost.
#
# Production deploys never set this: the prod docker-compose.yml does not
# reference it, the host-side deploy script copies only docker-compose.yml
# (not the override file that turns it on), and any unintended setting trips
# the loud WARNING below at boot.
DASHBOARD_ALLOW_PLAIN_HTTP = os.getenv(
    'DASHBOARD_ALLOW_PLAIN_HTTP', ''
).lower() in ('1', 'true', 'yes')
# Explicit SCRIPT_NAME wins over the local-dev default so an operator running
# Flask behind a different prefix can still set it manually.
SCRIPT_NAME = os.getenv(
    'SCRIPT_NAME',
    '' if DASHBOARD_ALLOW_PLAIN_HTTP else '/dashboard',
)

if DASHBOARD_ALLOW_PLAIN_HTTP:
    print(
        "WARNING: DASHBOARD_ALLOW_PLAIN_HTTP=1 is set. The dashboard is running "
        "in local-dev mode (no /dashboard URL prefix, no Secure cookie flag). "
        "This is unsafe on any reachable network. Unset for production.",
        file=sys.stderr,
    )

if not DASHBOARD_PASSWORD:
    print(
        "FATAL: DASHBOARD_PASSWORD is not set. Refusing to start an unauthenticated dashboard.",
        file=sys.stderr,
    )
    raise SystemExit(1)

if not DASHBOARD_SECRET_KEY:
    print(
        "FATAL: DASHBOARD_SECRET_KEY is not set. Refusing to start without a stable session-signing key.",
        file=sys.stderr,
    )
    raise SystemExit(1)

# ── Flask app setup ──────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = DASHBOARD_SECRET_KEY
app.config.update(
    SESSION_COOKIE_SECURE=not DASHBOARD_ALLOW_PLAIN_HTTP,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    # Scope the session cookie to the public dashboard prefix so the browser
    # never sends it to Roundcube/signup-api on the same hostname.
    SESSION_COOKIE_PATH=SCRIPT_NAME or '/',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=DASHBOARD_SESSION_HOURS),
)


# Tell Flask its public URL prefix so url_for(...) emits /dashboard/... links
# even though nginx strips the prefix before proxying. Without this, nav
# links and form actions would render as bare /, /login, etc., and 404 at
# the public edge.
class PrefixMiddleware:
    def __init__(self, wsgi_app, prefix):
        self.wsgi_app = wsgi_app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        environ['SCRIPT_NAME'] = self.prefix
        return self.wsgi_app(environ, start_response)


if SCRIPT_NAME:
    app.wsgi_app = PrefixMiddleware(app.wsgi_app, SCRIPT_NAME)

# ── Mail/account configuration ───────────────────────────────────────────────

MAIL_DOMAIN = os.getenv('MAIL_DOMAIN', 'gmail.com')
# MAIL_SERVER kept as a backward-compat fallback for the env name previously
# (mistakenly) set in docker-compose.yml. The compose file now uses
# MAILSERVER_HOST directly; this fallback can be dropped on the next sweep.
MAILSERVER_HOST = os.getenv('MAILSERVER_HOST') or os.getenv('MAIL_SERVER') or 'mailserver'
MAILSERVER_PORT = int(os.getenv('MAILSERVER_PORT', '143'))
CLASSIFIER_DB_PATH = os.getenv('CLASSIFIER_DB_PATH', '/var/roundcube/db/sqlite.db')
TEST_ACCOUNTS = [
    {"email": f"alice@{MAIL_DOMAIN}", "password": "alice-password"},
    {"email": f"bob@{MAIL_DOMAIN}", "password": "bob-password"},
    {"email": f"charlie@{MAIL_DOMAIN}", "password": "charlie-password"},
    {"email": f"diana@{MAIL_DOMAIN}", "password": "diana-password"},
    # Live agent mailboxes (not created by seed-conversations.sh).
    {"email": f"lobby@{MAIL_DOMAIN}", "password": "lobbypass"},
    {"email": f"mrsmith@{MAIL_DOMAIN}", "password": "smithpass"},
]


# ── Auth helpers ─────────────────────────────────────────────────────────────

CSRF_COOKIE_NAME = '_csrf'


def _csrf_cookie_path():
    # Path the browser sends the cookie back on. Must match the public URL
    # of POST /login, which is `${SCRIPT_NAME}/login`.
    return (SCRIPT_NAME or '') + '/login'


def _attach_csrf_cookie(response, token):
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        max_age=3600,
        path=_csrf_cookie_path(),
        secure=not DASHBOARD_ALLOW_PLAIN_HTTP,
        httponly=True,
        samesite='Strict',
    )
    return response


def _valid_csrf():
    cookie = request.cookies.get(CSRF_COOKIE_NAME, '')
    form_value = request.form.get(CSRF_COOKIE_NAME, '')
    if not cookie or not form_value:
        return False
    return hmac.compare_digest(cookie, form_value)


# ── Session-scoped CSRF (for authenticated POSTs) ────────────────────────────
#
# The login flow above uses a per-request cookie-scoped CSRF token because
# the session doesn't exist yet at that point. Once the operator is logged
# in we mint a stable token inside the Flask session itself and reuse it
# across every authenticated POST form (reclassify, future actions), so we
# don't have to attach a fresh cookie on every render.

def _session_csrf_token() -> str:
    """Return the per-session CSRF token, minting it on first access.

    Templates rendered by authenticated GET handlers receive this value as
    `csrf_token` and embed it as a hidden form field. The matching POST
    handler validates it via `_valid_session_csrf()`."""
    token = session.get('csrf')
    if not token:
        token = secrets.token_hex(16)
        session['csrf'] = token
    return token


def _valid_session_csrf() -> bool:
    expected = session.get('csrf', '')
    form_value = request.form.get(CSRF_COOKIE_NAME, '')
    if not expected or not form_value:
        return False
    return hmac.compare_digest(expected, form_value)


def _render_login(error, next_target, status=200):
    """Render the login template with a freshly minted CSRF token attached as a
    cookie. Both the GET and the failed-POST paths use this helper so the
    cookie+form contract is set up identically."""
    token = secrets.token_hex(16)
    body = render_template(
        'login.html',
        error=error,
        next=next_target,
        csrf_token=token,
        active_nav='',
    )
    response = make_response(body, status)
    return _attach_csrf_cookie(response, token)


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get('authed'):
            # script_root + path gives us the public URL (with the
            # /dashboard prefix re-attached) so the post-login redirect
            # lands back inside the dashboard rather than at Roundcube's /.
            return redirect(url_for('login', next=request.script_root + request.path))
        return view(*args, **kwargs)
    return wrapper

def connect_imap(email_addr, password):
    """Connect to IMAP server"""
    try:
        imap = imaplib.IMAP4(MAILSERVER_HOST, MAILSERVER_PORT)
        imap.login(email_addr, password)
        return imap
    except Exception as e:
        print(f"Failed to connect to IMAP for {email_addr}: {e}")
        return None

def decode_email_header(header):
    """Decode email header"""
    if header is None:
        return ""
    if isinstance(header, str):
        return header
    try:
        decoded_parts = decode_header(header)
        result = ""
        for text, charset in decoded_parts:
            if isinstance(text, bytes):
                result += text.decode(charset or 'utf-8', errors='ignore')
            else:
                result += str(text)
        return result
    except:
        return str(header)

def get_email_body(msg):
    """Extract body from email message"""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                    break
                except:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
        except:
            body = msg.get_payload()
    
    # Clean up the body
    body = body.strip()
    return body

def extract_email_address(email_string):
    """Extract email address from a string that may contain name + email"""
    if not email_string:
        return None
    # Try to find email in format: "Name <email@domain>" or just "email@domain"
    match = re.search(r'[\w\.-]+@[\w\.-]+', email_string)
    if match:
        return match.group().lower()
    return None

def parse_email_date(date_str):
    """Parse email date with multiple fallback strategies"""
    if not date_str:
        return datetime.now()
    
    try:
        # Try standard RFC 2822 parsing first
        result = parsedate_to_datetime(date_str)
        print(f"✓ Parsed date: {date_str} -> {result}")
        return result
    except Exception as e:
        print(f"RFC 2822 parsing failed for '{date_str}': {e}")
    
    # Try alternative parsing methods
    try:
        # Remove timezone info and try basic parsing
        date_part = date_str.split('+')[0].split('-')[0].strip()
        result = datetime.strptime(date_part, '%a, %d %b %Y %H:%M:%S')
        print(f"✓ Parsed date (alt1): {date_str} -> {result}")
        return result
    except Exception as e:
        print(f"Alt parsing 1 failed: {e}")
    
    try:
        # Try without day of week
        result = datetime.strptime(date_str[:19], '%d %b %Y %H:%M:%S')
        print(f"✓ Parsed date (alt2): {date_str} -> {result}")
        return result
    except Exception as e:
        print(f"Alt parsing 2 failed: {e}")
    
    # Fallback to current time if all parsing fails
    print(f"✗ Could not parse date: {date_str}, using current time")
    return datetime.now()

def load_classifications(message_refs):
    """Load LLM classifications keyed by mailbox owner + Message-ID."""
    refs = {(account_email, message_id) for account_email, message_id in message_refs if message_id}
    if not refs:
        return {}

    placeholders = ", ".join(["(?, ?)"] * len(refs))
    params = []
    for account_email, message_id in refs:
        params.extend([account_email, message_id])

    try:
        with sqlite3.connect(CLASSIFIER_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT account_email, message_id, classification_status,
                       classification_label, classification_severity
                FROM classifier_emails
                WHERE (account_email, message_id) IN ({placeholders})
                """,
                params,
            ).fetchall()
    except sqlite3.Error as e:
        print(f"Failed to load classifications from {CLASSIFIER_DB_PATH}: {e}")
        return {}

    return {
        (row["account_email"], row["message_id"]): {
            "status": row["classification_status"],
            "label": row["classification_label"] or "",
            # Coerce NULL severity to 0 so downstream aggregation
            # (max/sum) doesn't need to special-case None at every call
            # site. Genuinely-pending rows just contribute 0.
            "severity": row["classification_severity"] or 0,
        }
        for row in rows
    }


# Precedence used when reducing per-message labels to a single conversation
# badge: higher number = more important to surface. `pwned` outranks
# `malicious` because it tells the operator someone already fell for the
# attack, not just that the inbox received one.
_LABEL_RANK = {"pending": 0, "benign": 1, "malicious": 2, "pwned": 3}


def _conversation_aggregates(messages):
    """Compute conversation-level fields from its list of messages.

    Returned keys:
      max_severity   -- highest severity across messages (0 if all pending)
      total_severity -- sum across messages, used for 3D node sizing
      worst_label    -- highest-precedence label that appears in the thread
      label_counts   -- per-label tallies for the dashboard stats panels
    """
    counts = {"benign": 0, "malicious": 0, "pwned": 0, "pending": 0}
    max_severity = 0
    total_severity = 0
    worst_label = "pending"
    worst_rank = _LABEL_RANK["pending"]

    for msg in messages:
        label = msg.get("classification_label") or "pending"
        if label not in counts:
            label = "pending"
        counts[label] += 1

        severity = int(msg.get("classification_severity") or 0)
        if severity > max_severity:
            max_severity = severity
        total_severity += severity

        rank = _LABEL_RANK.get(label, 0)
        if rank > worst_rank:
            worst_rank = rank
            worst_label = label

    return {
        "max_severity": max_severity,
        "total_severity": total_severity,
        "worst_label": worst_label,
        "label_counts": counts,
    }

def fetch_conversations():
    """Fetch real conversations from mailserver"""
    conversations = defaultdict(lambda: {"messages": [], "participants": set(), "message_ids": set()})
    conv_id = 0
    conv_map = {}
    message_refs = []
    
    for account in TEST_ACCOUNTS:
        email_addr = account["email"].lower()
        password = account["password"]
        
        imap = connect_imap(email_addr, password)
        if not imap:
            continue
        
        try:
            # Select INBOX
            imap.select("INBOX")
            status, messages = imap.search(None, "ALL")
            
            if status == "OK" and messages[0]:
                msg_ids = messages[0].split()
                # Get latest 50 messages
                msg_ids = msg_ids[-50:] if len(msg_ids) > 50 else msg_ids
                
                for msg_id in msg_ids:
                    status, msg_data = imap.fetch(msg_id, "(RFC822 INTERNALDATE)")
                    if status == "OK":
                        msg = email.message_from_bytes(msg_data[0][1])
                        
                        # Get unique Message-ID to avoid duplicates
                        message_id = msg.get("Message-ID", "")
                        
                        sender = decode_email_header(msg.get("From", ""))
                        recipient = decode_email_header(msg.get("To", ""))
                        subject = decode_email_header(msg.get("Subject", ""))
                        body = get_email_body(msg)
                        
                        # Try Date header first, then INTERNALDATE
                        timestamp = msg.get("Date", "")
                        
                        # Parse the email date
                        dt = parse_email_date(timestamp)
                        
                        print(f"Message from {sender}: Date='{timestamp}' -> {dt.isoformat()}")
                        
                        # Extract sender/recipient email addresses
                        sender_email = extract_email_address(sender)
                        recipient_email = extract_email_address(recipient)
                        if not sender_email:
                            continue  # Skip if we can't extract a valid email

                        if not recipient_email:
                            # Fallback to mailbox owner when To header is missing
                            recipient_email = email_addr
                        
                        # Create a conversation key (sorted pair of sender and recipient)
                        conv_key = tuple(sorted([sender_email, recipient_email]))
                        
                        if conv_key not in conv_map:
                            conv_map[conv_key] = conv_id
                            conv_id += 1
                        
                        conv_idx = conv_map[conv_key]
                        
                        # Skip if we've already added this message (avoid duplicates)
                        if message_id and message_id in conversations[conv_idx]["message_ids"]:
                            continue
                        
                        if message_id:
                            conversations[conv_idx]["message_ids"].add(message_id)
                            message_refs.append((email_addr, message_id))
                        
                        conversations[conv_idx]["messages"].append({
                            "account_email": email_addr,
                            "message_id": message_id,
                            "sender": sender_email,
                            "text": body,
                            "timestamp": dt.isoformat(),
                            "subject": subject
                        })
                        conversations[conv_idx]["participants"].add(sender_email)
                        conversations[conv_idx]["participants"].add(recipient_email)
        
        except Exception as e:
            print(f"Error fetching messages for {email_addr}: {e}")
        finally:
            try:
                imap.close()
            except:
                pass

    classifications = load_classifications(message_refs)
    
    # Format conversations - ensure we always have valid participants
    result = []
    for conv_id, conv_data in conversations.items():
        if conv_data["messages"]:
            participants = sorted(list(conv_data["participants"]))
            # Only include conversations with at least 2 participants
            if len(participants) >= 2:
                for msg in conv_data["messages"]:
                    classification = classifications.get((msg["account_email"], msg["message_id"]), {})
                    msg["classification_status"] = classification.get("status", "pending")
                    msg["classification_label"] = classification.get("label", "")
                    msg["classification_severity"] = classification.get("severity", 0)

                # Sort messages by timestamp to maintain conversation order
                sorted_messages = sorted(conv_data["messages"], key=lambda x: x["timestamp"])
                print(f"Conversation {conv_id}: {participants[0]} ↔ {participants[1]}")
                for i, msg in enumerate(sorted_messages):
                    print(f"  Message {i+1}: {msg['sender']} @ {msg['timestamp']}")

                aggregates = _conversation_aggregates(sorted_messages)
                result.append({
                    "id": conv_id,
                    "sender": participants[0],
                    "receiver": participants[1],
                    "messages": sorted_messages,
                    "message_count": len(sorted_messages),
                    **aggregates,
                })

    # Sort conversations so the most recently active thread is on top in the
    # chat sidebar and the dashboard cards. messages[-1] is the newest because
    # sorted_messages above is timestamp-ascending.
    result.sort(key=lambda c: c["messages"][-1]["timestamp"], reverse=True)

    return result

_LABEL_KEYS = ("benign", "malicious", "pwned", "pending")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _msg_label(msg):
    """Bucket name for stats roll-ups: any unknown / empty label is
    coerced to `pending` so we never end up with stray keys."""
    label = msg.get("classification_label") or "pending"
    return label if label in _LABEL_KEYS else "pending"


def get_real_stats(conversations=None):
    """Generate statistics from real data.

    Accepts an optional pre-fetched conversations list so callers (e.g.
    `index()`) that need both the conversations and the stats don't pay
    for two IMAP roundtrips and two SQLite reads. Falls back to fetching
    when called without context (the `/api/stats` endpoint)."""
    if conversations is None:
        conversations = fetch_conversations()

    # Per-user message counts split by classification label so the bar
    # chart can render a stacked breakdown (benign / malicious / pwned /
    # pending) per user instead of a single flat total. The dict
    # comprehension uses _LABEL_KEYS so every user dict has the same shape
    # even when they've only ever sent benign mail.
    user_label_counts = defaultdict(lambda: {k: 0 for k in _LABEL_KEYS})
    for conv in conversations:
        for msg in conv["messages"]:
            user_label_counts[msg["sender"]][_msg_label(msg)] += 1

    # Per-day frequency tracked separately for total / malicious / pwned
    # so the line chart can layer three series. Total is the sum across
    # all labels, not just malicious+pwned, so the chart still reflects
    # how busy each weekday is overall.
    freq_by_day = {day: {"total": 0, "malicious": 0, "pwned": 0} for day in _DAYS}
    for conv in conversations:
        for msg in conv["messages"]:
            try:
                dt = datetime.fromisoformat(msg["timestamp"])
            except (TypeError, ValueError):
                continue
            day_name = _DAYS[dt.weekday()]
            bucket = freq_by_day[day_name]
            bucket["total"] += 1
            label = _msg_label(msg)
            if label in bucket:
                bucket[label] += 1

    active_users = sum(1 for buckets in user_label_counts.values()
                       if sum(buckets.values()) > 0)

    classification_stats = _compute_classification_stats(conversations)

    return {
        "total_users": len(TEST_ACCOUNTS),
        "total_conversations": len(conversations),
        "total_messages": sum(c["message_count"] for c in conversations),
        "active_today": active_users,
        "message_distribution": [
            {
                "user": user,
                "total": sum(buckets.values()),
                **buckets,
            }
            # Sort by total desc so the busiest mailbox sits on the left
            # of the bar chart; alphabetical fallback keeps the order
            # deterministic when two users tie.
            for user, buckets in sorted(
                user_label_counts.items(),
                key=lambda item: (-sum(item[1].values()), item[0]),
            )
        ],
        "conversation_frequency": [
            {
                "day": day,
                "total": freq_by_day[day]["total"],
                "malicious": freq_by_day[day]["malicious"],
                "pwned": freq_by_day[day]["pwned"],
            }
            for day in _DAYS
        ],
        **classification_stats,
    }


def _compute_classification_stats(conversations):
    """Roll per-message classifications into dashboard-wide stats, keeping
    `malicious` and `pwned` in separate buckets so the UI can rank
    attackers and victims independently.

    Returned keys:
      label_counts             -- benign/malicious/pwned/pending totals
      top_malicious_users      -- top 10 senders of `malicious` mail
      top_pwned_users          -- top 10 senders of `pwned` replies (victims)
      top_malicious_messages   -- top 10 `malicious` messages by severity
      top_pwned_messages       -- top 10 `pwned` messages by severity
    """
    label_counts = {k: 0 for k in _LABEL_KEYS}
    # Per-sender tallies. We rank attackers by who *sends* malicious mail
    # (not who happens to share a conversation with one), and victims by
    # who sends `pwned` replies, because that's the message whose author
    # got compromised.
    sender_buckets = defaultdict(lambda: {
        "malicious_count": 0,
        "malicious_severity": 0,
        "pwned_count": 0,
        "pwned_severity": 0,
    })
    malicious_messages = []
    pwned_messages = []

    for conv in conversations:
        for label, count in conv.get("label_counts", {}).items():
            label_counts[label] = label_counts.get(label, 0) + count

        for msg in conv["messages"]:
            label = _msg_label(msg)
            severity = int(msg.get("classification_severity") or 0)
            sender = msg.get("sender") or "(unknown)"

            if label == "malicious":
                sender_buckets[sender]["malicious_count"] += 1
                sender_buckets[sender]["malicious_severity"] += severity
            elif label == "pwned":
                sender_buckets[sender]["pwned_count"] += 1
                sender_buckets[sender]["pwned_severity"] += severity

            if label in ("malicious", "pwned") and severity > 0:
                text = msg.get("text") or ""
                snippet = text[:140] + ("…" if len(text) > 140 else "")
                row = {
                    "severity": severity,
                    "label": label,
                    "sender": sender,
                    "subject": msg.get("subject") or "(no subject)",
                    "snippet": snippet,
                    "timestamp": msg.get("timestamp") or "",
                    "conversation_id": conv["id"],
                }
                if label == "malicious":
                    malicious_messages.append(row)
                else:
                    pwned_messages.append(row)

    def _top_users(label_key, count_field, severity_field):
        return sorted(
            (
                {
                    "user": user,
                    "count": buckets[count_field],
                    "severity": buckets[severity_field],
                }
                for user, buckets in sender_buckets.items()
                if buckets[count_field] > 0
            ),
            # Primary: severity (intensity), secondary: count (volume).
            # Keeps a single high-severity row above a noise floor of
            # low-severity ones.
            key=lambda u: (u["severity"], u["count"]),
            reverse=True,
        )[:10]

    return {
        "label_counts": label_counts,
        "top_malicious_users": _top_users("malicious", "malicious_count", "malicious_severity"),
        "top_pwned_users": _top_users("pwned", "pwned_count", "pwned_severity"),
        "top_malicious_messages": sorted(
            malicious_messages, key=lambda m: m["severity"], reverse=True
        )[:10],
        "top_pwned_messages": sorted(
            pwned_messages, key=lambda m: m["severity"], reverse=True
        )[:10],
    }

@app.route('/')
@login_required
def index():
    """Main dashboard page"""
    conversations = fetch_conversations()
    stats = get_real_stats(conversations=conversations)
    return render_template('dashboard.html',
                         conversations=conversations,
                         stats=stats,
                         active_nav='dashboard',
                         csrf_token=_session_csrf_token(),
                         flashes=get_flashed_messages(with_categories=True))

@app.route('/chat')
@login_required
def chat():
    """Chat view page"""
    conversations = fetch_conversations()
    return render_template('chat.html',
                         conversations=conversations,
                         active_nav='chat',
                         csrf_token=_session_csrf_token())

@app.route('/visualize')
@login_required
def visualize():
    """3D conversation network view"""
    conversations = fetch_conversations()
    return render_template('visualize.html',
                         conversations=conversations,
                         active_nav='visualize',
                         csrf_token=_session_csrf_token())

@app.route('/api/conversations')
@login_required
def api_conversations():
    """API endpoint for conversations"""
    return jsonify(fetch_conversations())

@app.route('/api/stats')
@login_required
def api_stats():
    """API endpoint for statistics"""
    return jsonify(get_real_stats())

@app.route('/api/conversation/<int:conversation_id>')
@login_required
def api_conversation_detail(conversation_id):
    """API endpoint for specific conversation details"""
    conversations = fetch_conversations()
    if 0 <= conversation_id < len(conversations):
        return jsonify(conversations[conversation_id])
    return jsonify({"error": "Conversation not found"}), 404


@app.route('/user')
@login_required
def user_detail():
    """All messages sent by a single user, with deep links back to their
    conversations. Reached by clicking a user name in any of the top
    leaderboards on the dashboard."""
    email_addr = (request.args.get('email') or '').strip().lower()
    if not email_addr:
        abort(400, description='Missing required `email` query parameter.')

    conversations = fetch_conversations()
    summary = _user_message_summary(conversations, email_addr)

    return render_template(
        'user.html',
        email=email_addr,
        summary=summary,
        active_nav='',
        csrf_token=_session_csrf_token(),
    )


def _user_message_summary(conversations, email_addr):
    """Walk all conversations and pull out the per-user view that the
    /user page renders:
      messages       -- every message *sent by* this user, newest first,
                        each carrying the conversation id, counterparty,
                        classification label + severity, and a snippet.
      label_counts   -- benign/malicious/pwned/pending sent by this user.
      avg_severity   -- mean severity across classified sent messages.
      max_severity   -- the worst single message they sent.
      conversations  -- list of {id, counterparty, message_count,
                        worst_label, max_severity} for every thread they
                        participate in, so the page can also link back
                        to whole threads (which include the *incoming*
                        side that the flat per-message list omits)."""
    sent_messages = []
    label_counts = {k: 0 for k in _LABEL_KEYS}
    severities = []
    participates_in = []

    for conv in conversations:
        if email_addr not in (conv["sender"], conv["receiver"]):
            continue
        counterparty = conv["receiver"] if conv["sender"] == email_addr else conv["sender"]
        participates_in.append({
            "id": conv["id"],
            "counterparty": counterparty,
            "message_count": conv["message_count"],
            "worst_label": conv.get("worst_label", "pending"),
            "max_severity": conv.get("max_severity", 0),
        })

        for msg in conv["messages"]:
            if msg.get("sender") != email_addr:
                continue
            label = _msg_label(msg)
            severity = int(msg.get("classification_severity") or 0)
            label_counts[label] += 1
            if severity > 0:
                severities.append(severity)

            text = msg.get("text") or ""
            snippet = text[:200] + ("…" if len(text) > 200 else "")
            sent_messages.append({
                "conversation_id": conv["id"],
                "counterparty": counterparty,
                "subject": msg.get("subject") or "(no subject)",
                "snippet": snippet,
                "timestamp": msg.get("timestamp") or "",
                "label": label,
                "severity": severity,
            })

    # Newest first matches the rest of the dashboard's ordering.
    sent_messages.sort(key=lambda m: m["timestamp"], reverse=True)
    # Thread cards: surface the most-severe thread first so an operator
    # opens the worst one with one click.
    participates_in.sort(
        key=lambda c: (c["max_severity"], c["message_count"]), reverse=True
    )

    return {
        "messages": sent_messages,
        "label_counts": label_counts,
        "total_sent": len(sent_messages),
        "max_severity": max(severities) if severities else 0,
        "avg_severity": round(sum(severities) / len(severities)) if severities else 0,
        "conversations": participates_in,
    }


@app.route('/reclassify', methods=['POST'])
@login_required
def reclassify():
    """Reset every classifier_emails row back to 'pending' so the
    mail-classifier worker reprocesses them under the current prompt and
    label set on its next poll (~15s). No service restart required."""
    if not _valid_session_csrf():
        abort(403, description='Invalid CSRF token. Please reload the dashboard and try again.')

    try:
        with sqlite3.connect(CLASSIFIER_DB_PATH) as conn:
            cursor = conn.execute(
                """
                UPDATE classifier_emails
                SET classification_status   = 'pending',
                    classification_label    = NULL,
                    classification_reason   = NULL,
                    classification_severity = NULL,
                    classification_attempts = 0,
                    last_error              = NULL,
                    classified_at           = NULL
                """
            )
            reset_count = cursor.rowcount
        flash(f"Queued {reset_count} messages for reclassification. "
              f"The worker will reprocess them on its next poll (~15s).",
              "success")
    except sqlite3.Error as exc:
        print(f"Reclassify failed: {exc}")
        flash(f"Reclassify failed: {exc}", "error")

    return redirect(url_for('index'))


# ── Auth routes ──────────────────────────────────────────────────────────────

def _safe_next(target):
    # Only honour ?next= values that point back inside our SCRIPT_NAME prefix.
    # Rejects:
    #   - empty / non-path values
    #   - protocol-relative URLs (//evil.example/...)
    #   - absolute paths outside our prefix (e.g. / -> Roundcube,
    #     /signup -> signup-api), which would otherwise let an attacker
    #     bounce a logged-in operator to a different vhost service.
    if not target or not target.startswith('/') or target.startswith('//'):
        return url_for('index')
    prefix = SCRIPT_NAME or ''
    if prefix and not (target == prefix or target.startswith(prefix + '/')):
        return url_for('index')
    return target


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login form (GET) and submission (POST)."""
    if request.method == 'GET':
        if session.get('authed'):
            return redirect(_safe_next(request.args.get('next')))
        return _render_login(error=None, next_target=request.args.get('next', ''))

    if not _valid_csrf():
        abort(403, description='Invalid request. Please reload the login page and try again.')

    password = request.form.get('password', '')
    next_target = request.form.get('next', '')

    if not password or not hmac.compare_digest(password, DASHBOARD_PASSWORD):
        return _render_login(error='Incorrect password.', next_target=next_target, status=401)

    session.clear()
    session['authed'] = True
    session.permanent = True
    return redirect(_safe_next(next_target))


@app.route('/logout', methods=['GET', 'POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/healthz')
def healthz():
    """Liveness probe for the docker healthcheck. Intentionally unauthenticated."""
    return ('ok', 200, {'Content-Type': 'text/plain; charset=utf-8'})


if __name__ == '__main__':
    # Local-dev entrypoint only. Production uses gunicorn (see Dockerfile),
    # which imports `app:app` directly and never executes this block. debug=False
    # because Flask's debugger is dangerous on any reachable interface even
    # when PIN-protected.
    app.run(host='0.0.0.0', port=5000, debug=False)
