"""Conformité des consommateurs au registre `key_formats.py`.

Un préfixe de clé sert à deviner le fournisseur, refuser la clé d'un autre
fournisseur et la masquer dans les journaux. Recopiée dans chaque module, cette
connaissance a déjà divergé : les clés Gemini `AQ.` étaient détectées par le
routeur mais sortaient en clair dans les journaux. Ces tests parcourent le
registre, si bien qu'un fournisseur ajouté sans mettre à jour un consommateur —
ou sans exemple de clé ci-dessous — échoue.
"""

from __future__ import annotations

import sys
from pathlib import Path

from support import run  # noqa: I001

import config  # noqa: E402
from core.llm_router import _scrub, detect_provider  # noqa: E402
from key_formats import KEY_FORMATS  # noqa: E402
from security.redaction import redact  # noqa: E402

#: Une clé plausible par fournisseur du registre. En ajouter un sans compléter
#: ce tableau échoue : c'est le rappel de mettre à jour `.env.example`, la
#: documentation et les tests de masquage.
SAMPLES: dict[str, str] = {
    "openai": "sk-proj-AbCdEf0123456789AbCdEf0123456789",
    "anthropic": "sk-ant-api03-AbCdEf0123456789",
    "deepseek": "sk-" + "0123456789abcdef" * 2,
    "gemini": "AQ.AbCdEf0123456789AbCdEf0123456789",
    "groq": "gsk_AbCdEf0123456789AbCdEf",
    "openrouter": "sk-or-v1-AbCdEf0123456789AbCdEf",
    "mistral": "sk-mistral-AbCdEf0123456789AbCdEf",
}

_LABELS = {key_format.slug: key_format.label for key_format in KEY_FORMATS}
_ENV_VARS = {key_format.slug: key_format.env_var for key_format in KEY_FORMATS}


def test_chaque_fournisseur_a_un_exemple_de_cle() -> None:
    attendus = {key_format.slug for key_format in KEY_FORMATS if key_format.prefixes}
    assert set(SAMPLES) == attendus


def test_chaque_cle_est_masquee_dans_un_texte_libre() -> None:
    for slug, key in SAMPLES.items():
        masque = redact(f"erreur HTTP 401 pour la clé {key} rejetée")
        assert key not in masque, (slug, masque)


def test_chaque_cle_est_masquee_dans_les_traces_du_routeur() -> None:
    for slug, key in SAMPLES.items():
        assert key not in _scrub(f"clé refusée : {key}"), slug


def test_chaque_cle_est_attribuee_a_son_fournisseur() -> None:
    for slug, key in SAMPLES.items():
        assert detect_provider(key) == slug, slug


def test_chaque_cle_est_acceptee_par_le_preflight_de_son_fournisseur() -> None:
    for slug, key in SAMPLES.items():
        message = config.api_key_problem(key, provider=slug, origin="X_API_KEY")
        assert message == "", (slug, message)


def test_une_cle_d_un_autre_fournisseur_est_nommee_pour_deepseek() -> None:
    for slug, key in SAMPLES.items():
        if slug == "deepseek":
            continue
        message = config.deepseek_key_problem(key, origin="DEEPSEEK_API_KEY")
        assert _LABELS[slug] in message, (slug, message)


def test_les_variables_d_env_de_l_assistant_viennent_du_registre() -> None:
    from ui.setup_wizard import _PROVIDER_SETTINGS

    for slug, valeurs in _PROVIDER_SETTINGS.items():
        if slug not in _ENV_VARS:  # « custom » : service libre, hors registre
            continue
        assert valeurs[0] == _ENV_VARS[slug], slug


def test_le_gabarit_env_documente_chaque_fournisseur() -> None:
    example = (Path(config.__file__).resolve().parent / ".env.example").read_text(
        encoding="utf-8"
    )
    manquantes = [
        env_var
        for env_var in _ENV_VARS.values()
        if env_var and f"{env_var}=" not in example
    ]
    assert manquantes == [], manquantes


if __name__ == "__main__":
    sys.exit(run(globals()))
