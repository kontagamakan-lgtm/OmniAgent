"""Utilitaires partagés par les tests OmniAgent (exécutables sans pytest).

Chaque fichier `test_*.py` se termine par `sys.exit(run(globals()))`, ce qui
permet de lancer `python tests/test_x.py` sans installer pytest, tout en
restant compatible avec la collecte pytest standard.
"""

from __future__ import annotations

import shutil
import sys
import threading
import traceback
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402

_TMP_ROOT = ROOT / "data" / ".test-tmp"


@contextmanager
def tmpdir(label: str = "t") -> Iterator[Path]:
    """Répertoire temporaire isolé, sous le projet (jamais dans %TEMP%)."""
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    target = _TMP_ROOT / f"{label}-{uuid.uuid4().hex[:8]}"
    target.mkdir()
    try:
        yield target
    finally:
        shutil.rmtree(target, ignore_errors=True)


def make_settings(root: Path) -> config.Settings:
    """Configuration de test : chemins isolés, racines limitées à `root`."""
    settings = config.Settings()
    settings.paths = config.Paths(
        install_dir=root,
        data_dir=root,
        config_file=root / "config.json",
        vault_file=root / "vault.enc",
        index_dir=root / "index",
        log_dir=root / "logs",
        download_dir=root / "downloads",
        cache_dir=root / "cache",
        report_dir=root / "reports",
        browser_profile_dir=root / "browser_profile",
    )
    settings.files.allowed_roots = [str(root)]
    # Les tests unitaires ne créent ni vault Obsidian ni installation externe.
    settings.obsidian.enabled = False
    settings.obsidian.auto_install = False
    # KDF volontairement allégé : les tests ne mesurent pas la résistance au bruteforce.
    settings.vault.argon2_time_cost = 1
    settings.vault.argon2_memory_kib = 8192
    settings.vault.argon2_parallelism = 1
    settings.vault.max_failed_unlocks = 3
    settings.vault.auto_lock_minutes = 15
    return settings


def run(namespace: dict[str, Any]) -> int:
    """Exécute toutes les fonctions `test_*` du module et retourne un code de sortie."""
    tests: list[tuple[str, Callable[[], None]]] = [
        (name, value)
        for name, value in sorted(namespace.items())
        if name.startswith("test_") and callable(value)
    ]
    failures = 0
    for name, function in tests:
        try:
            function()
        except Exception as exc:  # noqa: BLE001 - rapport de test
            failures += 1
            traceback.print_exc()
            print(f"ECHEC {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"OK    {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passés")
    return 0 if failures == 0 else 1


@contextmanager
def serve(
    routes: Mapping[str, tuple[int, str, str | bytes]],
    *,
    post_routes: Mapping[str, Callable[[dict[str, str]], tuple[int, str, str]]] | None = None,
) -> Iterator[tuple[str, dict[str, int]]]:
    """Petit serveur HTTP local pour tester les couches réseau sans Internet.

    Retourne (URL de base, compteurs de requêtes par chemin).
    """
    counts: dict[str, int] = {}
    posts = dict(post_routes or {})

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _emit(self, status: int, content_type: str, body: str | bytes) -> None:
            payload = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self) -> None:  # noqa: N802 - API http.server
            path = urlparse(self.path).path
            counts[path] = counts.get(path, 0) + 1
            route = routes.get(path)
            if route is None:
                self._emit(404, "text/plain", "introuvable")
                return
            status, content_type, body = route
            self._emit(status, content_type, body)

        def do_POST(self) -> None:  # noqa: N802 - API http.server
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            form = {key: values[0] for key, values in parse_qs(raw).items()}
            handler = posts.get(urlparse(self.path).path)
            if handler is None:
                self._emit(404, "text/plain", "introuvable")
                return
            status, content_type, body = handler(form)
            self._emit(status, content_type, body)

        def log_message(self, *args: Any) -> None:  # silence
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", counts
    finally:
        server.shutdown()
        server.server_close()
