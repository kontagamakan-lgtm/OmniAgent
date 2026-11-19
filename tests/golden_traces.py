"""Harnais de golden traces déterministes.

Six invariants critiques sont exercés hors ligne, avec les vrais outils, le
vrai registre, les vraies propositions et de vrais effets sur disque :

* `resume_no_effect_replay` : la reprise rejoue la lecture et ne dispatche
  jamais l'écriture, même présente dans la trace ;
* `proposal_approval` : une proposition approuvée s'exécute, s'écrit sur
  disque et se clôt en `executed` avec le `run_id` réel ;
* `cancel` : l'annulation arrête la boucle avant l'étape suivante ;
* `budget_exhausted` : deux étapes au plus, synthèse sans outil, aucun
  troisième effet ;
* `scheduled_slot_already_honored` : une fenêtre déjà honorée ne relance
  aucune session et n'ajoute aucune ligne ;
* `prompt_without_secret` : le secret du coffre n'apparaît ni dans les
  messages envoyés au modèle, ni dans la trace, ni en clair sur disque.

Chaque scénario produit deux faits comparés **exactement** à
`tests/golden/<scenario>.json` :

* `events` : la trace normalisée (`type`, `step`, `tool`, `ok`, `error_code`,
  `sensitive`, `stop_reason`) — sans horodatage, ni identifiant, ni chemin,
  donc stable d'une machine à l'autre ;
* `effects` : les effets mesurés (outils réellement dispatchés, contenu des
  fichiers, état de la proposition, compteurs d'appels au fournisseur).

    python tests/golden_traces.py              # vérifie
    python tests/golden_traces.py --update     # régénère après un changement voulu
    python tests/golden_traces.py --only cancel

Aucun réseau : le fournisseur est un script déterministe et l'agent est
construit avec `with_web=False, with_browser=False`.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from support import make_settings, tmpdir  # noqa: E402

from core.factory import build_agent  # noqa: E402
from core.fingerprint import read_fingerprint  # noqa: E402
from core.llm_client import LLMResponse  # noqa: E402
from core.autonomy import goals_path  # noqa: E402
from core.proposals import (  # noqa: E402
    ProposalStore,
    approve_proposal,
    finish_proposal,
    propose,
    proposals_path,
)
from core.react_parser import ParsedToolCall  # noqa: E402
from core.resume import build_plan, replay_read_only, resume_prompt  # noqa: E402
from core.schedule import run_scheduled, sessions_path  # noqa: E402
from core.tool_registry import ToolRegistry  # noqa: E402
from core.trace import build_trace, read_events  # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

#: Clés conservées dans la trace normalisée. Tout le reste (texte, arguments,
#: horodatage, run_id, durées) varie d'une exécution à l'autre.
CANON_KEYS = ("step", "tool", "ok", "error_code", "sensitive", "stop_reason")

SECRET = "GoldenSecret#42"
PASSPHRASE = "phrase de test suffisamment longue"


# ---------------------------------------------------------------------------
# Fournisseur scripté et outils de scénario
# ---------------------------------------------------------------------------


class ScriptedLLM:
    """Réponses imposées, une par appel, sans réseau ni aléa."""

    supports_stream = False

    def __init__(self, steps: list[Any]) -> None:
        self.steps = list(steps)
        #: Messages de chaque appel, pour vérifier ce que le modèle a reçu.
        self.calls: list[list[dict[str, Any]]] = []
        #: `tools` de chaque appel (`None` pour la synthèse de budget).
        self.tool_schemas: list[Any] = []

    def complete(self, messages, tools=None, **kwargs) -> LLMResponse:
        self.calls.append([dict(message) for message in messages])
        self.tool_schemas.append(tools)
        if not self.steps:
            raise AssertionError("script du faux fournisseur épuisé")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def close(self) -> None:  # compatibilité avec l'interface LLMClient
        return


def _call(name: str, **arguments: Any) -> LLMResponse:
    return LLMResponse(
        text="",
        tool_calls=[
            ParsedToolCall(name=name, arguments=arguments, source="golden")
        ],
        finish_reason="tool_calls",
    )


def _final(text: str = "bilan") -> LLMResponse:
    return LLMResponse(text=text, finish_reason="stop")


def _spy(registry: ToolRegistry) -> list[str]:
    """Journalise les outils réellement dispatchés, sans modifier le résultat."""
    dispatched: list[str] = []
    original = registry.dispatch

    def dispatch(name: str, arguments=None):
        dispatched.append(name)
        return original(name, arguments)

    registry.dispatch = dispatch  # type: ignore[method-assign]
    return dispatched


class Run:
    """Agent réel, fournisseur scripté et trace, dans un répertoire isolé."""

    def __init__(
        self, tmp: Path, *, llm: ScriptedLLM, trace: bool = True, **overrides: Any
    ) -> None:
        self.settings = make_settings(tmp)
        for path, value in overrides.items():
            section, _, field = path.partition(".")
            setattr(getattr(self.settings, section), field, value)
        self.llm = llm
        self.bundle = build_agent(
            self.settings, llm=llm, with_web=False, with_browser=False
        )
        self.dispatched = _spy(self.bundle.registry)
        self.trace = build_trace(self.settings) if trace else None
        self._unsubscribe = (
            self.bundle.agent.bus.subscribe(self.trace)
            if self.trace is not None
            else (lambda: None)
        )

    def close(self) -> None:
        self._unsubscribe()
        if self.trace is not None:
            self.trace.close()
        self.bundle.close()

    def trace_path(self) -> Path:
        files = sorted((self.settings.paths.log_dir / "runs").glob("*.jsonl"))
        assert files, "aucune trace écrite"
        return files[-1]

    def events(self) -> list[dict[str, Any]]:
        return canonical_events(self.trace_path())


def canonical_events(path: Path) -> list[dict[str, Any]]:
    """Trace réduite aux faits stables, sans horodatage ni arguments."""
    events: list[dict[str, Any]] = []
    for record in read_events(path):
        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        item: dict[str, Any] = {"type": str(record.get("type") or "")}
        for key in CANON_KEYS:
            if key in payload and payload[key] is not None:
                item[key] = payload[key]
        events.append(item)
    return events


def _write_fixture_trace(directory: Path, records: list[tuple[str, dict[str, Any]]]) -> Path:
    """Trace interrompue écrite à la main : le processus a été tué sans bilan."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "golden-resume.jsonl"
    lines = [
        json.dumps(
            {
                "run_id": "golden-resume",
                "timestamp": "2026-01-02T03:00:00+00:00",
                "type": event_type,
                "seq": index,
                "payload": payload,
            },
            ensure_ascii=False,
        )
        for index, (event_type, payload) in enumerate(records, start=1)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Scénarios
# ---------------------------------------------------------------------------


def resume_no_effect_replay() -> dict[str, Any]:
    """La reprise relit, ne réécrit pas, et signale le fichier modifié."""
    with tmpdir("golden-reprise") as tmp:
        note = tmp / "note.txt"
        note.write_text("v1", encoding="utf-8")
        fingerprint = read_fingerprint("fs_read_text", {"path": str(note)})
        assert fingerprint is not None
        trace_path = _write_fixture_trace(
            tmp / "runs",
            [
                (
                    "run_started",
                    {
                        "task": "ecris puis lis une note",
                        "trace_format": 3,
                        "model": "script",
                        "backend": "script",
                        "settings_digest": "golden",
                        "budget_tokens": 1000,
                        "workspace": "golden",
                    },
                ),
                ("step_started", {"index": 1}),
                (
                    "tool_started",
                    {
                        "step": 1,
                        "tool": "fs_write_text",
                        "sensitive": False,
                        "arguments": {"path": str(note), "content": "v1"},
                    },
                ),
                (
                    "tool_finished",
                    {
                        "step": 1,
                        "tool": "fs_write_text",
                        "ok": True,
                        "error_code": "",
                        "duration_ms": 1.0,
                    },
                ),
                ("step_started", {"index": 2}),
                (
                    "tool_started",
                    {
                        "step": 2,
                        "tool": "fs_read_text",
                        "sensitive": False,
                        "arguments": {"path": str(note)},
                        "fingerprint": fingerprint,
                    },
                ),
                (
                    "tool_finished",
                    {
                        "step": 2,
                        "tool": "fs_read_text",
                        "ok": True,
                        "error_code": "",
                        "duration_ms": 1.0,
                    },
                ),
            ],
        )

        # Une autre application réécrit le fichier après l'interruption.
        note.write_text("v2", encoding="utf-8")
        bundle = build_agent(
            make_settings(tmp), with_web=False, with_browser=False
        )
        dispatched = _spy(bundle.registry)
        try:
            plan = build_plan(trace_path)
            replayed = replay_read_only(plan, bundle.registry)
            prompt = resume_prompt(plan, replay=replayed)
        finally:
            bundle.close()

        return {
            "events": canonical_events(trace_path),
            "effects": {
                "dispatched": dispatched,
                "refused": [item.tool for item in replayed.refused],
                "drift": [item.drift for item in replayed.executed],
                "content_after_replay": note.read_text(encoding="utf-8"),
                "backups_created": len(list(tmp.glob("*.bak"))),
                "prompt_has_fresh_content": "v2" in prompt,
                "prompt_has_stale_content": "v1" in prompt,
            },
        }


def proposal_approval() -> dict[str, Any]:
    """Proposition → approbation → exécution réelle → clôture avec run_id."""
    with tmpdir("golden-proposition") as tmp:
        run = Run(
            tmp,
            llm=ScriptedLLM(
                [
                    _call(
                        "fs_write_text",
                        path=str(tmp / "rapport.txt"),
                        content="rapport",
                    ),
                    _final("rapport ecrit"),
                ]
            ),
        )
        try:
            goals = goals_path(run.settings)
            goals.parent.mkdir(parents=True, exist_ok=True)
            goals.write_text("- produire un rapport\n", encoding="utf-8")
            proposer = ScriptedLLM(
                [
                    LLMResponse(
                        text=json.dumps(
                            {
                                "task": "Ecrire un rapport",
                                "tools": ["fs_write_text"],
                                "risk": "faible",
                                "why": "objectif",
                            }
                        )
                    )
                ]
            )
            record, error = propose(
                run.settings, SimpleNamespace(llm=proposer)
            )
            assert record is not None, error
            pending = ProposalStore(proposals_path(run.settings)).find(record["id"])
            approved = approve_proposal(run.settings, record["id"])
            assert approved is not None

            reply = run.bundle.agent.run(str(record["task"]))
            run_id = run.bundle.agent.run_id
            finish_proposal(
                run.settings, record["id"], ok=reply.ok, run_id=run_id, detail="golden"
            )
            stored = ProposalStore(proposals_path(run.settings)).find(record["id"])
        finally:
            run.close()

        return {
            "events": run.events(),
            "effects": {
                "pending_after_proposal": pending["state"] if pending else "",
                "approved_state": approved["state"] if approved else "",
                "proposer_requests": len(proposer.calls),
                "dispatched": run.dispatched,
                "report_written": (tmp / "rapport.txt")
                .read_text(encoding="utf-8")
                if (tmp / "rapport.txt").is_file()
                else "",
                "stored_state": stored["state"] if stored else "",
                "run_id_matches": bool(run_id)
                and (stored or {}).get("run_id") == run_id,
                "pending_left": len(
                    [
                        item
                        for item in ProposalStore(
                            proposals_path(run.settings)
                        ).all()
                        if item.get("state") == "pending"
                    ]
                ),
            },
        }


def cancel() -> dict[str, Any]:
    """L'annulation après le premier effet empêche la seconde écriture."""
    with tmpdir("golden-annulation") as tmp:
        run = Run(
            tmp,
            llm=ScriptedLLM(
                [
                    _call("fs_write_text", path=str(tmp / "un.txt"), content="un"),
                    _call("fs_write_text", path=str(tmp / "deux.txt"), content="deux"),
                    _final("jamais atteint"),
                ]
            ),
        )
        original = run.bundle.registry.dispatch

        def cancel_after_first(name: str, arguments=None):
            result = original(name, arguments)
            run.bundle.agent.cancel()
            return result

        run.bundle.registry.dispatch = cancel_after_first  # type: ignore[method-assign]
        try:
            reply = run.bundle.agent.run("ecris deux fichiers")
        finally:
            run.close()

        return {
            "events": run.events(),
            "effects": {
                "dispatched": run.dispatched,
                "stop_reason": reply.stop_reason,
                "error_code": reply.error_code,
                "ok": reply.ok,
                "provider_requests": len(run.llm.calls),
                "first_file": (tmp / "un.txt").is_file(),
                "second_file": (tmp / "deux.txt").is_file(),
            },
        }


def budget_exhausted() -> dict[str, Any]:
    """Deux étapes maximum, puis une synthèse sans outil ; aucun troisième effet."""
    with tmpdir("golden-budget") as tmp:
        run = Run(
            tmp,
            llm=ScriptedLLM(
                [
                    _call("fs_write_text", path=str(tmp / "a.txt"), content="a"),
                    _call("fs_write_text", path=str(tmp / "b.txt"), content="b"),
                    _text("bilan partiel"),
                ]
            ),
            **{"agent.max_steps": 2},
        )
        try:
            reply = run.bundle.agent.run("ecris jusqu'au budget")
        finally:
            run.close()

        return {
            "events": run.events(),
            "effects": {
                "dispatched": run.dispatched,
                "files": sorted(path.name for path in tmp.glob("*.txt")),
                "stop_reason": reply.stop_reason,
                "error_code": reply.error_code,
                "ok": reply.ok,
                "provider_requests": len(run.llm.calls),
                "closing_without_tools": run.llm.tool_schemas[-1] is None,
            },
        }


def scheduled_slot_already_honored() -> dict[str, Any]:
    """Une fenêtre déjà honorée ne relance rien et n'écrit aucune ligne."""
    with tmpdir("golden-planification") as tmp:
        settings = make_settings(tmp)
        settings.autonomy.notify_on_session = False
        log = sessions_path(settings)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            json.dumps({"type": "scheduled_session", "slot": "2026-01-02"}) + "\n",
            encoding="utf-8",
        )

        class _NeverRuns:
            bus = None
            registry = None
            run_id = ""

            def __init__(self) -> None:
                self.tasks: list[str] = []

            def run(self, task: str):  # pragma: no cover - doit rester muet
                self.tasks.append(task)
                raise AssertionError("aucune session ne doit demarrer")

            def cancel(self) -> None:
                pass

            def reset_cancel(self) -> None:
                pass

        agent = _NeverRuns()
        reasons: list[str] = []
        results = run_scheduled(
            settings,
            agent,
            max_checks=1,
            now=lambda: datetime(2026, 1, 2, 2, 0),
            idle=lambda: 600.0,
            on_battery=lambda: False,
            sleep=lambda _seconds: None,
            on_event=reasons.append,
        )

        return {
            "events": [],
            "effects": {
                "results": len(results),
                "runs": len(agent.tasks),
                "session_lines": len(log.read_text(encoding="utf-8").splitlines()),
                "reason": reasons[-1] if reasons else "",
            },
        }


def prompt_without_secret() -> dict[str, Any]:
    """Le secret révélé reste local : ni prompt, ni trace, ni clair sur disque."""
    with tmpdir("golden-secret") as tmp:
        run = Run(
            tmp,
            llm=ScriptedLLM(
                [
                    _call(
                        "vault_get_entry",
                        service="github.com",
                        field_name="password",
                        reveal=True,
                    ),
                    _final("mot de passe lu"),
                ]
            ),
        )
        try:
            run.bundle.vault.create(PASSPHRASE)
            run.bundle.vault.add("github.com", "jean", SECRET)
            reply = run.bundle.agent.run("lis le mot de passe")
            trace_text = run.trace_path().read_text(encoding="utf-8")
            vault_blob = run.bundle.vault.path.read_bytes()
        finally:
            run.close()

        prompts = "\n".join(
            str(message.get("content") or "")
            for call in run.llm.calls
            for message in call
        )

        return {
            "events": run.events(),
            "effects": {
                "dispatched": run.dispatched,
                "stop_reason": reply.stop_reason,
                "secret_in_prompts": SECRET in prompts,
                "secret_in_trace": SECRET in trace_text,
                "secret_in_vault_file": SECRET.encode("utf-8") in vault_blob,
                "sensitive_observation": "[SENSIBLE]" in prompts,
                "provider_requests": len(run.llm.calls),
            },
        }


def _text(text: str) -> LLMResponse:
    return LLMResponse(text=text, finish_reason="stop")


SCENARIOS: dict[str, Callable[[], dict[str, Any]]] = {
    "resume_no_effect_replay": resume_no_effect_replay,
    "proposal_approval": proposal_approval,
    "cancel": cancel,
    "budget_exhausted": budget_exhausted,
    "scheduled_slot_already_honored": scheduled_slot_already_honored,
    "prompt_without_secret": prompt_without_secret,
}


# ---------------------------------------------------------------------------
# Comparaison et CLI
# ---------------------------------------------------------------------------


def load_golden(name: str) -> dict[str, Any]:
    path = GOLDEN_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def explain(name: str, expected: Any, actual: Any) -> str:
    """Diff lisible entre le golden et l'observation."""
    before = json.dumps(expected, indent=2, ensure_ascii=False, sort_keys=True)
    after = json.dumps(actual, indent=2, ensure_ascii=False, sort_keys=True)
    diff = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"{name}.json (golden)",
            tofile=f"{name} (observe)",
            lineterm="",
        )
    )
    return "\n".join(diff[:60]) or "aucune difference textuelle"


def run_all(only: str = "") -> dict[str, dict[str, Any]]:
    names = [only] if only else list(SCENARIOS)
    return {name: SCENARIOS[name]() for name in names}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="regenere les golden")
    parser.add_argument("--only", default="", metavar="NOM", help="un seul scenario")
    args = parser.parse_args(argv)
    if args.only and args.only not in SCENARIOS:
        print(
            f"Scenario inconnu : {args.only} (connus : {', '.join(SCENARIOS)})",
            file=sys.stderr,
        )
        return 2

    results = run_all(args.only)
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    for name, actual in results.items():
        path = GOLDEN_DIR / f"{name}.json"
        if args.update:
            path.write_text(
                json.dumps(actual, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(f"ECRIT {name}: {len(actual['events'])} evenement(s)")
            continue
        if not path.is_file():
            print(f"ECHEC {name}: golden absent ({path.name})", file=sys.stderr)
            failures += 1
            continue
        expected = json.loads(path.read_text(encoding="utf-8"))
        if expected != actual:
            failures += 1
            print(f"ECHEC {name} : la trace ou les effets ont change", file=sys.stderr)
            print(explain(name, expected, actual), file=sys.stderr)
            continue
        print(
            f"OK    {name}: {len(actual['events'])} evenement(s), "
            f"{len(actual['effects'])} effet(s)"
        )
    if args.update:
        return 0
    if failures:
        print(f"\n{failures} scenario(s) en echec", file=sys.stderr)
        return 1
    print(f"\n{len(results)} scenario(s) conformes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
