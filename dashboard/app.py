import os
from flask import Flask, render_template, jsonify
from datetime import datetime, timedelta
import imaplib
import email
from email.header import decode_header
from collections import defaultdict
import re
from email.utils import parsedate_to_datetime
import time

app = Flask(__name__)

# Configuration
MAIL_DOMAIN = os.getenv('MAIL_DOMAIN', 'gmail.com')
MAILSERVER_HOST = os.getenv('MAILSERVER_HOST', 'mailserver')
MAILSERVER_PORT = int(os.getenv('MAILSERVER_PORT', '143'))
TEST_ACCOUNTS = [
    {"email": f"alice@{MAIL_DOMAIN}", "password": "alice-password"},
    {"email": f"bob@{MAIL_DOMAIN}", "password": "bob-password"},
    {"email": f"charlie@{MAIL_DOMAIN}", "password": "charlie-password"},
    {"email": f"diana@{MAIL_DOMAIN}", "password": "diana-password"},
]

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

def fetch_conversations():
    """Fetch real conversations from mailserver"""
    conversations = defaultdict(lambda: {"messages": [], "participants": set(), "message_ids": set()})
    conv_id = 0
    conv_map = {}
    
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
                        
                        conversations[conv_idx]["messages"].append({
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
    
    # Format conversations - ensure we always have valid participants
    result = []
    for conv_id, conv_data in conversations.items():
        if conv_data["messages"]:
            participants = sorted(list(conv_data["participants"]))
            # Only include conversations with at least 2 participants
            if len(participants) >= 2:
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
def index():
    """Main dashboard page"""
    conversations = fetch_conversations()
    stats = get_real_stats()
    return render_template('dashboard.html',
                         conversations=conversations,
                         stats=stats,
                         active_nav='dashboard')

@app.route('/chat')
def chat():
    """Chat view page"""
    conversations = fetch_conversations()
    return render_template('chat.html', conversations=conversations, active_nav='chat')

@app.route('/visualize')
def visualize():
    """3D conversation network view"""
    conversations = fetch_conversations()
    return render_template('visualize.html', conversations=conversations, active_nav='visualize')

@app.route('/api/conversations')
def api_conversations():
    """API endpoint for conversations"""
    return jsonify(fetch_conversations())

@app.route('/api/stats')
def api_stats():
    """API endpoint for statistics"""
    return jsonify(get_real_stats())

@app.route('/api/conversation/<int:conversation_id>')
def api_conversation_detail(conversation_id):
    """API endpoint for specific conversation details"""
    conversations = fetch_conversations()
    if 0 <= conversation_id < len(conversations):
        return jsonify(conversations[conversation_id])
    return jsonify({"error": "Conversation not found"}), 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
