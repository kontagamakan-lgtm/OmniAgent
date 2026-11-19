"""Enregistrement optionnel dans le Planificateur de tâches Windows.

La planification interne (`core.schedule`) attend sa fenêtre dans un processus
vivant : après une veille ou une fermeture de session, ce processus n'existe
plus. Une tâche Windows relance l'application au moment utile.

* déclencheur **à l'ouverture de session** ;
* déclencheur horaire aligné sur la fenêtre, avec `WakeToRun` : le réveil
  programmé réveille la machine pour la session ;
* `StartWhenAvailable` : Windows exécute la tâche dès que possible après une
  fenêtre manquée (veille, poste éteint), et `core.schedule` décide ensuite si
  le rattrapage a encore du sens (`catch_up`, `catch_up_hours`).

Le verrou anti-double exécution n'est pas déplacé dans la tâche : la tâche
lance `--autonomy-schedule --schedule-checks 1`, qui repasse par `plan_start`
et l'historique `autonomy/sessions.jsonl`. Windows ajoute sa propre protection
avec `MultipleInstancesPolicy=IgnoreNew`. Une session déjà en cours reste
protégée par le verrou de `AutonomySession`.

Rien n'est enregistré ici sans un appel explicite : `build_task_xml()` est une
fonction pure, et `install_task()` / `uninstall_task()` passent par
`schtasks.exe` uniquement sur Windows et avec un exécuteur injectable.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from xml.sax.saxutils import escape

from config import APP_ID, Settings, is_frozen
from core.schedule import Schedule, load_schedule

__all__ = [
    "INTEGRATION_ENV",
    "TASK_DESCRIPTION",
    "TASK_NAME",
    "build_task_xml",
    "install_task",
    "integration_enabled",
    "supported",
    "task_command",
    "uninstall_task",
]

logger = logging.getLogger(f"{APP_ID}.taskscheduler")

TASK_NAME = "OmniAgent - Session planifiee"
TASK_DESCRIPTION = (
    "OmniAgent - session autonome planifiee : ouverture de session, reveil et "
    "rattrapage des fenetres manquees"
)
# Le test d'intégration n'enregistre une vraie tâche que si cette variable vaut
# exactement "1" ; la suite normale ne touche jamais au Planificateur.
INTEGRATION_ENV = "FREEBUFF_SCHTASKS_INTEGRATION"

Runner = Callable[[list[str]], Any]
_XML_HEADER = '<?xml version="1.0" encoding="UTF-16"?>'


def supported() -> bool:
    """Le Planificateur de tâches n'existe que sous Windows."""
    return os.name == "nt"


def integration_enabled(environ: dict[str, str] | None = None) -> bool:
    """Autorisation explicite du test qui enregistre une vraie tâche.

    Strictement `"1"` : ni `true`, ni `yes`, ni une valeur vide. Hors Windows,
    le Planificateur n'existe pas, donc le test reste inactif même si la
    variable est posée.
    """
    if not supported():
        return False
    source = os.environ if environ is None else environ
    return source.get(INTEGRATION_ENV, "").strip() == "1"


def task_command() -> tuple[str, str, str]:
    """(exécutable, arguments, répertoire de travail) lancés par la tâche."""
    if is_frozen():
        command = sys.executable
        arguments = "--autonomy-schedule --schedule-checks 1"
        workdir = Path(sys.executable).resolve().parent
    else:
        script = Path(__file__).resolve().parent.parent / "app.py"
        command = sys.executable
        arguments = f'"{script}" --autonomy-schedule --schedule-checks 1'
        workdir = script.parent
    return command, arguments, str(workdir)


def _next_boundary(now: datetime, start_hour: int) -> str:
    """Prochaine occurrence de `start_hour`, au format attendu par Windows."""
    moment = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if moment <= now:
        moment += timedelta(days=1)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def build_task_xml(
    *,
    python: str,
    arguments: str,
    workdir: str,
    user: str = "",
    start_hour: int = 1,
    end_hour: int = 6,
    now: datetime | None = None,
    wake: bool = True,
    name: str = TASK_NAME,
) -> str:
    """Définition XML de la tâche, prête pour `schtasks /Create /XML`.

    Fonction pure : aucun fichier, aucun processus. Les créneaux sont calculés
    depuis `schedule.json` pour que le réveil tombe dans la fenêtre utile.
    """
    moment = now or datetime.now()
    boundary = _next_boundary(moment, start_hour)
    hours = end_hour - start_hour
    if hours <= 0:
        hours += 24
    principal_user = (
        f"\n      <UserId>{escape(user)}</UserId>" if user.strip() else ""
    )
    logon_user = f"\n      <UserId>{escape(user)}</UserId>" if user.strip() else ""
    return f"""{_XML_HEADER}
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(TASK_DESCRIPTION)}</Description>
    <URI>\\{escape(name)}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>{logon_user}
      <Delay>PT1M</Delay>
    </LogonTrigger>
    <TimeTrigger>
      <Enabled>true</Enabled>
      <StartBoundary>{boundary}</StartBoundary>
      <Repetition>
        <Interval>PT1H</Interval>
        <Duration>PT{hours}H</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">{principal_user}
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>{str(bool(wake)).lower()}</WakeToRun>
    <ExecutionTimeLimit>PT6H</ExecutionTimeLimit>
    <Priority>7</Priority>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(python)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _run(args: list[str]) -> Any:
    # Résoudre explicitement via PATH : c'est ce qui rend le chemin testable
    # avec un faux `schtasks` en tête de PATH, sous Windows comme ailleurs.
    executable = shutil.which(args[0]) or args[0]
    return subprocess.run(
        [executable, *args[1:]],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _failure(result: Any) -> str:
    detail = str(getattr(result, "stderr", "") or getattr(result, "stdout", "") or "")
    detail = " ".join(detail.split())
    return f"refuse par le Planificateur de taches : {detail or 'sans detail'}"


def install_task(
    settings: Settings,
    *,
    schedule: Schedule | None = None,
    user: str = "",
    runner: Runner | None = None,
    now: datetime | None = None,
    name: str = TASK_NAME,
) -> tuple[bool, str]:
    """Écrit la définition puis l'enregistre ; `(succès, message lisible)`.

    Le fichier XML reste dans `cache/` : il documente la tâche installée et
    permet de la réenregistrer sans reconstruire l'application.
    """
    if not supported():
        return False, "Planificateur de taches : Windows uniquement"
    chosen = schedule or load_schedule(settings)
    command, arguments, workdir = task_command()
    xml = build_task_xml(
        python=command,
        arguments=arguments,
        workdir=workdir,
        user=user,
        start_hour=chosen.start_hour,
        end_hour=chosen.end_hour,
        now=now,
        name=name,
    )
    target = settings.paths.cache_dir / "omniagent-task.xml"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(xml, encoding="utf-16")
    except OSError as exc:
        return False, f"definition non ecrite : {exc}"
    result = (runner or _run)(
        ["schtasks", "/Create", "/TN", name, "/XML", str(target), "/F"]
    )
    if int(getattr(result, "returncode", 1)) != 0:
        return False, _failure(result)
    return True, f"tache enregistree : {name}"


def uninstall_task(
    *, name: str = TASK_NAME, runner: Runner | None = None
) -> tuple[bool, str]:
    """Désinscrit la tâche ; succès seulement si `schtasks` l'a confirmé."""
    if not supported():
        return False, "Planificateur de taches : Windows uniquement"
    result = (runner or _run)(["schtasks", "/Delete", "/TN", name, "/F"])
    if int(getattr(result, "returncode", 1)) != 0:
        return False, _failure(result)
    return True, f"tache desinscrite : {name}"
