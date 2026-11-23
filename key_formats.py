"""Formats des clés d'API des fournisseurs d'IA — source unique.

Un préfixe de clé sert à quatre choses : deviner le fournisseur
(`core.llm_router.detect_provider`), refuser la clé d'un autre fournisseur
(`config.deepseek_key_problem`), la masquer avant journalisation
(`security.redaction`) et la documenter (`docs/OPERATIONS.md`). Recopié quatre
fois, il finit toujours par diverger : les clés Gemini `AQ.` ont été reconnues
par le routeur des semaines avant de l'être par le masquage, donc elles
sortaient en clair dans les journaux.

Ce module est volontairement sans dépendance — ni projet, ni tiers — pour
rester importable par `config.py` (stdlib seule) comme par `security/` et
`core/`. Ajouter un fournisseur ici suffit ; `tests/test_key_formats.py`
vérifie que chaque consommateur le prend en compte, et refuse un fournisseur
ajouté sans exemple de clé.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "KEY_FORMATS",
    "KEY_LIKE",
    "KeyFormat",
    "all_prefixes",
    "attribution",
    "foreign_prefixes",
    "key_patterns",
    "provider_prefixes",
]


@dataclass(frozen=True, slots=True)
class KeyFormat:
    """Un fournisseur et la forme de ses clés."""

    slug: str
    label: str
    #: Variable d'environnement qui reçoit la clé (`""` si aucune : Ollama).
    env_var: str = ""
    #: Préfixes signature. Le plus long gagne : `sk-ant-` désigne Anthropic
    #: avant que `sk-` ne désigne OpenAI.
    prefixes: tuple[str, ...] = ()
    #: Longueur minimale du corps pour le masquage. Trop court, le motif
    #: confondrait une clé avec une expression courante (« AQ. » en prose).
    mask_body_min: int = 16


#: Registre des fournisseurs, dans l'ordre d'affichage du routeur.
KEY_FORMATS: tuple[KeyFormat, ...] = (
    KeyFormat(
        "openai", "OpenAI", "OPENAI_API_KEY",
        ("sk-proj-", "sk-svcacct-", "sk-"),
    ),
    KeyFormat("anthropic", "Anthropic", "ANTHROPIC_API_KEY", ("sk-ant-",)),
    #: Les clés DeepSeek sont `sk-` suivi de 32 hexadécimaux : le préfixe seul
    #: ne les distingue pas d'OpenAI, `core.llm_router.detect_provider` tranche
    #: sur la forme du corps. `sk-ds-` couvre les clés déjà préfixées ainsi.
    KeyFormat("deepseek", "DeepSeek", "DEEPSEEK_API_KEY", ("sk-ds-",)),
    KeyFormat(
        "gemini", "Google Gemini", "GEMINI_API_KEY",
        ("AIza", "AQ."), mask_body_min=20,
    ),
    KeyFormat("groq", "Groq", "GROQ_API_KEY", ("gsk_",)),
    KeyFormat("openrouter", "OpenRouter", "OPENROUTER_API_KEY", ("sk-or-",)),
    KeyFormat("mistral", "Mistral", "MISTRAL_API_KEY", ("sk-mistral-",)),
    KeyFormat("ollama", "Ollama (local)"),
)

#: Préfixes qu'un fournisseur accepte lui-même alors qu'un autre les revendique
#: aussi : les citer reviendrait à refuser toutes ses clés.
_SHARED_PREFIXES = frozenset({"sk-"})

#: Corps d'une clé : alphanumériques, tirets et soulignés.
_KEY_BODY = r"[A-Za-z0-9_\-]"
_KEY_END = r"(?![A-Za-z0-9_\-])"


def provider_prefixes(slug: str) -> tuple[str, ...]:
    """Préfixes signature d'un fournisseur ; vide s'il n'en a pas."""
    for key_format in KEY_FORMATS:
        if key_format.slug == slug:
            return key_format.prefixes
    return ()


def attribution() -> tuple[tuple[str, str], ...]:
    """(préfixe, slug) du plus spécifique au plus général, pour la détection.

    Le tri par longueur décroissante garantit qu'une clé `sk-mistral-…` est
    attribuée à Mistral, et non à OpenAI par son simple `sk-`.
    """
    pairs = [
        (prefix, key_format.slug)
        for key_format in KEY_FORMATS
        for prefix in key_format.prefixes
    ]
    return tuple(sorted(pairs, key=lambda pair: (-len(pair[0]), pair[1])))


def all_prefixes() -> tuple[str, ...]:
    """Tous les préfixes connus, du plus long au plus court, sans doublon."""
    return tuple(dict.fromkeys(prefix for prefix, _slug in attribution()))


def foreign_prefixes(slug: str) -> tuple[tuple[str, str], ...]:
    """(préfixe, libellé) des fournisseurs qu'une clé de `slug` ne peut pas porter.

    Sert aux messages de refus : « ce préfixe appartient à Groq, pas à
    DeepSeek ». Les préfixes partagés sont écartés, sinon toutes les clés du
    fournisseur visé seraient refusées.
    """
    own = provider_prefixes(slug)
    pairs = [
        (prefix, key_format.label)
        for key_format in KEY_FORMATS
        if key_format.slug != slug
        for prefix in key_format.prefixes
        if prefix not in _SHARED_PREFIXES and prefix not in own
    ]
    return tuple(sorted(pairs, key=lambda pair: (-len(pair[0]), pair[0])))


def key_patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """(libellé, motif) de masquage, un par préfixe, du plus long au plus court.

    Un motif par préfixe plutôt qu'un par fournisseur : `sk-or-…` est étiqueté
    OpenRouter avant que le `sk-` générique d'OpenAI ne s'en empare. Les
    clés dont le corps est plus court que `mask_body_min` sont laissées
    tranquilles, pour ne pas masquer « AQ. » ou « sk- » dans une phrase.
    """
    minimums = {key_format.slug: key_format.mask_body_min for key_format in KEY_FORMATS}
    patterns: list[tuple[str, re.Pattern[str]]] = []
    for prefix, slug in attribution():
        patterns.append(
            (
                f"{slug}_key",
                re.compile(
                    rf"\b{re.escape(prefix)}{_KEY_BODY}"
                    rf"{{{minimums[slug]},256}}{_KEY_END}"
                ),
            )
        )
    return tuple(patterns)


def _lenient(prefix: str) -> str:
    """Motif d'un préfixe tolérant sur son séparateur final.

    Une clé recopiée à la main perd parfois son tiret ; les journaux et les
    messages d'erreur, eux, ne doivent pas la laisser passer pour autant.
    """
    core = prefix.rstrip("-_")
    escaped = re.escape(core)
    return escaped if core == prefix else escaped + r"[-_]?"


def _build_key_like() -> re.Pattern[str]:
    alternation = "|".join(_lenient(prefix) for prefix in all_prefixes())
    return re.compile(rf"(?:{alternation}){_KEY_BODY}{{6,}}")


#: Reconnaissance large destinée aux traces du routeur : il suffit d'une
#: apparence de clé pour la masquer, le libellé importe peu.
KEY_LIKE = _build_key_like()
