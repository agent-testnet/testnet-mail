"""Smoke tests for the dashboard's reclassify endpoint and CSRF wiring.

These run without the mailserver up: we point CLASSIFIER_DB_PATH at a
tmp_path SQLite file before importing app.py so the live Roundcube DB is
never touched, and we monkeypatch fetch_conversations() to skip the IMAP
roundtrips in routes that pull conversations.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import sys

import pytest


@pytest.fixture
def dashboard_app(tmp_path, monkeypatch):
    """Boot a clean copy of dashboard.app against a tmp SQLite DB.

    Importing dashboard.app reads env vars at module-import time
    (DASHBOARD_PASSWORD, DASHBOARD_SECRET_KEY, CLASSIFIER_DB_PATH), so we
    set them, drop any cached module, and re-import."""
    db_path = tmp_path / "classifier.db"

    # Seed a single 'classified' row so reclassify has something to reset.
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE classifier_emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_email TEXT NOT NULL,
                mailbox TEXT NOT NULL,
                uid TEXT NOT NULL,
                message_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                body_text TEXT NOT NULL,
                received_at TEXT NOT NULL,
                classification_status TEXT NOT NULL DEFAULT 'pending',
                classification_label TEXT,
                classification_reason TEXT,
                classification_severity INTEGER,
                classified_at TEXT,
                classification_model TEXT,
                classification_attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(account_email, mailbox, uid)
            );
            INSERT INTO classifier_emails
                (account_email, mailbox, uid, message_id, sender, recipient,
                 subject, body_text, received_at, classification_status,
                 classification_label, classification_reason,
                 classification_severity, classification_attempts)
            VALUES ('alice@gmail.com', 'INBOX', '1', '<1>',
                    'mallory@evil.example', 'alice@gmail.com',
                    'Phish', 'Send creds.', '2026-05-14T12:00:00+00:00',
                    'classified', 'malicious', 'Phishing.', 85, 1);
            """
        )

    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-password")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-secret-key-deadbeef")
    monkeypatch.setenv("DASHBOARD_ALLOW_PLAIN_HTTP", "1")
    monkeypatch.setenv("CLASSIFIER_DB_PATH", str(db_path))

    # Make `dashboard` importable in tests regardless of where pytest was
    # launched from. Resolved relative to this test file.
    dashboard_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if dashboard_root not in sys.path:
        sys.path.insert(0, dashboard_root)

    sys.modules.pop("app", None)
    module = importlib.import_module("app")

    # Replace IMAP-touching helpers with stubs so /reclassify (which
    # redirects to /) doesn't try to hit a real mailserver during the
    # follow_redirects=True flow some tests use.
    monkeypatch.setattr(module, "fetch_conversations", lambda: [])
    monkeypatch.setattr(module, "get_real_stats", lambda conversations=None: {
        "total_users": 0, "total_conversations": 0, "total_messages": 0,
        "active_today": 0,
        "message_distribution": [],
        "conversation_frequency": [],
        "label_counts": {"benign": 0, "malicious": 0, "pwned": 0, "pending": 0},
        "top_malicious_users": [],
        "top_pwned_users": [],
        "top_malicious_messages": [],
        "top_pwned_messages": [],
    })

    yield module, db_path


def _login(client, password="test-password"):
    """Walk through the login flow so the test client has an authed
    session cookie + the per-session CSRF token to embed in subsequent
    POSTs."""
    resp = client.get("/login")
    assert resp.status_code == 200
    # The login-flow CSRF cookie is path-scoped to /login (see
    # _csrf_cookie_path in app.py) so the test client lookup must specify
    # that path or it returns None.
    cookie = client.get_cookie("_csrf", path="/login")
    assert cookie is not None, "expected _csrf cookie attached to GET /login"
    resp = client.post(
        "/login",
        data={"password": password, "_csrf": cookie.value},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.data


def test_reclassify_requires_login(dashboard_app):
    module, _ = dashboard_app
    client = module.app.test_client()

    resp = client.post("/reclassify")
    # login_required wraps the view in a redirect to the login page.
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_reclassify_rejects_missing_csrf(dashboard_app):
    module, _ = dashboard_app
    client = module.app.test_client()
    _login(client)

    resp = client.post("/reclassify", data={})
    assert resp.status_code == 403


def test_reclassify_resets_classified_rows_to_pending(dashboard_app):
    module, db_path = dashboard_app
    client = module.app.test_client()
    _login(client)

    # Ask the dashboard GET to mint the session CSRF token, then read it
    # out of the session for the POST.
    client.get("/")
    with client.session_transaction() as sess:
        csrf = sess["csrf"]

    resp = client.post(
        "/reclassify",
        data={"_csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM classifier_emails WHERE id = 1").fetchone()

    assert row["classification_status"] == "pending"
    assert row["classification_label"] is None
    assert row["classification_reason"] is None
    assert row["classification_severity"] is None
    assert row["classification_attempts"] == 0
    assert row["last_error"] is None
    assert row["classified_at"] is None


def test_get_reclassify_is_method_not_allowed(dashboard_app):
    module, _ = dashboard_app
    client = module.app.test_client()
    _login(client)

    resp = client.get("/reclassify")
    assert resp.status_code == 405


# ---------------------------------------------------------------------------
# /user (per-user message view)
# ---------------------------------------------------------------------------


def _sample_conversations():
    """Two conversations centred on alice@gmail.com so the summary
    helper has something meaningful to aggregate: one thread with the
    attacker (malicious + pwned + malicious-followup) and one routine
    benign thread with a coworker."""
    return [
        {
            "id": 0,
            "sender": "mallory@evil.example",
            "receiver": "alice@gmail.com",
            "message_count": 3,
            "worst_label": "malicious",
            "max_severity": 95,
            "messages": [
                {
                    "sender": "mallory@evil.example",
                    "subject": "urgent invoice",
                    "text": "open the attached PDF",
                    "timestamp": "2026-05-14T09:00:00+00:00",
                    "classification_label": "malicious",
                    "classification_severity": 80,
                },
                {
                    "sender": "alice@gmail.com",
                    "subject": "re: urgent invoice",
                    "text": "opened it, what now?",
                    "timestamp": "2026-05-14T10:00:00+00:00",
                    "classification_label": "pwned",
                    "classification_severity": 90,
                },
                {
                    "sender": "mallory@evil.example",
                    "subject": "re: re: urgent invoice",
                    "text": "wire 5k to acct 1234",
                    "timestamp": "2026-05-14T11:00:00+00:00",
                    "classification_label": "malicious",
                    "classification_severity": 95,
                },
            ],
        },
        {
            "id": 1,
            "sender": "alice@gmail.com",
            "receiver": "bob@corp.example",
            "message_count": 1,
            "worst_label": "benign",
            "max_severity": 10,
            "messages": [
                {
                    "sender": "alice@gmail.com",
                    "subject": "lunch tomorrow?",
                    "text": "noon at the usual place",
                    "timestamp": "2026-05-13T12:00:00+00:00",
                    "classification_label": "benign",
                    "classification_severity": 10,
                },
            ],
        },
    ]


def test_user_detail_requires_login(dashboard_app):
    module, _ = dashboard_app
    client = module.app.test_client()

    resp = client.get("/user?email=alice@gmail.com")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_user_detail_rejects_missing_email(dashboard_app):
    module, _ = dashboard_app
    client = module.app.test_client()
    _login(client)

    resp = client.get("/user")
    assert resp.status_code == 400


def test_user_detail_aggregates_sent_messages_for_victim(dashboard_app, monkeypatch):
    """alice sent one pwned message + one benign message across two
    threads. The helper should bucket counts correctly, expose her max
    severity (90, from the pwned reply), and list both threads she
    participates in -- including the one she didn't start."""
    module, _ = dashboard_app
    monkeypatch.setattr(module, "fetch_conversations", _sample_conversations)

    summary = module._user_message_summary(_sample_conversations(), "alice@gmail.com")

    assert summary["total_sent"] == 2
    assert summary["label_counts"] == {
        "benign": 1, "malicious": 0, "pwned": 1, "pending": 0,
    }
    assert summary["max_severity"] == 90
    # Newest sent message first.
    assert summary["messages"][0]["subject"] == "re: urgent invoice"
    assert summary["messages"][0]["label"] == "pwned"
    assert summary["messages"][1]["subject"] == "lunch tomorrow?"
    # alice shows up on both threads even though she only originated one.
    thread_ids = {t["id"] for t in summary["conversations"]}
    assert thread_ids == {0, 1}


def test_user_detail_aggregates_sent_messages_for_attacker(dashboard_app, monkeypatch):
    """And the same helper applied to the attacker side: only mallory's
    two outgoing malicious messages count, not alice's pwned reply that
    sits between them in the thread."""
    module, _ = dashboard_app
    monkeypatch.setattr(module, "fetch_conversations", _sample_conversations)

    summary = module._user_message_summary(
        _sample_conversations(), "mallory@evil.example"
    )

    assert summary["total_sent"] == 2
    assert summary["label_counts"]["malicious"] == 2
    assert summary["label_counts"]["pwned"] == 0
    assert summary["max_severity"] == 95


def test_user_detail_route_renders_for_authed_user(dashboard_app, monkeypatch):
    module, _ = dashboard_app
    monkeypatch.setattr(module, "fetch_conversations", _sample_conversations)
    client = module.app.test_client()
    _login(client)

    resp = client.get("/user?email=alice@gmail.com")
    assert resp.status_code == 200
    body = resp.data.decode()
    # Header reflects the user we asked for, plus the per-label badge
    # row computed from the sample data above.
    assert "alice@gmail.com" in body
    assert "SENT MESSAGES (2)" in body
    # Newest-first ordering means the pwned reply renders before the
    # routine benign one.
    pwned_idx = body.find("re: urgent invoice")
    benign_idx = body.find("lunch tomorrow?")
    assert 0 < pwned_idx < benign_idx
