"""Notification Windows native, sans dépendance supplémentaire.

Le planificateur tournait dans une console que personne ne regarde la nuit. Une
toast Windows est le seul signal visible sans installer de paquet : PowerShell
5.1 expose déjà WinRT (`Windows.UI.Notifications`).

Le script est construit ici, encodé en UTF-16LE puis base64 (`-EncodedCommand`)
pour éviter tout problème de guillemets ou d'accent dans la ligne de commande.
Le titre et le message sont échappés en XML : une tâche utilisateur contenant
`<` ne doit pas casser le toast ni injecter de balise.

Hors Windows, `notify_windows` retourne `False` sans rien tenter. Un échec de
PowerShell (politique de groupe, session sans bureau) est journalisé et ne doit
jamais faire échouer la session planifiée.
"""

from __future__ import annotations

import base64
import html
import logging
import os
import subprocess
from typing import Any

from config import APP_ID

__all__ = ["APP_USER_MODEL_ID", "build_toast_script", "notify_windows", "supported"]

logger = logging.getLogger(f"{APP_ID}.notify")

APP_USER_MODEL_ID = "OmniAgent"


def supported() -> bool:
    """Vrai sur Windows ; les autres systèmes n'ont pas de toast WinRT."""
    return os.name == "nt"


def build_toast_script(
    title: str, message: str, *, app_id: str = APP_USER_MODEL_ID
) -> str:
    """Script PowerShell qui affiche une toast générique."""
    safe_title = html.escape(str(title or ""), quote=True)
    safe_message = html.escape(str(message or ""), quote=True)
    safe_app = html.escape(str(app_id or APP_USER_MODEL_ID), quote=True)
    xml = (
        '<toast><visual><binding template="ToastGeneric">'
        f"<text>{safe_title}</text><text>{safe_message}</text>"
        "</binding></visual></toast>"
    )
    return f"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(@'
{xml}
'@)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{safe_app}').Show($toast)
""".strip()


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def notify_windows(title: str, message: str, *, timeout_s: float = 15.0) -> bool:
    """Affiche une toast ; faux si la plateforme ou PowerShell la refuse."""
    if not supported():
        logger.debug("notification Windows ignorée hors Windows")
        return False
    script = build_toast_script(title, message)
    try:
        completed: Any = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-EncodedCommand",
                _encoded(script),
            ],
            capture_output=True,
            timeout=max(1.0, float(timeout_s)),
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("notification Windows impossible : %s", exc)
        return False
    if int(getattr(completed, "returncode", 1)) != 0:
        detail = getattr(completed, "stderr", b"")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        logger.warning("notification Windows refusée : %s", str(detail)[:300])
        return False
    return True


def notify_scheduled_session(
    *, cycles: int, cost: float, ok: bool, duration_s: float
) -> bool:
    """Message de fin de session planifiée."""
    status = "terminée" if ok else "terminée avec échec"
    message = (
        f"Session {status} : {cycles} cycle(s), "
        f"{cost:.4f} USD, {duration_s:.0f} s"
    )
    return notify_windows("OmniAgent — session planifiée", message)
