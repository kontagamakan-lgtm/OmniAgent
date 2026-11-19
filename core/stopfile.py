"""Arrêt d'urgence indépendant de l'interface.

Un fichier `data/autonomy/STOP` est le signal le plus simple qui survit à une
fenêtre masquée, à un bouton inaccessible ou à une interface figée. Il est lu
entre deux événements de l'agent (et au début de chaque cycle) : dès qu'il
existe, la session demande son arrêt et l'annulation coopérative ferme le flux
en cours.

Le fichier n'est jamais supprimé automatiquement : un arrêt d'urgence ne doit
pas disparaître tout seul pour laisser repartir une session au cycle suivant.
L'utilisateur (ou `app.py --autonomy-resume`) le retire explicitement.
"""

from __future__ import annotations

from pathlib import Path

from config import Settings
from core.autonomy import autonomy_dir

__all__ = ["STOP_NAME", "clear_stop", "request_stop", "stop_path", "stop_requested"]

STOP_NAME = "STOP"


def stop_path(settings: Settings) -> Path:
    return autonomy_dir(settings) / STOP_NAME


def stop_requested(settings: Settings) -> bool:
    return stop_path(settings).is_file()


def request_stop(settings: Settings, reason: str = "") -> Path:
    """Crée le fichier d'arrêt (idempotent) et retourne son chemin."""
    path = stop_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "arret demande par l'utilisateur" + (f" : {reason}" if reason else "")
        path.write_text(body + "\n", encoding="utf-8")
    except OSError:
        pass
    return path


def clear_stop(settings: Settings) -> bool:
    """Retire le fichier ; vrai s'il a effectivement été supprimé."""
    path = stop_path(settings)
    try:
        path.unlink()
        return True
    except OSError:
        return False
