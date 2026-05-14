"""Backend-agnostic primitives for email classification.

The classifier supports multiple LLM backends (currently Gemini direct via the
google-genai SDK and OpenRouter via its OpenAI-compatible HTTP API). Both
backends share the same prompt, system instruction, and JSON response schema
so the rest of the service can stay backend-agnostic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

VALID_LABELS = frozenset({"malicious", "benign"})

SYSTEM_INSTRUCTION = (
    "You are an email security classifier. Respond with strict JSON only."
)


@dataclass(frozen=True)
class ClassificationResult:
    label: str
    reason: str


class ClassifierClient(Protocol):
    """Common shape implemented by both GeminiClient and OpenRouterClient.

    `model_name` is read by the rest of the service when stamping
    classifier_emails.classification_model so operators can see in the DB
    which backend+model produced each label.
    """

    model_name: str

    def classify_email(
        self,
        *,
        sender: str,
        recipient: str,
        subject: str,
        body_text: str,
    ) -> ClassificationResult: ...


def build_prompt(*, sender: str, recipient: str, subject: str, body_text: str) -> str:
    return (
        "Classify the following email as either malicious or benign.\n"
        "Treat phishing, credential theft, malware delivery, fraud, impersonation, "
        "or suspicious payment urgency as malicious.\n"
        "Return JSON only with keys label and reason.\n\n"
        f"From: {sender}\n"
        f"To: {recipient}\n"
        f"Subject: {subject}\n"
        "Body:\n"
        f"{body_text}"
    )


def parse_classification_payload(raw_text: str) -> ClassificationResult:
    payload = json.loads(raw_text)
    label = str(payload.get("label", "")).strip().lower()
    reason = str(payload.get("reason", "")).strip()
    if label not in VALID_LABELS:
        raise ValueError(f"Unexpected classification label: {label!r}")
    if not reason:
        raise ValueError("Classification reason was empty")
    return ClassificationResult(label=label, reason=reason)
