"""Index documentaire local : SQLite FTS5 sur les fichiers du poste.

Aucune dépendance externe (NumPy n'est pas requis : le classement est celui de
FTS5, BM25). L'index vit sous `settings.paths.index_dir` et ne contient que du
texte déjà présent sur la machine ; les valeurs sensibles n'y sont pas
filtrées, c'est pourquoi l'outil de recherche renvoie des extraits, pas des
fichiers entiers.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from config import APP_ID, Settings
from tools.base import FunctionTool, Tool, ToolError, object_schema, param
from tools.file_manager import FileManager

__all__ = ["DocIndex", "build_doc_tools"]

logger = logging.getLogger(f"{APP_ID}.docindex")

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150
MAX_QUERY_TERMS = 8

#: Formats indexables : texte brut, plus les documents bureautiques convertis
#: par `FileManager.document_text` (mêmes extractions que `file_convert`).
INDEXABLE_SUFFIXES = (".txt", ".md", ".pdf", ".docx", ".xlsx")


def _chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Découpe en morceaux qui se recouvrent, sans perdre de contenu."""
    flattened = " ".join(str(text).split())
    if not flattened:
        return []
    step = max(1, size - overlap)
    chunks = [
        flattened[start : start + size]
        for start in range(0, len(flattened), step)
        if flattened[start : start + size].strip()
    ]
    return chunks


def _query_terms(query: str) -> list[str]:
    terms = re.findall(r"\w{2,}", str(query or ""), re.UNICODE)
    return terms[:MAX_QUERY_TERMS]


class DocIndex:
    """Index FTS5 persistant des documents texte accessibles."""

    def __init__(
        self,
        settings: Settings,
        manager: FileManager | None = None,
        *,
        db_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.files = manager
        self.db_path = Path(db_path) if db_path else settings.paths.index_dir / "documents.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.db_path))
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS documents "
                "USING fts5(path, ordinal UNINDEXED, text)"
            )
            # Empreinte du dernier passage : date (ns) et taille. Elle rend
            # `index_path` incrémental — un fichier inchangé n'est pas relu, et
            # donc pas reconverti (un PDF coûte une extraction complète).
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS document_files ("
                "path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, "
                "size INTEGER NOT NULL, chunk_count INTEGER NOT NULL DEFAULT 0)"
            )
            self._db.commit()
        except sqlite3.OperationalError as exc:  # pragma: no cover - Python sans FTS5
            raise ToolError(
                f"indexation indisponible : FTS5 absent de ce Python ({exc})",
                code="fts_unavailable",
            ) from exc

    # -- cycle de vie -------------------------------------------------------

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "DocIndex":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- indexation ---------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        if self.files is not None:
            return self.files.resolve(path or ".", must_exist=True)
        target = Path(path or ".").expanduser()
        if not target.exists():
            raise ToolError(f"chemin introuvable : {target}", code="file_not_found")
        return target.resolve()

    def _read(self, file: Path, max_chars: int) -> str:
        if self.files is not None:
            return self.files.document_text(str(file), max_chars=max_chars)
        if file.suffix.lower() in (".pdf", ".docx", ".xlsx"):
            raise ToolError(
                f"format binaire sans gestionnaire de fichiers : {file.suffix}",
                code="unsupported_format",
            )
        return file.read_text(encoding="utf-8", errors="replace")[:max_chars]

    def index_path(
        self,
        path: str = ".",
        *,
        pattern: str = "",
        max_files: int = 200,
        max_chars: int = 200_000,
    ) -> dict[str, Any]:
        """Indexe un fichier ou un répertoire, de façon incrémentale.

        Sans `pattern`, tous les formats de `INDEXABLE_SUFFIXES` sont pris :
        texte, Markdown, PDF, DOCX et XLSX. Un fichier dont la date de
        modification (ns) et la taille n'ont pas changé depuis le dernier
        passage n'est pas relu : il sort dans `unchanged` au lieu d'être
        reconverti. Un index créé avant cette table réindexe une fois chaque
        document, puis devient incrémental.
        """
        root = self._resolve(path)
        if root.is_file():
            if root.suffix.lower() not in INDEXABLE_SUFFIXES:
                raise ToolError(
                    f"format non indexable : {root.suffix or '(aucun)'} ; "
                    f"attendu {', '.join(INDEXABLE_SUFFIXES)}",
                    code="unsupported_format",
                )
            candidates = [root]
        else:
            candidates = sorted(item for item in root.rglob(pattern or "*") if item.is_file())
        candidates = [
            item for item in candidates if item.suffix.lower() in INDEXABLE_SUFFIXES
        ][: max(1, int(max_files))]

        documents = 0
        chunks_written = 0
        unchanged = 0
        skipped: list[str] = []
        with self._db:
            for file in candidates:
                key = str(file)
                try:
                    stat = file.stat()
                except OSError as exc:
                    skipped.append(f"{key} : {exc}")
                    continue
                previous = self._db.execute(
                    "SELECT mtime_ns, size FROM document_files WHERE path = ?", (key,)
                ).fetchone()
                if previous and int(previous[0]) == stat.st_mtime_ns and int(previous[1]) == stat.st_size:
                    # Même date et même taille : rien n'a bougé, on ne lit pas.
                    unchanged += 1
                    continue
                try:
                    text = self._read(file, max(1, int(max_chars)))
                except Exception as exc:  # noqa: BLE001 - un fichier illisible n'arrête pas l'index
                    skipped.append(f"{key} : {exc}")
                    continue
                chunks = _chunk_text(text)
                self._db.execute("DELETE FROM documents WHERE path = ?", (key,))
                if chunks:
                    self._db.executemany(
                        "INSERT INTO documents (path, ordinal, text) VALUES (?, ?, ?)",
                        [(key, index, chunk) for index, chunk in enumerate(chunks)],
                    )
                # L'empreinte est écrite même sans morceau : un fichier vide ne
                # sera pas relu à chaque passage.
                self._db.execute(
                    "INSERT INTO document_files (path, mtime_ns, size, chunk_count) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET mtime_ns = excluded.mtime_ns, "
                    "size = excluded.size, chunk_count = excluded.chunk_count",
                    (key, stat.st_mtime_ns, stat.st_size, len(chunks)),
                )
                documents += 1
                chunks_written += len(chunks)
        return {
            "root": str(root),
            "pattern": pattern,
            "documents": documents,
            "chunks": chunks_written,
            "unchanged": unchanged,
            "skipped": skipped,
            "index": str(self.db_path),
        }

    # -- recherche ----------------------------------------------------------

    def search(self, query: str, *, limit: int = 5) -> dict[str, Any]:
        terms = _query_terms(query)
        if not terms:
            raise ToolError("requête vide : donnez au moins un mot", code="invalid_query")
        fts_query = " OR ".join(f'"{term}"' for term in terms)
        try:
            rows = self._db.execute(
                "SELECT path, ordinal, snippet(documents, 2, '«', '»', ' … ', 16) AS excerpt, "
                "bm25(documents) AS score FROM documents WHERE documents MATCH ? "
                "ORDER BY score LIMIT ?",
                (fts_query, max(1, int(limit))),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise ToolError(f"requête refusée : {exc}", code="invalid_query") from exc
        results = [
            {
                "path": str(path),
                "chunk": int(ordinal),
                "score": round(-float(score), 4),
                "excerpt": str(excerpt),
            }
            for path, ordinal, excerpt, score in rows
        ]
        return {"query": query, "terms": terms, "count": len(results), "results": results}

    def stats(self) -> dict[str, Any]:
        row = self._db.execute(
            "SELECT count(*), count(DISTINCT path) FROM documents"
        ).fetchone()
        chunks, documents = (int(row[0]), int(row[1])) if row else (0, 0)
        size = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "documents": documents,
            "chunks": chunks,
            "index": str(self.db_path),
            "size_bytes": size,
        }


def build_doc_tools(index: DocIndex) -> list[Tool]:
    """Outils `doc_index_*` liés à cet index."""

    def doc_index_build(
        path: str = ".",
        pattern: str = "*.txt",
        max_files: int = 200,
        max_chars: int = 200000,
    ) -> dict[str, Any]:
        return index.index_path(
            path, pattern=pattern, max_files=max_files, max_chars=max_chars
        )

    def doc_index_search(query: str, limit: int = 5) -> dict[str, Any]:
        return index.search(query, limit=limit)

    def doc_index_stats() -> dict[str, Any]:
        return index.stats()

    return [
        FunctionTool(
            "doc_index_build",
            "Indexe un fichier ou un répertoire pour la recherche locale (SQLite "
            "FTS5) : .txt, .md, .pdf, .docx et .xlsx. Incrémental : seuls les "
            "fichiers nouveaux ou modifiés (date + taille) sont relus. Un motif "
            "restreint le choix (par exemple « *.pdf »).",
            object_schema(
                {
                    "path": param("string", "Fichier ou répertoire à indexer", default="."),
                    "pattern": param(
                        "string",
                        "Motif des fichiers à indexer (vide = tous les formats gérés)",
                        default="",
                    ),
                    "max_files": param(
                        "integer", "Nombre maximal de fichiers", default=200, minimum=1, maximum=5000
                    ),
                    "max_chars": param(
                        "integer",
                        "Caractères lus par fichier",
                        default=200000,
                        minimum=1000,
                        maximum=2000000,
                    ),
                }
            ),
            doc_index_build,
            read_only=False,
        ),
        FunctionTool(
            "doc_index_search",
            "Cherche dans l'index documentaire local et renvoie des extraits classés par "
            "pertinence (BM25), avec le chemin et le numéro de morceau.",
            object_schema(
                {
                    "query": param("string", "Mots à chercher"),
                    "limit": param(
                        "integer", "Nombre de résultats", default=5, minimum=1, maximum=50
                    ),
                },
                required=["query"],
            ),
            doc_index_search,
        ),
        FunctionTool(
            "doc_index_stats",
            "Donne le nombre de documents et de morceaux présents dans l'index local.",
            object_schema({}),
            doc_index_stats,
        ),
    ]
