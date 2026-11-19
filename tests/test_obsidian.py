"""Cerveau Obsidian : notes, liens, mémorisation automatique et bootstrap."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

import tools.obsidian as obsidian  # noqa: E402
from core.events import Event  # noqa: E402
from core.factory import build_agent  # noqa: E402
from core.tool_registry import ToolRegistry  # noqa: E402
from tools.base import FunctionTool, ToolResult  # noqa: E402
from tools.obsidian import ObsidianBrain, ensure_obsidian, install_obsidian  # noqa: E402


class _FakeLLM:
    supports_stream = False

    def complete(self, *args, **kwargs):
        raise AssertionError("le test ne doit pas appeler le fournisseur")

    def close(self) -> None:
        pass


def _enabled(settings):
    settings.obsidian.enabled = True
    settings.obsidian.auto_install = False
    return settings


def test_vault_creates_index_and_interconnected_notes() -> None:
    with tmpdir("obsidian-vault") as tmp:
        settings = _enabled(make_settings(tmp))
        brain = ObsidianBrain(settings)

        assert (brain.root / "00_CORTEX_INDEX.md").is_file()
        assert {folder for folder in obsidian.FOLDERS} <= {
            path.name for path in brain.root.iterdir() if path.is_dir()
        }
        note = brain.remember(
            "Projet Atlas",
            "Le budget du projet Atlas est de 42 EUR.",
            tags=["projet", "budget"],
            links=["00_CORTEX_INDEX", "Fournisseur"],
        )
        assert Path(note["path"]).is_file()
        text = Path(note["path"]).read_text(encoding="utf-8")
        assert "[[00_CORTEX_INDEX]]" in text
        assert "[[Fournisseur]]" in text
        assert "#projet" in text and "#budget" in text

        index = (brain.root / "00_CORTEX_INDEX.md").read_text(encoding="utf-8")
        assert "[[Projet Atlas]]" in index
        assert "02_Knowledge/projet-atlas.md" in index
        hits = brain.search("budget")
        assert hits and hits[0]["title"] == "Projet Atlas"
        assert "42 EUR" in hits[0]["excerpt"]


def test_tool_results_become_notes_but_sensitive_results_are_ignored() -> None:
    with tmpdir("obsidian-auto") as tmp:
        settings = _enabled(make_settings(tmp))
        brain = ObsidianBrain(settings)
        result = ToolResult(
            ok=True,
            data={"url": "https://example.com/atlas", "text": "ATLAS budget 42"},
        )
        remembered = brain.remember_tool_result(
            "web_fetch", {"url": "https://example.com/atlas"}, result
        )
        assert remembered is not None
        assert Path(remembered["path"]).is_file()
        assert "ATLAS budget 42" in Path(remembered["path"]).read_text(encoding="utf-8")
        assert brain.search("ATLAS")

        secret = ToolResult(ok=True, data={"password": "secret"}, sensitive=True)
        assert brain.remember_tool_result("web_fetch", {}, secret) is None
        assert brain.remember_tool_result("fs_write_text", {}, result) is None


def test_final_run_is_journaled_and_indexed() -> None:
    with tmpdir("obsidian-journal") as tmp:
        settings = _enabled(make_settings(tmp))
        brain = ObsidianBrain(settings)
        brain.on_event(
            Event(type="run_started", run_id="run-1", payload={"task": "préparer Atlas"})
        )
        brain.on_event(
            Event(
                type="final",
                run_id="run-1",
                payload={"text": "Le rapport Atlas est prêt."},
            )
        )
        journal = list((brain.root / "04_Journal").glob("*.md"))
        assert len(journal) == 1
        text = journal[0].read_text(encoding="utf-8")
        assert "préparer Atlas" in text and "Le rapport Atlas est prêt." in text
        assert "[[00_CORTEX_INDEX]]" in text


def test_registry_hook_persists_a_real_tool_result() -> None:
    with tmpdir("obsidian-hook") as tmp:
        settings = _enabled(make_settings(tmp))
        brain = ObsidianBrain(settings)
        tool = FunctionTool(
            "web_search",
            "recherche",
            {"type": "object", "properties": {}},
            lambda: {"results": [{"title": "Atlas", "url": "https://example.com"}]},
        )
        registry = ToolRegistry([tool], on_result=brain.remember_tool_result)
        assert registry.dispatch("web_search", {}).ok
        assert brain.search("Atlas")


def test_build_agent_wires_obsidian_tools_and_automatic_memory() -> None:
    with tmpdir("obsidian-agent") as tmp:
        settings = _enabled(make_settings(tmp))
        source = tmp / "atlas.txt"
        source.write_text("Le budget ATLAS est 42.", encoding="utf-8")
        bundle = build_agent(
            settings, llm=_FakeLLM(), with_web=False, with_browser=False
        )
        try:
            assert {"obsidian_remember", "obsidian_search", "obsidian_status"} <= set(
                bundle.tool_names()
            )
            assert bundle.brain is not None
            assert bundle.registry.dispatch(
                "fs_read_text", {"path": str(source)}
            ).ok
            bundle.agent.bus.emit(
                "run_started", {"task": "analyser Atlas"}, run_id="run-test"
            )
            bundle.agent.bus.emit(
                "final", {"text": "Atlas utilise 42."}, run_id="run-test"
            )
            assert list((bundle.brain.root / "04_Journal").glob("*.md"))
        finally:
            bundle.close()
        assert bundle.brain is not None
        assert bundle.brain.search("ATLAS")


def test_install_uses_winget_and_rechecks_the_executable(monkeypatch) -> None:
    with tmpdir("obsidian-install") as tmp:
        settings = _enabled(make_settings(tmp))
        installed = [False]

        def find():
            return Path(r"C:\Obsidian\Obsidian.exe") if installed[0] else None

        def which(name):
            return r"C:\winget.exe" if name == "winget" else None

        calls: list[list[str]] = []

        def runner(command, **kwargs):
            calls.append(command)
            installed[0] = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(obsidian, "find_obsidian", find)
        monkeypatch.setattr(obsidian.shutil, "which", which)
        ok, detail = install_obsidian(settings, runner=runner)
        assert ok and "installé" in detail
        assert calls and calls[0][:3] == [r"C:\winget.exe", "install", "--id"]
        assert "Obsidian.Obsidian" in calls[0]


def test_install_is_not_attempted_when_auto_install_is_off(monkeypatch) -> None:
    with tmpdir("obsidian-off") as tmp:
        settings = _enabled(make_settings(tmp))
        settings.obsidian.auto_install = False
        monkeypatch.setattr(obsidian, "find_obsidian", lambda: None)
        ok, detail = ensure_obsidian(settings)
        assert not ok and "désactivée" in detail


if __name__ == "__main__":
    from support import run

    sys.exit(run(globals()))
