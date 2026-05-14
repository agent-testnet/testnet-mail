from __future__ import annotations

import os
from dataclasses import dataclass

PROVIDER_GEMINI = "gemini"
PROVIDER_OPENROUTER = "openrouter"
SUPPORTED_PROVIDERS = (PROVIDER_GEMINI, PROVIDER_OPENROUTER)

DEFAULT_ACCOUNT_NAMES = ("alice", "bob", "charlie", "diana")
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-lite"
DEFAULT_OPENROUTER_MODEL = "google/gemini-2.5-flash-lite"


@dataclass(frozen=True)
class MailAccount:
    email: str
    password: str


@dataclass(frozen=True)
class Settings:
    db_path: str
    heartbeat_path: str
    mailserver_host: str
    mailserver_port: int
    mailbox: str
    poll_interval_seconds: int
    batch_size: int
    provider: str
    api_key: str
    model_name: str
    accounts: tuple[MailAccount, ...]


def _parse_accounts(raw_accounts: str | None, mail_domain: str) -> tuple[MailAccount, ...]:
    if not raw_accounts:
        return tuple(
            MailAccount(f"{name}@{mail_domain}", f"{name}-password")
            for name in DEFAULT_ACCOUNT_NAMES
        )

    accounts: list[MailAccount] = []
    for item in raw_accounts.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(
                "CLASSIFIER_ACCOUNTS entries must look like email@example.com:password"
            )
        email, password = item.split(":", 1)
        email = email.strip().lower()
        password = password.strip()
        if not email or not password:
            raise ValueError("CLASSIFIER_ACCOUNTS entries must include email and password")
        accounts.append(MailAccount(email=email, password=password))

    if not accounts:
        raise ValueError("CLASSIFIER_ACCOUNTS did not contain any usable accounts")

    return tuple(accounts)


def _select_provider(gemini_key: str, openrouter_key: str) -> str:
    """Decide which LLM backend to use based on which API key the operator
    set. Explicit-is-better-than-implicit: refuse to start if both are set
    (operator must pick one) or if neither is set (no backend means the
    classifier has nothing to do).
    """
    if gemini_key and openrouter_key:
        raise ValueError(
            "Set only one of GEMINI_API_KEY or OPENROUTER_API_KEY, not both. "
            "The mail-classifier auto-selects its backend from whichever key is "
            "present, and refuses to guess when both are."
        )
    if gemini_key:
        return PROVIDER_GEMINI
    if openrouter_key:
        return PROVIDER_OPENROUTER
    raise ValueError(
        "One of GEMINI_API_KEY (Vertex AI Express Mode) or OPENROUTER_API_KEY "
        "must be set for the mail-classifier service to start."
    )


def load_settings() -> Settings:
    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
    openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    provider = _select_provider(gemini_api_key, openrouter_api_key)

    if provider == PROVIDER_GEMINI:
        api_key = gemini_api_key
        model_name = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    else:
        api_key = openrouter_api_key
        model_name = os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)

    return Settings(
        db_path=os.getenv("CLASSIFIER_DB_PATH", "/var/roundcube/db/sqlite.db"),
        heartbeat_path=os.getenv("CLASSIFIER_HEARTBEAT_PATH", "/tmp/mail-classifier.heartbeat"),
        mailserver_host=os.getenv("MAILSERVER_HOST", "mailserver"),
        mailserver_port=int(os.getenv("MAILSERVER_PORT", "143")),
        mailbox=os.getenv("CLASSIFIER_MAILBOX", "INBOX"),
        poll_interval_seconds=int(os.getenv("CLASSIFIER_POLL_INTERVAL_SECONDS", "15")),
        batch_size=int(os.getenv("CLASSIFIER_BATCH_SIZE", "10")),
        provider=provider,
        api_key=api_key,
        model_name=model_name,
        accounts=_parse_accounts(
            os.getenv("CLASSIFIER_ACCOUNTS"),
            os.getenv("MAIL_DOMAIN", "gmail.com"),
        ),
    )
