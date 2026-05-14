from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_ACCOUNT_NAMES = ("alice", "bob", "charlie", "diana")


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
    gemini_api_key: str
    gemini_model: str
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


def load_settings() -> Settings:
    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not gemini_api_key:
        raise ValueError("GEMINI_API_KEY is required for the mail-classifier service")

    return Settings(
        db_path=os.getenv("CLASSIFIER_DB_PATH", "/var/roundcube/db/sqlite.db"),
        heartbeat_path=os.getenv("CLASSIFIER_HEARTBEAT_PATH", "/tmp/mail-classifier.heartbeat"),
        mailserver_host=os.getenv("MAILSERVER_HOST", "mailserver"),
        mailserver_port=int(os.getenv("MAILSERVER_PORT", "143")),
        mailbox=os.getenv("CLASSIFIER_MAILBOX", "INBOX"),
        poll_interval_seconds=int(os.getenv("CLASSIFIER_POLL_INTERVAL_SECONDS", "15")),
        batch_size=int(os.getenv("CLASSIFIER_BATCH_SIZE", "10")),
        gemini_api_key=gemini_api_key,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"),
        accounts=_parse_accounts(
            os.getenv("CLASSIFIER_ACCOUNTS"),
            os.getenv("MAIL_DOMAIN", "gmail.com"),
        ),
    )
