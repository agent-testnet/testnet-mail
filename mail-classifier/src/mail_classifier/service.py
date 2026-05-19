from __future__ import annotations

from pathlib import Path
from time import sleep

from .classification import ClassifierClient
from .config import Settings
from .db import ClassificationRepository
from .imap_sync import IMAPEmailSync


class MailClassifierService:
    def __init__(
        self,
        settings: Settings,
        repository: ClassificationRepository,
        syncer: IMAPEmailSync,
        classifier_client: ClassifierClient,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.syncer = syncer
        self.classifier_client = classifier_client

    def run_forever(self) -> None:
        while True:
            self.run_once()
            sleep(self.settings.poll_interval_seconds)

    def run_once(self) -> dict[str, int]:
        sync_result = self.syncer.sync_accounts(self.settings.accounts)
        classified = self.classify_pending()
        self._write_heartbeat()
        return {
            "inserted": sync_result.inserted,
            "skipped": sync_result.skipped,
            "classified": classified,
        }

    def classify_pending(self) -> int:
        processed = 0
        for email_row in self.repository.pending_emails(self.settings.batch_size):
            try:
                # Give the LLM the prior thread between these two parties so
                # it can identify `pwned` replies to earlier `malicious`
                # messages. Empty list on first contact, which is fine —
                # build_prompt simply omits the context block.
                prior_messages = self.repository.prior_thread(
                    sender=email_row.sender,
                    recipient=email_row.recipient,
                    exclude_id=email_row.id,
                )
                result = self.classifier_client.classify_email(
                    sender=email_row.sender,
                    recipient=email_row.recipient,
                    subject=email_row.subject,
                    body_text=email_row.body_text,
                    prior_messages=prior_messages,
                )
                self.repository.save_classification(
                    email_row.id,
                    result.label,
                    result.reason,
                    self.classifier_client.model_name,
                    result.severity,
                )
                processed += 1
            except Exception as exc:
                self.repository.record_classification_error(email_row.id, str(exc))
        return processed

    def _write_heartbeat(self) -> None:
        heartbeat = Path(self.settings.heartbeat_path)
        heartbeat.parent.mkdir(parents=True, exist_ok=True)
        heartbeat.write_text("ok\n", encoding="utf-8")
