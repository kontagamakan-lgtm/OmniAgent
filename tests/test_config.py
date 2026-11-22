"""Tests de `config.py` — exécutables via pytest ou directement.

    python tests/test_config.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

# Bac à sable local au projet : évite les restrictions du %TEMP% système et
# n'écrit jamais hors de l'arborescence de travail.
_TMP_ROOT = Path(__file__).resolve().parent.parent / "data" / ".test-tmp"


@contextmanager
def tmpdir() -> Iterator[Path]:
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    target = _TMP_ROOT / uuid.uuid4().hex
    target.mkdir()
    try:
        yield target
    finally:
        shutil.rmtree(target, ignore_errors=True)


def test_defaults_are_valid() -> None:
    settings = config.Settings()
    errors = [i for i in settings.validate() if i.level == "error"]
    assert errors == [], f"défauts invalides: {errors}"


def test_default_config_raises_no_warning(monkeypatch) -> None:
    defaults = config.Settings()
    monkeypatch.setenv(
        defaults.llm.api_key_env, "sk-0123456789abcdef0123456789abcdef"
    )
    warnings = [i for i in defaults.validate() if i.level == "warning"]
    assert warnings == [], f"défauts bruités: {warnings}"


def test_roundtrip_keeps_every_field() -> None:
    original = config.Settings()
    original.agent.max_steps = 42
    original.llm.backend = "ollama"
    original.files.allowed_roots = [str(Path.home())]
    with tmpdir() as tmp:
        target = Path(tmp) / "config.json"
        original.save(target)
        reloaded = config.Settings.load(target, apply_env=False, strict=False)
    assert reloaded.to_dict() == original.to_dict()
    assert reloaded.agent.max_steps == 42
    assert reloaded.llm.backend == "ollama"


def test_paths_are_not_persisted() -> None:
    with tmpdir() as tmp:
        target = Path(tmp) / "config.json"
        config.Settings().save(target)
        payload = json.loads(target.read_text(encoding="utf-8"))
    assert "paths" not in payload
    assert payload["schema_version"] == config.CONFIG_SCHEMA_VERSION


def test_env_overrides_are_typed() -> None:
    environ = {
        "OMNIAGENT_LLM_MODEL": "qwen2.5:14b",
        "OMNIAGENT_LLM_TEMPERATURE": "0.7",
        "OMNIAGENT_BROWSER_HEADLESS": "1",
        "OMNIAGENT_FILES_ALLOWED_ROOTS": r"C:\A;C:\B",
        "OMNIAGENT_SEARCH_MAX_RESULTS_PER_QUERY": "5",
        "OMNIAGENT_VAULT_KDF": "scrypt",
    }
    settings = config.Settings()
    applied = config._apply_env_overrides(settings, environ)
    assert len(applied) == 6
    assert settings.llm.model == "qwen2.5:14b"
    assert settings.llm.temperature == 0.7
    assert settings.browser.headless is True
    assert settings.files.allowed_roots == [r"C:\A", r"C:\B"]
    assert settings.search.max_results_per_query == 5
    assert settings.vault.kdf == "scrypt"


def test_env_override_rejects_bad_literal() -> None:
    try:
        config._apply_env_overrides(
            config.Settings(), {"OMNIAGENT_LLM_BACKEND": "skynet"}
        )
    except config.ConfigError as exc:
        assert "OMNIAGENT_LLM_BACKEND" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("une valeur de Literal invalide doit être rejetée")


def test_validation_catches_unsafe_vault_kdf() -> None:
    settings = config.Settings()
    settings.vault.argon2_memory_kib = 1024
    messages = [i.field for i in settings.validate() if i.level == "error"]
    assert "vault.argon2_memory_kib" in messages


def test_validation_catches_incoherent_research_budget() -> None:
    settings = config.Settings()
    settings.agent.deep_research_min_queries = 12
    messages = [i.field for i in settings.validate() if i.level == "error"]
    assert "agent.deep_research_min_queries" in messages


def test_validation_rejects_an_unusable_emergency_hotkey() -> None:
    settings = config.Settings()
    settings.ui.emergency_hotkey = "S"
    messages = [i.field for i in settings.validate() if i.level == "error"]
    assert "ui.emergency_hotkey" in messages


def test_emergency_hotkey_is_serialised_and_overridable() -> None:
    settings = config.Settings()
    settings.ui.emergency_hotkey = "Ctrl+Shift+F9"
    assert settings.to_dict()["ui"]["emergency_hotkey"] == "Ctrl+Shift+F9"
    assert settings.summary()["ui"]["emergency_hotkey"] == "Ctrl+Shift+F9"

    overridden = config.Settings()
    config._apply_env_overrides(
        overridden, {"OMNIAGENT_UI_EMERGENCY_HOTKEY": "Alt+Shift+K"}
    )
    assert overridden.ui.emergency_hotkey == "Alt+Shift+K"


def test_unknown_keys_are_ignored() -> None:
    with tmpdir() as tmp:
        target = Path(tmp) / "config.json"
        target.write_text(
            json.dumps({"llm": {"model": "x", "unknown_option": 1}, "legacy": True}),
            encoding="utf-8",
        )
        settings = config.Settings.load(target, apply_env=False, strict=False)
    assert settings.llm.model == "x"


def test_newer_schema_is_rejected() -> None:
    with tmpdir() as tmp:
        target = Path(tmp) / "config.json"
        target.write_text(
            json.dumps({"schema_version": config.CONFIG_SCHEMA_VERSION + 1}),
            encoding="utf-8",
        )
        try:
            config.Settings.load(target, apply_env=False, strict=False)
        except config.ConfigError as exc:
            assert "schéma" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("un schéma plus récent doit être refusé")


def test_dotenv_does_not_override_real_env() -> None:
    variable = "OMNIAGENT_TEST_DOTENV"
    os.environ[variable] = "reel"
    try:
        with tmpdir() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(f'{variable}="fichier"\nOMNIAGENT_TEST_OTHER=ok\n', encoding="utf-8")
            assert config.load_dotenv(env_file) is True
        assert os.environ[variable] == "reel"
        assert os.environ["OMNIAGENT_TEST_OTHER"] == "ok"
    finally:
        os.environ.pop(variable, None)
        os.environ.pop("OMNIAGENT_TEST_OTHER", None)


def test_summary_never_leaks_secrets() -> None:
    summary = config.Settings().summary()
    blob = json.dumps(summary)
    assert "DEEPSEEK_API_KEY" not in blob or "credentials_configured" in blob
    assert "api_key" not in blob

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                assert "secret" not in key.lower()
                assert key.lower() not in {"api_key", "password", "token"}
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(summary)


def main() -> int:
    failures = 0
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - runner minimaliste
            failures += 1
            print(f"ECHEC {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"OK    {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passés")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())


def test_http_vers_un_hote_distant_est_refuse() -> None:
    """La clé et les prompts traverseraient le réseau en clair."""
    settings = config.Settings()
    settings.llm.base_url = "http://api.exemple.invalid/v1"
    errors = [i for i in settings.validate() if i.level == "error"]
    assert any(i.field == "llm.base_url" for i in errors), errors


def test_http_sur_la_boucle_locale_reste_accepte() -> None:
    settings = config.Settings()
    settings.llm.backend = "openai_compatible"
    settings.llm.base_url = "http://127.0.0.1:11434/v1"
    errors = [i for i in settings.validate() if i.level == "error" and i.field == "llm.base_url"]
    assert errors == []


def test_env_example_matches_the_init_template() -> None:
    """`config.py --init` doit produire le même gabarit que le fichier livré."""
    root_example = Path(config.__file__).resolve().parent / ".env.example"
    assert root_example.is_file(), f"gabarit absent : {root_example}"
    committed = root_example.read_text(encoding="utf-8")
    assert committed == config._DEFAULT_ENV_EXAMPLE, (
        "`.env.example` et `config._DEFAULT_ENV_EXAMPLE` ont divergé : "
        "mettre à jour les deux (le fichier reste la référence)."
    )


def test_l_index_semantique_refuse_le_clair_hors_localhost() -> None:
    settings = config.Settings()
    settings.embedding.base_url = "http://vecteurs.exemple.invalid/v1"
    errors = [i for i in settings.validate() if i.level == "error"]
    assert any(i.field == "embedding.base_url" for i in errors), errors

    settings.embedding.base_url = "http://localhost:11434"
    errors = [i for i in settings.validate() if i.level == "error"]
    assert not any(i.field == "embedding.base_url" for i in errors), errors
