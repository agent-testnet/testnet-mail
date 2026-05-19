import imaplib

from mail_classifier.classification import ClassificationResult
from mail_classifier.config import PROVIDER_GEMINI, MailAccount, Settings
from mail_classifier.db import ClassificationRepository, EmailRecord
from mail_classifier.imap_sync import IMAPEmailSync
from mail_classifier.service import MailClassifierService


class FakeSyncer:
    def __init__(self, repository: ClassificationRepository) -> None:
        self.repository = repository

    def sync_accounts(self, accounts: tuple[MailAccount, ...]):
        inserted = 0
        for index, account in enumerate(accounts, start=1):
            inserted += int(
                self.repository.insert_email(
                    EmailRecord(
                        account_email=account.email,
                        mailbox="INBOX",
                        uid=str(index),
                        message_id=f"<{index}@example.test>",
                        sender="Security Team <security@example.test>",
                        recipient=account.email,
                        subject="Reset your wallet immediately",
                        body_text="Click this urgent link to keep access to your funds.",
                        received_at="2026-05-14T12:00:00+00:00",
                    )
                )
            )
        return type("SyncResult", (), {"inserted": inserted, "skipped": 0})()


class FakeClassifierClient:
    model_name = "fake-classifier"

    def __init__(self) -> None:
        # Record the last set of kwargs the service handed us so tests can
        # assert prior-thread context was wired through correctly.
        self.last_call: dict = {}

    def classify_email(self, **kwargs) -> ClassificationResult:
        self.last_call = kwargs
        return ClassificationResult(
            label="malicious",
            reason="Urgent credential-style request with suspicious language.",
            severity=85,
        )


def test_service_run_once_syncs_and_classifies(tmp_path):
    repository = ClassificationRepository(str(tmp_path / "classifier.db"))
    settings = Settings(
        db_path=str(tmp_path / "classifier.db"),
        heartbeat_path=str(tmp_path / "heartbeat"),
        mailserver_host="mailserver",
        mailserver_port=143,
        mailbox="INBOX",
        poll_interval_seconds=1,
        batch_size=10,
        provider=PROVIDER_GEMINI,
        api_key="test-key",
        model_name="gemini-2.5-flash-lite",
        accounts=(MailAccount("alice@gmail.com", "alice-password"),),
    )
    service = MailClassifierService(
        settings=settings,
        repository=repository,
        syncer=FakeSyncer(repository),
        classifier_client=FakeClassifierClient(),
    )

    result = service.run_once()

    assert result == {"inserted": 1, "skipped": 0, "classified": 1}
    stored = repository.fetch_email(1)
    assert stored is not None
    assert stored["classification_label"] == "malicious"
    assert stored["classification_model"] == "fake-classifier"
    assert tmp_path.joinpath("heartbeat").exists()


def test_imap_sync_skips_auth_failures_and_continues(monkeypatch, tmp_path):
    repository = ClassificationRepository(str(tmp_path / "classifier.db"))
    syncer = IMAPEmailSync(
        repository=repository,
        mailserver_host="mailserver",
        mailserver_port=143,
        mailbox="INBOX",
    )
    accounts = (
        MailAccount("missing@gmail.com", "wrong-password"),
        MailAccount("alice@gmail.com", "alice-password"),
    )
    auth_error = imaplib.IMAP4.error

    class FakeIMAP4:
        def __init__(self, host: str, port: int) -> None:
            self.host = host
            self.port = port
            self.email = ""

        def login(self, email: str, password: str) -> None:
            self.email = email
            if email == "missing@gmail.com":
                raise auth_error(b"[AUTHENTICATIONFAILED] Authentication failed.")

        def select(self, mailbox: str):
            return "OK", [b""]

        def uid(self, command: str, *_args: str):
            if command == "search":
                return "OK", [b"100"]
            if command == "fetch":
                return "OK", [(b"100 (RFC822 {120})", SAMPLE_MESSAGE)]
            raise AssertionError(f"Unexpected command: {command}")

        def logout(self) -> None:
            return None

    monkeypatch.setattr("mail_classifier.imap_sync.imaplib.IMAP4", FakeIMAP4)

    result = syncer.sync_accounts(accounts)

    assert result.inserted == 1
    assert result.skipped == 0
    stored = repository.fetch_email(1)
    assert stored is not None
    assert stored["account_email"] == "alice@gmail.com"


def test_service_passes_prior_thread_context_to_classifier(tmp_path):
    """Once a prior message between the same two parties is classified, the
    next pending message for that pair must be classified WITH that prior
    row passed in as `prior_messages`. This is what enables `pwned` to be
    detected at all."""
    repository = ClassificationRepository(str(tmp_path / "classifier.db"))

    repository.insert_email(
        EmailRecord(
            account_email="alice@gmail.com",
            mailbox="INBOX",
            uid="m1",
            message_id="<m1@example.test>",
            sender="mallory@evil.example",
            recipient="alice@gmail.com",
            subject="Reset your wallet",
            body_text="Send credentials.",
            received_at="2026-05-14T12:00:00+00:00",
        )
    )
    first_pending = repository.pending_emails(limit=10)[0]
    repository.save_classification(
        first_pending.id, "malicious", "Phishing.", "seed", severity=80
    )

    repository.insert_email(
        EmailRecord(
            account_email="alice@gmail.com",
            mailbox="INBOX",
            uid="m2",
            message_id="<m2@example.test>",
            sender="mallory@evil.example",
            recipient="alice@gmail.com",
            subject="Re: Reset your wallet",
            body_text="One more thing...",
            received_at="2026-05-14T13:00:00+00:00",
        )
    )

    fake_client = FakeClassifierClient()
    settings = Settings(
        db_path=str(tmp_path / "classifier.db"),
        heartbeat_path=str(tmp_path / "heartbeat"),
        mailserver_host="mailserver",
        mailserver_port=143,
        mailbox="INBOX",
        poll_interval_seconds=1,
        batch_size=10,
        provider=PROVIDER_GEMINI,
        api_key="test-key",
        model_name="gemini-2.5-flash-lite",
        accounts=(MailAccount("alice@gmail.com", "alice-password"),),
    )

    # Use a no-op syncer so we classify exactly the row we pre-seeded.
    class NoopSyncer:
        def sync_accounts(self, accounts):
            return type("SyncResult", (), {"inserted": 0, "skipped": 0})()

    service = MailClassifierService(
        settings=settings,
        repository=repository,
        syncer=NoopSyncer(),
        classifier_client=fake_client,
    )
    service.run_once()

    prior = fake_client.last_call.get("prior_messages")
    assert prior is not None and len(prior) == 1
    assert prior[0].label == "malicious"
    assert prior[0].sender == "mallory@evil.example"
    assert prior[0].subject == "Reset your wallet"


SAMPLE_MESSAGE = (
    b"From: Bob <bob@gmail.com>\r\n"
    b"To: Alice <alice@gmail.com>\r\n"
    b"Subject: Hello\r\n"
    b"Date: Thu, 14 May 2026 12:00:00 +0000\r\n"
    b"Message-ID: <100@example.test>\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Hi Alice.\r\n"
)
