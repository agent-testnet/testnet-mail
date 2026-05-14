import json

import pytest

from mail_classifier.openrouter import OpenRouterClient, _extract_message_content


def test_openrouter_client_constructs_correct_request_and_parses_response(monkeypatch):
    captured: dict = {}

    class FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None:
            return None

        def read(self) -> bytes:
            return self._body

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        response_payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {"label": "malicious", "reason": "Phishing-style urgency."}
                        ),
                    }
                }
            ]
        }
        return FakeResponse(json.dumps(response_payload).encode("utf-8"))

    monkeypatch.setattr("mail_classifier.openrouter.urllib.request.urlopen", fake_urlopen)

    client = OpenRouterClient(api_key="sk-or-test", model_name="google/gemini-2.5-flash-lite")
    result = client.classify_email(
        sender="Mallory <mallory@evil.example>",
        recipient="alice@gmail.com",
        subject="Urgent password reset",
        body_text="Send me your password immediately.",
    )

    assert result.label == "malicious"
    assert "urgency" in result.reason.lower()

    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"] == "Bearer sk-or-test"
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["body"]["model"] == "google/gemini-2.5-flash-lite"
    assert captured["body"]["temperature"] == 0
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["body"]["messages"][0]["role"] == "system"
    assert captured["body"]["messages"][1]["role"] == "user"


def test_openrouter_client_rejects_empty_api_key():
    with pytest.raises(ValueError):
        OpenRouterClient(api_key="", model_name="google/gemini-2.5-flash-lite")


def test_openrouter_client_rejects_empty_model():
    with pytest.raises(ValueError):
        OpenRouterClient(api_key="sk-or-test", model_name="")


def test_extract_message_content_surfaces_api_error():
    payload = {"error": {"message": "Rate limit exceeded", "code": 429}}
    with pytest.raises(RuntimeError, match="Rate limit exceeded"):
        _extract_message_content(payload)


def test_extract_message_content_handles_empty_choices():
    with pytest.raises(RuntimeError, match="no choices"):
        _extract_message_content({"choices": []})


def test_extract_message_content_handles_empty_message():
    with pytest.raises(RuntimeError, match="empty message content"):
        _extract_message_content({"choices": [{"message": {"content": ""}}]})
