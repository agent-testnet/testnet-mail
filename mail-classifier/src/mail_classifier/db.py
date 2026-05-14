from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MAX_CLASSIFICATION_ATTEMPTS = int(os.getenv("CLASSIFIER_MAX_ATTEMPTS", "5"))


@dataclass(frozen=True)
class EmailRecord:
    account_email: str
    mailbox: str
    uid: str
    message_id: str
    sender: str
    recipient: str
    subject: str
    body_text: str
    received_at: str


@dataclass(frozen=True)
class PendingEmail:
    id: int
    sender: str
    recipient: str
    subject: str
    body_text: str


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ClassificationRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self) -> None:
        with self._connect() as conn:
            # WAL lets the dashboard (read-only SELECTs) run concurrently with
            # the classifier's writes against this same SQLite file, and also
            # avoids "database is locked" errors when the roundcube container
            # writes its own session/cache tables on the shared volume.
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS classifier_emails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_email TEXT NOT NULL,
                    mailbox TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body_text TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    classification_status TEXT NOT NULL DEFAULT 'pending',
                    classification_label TEXT,
                    classification_reason TEXT,
                    classified_at TEXT,
                    classification_model TEXT,
                    classification_attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(account_email, mailbox, uid)
                );

                CREATE INDEX IF NOT EXISTS idx_classifier_emails_pending
                    ON classifier_emails(classification_status, id);
                """
            )

    def list_known_uids(self, account_email: str, mailbox: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT uid FROM classifier_emails WHERE account_email = ? AND mailbox = ?",
                (account_email, mailbox),
            ).fetchall()
        return {row["uid"] for row in rows}

    def insert_email(self, record: EmailRecord) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO classifier_emails (
                    account_email,
                    mailbox,
                    uid,
                    message_id,
                    sender,
                    recipient,
                    subject,
                    body_text,
                    received_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.account_email,
                    record.mailbox,
                    record.uid,
                    record.message_id,
                    record.sender,
                    record.recipient,
                    record.subject,
                    record.body_text,
                    record.received_at,
                    utcnow_iso(),
                ),
            )
        return cursor.rowcount == 1

    def pending_emails(self, limit: int) -> list[PendingEmail]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, sender, recipient, subject, body_text
                FROM classifier_emails
                WHERE classification_status = 'pending'
                  AND classification_attempts < ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (MAX_CLASSIFICATION_ATTEMPTS, limit),
            ).fetchall()
        return [
            PendingEmail(
                id=row["id"],
                sender=row["sender"],
                recipient=row["recipient"],
                subject=row["subject"],
                body_text=row["body_text"],
            )
            for row in rows
        ]

    def save_classification(
        self, email_id: int, label: str, reason: str, model_name: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE classifier_emails
                SET classification_status = 'classified',
                    classification_label = ?,
                    classification_reason = ?,
                    classified_at = ?,
                    classification_model = ?,
                    classification_attempts = classification_attempts + 1,
                    last_error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (label, reason, utcnow_iso(), model_name, utcnow_iso(), email_id),
            )

    def record_classification_error(self, email_id: int, error_message: str) -> None:
        # Bump the attempt counter and, once we've burned through the budget,
        # flip status to 'failed' so the pending_emails() query stops handing
        # this row out on every poll cycle. Without this guard a permanently
        # bad row (Gemini quota exhausted, content blocked, malformed body,
        # etc.) gets re-pulled every poll_interval_seconds forever, burning
        # API quota and dollars.
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE classifier_emails
                SET classification_attempts = classification_attempts + 1,
                    last_error = ?,
                    classification_status = CASE
                        WHEN classification_attempts + 1 >= ? THEN 'failed'
                        ELSE classification_status
                    END,
                    updated_at = ?
                WHERE id = ?
                """,
                (error_message, MAX_CLASSIFICATION_ATTEMPTS, utcnow_iso(), email_id),
            )

    def fetch_email(self, email_id: int) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM classifier_emails WHERE id = ?",
                (email_id,),
            ).fetchone()
