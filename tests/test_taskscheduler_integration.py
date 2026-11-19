"""Test d'intégration Windows du Planificateur : opt-in, réel, nettoyant.

Rien n'est enregistré par la suite normale : ce module est entièrement ignoré
tant que `FREEBUFF_SCHTASKS_INTEGRATION` ne vaut pas exactement `1` sous
Windows. Pour l'activer sur Windows :

    PowerShell
    $env:FREEBUFF_SCHTASKS_INTEGRATION = "1"
    python -m pytest tests/test_taskscheduler_integration.py -q

    Invite de commandes
    set FREEBUFF_SCHTASKS_INTEGRATION=1
    python -m pytest tests/test_taskscheduler_integration.py -q

Quand il est autorisé, il appelle le vrai `install_task()` (sans runner
injecté), laisse `install_task()` écrire le XML puis exécuter le binaire
`schtasks.exe`, relit la définition avec `schtasks /Query /XML`, et appelle
`uninstall_task()` dans un `try/finally` avant de vérifier que la tâche a
disparu. Cela prouve le chemin réel : création par le Planificateur Windows,
contenu enregistré et suppression. Les tests de
`tests/test_taskscheduler_subprocess.py` prouvent seulement que le code appelle
un exécutable `schtasks` (factice, via `PATH`) avec les bons arguments et
propage son code retour ; ils ne prouvent pas qu'une tâche Windows est
réellement enregistrée.

Après le test, vérifier qu'aucune tâche jetable ne reste :

    schtasks /Query /FO TABLE | findstr /I "OmniAgent - Test integration"
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

import core.taskscheduler as tasks  # noqa: E402

pytestmark = pytest.mark.skipif(
    not tasks.integration_enabled(),
    reason=(
        "test d'integration schtasks opt-in : definir "
        f"{tasks.INTEGRATION_ENV}=1 sous Windows pour enregistrer une vraie tache"
    ),
)


def _schtasks(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["schtasks", *args], capture_output=True, timeout=90, check=False
    )


def _text(raw: bytes) -> str:
    """`schtasks` écrit selon la console : BOM UTF-16, UTF-8 ou code OEM."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for encoding in ("utf-8", "cp1252", "cp850"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def test_real_registration_definition_then_removal() -> None:
    name = f"OmniAgent - Test integration {uuid.uuid4().hex[:8]}"
    with tmpdir("tache-integration") as tmp:
        settings = make_settings(tmp)
        attempted = False
        installed = False
        try:
            attempted = True
            ok, message = tasks.install_task(
                settings, user=os.environ.get("USERNAME", ""), name=name
            )
            if not ok:
                pytest.fail(f"enregistrement refuse par le Planificateur : {message}")
            installed = True

            query = _schtasks("/Query", "/TN", name, "/XML")
            assert query.returncode == 0, _text(query.stderr)
            definition = _text(query.stdout)

            # La définition relue est bien celle construite, pas un ancien nom.
            assert name in definition
            assert tasks.TASK_DESCRIPTION in definition
            assert "<LogonTrigger>" in definition
            assert "<WakeToRun>true</WakeToRun>" in definition
            assert "<StartWhenAvailable>true</StartWhenAvailable>" in definition
            assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in definition
            assert "--autonomy-schedule --schedule-checks 1" in definition
            assert Path(sys.executable).name in definition
        finally:
            # Nettoyage garanti dès que l'enregistrement a été accepté ; le
            # finally couvre aussi une erreur de relecture ou d'assertion.
            if attempted:
                removed, removal_message = tasks.uninstall_task(name=name)
                if installed:
                    assert removed is True, removal_message

        gone = _schtasks("/Query", "/TN", name)
        assert gone.returncode != 0, "la tache de test doit avoir disparu"
