import pytest

from mail_classifier.config import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    PROVIDER_GEMINI,
    PROVIDER_OPENROUTER,
    _select_provider,
    load_settings,
)


def test_select_provider_picks_gemini_when_only_gemini_key_is_set():
    assert _select_provider("gemini-key", "") == PROVIDER_GEMINI


def test_select_provider_picks_openrouter_when_only_openrouter_key_is_set():
    assert _select_provider("", "openrouter-key") == PROVIDER_OPENROUTER


def test_select_provider_refuses_when_both_keys_are_set():
    with pytest.raises(ValueError, match="Set only one of"):
        _select_provider("gemini-key", "openrouter-key")


def test_select_provider_refuses_when_neither_key_is_set():
    with pytest.raises(ValueError, match="must be set"):
        _select_provider("", "")


def test_load_settings_with_gemini_key_uses_gemini_defaults(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)

    settings = load_settings()

    assert settings.provider == PROVIDER_GEMINI
    assert settings.api_key == "g-key"
    assert settings.model_name == DEFAULT_GEMINI_MODEL


def test_load_settings_with_openrouter_key_uses_openrouter_defaults(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "o-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)

    settings = load_settings()

    assert settings.provider == PROVIDER_OPENROUTER
    assert settings.api_key == "o-key"
    assert settings.model_name == DEFAULT_OPENROUTER_MODEL


def test_load_settings_respects_openrouter_model_override(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "o-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    settings = load_settings()

    assert settings.provider == PROVIDER_OPENROUTER
    assert settings.model_name == "openai/gpt-4o-mini"
