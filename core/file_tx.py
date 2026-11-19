"""Transaction par run pour les outils qui modifient des fichiers.

Un run peut écrire plusieurs fichiers puis échouer : sans trace de son passage,
revenir en arrière signifie deviner. Chaque exécution ouvre donc une
transaction sous `data/transactions/<run_id>/` :

* `journal.jsonl` — les **intentions**, écrites *avant* chaque mutation :
  opération, chemin, destination éventuelle, existence préalable ;
* `snapshots/` — une copie des **seuls fichiers touchés** (contenu exact,
  arborescence comprise), pas du reste du disque ;
* `undone.json` — la marque d'une annulation déjà appliquée.

L'annulation rejoue les intentions à l'envers : elle restaure les octets
d'origine, supprime ce que le run a créé et remet en place ce qu'il a déplacé.
Elle ne touche que les chemins du journal, et refuse tout chemin sorti des
racines enregistrées dans l'en-tête.

    python -m core.file_tx --list
    python -m core.file_tx --undo <run_id>             # aperçu
    python -m core.file_tx --undo <run_id> --apply     # restaure

L'instantané est borné (`MAX_SNAPSHOT_BYTES`) : au-delà, l'intention est
journalisée sans copie et l'annulation le dit au lieu de faire semblant. Ce
n'est pas une sauvegarde : la transaction couvre un run, pas l'historique.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import APP_ID, Settings

__all__ = [
    "MAX_SNAPSHOT_BYTES",
    "FileTransaction",
    "UndoReport",
    "UndoStep",
    "main",
    "transaction_dir",
    "transactions_dir",
    "undo_run",
    "undo_transaction",
]

logger = logging.getLogger(f"{APP_ID}.file_tx")

TRANSACTIONS_DIRNAME = "transactions"
#: Plafond de copie par transaction. Au-delà, l'intention reste journalisée
#: mais sans instantané : l'annulation dira qu'elle ne peut pas restaurer.
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024

_RUN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def transactions_dir(settings: Settings) -> Path:
    return settings.paths.data_dir / TRANSACTIONS_DIRNAME


def transaction_dir(settings: Settings, run_id: str) -> Path | None:
    """Dossier d'un run, ou `None` si l'identifiant n'est pas sûr."""
    text = str(run_id or "").strip()
    if not _RUN_ID.match(text) or ".." in text:
        return None
    return transactions_dir(settings) / text


def _inside(path: Path, roots: Iterable[Path]) -> bool:
    for root in roots:
        try:
            if path == root or path.is_relative_to(root):
                return True
        except (ValueError, OSError):
            continue
    return False


@dataclass(slots=True)
class Intent:
    """Une mutation annoncée avant d'être appliquée."""

    seq: int
    op: str
    path: str
    destination: str = ""
    kind: str = "file"
    existed: bool = False
    snapshot: str = ""
    size: int = 0
    mtime_ns: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "intent",
            "seq": self.seq,
            "op": self.op,
            "path": self.path,
            "destination": self.destination,
            "kind": self.kind,
            "existed": self.existed,
            "snapshot": self.snapshot,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "at": _now(),
        }


class FileTransaction:
    """Journal et instantanés d'un run ; les erreurs d'écriture sont non fatales."""

    def __init__(
        self,
        directory: Path | str,
        *,
        run_id: str = "",
        roots: Iterable[str | os.PathLike[str]] = (),
        max_snapshot_bytes: int = MAX_SNAPSHOT_BYTES,
    ) -> None:
        self.directory = Path(directory)
        self.run_id = run_id or self.directory.name
        self.journal_path = self.directory / "journal.jsonl"
        self.snapshot_dir = self.directory / "snapshots"
        self.roots = [Path(root).resolve() for root in roots]
        self.max_snapshot_bytes = max(0, int(max_snapshot_bytes))
        self._lock = threading.Lock()
        self._seq = 0
        self._captured = 0
        self._broken = False

    # -- cycle de vie -------------------------------------------------------

    def begin(self) -> None:
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._append(
            {
                "type": "run",
                "run_id": self.run_id,
                "started_at": _now(),
                "roots": [str(root) for root in self.roots],
            }
        )

    def finish(self, *, ok: bool, stop_reason: str = "") -> None:
        self._append(
            {"type": "run_finished", "ok": bool(ok), "stop_reason": stop_reason, "at": _now()}
        )

    def _append(self, record: dict[str, Any]) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
        except OSError:
            self._broken = True
            logger.warning("journal de transaction non écrit", exc_info=True)

    # -- journalisation des intentions --------------------------------------

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def _snapshot(self, target: Path, seq: int) -> tuple[str, int, int]:
        """Copie le fichier ou l'arborescence ; vide si le plafond est atteint."""
        try:
            if target.is_dir():
                size = sum(
                    item.stat().st_size for item in target.rglob("*") if item.is_file()
                )
                name = f"{seq:04d}.tree"
            else:
                size = target.stat().st_size
                name = f"{seq:04d}.bin"
        except OSError:
            return "", 0, 0
        if size + self._captured > self.max_snapshot_bytes:
            logger.warning(
                "instantané omis pour %s : plafond de transaction atteint", target
            )
            return "", size, 0
        destination = self.snapshot_dir / name
        try:
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            if target.is_dir():
                shutil.copytree(target, destination)
            else:
                shutil.copy2(target, destination)
            stat = target.stat()
            self._captured += size
            return name, size, stat.st_mtime_ns
        except (OSError, shutil.Error):
            logger.warning("instantané impossible pour %s", target, exc_info=True)
            return "", size, 0

    def _record(
        self,
        op: str,
        path: Path,
        *,
        destination: Path | None = None,
        existed: bool,
        kind: str = "file",
        snapshot: bool = True,
    ) -> None:
        with self._lock:
            seq = self._next()
            name, size, mtime_ns = "", 0, 0
            if existed and snapshot:
                name, size, mtime_ns = self._snapshot(path, seq)
            intent = Intent(
                seq=seq,
                op=op,
                path=str(path),
                destination=str(destination) if destination is not None else "",
                kind=kind,
                existed=existed,
                snapshot=name,
                size=size,
                mtime_ns=mtime_ns,
            )
            self._append(intent.as_dict())

    # -- points d'entrée utilisés par FileManager ---------------------------

    def will_write(self, path: Path | str) -> None:
        target = Path(path)
        if target.exists():
            if target.is_dir():
                self._record("write", target, existed=True, kind="dir")
            else:
                self._record("write", target, existed=True, kind="file")
        else:
            self._record("create", target, existed=False, kind="file", snapshot=False)

    def will_create(self, path: Path | str, *, kind: str = "file") -> None:
        """Le chemin est créé par le run ; s'il existe déjà, il sera écrasé."""
        self.will_write(path) if Path(path).exists() else self._record(
            "create", Path(path), existed=False, kind=kind, snapshot=False
        )

    def will_mkdir(self, path: Path | str) -> None:
        target = Path(path)
        if not target.exists():
            self._record("mkdir", target, existed=False, kind="dir", snapshot=False)

    def will_delete(self, path: Path | str) -> None:
        target = Path(path)
        if target.exists():
            self._record(
                "delete",
                target,
                existed=True,
                kind="dir" if target.is_dir() else "file",
            )

    def will_move(self, source: Path | str, destination: Path | str) -> None:
        origin = Path(source)
        if not origin.exists():
            return
        self._record(
            "move",
            origin,
            destination=Path(destination),
            existed=True,
            kind="dir" if origin.is_dir() else "file",
        )


# ---------------------------------------------------------------------------
# Annulation
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class UndoStep:
    seq: int
    op: str
    path: str
    status: str
    detail: str = ""


@dataclass(slots=True)
class UndoReport:
    run_id: str
    directory: Path
    applied: bool = False
    steps: list[UndoStep] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not [
            step
            for step in self.steps
            if step.status.startswith(("refus", "impossible", "echec"))
        ]

    @property
    def restored(self) -> int:
        return sum(1 for step in self.steps if step.status == "restaure")

    @property
    def removed(self) -> int:
        return sum(1 for step in self.steps if step.status == "supprime")

    def format(self) -> str:
        lines = [
            f"Transaction : {self.run_id}",
            f"Dossier : {self.directory}",
            f"Mode : {'application' if self.applied else 'apercu (aucune ecriture)'}",
            f"Intentions : {len(self.steps)}",
        ]
        for step in self.steps:
            detail = f"  ({step.detail})" if step.detail else ""
            lines.append(f"  {step.status.ljust(18)} {step.op} {step.path}{detail}")
        lines.append(
            f"Bilan : {self.restored} restaure(s), {self.removed} supprime(s), "
            f"{'ok' if self.ok else 'incomplet'}"
        )
        return "\n".join(lines)


def _read_journal(directory: Path) -> tuple[list[dict[str, Any]], list[str]]:
    intents: list[dict[str, Any]] = []
    roots: list[str] = []
    try:
        lines = (directory / "journal.jsonl").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return [], []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") == "run":
            roots = [str(item) for item in record.get("roots") or []]
        elif record.get("type") == "intent":
            intents.append(record)
    return intents, roots


def _restore(snapshot: Path, target: Path, *, mtime_ns: int = 0) -> None:
    """Réécrit le contenu exact ; remplace atomiquement pour ne rien laisser à moitié."""
    if snapshot.is_dir():
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(snapshot, target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    data = snapshot.read_bytes()
    temp = target.with_name(target.name + ".omni-undo")
    temp.write_bytes(data)
    os.replace(temp, target)
    if mtime_ns:
        try:
            os.utime(target, ns=(mtime_ns, mtime_ns))
        except OSError:
            pass


def _remove(path: Path, roots: list[Path]) -> str:
    if path in roots:
        return "refuse: racine autorisee"
    if not path.exists():
        return "deja absent"
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
    return "supprime"


def undo_transaction(
    directory: Path | str,
    *,
    apply: bool = True,
    roots: Iterable[str | os.PathLike[str]] | None = None,
) -> UndoReport:
    """Rejoue les intentions à l'envers ; par défaut, seulement un aperçu."""
    target_dir = Path(directory)
    run_id = target_dir.name
    intents, recorded_roots = _read_journal(target_dir)
    guard = [Path(root).resolve() for root in (recorded_roots or roots or [])]
    report = UndoReport(run_id=run_id, directory=target_dir, applied=apply)

    for record in reversed(intents):
        seq = int(record.get("seq") or 0)
        op = str(record.get("op") or "")
        path = Path(str(record.get("path") or ""))
        destination = Path(str(record.get("destination") or "")) if record.get("destination") else None
        snapshot = target_dir / "snapshots" / str(record.get("snapshot") or "")
        detail = ""

        if not str(path) or (guard and not _inside(path, guard)):
            report.steps.append(
                UndoStep(seq, op, str(path), "refuse: hors transaction", "chemin non enregistre")
            )
            continue
        if destination is not None and guard and not _inside(destination, guard):
            report.steps.append(
                UndoStep(seq, op, str(path), "refuse: hors transaction", "destination externe")
            )
            continue

        try:
            if op in {"write", "delete"}:
                if not record.get("snapshot") or not snapshot.exists():
                    status = "impossible: instantane absent"
                    detail = "fichier trop gros ou copie ratee"
                elif not apply:
                    status = "restaurerait"
                else:
                    _restore(snapshot, path, mtime_ns=int(record.get("mtime_ns") or 0))
                    status = "restaure"
            elif op == "create":
                if not apply:
                    status = "supprimerait"
                else:
                    status = _remove(path, guard)
            elif op == "move":
                if not record.get("snapshot") or not snapshot.exists():
                    status = "impossible: instantane absent"
                elif not apply:
                    status = "remettrait"
                else:
                    if destination is not None and destination != path:
                        _remove(destination, guard)
                    _restore(snapshot, path, mtime_ns=int(record.get("mtime_ns") or 0))
                    status = "restaure"
            elif op == "mkdir":
                if not apply:
                    status = "supprimerait"
                elif not path.exists():
                    status = "deja absent"
                else:
                    try:
                        path.rmdir()
                        status = "supprime"
                    except OSError:
                        status = "conserve: non vide"
            else:
                status = "ignore: operation inconnue"
        except (OSError, shutil.Error) as exc:
            status = "echec"
            detail = str(exc)
            logger.warning("annulation de %s impossible : %s", path, exc)

        report.steps.append(UndoStep(seq, op, str(path), status, detail))

    if apply:
        try:
            (target_dir / "undone.json").write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "at": _now(),
                        "steps": len(report.steps),
                        "ok": report.ok,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            logger.debug("marque d'annulation non écrite", exc_info=True)
    return report


def undo_run(settings: Settings, run_id: str, *, apply: bool = True) -> UndoReport | None:
    """Annule un run nommé ; `None` si l'identifiant est invalide."""
    directory = transaction_dir(settings, run_id)
    if directory is None:
        return None
    if not (directory / "journal.jsonl").is_file():
        return None
    roots = [str(root) for root in settings.files.allowed_roots]
    return undo_transaction(directory, apply=apply, roots=roots)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.file_tx",
        description="Journal et annulation des fichiers modifies pendant un run.",
    )
    parser.add_argument("--list", action="store_true", help="liste les transactions")
    parser.add_argument("--undo", metavar="RUN", help="annule le run indique")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="ecrit l'annulation (sans cette option : apercu seulement)",
    )
    args = parser.parse_args(argv)

    settings = Settings.load(strict=False)
    if args.list:
        base = transactions_dir(settings)
        if not base.is_dir():
            print(f"Aucune transaction : {base}")
            return 0
        for directory in sorted(base.iterdir()):
            if not directory.is_dir():
                continue
            journal = directory / "journal.jsonl"
            count = 0
            if journal.is_file():
                count = sum(
                    1
                    for line in journal.read_text(encoding="utf-8").splitlines()
                    if '"type": "intent"' in line
                )
            print(f"{directory.name.ljust(40)} {count} intention(s)")
        return 0

    if not args.undo:
        parser.print_help()
        return 2
    report = undo_run(settings, args.undo, apply=args.apply)
    if report is None:
        print(f"Transaction introuvable : {args.undo}", file=sys.stderr)
        return 2
    print(report.format())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
