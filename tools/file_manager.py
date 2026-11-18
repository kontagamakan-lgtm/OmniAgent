"""Gestion des fichiers et répertoires locaux.

Confinement
-----------
Toute opération passe par :meth:`FileManager._resolve`, qui :

1. développe les variables d'environnement et ``~`` ;
2. résout les liens symboliques et les composants ``..`` ;
3. vérifie que le chemin final est *réellement* sous l'une des racines
   autorisées (`files.allowed_roots`) — un lien symbolique qui pointerait hors
   zone est donc rejeté ;
4. rejette les noms réservés Windows (``CON``, ``NUL``, ``COM1``…) et les flux
   de données alternatifs (``fichier:flux``) ;
5. vérifie les motifs d'exclusion (`files.denied_globs`).

Aucune opération n'accepte de joker dans un chemin : les motifs sont traités
par les paramètres dédiés (`glob`, `content_regex`).
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import logging
import os
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from config import APP_ID, Settings
from tools.base import FunctionTool, Tool, ToolError, object_schema, param

__all__ = [
    "FileManager",
    "FileOperationError",
    "FileToolError",
    "PathNotAllowed",
    "app_file_manager",
    "build_file_tools",
]

logger = logging.getLogger(f"{APP_ID}.files")

_CHUNK = 1024 * 1024
_TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *{f"com{i}" for i in range(1, 10)},
    *{f"lpt{i}" for i in range(1, 10)},
}
_INVALID_NAME_CHARS = set('<>:"/\\|?*')
_MAX_SCAN_FILES = 20000

# Classement utilisé par `organize`.
CATEGORIES: dict[str, tuple[str, ...]] = {
    "Images": (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".svg", ".heic"),
    "Documents": (".pdf", ".docx", ".doc", ".txt", ".md", ".rtf", ".odt", ".xlsx", ".xlsm", ".xls",
                  ".csv", ".pptx", ".ppt", ".odp", ".ods", ".epub"),
    "Archives": (".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".iso"),
    "Code": (".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".xml", ".yaml", ".yml",
             ".ps1", ".bat", ".cmd", ".sh", ".sql", ".c", ".cpp", ".h", ".hpp", ".cs", ".java",
             ".go", ".rs", ".php", ".rb", ".toml", ".ini", ".cfg"),
    "Audio": (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma"),
    "Vidéos": (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".flv", ".m4v"),
    "Exécutables": (".exe", ".msi", ".dll", ".bat", ".cmd"),
}


class FileToolError(ToolError):
    """Erreur générique des outils fichiers."""

    code = "file_error"


class PathNotAllowed(FileToolError):
    """Chemin hors périmètre ou interdit."""

    code = "path_not_allowed"


class FileOperationError(FileToolError):
    """Opération impossible (droits, existence, type, taille…)."""

    code = "file_operation_failed"


def _iso(timestamp: float) -> str:
    try:
        return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("o", "Kio", "Mio", "Gio", "Tio"):
        if value < 1024 or unit == "Tio":
            return f"{value:.0f} {unit}" if unit == "o" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} o"


class FileManager:
    """Opérations fichiers confinées aux racines autorisées."""

    def __init__(self, settings: Settings, *, logger_: logging.Logger | None = None) -> None:
        self.settings = settings
        self.logger = logger_ or logger
        self.roots: list[Path] = []
        for raw in settings.files.allowed_roots:
            try:
                self.roots.append(Path(os.path.expandvars(raw)).expanduser().resolve())
            except OSError as exc:  # pragma: no cover - disque absent
                self.logger.warning("racine ignorée (%s): %s", raw, exc)
        if not self.roots:
            self.roots = [Path.cwd().resolve()]
        self.workspace = self.roots[0]
        self.denied = [self._normalize_pattern(p) for p in settings.files.denied_globs]

    # -- confinement --------------------------------------------------------

    @staticmethod
    def _normalize_pattern(pattern: str) -> str:
        return pattern.replace("\\", "/").strip().lower()

    def _denied(self, path: Path) -> bool:
        text = path.as_posix().lower()
        return any(fnmatch.fnmatch(text, pattern) for pattern in self.denied)

    def _is_allowed(self, path: Path) -> bool:
        for root in self.roots:
            try:
                if path == root or path.is_relative_to(root):
                    return True
            except (ValueError, OSError):
                continue
        return False

    @staticmethod
    def _reject_unsafe_components(path: Path) -> None:
        parts = path.parts
        for part in parts[1:] if parts and parts[0].endswith(("\\", "/")) else parts:
            if ":" in part:
                raise PathNotAllowed(
                    f"flux de données alternatif interdit dans '{part}'"
                )
            stem = part.split(".")[0].strip().lower()
            if stem in _RESERVED_NAMES:
                raise PathNotAllowed(f"nom réservé Windows interdit : '{part}'")

    def _resolve(
        self, raw: str | os.PathLike[str] | None, *, must_exist: bool = False
    ) -> Path:
        text = "." if raw is None or str(raw).strip() == "" else str(raw).strip()
        if any(character in text for character in ("*", "?")):
            raise PathNotAllowed("joker interdit dans un chemin ; utilisez le paramètre glob")
        expanded = Path(os.path.expandvars(text)).expanduser()
        candidate = expanded if expanded.is_absolute() else self.workspace / expanded
        self._reject_unsafe_components(candidate)
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            raise FileOperationError(f"chemin illisible : {exc}") from exc
        if not self._is_allowed(resolved):
            raise PathNotAllowed(
                f"chemin hors des racines autorisées : {resolved}"
            )
        if self._denied(resolved):
            raise PathNotAllowed(f"chemin exclu par la configuration : {resolved}")
        if must_exist and not resolved.exists():
            raise FileOperationError(f"chemin introuvable : {resolved}")
        return resolved

    def resolve(self, path: str | os.PathLike[str] | None, *, must_exist: bool = False) -> Path:
        """Confinement public réutilisable par les autres modules.

        Applique la même politique que les outils `fs_*` : développement des
        variables d'environnement et de `~`, résolution des liens et des `..`,
        appartenance réelle à une racine autorisée, puis `denied_globs`.
        """
        return self._resolve(path, must_exist=must_exist)

    def _check_new_name(self, name: str) -> None:
        if not name or name in {".", ".."}:
            raise FileOperationError("nom de fichier vide")
        if set(name) & _INVALID_NAME_CHARS:
            raise FileOperationError(f"caractère interdit dans le nom : {name!r}")
        if name.split(".")[0].strip().lower() in _RESERVED_NAMES:
            raise FileOperationError(f"nom réservé Windows : {name!r}")
        if len(name) > 255:
            raise FileOperationError("nom de fichier trop long (> 255 caractères)")

    @staticmethod
    def _is_hidden(path: Path) -> bool:
        if path.name.startswith("."):
            return True
        if os.name != "nt":
            return False
        try:
            attributes = getattr(path.stat(), "st_file_attributes", 0)
        except OSError:
            return False
        return bool(attributes & 2)  # FILE_ATTRIBUTE_HIDDEN

    # -- lecture ------------------------------------------------------------

    def hash_file(self, path: str | os.PathLike[str], algorithm: str = "sha256") -> dict[str, Any]:
        target = self._resolve(path, must_exist=True)
        if not target.is_file():
            raise FileOperationError(f"pas un fichier : {target}")
        try:
            digest = hashlib.new(algorithm)
        except ValueError as exc:
            raise FileOperationError(f"algorithme inconnu : {algorithm}") from exc
        with target.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
        return {
            "path": str(target),
            "algorithm": algorithm,
            "hash": digest.hexdigest(),
            "size_bytes": target.stat().st_size,
        }

    def info(self, path: str | os.PathLike[str], *, with_hash: bool = False) -> dict[str, Any]:
        target = self._resolve(path, must_exist=True)
        stat = target.stat()
        payload: dict[str, Any] = {
            "path": str(target),
            "name": target.name,
            "parent": str(target.parent),
            "is_dir": target.is_dir(),
            "is_file": target.is_file(),
            "size_bytes": stat.st_size,
            "size_human": _human_size(stat.st_size),
            "created": _iso(stat.st_ctime),
            "modified": _iso(stat.st_mtime),
            "accessed": _iso(stat.st_atime),
            "suffix": target.suffix.lower(),
            "hidden": self._is_hidden(target),
            "writable": bool(stat.st_mode & 0o200),
        }
        if with_hash and target.is_file():
            payload["sha256"] = self.hash_file(target)["hash"]
        return payload

    def list_dir(
        self,
        path: str | os.PathLike[str] = ".",
        *,
        pattern: str | None = None,
        recursive: bool = False,
        include_hidden: bool = False,
        max_items: int = 500,
    ) -> dict[str, Any]:
        base = self._resolve(path, must_exist=True)
        if not base.is_dir():
            raise FileOperationError(f"pas un répertoire : {base}")

        entries: list[dict[str, Any]] = []
        truncated = False
        iterator = base.rglob("*") if recursive else base.iterdir()
        for item in iterator:
            if len(entries) >= max_items:
                truncated = True
                break
            try:
                resolved = item.resolve(strict=False)
                if self._denied(resolved) or not self._is_allowed(resolved):
                    continue
                if not include_hidden and self._is_hidden(item):
                    continue
                if pattern and not fnmatch.fnmatch(item.name.lower(), pattern.lower()):
                    continue
                stat = item.stat()
                entries.append(
                    {
                        "name": item.name,
                        "path": str(resolved),
                        "type": "dir" if item.is_dir() else "file",
                        "size_bytes": stat.st_size if item.is_file() else 0,
                        "modified": _iso(stat.st_mtime),
                        "suffix": item.suffix.lower(),
                    }
                )
            except OSError as exc:
                entries.append({"name": item.name, "path": str(item), "type": "error", "error": str(exc)})

        entries.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))
        return {
            "path": str(base),
            "count": len(entries),
            "truncated": truncated,
            "entries": entries,
        }

    def read_text(
        self,
        path: str | os.PathLike[str],
        *,
        max_bytes: int | None = None,
        encoding: str | None = None,
        max_chars: int = 0,
    ) -> dict[str, Any]:
        target = self._resolve(path, must_exist=True)
        if target.is_dir():
            raise FileOperationError(f"pas un fichier : {target}")
        limit = max_bytes or self.settings.files.max_read_bytes
        size = target.stat().st_size
        if size > limit:
            raise FileOperationError(
                f"fichier trop volumineux : {_human_size(size)} > {_human_size(limit)}"
            )
        raw = target.read_bytes()
        if b"\x00" in raw[:8192]:
            raise FileOperationError(
                f"fichier binaire : {target.name} (utilisez un convertisseur dédié)"
            )
        text, used = self._decode(raw, encoding)
        truncated = False
        if max_chars and len(text) > max_chars:
            text = text[:max_chars]
            truncated = True
        return {
            "path": str(target),
            "encoding": used,
            "size_bytes": size,
            "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
            "truncated": truncated,
            "text": text,
        }

    @staticmethod
    def _decode(raw: bytes, encoding: str | None) -> tuple[str, str]:
        if encoding:
            try:
                return raw.decode(encoding), encoding
            except (LookupError, UnicodeDecodeError) as exc:
                raise FileOperationError(f"décodage impossible en {encoding} : {exc}") from exc
        for candidate in _TEXT_ENCODINGS:
            try:
                return raw.decode(candidate), candidate
            except UnicodeDecodeError:
                continue
        return raw.decode("latin-1", errors="replace"), "latin-1"

    # -- écriture -----------------------------------------------------------

    def write_text(
        self,
        path: str | os.PathLike[str],
        content: str,
        *,
        mode: str = "w",
        create_dirs: bool = True,
        overwrite: bool = True,
        backup: bool | None = None,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        if mode not in {"w", "a"}:
            raise FileOperationError("mode d'écriture accepté : 'w' ou 'a'")
        target = self._resolve(path)
        if target.exists() and target.is_dir():
            raise FileOperationError(f"un répertoire occupe ce chemin : {target}")

        data = content.encode(encoding)
        limit = self.settings.files.max_write_bytes
        if len(data) > limit:
            raise FileOperationError(
                f"écriture refusée : {_human_size(len(data))} > {_human_size(limit)}"
            )

        backup_path = ""
        if mode == "a" and target.exists():
            with target.open("ab") as handle:
                handle.write(data)
            written = len(data)
        else:
            if target.exists() and not overwrite:
                raise FileOperationError(f"le fichier existe déjà : {target}")
            if create_dirs:
                target.parent.mkdir(parents=True, exist_ok=True)
            should_backup = (
                self.settings.files.backup_before_overwrite if backup is None else backup
            )
            if should_backup and target.exists():
                backup_path = str(self._backup(target))
            tmp = target.with_name(target.name + ".omni-tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
            written = len(data)

        return {
            "path": str(target),
            "bytes_written": written,
            "mode": mode,
            "backup": backup_path,
        }

    def create_dir(self, path: str | os.PathLike[str], *, parents: bool = True) -> dict[str, Any]:
        target = self._resolve(path)
        existed = target.exists()
        if existed and not target.is_dir():
            raise FileOperationError(f"un fichier occupe ce chemin : {target}")
        if not existed:
            target.mkdir(parents=parents, exist_ok=True)
        return {"path": str(target), "created": not existed}

    @staticmethod
    def _backup(target: Path) -> Path:
        backup = target.with_name(target.name + ".bak")
        shutil.copy2(target, backup)
        return backup

    def _unique_target(self, target: Path, reserved: set[str]) -> Path:
        """Trouve un nom libre : 'rapport.pdf' → 'rapport (1).pdf'."""
        if not target.exists() and str(target).lower() not in reserved:
            return target
        stem, suffix = target.stem, target.suffix
        for index in range(1, 1000):
            candidate = target.with_name(f"{stem} ({index}){suffix}")
            if not candidate.exists() and str(candidate).lower() not in reserved:
                return candidate
        raise FileOperationError(f"impossible de trouver un nom libre pour {target.name}")

    def _destination(
        self, source: Path, destination: str | os.PathLike[str], conflict: str, reserved: set[str]
    ) -> Path | None:
        raw = self._resolve(destination)
        target = raw / source.name if raw.is_dir() else raw
        self._check_new_name(target.name)
        if not self._is_allowed(target) or self._denied(target):
            raise PathNotAllowed(f"destination hors périmètre : {target}")
        if target.exists() or str(target).lower() in reserved:
            if conflict == "skip":
                return None
            if conflict == "rename":
                return self._unique_target(target, reserved)
            if conflict != "overwrite":
                raise FileOperationError(f"stratégie de conflit inconnue : {conflict}")
        return target

    @staticmethod
    def _as_list(paths: str | os.PathLike[str] | Iterable[str | os.PathLike[str]]) -> list[str]:
        if isinstance(paths, (str, os.PathLike)):
            return [str(paths)]
        return [str(item) for item in paths]

    def copy(
        self,
        sources: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
        destination: str | os.PathLike[str],
        *,
        conflict: str = "rename",
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        reserved: set[str] = set()
        for raw in self._as_list(sources):
            try:
                source = self._resolve(raw, must_exist=True)
                target = self._destination(source, destination, conflict, reserved)
                if target is None:
                    results.append({"source": str(source), "status": "ignoré (existe déjà)"})
                    continue
                reserved.add(str(target).lower())
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=False)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                results.append({"source": str(source), "target": str(target), "status": "copié"})
            except (ToolError, OSError, shutil.Error) as exc:
                results.append({"source": raw, "status": "échec", "error": str(exc)})
        return self._summary(results)

    def move(
        self,
        sources: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
        destination: str | os.PathLike[str],
        *,
        conflict: str = "rename",
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        reserved: set[str] = set()
        for raw in self._as_list(sources):
            try:
                source = self._resolve(raw, must_exist=True)
                target = self._destination(source, destination, conflict, reserved)
                if target is None:
                    results.append({"source": str(source), "status": "ignoré (existe déjà)"})
                    continue
                reserved.add(str(target).lower())
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
                results.append({"source": str(source), "target": str(target), "status": "déplacé"})
            except (ToolError, OSError, shutil.Error) as exc:
                results.append({"source": raw, "status": "échec", "error": str(exc)})
        return self._summary(results)

    @staticmethod
    def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
        succeeded = sum(1 for item in results if item.get("status") in {"copié", "déplacé", "supprimé"})
        failed = sum(1 for item in results if item.get("status") == "échec")
        return {"count": len(results), "succeeded": succeeded, "failed": failed, "results": results}

    # -- suppression --------------------------------------------------------

    def delete(
        self,
        paths: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
        *,
        use_recycle_bin: bool | None = None,
    ) -> dict[str, Any]:
        recycle = self.settings.files.use_recycle_bin if use_recycle_bin is None else use_recycle_bin
        results: list[dict[str, Any]] = []
        for raw in self._as_list(paths):
            try:
                target = self._resolve(raw, must_exist=True)
                if target in self.roots:
                    raise PathNotAllowed(f"suppression d'une racine autorisée refusée : {target}")
                if recycle and os.name == "nt":
                    ok, error = _recycle_paths([target])
                    if ok:
                        results.append({"path": str(target), "status": "supprimé", "method": "corbeille"})
                    else:
                        raise FileOperationError(f"corbeille indisponible : {error}")
                else:
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                    results.append({"path": str(target), "status": "supprimé", "method": "définitif"})
            except (ToolError, OSError) as exc:
                results.append({"path": raw, "status": "échec", "error": str(exc)})
        return self._summary(results)

    # -- renommage en masse -------------------------------------------------

    def rename_bulk(
        self,
        directory: str | os.PathLike[str],
        pattern: str,
        replacement: str,
        *,
        glob: str = "*",
        recursive: bool = False,
        dry_run: bool = True,
        include_dirs: bool = False,
        ignore_case: bool = False,
    ) -> dict[str, Any]:
        base = self._resolve(directory, must_exist=True)
        if not base.is_dir():
            raise FileOperationError(f"pas un répertoire : {base}")
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise FileOperationError(f"expression régulière invalide : {exc}") from exc

        iterator = base.rglob(glob) if recursive else base.glob(glob)
        plan: list[dict[str, Any]] = []
        seen_targets: set[str] = set()
        for item in iterator:
            if not include_dirs and item.is_dir():
                continue
            if self._denied(item.resolve(strict=False)):
                continue
            new_name = regex.sub(replacement, item.name)
            if new_name == item.name:
                continue
            try:
                self._check_new_name(new_name)
            except FileOperationError as exc:
                plan.append({"source": str(item), "target": "", "status": f"nom invalide : {exc}"})
                continue
            target = item.with_name(new_name)
            key = str(target).lower()
            if key in seen_targets or (target.exists() and target != item):
                plan.append({"source": str(item), "target": str(target), "status": "collision"})
                continue
            seen_targets.add(key)
            plan.append({"source": str(item), "target": str(target), "status": "prévu"})

        moved: list[tuple[Path, Path]] = []
        if not dry_run:
            try:
                for step in plan:
                    if step["status"] != "prévu":
                        continue
                    source, target = Path(step["source"]), Path(step["target"])
                    source.rename(target)
                    moved.append((target, source))
                    step["status"] = "renommé"
            except OSError as exc:
                for target, source in reversed(moved):  # retour arrière
                    try:
                        target.rename(source)
                    except OSError:  # pragma: no cover - retour arrière best effort
                        self.logger.error("retour arrière impossible pour %s", target)
                raise FileOperationError(f"renommage interrompu et annulé : {exc}") from exc

        renameable = [step for step in plan if step["status"] in {"prévu", "renommé"}]
        return {
            "directory": str(base),
            "dry_run": dry_run,
            "planned": len(renameable),
            "skipped": len(plan) - len(renameable),
            "changes": plan,
        }

    # -- recherche ----------------------------------------------------------

    def search(
        self,
        query: str = "",
        *,
        root: str | os.PathLike[str] = ".",
        glob: str | None = None,
        content_regex: str | None = None,
        max_results: int = 100,
        max_file_bytes: int = 2_000_000,
        ignore_case: bool = True,
    ) -> dict[str, Any]:
        base = self._resolve(root, must_exist=True)
        if not base.is_dir():
            raise FileOperationError(f"pas un répertoire : {base}")

        flags = re.IGNORECASE if ignore_case else 0
        try:
            content_pattern = re.compile(content_regex, flags) if content_regex else None
        except re.error as exc:
            raise FileOperationError(f"expression régulière invalide : {exc}") from exc
        needle = query.lower()

        hits: list[dict[str, Any]] = []
        scanned = 0
        truncated = False
        for current, dirnames, filenames in os.walk(base):
            dirnames[:] = [
                name
                for name in dirnames
                if not self._denied(Path(current) / name)
            ]
            for filename in filenames:
                if len(hits) >= max_results:
                    truncated = True
                    break
                full = Path(current) / filename
                if self._denied(full) or not self._is_allowed(full):
                    continue
                if glob and not fnmatch.fnmatch(filename.lower(), glob.lower()):
                    continue
                scanned += 1
                if scanned > _MAX_SCAN_FILES:
                    truncated = True
                    break
                if needle and needle in filename.lower():
                    hits.append({"path": str(full), "kind": "nom", "line": 0, "snippet": filename})
                if content_pattern is None:
                    continue
                try:
                    if full.stat().st_size > max_file_bytes:
                        continue
                    raw = full.read_bytes()
                    if b"\x00" in raw[:4096]:
                        continue
                    text, _encoding = self._decode(raw, None)
                except OSError:
                    continue
                for number, line in enumerate(text.splitlines(), start=1):
                    if content_pattern.search(line):
                        hits.append(
                            {
                                "path": str(full),
                                "kind": "contenu",
                                "line": number,
                                "snippet": line.strip()[:300],
                            }
                        )
                        if len(hits) >= max_results:
                            truncated = True
                            break
            if truncated:
                break

        return {
            "root": str(base),
            "query": query,
            "content_regex": content_regex,
            "scanned_files": scanned,
            "count": len(hits),
            "truncated": truncated,
            "hits": hits,
        }

    # -- entretien du disque ------------------------------------------------

    def find_duplicates(
        self,
        root: str | os.PathLike[str] = ".",
        *,
        min_size: int = 1,
        max_files: int = _MAX_SCAN_FILES,
    ) -> dict[str, Any]:
        base = self._resolve(root, must_exist=True)
        by_size: dict[int, list[Path]] = {}
        scanned = 0
        for current, dirnames, filenames in os.walk(base):
            dirnames[:] = [name for name in dirnames if not self._denied(Path(current) / name)]
            for filename in filenames:
                full = Path(current) / filename
                try:
                    if self._denied(full):
                        continue
                    size = full.stat().st_size
                except OSError:
                    continue
                if size < min_size:
                    continue
                by_size.setdefault(size, []).append(full)
                scanned += 1
                if scanned >= max_files:
                    break
            if scanned >= max_files:
                break

        groups: list[dict[str, Any]] = []
        wasted = 0
        for size, candidates in by_size.items():
            if len(candidates) < 2:
                continue
            by_hash: dict[str, list[Path]] = {}
            for candidate in candidates:
                try:
                    digest = self.hash_file(candidate)["hash"]
                except (ToolError, OSError):
                    continue
                by_hash.setdefault(digest, []).append(candidate)
            for digest, paths in by_hash.items():
                if len(paths) < 2:
                    continue
                wasted += size * (len(paths) - 1)
                groups.append(
                    {
                        "sha256": digest,
                        "size_bytes": size,
                        "size_human": _human_size(size),
                        "count": len(paths),
                        "paths": [str(path) for path in paths],
                    }
                )
        groups.sort(key=lambda group: group["size_bytes"] * (group["count"] - 1), reverse=True)
        return {
            "root": str(base),
            "scanned_files": scanned,
            "groups": len(groups),
            "wasted_bytes": wasted,
            "wasted_human": _human_size(wasted),
            "duplicates": groups,
        }

    def organize(
        self,
        directory: str | os.PathLike[str],
        *,
        dry_run: bool = True,
        copy: bool = False,
        include_hidden: bool = False,
    ) -> dict[str, Any]:
        base = self._resolve(directory, must_exist=True)
        if not base.is_dir():
            raise FileOperationError(f"pas un répertoire : {base}")

        extension_map = {
            extension: category
            for category, extensions in CATEGORIES.items()
            for extension in extensions
        }
        plan: list[dict[str, str]] = []
        reserved: set[str] = set()
        for item in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if item.is_dir():
                continue
            if not include_hidden and self._is_hidden(item):
                continue
            category = extension_map.get(item.suffix.lower(), "Autres")
            target_dir = base / category
            target = target_dir / item.name
            if str(target).lower() in reserved:
                target = self._unique_target(target, reserved)
            reserved.add(str(target).lower())
            plan.append(
                {
                    "source": str(item),
                    "target": str(target),
                    "category": category,
                    "status": "prévu",
                }
            )

        if not dry_run:
            for step in plan:
                source = Path(step["source"])
                target = Path(step["target"])
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if copy:
                        shutil.copy2(source, target)
                    else:
                        shutil.move(str(source), str(target))
                    step["status"] = "copié" if copy else "déplacé"
                except OSError as exc:
                    step["status"] = f"échec : {exc}"

        return {
            "directory": str(base),
            "dry_run": dry_run,
            "files": len(plan),
            "categories": sorted({step["category"] for step in plan}),
            "plan": plan,
        }

    # -- archives -----------------------------------------------------------

    def archive(
        self,
        sources: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
        destination: str | os.PathLike[str],
        *,
        compression: str = "deflate",
    ) -> dict[str, Any]:
        target = self._resolve(destination)
        if target.suffix.lower() != ".zip":
            target = target.with_suffix(".zip")
        if target.exists():
            raise FileOperationError(f"l'archive existe déjà : {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = zipfile.ZIP_STORED if compression == "store" else zipfile.ZIP_DEFLATED
        added = 0
        with zipfile.ZipFile(target, "w", compression=mode) as archive:
            for raw in self._as_list(sources):
                source = self._resolve(raw, must_exist=True)
                if source.is_dir():
                    root_parent = source.parent
                    for file_path in source.rglob("*"):
                        if file_path.is_file() and not self._denied(file_path):
                            archive.write(file_path, file_path.relative_to(root_parent))
                            added += 1
                else:
                    archive.write(source, source.name)
                    added += 1
        return {
            "archive": str(target),
            "entries": added,
            "size_bytes": target.stat().st_size,
            "size_human": _human_size(target.stat().st_size),
        }

    def extract(
        self,
        archive_path: str | os.PathLike[str],
        destination: str | os.PathLike[str] = ".",
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        source = self._resolve(archive_path, must_exist=True)
        if source.suffix.lower() != ".zip":
            raise FileOperationError("seules les archives .zip sont prises en charge")
        dest = self._resolve(destination)
        dest.mkdir(parents=True, exist_ok=True)
        dest_resolved = dest.resolve()
        extracted: list[str] = []
        with zipfile.ZipFile(source) as archive:
            for member in archive.infolist():
                target = (dest / member.filename).resolve()
                # Protection contre la traversée de répertoire (zip slip)
                if target != dest_resolved and not target.is_relative_to(dest_resolved):
                    raise FileOperationError(f"archive suspecte : entrée hors destination '{member.filename}'")
                if self._denied(target):
                    continue
                if target.exists() and not overwrite and not member.is_dir():
                    raise FileOperationError(f"fichier déjà présent : {target}")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                extracted.append(str(target))
        return {"archive": str(source), "destination": str(dest), "files": len(extracted), "extracted": extracted}

    # -- conversion ---------------------------------------------------------

    _CONVERSIONS: dict[tuple[str, str], str] = {
        (".png", ".jpg"): "image",
        (".png", ".webp"): "image",
        (".jpg", ".png"): "image",
        (".jpeg", ".png"): "image",
        (".webp", ".png"): "image",
        (".bmp", ".png"): "image",
        (".tif", ".png"): "image",
        (".tiff", ".png"): "image",
        (".heic", ".jpg"): "image",
        (".docx", ".txt"): "docx_text",
        (".docx", ".md"): "docx_text",
        (".pdf", ".txt"): "pdf_text",
        (".xlsx", ".csv"): "xlsx_csv",
        (".csv", ".xlsx"): "csv_xlsx",
        (".txt", ".docx"): "text_docx",
        (".md", ".docx"): "text_docx",
        (".html", ".txt"): "html_text",
        (".htm", ".txt"): "html_text",
    }

    def document_text(
        self,
        source: str | os.PathLike[str],
        *,
        max_chars: int = 200_000,
    ) -> str:
        """Texte exploitable d'un fichier, quel que soit son format.

        Les formats bureautiques passent par les mêmes extractions que
        `convert()` (`.pdf→.txt`, `.docx→.txt`, `.xlsx→.csv`) sans écrire de
        fichier intermédiaire ; le reste est lu comme du texte brut.
        """
        src = self._resolve(source, must_exist=True)
        if not src.is_file():
            raise FileOperationError(f"pas un fichier : {src}")
        suffix = src.suffix.lower()
        if suffix == ".pdf":
            text = self._pdf_text(src)
        elif suffix == ".docx":
            text = self._docx_text(src)
        elif suffix == ".xlsx":
            text = self._xlsx_text(src)
        else:
            return str(self.read_text(str(src), max_chars=max_chars)["text"])
        return text[: max(1, int(max_chars))]

    def convert(
        self,
        source: str | os.PathLike[str],
        target_format: str,
        destination: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any]:
        src = self._resolve(source, must_exist=True)
        if not src.is_file():
            raise FileOperationError(f"pas un fichier : {src}")
        target_suffix = target_format.strip().lower()
        if not target_suffix.startswith("."):
            target_suffix = "." + target_suffix
        kind = self._CONVERSIONS.get((src.suffix.lower(), target_suffix))
        if kind is None:
            supported = ", ".join(f"{a}→{b}" for a, b in sorted(self._CONVERSIONS))
            raise FileOperationError(
                f"conversion {src.suffix}→{target_suffix} non prise en charge ; disponibles : {supported}"
            )

        target = self._resolve(destination) if destination else src.with_suffix(target_suffix)
        if destination and target.is_dir():
            target = target / (src.stem + target_suffix)
        if target == src:
            raise FileOperationError("source et destination identiques")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileOperationError(f"la destination existe déjà : {target}")

        getattr(self, f"_convert_{kind}")(src, target)
        return {
            "source": str(src),
            "target": str(target),
            "format": target_suffix.lstrip("."),
            "size_bytes": target.stat().st_size,
            "size_human": _human_size(target.stat().st_size),
        }

    @staticmethod
    def _require(module: str, package: str) -> Any:
        try:
            return __import__(module, fromlist=["_"])
        except ImportError as exc:
            raise FileOperationError(
                f"conversion impossible : installez '{package}' (pip install {package})"
            ) from exc

    def _convert_image(self, source: Path, target: Path) -> None:
        module = self._require("PIL.Image", "Pillow")
        with module.open(source) as image:
            if target.suffix.lower() in {".jpg", ".jpeg"} and image.mode in {"RGBA", "P", "LA"}:
                image = image.convert("RGB")
            image.save(target)

    def _docx_text(self, source: Path) -> str:
        """Texte des paragraphes et des cellules de tableau d'un .docx."""
        document_module = self._require("docx", "python-docx")
        document = document_module.Document(str(source))
        parts = [paragraph.text for paragraph in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        return "\n".join(parts)

    def _convert_docx_text(self, source: Path, target: Path) -> None:
        target.write_text(self._docx_text(source), encoding="utf-8")

    def _pdf_text(self, source: Path) -> str:
        """Texte page par page d'un PDF ; une page scannée reste vide."""
        pypdf = self._require("pypdf", "pypdf")
        reader = pypdf.PdfReader(str(source))
        chunks: list[str] = []
        for index, page in enumerate(reader.pages, start=1):
            chunks.append(f"--- Page {index} ---\n{page.extract_text() or ''}")
        return "\n\n".join(chunks)

    def _convert_pdf_text(self, source: Path, target: Path) -> None:
        target.write_text(self._pdf_text(source), encoding="utf-8")

    def _xlsx_text(self, source: Path) -> str:
        """CSV (séparateur « ; ») de toutes les feuilles, en-têtes compris."""
        openpyxl = self._require("openpyxl", "openpyxl")
        workbook = openpyxl.load_workbook(str(source), read_only=True, data_only=True)
        import csv

        buffer = io.StringIO()
        try:
            writer = csv.writer(buffer, delimiter=";")
            for sheet in workbook.worksheets:
                writer.writerow([f"### Feuille : {sheet.title}"])
                for row in sheet.iter_rows(values_only=True):
                    if any(cell is not None for cell in row):
                        writer.writerow(["" if cell is None else cell for cell in row])
                writer.writerow([])
        finally:
            workbook.close()
        return buffer.getvalue()

    def _convert_xlsx_csv(self, source: Path, target: Path) -> None:
        target.write_text(self._xlsx_text(source), encoding="utf-8", newline="")

    def _convert_csv_xlsx(self, source: Path, target: Path) -> None:
        openpyxl = self._require("openpyxl", "openpyxl")
        import csv

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        with source.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            sample = handle.read(4096)
            handle.seek(0)
            delimiter = ";" if sample.count(";") >= sample.count(",") else ","
            for row in csv.reader(handle, delimiter=delimiter):
                sheet.append(row)
        workbook.save(str(target))

    def _convert_text_docx(self, source: Path, target: Path) -> None:
        document_module = self._require("docx", "python-docx")
        document = document_module.Document()
        for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("#"):
                document.add_heading(line.lstrip("#").strip(), level=min(4, len(line) - len(line.lstrip("#"))))
            else:
                document.add_paragraph(line)
        document.save(str(target))

    def _convert_html_text(self, source: Path, target: Path) -> None:
        raw = source.read_text(encoding="utf-8", errors="replace")
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
            text = re.sub(r"[ \t]+", " ", text)
            target.write_text(re.sub(r"\n{3,}", "\n\n", text).strip(), encoding="utf-8")
            return
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        target.write_text(soup.get_text("\n", strip=True), encoding="utf-8")

    # -- disque -------------------------------------------------------------

    def disk_usage(self, path: str | os.PathLike[str] = ".") -> dict[str, Any]:
        target = self._resolve(path, must_exist=True)
        usage = shutil.disk_usage(target)
        return {
            "path": str(target),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "total_human": _human_size(usage.total),
            "free_human": _human_size(usage.free),
            "percent_used": round(usage.used / usage.total * 100, 1) if usage.total else 0.0,
        }


def app_file_manager(settings: Settings, logger_: logging.Logger | None = None) -> FileManager:
    """FileManager autorisant en plus le dossier de données de l'application.

    Les captures et sauvegardes de l'agent vivent sous `data/`, qui sort des
    racines utilisateur quand l'exécutable est installé (``%LOCALAPPDATA%``).
    """
    manager = FileManager(settings, logger_=logger_)
    data_root = Path(settings.paths.data_dir).resolve()
    if data_root not in manager.roots:
        manager.roots.append(data_root)
    return manager


# ---------------------------------------------------------------------------
# Corbeille Windows (SHFileOperationW, sans dépendance)
# ---------------------------------------------------------------------------


def _recycle_paths(paths: list[Path]) -> tuple[bool, str]:
    """Envoie des chemins à la corbeille Windows. Retourne (succès, erreur)."""
    if os.name != "nt":  # pragma: no cover - plateforme non Windows
        return False, "corbeille indisponible hors Windows"
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:  # pragma: no cover
        return False, "ctypes indisponible"

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 3
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040
    FOF_NOERRORUI = 0x0400

    joined = "\0".join(str(path.resolve()) for path in paths) + "\0\0"
    buffer = ctypes.create_unicode_buffer(joined, len(joined) + 1)

    operation = SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = FO_DELETE
    operation.pFrom = ctypes.cast(buffer, ctypes.c_wchar_p)
    operation.pTo = None
    operation.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    operation.fAnyOperationsAborted = False
    operation.hNameMappings = None
    operation.lpszProgressTitle = None

    try:
        code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    except OSError as exc:  # pragma: no cover - API indisponible
        return False, str(exc)
    if code != 0:
        return False, f"SHFileOperationW a renvoyé {code}"
    if operation.fAnyOperationsAborted:
        return False, "opération annulée par le système"
    return True, ""


# ---------------------------------------------------------------------------
# Outils exposés au modèle
# ---------------------------------------------------------------------------


def build_file_tools(manager: FileManager) -> list[Tool]:
    """Construit la liste d'outils `fs_*` liés à ce gestionnaire."""

    def fs_list(path: str = ".", pattern: str = "", recursive: bool = False,
                include_hidden: bool = False, max_items: int = 200) -> dict[str, Any]:
        return manager.list_dir(
            path,
            pattern=pattern or None,
            recursive=recursive,
            include_hidden=include_hidden,
            max_items=max_items,
        )

    def fs_info(path: str, with_hash: bool = False) -> dict[str, Any]:
        return manager.info(path, with_hash=with_hash)

    def fs_read_text(path: str, max_chars: int = 20000, encoding: str = "") -> dict[str, Any]:
        return manager.read_text(path, max_chars=max_chars, encoding=encoding or None)

    def fs_write_text(path: str, content: str, overwrite: bool = True, append: bool = False) -> dict[str, Any]:
        return manager.write_text(path, content, mode="a" if append else "w", overwrite=overwrite)

    def fs_mkdir(path: str) -> dict[str, Any]:
        return manager.create_dir(path)

    def fs_copy(paths: list[str], destination: str, conflict: str = "rename") -> dict[str, Any]:
        return manager.copy(paths, destination, conflict=conflict)

    def fs_move(paths: list[str], destination: str, conflict: str = "rename") -> dict[str, Any]:
        return manager.move(paths, destination, conflict=conflict)

    def fs_delete(paths: list[str], use_recycle_bin: bool = True) -> dict[str, Any]:
        return manager.delete(paths, use_recycle_bin=use_recycle_bin)

    def fs_rename_bulk(
        directory: str,
        pattern: str,
        replacement: str,
        glob: str = "*",
        recursive: bool = False,
        dry_run: bool = True,
        include_dirs: bool = False,
    ) -> dict[str, Any]:
        return manager.rename_bulk(
            directory,
            pattern,
            replacement,
            glob=glob or "*",
            recursive=recursive,
            dry_run=dry_run,
            include_dirs=include_dirs,
        )

    def fs_search(
        query: str = "",
        root: str = ".",
        glob: str = "",
        content_regex: str = "",
        max_results: int = 50,
    ) -> dict[str, Any]:
        return manager.search(
            query,
            root=root,
            glob=glob or None,
            content_regex=content_regex or None,
            max_results=max_results,
        )

    def fs_find_duplicates(root: str = ".", min_size: int = 1) -> dict[str, Any]:
        return manager.find_duplicates(root, min_size=min_size)

    def fs_organize(directory: str, dry_run: bool = True, copy: bool = False) -> dict[str, Any]:
        return manager.organize(directory, dry_run=dry_run, copy=copy)

    def fs_archive(paths: list[str], destination: str, compression: str = "deflate") -> dict[str, Any]:
        return manager.archive(paths, destination, compression=compression)

    def fs_extract(archive_path: str, destination: str = ".", overwrite: bool = False) -> dict[str, Any]:
        return manager.extract(archive_path, destination, overwrite=overwrite)

    def fs_convert(source: str, target_format: str, destination: str = "") -> dict[str, Any]:
        return manager.convert(source, target_format, destination or None)

    def fs_disk_usage(path: str = ".") -> dict[str, Any]:
        return manager.disk_usage(path)

    def fs_hash(path: str, algorithm: str = "sha256") -> dict[str, Any]:
        return manager.hash_file(path, algorithm)

    conflict_enum = ["rename", "overwrite", "skip"]

    tools: list[Tool] = [
        FunctionTool(
            "fs_list",
            "Liste un répertoire (fichiers et dossiers) avec tailles et dates de modification.",
            object_schema(
                {
                    "path": param("string", "Répertoire à lister", default="."),
                    "pattern": param("string", "Filtre glob sur le nom (ex. *.pdf)", default=""),
                    "recursive": param("boolean", "Descendre récursivement", default=False),
                    "include_hidden": param("boolean", "Inclure les fichiers cachés", default=False),
                    "max_items": param("integer", "Limite d'entrées", default=200, minimum=1, maximum=2000),
                }
            ),
            fs_list,
        ),
        FunctionTool(
            "fs_info",
            "Métadonnées complètes d'un fichier ou dossier (taille, dates, droits).",
            object_schema(
                {
                    "path": param("string", "Chemin cible"),
                    "with_hash": param("boolean", "Calculer aussi le SHA-256", default=False),
                },
                required=["path"],
            ),
            fs_info,
        ),
        FunctionTool(
            "fs_read_text",
            "Lit un fichier texte (détection automatique de l'encodage, troncature paramétrable).",
            object_schema(
                {
                    "path": param("string", "Fichier à lire"),
                    "max_chars": param("integer", "Nombre maximal de caractères retournés", default=20000, minimum=0, maximum=200000),
                    "encoding": param("string", "Forcer un encodage (utf-8, cp1252…)", default=""),
                },
                required=["path"],
            ),
            fs_read_text,
        ),
        FunctionTool(
            "fs_write_text",
            "Écrit ou complète un fichier texte (sauvegarde .bak automatique de l'ancienne version).",
            object_schema(
                {
                    "path": param("string", "Fichier à écrire"),
                    "content": param("string", "Contenu complet"),
                    "overwrite": param("boolean", "Autoriser le remplacement", default=True),
                    "append": param("boolean", "Ajouter à la fin au lieu de remplacer", default=False),
                },
                required=["path", "content"],
            ),
            fs_write_text,
            read_only=False,
        ),
        FunctionTool(
            "fs_mkdir",
            "Crée un répertoire (et ses parents).",
            object_schema({"path": param("string", "Répertoire à créer")}, required=["path"]),
            fs_mkdir,
            read_only=False,
        ),
        FunctionTool(
            "fs_copy",
            "Copie un ou plusieurs fichiers/dossiers vers une destination.",
            object_schema(
                {
                    "paths": param("array", "Chemins sources", items=param("string", "Chemin"), min_items=1),
                    "destination": param("string", "Répertoire ou fichier cible"),
                    "conflict": param("string", "Conflit de nom", enum=conflict_enum, default="rename"),
                },
                required=["paths", "destination"],
            ),
            fs_copy,
            read_only=False,
        ),
        FunctionTool(
            "fs_move",
            "Déplace ou renomme un ou plusieurs fichiers/dossiers.",
            object_schema(
                {
                    "paths": param("array", "Chemins sources", items=param("string", "Chemin"), min_items=1),
                    "destination": param("string", "Répertoire ou nouveau chemin"),
                    "conflict": param("string", "Conflit de nom", enum=conflict_enum, default="rename"),
                },
                required=["paths", "destination"],
            ),
            fs_move,
            read_only=False,
        ),
        FunctionTool(
            "fs_delete",
            "Supprime des fichiers ou dossiers (corbeille par défaut, restauration possible).",
            object_schema(
                {
                    "paths": param("array", "Chemins à supprimer", items=param("string", "Chemin"), min_items=1),
                    "use_recycle_bin": param("boolean", "Passer par la corbeille Windows", default=True),
                },
                required=["paths"],
            ),
            fs_delete,
            read_only=False,
            dangerous=True,
        ),
        FunctionTool(
            "fs_rename_bulk",
            "Renommage en masse par expression régulière, avec simulation préalable (dry_run) et retour arrière automatique en cas d'erreur.",
            object_schema(
                {
                    "directory": param("string", "Répertoire de travail"),
                    "pattern": param("string", "Expression régulière appliquée au nom"),
                    "replacement": param("string", "Remplacement (groupes \\1, \\2…)"),
                    "glob": param("string", "Filtre glob des fichiers concernés", default="*"),
                    "recursive": param("boolean", "Descendre récursivement", default=False),
                    "dry_run": param("boolean", "Simuler sans modifier", default=True),
                    "include_dirs": param("boolean", "Renommer aussi les dossiers", default=False),
                },
                required=["directory", "pattern", "replacement"],
            ),
            fs_rename_bulk,
            read_only=False,
        ),
        FunctionTool(
            "fs_search",
            "Recherche des fichiers par nom et/ou par contenu (expression régulière), dans une racine donnée.",
            object_schema(
                {
                    "query": param("string", "Fragment de nom de fichier", default=""),
                    "root": param("string", "Racine de recherche", default="."),
                    "glob": param("string", "Filtre glob (ex. *.py)", default=""),
                    "content_regex": param("string", "Expression régulière cherchée dans le contenu", default=""),
                    "max_results": param("integer", "Nombre maximal de résultats", default=50, minimum=1, maximum=500),
                }
            ),
            fs_search,
        ),
        FunctionTool(
            "fs_find_duplicates",
            "Détecte les doublons par empreinte SHA-256 et calcule l'espace récupérable.",
            object_schema(
                {
                    "root": param("string", "Racine à analyser", default="."),
                    "min_size": param("integer", "Taille minimale en octets", default=1, minimum=0),
                }
            ),
            fs_find_duplicates,
        ),
        FunctionTool(
            "fs_organize",
            "Range les fichiers d'un dossier dans des sous-dossiers thématiques (Images, Documents, Code…). Simulation par défaut.",
            object_schema(
                {
                    "directory": param("string", "Dossier à organiser"),
                    "dry_run": param("boolean", "Simuler sans déplacer", default=True),
                    "copy": param("boolean", "Copier au lieu de déplacer", default=False),
                },
                required=["directory"],
            ),
            fs_organize,
            read_only=False,
        ),
        FunctionTool(
            "fs_archive",
            "Compresse des fichiers/dossiers dans une archive ZIP.",
            object_schema(
                {
                    "paths": param("array", "Chemins à archiver", items=param("string", "Chemin"), min_items=1),
                    "destination": param("string", "Chemin de l'archive .zip"),
                    "compression": param("string", "Compression", enum=["deflate", "store"], default="deflate"),
                },
                required=["paths", "destination"],
            ),
            fs_archive,
            read_only=False,
        ),
        FunctionTool(
            "fs_extract",
            "Extrait une archive ZIP (protection contre la traversée de répertoire).",
            object_schema(
                {
                    "archive_path": param("string", "Archive .zip"),
                    "destination": param("string", "Répertoire d'extraction", default="."),
                    "overwrite": param("boolean", "Écraser les fichiers existants", default=False),
                },
                required=["archive_path"],
            ),
            fs_extract,
            read_only=False,
        ),
        FunctionTool(
            "fs_convert",
            "Convertit un fichier entre formats (images, docx/txt, pdf/txt, xlsx/csv, html/txt).",
            object_schema(
                {
                    "source": param("string", "Fichier source"),
                    "target_format": param("string", "Format cible (ex. pdf, png, csv, txt, docx)"),
                    "destination": param("string", "Chemin de sortie (facultatif)", default=""),
                },
                required=["source", "target_format"],
            ),
            fs_convert,
            read_only=False,
        ),
        FunctionTool(
            "fs_disk_usage",
            "Espace disque total, utilisé et libre pour un chemin.",
            object_schema({"path": param("string", "Chemin de référence", default=".")}),
            fs_disk_usage,
        ),
        FunctionTool(
            "fs_hash",
            "Calcule l'empreinte d'un fichier (sha256, md5, sha1…).",
            object_schema(
                {
                    "path": param("string", "Fichier à hacher"),
                    "algorithm": param("string", "Algorithme", default="sha256"),
                },
                required=["path"],
            ),
            fs_hash,
        ),
    ]

    return tools
