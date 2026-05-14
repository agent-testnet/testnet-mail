import pytest

from mail_classifier.gemini import parse_classification_payload


def test_parse_classification_payload_accepts_benign_json():
    result = parse_classification_payload(
        '{"label":"benign","reason":"Routine project update between known users."}'
    )

    assert result.label == "benign"
    assert "Routine project update" in result.reason


def test_parse_classification_payload_rejects_unknown_label():
    with pytest.raises(ValueError):
        parse_classification_payload('{"label":"spam","reason":"Not one of the allowed labels."}')

