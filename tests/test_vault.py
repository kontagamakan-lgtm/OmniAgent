"""Tests du coffre-fort d'identifiants."""

from __future__ import annotations

import json
import sys
import time
import types

from support import make_settings, run, tmpdir  # noqa: I001

import security.crypto as crypto
from tools.base import ToolSet
from tools.file_manager import PathNotAllowed
from tools.vault import (
    MASK,
    Vault,
    VaultAuthError,
    VaultError,
    VaultThrottled,
    build_vault_tools,
    normalize_service,
    totp_code,
    totp_remaining,
    verify_totp,
)

PASSPHRASE = "Correct-Horse-Battery-9!"
NEW_PASSPHRASE = "Une-Autre-Phrase-Solide-42!"


def test_create_unlock_and_lock() -> None:
    with tmpdir("vault") as tmp:
        vault = Vault(make_settings(tmp))
        assert vault.exists is False
        vault.create(PASSPHRASE)
        assert vault.exists and vault.unlocked
        vault.lock()
        assert not vault.unlocked
        vault.unlock(PASSPHRASE)
        assert vault.unlocked


def test_create_force_backs_up_existing_vault() -> None:
    with tmpdir("vault") as tmp:
        settings = make_settings(tmp)
        vault = Vault(settings)
        vault.create(PASSPHRASE)
        vault.add("avant.example", "jean", "Avant#1")
        vault.lock()
        original = settings.paths.vault_file.read_bytes()

        replacement = Vault(settings)
        replacement.create(NEW_PASSPHRASE, force=True)

        backups = sorted(settings.paths.data_dir.glob("vault.enc.*.bak"))
        assert backups, "create(force=True) doit sauvegarder le coffre précédent"
        assert backups[0].read_bytes() == original
        assert replacement.unlocked
        replacement.lock()
        replacement.unlock(NEW_PASSPHRASE)


def test_keyring_fallback_is_opt_in() -> None:
    with tmpdir("vault") as tmp:
        settings = make_settings(tmp)
        assert settings.vault.keyring_fallback is False
        vault = Vault(settings)
        assert vault.keyring_available() is False
        assert vault.remember_in_keyring(PASSPHRASE) is False


def test_stale_keyring_entry_is_detected_then_purged() -> None:
    """Démarrage : une phrase déposée alors que l'option est off doit être signalée."""
    calls: list[tuple[str, ...]] = []
    state: dict[str, str | None] = {"password": "phrase-deposee"}
    fake = types.ModuleType("keyring")
    fake.get_password = lambda service, user: state["password"]  # type: ignore[attr-defined]
    fake.set_password = lambda service, user, value: state.update(password=value)  # type: ignore[attr-defined]

    def _delete(service: str, user: str) -> None:
        calls.append(("delete", service, user))
        state["password"] = None

    fake.delete_password = _delete  # type: ignore[attr-defined]

    previous = sys.modules.get("keyring")
    sys.modules["keyring"] = fake
    try:
        with tmpdir("vault") as tmp:
            settings = make_settings(tmp)
            vault = Vault(settings)
            status = vault.keyring_status()
            assert status == {"enabled": False, "available": True, "stored": True, "stale": True}

            # Refus explicite : rien n'est supprimé.
            assert vault.purge_stale_keyring(confirm=lambda: False) is False
            assert calls == []

            # Accord explicite : l'entrée disparaît et le reliquat aussi.
            assert vault.purge_stale_keyring(confirm=lambda: True) is True
            assert calls == [("delete", "OmniAgent-Vault", "master")]
            assert vault.keyring_status()["stale"] is False

            # Option activée : ce n'est plus un reliquat.
            state["password"] = "phrase-deposee"
            settings.vault.keyring_fallback = True
            assert vault.keyring_status()["stale"] is False
    finally:
        if previous is None:
            sys.modules.pop("keyring", None)
        else:
            sys.modules["keyring"] = previous


def test_forget_keyring_works_even_when_option_is_disabled() -> None:
    """Désactiver le dépôt ne doit pas empêcher de purger une phrase déjà stockée."""
    calls: list[tuple[str, ...]] = []
    fake = types.ModuleType("keyring")
    fake.set_password = lambda service, user, value: calls.append(("set", service, user))  # type: ignore[attr-defined]
    fake.get_password = lambda service, user: None  # type: ignore[attr-defined]
    fake.delete_password = lambda service, user: calls.append(("delete", service, user))  # type: ignore[attr-defined]

    previous = sys.modules.get("keyring")
    sys.modules["keyring"] = fake
    try:
        with tmpdir("vault") as tmp:
            vault = Vault(make_settings(tmp))
            assert vault.settings.vault.keyring_fallback is False
            # L'écriture reste soumise à l'opt-in.
            assert vault.remember_in_keyring(PASSPHRASE) is False
            # La purge, elle, doit aboutir.
            assert vault.forget_keyring() is True
            assert calls == [("delete", "OmniAgent-Vault", "master")]
    finally:
        if previous is None:
            sys.modules.pop("keyring", None)
        else:
            sys.modules["keyring"] = previous


def test_wrong_passphrase_then_throttle() -> None:
    with tmpdir("vault") as tmp:
        settings = make_settings(tmp)
        vault = Vault(settings)
        vault.create(PASSPHRASE)
        vault.lock()
        for _ in range(settings.vault.max_failed_unlocks):
            try:
                vault.unlock("mauvaise-phrase")
            except VaultAuthError:
                continue
            raise AssertionError("une mauvaise phrase doit être refusée")
        try:
            vault.unlock(PASSPHRASE)
        except VaultThrottled:
            return
        raise AssertionError("le verrouillage temporaire doit s'appliquer")


def test_crud_cycle() -> None:
    with tmpdir("vault") as tmp:
        vault = Vault(make_settings(tmp))
        vault.create(PASSPHRASE)

        entry = vault.add("https://www.GitHub.com/login", "jean@exemple.fr", "Pa55word!", tags=["dev"])
        assert entry.id
        found = vault.find("github.com")
        assert found is not None and found.username == "jean@exemple.fr"

        credentials = vault.credentials("github.com")
        assert credentials.password == "Pa55word!"
        assert "Pa55word!" not in repr(credentials)

        vault.update("github.com", password="NouveauMotDePasse#1")
        assert vault.find("github.com").password == "NouveauMotDePasse#1"

        assert vault.delete("github.com") is True
        assert vault.find("github.com") is None


def test_persistence_across_instances() -> None:
    with tmpdir("vault") as tmp:
        settings = make_settings(tmp)
        first = Vault(settings)
        first.create(PASSPHRASE)
        first.add("bank.example", "jean", "VraimentSecret#9")
        first.lock()

        second = Vault(settings)
        second.unlock(PASSPHRASE)
        entry = second.find("bank.example")
        assert entry is not None and entry.password == "VraimentSecret#9"


def test_login_url_survives_a_reload_and_vault_add_entry_exposes_it() -> None:
    """`login_url` s'écrit dès `vault_add_entry` et résiste au chiffrement/relecture."""
    with tmpdir("vault") as tmp:
        settings = make_settings(tmp)
        vault = Vault(settings)
        vault.create(PASSPHRASE)
        tools = ToolSet(build_vault_tools(vault))

        result = tools.dispatch(
            "vault_add_entry",
            {
                "service": "portail.example",
                "username": "jean",
                "password": "MotDePasse#1",
                "url": "https://portail.example/inscription",
                "login_url": "https://portail.example/compte/connexion",
            },
        )
        assert result.ok, result.error
        returned = result.data["entry"]
        assert returned["url"].endswith("/inscription"), returned
        assert returned["login_url"].endswith("/compte/connexion"), returned
        vault.lock()

        reloaded = Vault(settings)
        reloaded.unlock(PASSPHRASE)
        entry = reloaded.find("portail.example")
        assert entry is not None, "entrée perdue au rechargement"
        assert entry.url.endswith("/inscription"), entry.url
        assert entry.login_url.endswith("/compte/connexion"), entry.login_url
        assert reloaded.public_entry(entry)["login_url"].endswith("/compte/connexion")


def test_duplicate_entry_requires_overwrite() -> None:
    with tmpdir("vault") as tmp:
        vault = Vault(make_settings(tmp))
        vault.create(PASSPHRASE)
        vault.add("service.example", "jean", "un")
        try:
            vault.add("service.example", "jean", "deux")
        except VaultError as exc:
            assert exc.code == "duplicate_entry"
        else:
            raise AssertionError("un doublon doit être refusé")
        vault.add("service.example", "jean", "deux", overwrite=True)
        assert vault.find("service.example").password == "deux"


def test_secrets_are_masked_for_the_model() -> None:
    with tmpdir("vault") as tmp:
        vault = Vault(make_settings(tmp))
        vault.create(PASSPHRASE)
        vault.add("github.com", "jean", "SuperSecret#42")
        tools = ToolSet(build_vault_tools(vault))

        result = tools.dispatch("vault_get_entry", {"service": "github.com"})
        assert result.ok
        assert result.data["value"] == MASK

        revealed = tools.dispatch("vault_get_entry", {"service": "github.com", "reveal": True})
        assert revealed.data["value"] == "SuperSecret#42"
        # Le code Python y a accès, mais le contexte du modèle n'en reçoit rien.
        assert revealed.sensitive is True
        assert "SuperSecret#42" not in revealed.to_text()
        assert "SENSIBLE" in revealed.to_text()
        # La vue sérialisable masque aussi, sauf demande locale explicite.
        assert "SuperSecret#42" not in json.dumps(revealed.to_dict())
        assert revealed.to_dict(include_sensitive=True)["data"]["value"] == "SuperSecret#42"

        listing = tools.dispatch("vault_list_entries", {})
        assert listing.ok
        assert listing.data["entries"][0]["password"] == MASK
        assert json.dumps(listing.data) .count("SuperSecret#42") == 0


def test_add_and_update_never_return_the_password() -> None:
    with tmpdir("vault") as tmp:
        vault = Vault(make_settings(tmp))
        vault.create(PASSPHRASE)
        tools = ToolSet(build_vault_tools(vault))
        secret = "UltraSecret#2024"

        added = tools.dispatch(
            "vault_add_entry",
            {"service": "banque.example", "username": "jean", "password": secret},
        )
        assert added.ok
        assert added.data["entry"]["password"] == MASK
        assert secret not in json.dumps(added.data, default=str)
        assert secret not in added.to_text()

        changed = "Autre#Secret42"
        updated = tools.dispatch(
            "vault_update_entry", {"service": "banque.example", "password": changed}
        )
        assert updated.ok
        assert updated.data["entry"]["password"] == MASK
        assert changed not in json.dumps(updated.data, default=str)
        assert changed not in updated.to_text()

        # Le masquage ne doit pas cacher un échec d'écriture : la valeur est bien là.
        assert vault.credentials("banque.example").password == changed


def test_notes_cannot_smuggle_a_secret_to_the_model() -> None:
    with tmpdir("vault") as tmp:
        vault = Vault(make_settings(tmp))
        vault.create(PASSPHRASE)
        tools = ToolSet(build_vault_tools(vault))
        secret = "UltraSecret#2024"

        added = tools.dispatch(
            "vault_add_entry",
            {
                "service": "banque.example",
                "username": "jean",
                "password": secret,
                "notes": f"mot de passe de secours : {secret}",
            },
        )
        assert added.ok
        assert secret not in json.dumps(added.data, ensure_ascii=False)
        assert secret not in added.to_text()
        assert "notes masquées" in added.to_text()

        listed = tools.dispatch("vault_list_entries", {"query": "banque"})
        assert listed.ok
        assert secret not in listed.to_text()
        entry_view = listed.data["entries"][0]
        assert entry_view["notes"] == "[notes masquées]"

        updated = tools.dispatch(
            "vault_update_entry",
            {"service": "banque.example", "notes": f"autre copie : {secret}"},
        )
        assert updated.ok and secret not in updated.to_text()

        # Le contenu reste consultable localement, mais l'affichage masque le
        # secret même quand il est collé en clair dans les notes.
        entry = vault.find("banque.example")
        assert entry is not None and secret in entry.notes
        local = vault.public_entry(entry, reveal=True)
        assert secret not in local["notes"], local["notes"]
        assert MASK in local["notes"]
        assert secret not in json.dumps(vault.public_entry(entry), ensure_ascii=False)


def test_revealed_notes_mask_the_entry_secrets_and_api_keys() -> None:
    with tmpdir("vault") as tmp:
        vault = Vault(make_settings(tmp))
        vault.create(PASSPHRASE)
        tools = ToolSet(build_vault_tools(vault))
        password = "Pa$$-du-compte-42"
        api_key = "sk-abcdefghijklmnopqrstuvwx"
        notes = f"secours : {password} ; jeton {api_key} ; compte gratuit"

        added = tools.dispatch(
            "vault_add_entry",
            {
                "service": "notes.example",
                "username": "jean",
                "password": password,
                "notes": notes,
            },
        )
        assert added.ok

        entry = vault.find("notes.example")
        assert entry is not None
        assert entry.notes == notes, "la valeur stockée reste intacte"

        # Même sans Redactor fourni, les secrets de l'entrée sont retirés des
        # notes ; le champ `password` reste, lui, réservé à l'affichage local.
        bare = entry.public(reveal=True)
        assert bare["password"] == password
        assert password not in bare["notes"]
        # Le Redactor du module prend le relais : une clé API collée dans les
        # notes ne dépend pas de l'appelant pour être masquée.
        assert api_key not in bare["notes"], bare["notes"]
        assert "[MASQUÉ" in bare["notes"]
        assert "compte gratuit" in bare["notes"], "le texte utile est conservé"

        # Avec le Redactor du coffre, les motifs de clé API le sont aussi.
        shown = vault.public_entry(entry, reveal=True)
        assert password not in shown["notes"]
        assert api_key not in json.dumps(shown, ensure_ascii=False)
        assert MASK in shown["notes"]

        # Masquage par défaut inchangé.
        assert vault.public_entry(entry)["notes"] == "[notes masquées]"


def test_notes_are_redacted_before_return_without_an_explicit_redactor() -> None:
    """`public()` masque les secrets collés dans les notes, même sans Redactor passé."""
    with tmpdir("vault") as tmp:
        vault = Vault(make_settings(tmp))
        vault.create(PASSPHRASE)
        api_key = "sk-abcdefghijklmnopqrstuvwx"
        token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        notes = (
            "Compte de test ; clé API "
            + api_key
            + " ; jeton "
            + token
            + " ; question de sécurité : nom du chat ; ne pas supprimer."
        )
        assert len(notes) > 80
        vault.add("redaction.example", "jean", "MotDePasse#42", notes=notes)
        entry = vault.find("redaction.example")
        assert entry is not None

        # Vue par défaut : les notes ne sortent pas du tout.
        assert entry.public()["notes"] == "[notes masquées]"

        # Révélation locale : nettoyée même quand l'appelant omet `redactor`.
        shown = entry.public(reveal=True)
        assert api_key not in shown["notes"], shown["notes"]
        assert token not in shown["notes"], shown["notes"]
        assert "[MASQUÉ" in shown["notes"]
        assert "nom du chat" in shown["notes"], "le texte utile est conservé"
        assert len(shown["notes"]) > 80, "la note n'est pas tronquée à 80 caractères"

        # Et avec le Redactor du coffre, le résultat est identique.
        via_vault = vault.public_entry(entry, reveal=True)
        assert api_key not in via_vault["notes"]
        assert token not in via_vault["notes"]
        assert "nom du chat" in via_vault["notes"]


def test_vault_write_errors_never_echo_the_password() -> None:
    with tmpdir("vault") as tmp:
        vault = Vault(make_settings(tmp))
        vault.create(PASSPHRASE)
        tools = ToolSet(build_vault_tools(vault))
        secret = "UltraSecret#2024"

        tools.dispatch(
            "vault_add_entry",
            {"service": "banque.example", "username": "jean", "password": secret},
        )
        duplicate = tools.dispatch(
            "vault_add_entry",
            {"service": "banque.example", "username": "jean", "password": secret},
        )
        assert duplicate.ok is False and duplicate.error_code == "duplicate_entry"
        assert secret not in duplicate.to_text()

        bad_totp = "!!!pas-du-base32!!!"
        rejected = tools.dispatch(
            "vault_add_entry",
            {"service": "autre.example", "password": secret, "totp_secret": bad_totp},
        )
        assert rejected.ok is False and rejected.error_code == "invalid_totp_secret"
        assert secret not in rejected.to_text()
        assert bad_totp not in rejected.to_text()
        assert vault.find("autre.example") is None, "aucune entrée ne doit être créée"

        wrong_type = tools.dispatch(
            "vault_update_entry",
            {"service": "banque.example", "password": ["secret-dans-une-liste"]},
        )
        assert wrong_type.ok is False and wrong_type.error_code == "invalid_arguments"
        assert "secret-dans-une-liste" not in wrong_type.to_text()


def test_locked_vault_refuses_tool_calls() -> None:
    with tmpdir("vault") as tmp:
        tools = ToolSet(build_vault_tools(Vault(make_settings(tmp))))
        result = tools.dispatch("vault_status", {})
        assert result.ok and result.data["unlocked"] is False
        blocked = tools.dispatch("vault_services", {})
        assert blocked.ok is False and blocked.error_code == "vault_locked"


def test_totp_matches_rfc6238_vectors() -> None:
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp_code(secret, timestamp=59) == "287082"
    assert totp_code(secret, timestamp=1111111109) == "081804"
    assert totp_code(secret, timestamp=1234567890) == "005924"
    assert verify_totp(secret, totp_code(secret, timestamp=1234567890), timestamp=1234567890)
    assert 1 <= totp_remaining(timestamp=30.0) <= 30


def test_totp_tool_returns_fresh_code() -> None:
    with tmpdir("vault") as tmp:
        vault = Vault(make_settings(tmp))
        vault.create(PASSPHRASE)
        vault.add("mail.example", "jean", "x", totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
        code = vault.totp("mail.example")
        assert len(code["code"]) == 6 and code["code"].isdigit()
        assert code["seconds_remaining"] >= 1


def test_totp_result_is_withheld_from_model() -> None:
    with tmpdir("vault") as tmp:
        vault = Vault(make_settings(tmp))
        vault.create(PASSPHRASE)
        vault.add("mail.example", "jean", "x", totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
        tools = ToolSet(build_vault_tools(vault))

        result = tools.dispatch("vault_totp", {"service": "mail.example"})
        assert result.ok and result.sensitive is True
        code = result.data["code"]
        assert len(code) == 6 and code.isdigit()

        text = result.to_text()
        assert code not in text
        assert "code 2FA" in text and "mail.example" in text
        assert code not in json.dumps(result.to_dict())


def test_generated_password_is_withheld_from_model() -> None:
    with tmpdir("vault") as tmp:
        vault = Vault(make_settings(tmp))
        vault.create(PASSPHRASE)
        tools = ToolSet(build_vault_tools(vault))

        result = tools.dispatch("vault_generate_password", {"password_length": 24})
        assert result.ok and result.sensitive is True
        generated = result.data["password"]
        assert len(generated) == 24

        text = result.to_text()
        assert generated not in text
        assert "24 caractères" in text and "symboles" in text
        assert generated not in json.dumps(result.to_dict())
        # La valeur reste exploitable en local.
        assert result.to_dict(include_sensitive=True)["data"]["password"] == generated


def test_credential_creation_stores_without_leaking() -> None:
    with tmpdir("vault") as tmp:
        vault = Vault(make_settings(tmp))
        vault.create(PASSPHRASE)
        tools = ToolSet(build_vault_tools(vault))

        result = tools.dispatch(
            "vault_create_credential",
            {"service": "forum.example", "username": "jean", "password_length": 28},
        )
        assert result.ok and result.sensitive is True

        # Le mot de passe existe, fait la bonne longueur, et est bien stocké.
        stored = vault.credentials("forum.example")
        assert stored.username == "jean" and stored.password
        assert len(stored.password) == 28

        # Il n'apparaît nulle part dans ce que voit le modèle.
        text = result.to_text()
        assert stored.password not in text
        assert stored.password not in json.dumps(result.to_dict())
        assert stored.password not in json.dumps(result.data, default=str)
        assert MASK == result.data["entry"]["password"]

        # Mais le résumé porte le contexte utile.
        assert "forum.example" in text and "jean" in text
        assert "28 caractères" in text and "enregistré" in text

        # Et l'identifiant est réutilisable par la connexion automatique.
        again = vault.credentials("forum.example")
        assert again.password == stored.password


def test_password_generator() -> None:
    first = Vault.generate_password(24)
    second = Vault.generate_password(24)
    assert len(first) == 24 and first != second
    assert any(c.islower() for c in first) and any(c.isupper() for c in first)
    assert any(c.isdigit() for c in first)
    assert len(Vault.generate_password(500)) == 128  # borne haute appliquée


def test_audit_log_never_contains_secrets() -> None:
    with tmpdir("vault") as tmp:
        vault = Vault(make_settings(tmp))
        vault.create(PASSPHRASE)
        vault.add("banque.example", "jean", "UltraSecret#2024")
        vault.credentials("banque.example")
        records = vault.read_audit()
        blob = json.dumps(records, ensure_ascii=False)
        assert "UltraSecret#2024" not in blob
        assert any(record["action"] == "add" for record in records)
        assert any(record["action"] == "credentials" for record in records)


def test_backup_is_decryptable() -> None:
    with tmpdir("vault") as tmp:
        vault = Vault(make_settings(tmp))
        vault.create(PASSPHRASE)
        vault.add("site.example", "jean", "motdepasse")
        target = vault.backup(tmp / "sauvegardes")
        assert target.is_file()
        payload, _params = crypto.unseal(target.read_bytes(), PASSPHRASE)
        assert payload["entries"][0]["service"] == "site.example"


def test_backup_is_confined_to_allowed_roots() -> None:
    with tmpdir("vault") as tmp:
        settings = make_settings(tmp)
        vault = Vault(settings)
        vault.create(PASSPHRASE)

        outside = tmp.parent / "hors-racines.enc"
        try:
            vault.backup(outside)
        except PathNotAllowed:
            pass
        else:
            raise AssertionError("une sauvegarde hors racines doit être refusée")
        assert not outside.exists(), "aucun fichier ne doit être créé hors racines"

        assert vault.backup(settings.paths.data_dir).is_file()

        occupied = settings.paths.data_dir / "occupe.enc"
        occupied.write_bytes(b"contenu existant")
        try:
            vault.backup(occupied)
        except VaultError as exc:
            assert exc.code == "backup_exists"
        else:
            raise AssertionError("un fichier existant ne doit pas être écrasé")
        assert occupied.read_bytes() == b"contenu existant"


def test_change_passphrase() -> None:
    with tmpdir("vault") as tmp:
        settings = make_settings(tmp)
        vault = Vault(settings)
        vault.create(PASSPHRASE)
        vault.add("site.example", "jean", "motdepasse")
        vault.change_passphrase(PASSPHRASE, NEW_PASSPHRASE)
        assert not vault.unlocked

        reopened = Vault(settings)
        try:
            reopened.unlock(PASSPHRASE)
        except VaultAuthError:
            pass
        else:
            raise AssertionError("l'ancienne phrase ne doit plus fonctionner")

        reopened.unlock(NEW_PASSPHRASE)
        entry = reopened.find("site.example")
        assert entry is not None and entry.password == "motdepasse"


def test_auto_lock_on_inactivity() -> None:
    with tmpdir("vault") as tmp:
        settings = make_settings(tmp)
        settings.vault.auto_lock_minutes = 1
        vault = Vault(settings)
        vault.create(PASSPHRASE)
        vault._last_activity = time.time() - 61
        assert vault.check_timeout() is True
        assert vault.unlocked is False


def test_service_normalization() -> None:
    assert normalize_service("https://www.GitHub.com/login?x=1") == "github.com"
    assert normalize_service("  Example.ORG/  ") == "example.org"
    assert normalize_service("") == ""


def test_unknown_tool_is_reported() -> None:
    with tmpdir("vault") as tmp:
        tools = ToolSet(build_vault_tools(Vault(make_settings(tmp))))
        result = tools.dispatch("vault_inexistant", {})
        assert result.ok is False and result.error_code == "unknown_tool"


if __name__ == "__main__":
    sys.exit(run(globals()))
