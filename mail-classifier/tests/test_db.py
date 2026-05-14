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
    )

    stored = repo.fetch_email(pending[0].id)
    assert stored is not None
    assert stored["classification_status"] == "classified"
    assert stored["classification_label"] == "malicious"
    assert stored["classification_reason"] == "Requests credentials with urgency."


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
