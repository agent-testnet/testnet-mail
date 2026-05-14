import imaplib

from mail_classifier.config import MailAccount, Settings
from mail_classifier.db import ClassificationRepository, EmailRecord
from mail_classifier.gemini import ClassificationResult
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


class FakeGeminiClient:
    model_name = "fake-gemini"

    def classify_email(self, **_: str) -> ClassificationResult:
        return ClassificationResult(
            label="malicious",
            reason="Urgent credential-style request with suspicious language.",
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
        gemini_api_key="test-key",
        gemini_model="gemini-2.0-flash",
        accounts=(MailAccount("alice@gmail.com", "alice-password"),),
    )
    service = MailClassifierService(
        settings=settings,
        repository=repository,
        syncer=FakeSyncer(repository),
        gemini_client=FakeGeminiClient(),
    )

    result = service.run_once()

    assert result == {"inserted": 1, "skipped": 0, "classified": 1}
    stored = repository.fetch_email(1)
    assert stored is not None
    assert stored["classification_label"] == "malicious"
    assert stored["classification_model"] == "fake-gemini"
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
