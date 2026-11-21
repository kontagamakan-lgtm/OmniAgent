"""Index append-only des exécutions : `<runs>/index.jsonl`.

Le dossier des traces suffisait tant qu'il fallait seulement les compter. Pour
savoir *ce que contient* un run (manifeste, état, raison d'arrêt) sans ouvrir
chaque fichier, et pour distinguer un run tué d'un run encore en cours
d'écriture, il faut une vue latérale : chaque ligne de l'index décrit un
changement d'état (`started`, `finished`, `interrupted`, `archived`).

Le journal est append-only, mais il est compacté à la fin d'un run, à chaque
application de la rétention et par la CLI de maintenance (`compact`) : une
ligne par exécution dont la trace existe encore. Sans cela, il garderait les
états de runs purgés et grossirait sans fin alors que le dossier des traces,
lui, reste borné. Une compaction déjà propre ne réécrit rien.

L'index est un cache, jamais une source de vérité : s'il est absent ou
tronqué, il se reconstruit en balayant les traces (`rebuild`). Chaque lecture
repart donc des fichiers quand l'index ne dit rien.

Ce module n'importe pas `core.trace` : il lit le JSONL lui-même, pour que le
traceur puisse l'appeler sans cycle d'import.
"""

from __future__ import annotations

import copy
import gzip
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from config import APP_ID
from core.locking import LockTimeout, file_lock

__all__ = [
    "INDEX_NAME",
    "RunIndex",
    "RunRecord",
    "maintain",
    "scan_interrupted",
]

logger = logging.getLogger(f"{APP_ID}.trace")

#: Nommé pour rester hors des motifs `*.jsonl` : le dossier des traces est
#: parcouru partout (rétention, tests, smoke) et l'index ne doit jamais être
#: compté comme une exécution. Son contenu est bien du JSONL, une ligne par
#: changement d'état.
INDEX_NAME = "runs.index"
#: États possibles d'une exécution dans l'index.
STARTED = "started"
FINISHED = "finished"
INTERRUPTED = "interrupted"
ARCHIVED = "archived"
TERMINAL_STATES = frozenset({FINISHED, INTERRUPTED, ARCHIVED})

#: Fenêtre de queue inspectée pour trouver le marqueur terminal d'une trace :
#: inutile de relire 20 Mio pour savoir comment un run s'est terminé.
_TAIL_BYTES = 8_192

#: Une écriture peut échouer pour une raison passagère sans rapport avec le
#: verrou : antivirus, indexeur Windows ou sauvegarde ouvre le fichier en
#: partage exclusif pendant quelques millisecondes. Renoncer au premier essai
#: perdait l'état d'un run (49 lignes sur 50 lors d'une CI chargée).
_APPEND_ATTEMPTS = 6
#: Attente croissante entre deux essais : ~20 + 40 + … = 0,4 s au total, très
#: en deçà du délai laissé aux appelants.
_APPEND_BACKOFF_S = 0.02
#: Verrou visé à chaque essai : court, puisque l'essai est répété.
_APPEND_LOCK_TIMEOUT_S = 2.0


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(slots=True)
class RunRecord:
    """Dernier état connu d'une exécution."""

    run_id: str
    state: str
    path: str = ""
    started: str = ""
    updated: str = ""
    app_version: str = ""
    model: str = ""
    workspace: str = ""
    trace_format: int = 0
    stop_reason: str = ""
    ok: bool = False
    bytes: int = 0
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state,
            "path": self.path,
            "started": self.started,
            "updated": self.updated or _now(),
            "app_version": self.app_version,
            "model": self.model,
            "workspace": self.workspace,
            "trace_format": self.trace_format,
            "stop_reason": self.stop_reason,
            "ok": self.ok,
            "bytes": self.bytes,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunRecord":
        return cls(
            run_id=str(data.get("run_id") or ""),
            state=str(data.get("state") or ""),
            path=str(data.get("path") or ""),
            started=str(data.get("started") or ""),
            updated=str(data.get("updated") or ""),
            app_version=str(data.get("app_version") or ""),
            model=str(data.get("model") or ""),
            workspace=str(data.get("workspace") or ""),
            trace_format=int(data.get("trace_format") or 0),
            stop_reason=str(data.get("stop_reason") or ""),
            ok=bool(data.get("ok")),
            bytes=int(data.get("bytes") or 0),
            detail=str(data.get("detail") or ""),
        )


class RunIndex:
    """Journal d'états append-only, tolérant aux écritures concurrentes."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.path = self.directory / INDEX_NAME

    # -- écriture -----------------------------------------------------------

    def append(self, record: RunRecord) -> bool:
        """Ajoute un état. Un index inaccessible ne casse jamais l'exécution.

        Une erreur passagère est réessayée avant d'être abandonnée : perdre une
        ligne parce qu'un scanner a tenu le fichier quelques millisecondes
        laisserait l'index incomplet sans que personne ne le sache. Réessayer
        après une panne survenue en fin d'écriture peut dupliquer la ligne ;
        c'est sans conséquence, la lecture ne retient que le dernier état de
        chaque run et la compaction déduplique.
        """
        if not record.run_id:
            return False
        record.updated = record.updated or _now()
        line = json.dumps(record.as_dict(), ensure_ascii=False, default=str) + "\n"
        for attempt in range(1, _APPEND_ATTEMPTS + 1):
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                with file_lock(self._lock_path(), timeout=_APPEND_LOCK_TIMEOUT_S):
                    with self.path.open("a", encoding="utf-8") as handle:
                        handle.write(line)
                        handle.flush()
                        os.fsync(handle.fileno())
            except LockTimeout:
                logger.debug("index des runs verrouille (essai %s)", attempt)
            except OSError:
                logger.debug(
                    "index des runs non écrit (essai %s)", attempt, exc_info=True
                )
            else:
                return True
            if attempt < _APPEND_ATTEMPTS:
                time.sleep(_APPEND_BACKOFF_S * attempt)
        logger.warning(
            "index des runs non écrit après %s essais : état %s de %s perdu",
            _APPEND_ATTEMPTS,
            record.state,
            record.run_id,
        )
        return False

    def _lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.lock")

    def mark(
        self,
        run_id: str,
        state: str,
        *,
        path: Path | str = "",
        stop_reason: str = "",
        ok: bool = False,
        detail: str = "",
        manifest: dict[str, Any] | None = None,
    ) -> bool:
        """Raccourci : construit l'enregistrement puis l'ajoute."""
        manifest = manifest or {}
        record = RunRecord(
            run_id=run_id,
            state=state,
            path=str(path) if path else "",
            started=str(manifest.get("started") or _now()),
            app_version=str(manifest.get("app_version") or ""),
            model=str(manifest.get("model") or ""),
            workspace=str(manifest.get("workspace") or ""),
            trace_format=int(manifest.get("trace_format") or 0),
            stop_reason=stop_reason,
            ok=bool(ok),
            detail=detail,
        )
        if path:
            try:
                record.bytes = Path(path).stat().st_size
            except OSError:
                record.bytes = 0
        return self.append(record)

    # -- lecture ------------------------------------------------------------

    def records(self) -> Iterator[RunRecord]:
        """Toutes les lignes valides, dans l'ordre d'écriture."""
        try:
            handle = self.path.open(encoding="utf-8")
        except OSError:
            return
        with handle:
            for line in handle:
                record = self._parse(line)
                if record is not None:
                    yield record

    def entries(self) -> dict[str, RunRecord]:
        """Dernier état par exécution, du plus récent au plus ancien.

        Les lignes sont fusionnées et non remplacées : un enregistrement de fin
        ne répète pas le manifeste du début, et perdre le modèle ou la version
        au moment où le run se termine viderait l'index de son intérêt.
        """
        return dict(
            sorted(
                _latest(self.records()).items(),
                key=lambda item: item[1].updated,
                reverse=True,
            )
        )

    @staticmethod
    def _parse(line: str) -> RunRecord | None:
        """Un enregistrement, ou `None` pour une ligne vide ou illisible."""
        body = line.strip()
        if not body:
            return None
        try:
            data = json.loads(body)
        except ValueError:
            logger.debug("ligne d'index ignorée", exc_info=True)
            return None
        return RunRecord.from_dict(data) if isinstance(data, dict) else None

    def _scan(self) -> tuple[list[RunRecord], int]:
        """Enregistrements valides et lignes physiques, en une seule lecture.

        Compter les lignes physiques évite de croire l'index propre alors qu'il
        contient encore du JSON invalide, que `records()` ignore par nature.
        """
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return [], 0
        records: list[RunRecord] = []
        physical = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            physical += 1
            record = self._parse(line)
            if record is not None:
                records.append(record)
        return records, physical

    def _present_run_ids(self) -> set[str]:
        """Identifiants des traces encore sur disque (archives comprises)."""
        try:
            files = list(self.directory.glob("*.jsonl*"))
        except OSError:
            return set()
        return {
            _run_id_of(path)
            for path in files
            if path.is_file() and path.name != INDEX_NAME
        }

    def compact(self) -> bool:
        """Réécrit l'index : une ligne par exécution dont la trace existe encore.

        Les états des runs présents sont conservés fusionnés (dernier état
        connu), donc un run terminé garde son modèle, sa version et sa raison
        d'arrêt. Un run repris plusieurs fois — plusieurs `started`, plusieurs
        `finished` — ne laisse donc qu'une seule ligne. Seules les exécutions
        dont la rétention a supprimé la trace disparaissent : la taille de
        l'index suit le nombre de traces, pas le nombre d'écritures.

        Un index est un cache : si une autre écriture se glisse pendant la
        réécriture, on renonce plutôt que de la perdre (le prochain passage la
        compactera). Retourne vrai si le fichier a été réécrit.
        """
        try:
            before = self.path.stat()
        except OSError:
            return False
        try:
            with file_lock(self._lock_path()):
                return self._compact_locked(before)
        except LockTimeout:
            logger.warning("index des runs verrouille : compaction differee")
            return False

    def _compact_locked(self, before: os.stat_result) -> bool:
        """Compaction sous verrou : aucune écriture concurrente ne se glisse."""
        records, physical = self._scan()
        latest = _latest(iter(records))
        present = self._present_run_ids()
        kept = {
            run_id: record for run_id, record in latest.items() if run_id in present
        }
        # Une ligne illisible n'apparaît pas dans `records` : sans compter les
        # lignes physiques, un index déjà dédupliqué mais corrompu ne serait
        # jamais nettoyé. Déjà propre : ne rien réécrire.
        if (
            len(kept) == len(latest)
            and len(records) == len(kept)
            and physical == len(records)
        ):
            return False
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                for record in sorted(kept.values(), key=lambda item: item.updated):
                    handle.write(
                        json.dumps(record.as_dict(), ensure_ascii=False, default=str)
                        + "\n"
                    )
            after = self.path.stat()
            if (after.st_size, after.st_mtime_ns) != (
                before.st_size,
                before.st_mtime_ns,
            ):
                logger.debug("compaction abandonnée : index modifié entre-temps")
                tmp.unlink(missing_ok=True)
                return False
            os.replace(tmp, self.path)
        except OSError:
            logger.debug("compaction de l'index impossible", exc_info=True)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        return True

    def rebuild(
        self, *, max_files: int = 200, reset: bool = False
    ) -> dict[str, RunRecord]:
        """Reconstruit l'état de chaque trace présente, si l'index manque.

        `reset=True` repart de zéro : l'index est un cache, le supprimer puis
        le regarnir depuis les fichiers est la maintenance demandée.
        """
        latest: dict[str, RunRecord] = {}
        if reset:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                logger.debug("index non supprimé pour reconstruction", exc_info=True)
                return self.entries()
        else:
            latest = self.entries()
        try:
            files = sorted(
                (
                    path
                    for path in self.directory.glob("*.jsonl*")
                    if path.is_file() and path.name != INDEX_NAME
                ),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:max_files]
        except OSError:
            return latest
        for path in files:
            run_id = _run_id_of(path)
            if not run_id or run_id in latest:
                continue
            manifest = _manifest_of(path)
            record = RunRecord(
                run_id=run_id,
                # Le fichier fait foi : une trace qui porte `run_finished` (ou
                # `trace_truncated`) est terminée, même si elle a été purgée
                # de l'index entre-temps.
                state=FINISHED if _tail_is_terminal(path) else STARTED,
                path=str(path),
                app_version=str(manifest.get("app_version") or ""),
                model=str(manifest.get("model") or ""),
                workspace=str(manifest.get("workspace") or ""),
                trace_format=int(manifest.get("trace_format") or 0),
                bytes=path.stat().st_size,
                detail="reconstruit depuis le fichier",
            )
            latest[run_id] = record
            self.append(record)
        return latest


# ---------------------------------------------------------------------------
# Détection des runs interrompus
# ---------------------------------------------------------------------------


def _latest(records: Iterator[RunRecord]) -> dict[str, RunRecord]:
    """Dernier état connu par exécution, en fusionnant les lignes successives."""
    latest: dict[str, RunRecord] = {}
    for record in records:
        if not record.run_id:
            continue
        previous = latest.get(record.run_id)
        latest[record.run_id] = record if previous is None else _merge(previous, record)
    return latest


def _merge(previous: RunRecord, record: RunRecord) -> RunRecord:
    """Fusionne deux états successifs ; le plus récent fait foi."""
    merged = copy.copy(record)
    for name in (
        "path",
        "started",
        "app_version",
        "model",
        "workspace",
        "trace_format",
        "bytes",
    ):
        if not getattr(merged, name):
            setattr(merged, name, getattr(previous, name))
    if not merged.stop_reason and record.state not in {FINISHED}:
        merged.stop_reason = previous.stop_reason
    return merged


def _run_id_of(path: Path) -> str:
    """Identifiant d'exécution d'une trace, sans la lire entièrement."""
    name = path.name[:-3] if path.name.endswith(".gz") else path.name
    if name.endswith(".jsonl"):
        name = name[:-6]
    return name


def _first_record(path: Path) -> dict[str, Any]:
    """Premier enregistrement JSON d'une trace (compressée ou non)."""
    try:
        if path.name.endswith(".gz"):
            import gzip

            with gzip.open(path, "rt", encoding="utf-8") as handle:
                line = handle.readline()
        else:
            with path.open(encoding="utf-8") as handle:
                line = handle.readline()
        data = json.loads(line)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _manifest_of(path: Path) -> dict[str, Any]:
    """Manifeste d'une trace, sans la clé : `meta` en clair si chiffrée."""
    record = _first_record(path)
    if record.get("type") != "run_started":
        return {}
    payload = record.get("payload")
    if isinstance(payload, dict):
        return payload
    meta = record.get("meta")
    return dict(meta) if isinstance(meta, dict) else {}


def _tail_is_terminal(path: Path) -> bool:
    """Vrai si la fin du fichier porte un marqueur de fin d'exécution.

    Vaut pour une trace en clair comme pour une archive `.gz` : la fin de
    l'index doit refléter le fichier, pas son extension. Un fichier illisible
    est réputé terminé pour ne pas inventer un run interrompu.
    """
    try:
        if path.name.endswith(".gz"):
            with gzip.open(path, "rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                handle.seek(max(0, size - _TAIL_BYTES))
                tail = handle.read().decode("utf-8", errors="replace")
        else:
            with path.open("rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                handle.seek(max(0, size - _TAIL_BYTES))
                tail = handle.read().decode("utf-8", errors="replace")
    except (OSError, EOFError):
        return True
    return '"run_finished"' in tail or '"trace_truncated"' in tail


def maintain(
    directory: Path | str, *, rebuild: bool = False, max_files: int = 200
) -> list[dict[str, Any]]:
    """Compacte ou reconstruit les index d'un dossier et de ses sous-dossiers.

    Chaque espace de travail (`trace.per_workspace`) possède son propre index :
    les deux niveaux sont traités, sinon une maintenance manuelle laisserait
    les sous-dossiers intacts.
    """
    root = Path(directory)
    targets = [root]
    try:
        targets += sorted({path.parent for path in root.glob(f"*/{INDEX_NAME}")})
    except OSError:
        pass
    report: list[dict[str, Any]] = []
    for target in targets:
        index = RunIndex(target)
        if rebuild:
            entries = index.rebuild(max_files=max_files, reset=True)
            action = "rebuild"
        else:
            changed = index.compact()
            entries = index.entries()
            action = "compact" if changed else "unchanged"
        report.append(
            {
                "directory": str(target),
                "action": action,
                "runs": len(entries),
            }
        )
    return report


def workspace_indexes(root: Path | str) -> list[Path]:
    """Index de la racine et de ses sous-dossiers d'espace de travail.

    `trace.per_workspace` écrit `runs/<slug>/runs.index` : le panneau Traces et
    la maintenance doivent lire les deux niveaux, sinon les runs d'atelier
    apparaissent sans état ni modèle.
    """
    base = Path(root)
    targets = [base]
    try:
        targets += sorted({path.parent for path in base.glob(f"*/{INDEX_NAME}")})
    except OSError:
        pass
    return targets


def merged_entries(root: Path | str) -> dict[str, RunRecord]:
    """Dernier état de chaque run, tous espaces de travail confondus.

    Un identifiant de run reste unique (aléatoire) ; en cas de collision entre
    deux dossiers, l'état le plus récent gagne plutôt que d'écraser un run au
    hasard. L'ordre retourné va du plus récent au plus ancien.
    """
    merged: dict[str, RunRecord] = {}
    for directory in workspace_indexes(root):
        for run_id, record in RunIndex(directory).entries().items():
            previous = merged.get(run_id)
            if previous is None or record.updated >= previous.updated:
                merged[run_id] = record
    return dict(
        sorted(merged.items(), key=lambda item: item[1].updated, reverse=True)
    )


def scan_interrupted(
    directory: Path | str,
    *,
    active_ttl_s: float = 300.0,
    known: dict[str, RunRecord] | None = None,
) -> list[str]:
    """Marque `interrupted` les traces sans fin et sans marqueur d'activité.

    Une trace encore ouverte par un processus vivant n'est pas concernée : son
    marqueur `.active` est frais. Un marqueur périmé, lui, signifie que le
    processus a été tué — le run est alors déclaré interrompu plutôt que
    présumé en cours. Retourne les identifiants nouvellement marqués.
    """
    root = Path(directory)
    index = RunIndex(root)
    states = dict(known if known is not None else index.entries())
    now = time.time()
    marked: list[str] = []

    try:
        traces = [
            path
            for path in root.glob("*.jsonl")
            if path.is_file() and path.name != INDEX_NAME
        ]
    except OSError:
        return marked

    for path in sorted(traces):
        run_id = _run_id_of(path)
        if not run_id:
            continue
        state = states.get(run_id)
        if state is not None and state.state in TERMINAL_STATES:
            continue
        marker = root / f"{run_id}.active"
        try:
            age = now - marker.stat().st_mtime
        except OSError:
            age = None
        if age is not None and age <= active_ttl_s:
            continue  # un autre processus écrit encore
        if age is not None:
            try:
                marker.unlink()
            except OSError:
                logger.debug("marqueur périmé non supprimé : %s", marker, exc_info=True)
        if _tail_is_terminal(path):
            continue
        index.mark(
            run_id,
            INTERRUPTED,
            path=path,
            detail="aucun run_finished et marqueur absent ou périmé",
            manifest=_manifest_of(path),
        )
        marked.append(run_id)
    return marked
