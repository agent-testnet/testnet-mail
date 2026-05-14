from __future__ import annotations

import logging

from .config import load_settings
from .db import ClassificationRepository
from .gemini import GeminiClient
from .imap_sync import IMAPEmailSync
from .service import MailClassifierService


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = load_settings()
    repository = ClassificationRepository(settings.db_path)
    syncer = IMAPEmailSync(
        repository=repository,
        mailserver_host=settings.mailserver_host,
        mailserver_port=settings.mailserver_port,
        mailbox=settings.mailbox,
    )
    gemini_client = GeminiClient(
        api_key=settings.gemini_api_key,
        model_name=settings.gemini_model,
    )
    service = MailClassifierService(settings, repository, syncer, gemini_client)
    service.run_forever()


if __name__ == "__main__":
    main()

