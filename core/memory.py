"""Mémoire projet durable : décisions, conventions et faits, avec provenance.

Le contexte est replié et les traces racontent le passé, mais rien ne garde ce
que l'équipe a décidé : « les rapports vont dans rapports/ », « la base est en
lecture seule le lundi ». Une note porte :

* son texte, sa portée et sa confiance ;
* son **chemin source**, sa date et une empreinte légère du fichier ;
* une date d'expiration facultative.

Une note dont la source a changé n'est plus injectée comme une vérité : elle
passe `stale`, reste consultable avec sa provenance, et l'utilisateur peut la
corriger ou la supprimer (`python -m core.memory --list|--edit|--delete`).
Le prompt ne reçoit que les notes actives, dans une section bornée.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from config import APP_ID, Settings
from core.fingerprint import compare_fingerprint, file_fingerprint
from core.locking import LockTimeout, file_lock

__all__ = [
    "MEMORY_NAME",
    "MemoryNote",
    "add_note",
    "delete_note",
    "edit_note",
    "format_notes",
    "load_notes",
    "main",
    "memory_dir",
    "memory_path",
    "memory_prompt",
    "note_status",
    "save_notes",
]

logger = logging.getLogger(f"{APP_ID}.memory")

MEMORY_NAME = "notes.json"


def memory_dir(settings: Settings) -> Path:
    return settings.paths.data_dir / "memory"


def memory_path(settings: Settings) -> Path:
    return memory_dir(settings) / MEMORY_NAME


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


@dataclass(slots=True)
class MemoryNote:
    """Un fait daté, avec sa source et son état de fraîcheur."""

    id: str
    text: str
    source: str = ""
    scope: str = "projet"
    confidence: float = 0.6
    created: str = ""
    updated: str = ""
    expires_at: str = ""
    fingerprint: dict[str, Any] | None = None

    @property
    def when(self) -> str:
        """Date lisible, pour le prompt et la CLI."""
        stamp = _parse(self.updated or self.created)
        return stamp.strftime("%Y-%m-%d") if stamp else "?"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MemoryNote | None":
        text = str(raw.get("text") or "").strip()
        if not text:
            return None
        try:
            confidence = float(raw.get("confidence", 0.6))
        except (TypeError, ValueError):
            confidence = 0.6
        fingerprint = raw.get("fingerprint")
        return cls(
            id=str(raw.get("id") or uuid.uuid4().hex[:8]),
            text=text,
            source=str(raw.get("source") or ""),
            scope=str(raw.get("scope") or "projet"),
            confidence=max(0.0, min(1.0, confidence)),
            created=str(raw.get("created") or ""),
            updated=str(raw.get("updated") or ""),
            expires_at=str(raw.get("expires_at") or ""),
            fingerprint=dict(fingerprint) if isinstance(fingerprint, dict) else None,
        )


def load_notes(settings: Settings) -> list[MemoryNote]:
    try:
        raw = json.loads(memory_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = raw.get("notes") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    notes: list[MemoryNote] = []
    for item in items:
        if isinstance(item, dict):
            note = MemoryNote.from_dict(item)
            if note is not None:
                notes.append(note)
    return notes


def save_notes(settings: Settings, notes: Iterable[MemoryNote]) -> bool:
    path = memory_path(settings)
    payload = {
        "version": 1,
        "saved_at": _now().isoformat(),
        "notes": [note.as_dict() for note in notes],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path.with_suffix(".lock")):
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)
    except (OSError, LockTimeout):
        logger.warning("mémoire non enregistrée", exc_info=True)
        return False
    return True


def note_status(note: MemoryNote, *, now: datetime | None = None) -> str:
    """`active`, `expired`, `stale` (source modifiée), `missing` ou `unverified`."""
    moment = now or _now()
    expiry = _parse(note.expires_at)
    if expiry is not None and expiry <= moment:
        return "expired"
    if not note.source:
        return "unverified"
    drift, _current = compare_fingerprint(note.fingerprint)
    if drift == "modified":
        return "stale"
    if drift == "missing":
        return "missing"
    if not note.fingerprint:
        return "unverified"
    # `compare_fingerprint` ne renvoie "identical" que si l'empreinte existe :
    # toute autre valeur vide signifie qu'aucune comparaison n'a eu lieu.
    return "active" if drift == "identical" else "unverified"


def add_note(
    settings: Settings,
    text: str,
    *,
    source: str = "",
    scope: str = "projet",
    confidence: float = 0.6,
    ttl_days: float = 0.0,
    now: datetime | None = None,
) -> MemoryNote | None:
    """Ajoute une note ; `None` si le texte est vide ou la mémoire pleine."""
    body = " ".join(str(text or "").split())
    if not body:
        return None
    cfg = getattr(settings, "memory", None)
    notes = load_notes(settings)
    limit = int(getattr(cfg, "max_notes", 200) or 200)
    if len(notes) >= limit:
        logger.warning("mémoire pleine (%d notes)", len(notes))
        return None
    moment = now or _now()
    fingerprint = file_fingerprint(source) if source else None
    note = MemoryNote(
        id=uuid.uuid4().hex[:8],
        text=body[:2000],
        source=str(source),
        scope=str(scope or "projet")[:40],
        confidence=max(0.0, min(1.0, float(confidence))),
        created=moment.isoformat(),
        updated=moment.isoformat(),
        expires_at=(moment + timedelta(days=float(ttl_days))).isoformat()
        if ttl_days
        else "",
        fingerprint=fingerprint,
    )
    notes.append(note)
    return note if save_notes(settings, notes) else None


def edit_note(
    settings: Settings,
    note_id: str,
    *,
    text: str | None = None,
    source: str | None = None,
    confidence: float | None = None,
    scope: str | None = None,
) -> MemoryNote | None:
    """Corrige une note et rafraîchit sa provenance (empreinte et date)."""
    notes = load_notes(settings)
    for note in notes:
        if note.id != str(note_id):
            continue
        if text is not None and str(text).strip():
            note.text = " ".join(str(text).split())[:2000]
        if scope is not None and str(scope).strip():
            note.scope = str(scope).strip()[:40]
        if confidence is not None:
            note.confidence = max(0.0, min(1.0, float(confidence)))
        if source is not None:
            note.source = str(source)
            note.fingerprint = file_fingerprint(source) if source else None
        note.updated = _now().isoformat()
        return note if save_notes(settings, notes) else None
    return None


def delete_note(settings: Settings, note_id: str) -> bool:
    notes = load_notes(settings)
    kept = [note for note in notes if note.id != str(note_id)]
    if len(kept) == len(notes):
        return False
    return save_notes(settings, kept)


def _active(notes: Iterable[MemoryNote], now: datetime | None = None) -> list[MemoryNote]:
    return [note for note in notes if note_status(note, now=now) == "active"]


def memory_prompt(
    settings: Settings,
    *,
    max_chars: int | None = None,
    now: datetime | None = None,
) -> str:
    """Section à injecter : notes actives, avec source, date et confiance."""
    cfg = getattr(settings, "memory", None)
    if cfg is None or not getattr(cfg, "enabled", True):
        return ""
    notes = sorted(
        _active(load_notes(settings), now), key=lambda note: note.confidence, reverse=True
    )
    limit = int(max_chars or getattr(cfg, "max_chars", 1500) or 1500)
    lines: list[str] = []
    used = 0
    for note in notes:
        origin = f", source {note.source}" if note.source else ""
        line = (
            f"- [{note.scope}] {note.text} (confiance {note.confidence:.1f}, "
            f"maj {note.when}{origin})"
        )
        if used + len(line) > limit:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return (
        "Mémoire projet (faits datés ; relis la source si elle est disponible) :\n"
        + "\n".join(lines)
    )


_STATUS_LABELS = {
    "active": "active",
    "expired": "expiree",
    "stale": "source modifiee",
    "missing": "source disparue",
    "unverified": "sans empreinte",
}


def format_notes(notes: Iterable[MemoryNote], *, now: datetime | None = None) -> str:
    rows: list[str] = []
    for note in notes:
        status = _STATUS_LABELS.get(note_status(note, now=now), "?")
        origin = f" <- {note.source}" if note.source else ""
        rows.append(
            f"{note.id}  [{status}] ({note.scope}, {note.confidence:.1f}, {note.when}) "
            f"{note.text}{origin}"
        )
    if not rows:
        return "Aucune note de mémoire projet."
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.memory",
        description="Notes durables du projet, avec provenance et expiration.",
    )
    parser.add_argument("--list", action="store_true", help="liste les notes et leur état")
    parser.add_argument("--add", metavar="TEXTE", help="ajoute une note")
    parser.add_argument("--source", metavar="CHEMIN", default="", help="fichier d'origine")
    parser.add_argument("--scope", default="projet", help="portée (projet, fichiers, web…)")
    parser.add_argument("--confidence", type=float, default=0.6, help="confiance 0..1")
    parser.add_argument("--ttl-days", type=float, default=0.0, help="expiration en jours (0 = jamais)")
    parser.add_argument("--edit", metavar="ID", help="corrige une note")
    parser.add_argument("--text", help="nouveau texte pour --edit")
    parser.add_argument("--delete", metavar="ID", help="supprime une note")
    parser.add_argument("--forget", action="store_true", help="supprime les notes expirées ou périmées")
    parser.add_argument("--json", action="store_true", help="sortie JSON pour --list")
    args = parser.parse_args(argv)

    settings = Settings.load(strict=False)
    if args.add:
        note = add_note(
            settings,
            args.add,
            source=args.source,
            scope=args.scope,
            confidence=args.confidence,
            ttl_days=args.ttl_days,
        )
        if note is None:
            print("Note non ajoutée (texte vide ou mémoire pleine).", file=sys.stderr)
            return 1
        print(f"Note {note.id} ajoutee.")
        return 0
    if args.edit:
        note = edit_note(
            settings,
            args.edit,
            text=args.text,
            source=args.source or None,
            confidence=args.confidence if args.confidence != 0.6 else None,
            scope=args.scope if args.scope != "projet" else None,
        )
        if note is None:
            print(f"Note introuvable : {args.edit}", file=sys.stderr)
            return 1
        print(f"Note {note.id} mise a jour.")
        return 0
    if args.delete:
        ok = delete_note(settings, args.delete)
        print("Note supprimee." if ok else f"Note introuvable : {args.delete}")
        return 0 if ok else 1
    if args.forget:
        notes = load_notes(settings)
        kept = [note for note in notes if note_status(note) == "active"]
        removed = len(notes) - len(kept)
        save_notes(settings, kept)
        print(f"{removed} note(s) oubliee(s).")
        return 0
    if args.json:
        notes = load_notes(settings)
        print(
            json.dumps(
                [
                    {**note.as_dict(), "status": note_status(note)}
                    for note in notes
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    # `--list` est le comportement par défaut : c'est la commande de consultation.
    print(format_notes(load_notes(settings)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
