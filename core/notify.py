"""Notifications Windows natives : identité, actions et repli.

Une toast WinRT n'a pas d'identité stable sans raccourci de menu Démarrer portant
le même `AppUserModelID` : Windows peut la reléguer, l'attribuer à un autre
programme ou l'ignorer. Ce module :

* crée (sur demande) un raccourci de menu Démarrer pointant vers l'application,
  avec l'AUMID `OmniAgent.Desktop` écrit dans les propriétés du `.lnk` ;
* déclare le protocole `omniagent://` dans `HKCU\\Software\\Classes`, ce qui rend
  les boutons « Ouvrir la trace » et « Arrêter » réellement cliquables ;
* construit la toast avec ces actions, en échappant tout contenu utilisateur ;
* retombe sur une toast sans boutons si Windows refuse la version enrichie.

Rien de tout cela n'est automatique : l'utilisateur lance `--notify-install`.
Hors Windows, `notify_windows` retourne `False` sans rien tenter ; un échec
PowerShell est journalisé et ne fait jamais échouer une session planifiée.

    python app.py --notify-install
    python app.py --notify-action omniagent://trace/<run_id>
"""

from __future__ import annotations

import base64
import html
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import unquote, urlparse

from config import APP_ID

__all__ = [
    "APP_USER_MODEL_ID",
    "PROTOCOL",
    "ToastAction",
    "build_protocol_script",
    "build_shortcut_script",
    "build_toast_script",
    "install_notifications",
    "notify_scheduled_session",
    "notify_windows",
    "parse_toast_action",
    "protocol_command",
    "shortcut_path",
    "supported",
]

logger = logging.getLogger(f"{APP_ID}.notify")

APP_USER_MODEL_ID = "OmniAgent.Desktop"
PROTOCOL = "omniagent"
SHORTCUT_NAME = "OmniAgent.lnk"


def supported() -> bool:
    return os.name == "nt"


@dataclass(frozen=True, slots=True)
class ToastAction:
    """Un bouton de toast, identifié par son URI de protocole."""

    label: str
    uri: str


def shortcut_path() -> Path:
    """Raccourci dans le menu Démarrer de l'utilisateur courant."""
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / SHORTCUT_NAME
    )


def _ps_quote(value: str) -> str:
    """Chaîne PowerShell entre apostrophes, apostrophes internes doublées."""
    return "'" + str(value).replace("'", "''") + "'"


def build_toast_script(
    title: str,
    message: str,
    *,
    app_id: str = APP_USER_MODEL_ID,
    actions: Sequence[ToastAction] = (),
) -> str:
    """Script PowerShell qui affiche une toast, avec actions facultatives."""
    safe_title = html.escape(str(title or ""), quote=True)
    safe_message = html.escape(str(message or ""), quote=True)
    safe_app = html.escape(str(app_id or APP_USER_MODEL_ID), quote=True)
    buttons = ""
    if actions:
        rendered = "".join(
            f'<action content="{html.escape(str(action.label), quote=True)}" '
            f'activationType="protocol" '
            f'arguments="{html.escape(str(action.uri), quote=True)}"/>'
            for action in actions
        )
        buttons = f"<actions>{rendered}</actions>"
    xml = (
        '<toast><visual><binding template="ToastGeneric">'
        f"<text>{safe_title}</text><text>{safe_message}</text>"
        f"</binding></visual>{buttons}</toast>"
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


def build_shortcut_script(
    target: str,
    arguments: str = "",
    *,
    icon: str = "",
    app_id: str = APP_USER_MODEL_ID,
    destination: Path | None = None,
) -> str:
    """Script qui crée le `.lnk` du menu Démarrer et y inscrit l'AUMID.

    WScript.Shell crée le raccourci ; l'AUMID est écrit via `IPropertyStore`
    (Add-Type C#), car `WScript.Shell` ne connaît pas cette propriété.
    """
    link = destination or shortcut_path()
    icon_line = (
        f"$shortcut.IconLocation = {_ps_quote(str(icon))}\n" if icon else ""
    )
    return f"""
$ErrorActionPreference = 'Stop'
$link = {_ps_quote(str(link))}
$directory = Split-Path -Parent $link
if (-not (Test-Path -LiteralPath $directory)) {{ New-Item -ItemType Directory -Path $directory -Force | Out-Null }}
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = {_ps_quote(str(target))}
$shortcut.Arguments = {_ps_quote(str(arguments))}
$shortcut.WorkingDirectory = {_ps_quote(str(Path(target).parent))}
{icon_line}$shortcut.Save()

$code = @"
using System;
using System.Runtime.InteropServices;

[ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPropertyStore {{
    uint GetCount(out uint count);
    void GetAt(uint index, out PROPERTYKEY key);
    void GetValue(ref PROPERTYKEY key, out PROPVARIANT value);
    void SetValue(ref PROPERTYKEY key, ref PROPVARIANT value);
    void Commit();
}}

[StructLayout(LayoutKind.Sequential, Pack = 4)]
struct PROPERTYKEY {{ public Guid fmtid; public uint pid; }}

[StructLayout(LayoutKind.Explicit)]
struct PROPVARIANT {{
    [FieldOffset(0)] public ushort vt;
    [FieldOffset(8)] public IntPtr pointerValue;
}}

public static class AumidSetter {{
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    static extern void SHGetPropertyStoreFromParsingName(
        string path, IntPtr pbc, uint flags, ref Guid riid,
        [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);

    public static void Set(string path, string aumid) {{
        Guid iid = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
        IPropertyStore store;
        SHGetPropertyStoreFromParsingName(path, IntPtr.Zero, 2, ref iid, out store);
        PROPERTYKEY key = new PROPERTYKEY();
        key.fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
        key.pid = 5;
        PROPVARIANT value = new PROPVARIANT();
        value.vt = 31;
        value.pointerValue = Marshal.StringToCoTaskMemUni(aumid);
        try {{ store.SetValue(ref key, ref value); store.Commit(); }}
        finally {{ Marshal.FreeCoTaskMem(value.pointerValue); Marshal.ReleaseComObject(store); }}
    }}
}}
"@
Add-Type -TypeDefinition $code -Language CSharp | Out-Null
[AumidSetter]::Set($link, {_ps_quote(str(app_id))})
Write-Output $link
""".strip()


def protocol_command(target: str, arguments: str = '--notify-action "%1"') -> str:
    """Ligne de commande enregistrée pour `omniagent://`."""
    return f'"{target}" {arguments}'.strip()


def build_protocol_script(
    target: str,
    arguments: str = '--notify-action "%1"',
    *,
    protocol: str = PROTOCOL,
) -> str:
    """Script qui déclare le protocole dans HKCU (aucun droit administrateur)."""
    command = protocol_command(target, arguments)
    return f"""
$ErrorActionPreference = 'Stop'
$root = 'HKCU:\\Software\\Classes\\{protocol}'
New-Item -Path $root -Force | Out-Null
Set-ItemProperty -Path $root -Name '(default)' -Value 'URL:OmniAgent Protocol'
Set-ItemProperty -Path $root -Name 'URL Protocol' -Value ''
$command = Join-Path $root 'shell\\open\\command'
New-Item -Path $command -Force | Out-Null
Set-ItemProperty -Path $command -Name '(default)' -Value {_ps_quote(command)}
Write-Output $command
""".strip()


def parse_toast_action(uri: str) -> tuple[str, str] | None:
    """Décode `omniagent://trace/<id>` ou `omniagent://stop`.

    Retourne `("trace", id)` / `("stop", "")`, ou `None` pour toute autre URL :
    un bouton ne doit jamais lancer une action arbitraire.
    """
    try:
        parsed = urlparse(str(uri or ""))
    except ValueError:
        return None
    if parsed.scheme.lower() != PROTOCOL:
        return None
    action = (parsed.netloc or parsed.path.lstrip("/")).strip().lower()
    if action == "stop":
        return ("stop", "")
    if action == "trace":
        run_id = unquote(parsed.path.lstrip("/")).strip()
        return ("trace", run_id)
    return None


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _run_script(script: str, *, timeout_s: float = 20.0) -> tuple[bool, str]:
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
        return False, str(exc)
    stdout = getattr(completed, "stdout", b"")
    stderr = getattr(completed, "stderr", b"")
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if int(getattr(completed, "returncode", 1)) != 0:
        return False, str(stderr or stdout or "code non nul")[:300]
    return True, str(stdout or "").strip()


def _show(script: str, timeout_s: float) -> bool:
    ok, detail = _run_script(script, timeout_s=timeout_s)
    if not ok:
        logger.warning("notification Windows refusée : %s", detail)
    return ok


def notify_windows(
    title: str,
    message: str,
    *,
    timeout_s: float = 15.0,
    actions: Iterable[ToastAction] = (),
) -> bool:
    """Affiche une toast ; si les actions sont refusées, réessaie sans elles."""
    if not supported():
        logger.debug("notification Windows ignorée hors Windows")
        return False
    buttons = tuple(actions)
    if buttons and _show(
        build_toast_script(title, message, actions=buttons), timeout_s
    ):
        return True
    if buttons:
        logger.info("toast avec actions refusée : nouvel essai sans boutons")
    return _show(build_toast_script(title, message), timeout_s)


def install_notifications(
    target: str,
    arguments: str = "",
    *,
    icon: str = "",
) -> tuple[bool, str]:
    """Crée le raccourci et déclare le protocole ; retourne (ok, message)."""
    if not supported():
        return False, "les notifications Windows ne sont disponibles que sur Windows"
    ok, detail = _run_script(build_shortcut_script(target, arguments, icon=icon))
    if not ok:
        return False, f"raccourci non créé : {detail}"
    ok, detail = _run_script(build_protocol_script(target))
    if not ok:
        return False, f"protocole non déclaré : {detail}"
    return True, f"raccourci et protocole {PROTOCOL}:// installés ({shortcut_path()})"


def notify_scheduled_session(
    *,
    cycles: int,
    cost: float,
    ok: bool,
    duration_s: float,
    trace_path: str | Path | None = None,
    run_id: str = "",
) -> bool:
    """Message de fin de session planifiée, avec « Ouvrir la trace » si connue."""
    status = "terminée" if ok else "terminée avec échec"
    message = (
        f"Session {status} : {cycles} cycle(s), "
        f"{cost:.4f} USD, {duration_s:.0f} s"
    )
    actions: list[ToastAction] = []
    identifier = run_id or (Path(trace_path).stem if trace_path else "")
    if identifier:
        actions.append(ToastAction("Ouvrir la trace", f"{PROTOCOL}://trace/{identifier}"))
    if not ok:
        actions.append(ToastAction("Arrêter", f"{PROTOCOL}://stop"))
    return notify_windows("OmniAgent — session planifiée", message, actions=actions)


def self_test() -> int:
    """Affiche une toast de vérification ; code 1 si Windows la refuse."""
    return 0 if notify_windows("OmniAgent", "Les notifications fonctionnent.") else 1


if __name__ == "__main__":  # pragma: no cover - manuel
    sys.exit(self_test())
