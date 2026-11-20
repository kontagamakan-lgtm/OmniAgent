"""Cerveau Obsidian : vault Markdown, liens et mémorisation continue.

Le vault est la mémoire longue durée de l'agent : chaque note est un fichier
Markdown avec un frontmatter lisible, des liens `[[...]]` et des tags. Le fichier
`00_CORTEX_INDEX.md` est régénéré après chaque écriture ; il liste les notes du
vault et sert de point d'entrée lorsqu'on ouvre le dossier dans Obsidian.

L'installation d'Obsidian est séparée de l'écriture du vault. Le vault reste
utilisable même si l'application Obsidian n'est pas installée ; le bootstrap
`ensure_obsidian()` ne fait l'installation que sur demande explicite ou au
premier lancement graphique avec `obsidian.auto_install`.

Confidentialité : tout ce qui est écrit (titre, corps, source) passe par le
redactor, les résultats d'outils marqués sensibles ne sont jamais mémorisés et
les dossiers cachés d'Obsidian (`.obsidian`, `.trash`) ne sont ni indexés ni
cherchés.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from config import APP_ID, Settings
from security.redaction import redact
from tools.base import FunctionTool, Tool, ToolResult, object_schema, param

__all__ = [
    "AUTO_MEMORY_TOOLS",
    "INDEX_NAME",
    "ObsidianBrain",
    "OBSIDIAN_DOWNLOAD_URL",
    "OBSIDIAN_INSTALLER_ENV",
    "OBSIDIAN_RELEASES_API",
    "build_obsidian_tools",
    "download_obsidian_installer",
    "ensure_obsidian",
    "find_obsidian",
    "install_obsidian",
    "resolve_obsidian_installer",
    "vault_path",
    "verify_authenticode",
]

logger = logging.getLogger(f"{APP_ID}.obsidian")

INDEX_NAME = "00_CORTEX_INDEX.md"
OBSIDIAN_WINGET_ID = "Obsidian.Obsidian"
OBSIDIAN_DOWNLOAD_URL = "https://obsidian.md/download"
OBSIDIAN_RELEASES_API = (
    "https://api.github.com/repos/obsidianmd/obsidian-releases/releases/latest"
)
#: Variable qui court-circuite l'API pour fournir une URL d'installateur.
OBSIDIAN_INSTALLER_ENV = "OBSIDIAN_INSTALLER_URL"
MAX_INSTALLER_BYTES = 512 * 1024 * 1024
_MIN_INSTALLER_BYTES = 1024
#: Noms exacts acceptés dans CN et O (comparaison insensible à la casse).
OBSIDIAN_PUBLISHERS = ("Dynalist Inc", "Obsidian")
FOLDERS = (
    "00_CORTEX",
    "01_Sources",
    "02_Knowledge",
    "03_Entities",
    "04_Journal",
    "99_Inbox",
)
#: Lectures dont le résultat mérite une note automatique. Les écritures, le
#: shell, le coffre et les outils sensibles sont volontairement exclus.
AUTO_MEMORY_TOOLS = frozenset(
    {
        "web_search",
        "web_fetch",
        "web_search_and_fetch",
        "web_deep_research",
        "fs_read_text",
        "fs_search",
        "doc_index_search",
        "ocr_image",
        "browser_text",
        "browser_fetch_rendered",
    }
)
_SAFE_TITLE = re.compile(r"[^A-Za-z0-9À-ÖØ-öø-ÿ ._-]+")
_SAFE_TAG = re.compile(r"[^A-Za-z0-9_.-]+")
_INVALID_FOLDER_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
#: Profondeur maximale d'un sous-dossier personnalisé (ex. `02_Knowledge/A/B`).
MAX_FOLDER_DEPTH = 4
#: Nombre de dossiers listés dans l'index et `obsidian_status`.
_MAX_INDEX_FOLDERS = 200
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


def vault_path(settings: Settings) -> Path:
    """Chemin du vault, explicite ou sous `data/Obsidian/<vault_name>`."""
    cfg = settings.obsidian
    if str(cfg.vault_dir).strip():
        raw = os.path.expandvars(str(cfg.vault_dir).strip())
        return Path(raw).expanduser().absolute()
    return (settings.paths.data_dir / "Obsidian" / str(cfg.vault_name).strip()).absolute()


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _date() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _safe_title(value: str, *, fallback: str = "Note") -> str:
    title = " ".join(_SAFE_TITLE.sub(" ", str(value or "")).split()).strip(" .")
    if not title or title in {".", ".."}:
        title = fallback
    return title[:100]


def _folder_parts(folder: str) -> tuple[str, ...]:
    """Valide un dossier relatif du vault et refuse toute traversée.

    Accepte les sous-dossiers personnalisés (`02_Knowledge/Projets/Atlas`),
    avec `/` ou `\\` comme séparateur, et retourne les segments normalisés.
    Refuse les chemins absolus, les lecteurs Windows, `..`, les dossiers
    masqués, les noms réservés et les caractères interdits.
    """
    text = str(folder or "").strip()
    if not text:
        return ("02_Knowledge",)
    normalized = text.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"dossier Obsidian invalide (chemin absolu) : {text}")
    parts = normalized.split("/")
    while parts and not parts[0].strip():
        parts.pop(0)
    while parts and not parts[-1].strip():
        parts.pop()
    if not parts:
        return ("02_Knowledge",)
    clean: list[str] = []
    for part in parts:
        name = part.strip()
        if not name or name in {".", ".."}:
            raise ValueError(f"dossier Obsidian invalide (traversée) : {text}")
        if name != part or name.endswith("."):
            raise ValueError(f"dossier Obsidian invalide (espace ou point final) : {text}")
        if _INVALID_FOLDER_CHARS.search(name):
            raise ValueError(f"dossier Obsidian invalide (caractère interdit) : {text}")
        if name.startswith("."):
            raise ValueError(f"dossier Obsidian invalide (dossier masqué) : {text}")
        if name.split(".")[0].casefold() in _WINDOWS_RESERVED:
            raise ValueError(f"dossier Obsidian invalide (nom réservé) : {text}")
        if name.casefold() == INDEX_NAME[:-3].casefold():
            raise ValueError("le dossier 00_CORTEX_INDEX est réservé")
        if len(name) > 64:
            raise ValueError(f"dossier Obsidian invalide (nom trop long) : {text}")
        clean.append(name)
    if len(clean) > MAX_FOLDER_DEPTH:
        raise ValueError(f"dossier Obsidian trop profond (max {MAX_FOLDER_DEPTH}) : {text}")
    return tuple(clean)


def _slug(value: str) -> str:
    title = _safe_title(value)
    return re.sub(r"\s+", "-", title).strip("-").lower()[:100] or "note"


def _tags(values: Iterable[str] | None) -> list[str]:
    result: list[str] = []
    for raw in values or ():
        for item in str(raw).replace(",", " ").split():
            tag = _SAFE_TAG.sub("-", item.strip().lstrip("#")).strip("-")
            if tag and tag not in result:
                result.append(tag[:48])
    return result[:24]


def _links(values: Iterable[str] | None) -> list[str]:
    result: list[str] = []
    for raw in values or ():
        value = str(raw).strip().strip("[]")
        if value.lower().endswith(".md"):
            value = value[:-3]
        value = _safe_title(value, fallback="")
        if value and value not in result:
            result.append(value[:100])
    return result[:32]


def _first_source(arguments: Mapping[str, Any], result: ToolResult) -> str:
    for key in ("url", "path", "query", "root", "pattern"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:500]
    data = result.data
    if isinstance(data, dict):
        for key in ("url", "path", "query"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
    return ""


class ObsidianBrain:
    """Vault Obsidian local, sans dépendance au programme Obsidian."""

    def __init__(
        self,
        settings: Settings,
        *,
        path: str | Path | None = None,
        create: bool = True,
    ) -> None:
        self.settings = settings
        self.root = (Path(path) if path else vault_path(settings)).resolve()
        if self.root.parent == self.root:
            raise ValueError("le vault Obsidian ne peut pas être une racine de disque")
        self._lock = threading.RLock()
        self._tasks: dict[str, str] = {}
        if create:
            self.ensure()

    # -- structure ----------------------------------------------------------

    def ensure(self) -> Path:
        """Crée les dossiers du cerveau et la note maîtresse."""
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            for folder in FOLDERS:
                (self.root / folder).mkdir(exist_ok=True)
            if not (self.root / INDEX_NAME).is_file():
                self._refresh_index()
        return self.root

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)

    def _folder_dir(self, parts: Iterable[str]) -> Path:
        """Crée le dossier demandé et vérifie qu'il reste dans le vault."""
        root = self.root
        target = root.joinpath(*parts)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"dossier Obsidian impossible : {exc}") from exc
        resolved = target.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError("dossier Obsidian hors du vault")
        if not resolved.is_dir():
            raise ValueError("dossier Obsidian invalide")
        return resolved

    def _folder_listing(self) -> list[str]:
        """Dossiers visibles du vault, dossiers personnalisés compris."""
        found: list[str] = []
        pending = [self.root]
        while pending and len(found) < _MAX_INDEX_FOLDERS:
            current = pending.pop(0)
            try:
                children = sorted(current.iterdir())
            except OSError:
                continue
            for child in children:
                if len(found) >= _MAX_INDEX_FOLDERS:
                    break
                try:
                    if not child.is_dir() or child.name.startswith("."):
                        continue
                except OSError:
                    continue
                found.append(child.relative_to(self.root).as_posix())
                pending.append(child)
        return found

    def _note_files(self) -> list[Path]:
        """Notes visibles du vault ; `.obsidian`, `.trash` et index exclus."""
        notes: list[Path] = []
        for path in self.root.rglob("*.md"):
            if path.name == INDEX_NAME or not path.is_file():
                continue
            relative = path.relative_to(self.root)
            if any(part.startswith(".") for part in relative.parts[:-1]):
                continue
            notes.append(path)
        notes.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return notes

    def _title_of(self, path: Path) -> str:
        try:
            head = path.read_text(encoding="utf-8")[:600]
        except OSError:
            return path.stem
        match = re.search(r'^title:\s*["\']?(.*?)["\']?\s*$', head, re.MULTILINE)
        return _safe_title(match.group(1) if match else path.stem)

    def _refresh_index(self) -> None:
        notes = self._note_files()[: max(1, int(self.settings.obsidian.max_index_links))]
        lines = [
            "# 00_CORTEX_INDEX",
            "",
            "Mémoire longue durée d'OmniAgent. Ce fichier est régénéré après "
            "chaque note ; les liens ci-dessous ouvrent le cerveau.",
            "",
            f"Dernière mise à jour : {_now()}",
            f"Notes indexées : {len(notes)}",
            "",
            "## Notes",
        ]
        for path in notes:
            title = self._title_of(path)
            relative = path.relative_to(self.root).as_posix()
            lines.append(f"- [[{title}]] — `{relative}`")
        folders = self._folder_listing() or list(FOLDERS)
        lines.extend(["", "## Dossiers", *[f"- `{folder}/`" for folder in folders], ""])
        self._write(self.root / INDEX_NAME, "\n".join(lines))

    # -- écriture -----------------------------------------------------------

    def remember(
        self,
        title: str,
        content: str,
        *,
        folder: str = "02_Knowledge",
        tags: Iterable[str] | None = None,
        links: Iterable[str] | None = None,
        source: str = "",
    ) -> dict[str, Any]:
        """Crée ou met à jour une note interconnectée et rafraîchit le cortex."""
        clean_title = _safe_title(redact(str(title or "")))
        if clean_title == INDEX_NAME or clean_title.lower().startswith("00_cortex_index"):
            raise ValueError("le nom 00_CORTEX_INDEX est réservé")
        selected = _folder_parts(folder)
        body = str(content or "").strip()
        if not body:
            raise ValueError("contenu de note vide")
        max_chars = max(1000, int(self.settings.obsidian.max_note_chars))
        body = redact(body)[:max_chars]
        # `source` finit dans le frontmatter : masquée, sur une seule ligne, bornée.
        safe_source = " ".join(redact(str(source or "")).split())[:500]
        clean_tags = _tags(tags)
        clean_links = _links(links)
        with self._lock:
            directory = self._folder_dir(selected)
            path = directory / f"{_slug(clean_title)}.md"
            created = _now()
            if path.is_file():
                try:
                    old = path.read_text(encoding="utf-8")
                    match = re.search(r"^created:\s*(.+)$", old, re.MULTILINE)
                    if match:
                        created = match.group(1).strip()
                except OSError:
                    pass
            link_lines = "\n".join(f"- [[{link}]]" for link in clean_links)
            tag_lines = " ".join(f"#{tag}" for tag in clean_tags)
            note = (
                "---\n"
                f'title: "{clean_title.replace(chr(34), chr(39))}"\n'
                f"created: {created}\n"
                f"updated: {_now()}\n"
                f"source: \"{safe_source.replace(chr(34), chr(39))}\"\n"
                f"tags: [{', '.join(clean_tags)}]\n"
                "---\n\n"
                f"# {clean_title}\n\n"
                f"{body}\n"
            )
            if link_lines:
                note += f"\n## Liens\n\n{link_lines}\n"
            if tag_lines:
                note += f"\n{tag_lines}\n"
            self._write(path, note)
            self._refresh_index()
            return {
                "title": clean_title,
                "path": str(path),
                "relative": path.relative_to(self.root).as_posix(),
                "folder": "/".join(selected),
                "tags": clean_tags,
                "links": clean_links,
            }

    def remember_tool_result(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        result: ToolResult,
    ) -> dict[str, Any] | None:
        """Transforme une lecture utile en note, sans secret ni doublon bruyant."""
        if not self.settings.obsidian.auto_memory:
            return None
        if not result.ok or result.sensitive or tool_name not in AUTO_MEMORY_TOOLS:
            return None
        # `remember()` masque et borne ; une seule passe de rédaction suffit.
        text = result.to_text(max_chars=6000).strip()
        if not text:
            return None
        source = _first_source(arguments, result)
        label = {
            "web_search": "Recherche web",
            "web_fetch": "Page web",
            "web_search_and_fetch": "Recherche et pages",
            "web_deep_research": "Recherche approfondie",
            "fs_read_text": "Fichier lu",
            "fs_search": "Recherche de fichiers",
            "doc_index_search": "Recherche documentaire",
            "ocr_image": "OCR",
            "browser_text": "Page navigateur",
            "browser_fetch_rendered": "Page rendue",
        }.get(tool_name, tool_name)
        title = f"{label} — {source}" if source else label
        tags = ["source", tool_name.replace("_", "-")]
        link = INDEX_NAME[:-3]
        try:
            return self.remember(
                title,
                f"Outil `{tool_name}`.\n\n```text\n{text}\n```",
                folder="01_Sources",
                tags=tags,
                links=[link],
                source=source,
            )
        except (OSError, ValueError) as exc:
            logger.warning("note Obsidian automatique impossible : %s", exc)
            return None

    # -- journal ------------------------------------------------------------

    def on_event(self, event: Any) -> None:
        """Abonné au bus : journalise la question et la réponse finale."""
        if not self.settings.obsidian.auto_journal:
            return
        kind = str(getattr(event, "type", ""))
        run_id = str(getattr(event, "run_id", ""))
        payload = getattr(event, "payload", {}) or {}
        if kind == "run_started":
            self._tasks[run_id] = str(payload.get("task") or "")
            return
        if kind != "final" or not run_id:
            return
        text = str(payload.get("text") or "").strip()
        if not text:
            return
        task = self._tasks.pop(run_id, "") or "session"
        title = f"Journal {_date()} — {task}"
        try:
            self.remember(
                title,
                f"**Tâche :** {task}\n\n**Réponse finale :**\n\n{text}",
                folder="04_Journal",
                tags=["journal"],
                links=[INDEX_NAME[:-3]],
                source=f"run:{run_id}",
            )
        except (OSError, ValueError) as exc:
            logger.warning("journal Obsidian impossible : %s", exc)

    # -- lecture ------------------------------------------------------------

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        terms = [term.casefold() for term in str(query or "").split() if term]
        if not terms:
            return []
        hits: list[tuple[int, Path, str]] = []
        for path in self._note_files():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            lowered = text.casefold()
            score = sum(lowered.count(term) for term in terms)
            if score <= 0:
                continue
            position = lowered.find(terms[0])
            excerpt = " ".join(text[max(0, position - 180) : position + 420].split())
            hits.append((score, path, excerpt))
        hits.sort(key=lambda item: (-item[0], str(item[1])))
        return [
            {
                "title": self._title_of(path),
                "path": str(path),
                "relative": path.relative_to(self.root).as_posix(),
                "excerpt": excerpt,
            }
            for _score, path, excerpt in hits[: max(1, int(limit))]
        ]

    def status(self) -> dict[str, Any]:
        installed = find_obsidian()
        return {
            "vault": str(self.root),
            "index": str(self.root / INDEX_NAME),
            "notes": len(self._note_files()),
            "folders": self._folder_listing() or list(FOLDERS),
            "obsidian_installed": installed is not None,
            "obsidian_path": str(installed) if installed else "",
            "auto_memory": bool(self.settings.obsidian.auto_memory),
            "auto_journal": bool(self.settings.obsidian.auto_journal),
        }


def build_obsidian_tools(brain: ObsidianBrain) -> list[Tool]:
    """Outils `obsidian_*` : mémoire explicite, rappel et état du cortex."""

    def obsidian_remember(
        title: str,
        content: str,
        tags: list[str] | None = None,
        links: list[str] | None = None,
        folder: str = "02_Knowledge",
        source: str = "",
    ) -> dict[str, Any]:
        return brain.remember(
            title,
            content,
            folder=folder,
            tags=tags or (),
            links=links or (),
            source=source,
        )

    def obsidian_search(query: str, limit: int = 10) -> dict[str, Any]:
        return {"query": query, "results": brain.search(query, limit=limit)}

    def obsidian_status() -> dict[str, Any]:
        return brain.status()

    return [
        FunctionTool(
            "obsidian_remember",
            "Écrit une note interconnectée dans le cerveau Obsidian. Utilise "
            "`[[Titre]]` dans `links` et des tags pour relier les connaissances. "
            "À appeler dès qu'un fait durable, une préférence ou une découverte "
            "mérite d'être retenu.",
            object_schema(
                {
                    "title": param("string", "Titre court et unique de la note"),
                    "content": param("string", "Contenu Markdown de la note"),
                    "tags": param(
                        "array",
                        "Tags sans # (par exemple projet, préférence)",
                        items=param("string", "Tag"),
                        default=[],
                    ),
                    "links": param(
                        "array",
                        "Notes à relier par [[Wikilink]]",
                        items=param("string", "Titre de note"),
                        default=[],
                    ),
                    "folder": param(
                        "string",
                        "Dossier relatif du vault ; un sous-dossier personnalisé "
                        "est accepté (par exemple 02_Knowledge/Projets/Atlas). "
                        "Les chemins absolus et les traversées `..` sont refusés.",
                        default="02_Knowledge",
                    ),
                    "source": param("string", "Source ou URL d'origine", default=""),
                },
                required=["title", "content"],
            ),
            obsidian_remember,
            read_only=False,
        ),
        FunctionTool(
            "obsidian_search",
            "Cherche dans la mémoire Obsidian et retourne les notes et extraits "
            "les plus pertinents. À utiliser avant de redécouvrir un fait déjà appris.",
            object_schema(
                {
                    "query": param("string", "Mots à chercher"),
                    "limit": param(
                        "integer", "Nombre de résultats", default=10, minimum=1, maximum=50
                    ),
                },
                required=["query"],
            ),
            obsidian_search,
        ),
        FunctionTool(
            "obsidian_status",
            "Donne l'état du cerveau Obsidian : vault, index, nombre de notes et "
            "présence de l'application Obsidian.",
            object_schema({}),
            obsidian_status,
        ),
    ]


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def _candidate_paths() -> list[Path]:
    roots = [
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
    ]
    candidates: list[Path] = []
    for root in roots:
        if not root:
            continue
        base = Path(root)
        candidates.extend(
            [
                base / "Obsidian" / "Obsidian.exe",
                base / "Programs" / "Obsidian" / "Obsidian.exe",
            ]
        )
    which = shutil.which("Obsidian.exe") or shutil.which("obsidian")
    if which:
        candidates.insert(0, Path(which))
    return candidates


def find_obsidian() -> Path | None:
    """Retourne l'exécutable Obsidian s'il est déjà installé."""
    override = os.environ.get("OBSIDIAN_EXE", "").strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return candidate
    seen: set[str] = set()
    for candidate in _candidate_paths():
        key = str(candidate).casefold()
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate
    return None


def _open(url: str, *, opener: Any = None, timeout: int = 60) -> Any:
    """Ouvre une URL https avec un User-Agent identifiable."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"{APP_ID}-installer",
            "Accept": "application/vnd.github+json, application/octet-stream",
        },
    )
    return (opener or urllib.request.urlopen)(request, timeout=timeout)


def resolve_obsidian_installer(*, opener: Any = None) -> tuple[str, str]:
    """Résout l'installateur Windows officiel de la dernière version.

    Retourne `(url, version)`. L'URL de l'asset `.exe` est prise dans la
    dernière release GitHub `obsidianmd/obsidian-releases` ;
    `OBSIDIAN_INSTALLER_URL` court-circuite l'API avec une URL https directe.
    """
    override = os.environ.get(OBSIDIAN_INSTALLER_ENV, "").strip()
    if override:
        if not override.lower().startswith("https://"):
            raise ValueError(f"{OBSIDIAN_INSTALLER_ENV} doit être une URL https")
        return override, "personnalisée"
    try:
        with _open(OBSIDIAN_RELEASES_API, opener=opener, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"liste des versions indisponible : {type(exc).__name__}: {exc}"
        ) from exc
    assets = payload.get("assets") if isinstance(payload, dict) else None
    if not isinstance(assets, list):
        raise RuntimeError("réponse de version inattendue")
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        lowered = name.lower()
        if lowered.startswith("obsidian") and lowered.endswith(".exe"):
            if url.lower().startswith("https://"):
                version = str(payload.get("tag_name") or name)[:32].lstrip("vV")
                return url, version
    raise RuntimeError("aucun installateur Windows dans la dernière version")


def _subject_fields(subject: str) -> dict[str, list[str]]:
    """Extrait les valeurs CN et O d'un sujet X.500, guillemets et échappements compris."""
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    index = 0
    text = str(subject or "")
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            current.append(text[index + 1])
            index += 2
            continue
        if char == '"':
            quoted = not quoted
            index += 1
            continue
        if char == "," and not quoted:
            parts.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    parts.append("".join(current))

    fields: dict[str, list[str]] = {}
    for part in parts:
        key, separator, value = part.partition("=")
        if not separator:
            continue
        name = key.strip().upper()
        if name in {"CN", "O"}:
            fields.setdefault(name, []).append(value.strip())
    return fields


def _is_obsidian_publisher(subject: str) -> bool:
    """Vrai si CN **et** O sont exactement un nom d'éditeur autorisé.

    Une comparaison par sous-chaîne laisserait passer « Evil Obsidian Ltd » ou
    un `L=Obsidian`; on ne lit donc que les RDN CN/O et on compare la valeur
    entière, sans préfixe ni suffixe.
    """
    fields = _subject_fields(subject)
    common_names = fields.get("CN") or []
    organizations = fields.get("O") or []
    if not common_names or not organizations:
        return False
    allowed = {" ".join(name.split()).casefold() for name in OBSIDIAN_PUBLISHERS}
    return all(
        " ".join(value.split()).casefold() in allowed
        for value in [*common_names, *organizations]
    )


def verify_authenticode(
    path: str | Path, *, runner: Any = None
) -> tuple[bool, str]:
    """Vérifie la signature Authenticode et l'éditeur avant exécution.

    Échoue fermé : sans PowerShell ou sans réponse lisible, le fichier est
    refusé. Le statut doit être `Valid` et le certificat doit porter un CN et un
    O **exactement** égaux à `Dynalist Inc` ou `Obsidian` (casse ignorée).
    """
    candidate = Path(path)
    if not candidate.is_file():
        return False, f"fichier absent : {candidate}"
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return False, "PowerShell introuvable : signature non vérifiable"
    script = (
        "$s = Get-AuthenticodeSignature -LiteralPath $env:OMNIAGENT_SIGN_PATH; "
        "$c = $s.SignerCertificate; $subject = ''; "
        "if ($c) { $subject = [string]$c.Subject }; "
        "[pscustomobject]@{ status = [string]$s.Status; subject = $subject } | "
        "ConvertTo-Json -Compress"
    )
    run = runner or subprocess.run
    try:
        completed = run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, "OMNIAGENT_SIGN_PATH": str(candidate)},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt"
            else 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"vérification Authenticode impossible : {type(exc).__name__}: {exc}"
    try:
        payload = json.loads(str(getattr(completed, "stdout", "") or "").strip())
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return False, "réponse Authenticode illisible"
    status = str(payload.get("status") or "").strip()
    subject = str(payload.get("subject") or "").strip()
    if status != "Valid":
        return False, f"signature absente ou invalide (statut {status or 'inconnu'})"
    if not _is_obsidian_publisher(subject):
        return False, f"signataire inattendu : {subject or 'aucun'}"
    return True, f"signature valide ({subject})"


def download_obsidian_installer(
    settings: Settings,
    *,
    opener: Any = None,
) -> Path:
    """Télécharge l'installateur officiel et vérifie que c'est bien un `.exe`."""
    url, version = resolve_obsidian_installer(opener=opener)
    name = Path(urlparse(url).path).name
    if not name.lower().endswith(".exe"):
        name = "Obsidian-Setup.exe"
    target_dir = Path(tempfile.mkdtemp(prefix="omniagent-obsidian-"))
    target = target_dir / name
    temporary = target.with_name(target.name + ".part")
    total = 0
    try:
        with _open(
            url,
            opener=opener,
            timeout=max(60, int(settings.obsidian.install_timeout_s)),
        ) as response:
            with open(temporary, "wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_INSTALLER_BYTES:
                        raise RuntimeError("installateur anormalement volumineux")
                    handle.write(chunk)
        with open(temporary, "rb") as handle:
            header = handle.read(2)
        if header != b"MZ" or total < _MIN_INSTALLER_BYTES:
            raise RuntimeError(
                "le fichier téléchargé n'est pas un exécutable Windows valide"
            )
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    logger.info("installateur Obsidian %s téléchargé : %s", version, url)
    return target


def _run_installer(
    command: list[str], settings: Settings, runner: Any, *, label: str
) -> str:
    """Exécute une commande d'installation silencieuse et résume l'échec."""
    run = runner or subprocess.run
    try:
        completed = run(
            command,
            capture_output=True,
            text=True,
            timeout=max(30, int(settings.obsidian.install_timeout_s)),
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt"
            else 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{label} impossible : {type(exc).__name__}: {exc}"
    output = (
        str(getattr(completed, "stderr", "") or "")
        + " "
        + str(getattr(completed, "stdout", "") or "")
    ).strip()
    detail = f"code {getattr(completed, 'returncode', '?')}"
    if output:
        detail += f" — {output[-500:]}"
    return f"{label} n'a pas confirmé l'installation ({detail})"


def _cleanup_installer(installer: Path) -> None:
    """Supprime le dossier temporaire créé pour l'installateur téléchargé."""
    parent = installer.parent
    if parent.name.startswith("omniagent-obsidian-") and parent.parent == Path(
        tempfile.gettempdir()
    ):
        shutil.rmtree(parent, ignore_errors=True)


def install_obsidian(
    settings: Settings,
    *,
    runner: Any = None,
    downloader: Any = None,
) -> tuple[bool, str]:
    """Installe Obsidian via winget, sinon l'installateur officiel téléchargé.

    Après chaque tentative, l'exécutable est recherché à nouveau ; le succès
    n'est annoncé que si `find_obsidian()` retourne un fichier existant.
    """
    found = find_obsidian()
    if found is not None:
        return True, f"Obsidian déjà installé : {found}"
    problems: list[str] = []
    winget = shutil.which("winget")
    if winget:
        problems.append(
            _run_installer(
                [
                    winget,
                    "install",
                    "--id",
                    OBSIDIAN_WINGET_ID,
                    "--exact",
                    "--silent",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                ],
                settings,
                runner,
                label="winget",
            )
        )
        installed = find_obsidian()
        if installed is not None:
            return True, f"Obsidian installé via winget : {installed}"
    else:
        problems.append("winget absent")

    installer: Path | None = None
    try:
        installer = Path((downloader or download_obsidian_installer)(settings))
    except (OSError, RuntimeError, ValueError) as exc:
        return (
            False,
            "; ".join(problems)
            + " — téléchargement de l'installateur officiel impossible : "
            f"{type(exc).__name__}: {exc}. "
            f"Téléchargez-le depuis {OBSIDIAN_DOWNLOAD_URL}.",
        )
    try:
        if not installer.is_file():
            return (
                False,
                f"installateur officiel introuvable : {installer}. "
                f"Téléchargez-le depuis {OBSIDIAN_DOWNLOAD_URL}.",
            )
        signed, signature = verify_authenticode(installer, runner=runner)
        if not signed:
            return (
                False,
                "; ".join([*problems, f"installateur refusé : {signature}"])
                + f". Téléchargez l'installateur officiel : {OBSIDIAN_DOWNLOAD_URL}",
            )
        detail = _run_installer(
            [str(installer), "/S", "/currentuser"],
            settings,
            runner,
            label="installateur officiel",
        )
        installed = find_obsidian()
        if installed is not None:
            return True, f"Obsidian installé depuis l'installateur officiel : {installed}"
        return (
            False,
            "; ".join([*problems, detail])
            + f". Téléchargez l'installateur officiel : {OBSIDIAN_DOWNLOAD_URL}",
        )
    finally:
        _cleanup_installer(installer)


def ensure_obsidian(settings: Settings, *, force: bool = False) -> tuple[bool, str]:
    """Vérifie Obsidian et l'installe au besoin ; jamais bloquant pour le vault."""
    found = find_obsidian()
    if found is not None:
        return True, f"Obsidian présent : {found}"
    if not force and not settings.obsidian.auto_install:
        return False, "Obsidian absent (installation automatique désactivée)."
    return install_obsidian(settings)
