from __future__ import annotations

import email
import imaplib
import logging
import re
from dataclasses import dataclass
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime
from html import unescape

from .config import MailAccount
from .db import ClassificationRepository, EmailRecord, utcnow_iso

logger = logging.getLogger(__name__)
IMAP_AUTH_ERROR = imaplib.IMAP4.error


@dataclass(frozen=True)
class SyncResult:
    inserted: int = 0
    skipped: int = 0


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    return "".join(
        chunk.decode(charset or "utf-8", errors="ignore")
        if isinstance(chunk, bytes)
        else chunk
        for chunk, charset in decode_header(value)
    ).strip()


def extract_body_text(message: Message) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get("Content-Disposition", "").lower().startswith("attachment"):
            continue

        charset = part.get_content_charset() or "utf-8"
        payload = part.get_payload(decode=True)
        if payload is None:
            continue

        text = payload.decode(charset, errors="ignore").strip()
        content_type = part.get_content_type()
        if content_type == "text/plain":
            plain_parts.append(text)
        elif content_type == "text/html":
            html_parts.append(_strip_html(text))

    if plain_parts:
        return "\n".join(part for part in plain_parts if part).strip()
    if html_parts:
        return "\n".join(part for part in html_parts if part).strip()

    payload = message.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="ignore").strip()
    if isinstance(payload, str):
        return payload.strip()
    return ""


def _strip_html(raw_html: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", raw_html)
    return re.sub(r"\s+", " ", unescape(without_tags)).strip()


def parse_received_at(message: Message) -> str:
    date_header = message.get("Date")
    if not date_header:
        return utcnow_iso()
    try:
        return parsedate_to_datetime(date_header).isoformat()
    except Exception:
        return utcnow_iso()


class IMAPEmailSync:
    def __init__(
        self,
        repository: ClassificationRepository,
        mailserver_host: str,
        mailserver_port: int,
        mailbox: str,
    ) -> None:
        self.repository = repository
        self.mailserver_host = mailserver_host
        self.mailserver_port = mailserver_port
        self.mailbox = mailbox

    def sync_accounts(self, accounts: tuple[MailAccount, ...]) -> SyncResult:
        total_inserted = 0
        total_skipped = 0
        for account in accounts:
            try:
                result = self.sync_account(account)
            except IMAP_AUTH_ERROR as exc:
                logger.warning("Skipping mailbox %s due to IMAP auth error: %s", account.email, exc)
                continue
            except Exception:
                logger.exception("Skipping mailbox %s due to unexpected sync error", account.email)
                continue
            total_inserted += result.inserted
            total_skipped += result.skipped
        return SyncResult(inserted=total_inserted, skipped=total_skipped)

    def sync_account(self, account: MailAccount) -> SyncResult:
        known_uids = self.repository.list_known_uids(account.email, self.mailbox)
        inserted = 0
        skipped = 0

        connection = imaplib.IMAP4(self.mailserver_host, self.mailserver_port)
        try:
            connection.login(account.email, account.password)
            connection.select(self.mailbox)
            status, payload = connection.uid("search", None, "ALL")
            if status != "OK":
                raise RuntimeError(f"UID SEARCH failed for {account.email}: {payload!r}")

            for uid_bytes in payload[0].split():
                uid = uid_bytes.decode("utf-8")
                if uid in known_uids:
                    skipped += 1
                    continue

                email_record = self._fetch_email(connection, account, uid)
                if self.repository.insert_email(email_record):
                    inserted += 1
                else:
                    skipped += 1
        finally:
            try:
                connection.logout()
            except Exception:
                pass

        return SyncResult(inserted=inserted, skipped=skipped)

    def _fetch_email(
        self, connection: imaplib.IMAP4, account: MailAccount, uid: str
    ) -> EmailRecord:
        status, payload = connection.uid("fetch", uid, "(RFC822)")
        if status != "OK" or not payload or payload[0] is None:
            raise RuntimeError(f"UID FETCH failed for {account.email} uid={uid}")

        raw_message = payload[0][1]
        message = email.message_from_bytes(raw_message)
        subject = decode_mime_header(message.get("Subject"))
        sender = decode_mime_header(message.get("From"))
        recipient = decode_mime_header(message.get("To")) or account.email
        body_text = extract_body_text(message)
        message_id = decode_mime_header(message.get("Message-ID")) or f"{account.email}:{uid}"

        return EmailRecord(
            account_email=account.email,
            mailbox=self.mailbox,
            uid=uid,
            message_id=message_id,
            sender=sender,
            recipient=recipient,
            subject=subject,
            body_text=body_text,
            received_at=parse_received_at(message),
        )
