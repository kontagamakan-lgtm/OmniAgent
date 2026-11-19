"""Contrôles hors-ligne des exécutables reconstruits.

Deux chemins, tous deux sans réseau :

* **budget de contexte** — un faux fournisseur OpenAI local enchaîne des appels
  d'outil ; l'historique réellement reçu est inspecté (borne, synthèse, tâche
  conservée) pour prouver que `core/context_manager.py` agit dans l'exe ;
* **compatibilité** — l'exe lance son propre `--compat-smoke`, qui exerce
  `AgentRunner`, `run_task` et `history(run_id)` depuis le paquet figé.

Usage :
    python build_smoke/offline_smoke.py dist/OmniAgent/OmniAgent.exe
    python build_smoke/offline_smoke.py dist/OmniAgent-lite/OmniAgent.exe
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
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
#: Budget volontairement large : les schémas d'outils et le prompt système y
#: sont désormais réservés, et l'objectif du scénario est le déclencheur « trop
#: de messages » (8), pas l'épuisement du budget par le coût fixe.
MAX_CONTEXT_TOKENS = 48000
SUMMARY_MARKER = "Résumé des tours précédents"
COMPAT_MARKER = "COMPAT_SMOKE_OK"

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
    # Hors du dépôt : le contrôle ne laisse aucun fichier à commiter.
    target = Path(tempfile.mkdtemp(prefix="omniagent-offline-smoke-")) / "config.json"
    return settings.save(target)


def _trace_dirs() -> list[Path]:
    """Répertoires de trace possibles : sources et paquet figé ne résolvent pas
    `data_dir` au même endroit (dépôt contre `%LOCALAPPDATA%`)."""
    dirs = [Settings.load().paths.log_dir / "runs"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "OmniAgent" / "logs" / "runs")
    return dirs


def _traces() -> set[Path]:
    return {
        path
        for directory in _trace_dirs()
        if directory.is_dir()
        for path in directory.glob("*.jsonl")
    }


def _decode(data: bytes) -> str:
    """Décode la sortie de l'exe sans dépendre de la locale du parent.

    L'exe écrit dans l'encodage de la console Windows (cp1252 : `•` en 0x95),
    tandis que la CI force `PYTHONUTF8=1` côté harnais : décoder en UTF-8 tue
    le thread de lecture et rend `stdout` à `None`.
    """
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _run_exe(exe: Path, config: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "OMNI_SMOKE_KEY": "smoke"}
    proc = subprocess.run(
        [str(exe), "--config", str(config), *arguments],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        timeout=240,
    )
    return subprocess.CompletedProcess(
        proc.args,
        proc.returncode,
        stdout=_decode(proc.stdout or b""),
        stderr=_decode(proc.stderr or b""),
    )


def _check_budget(exe: Path, config: Path) -> tuple[list[str], list[int], str]:
    """Fait tourner la boucle d'outils et inspecte les historiques reçus."""
    before = _traces()
    started = time.monotonic()
    proc = _run_exe(exe, config, "--task", "liste les fichiers du dossier")
    elapsed = time.monotonic() - started
    new_traces = sorted(_traces() - before)

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
    if not any(
        "Appels d'outils déjà tentés" in str(message.get("content"))
        for messages in requests[-2:]
        for message in messages
        if message.get("role") == "system"
    ):
        failures.append("le résumé de repli ne porte pas les appels d'outils tentés")
    if "contexte replié" not in proc.stdout:
        failures.append("le rapport CONTEXT_TRIMMED n'est pas affiché par la console")
    if not new_traces:
        failures.append("--task n'a écrit aucune trace JSONL")
    elif '"type": "run_finished"' not in new_traces[-1].read_text(encoding="utf-8"):
        failures.append(f"trace incomplète : {new_traces[-1]}")

    details = [f"durée      : {elapsed:.1f} s", f"requêtes   : {len(requests)}"]
    if proc.stderr.strip():
        details.append(f"stderr     : {proc.stderr.strip()[:300]!r}")
    return failures, counts, "\n".join(details)


def _check_compat(exe: Path, config: Path) -> tuple[list[str], str]:
    """Lance le contrôle de compatibilité embarqué dans l'exécutable."""
    started = time.monotonic()
    proc = _run_exe(exe, config, "--compat-smoke")
    elapsed = time.monotonic() - started

    failures: list[str] = []
    if proc.returncode != 0:
        failures.append(f"--compat-smoke a échoué (code {proc.returncode})")
    if COMPAT_MARKER not in proc.stdout:
        failures.append(f"marqueur {COMPAT_MARKER} absent de la sortie")
    if "ToolSpec" not in proc.stdout:
        failures.append("chemin ToolSpec absent de la sortie de --compat-smoke")

    details = [f"durée      : {elapsed:.1f} s", f"sortie     : {proc.stdout.strip()[:300]!r}"]
    if proc.stderr.strip():
        details.append(f"stderr     : {proc.stderr.strip()[:300]!r}")
    return failures, "\n".join(details)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    exe = Path(argv[1]).resolve()
    if not exe.is_file():
        print(f"exécutable introuvable : {exe}")
        return 2

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = _write_config(server.server_address[1])
    try:
        budget_failures, counts, budget_details = _check_budget(exe, config)
        compat_failures, compat_details = _check_compat(exe, config)
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(config.parent, ignore_errors=True)

    print(f"exe        : {exe}")
    print("=== budget de contexte ===")
    print(budget_details)
    print(f"messages   : {counts}")
    print("=== compatibilité ===")
    print(compat_details)

    failures = budget_failures + compat_failures
    if failures:
        for item in failures:
            print(f"ECHEC      : {item}")
        return 1
    print(
        "OK         : budget de contexte appliqué et façade AgentRunner/run_task/"
        "history(run_id) opérationnelle dans l'exécutable"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
