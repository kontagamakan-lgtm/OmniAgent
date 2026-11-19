"""Durcissement du shell : pas de secrets hérités, arbre tué au délai."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

from tools.base import ToolError  # noqa: E402
from tools.file_manager import FileManager  # noqa: E402
from tools.shell import ShellRunner  # noqa: E402


def test_shell_child_does_not_inherit_secrets(monkeypatch) -> None:
    with tmpdir("shell-env") as tmp:
        settings = make_settings(tmp)
        monkeypatch.setenv("OMNIAGENT_TEST_SECRET", "s3cret-value")
        monkeypatch.setenv(settings.llm.api_key_env, "sk-fournisseur")
        runner = ShellRunner(settings, FileManager(settings))
        code = (
            "import os;"
            "print(os.environ.get('OMNIAGENT_TEST_SECRET','absent'));"
            "print(os.environ.get('DEEPSEEK_API_KEY','absent'))"
        )
        result = runner.run(f'"{sys.executable}" -c "{code}"', cwd=str(tmp))

        assert result["returncode"] == 0, result
        assert "absent" in result["stdout"]
        assert "s3cret-value" not in result["stdout"]
        assert "sk-fournisseur" not in result["stdout"]


def test_timeout_kills_the_shell_tree() -> None:
    with tmpdir("shell-timeout") as tmp:
        settings = make_settings(tmp)
        runner = ShellRunner(settings, FileManager(settings))
        started = time.perf_counter()
        try:
            runner.run(
                f'"{sys.executable}" -c "import time; time.sleep(30)"',
                cwd=str(tmp),
                timeout_s=1,
            )
        except ToolError as exc:
            assert exc.code == "command_timeout", exc
        else:  # pragma: no cover - le délai doit toujours couper
            raise AssertionError("la commande lente aurait dû être interrompue")
        assert time.perf_counter() - started < 15


def test_status_admits_there_is_no_system_sandbox() -> None:
    with tmpdir("shell-statut") as tmp:
        settings = make_settings(tmp)
        status = ShellRunner(settings, FileManager(settings)).status()

        assert status["environment_scrubbed"] is True
        assert status["process_tree_killed_on_timeout"] is True
        assert status["system_sandbox"] is False


if __name__ == "__main__":
    from support import run

    sys.exit(run(globals()))
