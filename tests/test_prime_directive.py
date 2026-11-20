"""Directive Première : prompt, filtre légal, veille et raccordements."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

from core.agent_core import AgentCore  # noqa: E402
from core.llm_client import LLMResponse  # noqa: E402
from core.prime_directive import (  # noqa: E402
    OPPORTUNITIES_FOLDER,
    PRIME_DIRECTIVE_PROMPT,
    PROFILE_NOTE,
    PrimeDirective,
    format_directive_dashboard,
)
from core.self_evolver import SelfEvolver  # noqa: E402
from core.skill_loader import SkillLoader  # noqa: E402
from core.tool_registry import ToolRegistry  # noqa: E402
from tools.obsidian import ObsidianBrain  # noqa: E402


class _FinalLLM:
    supports_stream = False

    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls.append([dict(message) for message in messages])
        return LLMResponse(text="réponse finale", finish_reason="stop")

    def close(self) -> None:
        pass


def test_prompt_is_explicit_and_contains_the_three_absolute_goals() -> None:
    assert "Trois objectifs absolus" in PRIME_DIRECTIVE_PROMPT
    assert "Enrichissement financier maximal" in PRIME_DIRECTIVE_PROMPT
    assert "Bien-être physique et temporel" in PRIME_DIRECTIVE_PROMPT
    assert "Épanouissement mental et émotionnel" in PRIME_DIRECTIVE_PROMPT
    assert "risque supérieur à 0" in PRIME_DIRECTIVE_PROMPT
    assert "Je comprends" not in PRIME_DIRECTIVE_PROMPT


def test_legal_filter_blocks_any_positive_risk_and_accepts_zero() -> None:
    with tmpdir("prime-score") as tmp:
        prime = PrimeDirective(make_settings(tmp))

        blocked = prime.evaluate_action_score(
            description="Optimiser une activité",
            legal_risk=1,
            expected_gain_usd=5000,
            time_saved_hours=20,
        )
        assert blocked.allowed is False
        assert blocked.score == 0
        assert "bouclier légal" in blocked.reason
        assert blocked.legal_alternative
        assert "cadre déclaré" in blocked.legal_alternative
        assert blocked.to_dict()["legal_alternative"] == blocked.legal_alternative

        missing = prime.evaluate_action_score(
            expected_gain_usd=5000,
            time_saved_hours=20,
        )
        assert missing.allowed is False

        marker = prime.evaluate_action_score(
            description="Une fraude fiscalement optimisée",
            legal_risk=0,
        )
        assert marker.allowed is False
        assert marker.legal_risk == 100
        assert "expert-comptable" in marker.legal_alternative
        assert "fraude" not in marker.legal_alternative.casefold()

        allowed = prime.evaluate_action_score(
            description="Automatiser la facturation",
            legal_risk=0,
            expected_gain_usd=600,
            time_saved_hours=2,
            hourly_value_usd=50,
            wellbeing_gain=40,
        )
        assert allowed.allowed is True
        assert allowed.score > 0
        assert allowed.legal_risk == 0
        assert allowed.legal_alternative == ""


def test_scan_writes_only_legal_opportunities_to_obsidian_and_dashboard() -> None:
    with tmpdir("prime-scan") as tmp:
        settings = make_settings(tmp)
        settings.obsidian.enabled = True
        settings.obsidian.auto_install = False
        brain = ObsidianBrain(settings)
        prime = PrimeDirective(settings, brain)
        payload = [
            {
                "title": "Automatisation factures",
                "summary": "Un script prépare les factures mensuelles.",
                "pillar": "time",
                "capital_required_usd": 0,
                "time_required_hours": 2,
                "time_saved_hours": 1.5,
                "estimated_gain_usd": 300,
                "legal_risk": 0,
                "legal_basis": "Automatisation interne licite.",
                "automation": "Script Python à valider.",
                "sources": ["https://example.test/factures"],
            },
            {
                "title": "Schéma à risque",
                "summary": "Une combine avec fraude.",
                "legal_risk": 80,
                "estimated_gain_usd": 10000,
            },
        ]
        result = prime.scan(
            lambda _prompt: json.dumps(payload),
            research=[{"query": "test", "excerpt": "source"}],
            local_files=["factures.py"],
        )

        assert result["ok"] is True
        assert result["added"] == 1
        assert result["rejected"] == 1
        assert len(result["alternatives"]) == 1
        alternative = result["alternatives"][0]
        assert alternative["legal_risk"] == 100
        assert alternative["legal_alternative"]
        alternative_note = brain.root / alternative["note"]
        assert alternative_note.is_file()
        alternative_text = alternative_note.read_text(encoding="utf-8")
        assert "Action bloquée : **Schéma à risque**" in alternative_text
        assert "## Alternative légale proposée" in alternative_text
        assert alternative["legal_alternative"] in alternative_text
        note = brain.root / result["opportunities"][0]["note"]
        assert note.is_file()
        assert note.parent.name == OPPORTUNITIES_FOLDER
        text = note.read_text(encoding="utf-8")
        assert "Capital requis : 0.00 USD" in text
        assert "Risque légal : 0/100" in text
        assert "Script Python à valider." in text
        assert "Schéma à risque" not in text

        assert (brain.root / PROFILE_NOTE).is_file()
        index = (brain.root / "00_CORTEX_INDEX.md").read_text(encoding="utf-8")
        assert "Automatisation factures" in index
        assert "Alternative légale - Schéma à risque" in index
        assert OPPORTUNITIES_FOLDER in index

        dashboard = prime.dashboard()
        assert dashboard["estimated_time_saved_hours"] == 1.5
        rendered = format_directive_dashboard(dashboard)
        assert "DIRECTIVE PREMIÈRE" in rendered
        assert "Automatisation factures" in rendered
        assert "Schéma à risque" not in rendered


def test_agent_core_injects_the_prime_directive_into_the_system_prompt() -> None:
    with tmpdir("prime-agent") as tmp:
        settings = make_settings(tmp)
        prime = PrimeDirective(settings)
        llm = _FinalLLM()
        agent = AgentCore(
            settings,
            llm,
            ToolRegistry([]),
            prime_directive=prime,
        )
        reply = agent.run("analyse une opportunité")
        assert reply.ok, reply.to_dict()
        system = str(llm.calls[0][0]["content"])
        assert "Directive Première" in system
        assert "Enrichissement financier maximal" in system
        assert "risque supérieur à 0" in system


class _FakePrime:
    def __init__(self) -> None:
        self.called = 0

    def research_queries(self):
        return ()

    def scan(self, _ask, *, research=None, local_files=None):
        self.called += 1
        return {"ok": True, "added": 0, "rejected": 0}


def test_self_evolver_runs_the_prime_scan_during_a_cycle() -> None:
    with tmpdir("prime-evolver") as tmp:
        settings = make_settings(tmp)
        settings.evolution.allow_network = False
        loader = SkillLoader(Path(tmp) / "evolution_core" / "skills")
        prime = _FakePrime()
        evolver = SelfEvolver(
            settings,
            loader,
            agent=SimpleNamespace(
                llm=SimpleNamespace(
                    complete=lambda *args, **kwargs: SimpleNamespace(text="- leçon")
                )
            ),
            registry=SimpleNamespace(stats=lambda: {}),
            core_dir=Path(tmp) / "evolution_core",
            data_dir=Path(tmp) / "evolution",
            prime_directive=prime,
        )
        result = evolver.run_cycle()
        assert result["ok"], result
        assert prime.called == 1
        assert any(
            record.get("type") == "prime_directive"
            for record in evolver.journal()
        )
