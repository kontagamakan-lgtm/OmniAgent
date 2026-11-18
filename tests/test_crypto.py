"""Tests des primitives cryptographiques."""

from __future__ import annotations

import sys

from support import run  # noqa: I001

from security import crypto

# Paramètres rapides : les tests ne mesurent pas la résistance au bruteforce.
FAST = crypto.KdfParams(kdf="argon2id", time_cost=1, memory_kib=8192, parallelism=1)
FAST_SCRYPT = crypto.KdfParams(kdf="scrypt", scrypt_n=16384, scrypt_r=8, scrypt_p=1)
PAYLOAD = {"format": "omniagent.vault", "entries": [{"service": "github.com", "password": "s3cret"}]}


def test_argon2id_derivation_is_deterministic() -> None:
    if not crypto.is_argon2_available():
        return
    salt = b"0123456789abcdef"
    first = crypto.derive_key("phrase-secrete", salt, FAST)
    second = crypto.derive_key("phrase-secrete", salt, FAST)
    other = crypto.derive_key("autre-phrase", salt, FAST)
    assert first == second
    assert first != other
    assert len(first) == crypto.KEY_LEN


def test_scrypt_derivation_is_deterministic() -> None:
    salt = b"fedcba9876543210"
    first = crypto.derive_key("phrase-secrete", salt, FAST_SCRYPT)
    second = crypto.derive_key("phrase-secrete", salt, FAST_SCRYPT)
    assert first == second
    assert len(first) == crypto.KEY_LEN


def test_seal_unseal_roundtrip() -> None:
    blob = crypto.seal(PAYLOAD, "phrase-secrete", FAST)
    payload, params = crypto.unseal(blob, "phrase-secrete")
    assert payload == PAYLOAD
    assert params.kdf == "argon2id"


def test_wrong_passphrase_is_rejected() -> None:
    blob = crypto.seal(PAYLOAD, "phrase-secrete", FAST)
    try:
        crypto.unseal(blob, "mauvaise-phrase")
    except crypto.AuthenticationError:
        return
    raise AssertionError("une mauvaise phrase secrète doit échouer")


def test_tampered_ciphertext_is_detected() -> None:
    blob = bytearray(crypto.seal(PAYLOAD, "phrase-secrete", FAST))
    blob[-1] ^= 0x01
    try:
        crypto.unseal(bytes(blob), "phrase-secrete")
    except crypto.AuthenticationError:
        return
    raise AssertionError("une altération du chiffré doit invalider le tag GCM")


def test_tampered_salt_is_detected() -> None:
    blob = bytearray(crypto.seal(PAYLOAD, "phrase-secrete", FAST))
    header = crypto.parse_header(bytes(blob))
    salt_offset = len(header.aad) - crypto.NONCE_LEN - crypto.SALT_LEN
    blob[salt_offset] ^= 0xFF
    try:
        crypto.unseal(bytes(blob), "phrase-secrete")
    except crypto.AuthenticationError:
        return
    raise AssertionError("le sel est couvert par l'AAD : l'altération doit être détectée")


def test_weakened_kdf_parameters_are_detected() -> None:
    """Remplacer les paramètres KDF par des paramètres plus faibles casse l'AAD."""
    blob = crypto.seal(PAYLOAD, "phrase-secrete", FAST)
    header = crypto.parse_header(blob)
    weakened = crypto.KdfParams(kdf="argon2id", time_cost=1, memory_kib=8, parallelism=1)
    forged = crypto.build_header(weakened, header.salt, header.nonce) + header.ciphertext
    try:
        crypto.unseal(forged, "phrase-secrete")
    except (crypto.AuthenticationError, crypto.FormatError):
        return
    raise AssertionError("un en-tête forgé doit être rejeté")


def test_header_parsing_exposes_expected_fields() -> None:
    blob = crypto.seal(PAYLOAD, "phrase-secrete", FAST)
    header = crypto.parse_header(blob)
    assert header.params.memory_kib == 8192
    assert len(header.salt) == crypto.SALT_LEN
    assert len(header.nonce) == crypto.NONCE_LEN
    assert blob.startswith(crypto.MAGIC)
    assert blob[len(header.aad):] == header.ciphertext


def test_truncated_and_foreign_containers_are_rejected() -> None:
    for candidate in (b"", b"OMNIVLT1", b"pas un coffre du tout"):
        try:
            crypto.parse_header(candidate)
        except crypto.FormatError:
            continue
        raise AssertionError(f"conteneur invalide accepté : {candidate!r}")


def test_reseal_changes_passphrase() -> None:
    blob = crypto.seal(PAYLOAD, "ancienne-phrase", FAST)
    rotated = crypto.reseal(blob, "ancienne-phrase", "nouvelle-phrase")
    payload, _params = crypto.unseal(rotated, "nouvelle-phrase")
    assert payload == PAYLOAD
    try:
        crypto.unseal(rotated, "ancienne-phrase")
    except crypto.AuthenticationError:
        return
    raise AssertionError("l'ancienne phrase ne doit plus fonctionner")


def test_open_with_passphrase_rejects_non_dict_payload() -> None:
    """Un conteneur authentique dont le clair n'est pas un objet est refusé."""
    blob = crypto.seal(["pas", "un", "objet"], "phrase-secrete")  # type: ignore[arg-type]
    openers = (
        crypto.open_with_passphrase,
        lambda data, phrase: crypto.unseal(data, phrase)[0],
    )
    for opener in openers:
        try:
            opener(blob, "phrase-secrete")
        except crypto.FormatError:
            continue
        raise AssertionError("un contenu déchiffré non-objet doit lever FormatError")


def test_open_with_passphrase_returns_reusable_key() -> None:
    blob = crypto.seal(PAYLOAD, "phrase-secrete", FAST)
    payload, key, params, salt = crypto.open_with_passphrase(blob, "phrase-secrete")
    assert payload == PAYLOAD
    rewritten = crypto.seal_with_key(payload, key, params, salt)
    again, _params = crypto.unseal(rewritten, "phrase-secrete")
    assert again == PAYLOAD
    assert rewritten != blob  # nonce différent à chaque écriture


def test_key_based_encryption_with_aad() -> None:
    key = b"k" * crypto.KEY_LEN
    sealed = crypto.encrypt_with_key(key, b"message", aad=b"contexte")
    assert crypto.decrypt_with_key(key, sealed, aad=b"contexte") == b"message"
    try:
        crypto.decrypt_with_key(key, sealed, aad=b"autre-contexte")
    except crypto.AuthenticationError:
        return
    raise AssertionError("l'AAD doit être authentifiée")


def test_kdf_parameter_bounds_are_enforced() -> None:
    for params in (
        crypto.KdfParams(kdf="argon2id", memory_kib=2_000_000),
        crypto.KdfParams(kdf="argon2id", time_cost=99),
        crypto.KdfParams(kdf="scrypt", scrypt_n=1000),
        crypto.KdfParams(kdf="scrypt", scrypt_n=40000),
    ):
        try:
            params.ensure_sane()
        except crypto.FormatError:
            continue
        raise AssertionError(f"paramètres hors bornes acceptés : {params}")


def test_passphrase_assessment() -> None:
    weak_score, weak_issues = crypto.assess_passphrase("azerty123")
    assert weak_score <= 1 and weak_issues
    strong_score, strong_issues = crypto.assess_passphrase("Correct-Horse-Battery-9!")
    assert strong_score >= 3 and not strong_issues


def test_wipe_zeroes_buffer() -> None:
    buffer = bytearray(b"secret-key-material")
    crypto.wipe(buffer)
    assert set(buffer) == {0}


def test_argon2_availability_flag() -> None:
    assert isinstance(crypto.is_argon2_available(), bool)


if __name__ == "__main__":
    sys.exit(run(globals()))
