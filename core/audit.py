"""Journal d'audit chaîné : détecter une trace modifiée après coup.

Les traces chiffrées protègent la confidentialité, pas l'intégrité : qui
possède la clé peut réécrire une ligne, puis rechiffrer. Ce module tient un
journal append-only (`data/audit/chain.jsonl`) où chaque scellement enregistre :

* le nombre d'enregistrements d'une trace et la racine SHA-256 de leurs
  empreintes **dans l'ordre** — modifier un octet, supprimer une ligne ou en
  réordonner deux change la racine ;
* l'empreinte de l'entrée précédente, qui chaîne les scellements entre eux ;
* les décisions de politique d'outils (`kind: "policy"`), pour qu'un refus ou
  une autorisation ne puisse pas être effacé discrètement.

La vérification (`--verify-audit`) recalcule la chaîne et chaque racine de
trace. Elle détecte donc une altération accidentelle ou partielle. Elle ne
remplace pas un ancrage externe : un attaquant qui réécrit la trace **et** tout
le journal avec la clé peut recalculer la chaîne. Le rapport le dit.

    python -m core.audit --seal-all
    python -m core.audit --verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import APP_ID, Settings
from core.locking import LockTimeout, file_lock
from core.trace import read_events
from core.trace_crypto import TraceKeyError, optional_key

__all__ = [
    "AuditEntry",
    "AuditReport",
    "audit_path",
    "export_anchor",
    "record_hash",
    "record_policy_decision",
    "seal_all",
    "seal_trace",
    "verify",
    "verify_anchor",
    "verify_trace",
]

logger = logging.getLogger(f"{APP_ID}.audit")

AUDIT_NAME = "chain.jsonl"


def audit_path(settings: Settings) -> Path:
    return settings.paths.data_dir / "audit" / AUDIT_NAME


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_hash(record: dict[str, Any]) -> str:
    """Empreinte stable d'un enregistrement de trace, indépendante du fichier."""
    return hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()


def _entry_hash(entry: dict[str, Any]) -> str:
    body = {key: value for key, value in entry.items() if key != "hash"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def _trace_root(records: Iterable[dict[str, Any]]) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for record in records:
        digest.update(record_hash(record).encode("ascii"))
        count += 1
    return count, digest.hexdigest()


def _relative(settings: Settings, path: Path) -> str:
    try:
        return path.resolve().relative_to(settings.paths.data_dir.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _absolute(settings: Settings, relative: str) -> Path:
    candidate = Path(relative)
    return candidate if candidate.is_absolute() else settings.paths.data_dir / candidate


@dataclass(slots=True)
class AuditEntry:
    """Une entrée du journal, telle qu'écrite."""

    seq: int
    kind: str
    prev: str
    hash: str
    body: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return dict(self.body)


@dataclass(slots=True)
class AuditReport:
    """Bilan de vérification ; `problems` vide signifie chaîne cohérente."""

    ok: bool = True
    entries: int = 0
    traces: int = 0
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def format(self) -> str:
        lines = [
            f"Journal : {self.entries} entree(s), {self.traces} trace(s) scellee(s)",
            f"Etat : {'ok' if self.ok else 'incomplet'}",
        ]
        lines.extend(f"- {problem}" for problem in self.problems)
        lines.extend(f"note : {note}" for note in self.notes)
        if not self.problems:
            lines.append(
                "La chaine et les racines concordent ; un ancrage externe reste "
                "necessaire contre une reecriture complete."
            )
        return "\n".join(lines)


class _Log:
    """Lecture/écriture du journal, verrou inter-processus borné."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = audit_path(settings)

    def read(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        entries: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                entries.append(value)
        return entries

    def append(self, entry: dict[str, Any]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.path.with_suffix(".lock")):
            existing = self.read()
            previous = str(existing[-1].get("hash") or "") if existing else ""
            body = {
                "seq": len(existing) + 1,
                "at": datetime.now(tz=timezone.utc).isoformat(),
                "prev": previous,
                **entry,
            }
            body["hash"] = _entry_hash(body)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(body, ensure_ascii=False) + "\n")
        return body


def seal_trace(
    settings: Settings,
    trace: Path | str,
    *,
    key: bytes | None = None,
    run_id: str = "",
) -> dict[str, Any] | None:
    """Scelle une trace (ordre et contenu) ; `None` si elle est illisible."""
    path = Path(trace)
    if not path.is_file():
        return None
    if key is None:
        try:
            key = optional_key(settings.trace.key_env)
        except TraceKeyError:
            key = None
    try:
        records = list(read_events(path, key=key))
    except (OSError, TraceKeyError, ValueError) as exc:
        logger.warning("scellement impossible pour %s : %s", path, exc)
        return None
    count, root = _trace_root(records)
    if not count:
        return None
    return _Log(settings).append(
        {
            "kind": "trace",
            "run_id": run_id or path.stem,
            "trace": _relative(settings, path),
            "count": count,
            "root": root,
        }
    )


def record_policy_decision(
    settings: Settings,
    *,
    tool: str,
    allowed: bool,
    rule: str,
    run_id: str = "",
) -> dict[str, Any] | None:
    """Inscrit une décision de politique dans la même chaîne."""
    try:
        return _Log(settings).append(
            {
                "kind": "policy",
                "tool": str(tool),
                "allowed": bool(allowed),
                "rule": str(rule)[:300],
                "run_id": str(run_id),
            }
        )
    except (OSError, LockTimeout):
        logger.warning("décision de politique non scellée", exc_info=True)
        return None


def seal_all(settings: Settings, *, key: bytes | None = None) -> int:
    """Scelle les traces qui ne le sont pas encore ; retourne le nombre ajouté."""
    log = _Log(settings)
    sealed = {
        str(entry.get("trace") or "")
        for entry in log.read()
        if entry.get("kind") == "trace"
    }
    runs = settings.paths.log_dir / "runs"
    if not runs.is_dir():
        return 0
    added = 0
    for path in sorted(runs.rglob("*")):
        if not path.is_file() or not path.name.endswith((".jsonl", ".jsonl.gz")):
            continue
        relative = _relative(settings, path)
        if relative in sealed:
            continue
        if seal_trace(settings, path, key=key) is not None:
            added += 1
    return added


def verify_trace(settings: Settings, trace: Path | str) -> list[str]:
    """Compare une trace scellée à son empreinte ; retourne les écarts."""
    path = Path(trace)
    relative = _relative(settings, path)
    entries = [
        entry
        for entry in _Log(settings).read()
        if entry.get("kind") == "trace" and entry.get("trace") == relative
    ]
    if not entries:
        return [f"{path.name} : aucune entree d'audit"]
    expected = entries[-1]
    try:
        key = optional_key(settings.trace.key_env)
        records = list(read_events(path, key=key))
    except (OSError, TraceKeyError, ValueError) as exc:
        return [f"{path.name} : trace illisible ({exc})"]
    count, root = _trace_root(records)
    problems: list[str] = []
    if count != int(expected.get("count") or 0):
        problems.append(
            f"{path.name} : {count} enregistrement(s), "
            f"{expected.get('count')} scelle(s)"
        )
    if root != str(expected.get("root") or ""):
        problems.append(f"{path.name} : contenu ou ordre different du scellement")
    return problems


def export_anchor(settings: Settings, destination: Path | str) -> dict[str, Any]:
    """Copie le journal vers un support externe (USB, partage hors machine).

    L'ancre est une copie exacte, volontairement sans mécanisme secret : sa
    valeur vient du fait qu'elle quitte la machine. La vérification compare le
    préfixe du journal courant à cette copie, donc une réécriture complète
    (même rechaînée) est détectée tant que l'ancre est hors de portée de
    l'attaquant.
    """
    source = audit_path(settings)
    target = Path(destination)
    try:
        data = source.read_bytes() if source.is_file() else b""
    except OSError as exc:
        raise OSError(f"journal illisible : {exc}") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(target)
    entries = _parse_entries(data.decode("utf-8", errors="replace"))
    return {
        "path": str(target),
        "entries": len(entries),
        "head": str(entries[-1].get("hash") or "") if entries else "",
    }


def _parse_entries(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries


def verify_anchor(settings: Settings, anchor: Path | str) -> list[str]:
    """Compare le journal courant au préfixe conservé hors machine."""
    path = Path(anchor)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"ancre externe illisible : {exc}"]
    anchored = _parse_entries(text)
    if not anchored:
        return ["ancre externe vide : aucune garantie"]
    problems: list[str] = []
    previous = ""
    for index, entry in enumerate(anchored, start=1):
        if int(entry.get("seq") or 0) != index:
            problems.append(f"ancre entree {index} : sequence incoherente")
        if str(entry.get("prev") or "") != previous:
            problems.append(f"ancre entree {index} : chaine interne rompue")
        if _entry_hash(entry) != str(entry.get("hash") or ""):
            problems.append(f"ancre entree {index} : empreinte invalide")
        previous = str(entry.get("hash") or "")
    current = _Log(settings).read()
    if len(current) < len(anchored):
        problems.append(
            f"journal plus court que l'ancre ({len(current)} < {len(anchored)} entrées)"
        )
    for index, expected in enumerate(anchored, start=1):
        actual = current[index - 1] if index <= len(current) else {}
        if str(actual.get("hash") or "") != str(expected.get("hash") or ""):
            problems.append(f"entree {index} : differente de l'ancre externe")
    return problems


def verify(
    settings: Settings,
    *,
    key: bytes | None = None,
    traces: bool = True,
    anchor: Path | str | None = None,
) -> AuditReport:
    """Vérifie la chaîne du journal, puis les racines des traces scellées."""
    report = AuditReport()
    entries = _Log(settings).read()
    report.entries = len(entries)
    previous = ""
    for index, entry in enumerate(entries, start=1):
        if int(entry.get("seq") or 0) != index:
            report.problems.append(f"entree {index} : sequence {entry.get('seq')}")
        if str(entry.get("prev") or "") != previous:
            report.problems.append(f"entree {index} : maillon precedent rompu")
        if _entry_hash(entry) != str(entry.get("hash") or ""):
            report.problems.append(f"entree {index} : empreinte invalide")
        previous = str(entry.get("hash") or "")

    if traces:
        seen: set[str] = set()
        for entry in entries:
            if entry.get("kind") != "trace":
                continue
            relative = str(entry.get("trace") or "")
            if not relative or relative in seen:
                continue
            seen.add(relative)
            path = _absolute(settings, relative)
            if not path.is_file():
                report.problems.append(f"{relative} : trace scellee introuvable")
                continue
            report.problems.extend(verify_trace(settings, path))
        report.traces = len(seen)

    if anchor is not None:
        report.problems.extend(verify_anchor(settings, anchor))
        report.notes.append(f"ancre externe comparee : {anchor}")

    report.ok = not report.problems
    if report.ok and entries:
        report.notes.append(
            "un attaquant disposant de la cle peut recalculer trace et journal : "
            "copier regulierement le journal hors de la machine."
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.audit",
        description="Scelle et vérifie les traces dans un journal chaîné.",
    )
    parser.add_argument("--seal-all", action="store_true", help="scelle les traces non scellées")
    parser.add_argument("--seal", metavar="TRACE", help="scelle une trace précise")
    parser.add_argument("--verify", action="store_true", help="vérifie la chaîne et les traces")
    parser.add_argument(
        "--anchor",
        metavar="FICHIER",
        help="copie le journal hors machine, ou le compare avec --verify",
    )
    args = parser.parse_args(argv)

    settings = Settings.load(strict=False)
    if args.seal_all:
        print(f"{seal_all(settings)} trace(s) scellee(s)")
        return 0
    if args.seal:
        entry = seal_trace(settings, args.seal)
        if entry is None:
            print(f"Scellement impossible : {args.seal}", file=sys.stderr)
            return 1
        print(f"Trace scellee : {entry.get('trace')} ({entry.get('count')} enregistrements)")
        return 0
    if args.anchor and not args.verify:
        result = export_anchor(settings, args.anchor)
        print(
            f"Ancre externe : {result['path']} "
            f"({result['entries']} entree(s), tete {result['head'][:12] or '(vide)'})"
        )
        return 0
    if args.verify:
        report = verify(settings, anchor=args.anchor)
        print(report.format())
        return 0 if report.ok else 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
