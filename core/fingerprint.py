"""Empreinte légère d'un fichier lu, pour détecter une dérive à la reprise.

Une trace dit *quel* fichier a été lu et avec quels arguments, jamais son
contenu : au moment de reprendre, l'agent ne peut donc pas savoir si le fichier
a changé entre-temps. Une empreinte de quelques dizaines d'octets (taille +
SHA-256 des premiers 64 Kio) suffit à répondre : identique, modifié ou disparu.

Ce n'est **pas** une preuve d'intégrité : l'empreinte ne couvre que le début du
fichier et ne sert qu'à dire au modèle « ne te fie pas au contenu d'origine ».
Une écriture au-delà des 64 Kio change la taille et est donc vue ; une
réécriture à taille constante plus loin dans le fichier peut échapper à
l'empreinte, d'où la consigne de toujours relire l'observation fraîche.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "FILE_READ_TOOLS",
    "FINGERPRINT_MAX_BYTES",
    "IDENTICAL",
    "MISSING",
    "MODIFIED",
    "compare_fingerprint",
    "drift_label",
    "file_fingerprint",
    "read_fingerprint",
]

#: Longueur d'en-tête hachée : assez pour distinguer deux versions, assez peu
#: pour rester négligeable face au contenu réellement lu.
FINGERPRINT_MAX_BYTES = 64 * 1024

#: Outils dont l'argument `path` désigne un fichier lu. Les autres lectures
#: (web, index, coffre) n'ont pas d'état local comparable.
FILE_READ_TOOLS = frozenset({"fs_read_text", "fs_hash", "ocr_image"})

IDENTICAL = "identical"
MODIFIED = "modified"
MISSING = "missing"

_LABELS = {
    IDENTICAL: "contenu identique a la lecture d'origine",
    MODIFIED: "contenu modifie depuis la trace",
    MISSING: "fichier disparu depuis la trace",
}


def drift_label(status: str) -> str:
    """Libellé transmissible au modèle ; vide si aucune comparaison n'a eu lieu."""
    return _LABELS.get(str(status), "")


def file_fingerprint(
    path: str | os.PathLike[str], *, max_bytes: int = FINGERPRINT_MAX_BYTES
) -> dict[str, Any] | None:
    """Empreinte de `path`, ou `None` s'il est absent ou illisible.

    Le chemin est résolu : une reprise lancée depuis un autre répertoire compare
    le même fichier, pas un homonyme relatif au répertoire courant.
    """
    target = Path(os.path.expandvars(str(path))).expanduser()
    try:
        resolved = target.resolve()
        if not resolved.is_file():
            return None
        with resolved.open("rb") as handle:
            head = handle.read(max(1, int(max_bytes)))
        size = resolved.stat().st_size
    except OSError:
        return None
    return {
        "path": str(resolved),
        "size": int(size),
        "head_bytes": len(head),
        "head_sha256": hashlib.sha256(head).hexdigest(),
    }


def read_fingerprint(
    tool: str, arguments: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Empreinte à enregistrer pour un appel d'outil, si c'est une lecture de fichier."""
    if tool not in FILE_READ_TOOLS:
        return None
    raw = arguments.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return file_fingerprint(raw)


def compare_fingerprint(
    recorded: Any, *, max_bytes: int = FINGERPRINT_MAX_BYTES
) -> tuple[str, dict[str, Any] | None]:
    """Compare l'empreinte d'origine au fichier actuel.

    Retourne `(statut, empreinte actuelle)` ; statut vide quand la trace ne
    contient aucune empreinte exploitable (trace antérieure, argument absent) :
    dans ce cas la reprise ne conclut rien plutôt que de supposer.
    """
    if not isinstance(recorded, Mapping):
        return "", None
    path = recorded.get("path")
    expected_size = recorded.get("size")
    expected_digest = recorded.get("head_sha256")
    if (
        not isinstance(path, str)
        or not path.strip()
        or not isinstance(expected_size, int)
        or not isinstance(expected_digest, str)
        or not expected_digest
    ):
        return "", None
    current = file_fingerprint(path, max_bytes=max_bytes)
    if current is None:
        return MISSING, None
    if current["size"] != expected_size or current["head_sha256"] != expected_digest:
        return MODIFIED, current
    return IDENTICAL, current
