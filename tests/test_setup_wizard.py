"""Tests de l'assistant de premier démarrage (logique, sans Tk)."""

from __future__ import annotations

import os

from support import make_settings, tmpdir

from ui.setup_wizard import apply_setup, needs_setup, write_env_value

PASSPHRASE = "phrase-du-coffre-2026"
#: Clé qui passe le préflight DeepSeek (préfixe, longueur, corps varié).
KEY = "sk-0123456789abcdef0123456789abcd"


def test_needs_setup_tracks_the_api_key(monkeypatch) -> None:
    with tmpdir("setup") as tmp:
        settings = make_settings(tmp)
        variable = settings.llm.api_key_env
        monkeypatch.delenv(variable, raising=False)

        assert needs_setup(settings) is True
        monkeypatch.setenv(variable, "sk-0123456789abcdef0123456789abcdef")
        assert needs_setup(settings) is False

        settings.llm.backend = "ollama"
        monkeypatch.delenv(variable, raising=False)
        assert needs_setup(settings) is False, "Ollama n'a pas besoin de clé"


def test_needs_setup_treats_invalid_deepseek_keys_as_absent(monkeypatch) -> None:
    with tmpdir("setup-invalid-key") as tmp:
        settings = make_settings(tmp)
        variable = settings.llm.api_key_env
        invalid_keys = (
            "sk-court",
            "pas-une-cle",
            "sk-" + "test" + "x" * 30,
            "sk-" + "0" * 30,
        )

        for invalid in invalid_keys:
            monkeypatch.setenv(variable, invalid)
            assert needs_setup(settings) is True, (
                "une clé invalide doit être traitée comme absente par le wizard"
            )


def test_apply_setup_saves_all_four_settings(monkeypatch) -> None:
    with tmpdir("setup") as tmp:
        settings = make_settings(tmp)
        settings.ensure_directories()
        workdir = tmp / "travail"
        workdir.mkdir()
        env_file = tmp / ".env"
        variable = settings.llm.api_key_env
        monkeypatch.delenv(variable, raising=False)

        errors = apply_setup(
            settings,
            api_key=f"  {KEY}  ",
            workdir=str(workdir),
            channel="msedge",
            passphrase=PASSPHRASE,
            confirm=PASSPHRASE,
            env_path=env_file,
        )

        assert errors == [], errors
        assert os.environ[variable] == KEY, "la clé est active tout de suite"
        assert f"{variable}={KEY}" in env_file.read_text(encoding="utf-8")
        assert str(workdir) in settings.files.allowed_roots
        assert settings.browser.channel == "msedge"
        assert settings.paths.config_file.is_file(), "config.json doit être écrit"
        assert settings.paths.vault_file.is_file()

        from tools.vault import Vault

        vault = Vault(settings)
        vault.unlock(PASSPHRASE)
        assert vault.unlocked, "la phrase choisie doit ouvrir le coffre créé"


def test_apply_setup_reports_invalid_channel_and_missing_folder(monkeypatch) -> None:
    with tmpdir("setup") as tmp:
        settings = make_settings(tmp)
        monkeypatch.delenv(settings.llm.api_key_env, raising=False)
        before_roots = list(settings.files.allowed_roots)
        before_channel = settings.browser.channel

        errors = apply_setup(
            settings,
            api_key=KEY,
            workdir=str(tmp / "absent"),
            channel="opéra",
        )

        assert any("canal" in line for line in errors), errors
        assert any("introuvable" in line for line in errors), errors
        assert settings.browser.channel == before_channel
        assert settings.files.allowed_roots == before_roots
        # La clé valide est tout de même appliquée : l'utilisateur ne ressaisit pas tout.
        assert os.environ[settings.llm.api_key_env] == KEY


def test_apply_setup_refuses_mismatched_or_weak_passphrase(monkeypatch) -> None:
    with tmpdir("setup") as tmp:
        settings = make_settings(tmp)
        settings.ensure_directories()
        monkeypatch.delenv(settings.llm.api_key_env, raising=False)

        errors = apply_setup(
            settings,
            api_key=KEY,
            passphrase=PASSPHRASE,
            confirm="autre-chose",
        )
        assert any("correspondent pas" in line for line in errors), errors
        assert not settings.paths.vault_file.exists()

        errors = apply_setup(settings, api_key=KEY, passphrase="court", confirm="court")
        assert any("trop courte" in line for line in errors), errors
        assert not settings.paths.vault_file.exists()


def test_apply_setup_rejects_a_malformed_key_before_writing(monkeypatch) -> None:
    """Une clé refusée au démarrage ne doit pas être enregistrée en silence."""
    with tmpdir("setup-bad-key") as tmp:
        settings = make_settings(tmp)
        settings.ensure_directories()
        variable = settings.llm.api_key_env
        monkeypatch.delenv(variable, raising=False)
        env_file = tmp / ".env"

        errors = apply_setup(settings, api_key="pas-une-cle", env_path=env_file)

        assert any("clé d'API" in line for line in errors), errors
        assert variable not in os.environ
        assert not env_file.exists(), "rien ne doit être écrit dans .env"


def test_apply_setup_requires_a_key_when_generating(monkeypatch) -> None:
    with tmpdir("setup") as tmp:
        settings = make_settings(tmp)
        monkeypatch.delenv(settings.llm.api_key_env, raising=False)

        errors = apply_setup(settings, api_key="   ")

        assert any("clé API requise" in line for line in errors), errors


def test_write_env_value_preserves_other_lines() -> None:
    with tmpdir("setup") as tmp:
        env_file = tmp / ".env"
        env_file.write_text("# commentaire\nAUTRE=1\n", encoding="utf-8")

        write_env_value(env_file, "MA_CLE", "valeur avec espaces")
        write_env_value(env_file, "MA_CLE", "seconde")

        text = env_file.read_text(encoding="utf-8")
        assert "# commentaire" in text
        assert "AUTRE=1" in text
        assert text.count("MA_CLE=") == 1, text
        assert "MA_CLE=seconde" in text
