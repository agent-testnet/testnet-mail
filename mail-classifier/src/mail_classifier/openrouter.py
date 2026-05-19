"""OpenRouter backend.

OpenRouter exposes an OpenAI-compatible Chat Completions API at
https://openrouter.ai/api/v1/chat/completions and routes requests to
hundreds of underlying models (OpenAI, Anthropic, Google, Meta, etc.) via a
single API key. We use it via stdlib urllib to avoid pulling in the openai
or httpx packages just for one HTTP POST per email.

See https://openrouter.ai/docs/api-reference/chat-completion for the full
schema. The relevant subset:

    POST /chat/completions
    Authorization: Bearer <key>
    Content-Type: application/json
    {
      "model": "google/gemini-2.5-flash-lite",
      "messages": [{"role": "system", "content": "..."},
                   {"role": "user", "content": "..."}],
      "temperature": 0,
      "response_format": {"type": "json_object"}
    }

    -> 200 {"choices": [{"message": {"content": "<json string>"}}], ...}
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Sequence

from .classification import (
    SYSTEM_INSTRUCTION,
    ClassificationResult,
    PriorMessage,
    build_prompt,
    parse_classification_payload,
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_TIMEOUT_SECONDS = 60

# Sent in the Referer/X-Title headers so this service shows up sensibly in
# the operator's OpenRouter dashboard. Both headers are optional but
# recommended by OpenRouter for ranking + analytics.
_REFERRER = "https://github.com/agent-testnet/testnet-mail"
_APP_TITLE = "testnet-mail mail-classifier"


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        model_name: str,
        *,
        url: str = OPENROUTER_URL,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is required for OpenRouterClient")
        if not model_name:
            raise ValueError("OPENROUTER_MODEL is required for OpenRouterClient")
        self.api_key = api_key
        self.model_name = model_name
        self.url = url
        self.timeout_seconds = timeout_seconds

    def classify_email(
        self,
        *,
        sender: str,
        recipient: str,
        subject: str,
        body_text: str,
        prior_messages: Sequence[PriorMessage] = (),
    ) -> ClassificationResult:
        prompt = build_prompt(
            sender=sender,
            recipient=recipient,
            subject=subject,
            body_text=body_text,
            prior_messages=prior_messages,
        )
        body = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        raw_text = self._post(body)
        return parse_classification_payload(raw_text)

    def _post(self, body: dict) -> str:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": _REFERRER,
                "X-Title": _APP_TITLE,
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(
                f"OpenRouter HTTP {exc.code}: {error_body[:500]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenRouter request failed: {exc.reason}") from exc

        return _extract_message_content(payload)


def _extract_message_content(payload: dict) -> str:
    """Pull the assistant message content out of an OpenAI-style chat
    completion response, with friendly errors for the common failure shapes
    OpenRouter actually returns (rate limit, model not found, etc.)."""
    if "error" in payload:
        err = payload["error"]
        message = err.get("message") if isinstance(err, dict) else str(err)
        raise RuntimeError(f"OpenRouter returned error: {message}")

    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter response had no choices: {payload!r}")

    message = choices[0].get("message") or {}
    content = message.get("content")
    if not content:
        raise RuntimeError(
            f"OpenRouter response had empty message content: {payload!r}"
        )
    return content
