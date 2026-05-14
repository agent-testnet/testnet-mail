from __future__ import annotations

import logging

from .classification import ClassifierClient
from .config import PROVIDER_GEMINI, PROVIDER_OPENROUTER, Settings, load_settings
from .db import ClassificationRepository
from .gemini import GeminiClient
from .imap_sync import IMAPEmailSync
from .openrouter import OpenRouterClient
from .service import MailClassifierService

logger = logging.getLogger(__name__)


def _build_classifier_client(settings: Settings) -> ClassifierClient:
    if settings.provider == PROVIDER_GEMINI:
        return GeminiClient(api_key=settings.api_key, model_name=settings.model_name)
    if settings.provider == PROVIDER_OPENROUTER:
        return OpenRouterClient(api_key=settings.api_key, model_name=settings.model_name)
    raise ValueError(f"Unknown classifier provider: {settings.provider!r}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = load_settings()
    logger.info(
        "Mail-classifier starting: provider=%s model=%s",
        settings.provider,
        settings.model_name,
    )
    repository = ClassificationRepository(settings.db_path)
    syncer = IMAPEmailSync(
        repository=repository,
        mailserver_host=settings.mailserver_host,
        mailserver_port=settings.mailserver_port,
        mailbox=settings.mailbox,
    )
    classifier_client = _build_classifier_client(settings)
    service = MailClassifierService(settings, repository, syncer, classifier_client)
    service.run_forever()


if __name__ == "__main__":
    main()
