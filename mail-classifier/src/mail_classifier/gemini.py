from __future__ import annotations

import json
from dataclasses import dataclass

from google import genai
from google.genai import types


@dataclass(frozen=True)
class ClassificationResult:
    label: str
    reason: str


def parse_classification_payload(raw_text: str) -> ClassificationResult:
    payload = json.loads(raw_text)
    label = str(payload.get("label", "")).strip().lower()
    reason = str(payload.get("reason", "")).strip()
    if label not in {"malicious", "benign"}:
        raise ValueError(f"Unexpected classification label: {label!r}")
    if not reason:
        raise ValueError("Classification reason was empty")
    return ClassificationResult(label=label, reason=reason)


class GeminiClient:
    def __init__(self, api_key: str, model_name: str) -> None:
        self.model_name = model_name
        self.client = genai.Client(vertexai=True, api_key=api_key)

    def classify_email(
        self, *, sender: str, recipient: str, subject: str, body_text: str
    ) -> ClassificationResult:
        prompt = (
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
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=(
                    "You are an email security classifier. Respond with strict JSON only."
                ),
                temperature=0,
                response_mime_type="application/json",
            ),
        )
        text = response.text
        if not text:
            raise ValueError("Gemini response did not contain any text")
        return parse_classification_payload(text)
