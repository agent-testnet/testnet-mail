import sqlite3

from mail_classifier.db import (
    MAX_CLASSIFICATION_ATTEMPTS,
    ClassificationRepository,
    EmailRecord,
)


def _make_record(uid: str = "1") -> EmailRecord:
    return EmailRecord(
        account_email="alice@gmail.com",
        mailbox="INBOX",
        uid=uid,
        message_id=f"<message-{uid}>",
        sender="Mallory <mallory@evil.example>",
        recipient="alice@gmail.com",
        subject="Urgent password reset",
        body_text="Send me your password immediately.",
        received_at="2026-05-14T12:00:00+00:00",
    )


def test_repository_saves_and_classifies_email(tmp_path):
    repo = ClassificationRepository(str(tmp_path / "classifier.db"))
    inserted = repo.insert_email(
        EmailRecord(
            account_email="alice@gmail.com",
            mailbox="INBOX",
            uid="42",
            message_id="<message-42>",
            sender="Mallory <mallory@evil.example>",
            recipient="alice@gmail.com",
            subject="Urgent password reset",
            body_text="Send me your password immediately.",
            received_at="2026-05-14T12:00:00+00:00",
        )
    )

    assert inserted is True
    pending = repo.pending_emails(limit=10)
    assert len(pending) == 1
    assert pending[0].subject == "Urgent password reset"

    repo.save_classification(
        pending[0].id,
        label="malicious",
        reason="Requests credentials with urgency.",
        model_name="gemini-2.0-flash",
        severity=85,
    )

    stored = repo.fetch_email(pending[0].id)
    assert stored is not None
    assert stored["classification_status"] == "classified"
    assert stored["classification_label"] == "malicious"
    assert stored["classification_reason"] == "Requests credentials with urgency."
    assert stored["classification_severity"] == 85


def test_repeated_errors_eventually_drop_message_from_pending_queue(tmp_path):
    """A row that hits MAX_CLASSIFICATION_ATTEMPTS errors must move out of
    'pending' so the polling loop stops handing it back to Gemini forever."""
    repo = ClassificationRepository(str(tmp_path / "classifier.db"))
    repo.insert_email(_make_record(uid="bad"))
    email_id = repo.pending_emails(limit=10)[0].id

    for _ in range(MAX_CLASSIFICATION_ATTEMPTS):
        repo.record_classification_error(email_id, "Gemini said nope")

    assert repo.pending_emails(limit=10) == []
    stored = repo.fetch_email(email_id)
    assert stored is not None
    assert stored["classification_status"] == "failed"
    assert stored["classification_attempts"] == MAX_CLASSIFICATION_ATTEMPTS
    assert stored["last_error"] == "Gemini said nope"


def test_transient_errors_keep_message_in_pending_queue(tmp_path):
    repo = ClassificationRepository(str(tmp_path / "classifier.db"))
    repo.insert_email(_make_record(uid="flaky"))
    email_id = repo.pending_emails(limit=10)[0].id

    repo.record_classification_error(email_id, "Network blip")

    pending = repo.pending_emails(limit=10)
    assert len(pending) == 1
    assert pending[0].id == email_id


def _insert_classified(
    repo: ClassificationRepository,
    *,
    uid: str,
    account_email: str,
    sender: str,
    recipient: str,
    subject: str,
    body_text: str,
    received_at: str,
    label: str,
) -> int:
    """Insert + immediately classify a row so it shows up in prior_thread()
    queries (which filter on classification_status = 'classified').

    We resolve the new row's id via the unique (account_email, mailbox, uid)
    constraint rather than pending_emails(), so the helper still picks the
    correct row when there are other unrelated pending rows in the table."""
    repo.insert_email(
        EmailRecord(
            account_email=account_email,
            mailbox="INBOX",
            uid=uid,
            message_id=f"<{uid}@example.test>",
            sender=sender,
            recipient=recipient,
            subject=subject,
            body_text=body_text,
            received_at=received_at,
        )
    )
    with repo._connect() as conn:
        row = conn.execute(
            "SELECT id FROM classifier_emails "
            "WHERE account_email = ? AND mailbox = ? AND uid = ?",
            (account_email, "INBOX", uid),
        ).fetchone()
    assert row is not None, f"failed to look up inserted row uid={uid}"
    row_id = row["id"]
    repo.save_classification(row_id, label, "seeded", "test-model", severity=50)
    return row_id


def test_prior_thread_returns_symmetric_pair_oldest_first(tmp_path):
    repo = ClassificationRepository(str(tmp_path / "classifier.db"))

    # Two threads in flight: alice<->mallory (the one we'll query) and an
    # unrelated alice<->bob thread that must NOT leak into the result.
    _insert_classified(
        repo,
        uid="m1",
        account_email="alice@gmail.com",
        sender="mallory@evil.example",
        recipient="alice@gmail.com",
        subject="Reset your wallet",
        body_text="Send credentials.",
        received_at="2026-05-14T12:00:00+00:00",
        label="malicious",
    )
    _insert_classified(
        repo,
        uid="m2",
        account_email="mallory@evil.example",
        sender="alice@gmail.com",
        recipient="mallory@evil.example",
        subject="Re: Reset your wallet",
        body_text="Here is my password.",
        received_at="2026-05-14T12:30:00+00:00",
        label="pwned",
    )
    _insert_classified(
        repo,
        uid="b1",
        account_email="alice@gmail.com",
        sender="bob@gmail.com",
        recipient="alice@gmail.com",
        subject="Lunch?",
        body_text="Tomorrow at 12:30.",
        received_at="2026-05-14T11:00:00+00:00",
        label="benign",
    )

    # New incoming reply on the alice<->mallory thread; prior_thread should
    # surface both earlier messages between those two parties (either
    # direction) and skip the alice<->bob row entirely.
    new_id = _insert_classified(
        repo,
        uid="m3",
        account_email="alice@gmail.com",
        sender="mallory@evil.example",
        recipient="alice@gmail.com",
        subject="Re: Re: Reset your wallet",
        body_text="Thanks, one more thing...",
        received_at="2026-05-14T13:00:00+00:00",
        label="malicious",
    )

    prior = repo.prior_thread(
        sender="mallory@evil.example",
        recipient="alice@gmail.com",
        exclude_id=new_id,
    )

    # Oldest first; exclude_id row not present; symmetric pair pulled in
    # regardless of direction; unrelated alice<->bob row filtered out.
    assert [(p.sender, p.recipient, p.label) for p in prior] == [
        ("mallory@evil.example", "alice@gmail.com", "malicious"),
        ("alice@gmail.com", "mallory@evil.example", "pwned"),
    ]


def test_prior_thread_skips_unclassified_rows(tmp_path):
    repo = ClassificationRepository(str(tmp_path / "classifier.db"))
    # Prior message exists but is still 'pending' — must not show up as
    # context (would feed an empty label to the LLM otherwise).
    repo.insert_email(
        EmailRecord(
            account_email="alice@gmail.com",
            mailbox="INBOX",
            uid="pending-1",
            message_id="<pending-1@example.test>",
            sender="mallory@evil.example",
            recipient="alice@gmail.com",
            subject="First contact",
            body_text="Hi.",
            received_at="2026-05-14T10:00:00+00:00",
        )
    )

    new_id = _insert_classified(
        repo,
        uid="reply",
        account_email="alice@gmail.com",
        sender="mallory@evil.example",
        recipient="alice@gmail.com",
        subject="Re: First contact",
        body_text="Anything?",
        received_at="2026-05-14T11:00:00+00:00",
        label="malicious",
    )

    assert repo.prior_thread(
        sender="mallory@evil.example",
        recipient="alice@gmail.com",
        exclude_id=new_id,
    ) == []


def test_initialize_adds_severity_column_to_existing_table(tmp_path):
    """Simulates an old deploy whose classifier_emails table was created
    before classification_severity existed. Booting the new code must add
    the column in place (no DROP/recreate) so live rows survive."""
    db_path = str(tmp_path / "classifier.db")

    # Pre-create the table WITHOUT classification_severity, mirroring the
    # pre-feature schema. Everything else stays identical to today's
    # initialize() so the migration is the only thing under test.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
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
                classified_at TEXT,
                classification_model TEXT,
                classification_attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(account_email, mailbox, uid)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO classifier_emails
                (account_email, mailbox, uid, message_id, sender, recipient,
                 subject, body_text, received_at, classification_status,
                 classification_label, classification_reason)
            VALUES ('alice@gmail.com', 'INBOX', 'legacy', '<legacy>',
                    'mallory@evil.example', 'alice@gmail.com',
                    'old phish', 'pre-feature row', '2026-05-01T00:00:00+00:00',
                    'classified', 'malicious', 'Old reason.')
            """
        )

    # Booting the new ClassificationRepository must idempotently add the
    # missing column AND preserve the pre-existing legacy row.
    repo = ClassificationRepository(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(classifier_emails)").fetchall()}
        assert "classification_severity" in columns
        rows = conn.execute("SELECT classification_label, classification_severity FROM classifier_emails").fetchall()

    assert len(rows) == 1
    assert rows[0]["classification_label"] == "malicious"
    assert rows[0]["classification_severity"] is None  # backfilled to NULL

    # And the second boot is a no-op: re-calling initialize doesn't crash
    # with "duplicate column" and the column is still there.
    repo.initialize()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(classifier_emails)").fetchall()}
    assert "classification_severity" in columns
