"""Trace JSONL : un fichier par exécution, payload expurgé.

Le bus ne connaît pas le `run_id` à l'abonnement : le fichier est donc ouvert au
premier événement d'une exécution (`run_started`) et refermé sur `run_finished`.
Chaque ligne contient l'identifiant, l'horodatage, le type et le payload — ce
dernier passé au nettoyeur de secrets, car une trace est un fichier durable,
pas un affichage transitoire.

Une panne d'écriture ne doit jamais interrompre l'exécution : elle est
journalisée et la trace se referme.

Trois garanties supplémentaires, ajoutées après coup :

* **exécutions concurrentes** — un marqueur `<run>.active` (PID + fraîcheur)
  accompagne chaque trace ouverte, et la rétention épargne toute trace dont le
  marqueur est encore frais. Sans lui, le run d'un autre processus pouvait être
  le plus ancien et se faire supprimer pendant qu'il écrivait ;
* **trace incomplète visible** — un enregistrement `trace_truncated` est écrit
  lorsque le plafond de taille interrompt la trace, et l'objet expose
  `truncated` pour que l'appelant le dise à l'utilisateur ;
* **mode métadonnées** — `content="metadata"` retire la tâche, les réponses et
  les fragments de flux : la trace dit ce qui s'est passé sans conserver le
  contenu de la session.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, TextIO

from config import (
    APP_ID,
    DEFAULT_TRACE_MAX_AGE_DAYS,
    DEFAULT_TRACE_MAX_BYTES,
    DEFAULT_TRACE_MAX_RUNS,
    TRACE_FORMAT_VERSION,
    Settings,
)
from core.events import Event, EventType
from core.run_index import FINISHED, INDEX_NAME, STARTED, RunIndex, scan_interrupted
from core.trace_crypto import (
    TraceKeyError,
    decrypt_record,
    encrypt_record,
    is_encrypted,
    load_key,
)
from security.redaction import redactor

__all__ = [
    "DEFAULT_MAX_AGE_DAYS",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_RUNS",
    "RunTrace",
    "TraceFormatError",
    "TraceKeyError",
    "TraceReadReport",
    "build_trace",
    "read_events",
    "read_trace",
]

logger = logging.getLogger(f"{APP_ID}.trace")

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")

#: Rétention par défaut : assez pour comparer quelques sessions, sans laisser
#: grossir indéfiniment `data/logs/runs`. Les valeurs vivent dans `config.py`
#: pour que l'utilisateur puisse les ajuster sans modifier le code.
DEFAULT_MAX_RUNS = DEFAULT_TRACE_MAX_RUNS
DEFAULT_MAX_AGE_DAYS = DEFAULT_TRACE_MAX_AGE_DAYS
DEFAULT_MAX_BYTES = DEFAULT_TRACE_MAX_BYTES

#: Durée de validité d'un marqueur d'activité. Un processus tué laisse son
#: marqueur ; au-delà de ce délai il n'empêche plus la rétention. La valeur
#: dépasse le délai d'un appel au fournisseur, pendant lequel rien n'est écrit.
DEFAULT_ACTIVE_TTL_S = 300.0

#: Le marqueur est rafraîchi tous les N événements (et non à chaque ligne) :
#: un `utime` par fragment de flux serait un appel système inutile.
_MARKER_REFRESH_EVENTS = 20

#: Champs porteurs du contenu de la session, retirés en mode métadonnées.
_CONTENT_KEYS = frozenset({"task", "text", "content"})


class TraceFormatError(ValueError):
    """Trace illisible en mode strict (ligne vide, JSON invalide, non-objet)."""


@dataclass(slots=True)
class TraceReadReport:
    """Bilan d'une lecture : lignes lues, ignorées, coupure finale probable."""

    lines: int = 0
    events: int = 0
    ignored: int = 0
    truncated_final: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "lines": self.lines,
            "events": self.events,
            "ignored": self.ignored,
            "truncated_final": self.truncated_final,
        }


def read_events(
    path: Path | str,
    *,
    strict: bool = False,
    report: TraceReadReport | None = None,
    key: bytes | None = None,
) -> Iterator[dict[str, Any]]:
    """Événements d'une trace, en ignorant les lignes illisibles.

    Une coupure en pleine écriture laisse une demi-ligne JSON en fin de fichier :
    le lecteur doit rendre les événements valides précédents, pas échouer.

    `strict=True` lève `TraceFormatError` au premier défaut ; le mode par défaut
    reste tolérant mais remplit `report`, ce qui distingue « coupure finale
    attendue » de « trace corrompue au milieu ».

    Une trace chiffrée exige `key` : sans elle, `TraceKeyError` plutôt qu'un
    payload vide qui laisserait croire que le run n'a rien fait.
    """
    target = Path(path)
    counts = report if report is not None else TraceReadReport()
    ends_with_newline = True
    # Une trace archivée reste lisible : le rejeu ne doit pas dépendre de
    # l'extension choisie par la rétention.
    handle_ctx = (
        gzip.open(target, "rt", encoding="utf-8")
        if target.name.endswith(".gz")
        else target.open(encoding="utf-8")
    )
    with handle_ctx as handle:
        for number, line in enumerate(handle, start=1):
            counts.lines += 1
            ends_with_newline = line.endswith("\n")
            body = line.strip()
            if not body:
                if strict:
                    raise TraceFormatError(f"ligne {number} vide dans {target.name}")
                counts.ignored += 1
                continue
            try:
                record = json.loads(body)
            except ValueError as exc:
                if strict:
                    raise TraceFormatError(
                        f"ligne {number} illisible dans {target.name} : {exc}"
                    ) from exc
                logger.debug("ligne de trace ignorée", exc_info=True)
                counts.ignored += 1
                continue
            if not isinstance(record, dict):
                if strict:
                    raise TraceFormatError(
                        f"ligne {number} n'est pas un objet JSON dans {target.name}"
                    )
                counts.ignored += 1
                continue
            if is_encrypted(record):
                if key is None:
                    raise TraceKeyError(
                        f"trace chiffrée ({target.name}) : fournir la clé de déchiffrement"
                    )
                record = decrypt_record(record, key)
            counts.events += 1
            yield record
    # Une dernière ligne sans saut de ligne est le signe d'une coupure en pleine
    # écriture, pas d'une corruption : on le dit sans le confondre avec le reste.
    counts.truncated_final = bool(counts.ignored) and not ends_with_newline


def read_trace(
    path: Path | str, *, strict: bool = False, key: bytes | None = None
) -> tuple[list[dict[str, Any]], TraceReadReport]:
    """Comme `read_events`, mais retourne tous les événements avec leur bilan."""
    report = TraceReadReport()
    records = list(read_events(path, strict=strict, report=report, key=key))
    return records, report


def workspace_slug(directory: Path | str | None = None) -> str:
    """Nom de dossier stable et lisible pour un espace de travail.

    Le nom seul ne suffit pas (deux projets peuvent s'appeler `src`) : un court
    condensat du chemin complet évite de mélanger deux quotas de rétention.
    """
    base = Path(directory) if directory is not None else Path.cwd()
    name = _SAFE_NAME.sub("_", base.name)[:40].strip("._") or "espace"
    digest = hashlib.sha1(str(base).lower().encode("utf-8")).hexdigest()[:6]
    return f"{name}-{digest}"


def build_trace(settings: Settings, directory: Path | str | None = None) -> "RunTrace | None":
    """Trace conforme à la configuration, ou `None` si elle est désactivée."""
    cfg = settings.trace
    if not cfg.enabled:
        return None
    root = Path(directory) if directory else settings.paths.log_dir / "runs"
    if cfg.per_workspace:
        root = root / workspace_slug()
    key: bytes | None = None
    if cfg.encrypt:
        try:
            key = load_key(cfg.key_env)
        except TraceKeyError as exc:
            # Fail-closed : mieux vaut pas de trace qu'une trace en clair.
            logger.warning("traces désactivées : %s", exc)
            return None
    return RunTrace(
        root,
        content=cfg.content,
        max_runs=cfg.max_runs,
        max_age_days=cfg.max_age_days,
        max_bytes=cfg.max_bytes,
        archive=cfg.archive,
        archive_min_bytes=cfg.archive_min_bytes,
        fsync_every=cfg.fsync_every,
        active_ttl_s=cfg.active_ttl_s,
        index=cfg.index,
        encrypt_key=key,
    )


def _run_stem(path: Path) -> str:
    """`a.jsonl` et `a.jsonl.gz` désignent la même exécution `a`."""
    name = path.name[:-3] if path.name.endswith(".gz") else path.name
    return name[:-6] if name.endswith(".jsonl") else name


class RunTrace:
    """Abonné de bus qui écrit `<directory>/<run_id>.jsonl`."""

    def __init__(
        self,
        directory: Path | str,
        *,
        sanitize: Callable[[Any], Any] | None = None,
        max_runs: int = DEFAULT_MAX_RUNS,
        max_age_days: float = DEFAULT_MAX_AGE_DAYS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        content: str = "full",
        archive: bool = False,
        archive_min_bytes: int = 0,
        fsync_every: int = 0,
        active_ttl_s: float = DEFAULT_ACTIVE_TTL_S,
        index: bool = True,
        encrypt_key: bytes | None = None,
    ) -> None:
        self.directory = Path(directory)
        self._sanitize = sanitize or redactor.redact_data
        self.max_runs = max(1, int(max_runs))
        self.max_age_days = max(0.0, float(max_age_days))
        self.max_bytes = max(0, int(max_bytes))
        #: Place gardée pour l'enregistrement `trace_truncated` : sans elle, un
        #: plafond atteint laisserait une trace incomplète sans le dire. Trop
        #: petite pour être utile en dessous de 1 Kio, elle est alors abandonnée.
        self._reserve = 220 if self.max_bytes > 1024 else 0
        self.content = content if content in {"full", "metadata"} else "full"
        self.archive = bool(archive)
        #: Taille à partir de laquelle la trace est archivée dès sa fermeture
        #: (0 désactive l'archivage immédiat ; `_prune` reste le filet de fond).
        self.archive_min_bytes = max(0, int(archive_min_bytes))
        self.fsync_every = max(0, int(fsync_every))
        self.active_ttl_s = max(1.0, float(active_ttl_s))
        self.index = bool(index)
        #: Clé AEAD des payloads ; `None` écrit en clair (choix explicite de l'appelant).
        self.encrypt_key = bytes(encrypt_key) if encrypt_key else None
        self._handle: TextIO | None = None
        self._run_id = ""
        self._last_seq = 0
        self._beat_stop: threading.Event | None = None
        self._beat: threading.Thread | None = None
        self.path: Path | None = None
        self._marker_path: Path | None = None
        self._bytes_written = 0
        self._events_since_touch = 0
        self._lines_since_sync = 0
        #: Vrai si le plafond de taille a interrompu l'écriture de ce run.
        self.truncated = False

    def __call__(self, event: Event) -> None:
        try:
            if event.type == EventType.RUN_STARTED:
                self._open(event.run_id)
                self._index_start(event.payload)
            if self._handle is None or event.run_id != self._run_id:
                return
            payload = self._payload(event)
            if payload is not None:
                record = {
                    "run_id": event.run_id,
                    "timestamp": datetime.fromtimestamp(
                        event.timestamp, tz=timezone.utc
                    ).isoformat(),
                    "type": event.type,
                    "seq": event.seq,
                    "payload": payload,
                }
                if self.encrypt_key is not None:
                    record = encrypt_record(record, self.encrypt_key)
                if not self._write(record):
                    self._truncate(event.run_id)
        except Exception:  # noqa: BLE001 - une trace ne casse pas une exécution
            logger.exception("trace impossible pour %s", event.type)
            self.close()
        if event.type == EventType.RUN_FINISHED:
            self._index_finish(event.payload)
            self.close(finished=True)

    # -- contenu ------------------------------------------------------------

    def _payload(self, event: Event) -> dict[str, Any] | None:
        """Payload expurgé, réduit aux métadonnées si demandé.

        Un fragment de flux est du texte de réponse : en mode métadonnées il
        disparaît entièrement, pas seulement son contenu, pour ne pas laisser
        croire qu'un événement vide s'est produit.
        """
        payload = self._sanitize(event.payload)
        if self.content == "full" or not isinstance(payload, dict):
            return payload
        if event.type == EventType.STREAM_CHUNK:
            return None
        return {key: value for key, value in payload.items() if key not in _CONTENT_KEYS}

    # -- écriture -----------------------------------------------------------

    def _write(self, record: dict[str, Any], *, control: bool = False) -> bool:
        """Écrit un enregistrement ; faux si le plafond de taille s'y oppose."""
        handle = self._handle
        if handle is None:
            return False
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        size = len(line.encode("utf-8"))
        limit = self.max_bytes - (0 if control else self._reserve)
        if self.max_bytes and self._bytes_written + size > limit:
            # Le plafond s'applique aussi au fichier courant : un run très
            # bavard ne doit pas dépasser la taille prévue à lui seul.
            return False
        handle.write(line)
        handle.flush()
        self._last_seq = int(record.get("seq") or self._last_seq)
        self._bytes_written += size
        self._lines_since_sync += 1
        if self.fsync_every and self._lines_since_sync >= self.fsync_every:
            self._sync()
        self._events_since_touch += 1
        if self._events_since_touch >= _MARKER_REFRESH_EVENTS:
            self._events_since_touch = 0
            self._touch_marker()
        return True

    def _truncate(self, run_id: str) -> None:
        """Signale une trace incomplète dans la trace elle-même."""
        if self.truncated:
            return
        self.truncated = True
        logger.warning(
            "trace interrompue à %d octets ; la suite de %s n'est pas écrite",
            self.max_bytes,
            run_id,
        )
        record = {
            "run_id": run_id,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "type": EventType.TRACE_TRUNCATED,
            # Après le dernier événement : le vérificateur exige une
            # séquence strictement croissante.
            "seq": self._last_seq + 1,
            "payload": {
                "max_bytes": self.max_bytes,
                "bytes": self._bytes_written,
                "trace_format": TRACE_FORMAT_VERSION,
            },
        }
        # Une trace chiffrée ne mélange pas lignes claires et chiffrées : un
        # contrôle laissé en clair ouvrirait la porte à une ligne injectée.
        if self.encrypt_key is not None:
            record = encrypt_record(record, self.encrypt_key)
        self._write(record, control=True)

    def _sync(self) -> None:
        """Force l'écriture disque (le `flush` seul ne survit pas à une coupure)."""
        handle = self._handle
        if handle is None:
            return
        try:
            os.fsync(handle.fileno())
            self._lines_since_sync = 0
        except (OSError, ValueError):
            logger.debug("synchronisation disque impossible", exc_info=True)

    # -- ouverture / fermeture ----------------------------------------------

    def _open(self, run_id: str) -> None:
        self.close()
        self._run_id = str(run_id or "")
        if not self._run_id:
            return
        name = _SAFE_NAME.sub("_", self._run_id)[:80]
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.index:
            # Un run sans `run_finished` et sans marqueur frais est déclaré
            # interrompu avant que la rétention ne puisse l'effacer sans trace.
            scan_interrupted(self.directory, active_ttl_s=self.active_ttl_s)
        self._prune(keep=f"{name}.jsonl")
        self.path = self.directory / f"{name}.jsonl"
        self._marker_path = self.directory / f"{name}.active"
        self._handle = self.path.open("a", encoding="utf-8")
        self.truncated = False
        self._events_since_touch = 0
        self._lines_since_sync = 0
        try:
            self._bytes_written = self.path.stat().st_size
        except OSError:
            self._bytes_written = 0
        self._touch_marker(create=True)
        self._start_heartbeat()

    # -- index --------------------------------------------------------------

    def _index_start(self, payload: dict[str, Any]) -> None:
        """Inscrit le démarrage : la liste des runs n'a plus à lire chaque fichier."""
        if not self.index or self.path is None:
            return
        RunIndex(self.directory).mark(
            self._run_id,
            STARTED,
            path=self.path,
            manifest={
                "started": datetime.now(tz=timezone.utc).isoformat(),
                "app_version": payload.get("app_version", ""),
                "model": payload.get("model", ""),
                "workspace": payload.get("workspace", ""),
                "trace_format": payload.get("trace_format", TRACE_FORMAT_VERSION),
            },
        )

    def _index_finish(self, payload: dict[str, Any]) -> None:
        if not self.index or self.path is None:
            return
        RunIndex(self.directory).mark(
            self._run_id,
            FINISHED,
            path=self.path,
            stop_reason=str(payload.get("stop_reason") or ""),
            ok=bool(payload.get("ok")),
            detail="trace tronquee" if self.truncated else "",
        )

    # -- battement ----------------------------------------------------------

    def _start_heartbeat(self) -> None:
        """Rafraîchit le marqueur même quand aucun événement n'est écrit.

        Un appel d'outil long (navigateur, OCR, fsync sur un partage réseau) ne
        produit aucun événement : sans battement, le marqueur vieillirait et un
        autre processus pourrait purger la trace d'un run pourtant vivant.
        """
        self._stop_heartbeat()
        interval = max(1.0, self.active_ttl_s / 4)
        stop = threading.Event()
        self._beat_stop = stop

        def beat() -> None:
            while not stop.wait(interval):
                self._touch_marker()

        thread = threading.Thread(
            target=beat, name="omniagent-trace-beat", daemon=True
        )
        thread.start()
        self._beat = thread

    def _stop_heartbeat(self) -> None:
        stop, self._beat_stop = self._beat_stop, None
        if stop is not None:
            stop.set()
        beat, self._beat = self._beat, None
        if beat is not None and beat is not threading.current_thread():
            beat.join(timeout=1.0)

    def _touch_marker(self, *, create: bool = False) -> None:
        """Pose ou rafraîchit `<run>.active` : ce run ne doit pas être purgé."""
        marker = self._marker_path
        if marker is None:
            return
        try:
            if create or not marker.exists():
                marker.write_text(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "run_id": self._run_id,
                            "trace_format": TRACE_FORMAT_VERSION,
                            "since": datetime.now(tz=timezone.utc).isoformat(),
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            else:
                os.utime(marker, None)
        except OSError:
            logger.debug("marqueur d'activité indisponible : %s", marker, exc_info=True)

    def _active_runs(self) -> set[str]:
        """Exécutions ouvertes ailleurs : marqueur frais, ou supprimé si périmé."""
        active: set[str] = set()
        try:
            markers = list(self.directory.glob("*.active"))
        except OSError:
            return active
        now = time.time()
        for marker in markers:
            try:
                age = now - marker.stat().st_mtime
            except OSError:
                continue
            if age > self.active_ttl_s:
                # Processus tué sans fermeture : le marqueur ne protège plus rien.
                self._remove(marker)
                continue
            # `vivant.active` designe l'execution `vivant` (`.stem` retire le seul
            # suffixe `.active`, la ou `_run_stem` retire `.jsonl[.gz]`).
            active.add(marker.stem)
        return active

    # -- rétention ----------------------------------------------------------

    def _files(self, pattern: str) -> list[Path]:
        """Fichiers de trace du dossier ; l'index n'en est pas une."""
        try:
            return [
                path
                for path in self.directory.glob(pattern)
                if path.is_file() and path.name != INDEX_NAME
            ]
        except OSError:
            logger.debug("rétention impossible : dossier illisible", exc_info=True)
            return []

    def _prune(self, keep: str) -> None:
        """Applique la rétention avant d'ouvrir la trace courante.

        Trois bornes, dans cet ordre : âge, nombre de fichiers, taille totale.
        Le fichier courant est toujours épargné, ainsi que toute trace dont un
        autre processus a laissé un marqueur d'activité encore frais ; une
        suppression impossible est journalisée sans interrompre l'exécution.
        """
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.debug("rétention impossible : dossier non créable", exc_info=True)
            return

        active = self._active_runs()
        plain = [
            path
            for path in self._files("*.jsonl")
            if path.name != keep and _run_stem(path) not in active
        ]
        # Les archives ne comptent pas dans `max_runs` (sinon archiver ne
        # réduirait jamais le nombre) mais pèsent dans le quota de taille et
        # dans la limite d'âge.
        archived = [
            path for path in self._files("*.jsonl.gz") if _run_stem(path) not in active
        ]

        def entries_of(paths: list[Path]) -> list[tuple[float, int, Path]]:
            found: list[tuple[float, int, Path]] = []
            for path in paths:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                found.append((stat.st_mtime, stat.st_size, path))
            found.sort()  # du plus ancien au plus récent
            return found

        entries = entries_of(plain)
        archives = entries_of(archived)

        if self.max_age_days:
            cutoff = time.time() - self.max_age_days * 86_400

            def fresh(entry: tuple[float, int, Path]) -> bool:
                if entry[0] >= cutoff:
                    return True
                self._discard(entry[2])
                return False

            entries = [entry for entry in entries if fresh(entry)]
            archives = [entry for entry in archives if fresh(entry)]

        current = self.directory / keep
        total = sum(size for _, size, _ in entries) + sum(
            size for _, size, _ in archives
        )
        try:
            total += current.stat().st_size
        except OSError:
            pass
        for mtime, size, path in list(entries):
            # Le fichier courant compte dans le quota : `max_runs` runs au total.
            over_count = len(entries) >= self.max_runs
            over_size = self.max_bytes > 0 and total > self.max_bytes
            if not (over_count or over_size):
                break
            if self._discard(path):
                entries.remove((mtime, size, path))
                total -= size
        # L'archivage ne réduit pas le nombre de fichiers : la taille, elle, se
        # règle en dernier recours sur les archives les plus anciennes.
        for entry in list(archives):
            if not (self.max_bytes > 0 and total > self.max_bytes):
                break
            if self._discard(entry[2]):
                archives.remove(entry)
                total -= entry[1]
        # L'index suit la même rétention : une ligne par trace encore présente,
        # pas une ligne par run historique, sinon il grossirait sans fin.
        if self.index:
            RunIndex(self.directory).compact()

    def _discard(self, path: Path) -> bool:
        """Supprime une trace, ou l'archive en `.gz` si l'archivage est actif."""
        if self.archive and path.name.endswith(".jsonl"):
            if self._archive(path):
                return True
        return self._remove(path)

    def _archive(self, path: Path) -> bool:
        """Compresse au lieu de détruire : l'historique reste consultable."""
        target = path.with_name(path.name + ".gz")
        try:
            with path.open("rb") as source, gzip.open(target, "wb") as sink:
                shutil.copyfileobj(source, sink)
            path.unlink()
            return True
        except OSError:
            logger.debug("archivage impossible de %s", path, exc_info=True)
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def _remove(self, path: Path) -> bool:
        try:
            path.unlink()
            return True
        except OSError:
            logger.debug("rétention : suppression impossible de %s", path, exc_info=True)
            return False

    def close(self, *, finished: bool = False) -> None:
        """Ferme la trace, retire le marqueur et, si demandé, archive le run.

        `finished=True` n'est posé qu'après `run_finished` : une trace encore
        ouverte par une exécution en cours n'est jamais compressée sous les
        pieds de son lecteur.
        """
        handle, self._handle = self._handle, None
        if handle is not None:
            if self.fsync_every:
                try:
                    os.fsync(handle.fileno())
                except (OSError, ValueError):  # pragma: no cover - best effort
                    logger.debug("synchronisation de fermeture impossible", exc_info=True)
            try:
                handle.close()
            except OSError:  # pragma: no cover - fermeture best-effort
                logger.debug("fermeture de trace impossible", exc_info=True)
        self._stop_heartbeat()
        marker, self._marker_path = self._marker_path, None
        if marker is not None:
            try:
                marker.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - suppression best-effort
                logger.debug("marqueur d'activité non supprimé", exc_info=True)
        if finished:
            self._archive_finished()

    def _archive_finished(self) -> None:
        """Compresse le run terminé au-delà du seuil, sans attendre le suivant."""
        if not (self.archive and self.archive_min_bytes) or self.path is None:
            return
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.archive_min_bytes:
            return
        target = self.path.with_name(self.path.name + ".gz")
        if self._archive(self.path):
            self.path = target
            if self.index:
                RunIndex(self.directory).mark(
                    self._run_id,
                    FINISHED,
                    path=target,
                    detail="archive a la fermeture",
                )

    def summary(self) -> dict[str, Any]:
        """État de la trace courante, pour l'affichage ou un bilan."""
        return {
            "path": str(self.path) if self.path is not None else "",
            "run_id": self._run_id,
            "content": self.content,
            "bytes": self._bytes_written,
            "truncated": self.truncated,
            "encrypted": self.encrypt_key is not None,
            "trace_format": TRACE_FORMAT_VERSION,
        }
