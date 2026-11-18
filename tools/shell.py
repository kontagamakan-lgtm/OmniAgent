"""Exécution de commandes shell, sous confirmation obligatoire.

Ce n'est pas un bac à sable : la seule vraie barrière est la confirmation
demandée par le registre (`dangerous=True`), complétée par des garde-fous
utiles — répertoire de travail confiné aux racines autorisées, délai borné,
sortie tronquée, et refus des commandes manifestement destructrices du disque.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from config import APP_ID, Settings
from tools.base import FunctionTool, Tool, ToolError, object_schema, param
from tools.file_manager import FileManager

__all__ = ["ShellRunner", "build_shell_tools"]

logger = logging.getLogger(f"{APP_ID}.shell")

MAX_TIMEOUT_S = 120
MAX_OUTPUT_CHARS = 20000
MAX_COMMAND_CHARS = 4000

# Filet de sécurité, pas une liste exhaustive : la confirmation reste le
# véritable garde-fou. Ces motifs détruisent un disque en une commande.
_FORBIDDEN = (
    "format c:",
    "format /",
    "diskpart",
    "mkfs",
    "shutdown",
    "del /s /q c:\\",
    "rd /s /q c:\\",
    "rm -rf /",
)


class ShellRunner:
    """Exécute une commande locale avec des bornes explicites."""

    def __init__(self, settings: Settings, manager: FileManager | None = None) -> None:
        self.settings = settings
        self.files = manager

    def _cwd(self, cwd: str) -> Path:
        if self.files is not None:
            return self.files.resolve(cwd or ".", must_exist=True)
        target = Path(cwd or ".").expanduser()
        if not target.is_dir():
            raise ToolError(f"répertoire introuvable : {target}", code="path_not_found")
        return target.resolve()

    @staticmethod
    def _check(command: str) -> str:
        text = str(command or "").strip()
        if not text:
            raise ToolError("commande vide", code="invalid_command")
        if len(text) > MAX_COMMAND_CHARS:
            raise ToolError(
                f"commande trop longue ({len(text)} > {MAX_COMMAND_CHARS} caractères)",
                code="invalid_command",
            )
        lowered = " ".join(text.lower().split())
        for pattern in _FORBIDDEN:
            if pattern in lowered:
                raise ToolError(
                    f"commande refusée par le garde-fou ({pattern!r})",
                    code="command_blocked",
                )
        return text

    def run(self, command: str, *, cwd: str = ".", timeout_s: int = 30) -> dict[str, Any]:
        text = self._check(command)
        target = self._cwd(cwd)
        timeout = max(1, min(int(timeout_s), MAX_TIMEOUT_S))
        started = time.perf_counter()
        try:
            # `shell=True` est assumé : l'outil est marqué dangereux, donc
            # bloqué tant que l'utilisateur n'a pas confirmé la commande.
            completed = subprocess.run(
                text,
                shell=True,
                cwd=str(target),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(
                f"commande interrompue après {timeout} s", code="command_timeout"
            ) from exc
        except OSError as exc:
            raise ToolError(f"exécution impossible : {exc}", code="command_failed") from exc
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        truncated = len(stdout) > MAX_OUTPUT_CHARS or len(stderr) > MAX_OUTPUT_CHARS
        return {
            "command": text,
            "cwd": str(target),
            "returncode": completed.returncode,
            "stdout": stdout[:MAX_OUTPUT_CHARS],
            "stderr": stderr[:MAX_OUTPUT_CHARS],
            "truncated": truncated,
            "duration_ms": duration_ms,
        }

    def status(self) -> dict[str, Any]:
        roots = [str(root) for root in (self.files.roots if self.files else [])]
        return {
            "max_timeout_s": MAX_TIMEOUT_S,
            "max_output_chars": MAX_OUTPUT_CHARS,
            "roots": roots,
            "confirmation_required": True,
        }


def build_shell_tools(runner: ShellRunner) -> list[Tool]:
    """Outils `shell_*` liés à ce lanceur."""

    def shell_run(command: str, cwd: str = ".", timeout_s: int = 30) -> dict[str, Any]:
        return runner.run(command, cwd=cwd, timeout_s=timeout_s)

    def shell_status() -> dict[str, Any]:
        return runner.status()

    return [
        FunctionTool(
            "shell_run",
            "Exécute une commande shell dans un répertoire autorisé et renvoie code de "
            "sortie, stdout et stderr. Action dangereuse : confirmation obligatoire.",
            object_schema(
                {
                    "command": param("string", "Commande à exécuter"),
                    "cwd": param(
                        "string", "Répertoire de travail (dans les racines autorisées)", default="."
                    ),
                    "timeout_s": param(
                        "integer",
                        "Délai maximal en secondes",
                        default=30,
                        minimum=1,
                        maximum=MAX_TIMEOUT_S,
                    ),
                },
                required=["command"],
            ),
            shell_run,
            read_only=False,
            dangerous=True,
        ),
        FunctionTool(
            "shell_status",
            "Décrit les limites du lanceur de commandes (délai, sortie, racines autorisées).",
            object_schema({}),
            shell_status,
        ),
    ]
