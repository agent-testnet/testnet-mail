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
SCRIPT_NAME = os.getenv('SCRIPT_NAME', '/dashboard')

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
    SESSION_COOKIE_SECURE=True,
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
        secure=True,
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
                SELECT account_email, message_id, classification_status, classification_label
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
        }
        for row in rows
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

                # Sort messages by timestamp to maintain conversation order
                sorted_messages = sorted(conv_data["messages"], key=lambda x: x["timestamp"])
                print(f"Conversation {conv_id}: {participants[0]} ↔ {participants[1]}")
                for i, msg in enumerate(sorted_messages):
                    print(f"  Message {i+1}: {msg['sender']} @ {msg['timestamp']}")
                result.append({
                    "id": conv_id,
                    "sender": participants[0],
                    "receiver": participants[1],
                    "messages": sorted_messages,
                    "message_count": len(sorted_messages)
                })
    
    return result

def get_real_stats():
    """Generate statistics from real data"""
    conversations = fetch_conversations()
    
    user_message_count = defaultdict(int)
    for conv in conversations:
        for msg in conv["messages"]:
            user_message_count[msg["sender"]] += 1
    
    # Get conversation frequency by day
    freq_by_day = defaultdict(int)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    
    for conv in conversations:
        for msg in conv["messages"]:
            try:
                dt = datetime.fromisoformat(msg["timestamp"])
                day_name = days[dt.weekday()]
                freq_by_day[day_name] += 1
            except:
                pass
    
    return {
        "total_users": len(TEST_ACCOUNTS),
        "total_conversations": len(conversations),
        "total_messages": sum(c["message_count"] for c in conversations),
        "active_today": len([u for u in user_message_count if user_message_count[u] > 0]),
        "message_distribution": [
            {"user": user, "count": count}
            for user, count in sorted(user_message_count.items())
        ],
        "conversation_frequency": [
            {"day": day, "count": freq_by_day.get(day, 0)}
            for day in days
        ]
    }

@app.route('/')
@login_required
def index():
    """Main dashboard page"""
    conversations = fetch_conversations()
    stats = get_real_stats()
    return render_template('dashboard.html',
                         conversations=conversations,
                         stats=stats,
                         active_nav='dashboard')

@app.route('/chat')
@login_required
def chat():
    """Chat view page"""
    conversations = fetch_conversations()
    return render_template('chat.html', conversations=conversations, active_nav='chat')

@app.route('/visualize')
@login_required
def visualize():
    """3D conversation network view"""
    conversations = fetch_conversations()
    return render_template('visualize.html', conversations=conversations, active_nav='visualize')

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
