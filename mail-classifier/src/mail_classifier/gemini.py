from __future__ import annotations

from google import genai
from google.genai import types

from .classification import (
    SYSTEM_INSTRUCTION,
    ClassificationResult,
    build_prompt,
    parse_classification_payload,
)

# Re-export for backwards compatibility -- existing imports of
# `from mail_classifier.gemini import ClassificationResult,
# parse_classification_payload` keep working after the split.
__all__ = ["GeminiClient", "ClassificationResult", "parse_classification_payload"]


class GeminiClient:
    def __init__(self, api_key: str, model_name: str) -> None:
        self.model_name = model_name
        # vertexai=True + api_key selects Vertex AI Express Mode, which uses
        # a single API key (issued from the Vertex AI Express Mode console)
        # in place of full Google Cloud ADC. Note: AI Studio Gemini API keys
        # are NOT interchangeable here -- use OpenRouter if you only have an
        # AI Studio key.
        self.client = genai.Client(vertexai=True, api_key=api_key)

    def classify_email(
        self, *, sender: str, recipient: str, subject: str, body_text: str
    ) -> ClassificationResult:
        prompt = build_prompt(
            sender=sender, recipient=recipient, subject=subject, body_text=body_text
        )
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0,
                response_mime_type="application/json",
            ),
        )
        text = response.text
        if not text:
            raise ValueError("Gemini response did not contain any text")
        return parse_classification_payload(text)
