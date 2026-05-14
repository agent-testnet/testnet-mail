from mail_classifier.db import ClassificationRepository, EmailRecord


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

