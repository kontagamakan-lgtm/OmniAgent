"""Socle de persistance SQLite d'OmniAgent.

Le stockage local garde les métadonnées d'exécution et un index FTS5 de
documents. Les traces d'agent restent des fichiers JSONL : ce module ne
remplace ni `core.trace`, ni `core.audit`, ni `tools.doc_index`, qui ont chacun
un format et des garanties propres.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from config import APP_ID

__all__ = [
    "Database",
    "SCHEMA_VERSION",
    "StorageError",
    "database_status",
    "initialize_database",
    "schema_path",
]

SCHEMA_VERSION = 1


class StorageError(RuntimeError):
    """Erreur de schéma ou d'ouverture de la base locale."""


def schema_path() -> Path:
    """Chemin de `schema.sql`, en source comme dans un paquet PyInstaller."""
    return Path(__file__).resolve().with_name("schema.sql")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA synchronous=NORMAL")


def initialize_database(path: str | Path) -> Path:
    """Crée ou migre la base, puis retourne son chemin.

    L'opération est idempotente. Une base créée par une version plus récente est
    refusée au lieu d'être ouverte avec un schéma inconnu.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    schema_file = schema_path()
    if not schema_file.is_file():
        raise StorageError(f"schéma SQLite introuvable : {schema_file}")

    try:
        connection = sqlite3.connect(str(target), timeout=10)
    except sqlite3.Error as exc:
        raise StorageError(f"ouverture de la base impossible : {exc}") from exc
    try:
        _configure(connection)
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise StorageError(
                f"base au schéma v{version}, cette version d'OmniAgent lit v{SCHEMA_VERSION}"
            )
        connection.executescript(schema_file.read_text(encoding="utf-8"))
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, _now()),
        )
        connection.commit()
    except sqlite3.Error as exc:
        raise StorageError(f"migration de la base impossible : {exc}") from exc
    finally:
        connection.close()
    return target


class Database:
    """Connexion applicative, ouverte après migration du schéma."""

    def __init__(self, path: str | Path) -> None:
        self.path = initialize_database(path)
        try:
            self.connection = sqlite3.connect(str(self.path), timeout=10)
            _configure(self.connection)
            self.connection.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            raise StorageError(f"ouverture de la base impossible : {exc}") from exc

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        return int(self.connection.execute("PRAGMA user_version").fetchone()[0])

    def record_run(
        self,
        run_id: str,
        task: str = "",
        *,
        status: str = "running",
        tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Crée ou met à jour un run sans effacer son début."""
        self.connection.execute(
            "INSERT INTO runs(run_id, task, started_at, status, tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET "
            "task=excluded.task, status=excluded.status, "
            "tokens=excluded.tokens, cost_usd=excluded.cost_usd",
            (str(run_id), str(task), _now(), str(status), int(tokens), float(cost_usd)),
        )
        self.connection.commit()

    def finish_run(
        self,
        run_id: str,
        *,
        status: str = "finished",
        tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self.connection.execute(
            "UPDATE runs SET finished_at=?, status=?, tokens=?, cost_usd=? WHERE run_id=?",
            (_now(), str(status), int(tokens), float(cost_usd), str(run_id)),
        )
        self.connection.commit()

    def append_event(
        self, run_id: str, kind: str, payload: dict[str, Any] | None = None
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO events(run_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (
                str(run_id),
                str(kind),
                json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                _now(),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid or 0)

    def events(self, run_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT id, run_id, kind, payload, created_at FROM events "
            "WHERE run_id=? ORDER BY id LIMIT ?",
            (str(run_id), max(1, int(limit))),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "run_id": row["run_id"],
                "kind": row["kind"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def runs(self, *, limit: int = 50) -> Iterator[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT run_id, task, started_at, finished_at, status, tokens, cost_usd "
            "FROM runs ORDER BY started_at DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        for row in rows:
            yield dict(row)

    def index_document(self, path: str, content: str) -> None:
        """Insère ou remplace un document ; les déclencheurs tiennent FTS5."""
        self.connection.execute(
            "INSERT INTO documents(path, content, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET content=excluded.content, "
            "updated_at=excluded.updated_at",
            (str(path), str(content), _now()),
        )
        self.connection.commit()

    def search_documents(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Recherche FTS5, avec des termes échappés et liés en paramètres."""
        terms = [term for term in str(query or "").split() if term]
        if not terms:
            return []
        match = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
        try:
            rows = self.connection.execute(
                "SELECT d.path, d.updated_at, snippet(documents_fts, 1, '[', ']', '…', 12) AS excerpt "
                "FROM documents_fts JOIN documents d ON d.id = documents_fts.rowid "
                "WHERE documents_fts MATCH ? ORDER BY bm25(documents_fts) LIMIT ?",
                (match, max(1, int(limit))),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise StorageError(f"recherche FTS5 impossible : {exc}") from exc
        return [dict(row) for row in rows]


def database_status(path: str | Path) -> str:
    """Initialise la base et rend un résumé lisible pour `--check`."""
    target = initialize_database(path)
    try:
        connection = sqlite3.connect(str(target), timeout=10)
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
            tables = int(
                connection.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()[0]
            )
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise StorageError(f"lecture de la base impossible : {exc}") from exc
    return f"{target} (schéma v{version}, {mode.upper()}, {tables} tables)"


if __name__ == "__main__":  # pragma: no cover - diagnostic manuel
    import sys

    print(database_status(sys.argv[1] if len(sys.argv) > 1 else f"data/{APP_ID}.db"))
