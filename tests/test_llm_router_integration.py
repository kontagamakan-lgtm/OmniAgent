"""Test d'intégration réel du routeur LLM, strictement opt-in.

Aucun appel réseau n'est fait par la suite normale. Le test ne s'exécute que
si l'une de ces sources de clé est fournie :

* ``OMNIAGENT_TEST_LLM_KEY`` : clé de test placée dans un coffre temporaire ;
  ``OMNIAGENT_TEST_LLM_PROVIDER`` choisit le fournisseur (utile pour les clés
  ``sk-`` ambiguës) et ``OMNIAGENT_TEST_LLM_MODEL`` impose le modèle ;
* ``OMNIAGENT_TEST_VAULT_PASSPHRASE`` : ouvre le coffre configuré et utilise
  les clés déjà enregistrées par le routeur dans son état habituel.

Exemples PowerShell :

    $env:OMNIAGENT_TEST_LLM_KEY = "sk-ds-..."
    $env:OMNIAGENT_TEST_LLM_PROVIDER = "deepseek"
    $env:OMNIAGENT_TEST_LLM_MODEL = "deepseek-chat"
    python -m pytest tests/test_llm_router_integration.py -q

    $env:OMNIAGENT_TEST_VAULT_PASSPHRASE = "..."
    python -m pytest tests/test_llm_router_integration.py -q

Le test force un modèle et coupe le secours Ollama : une clé refusée doit
échouer, pas être masquée par une réponse locale. Sans clé, il est ignoré avec
un message explicite.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

from config import Settings  # noqa: E402
from core.llm_router import LLMRouter, PROVIDERS, detect_provider  # noqa: E402
from tools.vault import Vault  # noqa: E402

KEY_ENV = "OMNIAGENT_TEST_LLM_KEY"
PROVIDER_ENV = "OMNIAGENT_TEST_LLM_PROVIDER"
MODEL_ENV = "OMNIAGENT_TEST_LLM_MODEL"
VAULT_PASSPHRASE_ENV = "OMNIAGENT_TEST_VAULT_PASSPHRASE"

DEFAULT_MODELS = {
    "openai": "gpt-4.1-mini",
    "anthropic": "claude-haiku-4-5",
    "deepseek": "deepseek-chat",
    "gemini": "gemini-3.5-flash",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "deepseek/deepseek-chat",
    "mistral": "mistral-large-latest",
}


def _real_call(router: LLMRouter, model: str) -> None:
    """Un seul échange, court et sans secours local."""
    response = router.complete(
        [{"role": "user", "content": "Réponds uniquement par le mot OK."}],
        model=model,
        temperature=0.0,
    )
    assert response.text.strip(), "le fournisseur a renvoyé une réponse vide"
    assert router.current_provider in PROVIDERS


def test_router_reaches_a_real_provider_when_a_test_key_is_configured() -> None:
    key = os.environ.get(KEY_ENV, "").strip()
    passphrase = os.environ.get(VAULT_PASSPHRASE_ENV, "").strip()
    if not key and not passphrase:
        pytest.skip(
            f"intégration LLM opt-in : définir {KEY_ENV} ou "
            f"{VAULT_PASSPHRASE_ENV} pour faire un vrai appel"
        )

    model_override = os.environ.get(MODEL_ENV, "").strip()

    if key:
        with tmpdir("router-integration") as tmp:
            settings = make_settings(tmp)
            settings.llm.request_timeout_s = 90.0
            settings.llm.max_output_tokens = 32
            # Aucun repli local ne doit masquer un refus du fournisseur cloud.
            settings.llm.ollama_base_url = ""
            settings.llm_router.max_attempts = 1

            provider = os.environ.get(PROVIDER_ENV, "").strip().lower()
            if not provider:
                provider = detect_provider(key)
            if provider not in PROVIDERS or provider == "ollama":
                pytest.fail(f"fournisseur de test inconnu : {provider!r}")

            model = model_override or DEFAULT_MODELS[provider]
            vault = Vault(settings)
            vault.create("Test-Integration-Router-9!")
            router = LLMRouter(settings, vault, data_dir=tmp / "router")
            try:
                router.add_key(key, provider=provider, label="clé de test")
                _real_call(router, model)
            finally:
                router.close()
        return

    settings = Settings.load(strict=False)
    settings.llm.request_timeout_s = 90.0
    settings.llm.max_output_tokens = 32
    settings.llm.ollama_base_url = ""
    vault = Vault(settings)
    if not vault.exists:
        pytest.skip(f"{VAULT_PASSPHRASE_ENV} est défini mais le coffre est absent")
    try:
        vault.unlock(passphrase)
    except Exception as exc:  # noqa: BLE001 - l'option est ignorée proprement
        pytest.skip(f"coffre de test inaccessible : {type(exc).__name__}")

    router = LLMRouter(settings, vault)
    try:
        if not router.available():
            pytest.skip("aucune clé active dans le coffre configuré")
        model = model_override or ""
        _real_call(router, model)
    finally:
        router.close()
