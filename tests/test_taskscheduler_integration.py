"""Test d'intégration Windows du Planificateur : opt-in, réel, nettoyant.

Rien n'est enregistré par la suite normale : ce module est entièrement ignoré
tant que `OMNIAGENT_TASK_INTEGRATION` ne vaut pas exactement `1` sous Windows.
Quand il est autorisé, il enregistre une tâche au nom unique, vérifie la
définition relue par `schtasks`, puis la désinscrit et contrôle sa disparition.
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
        "integration Windows opt-in : definir "
        f"{tasks.INTEGRATION_ENV}=1 pour enregistrer une vraie tache"
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
        ok, message = tasks.install_task(
            settings, user=os.environ.get("USERNAME", ""), name=name
        )
        if not ok:
            pytest.fail(f"enregistrement refuse par le Planificateur : {message}")

        try:
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
            removed, removal_message = tasks.uninstall_task(name=name)

        assert removed is True, removal_message
        gone = _schtasks("/Query", "/TN", name)
        assert gone.returncode != 0, "la tache de test doit avoir disparu"
