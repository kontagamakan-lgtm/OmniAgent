"""Contrôle hors-ligne du budget de contexte dans les exécutables reconstruits.

Lance l'EXE avec un faux fournisseur OpenAI local qui demande quatre fois de
suite l'outil `fs_list`, puis conclut. L'historique envoyé par l'agent est
enregistré à chaque requête : le script vérifie qu'il reste borné et qu'un
message de synthèse apparaît, preuve que `core/context_manager.py` est bien
embarqué et actif dans l'exécutable.

Usage :
    python build_smoke/budget_smoke.py dist/OmniAgent/OmniAgent.exe
    python build_smoke/budget_smoke.py dist/OmniAgent-lite/OmniAgent.exe
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Settings  # noqa: E402

CALLS = 4
#: Chemins distincts : le garde-fou anti-boucle arrête une action répétée à
#: l'identique, or on veut atteindre la cinquième requête (celle qui replie).
PATHS = (".", "core", "tests", "ui")
MAX_MESSAGES = 8
TOOL_OUTPUT_CHARS = 500
MAX_CONTEXT_TOKENS = 4000
SUMMARY_MARKER = "Résumé des tours précédents"

requests: list[list[dict]] = []


class _Provider(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # silence
        pass

    def do_POST(self) -> None:  # noqa: N802 - nom imposé par http.server
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages") or []
        requests.append([dict(m) for m in messages])

        if len(requests) <= CALLS:
            reply = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{len(requests)}",
                        "type": "function",
                        "function": {
                            "name": "fs_list",
                            "arguments": json.dumps({"path": PATHS[len(requests) - 1]}),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            reply = {"role": "assistant", "content": "Tâche terminée.", "tool_calls": []}
            finish = "stop"

        payload = {
            "id": "chatcmpl-smoke",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": reply, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _write_config(port: int) -> Path:
    settings = Settings.load()
    settings.llm.backend = "openai_compatible"
    settings.llm.base_url = f"http://127.0.0.1:{port}/v1"
    settings.llm.api_key_env = "OMNI_SMOKE_KEY"
    settings.llm.model = "smoke"
    settings.llm.stream = False
    settings.llm.max_retries = 1
    settings.llm.request_timeout_s = 30
    settings.agent.history_max_messages = MAX_MESSAGES
    settings.agent.max_context_tokens = MAX_CONTEXT_TOKENS
    settings.agent.tool_output_max_chars = TOOL_OUTPUT_CHARS
    target = Path(__file__).resolve().parent / "config.json"
    return settings.save(target)


def _run_exe(exe: Path, config: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "OMNI_SMOKE_KEY": "smoke"}
    return subprocess.run(
        [str(exe), "--config", str(config), "--task", "liste les fichiers du dossier"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    exe = Path(argv[1]).resolve()
    if not exe.is_file():
        print(f"exécutable introuvable : {exe}")
        return 2

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    config = _write_config(port)
    started = time.monotonic()
    try:
        proc = _run_exe(exe, config)
    finally:
        server.shutdown()
        server.server_close()
    elapsed = time.monotonic() - started

    failures: list[str] = []
    if proc.returncode != 0:
        failures.append(f"code de sortie {proc.returncode}")
    if "Tâche terminée." not in proc.stdout:
        failures.append("réponse finale absente de la sortie")
    if len(requests) != CALLS + 1:
        failures.append(f"{len(requests)} requêtes au lieu de {CALLS + 1}")

    counts = [len(messages) for messages in requests]
    if counts and max(counts) > MAX_MESSAGES:
        failures.append(f"historique non borné : {counts} (limite {MAX_MESSAGES})")
    if not any(
        message.get("role") == "system" and SUMMARY_MARKER in str(message.get("content"))
        for messages in requests[-2:]
        for message in messages
    ):
        failures.append("aucun message de synthèse dans les dernières requêtes")

    print(f"exe        : {exe}")
    print(f"durée      : {elapsed:.1f} s")
    print(f"requêtes   : {len(requests)}")
    print(f"messages   : {counts}")
    print(f"sortie     : {proc.stdout.strip()[:300]!r}")
    if proc.stderr.strip():
        print(f"stderr     : {proc.stderr.strip()[:300]!r}")
    if failures:
        for item in failures:
            print(f"ECHEC      : {item}")
        return 1
    print("OK         : budget de contexte appliqué dans l'exécutable")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
