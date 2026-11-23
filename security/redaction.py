"""Masquage des secrets avant journalisation ou affichage.

Le module couvre deux besoins distincts :

1. **Texte libre** (lignes de log, réponses d'outils) : les motifs connus de
   clés API, jetons, en-têtes d'autorisation, URL authentifiées et blocs de
   clés privées sont remplacés par ``[MASQUÉ:<type>]``.
2. **Structures** (dict/list issus du JSON d'un outil) : tout champ dont le
   nom évoque un secret est remplacé, quel que soit son contenu.

Les expressions régulières utilisent des quantificateurs bornés : pas de
retour arrière catastrophique possible sur une entrée hostile.
"""

from __future__ import annotations

import re
from functools import partial
from typing import Any, Iterable, Mapping

from key_formats import key_patterns

__all__ = [
    "DEFAULT_REPLACEMENT",
    "SECRET_KEY_NAMES",
    "Redactor",
    "is_secret_key",
    "redact",
    "redactor",
]

DEFAULT_REPLACEMENT = "[MASQUÉ:{label}]"

# Noms de champs considérés comme secrets (comparaison normalisée).
SECRET_KEY_NAMES: frozenset[str] = frozenset(
    {
        "password", "passwd", "pwd", "pass", "motdepasse", "mot_de_passe",
        "secret", "client_secret", "clientsecret", "app_secret",
        "token", "access_token", "refresh_token", "id_token", "bearer",
        "authorization", "auth", "auth_header", "proxy-authorization",
        "api_key", "apikey", "api-key", "x-api-key",
        "private_key", "private_key_pem", "secret_key", "signing_key",
        "cookie", "cookies", "set-cookie", "session", "sessionid",
        "csrf", "csrf_token", "xsrf", "xsrf_token",
        "totp_secret", "otp_secret", "mfa_secret", "recovery_code",
        "connection_string", "dsn", "passphrase", "credential", "credentials",
        "master_key",
        "access_key", "secret_access_key", "sas_token",
    }
)

# (libellé, motif) — l'ordre compte. La règle « clé = valeur » passe tôt pour
# masquer d'un bloc les en-têtes d'autorisation (`Bearer`, `Basic`…) ; les
# lookaheads négatifs `(?!\[MASQUÉ)` rendent l'opération idempotente : repasser
# le nettoyeur sur un texte déjà masqué ne dégrade pas le résultat.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN[ A-Z]{0,32}PRIVATE KEY-----[\s\S]{0,16384}?-----END[ A-Z]{0,32}PRIVATE KEY-----"
        ),
    ),
    (
        "assigned_secret",
        re.compile(
            # `(?<![A-Za-z0-9])` et non `\b` : dans `GROQ_API_KEY=…`, le
            # souligné est un caractère de mot, donc `\b` interdisait le
            # masquage et la clé partait en clair dans les journaux.
            r"(?i)(?<![A-Za-z0-9])(password|passwd|pwd|secret|token|"
            r"api[_-]?key|apikey|access[_-]?key|client[_-]?secret|authorization)\b"
            r"(\s*[:=]\s*)"
            r"(?:(?:bearer|basic|token|digest)\s+)?"
            r"(?!\[MASQUÉ)"
            r"(\"[^\"]{1,2048}\"|'[^']{1,2048}'|[^\s,;&\"'}\]]{1,2048})"
        ),
    ),
    (
        "query_secret",
        re.compile(
            r"(?i)([?&;](?:token|access_token|api_key|apikey|key|sig|"
            r"signature|password|secret|auth)=)(?!\[MASQUÉ)[^&\s#\"']{1,2048}"
        ),
    ),
    (
        "cookie_header",
        re.compile(r"(?i)\b(cookie|set-cookie)(\s*:\s*)(?!\[MASQUÉ)([^\r\n]{4,4096})"),
    ),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_\-]{8,4096}\.[A-Za-z0-9_\-]{8,4096}"
            r"\.[A-Za-z0-9_\-]{8,4096}(?![A-Za-z0-9_\-])"
        ),
    ),
    # Pas de `\b` final : un tiret ou un souligné final est un caractère non
    # mot, donc la limite de mot ne peut pas être satisfaite et la clé
    # échapperait au masquage. Le lookahead interdit seulement de couper un
    # jeton plus long.
    # Un motif par préfixe de fournisseur d'IA, du plus spécifique au plus
    # général, générés depuis `key_formats.py` : aucune clé d'un fournisseur
    # géré ne peut plus manquer au masquage.
    *key_patterns(),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,255}\b")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "slack_token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,255}(?![A-Za-z0-9\-])"),
    ),
    (
        # Numero de carte : 16 chiffres, espaces ou tirets tolerés. Un motif
        # plus large masquerait des identifiants numeriques legitimes.
        "card_number",
        re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{4}(?!\d)"),
    ),
    ("bearer", re.compile(r"(?i)\b(Bearer)\s+([A-Za-z0-9._\-+/=]{8,2048})")),
    (
        "url_credentials",
        re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{1,15}://)[^/\s:@]{1,256}:[^/\s@]{1,256}@"),
    ),
)


def is_secret_key(name: str) -> bool:
    """Vrai si le nom de champ suggère une valeur secrète."""
    normalized = re.sub(r"[\s\-.]+", "_", str(name).strip().lower())
    if normalized in SECRET_KEY_NAMES:
        return True
    if normalized.endswith(("_password", "_secret", "_token", "_key")):
        return True
    return False


class Redactor:
    """Nettoyeur de secrets réutilisable et configurable."""

    def __init__(
        self,
        extra_patterns: Iterable[tuple[str, str]] = (),
        *,
        enabled: bool = True,
        replacement: str = DEFAULT_REPLACEMENT,
        mask_short_values: bool = True,
    ) -> None:
        self.enabled = enabled
        self.replacement = replacement
        self.mask_short_values = mask_short_values
        self._patterns: list[tuple[str, re.Pattern[str]]] = list(_PATTERNS)
        for label, pattern in extra_patterns:
            self._patterns.append((label, re.compile(pattern)))

    # -- texte libre --------------------------------------------------------

    def redact(self, text: Any) -> str:
        """Retourne `text` avec tous les secrets connus masqués."""
        if not self.enabled or text is None:
            return "" if text is None else str(text)
        result = str(text)
        for label, pattern in self._patterns:
            # `partial` fixe le libellé une fois pour toutes : la lambda
            # équivalente obligeait mypy à deviner le type de ses paramètres.
            result = pattern.sub(partial(self._substitute, label), result)
        return result

    def _substitute(self, label: str, match: re.Match[str]) -> str:
        mask = self.replacement.format(label=label)
        # On conserve les séparateurs structurels pour que le résultat reste
        # lisible : `password: ***` plutôt que `***`.
        if label == "assigned_secret":
            return f"{match.group(1)}{match.group(2)}{mask}"
        if label == "query_secret":
            return f"{match.group(1)}{mask}"
        if label == "url_credentials":
            return f"{match.group(1)}{mask}@"
        if label == "bearer":
            return f"{match.group(1)} {mask}"
        if label == "cookie_header":
            return f"{match.group(1)}{match.group(2)}{mask}"
        return mask
    # -- structures ---------------------------------------------------------

    def redact_data(self, value: Any, *, _depth: int = 0) -> Any:
        """Copie récursivement une structure en masquant les champs secrets."""
        if _depth > 32:
            return "[PROFONDEUR MAX]"
        if not self.enabled:
            return value
        if isinstance(value, Mapping):
            output: dict[Any, Any] = {}
            for key, item in value.items():
                if is_secret_key(str(key)) and not isinstance(item, (dict, list)):
                    output[key] = self.replacement.format(label="secret")
                elif isinstance(item, str) and is_secret_key(str(key)):
                    output[key] = self.replacement.format(label="secret")
                else:
                    output[key] = self.redact_data(item, _depth=_depth + 1)
            return output
        if isinstance(value, (list, tuple, set)):
            return [self.redact_data(item, _depth=_depth + 1) for item in value]
        if isinstance(value, str):
            return self.redact(value)
        return value

    def __repr__(self) -> str:  # pragma: no cover - confort de débogage
        return f"<Redactor patterns={len(self._patterns)} enabled={self.enabled}>"


# Instance partagée : suffisante pour les logs applicatifs.
redactor = Redactor()


def redact(text: Any) -> str:
    """Fonction module-level adossée au Redactor partagé."""
    return redactor.redact(text)
