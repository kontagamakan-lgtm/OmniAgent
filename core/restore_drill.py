"""Répétition de restauration : prouver qu'une sauvegarde sert vraiment.

Une archive peut passer l'inspection (manifeste, JSONL relisible) et rester
inutilisable : base SQLite corrompue, index incohérent, configuration illisible.
Le drill restaure donc l'archive dans un dossier temporaire, relit réellement
chaque fichier critique, puis supprime tout — sans jamais écrire dans les
données vivantes.

    python app.py --restore-drill backups/omniagent-....ombak
    python -m core.restore_drill backups/omniagent-....ombak

Un archive illisible est refusée **avant** toute écriture : le dossier
temporaire n'est créé qu'après `inspect_backup`. Le rapport donne la durée, les
contrôles et l'état du nettoyage.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from config import APP_ID, Settings
from core.backup import _read_archive, _safe_name, inspect_backup
from core.trace import read_events
from core.trace_crypto import TraceKeyError, optional_key

__all__ = ["DrillReport", "drill_archive", "format_report", "main"]

logger = logging.getLogger(f"{APP_ID}.restore_drill")


@dataclass(slots=True)
class DrillReport:
    """Résultat d'une répétition : rien n'est modifié dans les données vivantes."""

    archive: Path
    ok: bool = False
    duration_s: float = 0.0
    files: int = 0
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    cleaned: bool = False
    workdir: str = ""

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, bool(ok), str(detail)))
        if not ok:
            self.problems.append(f"{name} : {detail or 'échec'}")

    def format(self) -> str:
        lines = [
            f"Archive : {self.archive}",
            f"Durée : {self.duration_s:.2f} s, {self.files} fichier(s)",
            f"Dossier temporaire : {'supprimé' if self.cleaned else self.workdir or '(non créé)'}",
        ]
        for name, ok, detail in self.checks:
            lines.append(f"  {'ok  ' if ok else 'ECHEC'} {name}" + (f" — {detail}" if detail else ""))
        if self.problems:
            lines.append("Problemes :")
            lines.extend(f"- {problem}" for problem in self.problems)
        else:
            lines.append("Restauration repetee : ok, donnees vivantes intactes.")
        return "\n".join(lines)


def _check_jsonl(path: Path, key: bytes | None) -> tuple[bool, str]:
    """Relit une trace comme le ferait une reprise, ligne par ligne."""
    try:
        events = sum(1 for _event in read_events(path, key=key))
    except (TraceKeyError, OSError, ValueError) as exc:
        return False, str(exc)
    if events == 0:
        return False, "aucun événement lisible"
    return True, f"{events} événement(s)"


def _check_sqlite(path: Path) -> tuple[bool, str]:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return False, str(exc)
    result = str(row[0] if row else "")
    return result.lower() == "ok", result or "réponse vide"


def _check_index(path: Path) -> tuple[bool, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            if line.strip():
                json.loads(line)
    except (OSError, ValueError) as exc:
        return False, str(exc)
    return True, f"{sum(1 for line in lines if line.strip())} entrée(s)"


def _check_config(path: Path) -> tuple[bool, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, str(exc)
    return isinstance(value, dict), "objet JSON" if isinstance(value, dict) else "objet attendu"


def drill_archive(
    settings: Settings,
    source: Path | str,
    *,
    passphrase: str | None = None,
    key: bytes | None = None,
    keep: bool = False,
) -> DrillReport:
    """Restaure dans un dossier jetable, vérifie, puis supprime."""
    if key is None:
        try:
            key = optional_key(settings.trace.key_env)
        except TraceKeyError:
            key = None
    archive_path = Path(source)
    report = DrillReport(archive=archive_path)
    started = time.monotonic()

    inspection = inspect_backup(archive_path, passphrase=passphrase)
    if not inspection.get("ok"):
        report.problems.extend(str(item) for item in inspection.get("problems") or [])
        report.check("inspection", False, "archive refusée avant écriture")
        report.duration_s = round(time.monotonic() - started, 3)
        return report
    manifest = inspection.get("manifest") or {}

    try:
        archive, _manifest = _read_archive(archive_path, passphrase)
    except Exception as exc:  # noqa: BLE001 - archive illisible de toute facon
        report.check("ouverture", False, str(exc))
        report.duration_s = round(time.monotonic() - started, 3)
        return report

    base = Path(settings.paths.data_dir)
    try:
        base.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix=".restore-drill-", dir=base))
    except OSError as exc:
        archive.close()
        report.check("dossier temporaire", False, str(exc))
        report.duration_s = round(time.monotonic() - started, 3)
        return report
    report.workdir = str(workdir)

    written: list[Path] = []
    try:
        with archive:
            for item in manifest.get("files") or []:
                if not isinstance(item, dict):
                    continue
                try:
                    name = _safe_name(str(item.get("path")))
                except ValueError as exc:
                    report.check("chemin", False, str(exc))
                    continue
                target = workdir / Path(*name.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(str(name)) as handle, target.open("wb") as sink:
                    shutil.copyfileobj(handle, sink)
                written.append(target)
                report.files += 1
        report.check("extraction", report.files > 0, f"{report.files} fichier(s)")

        for path in written:
            suffix = path.suffix.lower()
            if suffix in {".jsonl", ".gz"}:
                ok, detail = _check_jsonl(path, key)
                report.check(f"trace {path.relative_to(workdir)}", ok, detail)
            elif suffix in {".db", ".sqlite", ".sqlite3"}:
                ok, detail = _check_sqlite(path)
                report.check(f"base {path.relative_to(workdir)}", ok, detail)
            elif path.name == "runs.index":
                ok, detail = _check_index(path)
                report.check("index des runs", ok, detail)
            elif path.name == "config.json":
                ok, detail = _check_config(path)
                report.check("configuration", ok, detail)
    except (OSError, ValueError, KeyError) as exc:
        report.check("restauration", False, str(exc))
    finally:
        if keep:
            report.cleaned = False
        else:
            shutil.rmtree(workdir, ignore_errors=True)
            report.cleaned = not workdir.exists()

    report.ok = not report.problems
    report.duration_s = round(time.monotonic() - started, 3)
    logger.info(
        "drill de restauration %s : %s (%d fichier(s), %.2f s)",
        archive_path.name,
        "ok" if report.ok else "echec",
        report.files,
        report.duration_s,
    )
    return report


def format_report(report: DrillReport) -> str:
    return report.format()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.restore_drill",
        description="Répète une restauration dans un dossier jetable, sans toucher aux données.",
    )
    parser.add_argument("archive", help="archive .ombak à vérifier")
    parser.add_argument("--keep", action="store_true", help="conserve le dossier restauré")
    args = parser.parse_args(argv)

    settings = Settings.load(strict=False)
    report = drill_archive(settings, args.archive, keep=args.keep)
    print(report.format())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
