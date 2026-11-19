"""Capture visuelle réelle : boîte d'approbation, toast et chemin .lnk/protocole.

Le script n'est pas un test unitaire : il ouvre les vraies fenêtres Windows,
prend une capture d'écran, vérifie que la zone n'est pas blanche, que la
géométrie est plausible, que les trois boutons natifs existent et que l'OCR
retrouve le texte attendu. Les images et le manifeste sont écrits dans
`artifacts/visual/`.

    python tools/visual_check.py --capture

Sous Windows uniquement. Si la session n'est pas interactive ou si la capture
échoue, le script sort en erreur avec la sortie concrète : il ne fabrique jamais
une image de remplacement.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
import unicodedata
from ctypes import wintypes
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageGrab, ImageStat

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ARTIFACTS = ROOT / "artifacts" / "visual"
APPROVAL_TITLE = "Confirmation requise — fs_delete"
PATH_TITLE = "OmniAgent — chemin des notifications"
TOAST_TITLE = "OmniAgent — vérification visuelle"
TOAST_MESSAGE = "Les notifications fonctionnent."


def _enable_dpi_awareness() -> str:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            return "system"
        except (AttributeError, OSError):
            return "none"


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", str(text))
    return "".join(char for char in folded if not unicodedata.combining(char)).lower()


def _window_text(hwnd: int) -> str:
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _find_window(substring: str, *, timeout_s: float = 20.0) -> int:
    """Attend une fenêtre visible dont le titre contient `substring`."""
    deadline = time.monotonic() + timeout_s
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd: int, _lparam: int) -> bool:
        if ctypes.windll.user32.IsWindowVisible(hwnd) and _normalize(
            substring
        ) in _normalize(_window_text(hwnd)):
            found.append(hwnd)
        return True

    while time.monotonic() < deadline:
        found.clear()
        ctypes.windll.user32.EnumWindows(callback, 0)
        if found:
            return found[0]
        time.sleep(0.2)
    raise RuntimeError(f"fenetre introuvable apres {timeout_s}s : {substring!r}")


def _rect(hwnd: int) -> tuple[int, int, int, int]:
    area = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(area)):
        raise ctypes.WinError()
    return (int(area.left), int(area.top), int(area.right), int(area.bottom))


def _buttons(hwnd: int) -> list[dict[str, Any]]:
    """Boutons natifs visibles, avec leur libellé et leur rectangle."""
    result: list[dict[str, Any]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(child: int, _lparam: int) -> bool:
        if not ctypes.windll.user32.IsWindowVisible(child):
            return True
        name = ctypes.create_unicode_buffer(64)
        ctypes.windll.user32.GetClassNameW(child, name, 64)
        if name.value.lower() != "button":
            return True
        label = _window_text(child).replace("&", "").strip()
        if not label:
            return True
        left, top, right, bottom = _rect(child)
        result.append(
            {
                "label": label,
                "rect": [left, top, right, bottom],
                "width": right - left,
                "height": bottom - top,
            }
        )
        return True

    ctypes.windll.user32.EnumChildWindows(hwnd, callback, 0)
    return result


def _pixel_stats(image: Image.Image) -> dict[str, Any]:
    gray = image.convert("L")
    stat = ImageStat.Stat(gray)
    histogram = gray.histogram()
    total = max(1, gray.width * gray.height)
    dark = sum(histogram[:230])
    colors = image.convert("RGB").getcolors(maxcolors=1_000_000)
    return {
        "size": [image.width, image.height],
        "stddev": round(float(stat.stddev[0]), 3),
        "mean": round(float(stat.mean[0]), 3),
        "unique_colors": len(colors) if colors else 1_000_000,
        "dark_fraction": round(dark / total, 4),
    }


def _ocr(image: Image.Image) -> tuple[str, list[dict[str, Any]]]:
    import tesserocr

    data = ROOT / "assets" / "tessdata"
    if not (data / "eng.traineddata").is_file():
        raise RuntimeError(f"donnees OCR absentes : {data}")
    with tesserocr.PyTessBaseAPI(path=str(data), lang="eng") as api:
        api.SetImage(image)
        text = api.GetUTF8Text()
    # `GetWords()` de cette version renvoie des couples (image, boîte), sans
    # texte : le libellé global suffit pour les assertions et le manifeste.
    return text, []


def _capture_region(
    path: Path, area: tuple[int, int, int, int]
) -> tuple[Image.Image, dict[str, Any]]:
    # Seule la fenêtre est conservée : une capture plein écran contiendrait le
    # bureau et des documents sans rapport avec la preuve visuelle.
    screen = ImageGrab.grab()
    left, top, right, bottom = area
    left, top = max(0, left), max(0, top)
    right, bottom = min(screen.width, right), min(screen.height, bottom)
    if right - left < 50 or bottom - top < 50:
        raise RuntimeError(f"zone de capture invalide : {(left, top, right, bottom)}")
    crop = screen.crop((left, top, right, bottom))
    crop.save(path)
    return crop, _pixel_stats(crop)


def _capture_window(
    hwnd: int, crop_path: Path
) -> tuple[Image.Image, tuple[int, int, int, int], dict[str, Any]]:
    area = _rect(hwnd)
    crop, stats = _capture_region(crop_path, area)
    if stats["stddev"] < 2.0 or stats["unique_colors"] < 8:
        raise RuntimeError(f"capture quasi vide : {stats}")
    return crop, area, stats


def _dialog_child() -> int:
    """Processus enfant : ouvre la vraie boîte d'approbation et attend."""
    import threading
    import tkinter as tk

    from core.approval import ApprovalRequest
    from ui.app_gui import OmniAgentWindow

    root = tk.Tk()
    root.withdraw()
    window = object.__new__(OmniAgentWindow)
    window.after = root.after
    request = ApprovalRequest(
        tool="fs_delete",
        scope="fichiers",
        effect="write",
        preview=(
            "Suppression definitive de C:\\tmp\\rapport.txt\n"
            "(1 fichier, 42 octet(s))."
        ),
    )

    def ask() -> None:
        window._tool_approval(request)

    root.after(200, lambda: threading.Thread(target=ask, daemon=True).start())
    root.mainloop()
    return 0


def _path_child(text: str) -> int:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    root.after(200, lambda: messagebox.showinfo(PATH_TITLE, text))
    root.mainloop()
    return 0


def _notification_paths() -> dict[str, str]:
    from core.notify import APP_USER_MODEL_ID, shortcut_path

    link = shortcut_path()
    if not link.is_file():
        raise RuntimeError(f"raccourci absent : {link}")
    script = (
        '$p=Join-Path $env:APPDATA "Microsoft\\Windows\\Start Menu\\Programs\\OmniAgent.lnk";'
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p);"
        'Write-Output ("TARGET=" + $s.TargetPath);'
        'Write-Output ("ARGS=" + $s.Arguments)'
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"lecture du raccourci impossible : {completed.stderr}")
    values: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    protocol_script = (
        'Get-ItemProperty -Path "HKCU:\\Software\\Classes\\omniagent\\shell\\open\\command"'
        ' | Select-Object -ExpandProperty "(default)"'
    )
    protocol = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", protocol_script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if protocol.returncode != 0:
        raise RuntimeError(f"lecture du protocole impossible : {protocol.stderr}")
    data = link.read_bytes()
    return {
        "shortcut": str(link),
        "target": values.get("TARGET", ""),
        "arguments": values.get("ARGS", ""),
        "protocol": protocol.stdout.strip(),
        "aumid": APP_USER_MODEL_ID,
        "aumid_in_shortcut": bool(APP_USER_MODEL_ID.encode("utf-16-le") in data),
    }


def _kill(process: subprocess.Popen) -> None:
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
        capture_output=True,
        check=False,
    )
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - taskkill a echoue
        process.kill()


def _spawn(mode: str, *extra: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), mode, *extra],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def capture() -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("capture visuelle : Windows uniquement")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    awareness = _enable_dpi_awareness()
    screen = ImageGrab.grab()
    if screen.width < 800 or screen.height < 600:
        raise RuntimeError(f"ecran trop petit pour une capture utile : {screen.size}")

    # --- Boîte d'approbation réelle -------------------------------------
    child = _spawn("--dialog-child")
    try:
        hwnd = _find_window(APPROVAL_TITLE)
        time.sleep(0.4)
        frame, area, stats = _capture_window(
            hwnd, ARTIFACTS / "approval_dialog.png"
        )
        width, height = frame.size
        if width < 250 or height < 120:
            raise RuntimeError(f"geometrie inattendue pour la boite : {frame.size}")
        buttons = _buttons(hwnd)
        labels = {_normalize(button["label"]) for button in buttons}
        expected = {"oui", "non", "annuler"}
        if not expected <= labels:
            raise RuntimeError(f"boutons natifs absents : {buttons}")
        for button in buttons:
            left, top, right, bottom = button["rect"]
            if not (area[0] <= left < right <= area[2] and area[1] <= top < bottom <= area[3]):
                raise RuntimeError(f"bouton hors de la fenetre : {button}")
            piece = screen.crop((left + 2, top + 2, right - 2, bottom - 2)).convert("L")
            if ImageStat.Stat(piece).stddev[0] < 1.0:
                raise RuntimeError(f"bouton visuellement vide : {button}")
        text, _words = _ocr(frame)
        normalized_text = _normalize(text)
        if not all(label in normalized_text for label in expected):
            raise RuntimeError(f"OCR sans les trois boutons : {text!r}")
        approval = {
            "title": APPROVAL_TITLE,
            "window": list(area),
            "geometry": [width, height],
            "buttons": buttons,
            "ocr": text,
            "pixels": stats,
            "files": {"crop": "artifacts/visual/approval_dialog.png"},
        }
    finally:
        _kill(child)

    # --- Chemin .lnk / protocole ----------------------------------------
    paths = _notification_paths()
    path_text = (
        f"Raccourci : {paths['shortcut']}\n"
        f"Cible : {paths['target']} {paths['arguments']}\n"
        f"Protocole : {paths['protocol']}\n"
        f"AUMID : {paths['aumid']} ({paths['aumid_in_shortcut']})"
    )
    child = _spawn("--path-child", path_text)
    try:
        hwnd = _find_window(PATH_TITLE)
        time.sleep(0.4)
        frame, area, stats = _capture_window(
            hwnd, ARTIFACTS / "notification_path.png"
        )
        text, _words = _ocr(frame)
        normalized = _normalize(text)
        # Les chemins sont vérifiés exactement via le système et conservés dans
        # le manifeste ; l'OCR prouve seulement que la fenêtre rendue les montre.
        if not all(word in normalized for word in ("raccourci", "protocole", "omniagent")):
            raise RuntimeError(f"OCR du chemin incomplet : {text!r}")
        notification_path = {
            "shortcut": paths["shortcut"],
            "target": paths["target"],
            "arguments": paths["arguments"],
            "protocol": paths["protocol"],
            "aumid": paths["aumid"],
            "aumid_in_shortcut": paths["aumid_in_shortcut"],
            "window": list(area),
            "geometry": list(frame.size),
            "ocr": text,
            "pixels": stats,
            "files": {"crop": "artifacts/visual/notification_path.png"},
        }
    finally:
        _kill(child)

    # --- Toast réelle ----------------------------------------------------
    from core.notify import notify_windows

    right = screen.width
    bottom = screen.height
    region = (max(0, right - 620), max(0, bottom - 320), right, bottom - 40)
    before = ImageGrab.grab(bbox=region)
    if not notify_windows(TOAST_TITLE, TOAST_MESSAGE, timeout_s=20.0):
        raise RuntimeError("Windows a refuse la toast (notify_windows a retourne False)")
    time.sleep(0.9)
    after = ImageGrab.grab(bbox=region)
    diff = ImageChops.difference(before, after).convert("L")
    changed = diff.point(lambda value: 255 if value > 28 else 0)
    bbox = changed.getbbox()
    if bbox is None:
        raise RuntimeError("aucun pixel de toast n'a change dans la zone bas-droite")
    # Marge pour inclure le cadre de la toast.
    left = max(0, bbox[0] - 12)
    top = max(0, bbox[1] - 12)
    right = min(after.width, bbox[2] + 12)
    bottom = min(after.height, bbox[3] + 12)
    crop = after.crop((left, top, right, bottom))
    if crop.width < 200 or crop.height < 70:
        raise RuntimeError(f"geometrie de toast inattendue : {crop.size}")
    stats = _pixel_stats(crop)
    if stats["stddev"] < 3.0 or stats["unique_colors"] < 8:
        raise RuntimeError(f"zone de toast quasi vide : {stats}")
    text, words = _ocr(crop)
    normalized = _normalize(text)
    if "omniagent" not in normalized or "notification" not in normalized:
        raise RuntimeError(f"OCR de toast inattendu : {text!r}")
    crop.save(ARTIFACTS / "toast.png")
    toast = {
        "title": TOAST_TITLE,
        "message": TOAST_MESSAGE,
        "region": list(region),
        "crop": [left, top, right, bottom],
        "geometry": [crop.width, crop.height],
        "changed_pixels": sum(
            1 for value in changed.getdata() if value  # noqa: PLC0206 - borné à 620x280
        ),
        "ocr": text,
        "words": words,
        "pixels": stats,
        "files": {"crop": "artifacts/visual/toast.png"},
    }

    manifest = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": os.environ.get("COMPUTERNAME", ""),
        "session": os.environ.get("SESSIONNAME", ""),
        "screen": [screen.width, screen.height],
        "dpi_awareness": awareness,
        "capture_backend": "PIL.ImageGrab (Win32 GDI)",
        "approval": approval,
        "notification_path": notification_path,
        "toast": toast,
    }
    (ARTIFACTS / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/visual_check.py")
    parser.add_argument("--capture", action="store_true", help="capture et vérifie les rendus")
    parser.add_argument("--dialog-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--path-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("text", nargs="?", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.dialog_child:
        return _dialog_child()
    if args.path_child:
        return _path_child(args.text or "")
    if args.capture:
        try:
            manifest = capture()
        except Exception as exc:  # noqa: BLE001 - le diagnostic concret est la sortie
            print(f"CAPTURE VISUELLE IMPOSSIBLE : {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
