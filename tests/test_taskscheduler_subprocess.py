"""Chemin réel de `schtasks` via un exécutable factice placé dans PATH.

Ces tests ne dépendent pas du Planificateur Windows : ils vérifient que
`install_task()` / `uninstall_task()` exécutent bien un sous-processus avec les
arguments attendus, propagent le code retour et n'appellent rien quand le
service est indisponible. Le test d'intégration qui enregistre une vraie tâche
reste séparé et opt-in dans `tests/test_taskscheduler_integration.py`.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings  # noqa: E402

import core.taskscheduler as tasks  # noqa: E402

_PYTHON_FAKE = r'''import os
import sys
from pathlib import Path

with Path(os.environ["FAKE_SCHTASKS_LOG"]).open("a", encoding="utf-8") as log:
    log.write("\t".join(sys.argv[1:]) + "\n")
if os.environ.get("FAKE_SCHTASKS_FAIL") == "1":
    print("panne simulee", file=sys.stderr)
    raise SystemExit(7)
raise SystemExit(0)
'''

_CSHARP_FAKE = r'''using System;
using System.IO;

internal static class FakeSchtasks
{
    private static int Main(string[] args)
    {
        File.AppendAllText(
            Environment.GetEnvironmentVariable("FAKE_SCHTASKS_LOG"),
            string.Join("\t", args) + "\n"
        );
        if (Environment.GetEnvironmentVariable("FAKE_SCHTASKS_FAIL") == "1")
        {
            Console.Error.WriteLine("panne simulee");
            return 7;
        }
        return 0;
    }
}
'''


def _compile_windows_fake(bin_dir: Path, tmp_path: Path) -> None:
    """Compile un vrai exécutable Windows sans dépendre de Visual Studio."""
    candidates = (
        Path(os.environ.get("WINDIR", r"C:\Windows"))
        / "Microsoft.NET"
        / "Framework64"
        / "v4.0.30319"
        / "csc.exe",
        Path(os.environ.get("WINDIR", r"C:\Windows"))
        / "Microsoft.NET"
        / "Framework"
        / "v4.0.30319"
        / "csc.exe",
    )
    compiler = next((item for item in candidates if item.is_file()), None)
    if compiler is None:
        pytest.skip("csc.exe introuvable pour compiler le faux schtasks Windows")
    source = tmp_path / "fake_schtasks.cs"
    source.write_text(_CSHARP_FAKE, encoding="utf-8")
    output = bin_dir / "schtasks.exe"
    completed = subprocess.run(
        [str(compiler), "/nologo", "/target:exe", f"/out:{output}", str(source)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert output.is_file()


def _install_fake(monkeypatch, tmp_path: Path) -> Path:
    """Place un faux `schtasks` en tête de PATH et retourne son journal."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    log = tmp_path / "schtasks-calls.log"
    if os.name == "nt":
        _compile_windows_fake(bin_dir, tmp_path)
    else:
        script = tmp_path / "fake_schtasks.py"
        script.write_text(_PYTHON_FAKE, encoding="utf-8")
        launcher = bin_dir / "schtasks"
        launcher.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} {shlex.quote(str(script))} \"$@\"\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)
    monkeypatch.setenv(
        "PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    )
    monkeypatch.setenv("FAKE_SCHTASKS_LOG", str(log))
    return log


def _calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [
        line.split("\t")
        for line in log.read_text(encoding="utf-8").splitlines()
        if line
    ]


def test_install_and_uninstall_invoke_fake_schtasks(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tasks, "supported", lambda: True)
    log = _install_fake(monkeypatch, tmp_path)
    settings = make_settings(tmp_path)
    name = f"OmniAgent - Test PATH {uuid.uuid4().hex[:8]}"

    ok, message = tasks.install_task(settings, name=name)

    assert ok is True, message
    definition = settings.paths.cache_dir / "omniagent-task.xml"
    assert definition.is_file()
    calls = _calls(log)
    assert calls == [
        ["/Create", "/TN", name, "/XML", str(definition), "/F"]
    ], calls

    ok, message = tasks.uninstall_task(name=name)

    assert ok is True, message
    assert _calls(log)[-1] == ["/Delete", "/TN", name, "/F"]


def test_fake_schtasks_failure_is_reported(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tasks, "supported", lambda: True)
    monkeypatch.setenv("FAKE_SCHTASKS_FAIL", "1")
    log = _install_fake(monkeypatch, tmp_path)
    settings = make_settings(tmp_path)

    ok, message = tasks.install_task(settings, name="OmniAgent - Test echec")

    assert ok is False
    assert "panne simulee" in message
    assert _calls(log)[0][:3] == ["/Create", "/TN", "OmniAgent - Test echec"]


def test_no_registration_without_opt_in_or_when_scheduler_is_disabled(
    monkeypatch, tmp_path: Path
) -> None:
    log = _install_fake(monkeypatch, tmp_path)
    monkeypatch.setattr(tasks, "supported", lambda: True)
    monkeypatch.delenv(tasks.INTEGRATION_ENV, raising=False)
    settings = make_settings(tmp_path)

    # Le module d'intégration est gardé par ce prédicat ; sans opt-in, il ne
    # doit même pas tenter l'installation.
    assert tasks.integration_enabled() is False
    if tasks.integration_enabled():
        tasks.install_task(settings, name="OmniAgent - Test interdit")
    assert not log.exists()

    monkeypatch.setattr(tasks, "supported", lambda: False)
    ok, message = tasks.install_task(settings)
    assert ok is False and "Windows" in message
    assert not log.exists()

    ok, message = tasks.uninstall_task()
    assert ok is False and "Windows" in message
    assert not log.exists()
