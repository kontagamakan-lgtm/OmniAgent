"""Cortex évolutif : validation des compétences, forge testée, instantanés."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

from core.factory import build_agent  # noqa: E402
from core.self_evolver import (  # noqa: E402
    SelfEvolver,
    build_evolution_tools,
    format_evolution_journal,
    format_evolution_status,
)
from core.skill_loader import (  # noqa: E402
    SkillError,
    SkillLoader,
    validate_skill_source,
)
from tools.obsidian import ObsidianBrain  # noqa: E402


class _FakeLLM:
    supports_stream = False

    def __init__(self, texts: list[str] | None = None) -> None:
        self.texts = list(texts or [])
        self.messages: list[list[dict[str, object]]] = []

    def complete(self, messages, tools=None, **kwargs):
        self.messages.append(messages)
        text = self.texts.pop(0) if self.texts else ""
        return SimpleNamespace(text=text)

    def close(self) -> None:
        pass


_SKILL_CODE = '''
NAME = "word_count"
DESCRIPTION = "Compte les mots d'un texte."
PARAMETERS = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}


def run(text):
    return {"words": len(text.split())}
'''

_SKILL_TEST = """
import word_count as skill

assert skill.run("deux mots")["words"] == 2
assert skill.run("")["words"] == 0
"""


def _payload(*, test: str = _SKILL_TEST, name: str = "word_count") -> str:
    return json.dumps(
        {
            "name": name,
            "description": "Compte les mots d'un texte.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            "code": _SKILL_CODE,
            "test": test,
            "pip": [],
        }
    )


def _evolver(
    tmp: Path,
    texts: list[str],
    *,
    registry: object | None = None,
    clock: object | None = None,
    brain: object | None = None,
) -> tuple[SelfEvolver, SkillLoader, _FakeLLM]:
    settings = make_settings(tmp)
    settings.evolution.enabled = True
    settings.evolution.allow_network = False
    settings.evolution.core_dir = str(Path(tmp) / "evolution_core")
    settings.evolution.test_timeout_s = 30
    loader = SkillLoader(
        Path(tmp) / "evolution_core" / "skills",
        max_chars=settings.evolution.max_skill_chars,
    )
    llm = _FakeLLM(texts)
    evolver = SelfEvolver(
        settings,
        loader,
        agent=SimpleNamespace(llm=llm),
        registry=registry
        if registry is not None
        # Un outil lent et instable : c'est ce que le diagnostic doit signaler.
        else SimpleNamespace(
            stats=lambda: {
                "web_search": {"calls": 3, "failures": 1, "total_ms": 900}
            }
        ),
        core_dir=Path(tmp) / "evolution_core",
        data_dir=Path(tmp) / "evolution",
        clock=clock,
    )
    if brain is not None:
        evolver.brain = brain
    return evolver, loader, llm


# -- chargeur ---------------------------------------------------------------


def test_skill_source_rejects_forbidden_imports_calls_and_missing_run() -> None:
    with pytest.raises(SkillError):
        validate_skill_source("import subprocess\n\ndef run():\n    return 1\n")
    with pytest.raises(SkillError):
        validate_skill_source("def run():\n    eval('1')\n")
    with pytest.raises(SkillError):
        validate_skill_source("VALUE = 1\n")


def test_loader_loads_runs_and_hot_reloads_a_skill() -> None:
    with tmpdir("evolution-loader") as tmp:
        skills = Path(tmp) / "skills"
        skills.mkdir()
        path = skills / "echo.py"
        path.write_text(
            'NAME = "echo"\nDESCRIPTION = "renvoie une valeur"\n'
            'PARAMETERS = {"type": "object", "properties": {}}\n'
            "def run():\n    return {'value': 1}\n",
            encoding="utf-8",
        )
        loader = SkillLoader(skills)
        loader.load(path)
        assert loader.run("echo") == {"value": 1}
        assert loader.names() == ["skill_echo"]

        path.write_text(
            'NAME = "echo"\nDESCRIPTION = "renvoie une valeur"\n'
            'PARAMETERS = {"type": "object", "properties": {}}\n'
            "def run():\n    return {'value': 2, 'reloaded': True}\n",
            encoding="utf-8",
        )
        loader.load(path)
        assert loader.run("echo")["value"] == 2
        loader.unload("echo")
        assert loader.names() == []


def test_loader_ignores_a_broken_skill_without_crashing_the_others() -> None:
    with tmpdir("evolution-loader-broken") as tmp:
        skills = Path(tmp) / "skills"
        skills.mkdir()
        (skills / "broken.py").write_text("def run(:\n", encoding="utf-8")
        (skills / "good.py").write_text(
            'NAME = "good"\nDESCRIPTION = "ok"\n'
            'PARAMETERS = {"type": "object", "properties": {}}\n'
            "def run():\n    return 'ok'\n",
            encoding="utf-8",
        )
        loader = SkillLoader(skills)
        tools = loader.load_all()
        assert [tool.name for tool in tools] == ["skill_good"]
        assert "broken" in loader.errors


# -- moteur -----------------------------------------------------------------


def test_cycle_forges_a_tested_skill_and_updates_the_learning_prompt() -> None:
    with tmpdir("evolution-cycle") as tmp:
        evolver, loader, _llm = _evolver(
            tmp, [_payload(), "- corriger les erreurs de parsing\n- préférer les boucles simples"]
        )
        result = evolver.run_cycle()

        assert result["ok"], result
        assert result["changed"] is True
        assert result["skill"]["name"] == "word_count"
        assert (Path(tmp) / "evolution_core" / "skills" / "word_count.py").is_file()
        assert "skill_word_count" in loader.names()
        assert loader.run("word_count", {"text": "deux mots"})["words"] == 2
        assert evolver.status()["version"].startswith("v1.0.1")
        assert "Auto-Générée" in evolver.status()["version"]
        prompt = (
            Path(tmp) / "evolution_core" / "prompts" / "adaptations.md"
        ).read_text(encoding="utf-8")
        assert "préférer les boucles simples" in prompt
        records = evolver.journal()
        assert any(
            record.get("type") == "skill_forged" and record.get("ok")
            for record in records
        )
        assert any(record.get("type") == "cycle_finished" and record.get("ok") for record in records)


def test_router_ranking_is_journaled_after_a_cycle() -> None:
    class _FakeRouter:
        def summary(self):
            return {"current_model": "gpt-5", "keys": 2, "states": {"ACTIVE": 2}}

        def optimize_scores(self):
            return [
                {
                    "model": "gpt-5",
                    "label": "GPT-5",
                    "provider": "openai",
                    "score": 100,
                    "base_score": 98,
                    "successes": 4,
                    "failures": 0,
                    "tokens_per_s": 55.0,
                }
            ]

    with tmpdir("evolution-router") as tmp:
        evolver, _loader, _llm = _evolver(tmp, [_payload(), "- une leçon"])
        evolver.attach_router(_FakeRouter())
        assert evolver.run_cycle()["ok"]
        records = evolver.journal()
        diagnosis = next(
            record for record in records if record.get("type") == "diagnosis"
        )
        assert diagnosis["llm_router"]["current_model"] == "gpt-5"
        ranking = [
            record for record in records if record.get("type") == "llm_ranking"
        ]
        assert ranking and ranking[-1]["top"][0]["model"] == "gpt-5"


def test_failed_test_does_not_bump_the_version() -> None:
    with tmpdir("evolution-no-bump") as tmp:
        bad_test = 'import word_count as skill\n\nassert skill.run("deux mots")["words"] == 99\n'
        evolver, _loader, _llm = _evolver(
            tmp, [_payload(test=bad_test), "- le test doit correspondre au code"]
        )
        assert evolver.status()["version"] == "v1.0.0 - Initiale"

        result = evolver.run_cycle()
        assert result["ok"]
        assert result["changed"] is False
        assert result["skill"] is None
        assert evolver.status()["version"] == "v1.0.0 - Initiale"
        assert evolver.status()["cycles"] == 1
        assert evolver.journal()[-1]["type"] == "cycle_finished"
        assert evolver.journal()[-1]["changed"] is False


def test_cycle_without_diagnostic_signal_skips_the_forge() -> None:
    with tmpdir("evolution-no-signal") as tmp:
        quiet = SimpleNamespace(stats=lambda: {})
        evolver, _loader, llm = _evolver(tmp, ["- rien à corriger"], registry=quiet)

        result = evolver.run_cycle()
        assert result["ok"]
        assert result["changed"] is False
        assert result["skill"] is None
        # Un seul appel : les leçons, pas de demande de compétence.
        assert len(llm.messages) == 1
        assert evolver.status()["version"] == "v1.0.0 - Initiale"
        assert not list(
            (Path(tmp) / "evolution_core" / "skills").glob("*.py")
        )
        assert any(
            record.get("type") == "forge_skipped"
            for record in evolver.journal()
        )


def test_failing_skill_is_rolled_back_and_journaled() -> None:
    with tmpdir("evolution-rollback") as tmp:
        bad_test = 'import word_count as skill\n\nassert skill.run("deux mots")["words"] == 99\n'
        evolver, loader, _llm = _evolver(
            tmp, [_payload(test=bad_test), "- le test doit correspondre au code"]
        )
        result = evolver.run_cycle()

        assert result["ok"], "le cycle reste utile même si la compétence est refusée"
        assert not (Path(tmp) / "evolution_core" / "skills" / "word_count.py").exists()
        assert "skill_word_count" not in loader.names()
        refused = [
            record
            for record in evolver.journal()
            if record.get("type") == "skill_forged"
        ]
        assert refused and refused[-1]["ok"] is False
        assert "test en échec" in refused[-1]["error"]
        assert list((Path(tmp) / "evolution" / "backups").iterdir())


def test_cycle_refuses_pip_requests_and_invalid_skill_names() -> None:
    with tmpdir("evolution-refusals") as tmp:
        payload = json.loads(_payload())
        payload["pip"] = ["requests"]
        evolver, loader, _llm = _evolver(
            tmp, [json.dumps(payload), "- aucune leçon"]
        )
        assert evolver.run_cycle()["ok"]
        assert "skill_word_count" not in loader.names()
        refused = [
            record
            for record in evolver.journal()
            if record.get("type") == "skill_forged"
        ]
        assert refused and "pip" in refused[-1]["error"]


def test_allow_pip_still_rejects_an_invalid_package_name() -> None:
    with tmpdir("evolution-pip") as tmp:
        payload = json.loads(_payload())
        payload["pip"] = ["../../evil"]
        evolver, loader, _llm = _evolver(
            tmp, [json.dumps(payload), "- aucune leçon"]
        )
        evolver.settings.evolution.allow_pip = True
        assert evolver.run_cycle()["ok"]
        assert "skill_word_count" not in loader.names()
        refused = [
            record
            for record in evolver.journal()
            if record.get("type") == "skill_forged"
        ]
        assert refused and "paquet" in refused[-1]["error"]


class _ManualClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_inactive_loop_is_due_only_after_idle_and_outside_interval() -> None:
    with tmpdir("evolution-due") as tmp:
        clock = _ManualClock()
        evolver, _loader, _llm = _evolver(tmp, [], clock=clock)
        evolver.settings.evolution.idle_minutes = 10.0
        evolver.settings.evolution.max_cycles_per_day = 2
        evolver._last_activity = clock.now
        evolver._started = clock.now
        evolver._next_cycle = 0.0

        assert evolver._due() is False
        clock.now += 10 * 60 - 1
        assert evolver._due() is False
        clock.now += 1
        assert evolver._due() is True

        evolver._next_cycle = clock.now + 30
        assert evolver._due() is False
        clock.now += 31
        assert evolver._due() is True

        evolver._state.cycles_today = 2
        assert evolver._due() is False


def test_background_thread_starts_and_stops_cleanly() -> None:
    with tmpdir("evolution-thread") as tmp:
        evolver, _loader, _llm = _evolver(tmp, [])
        evolver.settings.evolution.idle_minutes = 1440.0
        assert evolver.start() is True
        thread = evolver._thread
        assert thread is not None and thread.is_alive()
        assert thread.name == "omniagent-evolution"

        evolver.stop()
        assert evolver._thread is None
        assert not thread.is_alive()
        assert evolver.start() is True
        evolver.stop()


def test_bundle_close_leaves_no_evolution_thread_alive() -> None:
    with tmpdir("evolution-bundle-close") as tmp:
        settings = make_settings(tmp)
        settings.evolution.enabled = True
        settings.evolution.allow_network = False
        settings.evolution.idle_minutes = 1440.0
        settings.evolution.core_dir = str(Path(tmp) / "evolution_core")
        bundle = build_agent(
            settings, llm=_FakeLLM(), with_web=False, with_browser=False
        )
        evolver = bundle.evolver
        assert evolver is not None
        try:
            assert evolver.start() is True
            thread = evolver._thread
            assert thread is not None and thread.is_alive()
            bundle.close()
            assert evolver._thread is None
            assert not thread.is_alive()
            assert not any(
                item.name == "omniagent-evolution" and item.is_alive()
                for item in threading.enumerate()
            )
        finally:
            bundle.close()


def test_stop_joins_an_automatic_cycle_in_progress() -> None:
    """Un délai borné laisserait l'auto-cycle écrire après l'arrêt."""
    with tmpdir("evolution-auto-close") as tmp:
        evolver, _loader, _llm = _evolver(tmp, ["- une leçon"])
        evolver.settings.evolution.idle_minutes = 0.01
        evolver._started = evolver._clock() - 60
        evolver._last_activity = evolver._started
        evolver._next_cycle = 0.0

        entered = threading.Event()
        release = threading.Event()

        def blocked_diagnosis() -> dict[str, object]:
            entered.set()
            release.wait(5)
            return {"needs_forge": False}

        evolver._diagnose = blocked_diagnosis  # type: ignore[method-assign]
        evolver.start()
        assert entered.wait(10), "l'auto-cycle doit avoir commencé"

        closer = threading.Thread(target=evolver.stop, name="test-evolution-stop")
        closer.start()
        assert evolver._stop.wait(2), "stop doit demander l'arrêt"
        time.sleep(0.2)
        assert closer.is_alive(), "stop doit joindre l'auto-cycle"

        release.set()
        closer.join(10)
        assert not closer.is_alive()
        assert not any(
            item.name.startswith("omniagent-evolution") and item.is_alive()
            for item in threading.enumerate()
        )


def test_forced_cycle_is_joined_when_bundle_closes() -> None:
    with tmpdir("evolution-manual-close") as tmp:
        settings = make_settings(tmp)
        settings.evolution.enabled = True
        settings.evolution.allow_network = False
        settings.evolution.core_dir = str(Path(tmp) / "evolution_core")
        bundle = build_agent(
            settings, llm=_FakeLLM(), with_web=False, with_browser=False
        )
        evolver = bundle.evolver
        assert evolver is not None

        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        thread = None
        closer = None

        def blocked_diagnosis() -> dict[str, object]:
            entered.set()
            release.wait(5)
            return {"needs_forge": False}

        evolver._diagnose = blocked_diagnosis  # type: ignore[method-assign]
        try:
            assert evolver.force_cycle() is True
            thread = evolver._manual_thread
            assert thread is not None and thread.is_alive()
            assert entered.wait(5), "le cycle manuel doit avoir commencé"

            def close() -> None:
                try:
                    bundle.close()
                finally:
                    closed.set()

            closer = threading.Thread(target=close, name="test-bundle-close")
            closer.start()
            assert evolver._stop.wait(2), "close doit demander l'arrêt"
            time.sleep(0.2)
            assert closer.is_alive(), "close doit joindre le cycle manuel"
        finally:
            release.set()
            if closer is not None:
                closer.join(10)
            bundle.close()

        assert closed.is_set()
        assert thread is not None and not thread.is_alive()
        assert evolver._manual_thread is None
        assert not any(
            item.name == "omniagent-evolution-manual" and item.is_alive()
            for item in threading.enumerate()
        )


def test_lessons_are_written_to_obsidian() -> None:
    with tmpdir("evolution-lessons") as tmp:
        settings = make_settings(tmp)
        brain = ObsidianBrain(settings, path=Path(tmp) / "vault")
        evolver, _loader, _llm = _evolver(
            tmp,
            [_payload(), "- corriger les erreurs de parsing\n- préférer les boucles simples"],
            brain=brain,
        )
        result = evolver.run_cycle()
        assert result["ok"], result

        vault = Path(tmp) / "vault"
        notes = sorted(vault.rglob("*.md"))
        assert notes, "aucune note écrite dans Obsidian"
        lessons = [
            path
            for path in notes
            if path.name.startswith("leçons-apprises-")
        ]
        assert lessons, [path.name for path in notes]
        text = lessons[0].read_text(encoding="utf-8")
        assert "corriger les erreurs de parsing" in text
        assert "préférer les boucles simples" in text
        assert brain.search("erreurs de parsing", limit=5)
        assert brain.status()["notes"] >= 3


def test_forced_cycles_are_capped_per_day_and_journaled() -> None:
    with tmpdir("evolution-forced") as tmp:
        evolver, _loader, _llm = _evolver(tmp, [_payload(), "- une leçon"])
        evolver.settings.evolution.max_forced_cycles_per_day = 1

        assert evolver.run_cycle(reason="manuel")["ok"]
        assert evolver.status()["forced_today"] == 1

        refused = evolver.run_cycle(reason="manuel")
        assert refused["ok"] is False
        assert "forcé" in refused["error"]
        assert evolver.status()["forced_today"] == 1
        assert evolver.force_cycle() is False
        assert "forcé" in evolver.status()["last_refusal"]
        records = [
            record
            for record in evolver.journal()
            if record.get("type") == "cycle_refused"
        ]
        assert len(records) >= 2
        assert all("forcé" in record.get("error", "") for record in records)


def test_cycle_is_refused_when_the_global_budget_is_exhausted() -> None:
    with tmpdir("evolution-budget") as tmp:
        evolver, _loader, llm = _evolver(tmp, [_payload(), "- une leçon"])
        settings = evolver.settings
        settings.budget.daily_tokens = 10
        from core.budget import record_run

        assert record_run(
            settings,
            run_id="seed-over-budget",
            model=str(settings.llm.model),
            prompt_tokens=20,
            completion_tokens=0,
        )

        result = evolver.run_cycle()
        assert result["ok"] is False
        assert "budget" in result["error"]
        assert llm.messages == [], "aucun token ne doit être dépensé"
        assert evolver.status()["cycles"] == 0
        assert any(
            record.get("type") == "cycle_refused"
            for record in evolver.journal()
        )


def test_cycle_is_refused_when_the_budget_ledger_is_unreadable() -> None:
    with tmpdir("evolution-budget-illisible") as tmp:
        evolver, _loader, llm = _evolver(tmp, [_payload(), "- une leçon"])
        ledger = evolver.settings.paths.data_dir / "budget" / "ledger.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        # Un dossier à la place du fichier rend la lecture impossible sans
        # dépendre des permissions du compte qui exécute les tests.
        ledger.mkdir()

        result = evolver.run_cycle()
        assert result["ok"] is False
        assert "budget indisponible" in result["error"]
        assert llm.messages == [], "un registre illisible ne doit pas dépenser"
        assert evolver.status()["cycles"] == 0
        assert any(
            record.get("type") == "cycle_refused"
            and "budget indisponible" in record.get("error", "")
            for record in evolver.journal()
        )


def test_evolution_tools_are_audited_and_status_is_read_only() -> None:
    with tmpdir("evolution-tools") as tmp:
        evolver, _loader, _llm = _evolver(tmp, [])
        tools = {tool.name: tool for tool in build_evolution_tools(evolver)}
        assert set(tools) == {
            "evolution_status",
            "evolution_force_cycle",
            "evolution_run_skill",
        }
        assert tools["evolution_status"].read_only
        assert not tools["evolution_force_cycle"].read_only
        assert tools["evolution_force_cycle"].dangerous
        assert tools["evolution_run_skill"].dangerous
        status = tools["evolution_status"].execute().data
        assert status["enabled"] is True


def test_engine_is_inactive_by_default_and_formats_its_panel() -> None:
    with tmpdir("evolution-disabled") as tmp:
        settings = make_settings(tmp)
        loader = SkillLoader(Path(tmp) / "skills")
        evolver = SelfEvolver(
            settings,
            loader,
            core_dir=Path(tmp) / "evolution_core",
            data_dir=Path(tmp) / "evolution",
        )
        assert evolver.status()["enabled"] is False
        assert evolver.start() is False
        assert "désactivée" in format_evolution_status(evolver.status())

    records = [
        {
            "type": "cycle_finished",
            "ok": True,
            "version": "v1.0.1 - Auto-Générée",
            "at": "2026-01-01T10:00:00+00:00",
            "duration_ms": 12.5,
        }
    ]
    assert "cycle réussi" in format_evolution_journal(records)


def test_factory_wires_evolver_skills_and_dynamic_prompt() -> None:
    with tmpdir("evolution-factory") as tmp:
        settings = make_settings(tmp)
        settings.evolution.enabled = True
        settings.evolution.allow_network = False
        settings.evolution.core_dir = str(Path(tmp) / "evolution_core")
        (Path(tmp) / "evolution_core" / "prompts").mkdir(parents=True)
        (Path(tmp) / "evolution_core" / "prompts" / "adaptations.md").write_text(
            "- une leçon utile\n", encoding="utf-8"
        )
        bundle = build_agent(
            settings, llm=_FakeLLM(), with_web=False, with_browser=False
        )
        try:
            assert bundle.evolver is not None
            assert bundle.skills is not None
            assert bundle.agent.dynamic_prompt is not None
            assert "une leçon utile" in bundle.agent.dynamic_prompt()
            assert "evolution_status" in bundle.tool_names()
        finally:
            bundle.close()


def test_factory_starts_evolution_when_enabled() -> None:
    """`evolution.enabled=true` démarre réellement l'arrière-plan dès la fabrique."""
    with tmpdir("evolution-factory-active") as tmp:
        settings = make_settings(tmp)
        settings.evolution.enabled = True
        settings.evolution.allow_network = False
        settings.evolution.idle_minutes = 1440.0
        settings.evolution.core_dir = str(Path(tmp) / "evolution_core")
        settings.evolution.max_forced_cycles_per_day = 1
        # Le premier appel est la veille Directive Première (aucune opportunité),
        # le second produit les leçons du cycle.
        llm = _FakeLLM(["[]", "- leçon issue du cycle de test"])
        bundle = build_agent(
            settings, llm=llm, with_web=False, with_browser=False
        )
        evolver = bundle.evolver
        assert evolver is not None
        try:
            assert (Path(tmp) / "evolution_core" / "skills").is_dir()
            assert evolver.status()["enabled"] is True
            assert evolver.status()["running"] is True
            thread = evolver._thread
            assert thread is not None and thread.is_alive()
            assert thread.name == "omniagent-evolution"

            assert evolver.force_cycle() is True
            deadline = time.monotonic() + 15
            records: list[dict] = []
            while time.monotonic() < deadline:
                records = evolver.journal(limit=50)
                if any(record.get("type") == "cycle_finished" for record in records):
                    break
                time.sleep(0.05)
            assert any(record.get("type") == "cycle_finished" for record in records)
            assert records[-1].get("ok") is True, records[-1]
            assert "leçon issue du cycle de test" in (
                Path(tmp) / "evolution_core" / "prompts" / "adaptations.md"
            ).read_text(encoding="utf-8")
        finally:
            bundle.close()

        assert evolver._thread is None
        assert not any(
            item.name == "omniagent-evolution" and item.is_alive()
            for item in threading.enumerate()
        )


def test_config_validates_evolution_bounds() -> None:
    with tmpdir("evolution-config") as tmp:
        settings = make_settings(tmp)
        settings.evolution.idle_minutes = 0
        settings.evolution.max_cycles_per_day = 0
        settings.evolution.max_forced_cycles_per_day = -1
        issues = settings.validate()
        assert any("evolution.idle_minutes" in str(issue) for issue in issues)
        assert any("evolution.max_cycles_per_day" in str(issue) for issue in issues)
        assert any(
            "evolution.max_forced_cycles_per_day" in str(issue) for issue in issues
        )


if __name__ == "__main__":
    from support import run

    sys.exit(run(globals()))
