"""Contrôle de fumée de l'exécutable construit.

Démarre `dist/OmniAgent/OmniAgent.exe`, attend qu'une fenêtre **visible** de ce
processus apparaisse, vérifie qu'il tourne toujours, puis lui envoie WM_CLOSE et
exige une sortie avec le code 0.

    python smoke_exe.py                                        # paquet complet
    python smoke_exe.py --exe dist/OmniAgent-lite/OmniAgent.exe  # variante allégée
    python smoke_exe.py --browser                              # navigateur système

`--browser` lance l'exe avec `--browser-smoke` : il démarre Chromium avec le
Node.js du système (aucun paquet n'embarque `node.exe`), ouvre une page servie
en local et exige le marqueur `BROWSER_SMOKE_OK`. C'est ce contrôle qui prouve
que les outils `browser_*` fonctionnent dans le binaire livré.

Codes de sortie : 0 contrôle réussi, 1 échec, 2 exécutable introuvable.
Hors Windows, le contrôle est ignoré (code 0) : la recette est Windows-only.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

__all__ = ["main", "find_window"]

DEFAULT_EXE = Path("dist") / "OmniAgent" / "OmniAgent.exe"
WM_CLOSE = 0x0010


def _user32():
    """Charge user32 à la demande : `ctypes.windll` n'existe que sous Windows."""
    import ctypes

    return ctypes, ctypes.windll.user32


def find_window(pid: int) -> tuple[int, str] | None:
    """Première fenêtre visible avec titre appartenant à `pid`.

    Le filtre par PID évite de confondre l'exe avec une autre instance déjà
    ouverte ou avec la fenêtre du terminal qui lance le contrôle.
    """
    ctypes, user32 = _user32()
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _lparam):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid or not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        found.append((int(hwnd), buffer.value))
        return True

    user32.EnumWindows(callback, 0)
    return found[0] if found else None


def _close_window(hwnd: int) -> bool:
    ctypes, user32 = _user32()
    return bool(user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - Windows récalcitrant
            pass


def _fail(message: str) -> int:
    print(f"  !!   {message}", file=sys.stderr)
    return 1


def _printable(text: str) -> str:
    """Neutralise les caractères que la console courante ne sait pas écrire."""
    encoding = getattr(sys.stdout, "encoding", "") or "utf-8"
    return text.encode(encoding, "replace").decode(encoding, "replace")


def _run_check(
    exe: Path, args: list[str], *, timeout: float, marker: str, label: str
) -> int:
    """Lance l'exe avec un auto-contrôle et exige le marqueur attendu."""
    print(f"Contrôle {label} de {exe}")
    try:
        done = subprocess.run(
            [str(exe), *args],
            cwd=str(exe.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _fail(f"contrôle {label} toujours en cours après {timeout:.0f} s")
    output = f"{done.stdout or ''}{done.stderr or ''}"
    for line in output.splitlines():
        if line.strip():
            print(f"  {_printable(line.strip())}")
    if done.returncode != 0:
        return _fail(f"contrôle {label} en échec (code {done.returncode})")
    if marker not in output:
        return _fail(f"sortie sans marqueur {marker}")
    print(f"  ok   {marker} confirmé par l'exécutable")
    print(f"Contrôle {label} réussi.")
    return 0


def _browser_check(exe: Path, timeout: float) -> int:
    """Exige que l'exe pilote le navigateur du système, sans Node embarqué."""
    return _run_check(
        exe,
        ["--browser-smoke"],
        timeout=timeout,
        marker="BROWSER_SMOKE_OK",
        label="navigateur",
    )


def _doc_index_check(exe: Path, timeout: float) -> int:
    """Exige que l'exe indexe un PDF et le retrouve par un passage."""
    return _run_check(
        exe,
        ["--doc-index-smoke"],
        timeout=timeout,
        marker="DOC_INDEX_SMOKE_OK",
        label="index documentaire",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Contrôle de fumée de l'exe OmniAgent.")
    parser.add_argument("--exe", default=str(DEFAULT_EXE), help="exécutable à contrôler")
    parser.add_argument(
        "--browser",
        action="store_true",
        help="vérifie en plus le pilotage du navigateur du système",
    )
    parser.add_argument(
        "--browser-timeout",
        type=float,
        default=120.0,
        help="secondes accordées au contrôle navigateur",
    )
    parser.add_argument(
        "--doc-index",
        action="store_true",
        help="vérifie en plus l'indexation d'un PDF par l'index documentaire",
    )
    parser.add_argument(
        "--appear-timeout",
        type=float,
        default=45.0,
        help="secondes accordées à la fenêtre pour apparaître",
    )
    parser.add_argument(
        "--close-timeout",
        type=float,
        default=20.0,
        help="secondes accordées au processus pour sortir après WM_CLOSE",
    )
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("Contrôle ignoré : la recette PyInstaller est Windows-only.")
        return 0

    exe = Path(args.exe)
    if not exe.is_file():
        print(f"Exécutable introuvable : {exe} (lancer d'abord build.bat)", file=sys.stderr)
        return 2

    if args.browser or args.doc_index:
        code = 0
        if args.browser:
            code = _browser_check(exe, args.browser_timeout)
        if args.doc_index:
            code = _doc_index_check(exe, args.browser_timeout) or code
        return code

    print(f"Contrôle de {exe}")
    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent))
    try:
        deadline = time.monotonic() + args.appear_timeout
        window: tuple[int, str] | None = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return _fail(
                    f"le processus s'est arrêté (code {proc.returncode}) sans afficher de fenêtre"
                )
            window = find_window(proc.pid)
            if window is not None:
                break
            time.sleep(0.2)

        if window is None:
            return _fail(f"aucune fenêtre visible après {args.appear_timeout:.0f} s")

        hwnd, title = window
        print(f"  ok   fenêtre visible : « {title} »")

        # Une fenêtre qui s'affiche puis plante aussitôt ne valide rien.
        time.sleep(0.5)
        if proc.poll() is not None:
            return _fail(f"le processus a quitté juste après l'affichage (code {proc.returncode})")
        print("  ok   processus toujours en vie après l'affichage")

        if not _close_window(hwnd):
            return _fail("WM_CLOSE n'a pas pu être envoyé")
        try:
            code = proc.wait(timeout=args.close_timeout)
        except subprocess.TimeoutExpired:
            return _fail(f"toujours vivant {args.close_timeout:.0f} s après WM_CLOSE")

        if code != 0:
            return _fail(f"sortie en erreur (code {code})")
        print("  ok   fermeture propre (code 0)")
    finally:
        _kill(proc)

    print("Contrôle réussi.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
