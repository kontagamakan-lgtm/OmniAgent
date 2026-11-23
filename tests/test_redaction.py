"""Tests du masquage des secrets."""

from __future__ import annotations

import sys

from support import run  # noqa: I001

from security.redaction import Redactor, is_secret_key, redact

MASK = "MASQUÉ"


def test_openai_and_github_keys_are_masked() -> None:
    text = "clé = sk-abcdefghijklmnopqrstuvwxyz012345 et ghp_abcdefghijklmnopqrstuvwxyz0123"
    result = redact(text)
    assert "sk-abcdefghij" not in result
    assert "ghp_abcdefghij" not in result
    assert MASK in result


def _assert_masked(secret: str) -> str:
    result = redact(f"clé = {secret} fin")
    assert secret not in result, result
    assert MASK in result
    return result


def test_openai_key_ending_with_a_separator_is_masked() -> None:
    for separator in ("-", "_"):
        secret = "sk-" + "0" * 15 + separator
        _assert_masked(secret)


def test_jwt_ending_with_a_separator_is_masked() -> None:
    for separator in ("-", "_"):
        secret = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefgh" + separator
        _assert_masked(secret)


def test_slack_token_ending_with_a_separator_is_masked() -> None:
    # Le tiret fait partie du jeton Slack ; le souligné n'en fait pas partie.
    _assert_masked("xoxb-abcdefghij-")


def test_google_key_ending_with_a_separator_is_masked() -> None:
    # Les deux formats de Google : `AIza…` (historique) et `AQ.…` (AI Studio).
    for body in ("AIza" + "a" * 30, "AQ." + "a" * 30):
        for separator in ("-", "_"):
            _assert_masked(body + separator)


def test_separator_masking_is_idempotent() -> None:
    secrets = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefgh-",
        "xoxb-abcdefghij-",
        "AIza" + "a" * 30 + "_",
        "AQ." + "a" * 30 + "_",
    )
    once = redact("jetons : " + " ".join(secrets))
    for secret in secrets:
        assert secret not in once, once
    assert redact(once) == once


def test_assigned_password_is_masked_but_keeps_structure() -> None:
    result = redact('mot de passe: password = "MonSuperMotDePasse!"')
    assert "MonSuperMotDePasse" not in result
    assert "password" in result
    assert MASK in result


def test_url_credentials_are_masked() -> None:
    result = redact("connexion https://admin:secret123@intranet.local/api")
    assert "secret123" not in result
    assert "intranet.local" in result


def test_authorization_header_is_fully_masked() -> None:
    result = redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghij")
    assert "eyJhbGciOiJIUzI1NiJ9" not in result
    assert MASK in result


def test_standalone_bearer_token_keeps_scheme_readable() -> None:
    result = redact("en-tête reçu : Bearer abcdef1234567890xyz")
    assert "abcdef1234567890xyz" not in result
    assert "Bearer" in result


def test_redaction_is_idempotent() -> None:
    text = 'password = "hunter2" et https://u:p@hote/ et ?token=abc123'
    once = redact(text)
    assert redact(once) == once


def test_private_key_block_is_masked() -> None:
    block = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA1234567890abcdef\n"
        "-----END RSA PRIVATE KEY-----"
    )
    result = redact(f"clé trouvée : {block}")
    assert "MIIEowIBAAKCAQEA" not in result


def test_query_string_secret_is_masked() -> None:
    result = redact("https://exemple.fr/cb?token=abc123def456&user=jean")
    assert "abc123def456" not in result
    assert "user=jean" in result


def test_any_provider_key_variable_is_masked() -> None:
    """`GROQ_API_KEY=…` : le souligné empêchait la limite de mot de s'appliquer."""
    for name in ("GROQ_API_KEY", "MISTRAL_API_KEY", "OMNIAGENT_API_KEY", "X_API_KEY"):
        valeur = "AbCdEf0123456789"
        result = redact(f"{name}={valeur}")
        assert valeur not in result, (name, result)
        assert name in result, result


def test_structured_redaction_masks_secret_fields() -> None:
    payload = {
        "service": "github.com",
        "username": "jean",
        "password": "hunter2",
        "nested": {"api_key": "abc", "keep": "visible"},
        "list": [{"token": "xyz"}],
    }
    cleaned = Redactor().redact_data(payload)
    assert cleaned["username"] == "jean"
    assert cleaned["password"] != "hunter2"
    assert cleaned["nested"]["api_key"] != "abc"
    assert cleaned["nested"]["keep"] == "visible"
    assert cleaned["list"][0]["token"] != "xyz"


def test_benign_text_is_untouched() -> None:
    text = "Le fichier rapport-2024.pdf pèse 3 Mo et se trouve dans Documents."
    assert redact(text) == text


def test_secret_key_detection() -> None:
    assert is_secret_key("api_key")
    assert is_secret_key("Client-Secret")
    assert is_secret_key("user_password")
    assert not is_secret_key("username")
    assert not is_secret_key("service")


def test_disabled_redactor_passes_through() -> None:
    text = "password = hunter2"
    assert Redactor(enabled=False).redact(text) == text


if __name__ == "__main__":
    sys.exit(run(globals()))
