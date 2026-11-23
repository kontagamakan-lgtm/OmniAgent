"""Épuration rétroactive des journaux écrits avant le masquage.

`security/redaction.py` masque les clés au moment de l'écriture : une clé
consignée par une version antérieure à la 1.1.3 reste en clair sur le disque,
donc compromise. `scrub_tree` relit les journaux, compte les lignes à masquer
et ne réécrit que celles qui changent.

Périmètre volontairement étroit : traces et journaux (`paths.log_dir`), refus,
objectifs et propositions de l'autonomie (`data/autonomy`). Le journal d'audit
(`data/audit/chain.jsonl`) est exclu : le réécrire romprait la chaîne
d'empreintes qui sert justement à détecter une réécriture. Les notes de mémoire
et les fichiers de l'utilisateur ne sont pas des journaux et restent intacts
(voir la décision documentée dans `core/memory.py`).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from config import Settings
from core.autonomy import autonomy_dir
from security.redaction import redact

__all__ = ["FileReport", "ScrubReport", "scrub_file", "scrub_tree", "targets"]

#: Fichiers texte relus, rotations comprises (`trace.jsonl.1`, `omniagent.log.3`).
_TEXT_NAME = re.compile(r"\.(?:jsonl|log|json|txt)(?:\.\d+)?$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FileReport:
    """Un journal relu et le nombre de lignes qui portent une clé."""

    path: Path
    lines: int


@dataclass(frozen=True, slots=True)
class ScrubReport:
    """Ce qui a été relu, ce qui fuit encore, et si le masquage a été écrit."""

    scanned: int
    files: tuple[FileReport, ...]
    applied: bool

    @property
    def leaked_files(self) -> int:
        return len(self.files)

    @property
    def leaked_lines(self) -> int:
        return sum(item.lines for item in self.files)

    def format(self) -> str:
        if not self.files:
            return f"{self.scanned} fichier(s) relu(s) : aucune clé en clair."
        lines = [
            f"{self.scanned} fichier(s) relu(s), {self.leaked_files} avec des clés "
            f"en clair ({self.leaked_lines} ligne(s)) :"
        ]
        lines.extend(f"  {item.path}  ::  {item.lines} ligne(s)" for item in self.files)
        if self.applied:
            lines.append(
                "Masquage écrit ; relancer l'aperçu pour vérifier qu'il ne reste rien."
            )
        else:
            lines.append(
                "Aperçu seulement : relancer avec --redact-logs-apply pour masquer."
            )
        return "\n".join(lines)


def targets(settings: Settings) -> list[Path]:
    """Dossiers relus : journaux, puis données de l'autonomie.

    Aucun autre dossier de `data/` n'est touché : la mémoire projet et le
    journal d'audit ne sont pas des journaux de diagnostic.
    """
    return [
        path
        for path in (settings.paths.log_dir, autonomy_dir(settings))
        if path.is_dir()
    ]


def _text_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and _TEXT_NAME.search(path.name)
    )


def scrub_file(path: Path, *, apply: bool) -> int:
    """Masque les clés d'un fichier texte ; retourne ses lignes touchées.

    Un fichier illisible ou non UTF-8 est laissé tel quel : le masquage ne doit
    pas corrompre un journal binaire ni un fichier d'un autre encodage.
    """
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    cleaned = redact(text)
    if cleaned == text:
        return 0

    changed = sum(
        1
        for before, after in zip(text.splitlines(), cleaned.splitlines(), strict=False)
        if before != after
    )
    if not changed:  # différence en fin de fichier seulement
        changed = 1
    if apply:
        temporary = path.with_name(path.name + ".scrub.tmp")
        try:
            temporary.write_text(cleaned, encoding="utf-8", newline="")
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
    return changed


def scrub_tree(roots: Iterable[Path], *, apply: bool) -> ScrubReport:
    """Relit chaque journal sous `roots` et masque, sur demande, les clés."""
    scanned = 0
    files: list[FileReport] = []
    for root in roots:
        for path in _text_files(root):
            scanned += 1
            lines = scrub_file(path, apply=apply)
            if lines:
                files.append(FileReport(path, lines))
    return ScrubReport(scanned=scanned, files=tuple(files), applied=apply)
