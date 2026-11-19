"""Cerveau Obsidian : vault Markdown, liens et mémorisation continue.

Le vault est la mémoire longue durée de l'agent : chaque note est un fichier
Markdown avec un frontmatter lisible, des liens `[[...]]` et des tags. Le fichier
`00_CORTEX_INDEX.md` est régénéré après chaque écriture ; il liste les notes du
vault et sert de point d'entrée lorsqu'on ouvre le dossier dans Obsidian.

L'installation d'Obsidian est séparée de l'écriture du vault. Le vault reste
utilisable même si l'application Obsidian n'est pas installée ; le bootstrap
`ensure_obsidian()` ne fait l'installation que sur demande explicite ou au
premier lancement graphique avec `obsidian.auto_install`.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from config import APP_ID, Settings
from security.redaction import redact
from tools.base import FunctionTool, Tool, ToolResult, object_schema, param

__all__ = [
    "AUTO_MEMORY_TOOLS",
    "INDEX_NAME",
    "ObsidianBrain",
    "OBSIDIAN_DOWNLOAD_URL",
    "build_obsidian_tools",
    "ensure_obsidian",
    "find_obsidian",
    "install_obsidian",
    "vault_path",
]

logger = logging.getLogger(f"{APP_ID}.obsidian")

INDEX_NAME = "00_CORTEX_INDEX.md"
OBSIDIAN_WINGET_ID = "Obsidian.Obsidian"
OBSIDIAN_DOWNLOAD_URL = "https://obsidian.md/download"
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
        self.root = Path(path).absolute() if path else vault_path(settings)
        self._lock = threading.RLock()
        self._tasks: dict[str, str] = {}
        if create:
            self.ensure()

    # -- structure ----------------------------------------------------------

    def ensure(self) -> Path:
        """Crée les dossiers du cerveau et la note maîtresse si nécessaire."""
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            for folder in FOLDERS:
                (self.root / folder).mkdir(exist_ok=True)
            index = self.root / INDEX_NAME
            if not index.is_file():
                self._write(
                    index,
                    "# 00_CORTEX_INDEX\n\n"
                    "Mémoire longue durée d'OmniAgent. Ce fichier est régénéré "
                    "après chaque note ; les liens ci-dessous ouvrent le cerveau.\n\n"
                    "## Notes\n",
                )
            else:
                self._refresh_index()
        return self.root

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)

    def _note_files(self) -> list[Path]:
        return sorted(
            (
                path
                for path in self.root.rglob("*.md")
                if path.is_file() and path.name != INDEX_NAME
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )

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
        lines.extend(["", "## Dossiers", *[f"- `{folder}/`" for folder in FOLDERS], ""])
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
        clean_title = _safe_title(title)
        if clean_title == INDEX_NAME or clean_title.lower().startswith("00_cortex_index"):
            raise ValueError("le nom 00_CORTEX_INDEX est réservé")
        selected = str(folder or "02_Knowledge").strip().strip("/")
        if selected not in FOLDERS:
            raise ValueError(f"dossier Obsidian inconnu : {selected}")
        body = str(content or "").strip()
        if not body:
            raise ValueError("contenu de note vide")
        max_chars = max(1000, int(self.settings.obsidian.max_note_chars))
        body = redact(body)[:max_chars]
        clean_tags = _tags(tags)
        clean_links = _links(links)
        with self._lock:
            path = self.root / selected / f"{_slug(clean_title)}.md"
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
                f"source: \"{str(source).replace(chr(34), chr(39))}\"\n"
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
        text = redact(result.to_text(max_chars=6000)).strip()
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
            "folders": list(FOLDERS),
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
                        "Dossier du vault",
                        enum=list(FOLDERS),
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
                base / "Obsidian" / "Obsidian.exe",
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


def install_obsidian(settings: Settings, *, runner: Any = None) -> tuple[bool, str]:
    """Installe Obsidian via winget, sans interface, puis vérifie l'exécutable."""
    found = find_obsidian()
    if found is not None:
        return True, f"Obsidian déjà installé : {found}"
    winget = shutil.which("winget")
    if not winget:
        return (
            False,
            "winget absent : installez Obsidian depuis "
            f"{OBSIDIAN_DOWNLOAD_URL}, ou ajoutez-le au PATH.",
        )
    command = [
        winget,
        "install",
        "--id",
        OBSIDIAN_WINGET_ID,
        "--exact",
        "--silent",
        "--accept-package-agreements",
        "--accept-source-agreements",
    ]
    run = runner or subprocess.run
    try:
        completed = run(
            command,
            capture_output=True,
            text=True,
            timeout=max(30, int(settings.obsidian.install_timeout_s)),
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"installation Obsidian impossible : {type(exc).__name__}: {exc}"
    found = find_obsidian()
    if found is not None:
        return True, f"Obsidian installé : {found}"
    output = (str(getattr(completed, "stderr", "") or "") + " " + str(getattr(completed, "stdout", "") or "")).strip()
    detail = f"code {getattr(completed, 'returncode', '?')}"
    if output:
        detail += f" — {output[-500:]}"
    return (
        False,
        f"winget n'a pas confirmé l'installation ({detail}). "
        f"Téléchargez l'installateur officiel : {OBSIDIAN_DOWNLOAD_URL}",
    )


def ensure_obsidian(settings: Settings, *, force: bool = False) -> tuple[bool, str]:
    """Vérifie Obsidian et l'installe au besoin ; jamais bloquant pour le vault."""
    found = find_obsidian()
    if found is not None:
        return True, f"Obsidian présent : {found}"
    if not force and not settings.obsidian.auto_install:
        return False, "Obsidian absent (installation automatique désactivée)."
    return install_obsidian(settings)
